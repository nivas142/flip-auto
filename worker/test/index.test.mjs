import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.js";

class MemoryKv {
  constructor() {
    this.values = new Map();
  }
  async put(key, value) {
    this.values.set(key, value);
  }
  async get(key) {
    return this.values.get(key) ?? null;
  }
  async delete(key) {
    this.values.delete(key);
  }
}

const secret = "x".repeat(40);
const jobId = "a".repeat(64);

function callbackEnv(kv) {
  return {
    CALLBACK_SECRET: secret,
    CMA_RESULTS: kv,
    GITHUB_DISPATCH_TOKEN: "github_pat_" + "x".repeat(40),
    GITHUB_REPOSITORY: "nivas142/flip-auto",
    GITHUB_WORKFLOW: "monitor.yml",
    GITHUB_REF: "main",
  };
}

test("stores a valid callback, dispatches the monitor, and returns it securely", async (t) => {
  const kv = new MemoryKv();
  const env = callbackEnv(kv);
  const originalFetch = globalThis.fetch;
  const dispatches = [];
  globalThis.fetch = async (url, options) => {
    dispatches.push({ url, options });
    return new Response(null, { status: 204 });
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const form = new URLSearchParams({
    job_id: jobId,
    pdf_url: "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817",
  });
  const callback = await worker.fetch(
    new Request(`https://worker.example/callback/${secret}`, { method: "POST", body: form }),
    env,
  );
  assert.equal(callback.status, 204);
  assert.equal(dispatches.length, 1);
  assert.equal(
    dispatches[0].url,
    "https://api.github.com/repos/nivas142/flip-auto/actions/workflows/monitor.yml/dispatches",
  );
  assert.deepEqual(JSON.parse(dispatches[0].options.body), { ref: "main" });
  assert.equal(dispatches[0].options.headers.authorization, `Bearer ${env.GITHUB_DISPATCH_TOKEN}`);

  const result = await worker.fetch(
    new Request(`https://worker.example/results/${jobId}`, {
      headers: { authorization: `Bearer ${secret}` },
    }),
    env,
  );
  assert.equal(result.status, 200);
  assert.equal((await result.json()).pdf_url, form.get("pdf_url"));
});

test("does not dispatch the same completed job twice", async (t) => {
  const kv = new MemoryKv();
  const env = callbackEnv(kv);
  const originalFetch = globalThis.fetch;
  let dispatchCount = 0;
  globalThis.fetch = async () => {
    dispatchCount += 1;
    return new Response(null, { status: 204 });
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const form = new URLSearchParams({
    job_id: jobId,
    pdf_url: "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817",
  });

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await worker.fetch(
      new Request(`https://worker.example/callback/${secret}`, { method: "POST", body: form }),
      env,
    );
    assert.equal(response.status, 204);
  }
  assert.equal(dispatchCount, 1);
});

test("returns a retryable error when GitHub dispatch fails", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 503 });
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const form = new URLSearchParams({
    job_id: jobId,
    pdf_url: "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817",
  });
  const response = await worker.fetch(
    new Request(`https://worker.example/callback/${secret}`, { method: "POST", body: form }),
    callbackEnv(new MemoryKv()),
  );
  assert.equal(response.status, 502);
});

test("rejects callbacks from non-Cloud-CMA PDF hosts", async () => {
  const form = new URLSearchParams({ job_id: jobId, pdf_url: "https://evil.example/report.pdf" });
  const response = await worker.fetch(
    new Request(`https://worker.example/callback/${secret}`, { method: "POST", body: form }),
    { CALLBACK_SECRET: secret, CMA_RESULTS: new MemoryKv() },
  );
  assert.equal(response.status, 400);
});

test("requires bearer authentication to read results", async () => {
  const response = await worker.fetch(
    new Request(`https://worker.example/results/${jobId}`),
    { CALLBACK_SECRET: secret, CMA_RESULTS: new MemoryKv() },
  );
  assert.equal(response.status, 401);
});
