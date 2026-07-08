"""
VDAB Job Bot — finds English-only, no-experience jobs and prepares applications.

How it works (runs on GitHub Actions every hour):
1. Scrapes VDAB job search results for English-language jobs
2. For each NEW job: fetches the full description from VDAB's JSON API
3. Asks Gemini: "Does this need Dutch/French? Does it require experience?"
4. If it passes: Gemini writes a tailored cover letter + email + CV summary
5. Saves everything to docs/jobs.json (shown on the dashboard)
6. Sends a Telegram message so you know there's a new match
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings

SEARCH_URLS = [
    # VDAB search pages to monitor. Add or change keywords freely.
    "https://www.vdab.be/vindeenjob/jobs/english-jobs",
    "https://www.vdab.be/vindeenjob/jobs/digital-marketing",
    "https://www.vdab.be/vindeenjob/jobs/marketing-english",
    "https://www.vdab.be/vindeenjob/jobs/seo",
]

# VDAB is a JavaScript app; job pages have no content in their HTML. The real
# data comes from a JSON API. We try these candidate endpoints in order and
# use the first that returns usable job data. {id} is the numeric vacancy id.
API_CANDIDATES = [
    "https://www.vdab.be/api/ui/v1/vindeenjob/vacatures/{id}",
    "https://www.vdab.be/api/ui/v1/vacatures/{id}",
    "https://www.vdab.be/vindeenjob/api/v1/vacatures/{id}",
    "https://www.vdab.be/rest/vindeenjob/v3/vacatures/{id}",
]

JOBS_FILE = "docs/jobs.json"      # matched jobs (dashboard reads this)
SEEN_FILE = "seen.json"           # every job ID we already processed
CV_FILE = "cv.md"                 # your master CV

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
}

MAX_NEW_PER_RUN = 8  # safety cap so one run never floods Gemini's free tier

# Remembers which API endpoint worked, so later jobs skip straight to it.
_working_api_tmpl = None
_discovered = False  # run the JS-bundle API discovery only once per run


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def ask_gemini(prompt, expect_json=False):
    """Send a prompt to Gemini, return the text reply (or parsed JSON)."""
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(GEMINI_URL, json=body, timeout=90)
            if r.status_code in (429, 503):   # rate limited / busy — wait, retry
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if expect_json:
                text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
                return json.loads(text)
            return text
        except Exception as e:
            print(f"  Gemini error (attempt {attempt+1}): {e}")
            time.sleep(10)
    return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  (Telegram not configured, skipping notification)")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
    except Exception as e:
        print(f"  Telegram error: {e}")


def _json_to_text(obj):
    """Flatten a JSON structure into readable text (all string/number leaves)."""
    parts = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, (str, int, float)):
            s = str(o).strip()
            if s:
                parts.append(s)

    walk(obj)
    return "\n".join(parts)


# ---------------------------------------------------------------- scraping

def find_job_links(search_url):
    """Return a set of (job_url, job_id) found on one VDAB search page."""
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"  Could not load {search_url}: {e}")
        return set()

    soup = BeautifulSoup(r.text, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/vindeenjob/vacatures/(\d+)", href)
        if m:
            job_id = m.group(1)
            url = href if href.startswith("http") else "https://www.vdab.be" + href
            found.add((url.split("?")[0], job_id))

    if not found:
        # Log a snippet so we can debug if VDAB changes their page structure
        print(f"  WARNING: 0 job links found on {search_url}")
        print(f"  Page starts with: {r.text[:300]!r}")
    return found


def _discover_api(page_url):
    """One-off: download VDAB's JS bundles and print every API-looking string,
    so we can learn the exact vacancy endpoint the site itself calls."""
    try:
        html = requests.get(page_url, headers=HEADERS, timeout=60).text
    except Exception as e:
        print(f"  DISCOVER: could not load page: {e}")
        return
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    print(f"  DISCOVER: {len(scripts)} script srcs on page")
    hints = set()
    for src in scripts:
        js_url = src if src.startswith("http") else "https://www.vdab.be" + src
        try:
            js = requests.get(js_url, headers=HEADERS, timeout=60).text
        except Exception:
            continue
        for m in re.findall(
            r'["\'`]([^"\'`]*(?:vacature|/rest/|/api/|vindeenjob)[^"\'`]*)["\'`]', js
        ):
            if 3 < len(m) < 120:
                hints.add(m)
    for h in sorted(hints)[:80]:
        print(f"  DISCOVER hint: {h}")
    print(f"  DISCOVER: {len(hints)} unique hints total")


def fetch_job_detail(url, job_id):
    """Fetch one job's data from VDAB's JSON API. Returns (text, apply_email)."""
    global _working_api_tmpl, _discovered

    if not _discovered:
        _discovered = True
        _discover_api(url)

    api_headers = {**HEADERS, "Accept": "application/json", "Referer": url}

    # Try the known-good endpoint first, then fall back to the candidates.
    templates = []
    if _working_api_tmpl:
        templates.append(_working_api_tmpl)
    templates += [t for t in API_CANDIDATES if t != _working_api_tmpl]

    for tmpl in templates:
        api_url = tmpl.format(id=job_id)
        try:
            r = requests.get(api_url, headers=api_headers, timeout=60)
        except Exception as e:
            print(f"  API {api_url} -> error {e}")
            continue

        ctype = r.headers.get("content-type", "")
        print(f"  API {api_url} -> {r.status_code} ({ctype}, {len(r.text)} bytes)")

        if r.status_code == 200 and "json" in ctype.lower():
            try:
                data = r.json()
            except Exception as e:
                print(f"  (could not parse JSON: {e})")
                continue
            text = _json_to_text(data)
            if len(text) > 300:  # sanity check: real job data, not an empty stub
                _working_api_tmpl = tmpl
                m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
                apply_email = m.group(0) if m else None
                return text[:15000], apply_email

    print(f"  Could not fetch job data via API for {job_id}")
    return None, None


# ---------------------------------------------------------------- AI steps

def check_job(job_text):
    """Ask Gemini whether this job passes Baver's criteria."""
    prompt = f"""You are a strict job-filtering assistant. Analyze this Belgian job posting.

CRITERIA (ALL must be true to pass):
1. Dutch (Nederlands) is NOT required. If Dutch is listed as required, needed, or the posting is clearly aimed at Dutch speakers only, FAIL it. ("Dutch is a plus/nice to have" is OK.)
2. French (Frans) is NOT required. (Same rule: "a plus" is OK.)
3. English is required or the posting is written for English speakers.
4. Experience: either no experience required, OR the posting does not mention any years-of-experience requirement at all. If it demands 2+ years experience explicitly, FAIL it.

Reply ONLY with JSON:
{{
  "pass": true or false,
  "reason": "one short sentence explaining the decision",
  "title": "the job title",
  "company": "the company name or 'Unknown'",
  "location": "city or 'Unknown'",
  "match_score": 0-100 (how well it fits a junior digital marketer with SEO/WordPress/content skills)
}}

JOB POSTING:
{job_text[:8000]}"""
    return ask_gemini(prompt, expect_json=True)


def generate_documents(job_text, cv_text, job_info):
    """Generate tailored application documents for a matched job."""
    prompt = f"""You are an expert career writer. Write application documents for this job,
based ONLY on the real CV below. NEVER invent experience, education, or skills
that are not in the CV. Write in English, professional but warm, no clichés.

Reply ONLY with JSON:
{{
  "email_subject": "short email subject line",
  "email_body": "complete application email, 120-180 words, ready to send, ends with the signature block",
  "cover_letter": "full cover letter, 250-350 words",
  "cv_highlights": "5 bullet points (as one string, newline separated) reordering the CV's most relevant points for THIS job"
}}

THE JOB ({job_info.get('title')} at {job_info.get('company')}):
{job_text[:6000]}

THE REAL CV:
{cv_text}

Signature block to use at the end of email_body:
Baver Ok
+32 470 42 48 36
baverok@gmail.com
linkedin.com/in/baverok"""
    return ask_gemini(prompt, expect_json=True)


# ---------------------------------------------------------------- main

def main():
    if not GEMINI_KEY:
        raise SystemExit("GEMINI_API_KEY is not set — add it as a GitHub secret.")

    cv_text = open(CV_FILE, encoding="utf-8").read()
    seen = set(load_json(SEEN_FILE, []))
    jobs = load_json(JOBS_FILE, {"updated": "", "jobs": []})

    print("Collecting job links from VDAB...")
    all_links = set()
    for url in SEARCH_URLS:
        links = find_job_links(url)
        print(f"  {len(links)} links on {url}")
        all_links |= links
        time.sleep(2)  # be polite to VDAB's servers

    new_links = [(u, i) for (u, i) in all_links if i not in seen]
    print(f"{len(all_links)} total, {len(new_links)} new")
    new_links = new_links[:MAX_NEW_PER_RUN]

    matched = 0
    for url, job_id in new_links:
        print(f"\nChecking job {job_id}: {url}")
        seen.add(job_id)

        job_text, apply_email = fetch_job_detail(url, job_id)
        if not job_text:
            continue

        verdict = check_job(job_text)
        if not verdict:
            print("  Skipped (AI check failed)")
            continue
        if not verdict.get("pass"):
            print(f"  FILTERED OUT: {verdict.get('reason')}")
            continue

        print(f"  MATCH ({verdict.get('match_score')}%): {verdict.get('title')}")
        docs = generate_documents(job_text, cv_text, verdict)
        if not docs:
            print("  Skipped (document generation failed)")
            continue

        jobs["jobs"].insert(0, {
            "id": job_id,
            "url": url,
            "title": verdict.get("title", "Unknown"),
            "company": verdict.get("company", "Unknown"),
            "location": verdict.get("location", "Unknown"),
            "match_score": verdict.get("match_score", 0),
            "reason": verdict.get("reason", ""),
            "apply_email": apply_email or "",
            "found_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "status": "new",
            **docs,
        })
        matched += 1

        send_telegram(
            f"<b>New job match ({verdict.get('match_score')}%)</b>\n"
            f"{verdict.get('title')} — {verdict.get('company')}\n"
            f"{verdict.get('location')}\n\n"
            f"{verdict.get('reason')}\n\n"
            f'<a href="{url}">View on VDAB</a>\n'
            f"Documents are ready on your dashboard."
        )
        time.sleep(5)  # pace Gemini free-tier requests

    jobs["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jobs["jobs"] = jobs["jobs"][:100]  # keep the file small
    save_json(JOBS_FILE, jobs)
    save_json(SEEN_FILE, sorted(seen))
    print(f"\nDone. {matched} new match(es) this run.")


if __name__ == "__main__":
    main()
