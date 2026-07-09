# On-demand letter generation — Cloudflare Worker setup

The app's **"Write my letter"** button generates a tailored email + cover letter
for one job on demand (≈1 Gemini call, so the free tier is plenty). To do that
securely, a tiny Cloudflare Worker holds a GitHub token and triggers the
`prepare.yml` workflow. This is a **one-time, ~5-minute setup**.

## 1. Create a fine-grained GitHub token
GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate:
- **Repository access:** only `baverok5/vdab-job-bot`
- **Permissions:** *Actions* → **Read and write**, *Contents* → **Read**
- Copy the token (starts with `github_pat_…`).

## 2. Create the Worker
On https://dash.cloudflare.com → **Workers & Pages** → **Create** → **Create Worker**.
- Give it a name (e.g. `vdab-apply`), Deploy the default, then **Edit code**.
- Paste the contents of [`worker.js`](./worker.js) over the default, **Deploy**.

## 3. Add the Worker's variables
Worker → **Settings** → **Variables and Secrets**:
- Add variable `REPO` = `baverok5/vdab-job-bot` (plain text)
- Add secret `GH_PAT` = the token from step 1 (**Encrypt**)
- **Deploy** again so they take effect.

## 4. Tell the app the Worker URL
Copy the Worker URL (e.g. `https://vdab-apply.<you>.workers.dev`).
In the app, tap **"Write my letter"** on any job — it asks for the URL once and
remembers it. (It's stored only on your phone.)

## How it flows
`Write my letter` → Worker → triggers `prepare.yml` → renders that job +
writes `docs/prepared/<id>.json` → the app polls and shows the email + cover
letter with an **Open in Gmail** button. First run takes ~1–2 min (it installs a
browser); after that it's quicker.
