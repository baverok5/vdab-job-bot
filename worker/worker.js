// Cloudflare Worker — a tiny proxy that lets the app trigger the
// "Prepare application" GitHub Action without exposing a token in the browser.
//
// It holds a fine-grained GitHub token (as the secret GH_PAT) and simply
// forwards {job_id, url} to the workflow_dispatch API.
//
// Secrets/vars to set on the Worker:
//   REPO   = "baverok5/vdab-job-bot"        (plain var)
//   GH_PAT = a fine-grained PAT for that repo with:
//              Actions: Read and write, Contents: Read   (secret)
//
// See worker/README.md for one-time deploy steps.

export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST")
      return json({ error: "POST only" }, 405, cors);

    let body;
    try { body = await request.json(); } catch { return json({ error: "bad json" }, 400, cors); }

    const job_id = String(body.job_id || "").replace(/[^0-9]/g, "");
    const url = String(body.url || "");
    if (!job_id || !/^https:\/\/www\.vdab\.be\/vindeenjob\/vacatures\//.test(url))
      return json({ error: "invalid job_id or url" }, 400, cors);

    const gh = await fetch(
      `https://api.github.com/repos/${env.REPO}/actions/workflows/prepare.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_PAT}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "vdab-job-applier",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main", inputs: { job_id, url } }),
      }
    );

    if (gh.status === 204) return json({ ok: true, job_id }, 200, cors);
    const detail = (await gh.text()).slice(0, 300);
    return json({ error: "dispatch failed", status: gh.status, detail }, 502, cors);
  },
};

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...headers, "Content-Type": "application/json" },
  });
}
