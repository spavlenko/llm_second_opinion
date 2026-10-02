// OpenAI-compatible chat completions client for the advisor (non-streaming).
import type { ModelEndpoint } from "./contracts.js";

export interface CompletionRequest {
  system: string;
  user: string;
  maxTokens: number | null;
  /** Sent as X-LSO-Request-Id, so the metering proxy's usage record joins the events. */
  requestId?: string;
  signal?: AbortSignal;
}

export interface Completion {
  text: string;
  outputTokens: number;
  cachedTokens: number;
  /** The provider's own counts, null when its usage leaves them out. */
  promptTokens: number | null;
  reasoningTokens: number | null;
  latencyMs: number;
}

export interface AdvisorClientLike {
  complete(req: CompletionRequest): Promise<Completion>;
}

/** A failed advisor call: the HTTP status (null when no response came back: a network error,
 * a timeout or an abort) and the time spent before it failed. */
export class AdvisorClientError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
    readonly latencyMs: number | null = null,
  ) {
    super(message);
  }
}

const HEADER_REQUEST_ID = "X-LSO-Request-Id";

/** A non-negative integer count, or null when the provider left it out. */
function count(v: unknown): number | null {
  const n = Number(v);
  return v === undefined || v === null || !Number.isFinite(n) || n < 0 ? null : Math.round(n);
}

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
  headers(requestId?: string): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    const key = this.endpoint.api_key_env ? this.env[this.endpoint.api_key_env] : undefined;
    if (key) h.Authorization = `Bearer ${key}`;
    Object.assign(h, this.endpoint.headers ?? {});
    for (const [name, variable] of Object.entries(this.endpoint.header_env ?? {})) {
      const value = this.env[variable];
      if (value !== undefined) h[name] = value;
    }
    if (requestId) h[HEADER_REQUEST_ID] = requestId;
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
    const { temperature, top_p, sampling_seed } = this.endpoint;
    if (temperature != null) body.temperature = temperature;
    if (top_p != null) body.top_p = top_p;
    if (sampling_seed != null) body.seed = sampling_seed;
    return body;
  }

  async complete(req: CompletionRequest): Promise<Completion> {
    const start = this.now();
    const timeout = AbortSignal.timeout(this.timeoutMs);
    const signal = req.signal ? AbortSignal.any([req.signal, timeout]) : timeout;
    const elapsed = () => Math.max(0, this.now() - start);
    const fail = (message: string, status: number | null) => new AdvisorClientError(message, status, elapsed());
    let res: Response;
    try {
      res = await this.fetch(this.url, {
        method: "POST",
        headers: this.headers(req.requestId),
        body: JSON.stringify(this.body(req)),
        signal,
      });
    } catch (e) {
      throw fail(`request failed: ${(e as Error).message}`, null);
    }
    const raw = await res.text().catch(() => "");
    if (!res.ok) throw fail(`HTTP ${res.status}: ${raw.slice(0, 500)}`, res.status);
    let data: any;
    try {
      data = JSON.parse(raw);
    } catch {
      throw fail(`response is not JSON: ${raw.slice(0, 200)}`, res.status);
    }
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string") throw fail(`response has no message content: ${raw.slice(0, 200)}`, res.status);
    const usage = data.usage ?? {};
    return {
      text,
      outputTokens: Number(usage.completion_tokens ?? 0) || 0,
      cachedTokens: Number(usage.prompt_tokens_details?.cached_tokens ?? 0) || 0,
      promptTokens: count(usage.prompt_tokens),
      reasoningTokens: count(usage.completion_tokens_details?.reasoning_tokens),
      latencyMs: elapsed(),
    };
  }
}
