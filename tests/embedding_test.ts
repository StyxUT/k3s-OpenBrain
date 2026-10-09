import { createEmbeddingClient, type EmbeddingConfig } from "../app/embedding.ts";

function assert(value: unknown, message: string) {
  if (!value) throw new Error(message);
}

const vector = () => Response.json({ data: [{ embedding: [0.1, 0.2] }] });
const defaults = {
  primaryBase: "http://gpu.invalid/v1",
  primaryKey: "test-only",
  fallbackBase: "http://cpu.invalid/v1",
  model: "test-model",
  dimensions: 2,
};
const client = (options: Partial<EmbeddingConfig>) => createEmbeddingClient({ ...defaults, ...options });

async function rejects(operation: Promise<unknown>, expected: string) {
  let message = "";
  try { await operation; } catch (error) { message = String(error); }
  assert(message.includes(expected), `Expected ${expected}, got ${message}`);
}

Deno.test("GPU success uses the configured model and never calls CPU", async () => {
  let calls = 0;
  const c = client({ fetcher: async (url, init) => {
    calls++;
    assert(String(url) === "http://gpu.invalid/v1/embeddings", "wrong primary");
    assert(init?.signal instanceof AbortSignal, "missing deadline");
    const body = JSON.parse(String(init?.body));
    assert(body.model === "test-model" && body.input === "thought", "request changed");
    return vector();
  } });
  assert(JSON.stringify(await c.embed("thought")) === "[0.1,0.2]", "wrong vector");
  assert(calls === 1, "unexpected fallback");
});

Deno.test("GPU timeout cancels its request and falls back to CPU", async () => {
  const calls: string[] = [];
  const c = client({ timeoutMs: 100, primaryTimeoutMs: 10, fetcher: (url, init) => {
    calls.push(String(url));
    if (String(url).includes("cpu.invalid")) return Promise.resolve(vector());
    return new Promise((_resolve, reject) => {
      init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), { once: true });
    });
  } });
  await c.embed("thought");
  assert(calls.length === 2 && calls[1].includes("cpu.invalid"), "CPU was not used");
});

Deno.test("connection failure falls back", async () => {
  const c = client({ fetcher: (url) => String(url).includes("gpu.invalid")
    ? Promise.reject(new TypeError("connection refused")) : Promise.resolve(vector()) });
  await c.embed("thought");
});

for (const status of [408, 429, 503]) {
  Deno.test(`HTTP ${status} falls back`, async () => {
    const c = client({ fetcher: async (url) => String(url).includes("gpu.invalid")
      ? new Response("unavailable", { status }) : vector() });
    await c.embed("thought");
  });
}

for (const status of [400, 401, 404]) {
  Deno.test(`HTTP ${status} fails without retrying on CPU`, async () => {
    let calls = 0;
    const c = client({ fetcher: async () => { calls++; return new Response("invalid", { status }); } });
    await rejects(c.embed("thought"), `HTTP ${status}`);
    assert(calls === 1, "configuration/input error was retried");
  });
}

Deno.test("wrong dimensions fail without fallback", async () => {
  let calls = 0;
  const c = client({ fetcher: async () => { calls++; return Response.json({ data: [{ embedding: [1] }] }); } });
  await rejects(c.embed("thought"), "invalid 2-dimensional vector");
  assert(calls === 1, "invalid model output was retried");
});

Deno.test("non-finite vectors and malformed JSON are rejected", async () => {
  const c = client({ fetcher: async () => Response.json({ data: [{ embedding: [null, 1] }] }) });
  await rejects(c.embed("thought"), "invalid 2-dimensional vector");
  const malformed = client({ fetcher: async () => new Response("not json") });
  await rejects(malformed.embed("thought"), "could not be decoded");
});

Deno.test("CPU shares the remaining deadline, not a second full timeout", async () => {
  let clock = 0;
  let calls = 0;
  const started = performance.now();
  const c = client({ timeoutMs: 50, primaryTimeoutMs: 20, now: () => clock,
    fetcher: (_url, init) => {
      calls++;
      if (calls === 1) { clock = 40; return Promise.reject(new TypeError("offline")); }
      return new Promise((_resolve, reject) => {
        init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), { once: true });
      });
    } });
  await rejects(c.embed("thought"), "fallback timed out after 10 ms");
  assert(performance.now() - started < 200, "timeout was not bounded");
});

Deno.test("slow response body is bounded and triggers fallback", async () => {
  const c = client({ timeoutMs: 100, primaryTimeoutMs: 10, fetcher: async (url, init) => {
    if (String(url).includes("cpu.invalid")) return vector();
    const body = new ReadableStream({ start(controller) {
      init!.signal!.addEventListener("abort", () => controller.error(init!.signal!.reason), { once: true });
    } });
    return new Response(body);
  } });
  await c.embed("thought");
});

Deno.test("two failures open the circuit; cooldown allows recovery", async () => {
  let clock = 0;
  let primaryCalls = 0;
  let healthy = false;
  const c = client({ now: () => clock, cooldownMs: 100, fetcher: async (url) => {
    if (String(url).includes("gpu.invalid")) {
      primaryCalls++;
      if (!healthy) throw new TypeError("offline");
    }
    return vector();
  } });
  await c.embed("thought"); await c.embed("thought"); await c.embed("thought");
  assert(primaryCalls === 2, "open circuit did not bypass GPU");
  clock = 101; healthy = true;
  await c.embed("thought"); await c.embed("thought");
  assert(primaryCalls === 4, "GPU did not recover");
});

Deno.test("both backends unavailable fail rather than return a fake vector", async () => {
  const c = client({ fetcher: async () => { throw new TypeError("offline"); } });
  await rejects(c.embed("thought"), "fallback connection failed");
});

Deno.test("only one half-open recovery probe runs at a time", async () => {
  let clock = 0;
  let primaryCalls = 0;
  let recovery = false;
  let release: (() => void) | undefined;
  const c = client({ now: () => clock, cooldownMs: 100, fetcher: (url) => {
    if (!String(url).includes("gpu.invalid")) return Promise.resolve(vector());
    primaryCalls++;
    if (!recovery) return Promise.reject(new TypeError("offline"));
    return new Promise((resolve) => { release = () => resolve(vector()); });
  } });
  await c.embed("thought"); await c.embed("thought");
  clock = 101; recovery = true;
  const probe = c.embed("thought");
  await c.embed("concurrent thought");
  assert(primaryCalls === 3, "more than one recovery probe was started");
  release!();
  await probe;
});

Deno.test("single-backend configuration remains supported", async () => {
  const c = client({ fallbackBase: undefined, fetcher: async () => vector() });
  await c.embed("thought");
});

Deno.test("native Ollama fallback forces CPU and preserves model", async () => {
  const c = client({ fallbackBase: "http://cpu.invalid/api", fallbackFormat: "ollama",
    fetcher: async (url, init) => {
      if (String(url).includes("gpu.invalid")) throw new TypeError("offline");
      assert(String(url) === "http://cpu.invalid/api/embed", "wrong CPU endpoint");
      const body = JSON.parse(String(init?.body));
      assert(body.options.num_gpu === 0, "CPU offload was not enforced");
      assert(body.model === "test-model", "fallback used a different model");
      return Response.json({ embeddings: [[0.1, 0.2]] });
    } });
  await c.embed("thought");
});

Deno.test("invalid budgets fail at startup", () => {
  let failed = false;
  try { client({ primaryTimeoutMs: 15_000 }); } catch { failed = true; }
  assert(failed, "no fallback time was reserved");
});
