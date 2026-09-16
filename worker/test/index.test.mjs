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

test("stores a valid Cloud CMA form callback and returns it securely", async () => {
  const kv = new MemoryKv();
  const env = { CALLBACK_SECRET: secret, CMA_RESULTS: kv };
  const form = new URLSearchParams({
    job_id: jobId,
    pdf_url: "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817",
  });
  const callback = await worker.fetch(
    new Request(`https://worker.example/callback/${secret}`, { method: "POST", body: form }),
    env,
  );
  assert.equal(callback.status, 204);

  const result = await worker.fetch(
    new Request(`https://worker.example/results/${jobId}`, {
      headers: { authorization: `Bearer ${secret}` },
    }),
    env,
  );
  assert.equal(result.status, 200);
  assert.equal((await result.json()).pdf_url, form.get("pdf_url"));
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
