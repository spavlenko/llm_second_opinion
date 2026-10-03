import { type IncomingMessage, type Server, type ServerResponse, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { AdvisorClient, AdvisorClientError, USER_AGENT } from "../src/client.js";
import type { ModelEndpoint } from "../src/contracts.js";

interface Seen {
  headers: IncomingMessage["headers"];
  body: any;
  url: string;
}

let server: Server | undefined;

async function serve(reply: (res: ServerResponse) => void): Promise<{ url: string; seen: Seen[] }> {
  const seen: Seen[] = [];
  server = createServer((req, res) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      seen.push({ headers: req.headers, body: JSON.parse(data), url: req.url ?? "" });
      reply(res);
    });
  });
  await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/`, seen };
}

afterEach(() => new Promise<void>((r) => (server ? server.close(() => r()) : r())));

const endpoint = (base_url: string, extra: Partial<ModelEndpoint> = {}): ModelEndpoint => ({
  base_url,
  model: "kimi-k3",
  reasoning_effort: null,
  api_key_env: null,
  headers: {},
  header_env: {},
  temperature: null,
  top_p: null,
  sampling_seed: null,
  ...extra,
});

const OK = (res: ServerResponse) => {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(
    JSON.stringify({
      choices: [{ message: { role: "assistant", content: "Check the NaN branch." }, finish_reason: "stop" }],
      usage: { prompt_tokens: 100, completion_tokens: 7, prompt_tokens_details: { cached_tokens: 64 } },
    }),
  );
};

describe("advisor client", () => {
  it("posts a non-streaming chat completion and reads text and usage", async () => {
    const { url, seen } = await serve(OK);
    const client = new AdvisorClient(endpoint(url, { reasoning_effort: "high" }), { env: {} });
    const done = await client.complete({ system: "sys", user: "brief", maxTokens: 300, requestId: "r7" });
    expect(done).toMatchObject({
      text: "Check the NaN branch.",
      outputTokens: 7,
      cachedTokens: 64,
      promptTokens: 100,
      reasoningTokens: null,
      finishReason: "stop",
    });
    expect(seen[0]!.headers["x-lso-request-id"]).toBe("r7");
    expect(done.latencyMs).toBeGreaterThanOrEqual(0);
    expect(seen[0]!.url).toBe("/v1/chat/completions");
    expect(seen[0]!.body).toEqual({
      model: "kimi-k3",
      messages: [
        { role: "system", content: "sys" },
        { role: "user", content: "brief" },
      ],
      stream: false,
      max_tokens: 300,
      reasoning_effort: "high",
    });
  });

  it("reasoning tokens from completion_tokens_details; no usage leaves the provider counts null", async () => {
    const answers = [
      { usage: { prompt_tokens: 40, completion_tokens: 90, completion_tokens_details: { reasoning_tokens: 80 } } },
      {},
    ];
    const { url, seen } = await serve((res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { content: "x" } }], ...answers.shift() }));
    });
    const client = new AdvisorClient(endpoint(url), { env: {} });
    const req = { system: "", user: "", maxTokens: null };
    expect(await client.complete(req)).toMatchObject({ promptTokens: 40, outputTokens: 90, reasoningTokens: 80 });
    expect(await client.complete(req)).toMatchObject({
      promptTokens: null,
      outputTokens: 0,
      reasoningTokens: null,
      cachedTokens: 0,
      finishReason: null,
    });
    expect(seen[1]!.headers).not.toHaveProperty("x-lso-request-id");
  });

  it("sampling params are sent when set, and only then", async () => {
    const { url, seen } = await serve(OK);
    const req = { system: "", user: "", maxTokens: null };
    await new AdvisorClient(endpoint(url, { temperature: 0, top_p: 0.9, sampling_seed: 42 }), { env: {} }).complete(req);
    await new AdvisorClient(endpoint(url, { temperature: 0.7 }), { env: {} }).complete(req);
    await new AdvisorClient(endpoint(url), { env: {} }).complete(req);
    expect(seen[0]!.body).toMatchObject({ temperature: 0, top_p: 0.9, seed: 42 });
    expect(seen[1]!.body).toMatchObject({ temperature: 0.7 });
    expect(seen[1]!.body).not.toHaveProperty("top_p");
    expect(seen[1]!.body).not.toHaveProperty("seed");
    for (const key of ["temperature", "top_p", "seed", "sampling_seed"]) expect(seen[2]!.body).not.toHaveProperty(key);
  });

  it("errors carry the HTTP status (null without a response) and the elapsed time", async () => {
    const { url } = await serve((res) => {
      res.writeHead(429);
      res.end("slow down");
    });
    const req = { system: "", user: "", maxTokens: null };
    let t = 0;
    const now = () => (t += 25);
    const http = await new AdvisorClient(endpoint(url), { env: {}, now }).complete(req).catch((e) => e);
    expect(http).toBeInstanceOf(AdvisorClientError);
    expect(http).toMatchObject({ status: 429, latencyMs: 25 });
    const net = await new AdvisorClient(endpoint("http://127.0.0.1:9/v1"), { env: {} }).complete(req).catch((e) => e);
    expect(net).toMatchObject({ status: null });
    expect(net.latencyMs).toBeGreaterThanOrEqual(0);
    const aborted = await new AdvisorClient(endpoint(url), { env: {} })
      .complete({ ...req, signal: AbortSignal.abort() })
      .catch((e) => e);
    expect(aborted).toMatchObject({ status: null });
    const bad = await new AdvisorClient(endpoint(url), { env: {}, fetch: async () => new Response("nope") })
      .complete(req)
      .catch((e) => e);
    expect(bad).toMatchObject({ status: 200 });
  });

  it("no max_tokens without a cap", async () => {
    const { url, seen } = await serve(OK);
    await new AdvisorClient(endpoint(url), { env: {} }).complete({ system: "s", user: "u", maxTokens: null });
    expect(seen[0]!.body).not.toHaveProperty("max_tokens");
  });

  it("auth from api_key_env only when set; headers and header_env values are sent", async () => {
    const { url, seen } = await serve(OK);
    const ep = endpoint(url, {
      api_key_env: "ADVISOR_KEY",
      headers: { "X-Route": "advisor" },
      header_env: { "X-Secret": "SECRET_VAR", "X-Absent": "UNSET_VAR" },
    });
    await new AdvisorClient(ep, { env: { ADVISOR_KEY: "k1", SECRET_VAR: "s1" } }).complete({ system: "", user: "", maxTokens: null });
    await new AdvisorClient(ep, { env: {} }).complete({ system: "", user: "", maxTokens: null });
    expect(seen[0]!.headers).toMatchObject({ authorization: "Bearer k1", "x-route": "advisor", "x-secret": "s1" });
    expect(seen[0]!.headers).not.toHaveProperty("x-absent");
    expect(seen[1]!.headers).not.toHaveProperty("authorization");
  });

  it("baseUrl overrides the endpoint's", async () => {
    const { url, seen } = await serve(OK);
    await new AdvisorClient(endpoint("http://unreachable.invalid/v1"), { baseUrl: url, env: {} }).complete({
      system: "",
      user: "",
      maxTokens: null,
    });
    expect(seen).toHaveLength(1);
  });

  it("HTTP errors, bad bodies, and unreachable servers are AdvisorClientErrors", async () => {
    const { url } = await serve((res) => {
      res.writeHead(503);
      res.end("overloaded");
    });
    const req = { system: "", user: "", maxTokens: null };
    await expect(new AdvisorClient(endpoint(url), { env: {} }).complete(req)).rejects.toThrow(/HTTP 503: overloaded/);
    await expect(
      new AdvisorClient(endpoint(url), { env: {}, fetch: async () => new Response("nope") }).complete(req),
    ).rejects.toThrow(AdvisorClientError);
    await expect(
      new AdvisorClient(endpoint(url), { env: {}, fetch: async () => new Response('{"choices":[]}') }).complete(req),
    ).rejects.toThrow(/no message content/);
    await expect(new AdvisorClient(endpoint("http://127.0.0.1:9/v1"), { env: {} }).complete(req)).rejects.toThrow(
      /request failed/,
    );
  });

  it("the metering proxy's token_limit refusal is an error like any other", async () => {
    const { url } = await serve((res) => {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { type: "token_limit", message: "token budget spent" } }));
    });
    const client = new AdvisorClient(endpoint(url), { env: {} });
    await expect(client.complete({ system: "", user: "", maxTokens: null })).rejects.toThrow(/HTTP 403: .*token_limit/);
  });

  it("times out", async () => {
    const { url } = await serve(() => {}); // never answers
    const client = new AdvisorClient(endpoint(url), { env: {}, timeoutMs: 50 });
    await expect(client.complete({ system: "", user: "", maxTokens: null })).rejects.toThrow(AdvisorClientError);
    server!.closeAllConnections();
  });
});

describe("client identity", () => {
  it("sends a truthful User-Agent naming the project", () => {
    const client = new AdvisorClient(
      { base_url: "http://x/v1", model: "m", reasoning_effort: null, api_key_env: null, headers: {}, header_env: {}, temperature: null, top_p: null, sampling_seed: null },
      { env: {} },
    );
    expect(client.headers()["User-Agent"]).toBe(USER_AGENT);
    expect(USER_AGENT).toMatch(/^llm-second-opinion-advisor\//);
  });
});
