const RESULT_TTL_SECONDS = 7 * 24 * 60 * 60;
const JOB_ID_RE = /^[a-f0-9]{64}$/i;

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
      await env.CMA_RESULTS.put(
        `result:${jobId}`,
        JSON.stringify({ pdf_url: pdfUrl, received_at: new Date().toISOString() }),
        { expirationTtl: RESULT_TTL_SECONDS },
      );
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
