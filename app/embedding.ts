export interface EmbeddingConfig {
  primaryBase: string;
  primaryKey: string;
  fallbackBase?: string;
  fallbackKey?: string;
  fallbackFormat?: "openai" | "ollama";
  model: string;
  dimensions?: number;
  timeoutMs?: number;
  primaryTimeoutMs?: number;
  cooldownMs?: number;
  failureThreshold?: number;
  fetcher?: typeof fetch;
  now?: () => number;
  onEvent?: (event: Record<string, string | number>) => void;
}

class BackendError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export function createEmbeddingClient(config: EmbeddingConfig) {
  const totalMs = config.timeoutMs ?? 15_000;
  const primaryMs = config.primaryTimeoutMs ?? 5_000;
  const cooldownMs = config.cooldownMs ?? 60_000;
  const failureThreshold = config.failureThreshold ?? 2;
  const dimensions = config.dimensions ?? 4096;
  const now = config.now ?? (() => performance.now());
  const fetcher = config.fetcher ?? fetch;
  for (const value of [totalMs, primaryMs, cooldownMs, failureThreshold, dimensions]) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new Error("Embedding limits must be positive integers");
    }
  }
  if (config.fallbackBase && primaryMs >= totalMs) {
    throw new Error("GPU timeout must reserve time for CPU fallback");
  }
  // Validate configuration before accepting traffic; don't retry invalid URLs.
  for (const base of [config.primaryBase, config.fallbackBase].filter(Boolean)) {
    const url = new URL(base!);
    if (!["http:", "https:"].includes(url.protocol)) {
      throw new Error("Embedding endpoints must use HTTP or HTTPS");
    }
  }

  let failures = 0;
  let openUntil = 0;
  let probeInFlight = false;

  function emit(event: Record<string, string | number>) {
    config.onEvent?.(event);
  }

  async function request(
    backend: "primary" | "fallback",
    text: string,
    budgetMs: number,
  ): Promise<number[]> {
    if (budgetMs < 1) {
      throw new BackendError("Embedding overall deadline exhausted", true);
    }
    const started = now();
    const signal = AbortSignal.timeout(Math.floor(budgetMs));
    const base = backend === "primary" ? config.primaryBase : config.fallbackBase!;
    const key = backend === "primary" ? config.primaryKey : (config.fallbackKey ?? config.primaryKey);
    const nativeCpu = backend === "fallback" && config.fallbackFormat === "ollama";
    try {
      const response = await fetcher(`${base.replace(/\/$/, "")}/${nativeCpu ? "embed" : "embeddings"}`, {
        method: "POST",
        signal,
        headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          model: config.model,
          input: text,
          ...(nativeCpu ? { options: { num_gpu: 0 } } : {}),
        }),
      });
      if (!response.ok) {
        // Cancel rather than consume an error body that might hang or contain secrets.
        await response.body?.cancel();
        throw new BackendError(
          `Embedding ${backend} API failed: HTTP ${response.status}`,
          response.status >= 500 || response.status === 408 || response.status === 429,
        );
      }
      const data = await response.json();
      const vector = nativeCpu ? data?.embeddings?.[0] : data?.data?.[0]?.embedding;
      if (!Array.isArray(vector) || vector.length !== dimensions ||
        !vector.every((value: unknown) => typeof value === "number" && Number.isFinite(value))) {
        throw new BackendError(`Embedding ${backend} returned an invalid ${dimensions}-dimensional vector`, false);
      }
      emit({ event: "success", backend, duration_ms: Math.round(now() - started) });
      return vector;
    } catch (error) {
      if (signal.aborted) {
        throw new BackendError(`Embedding ${backend} timed out after ${Math.floor(budgetMs)} ms`, true);
      }
      if (error instanceof BackendError) throw error;
      // fetch transport failures are TypeErrors; malformed JSON is not retryable.
      if (error instanceof TypeError) {
        throw new BackendError(`Embedding ${backend} connection failed`, true);
      }
      throw new BackendError(`Embedding ${backend} response could not be decoded`, false);
    }
  }

  return {
    async embed(text: string): Promise<number[]> {
      const deadline = now() + totalMs;
      const fallbackAvailable = !!config.fallbackBase;
      // With no fallback configured, preserve the original single-backend deadline.
      if (!fallbackAvailable) return await request("primary", text, totalMs);

      const recovering = openUntil > 0;
      const tryPrimary = now() >= openUntil && (!recovering || !probeInFlight);
      if (tryPrimary) {
        if (recovering) probeInFlight = true;
        try {
          const vector = await request("primary", text, Math.min(primaryMs, deadline - now()));
          failures = 0;
          openUntil = 0;
          return vector;
        } catch (error) {
          if (!(error instanceof BackendError) || !error.retryable) throw error;
          failures++;
          emit({ event: "primary_unavailable", backend: "primary" });
          if (failures >= failureThreshold) {
            openUntil = now() + cooldownMs;
            emit({ event: "circuit_open", backend: "primary", cooldown_ms: cooldownMs });
          }
        } finally {
          if (recovering) probeInFlight = false;
        }
      } else {
        emit({ event: "circuit_bypass", backend: "fallback" });
      }
      return await request("fallback", text, deadline - now());
    },
  };
}
