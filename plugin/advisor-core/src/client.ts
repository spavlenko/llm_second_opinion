// OpenAI-compatible chat completions client for the advisor (non-streaming).
import type { ModelEndpoint } from "./contracts.js";

export interface CompletionRequest {
  system: string;
  user: string;
  maxTokens: number | null;
  signal?: AbortSignal;
}

export interface Completion {
  text: string;
  outputTokens: number;
  cachedTokens: number;
  latencyMs: number;
}

export interface AdvisorClientLike {
  complete(req: CompletionRequest): Promise<Completion>;
}

export class AdvisorClientError extends Error {}

export interface AdvisorClientOptions {
  /** Overrides the endpoint's base_url (e.g. the host as the container reaches it). */
  baseUrl?: string;
  env?: Record<string, string | undefined>;
  timeoutMs?: number;
  fetch?: typeof fetch;
  now?: () => number;
}

export class AdvisorClient implements AdvisorClientLike {
  private readonly url: string;
  private readonly env: Record<string, string | undefined>;
  private readonly timeoutMs: number;
  private readonly fetch: typeof fetch;
  private readonly now: () => number;

  constructor(
    private readonly endpoint: ModelEndpoint,
    options: AdvisorClientOptions = {},
  ) {
    this.url = `${(options.baseUrl || endpoint.base_url).replace(/\/+$/, "")}/chat/completions`;
    this.env = options.env ?? process.env;
    this.timeoutMs = options.timeoutMs ?? 300_000;
    this.fetch = options.fetch ?? globalThis.fetch;
    this.now = options.now ?? (() => performance.now());
  }

  /** Request headers: Authorization only when the key's variable is set (a metering proxy may
   * hold the credentials instead), then `headers`, then `header_env` values that are set. */
  headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    const key = this.endpoint.api_key_env ? this.env[this.endpoint.api_key_env] : undefined;
    if (key) h.Authorization = `Bearer ${key}`;
    Object.assign(h, this.endpoint.headers ?? {});
    for (const [name, variable] of Object.entries(this.endpoint.header_env ?? {})) {
      const value = this.env[variable];
      if (value !== undefined) h[name] = value;
    }
    return h;
  }

  body(req: CompletionRequest): Record<string, unknown> {
    const body: Record<string, unknown> = {
      model: this.endpoint.model,
      messages: [
        { role: "system", content: req.system },
        { role: "user", content: req.user },
      ],
      stream: false,
    };
    if (req.maxTokens) body.max_tokens = req.maxTokens;
    if (this.endpoint.reasoning_effort) body.reasoning_effort = this.endpoint.reasoning_effort;
    return body;
  }

  async complete(req: CompletionRequest): Promise<Completion> {
    const start = this.now();
    const timeout = AbortSignal.timeout(this.timeoutMs);
    const signal = req.signal ? AbortSignal.any([req.signal, timeout]) : timeout;
    let res: Response;
    try {
      res = await this.fetch(this.url, { method: "POST", headers: this.headers(), body: JSON.stringify(this.body(req)), signal });
    } catch (e) {
      throw new AdvisorClientError(`request failed: ${(e as Error).message}`);
    }
    const raw = await res.text().catch(() => "");
    if (!res.ok) throw new AdvisorClientError(`HTTP ${res.status}: ${raw.slice(0, 500)}`);
    let data: any;
    try {
      data = JSON.parse(raw);
    } catch {
      throw new AdvisorClientError(`response is not JSON: ${raw.slice(0, 200)}`);
    }
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string") throw new AdvisorClientError(`response has no message content: ${raw.slice(0, 200)}`);
    const usage = data.usage ?? {};
    return {
      text,
      outputTokens: Number(usage.completion_tokens ?? 0) || 0,
      cachedTokens: Number(usage.prompt_tokens_details?.cached_tokens ?? 0) || 0,
      latencyMs: Math.max(0, this.now() - start),
    };
  }
}
