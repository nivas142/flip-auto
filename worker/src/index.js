const RESULT_TTL_SECONDS = 7 * 24 * 60 * 60;
const JOB_ID_RE = /^[a-f0-9]{64}$/i;
const GITHUB_REPOSITORY_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const GITHUB_WORKFLOW_RE = /^[A-Za-z0-9_.-]+\.ya?ml$/;

function responseJson(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function authorized(request, env) {
  const expected = env.CALLBACK_SECRET || "";
  const supplied = request.headers.get("authorization") || "";
  return expected.length >= 32 && supplied === `Bearer ${expected}`;
}

function validPdfUrl(value) {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      (url.hostname === "cloudcma.com" || url.hostname.endsWith(".cloudcma.com"))
    );
  } catch {
    return false;
  }
}

async function callbackPayload(request) {
  const contentType = request.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return request.json();
  }
  const form = await request.formData();
  return Object.fromEntries(form.entries());
}

async function dispatchMonitor(env) {
  const repository = String(env.GITHUB_REPOSITORY || "");
  const workflow = String(env.GITHUB_WORKFLOW || "");
  const ref = String(env.GITHUB_REF || "main");
  const token = String(env.GITHUB_DISPATCH_TOKEN || "");
  if (
    !GITHUB_REPOSITORY_RE.test(repository) ||
    !GITHUB_WORKFLOW_RE.test(workflow) ||
    !ref ||
    token.length < 20
  ) {
    throw new Error("GitHub workflow dispatch is not configured");
  }

  const [owner, repo] = repository.split("/");
  const endpoint =
    `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}` +
    `/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "user-agent": "flip-auto-cma-callback",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ ref }),
  });
  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch returned HTTP ${response.status}`);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return responseJson({ ok: true });
    }

    const callbackMatch = url.pathname.match(/^\/callback\/([^/]+)$/);
    if (request.method === "POST" && callbackMatch) {
      if (!env.CALLBACK_SECRET || decodeURIComponent(callbackMatch[1]) !== env.CALLBACK_SECRET) {
        return responseJson({ error: "unauthorized" }, 401);
      }
      const payload = await callbackPayload(request);
      const jobId = String(payload.job_id || "");
      const pdfUrl = String(payload.pdf_url || "");
      if (!JOB_ID_RE.test(jobId) || !validPdfUrl(pdfUrl)) {
        return responseJson({ error: "invalid callback payload" }, 400);
      }
      const dispatchKey = `dispatch:${jobId}`;
      await env.CMA_RESULTS.put(
        `result:${jobId}`,
        JSON.stringify({ pdf_url: pdfUrl, received_at: new Date().toISOString() }),
        { expirationTtl: RESULT_TTL_SECONDS },
      );
      if (!(await env.CMA_RESULTS.get(dispatchKey))) {
        try {
          await dispatchMonitor(env);
          await env.CMA_RESULTS.put(dispatchKey, new Date().toISOString(), {
            expirationTtl: RESULT_TTL_SECONDS,
          });
        } catch (error) {
          console.error("Unable to dispatch the GitHub monitor", error);
          return responseJson({ error: "monitor dispatch failed" }, 502);
        }
      }
      return new Response(null, { status: 204 });
    }

    const resultMatch = url.pathname.match(/^\/results\/([a-f0-9]{64})$/i);
    if (resultMatch && (request.method === "GET" || request.method === "DELETE")) {
      if (!authorized(request, env)) {
        return responseJson({ error: "unauthorized" }, 401);
      }
      const key = `result:${resultMatch[1]}`;
      if (request.method === "DELETE") {
        await env.CMA_RESULTS.delete(key);
        return new Response(null, { status: 204 });
      }
      const result = await env.CMA_RESULTS.get(key);
      return result
        ? new Response(result, { headers: { "content-type": "application/json" } })
        : responseJson({ status: "pending" }, 404);
    }

    return responseJson({ error: "not found" }, 404);
  },
};
