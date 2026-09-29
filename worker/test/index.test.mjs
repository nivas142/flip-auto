import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.js";

class MemoryKv {
  constructor() {
    this.values = new Map();
    this.writes = [];
  }
  async put(key, value, options) {
    this.writes.push({ key, value, options });
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
const controlKey = "control:monitor-dispatch";

function controlRequest(method = "GET", body) {
  return new Request("https://worker.example/control/monitor-dispatch", {
    method,
    headers: { authorization: `Bearer ${secret}`, "content-type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

function callbackRequest(id = jobId) {
  return new Request(`https://worker.example/callback/${secret}`, {
    method: "POST",
    body: new URLSearchParams({
      job_id: id,
      pdf_url: "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817",
    }),
  });
}

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

test("defaults to github and persists an authenticated mode across Worker requests", async () => {
  const kv = new MemoryKv();
  const env = callbackEnv(kv);
  const initial = await worker.fetch(controlRequest(), env);
  assert.equal(initial.status, 200);
  assert.deepEqual(await initial.json(), { mode: "github" });
  assert.equal(kv.writes.length, 0);

  for (const mode of ["poll", "github"]) {
    const update = await worker.fetch(controlRequest("PUT", { mode }), env);
    assert.equal(update.status, 200);
    assert.deepEqual(await update.json(), { mode });
    const readback = await worker.fetch(controlRequest(), callbackEnv(kv));
    assert.deepEqual(await readback.json(), { mode });
    assert.deepEqual(kv.writes.at(-1), {
      key: controlKey,
      value: JSON.stringify({ mode }),
      options: undefined,
    });
  }
});

test("requires the existing minimum-length bearer secret for all control access", async () => {
  const kv = new MemoryKv();
  for (const method of ["GET", "PUT"]) {
    for (const authorization of [undefined, `Bearer wrong`, `Basic ${secret}`, secret]) {
      const response = await worker.fetch(
        new Request("https://worker.example/control/monitor-dispatch", {
          method,
          headers: authorization ? { authorization } : {},
        }),
        callbackEnv(kv),
      );
      assert.equal(response.status, 401);
    }
    for (const configured of [undefined, "", "x".repeat(31)]) {
      const response = await worker.fetch(
        new Request("https://worker.example/control/monitor-dispatch", {
          method,
          headers: { authorization: `Bearer ${configured || ""}` },
        }),
        { ...callbackEnv(kv), CALLBACK_SECRET: configured },
      );
      assert.equal(response.status, 401);
    }
  }
  assert.equal(kv.writes.length, 0);
});

test("rejects malformed or unsupported control updates without changing the mode", async () => {
  const kv = new MemoryKv();
  kv.values.set(controlKey, JSON.stringify({ mode: "poll" }));
  const env = callbackEnv(kv);
  for (const payload of [
    null, [], "github", {}, { mode: "GITHUB" }, { mode: "" }, { mode: true },
    { mode: "gcp" }, { mode: "github", extra: "unexpected" },
  ]) {
    const response = await worker.fetch(controlRequest("PUT", payload), env);
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: "invalid dispatch configuration" });
  }
  for (const [contentType, body] of [
    ["application/json", "{"],
    ["application/json", ""],
    ["text/plain", '{"mode":"github"}'],
    ["application/x-www-form-urlencoded", "mode=github"],
  ]) {
    const response = await worker.fetch(
      new Request("https://worker.example/control/monitor-dispatch", {
        method: "PUT",
        headers: { authorization: `Bearer ${secret}`, "content-type": contentType },
        body,
      }),
      env,
    );
    assert.equal(response.status, 400);
  }
  assert.equal(kv.writes.length, 0);
  assert.equal(await kv.get(controlKey), JSON.stringify({ mode: "poll" }));
});

test("poll mode keeps callbacks for seven days without a GitHub token or dispatch", async (t) => {
  const kv = new MemoryKv();
  kv.values.set(controlKey, JSON.stringify({ mode: "poll" }));
  const env = { CALLBACK_SECRET: secret, CMA_RESULTS: kv };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => assert.fail("Poll mode must not make outbound requests");
  t.after(() => { globalThis.fetch = originalFetch; });
  const callback = await worker.fetch(callbackRequest(), env);
  assert.equal(callback.status, 204);
  assert.equal(kv.writes.length, 1);
  assert.equal(kv.writes[0].key, `result:${jobId}`);
  assert.deepEqual(kv.writes[0].options, { expirationTtl: 7 * 24 * 60 * 60 });
  assert.equal(await kv.get(`dispatch:${jobId}`), null);
  const headers = { authorization: `Bearer ${secret}` };
  const readback = await worker.fetch(
    new Request(`https://worker.example/results/${jobId}`, { headers }), env,
  );
  assert.equal(readback.status, 200);
  assert.match((await readback.json()).pdf_url, /^https:\/\/cloudcma.com\//);
  const deletion = await worker.fetch(
    new Request(`https://worker.example/results/${jobId}`, { method: "DELETE", headers }), env,
  );
  assert.equal(deletion.status, 204);
  assert.equal(await kv.get(`result:${jobId}`), null);
  assert.equal(await kv.get(controlKey), JSON.stringify({ mode: "poll" }));
});

test("invalid stored control fails closed before result storage or GitHub dispatch", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => assert.fail("Invalid control must not dispatch");
  t.after(() => { globalThis.fetch = originalFetch; });
  for (const stored of ["", "invalid json", "null", '"github"', "[]", "{}",
    '{"mode":"unknown"}', '{"mode":"poll","extra":"untrusted"}']) {
    const kv = new MemoryKv();
    kv.values.set(controlKey, stored);
    const env = callbackEnv(kv);
    for (const request of [controlRequest(), callbackRequest()]) {
      const response = await worker.fetch(request, env);
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { error: "dispatch configuration unavailable" });
    }
    assert.equal(kv.writes.length, 0);
  }
});

test("KV control errors fail closed without reflecting storage details", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => assert.fail("Unavailable control must not dispatch");
  t.after(() => { globalThis.fetch = originalFetch; });
  const kv = new MemoryKv();
  kv.get = async () => { throw new Error("private storage failure"); };
  const env = callbackEnv(kv);
  for (const request of [controlRequest(), callbackRequest()]) {
    const response = await worker.fetch(request, env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: "dispatch configuration unavailable" });
  }
  assert.equal(kv.writes.length, 0);
  kv.put = async () => { throw new Error("private storage failure"); };
  const response = await worker.fetch(controlRequest("PUT", { mode: "poll" }), env);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "dispatch configuration unavailable" });
});

test("rollback resumes dispatch for polled jobs and preserves existing GitHub deduplication", async (t) => {
  const kv = new MemoryKv();
  const env = callbackEnv(kv);
  const originalFetch = globalThis.fetch;
  let dispatches = 0;
  globalThis.fetch = async () => {
    dispatches += 1;
    return new Response(null, { status: 204 });
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  assert.equal((await worker.fetch(callbackRequest(), env)).status, 204);
  assert.equal(dispatches, 1);
  await worker.fetch(controlRequest("PUT", { mode: "poll" }), env);
  const polledJobId = "b".repeat(64);
  for (const id of [jobId, polledJobId]) {
    assert.equal((await worker.fetch(callbackRequest(id), env)).status, 204);
  }
  assert.equal(dispatches, 1);
  assert.equal(await kv.get(`dispatch:${polledJobId}`), null);
  await worker.fetch(controlRequest("PUT", { mode: "github" }), env);
  for (const id of [jobId, polledJobId, polledJobId]) {
    assert.equal((await worker.fetch(callbackRequest(id), env)).status, 204);
  }
  assert.equal(dispatches, 2);
});
