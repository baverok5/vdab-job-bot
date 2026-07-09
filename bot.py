"""
VDAB Job Bot — finds English-only, no-experience jobs and prepares applications.

How it works (runs on GitHub Actions every hour):
1. Scrapes VDAB job search results for English-language jobs
2. For each NEW job: renders the full description in a headless browser
   (VDAB is a JavaScript app + bot-protected API, so plain HTTP can't see it)
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
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- settings

SEARCH_URLS = [
    # VDAB's real job search, keyword "english" — the full result set (the
    # curated /jobs/english-jobs page only ever shows 28). Paged through in the
    # browser by collect_links(). The /jobs/english-jobs page is kept as a
    # second source so nothing curated is lost.
    "https://www.vdab.be/vindeenjob/vacatures?trefwoord=english",
    "https://www.vdab.be/vindeenjob/jobs/english-jobs",
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

MAX_NEW_PER_RUN = 0  # TEMP: skip AI this run to test pagination fast; restore after


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
    for attempt in range(4):
        try:
            r = requests.post(GEMINI_URL, json=body, timeout=90)
            if r.status_code in (429, 503):   # rate limited / busy — brief wait, retry
                # 429 (quota) deserves a longer pause than a transient 503 blip.
                time.sleep((12 if r.status_code == 429 else 5) * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if expect_json:
                text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
                return json.loads(text)
            return text
        except Exception as e:
            print(f"  Gemini error (attempt {attempt+1}): {e}")
            time.sleep(4)
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


# ---------------------------------------------------------------- scraping

def _slug_title(url):
    """Turn a VDAB job URL (.../vacatures/{id}/{slug}) into a readable title."""
    m = re.search(r"/vacatures/\d+/([^/?#]+)", url)
    if not m:
        return "Vacature"
    words = m.group(1).replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Vacature"


def _dismiss_cookies(page):
    for sel in (
        "#onetrust-accept-btn-handler",
        'button:has-text("Alle cookies aanvaarden")',
        'button:has-text("Aanvaarden")',
        'button:has-text("Accepteren")',
        'button:has-text("Accept all")',
    ):
        try:
            page.click(sel, timeout=1500)
            return
        except Exception:
            pass


NEXT_BTN = "a:has-text('Volgende'), button:has-text('Volgende')"


def collect_links(browser, search_url, cap=1400, budget_s=210, max_pages=70):
    """Walk VDAB's real search results page by page (clicking the "Volgende"
    next button) collecting (job_url, job_id) pairs. VDAB uses numbered
    pagination, not infinite scroll. Bounded by cap links / budget / max_pages."""
    page = browser.new_page(
        user_agent=HEADERS["User-Agent"],
        locale="nl-BE",
        extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
    )
    found = {}
    t0 = time.time()
    pages_done = 0
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        _dismiss_cookies(page)
        page.wait_for_timeout(1800)

        stagnant = 0
        for pages_done in range(1, max_pages + 1):
            hrefs = page.eval_on_selector_all(
                "a[href*='/vindeenjob/vacatures/']",
                "els => els.map(e => e.getAttribute('href'))",
            )
            before = len(found)
            for h in hrefs:
                m = re.search(r"/vindeenjob/vacatures/(\d+)", h or "")
                if m:
                    url = h if h.startswith("http") else "https://www.vdab.be" + h
                    found[m.group(1)] = url.split("?")[0]
            added = len(found) - before

            if len(found) >= cap or time.time() - t0 > budget_s:
                break

            nxt = page.query_selector(NEXT_BTN)
            if not nxt:
                break
            try:
                nxt.scroll_into_view_if_needed(timeout=1500)
                nxt.click(timeout=2500)
            except Exception:
                break
            page.wait_for_timeout(1500)

            if added == 0:  # a page added nothing new → we've reached the end
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0

        print(f"  collect: {len(found)} links, {pages_done} pages, {int(time.time() - t0)}s")
    except Exception as e:
        print(f"  collect error {search_url}: {e}")
    finally:
        page.close()
    return {(u, jid) for jid, u in found.items()}


def fetch_job_detail(browser, url, job_id):
    """Render one job page in a headless browser and return its readable text
    + any apply email. VDAB is a JS app with a bot-protected API, so a real
    browser is the only reliable way to see the posting."""
    page = browser.new_page(
        user_agent=HEADERS["User-Agent"],
        locale="nl-BE",
        extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
    )
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait until the SPA has actually rendered the posting (body fills up),
        # rather than the near-empty "Toepassing laden..." loading shell.
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.length > 800",
                timeout=12000,
            )
        except Exception:
            pass
        page.wait_for_timeout(800)  # let late content settle
        body_text = page.inner_text("body")
        html = page.content()
    except Exception as e:
        print(f"  render error {url}: {e}")
        page.close()
        return None, None
    page.close()

    text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
    print(f"  rendered {job_id}: {len(text)} chars of text")
    if len(text) < 300 or "Toepassing laden" in text:
        print(f"  (page did not render real content for {job_id})")
        return None, None

    apply_email = None
    mail_link = BeautifulSoup(html, "html.parser").select_one('a[href^="mailto:"]')
    if mail_link:
        apply_email = mail_link["href"].replace("mailto:", "").split("?")[0]

    return text[:15000], apply_email


# ---------------------------------------------------------------- AI steps

def evaluate_job(job_text, cv_text):
    """One Gemini call: judge if the job is open to an English speaker AND, if
    so, write the full application documents. Halves the number of API calls
    (important on the free tier). Returns the parsed JSON dict (or None)."""
    prompt = f"""You help a candidate who speaks fluent English but NOT Dutch or French.

STEP 1 — Decide if this Belgian job is open to an English speaker:
- PASS if English is required/preferred/accepted, OR the posting is written in
  English, AND Dutch or French is not strictly mandatory ("a plus" is fine).
- FAIL only if Dutch or French is genuinely required, or the role is unworkable
  without it. Do NOT judge sector, seniority, salary, or experience.

STEP 2 — ONLY if it passes, write application documents based STRICTLY on the
real CV below. Never invent experience, education, or skills not in the CV.
English, professional but warm, no clichés. If it fails, leave those fields "".

Reply ONLY with JSON:
{{
  "pass": true or false,
  "reason": "one short sentence explaining the decision",
  "title": "the job title",
  "company": "the company name or 'Unknown'",
  "location": "city or 'Unknown'",
  "match_score": 0-100 (how clearly this job is open to an English-only speaker),
  "email_subject": "short email subject line (or '')",
  "email_body": "complete application email 120-180 words ending with the signature block (or '')",
  "cover_letter": "full cover letter 250-350 words (or '')",
  "cv_highlights": "5 bullet points as one newline-separated string, most relevant CV points for THIS job (or '')"
}}

Signature block to end email_body with:
Baver Ok
+32 470 42 48 36
baverok@gmail.com
linkedin.com/in/baverok

THE REAL CV:
{cv_text}

JOB POSTING:
{job_text[:8000]}"""
    return ask_gemini(prompt, expect_json=True)


# ---------------------------------------------------------------- main

def main():
    if not GEMINI_KEY:
        raise SystemExit("GEMINI_API_KEY is not set — add it as a GitHub secret.")

    cv_text = open(CV_FILE, encoding="utf-8").read()
    seen = set(load_json(SEEN_FILE, []))
    jobs = load_json(JOBS_FILE, {"updated": "", "jobs": []})

    matched = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            print("Collecting job links from VDAB...")
            all_links = set()
            for url in SEARCH_URLS:
                links = collect_links(browser, url)
                print(f"  {len(links)} links from {url}")
                all_links |= links

            # Breadth: record EVERY English job we can see, right away — a
            # lightweight browsable listing (title derived from the URL slug),
            # independent of the slower AI "ready to apply" pipeline below.
            jobs["listing"] = sorted(
                ({"id": i, "url": u, "title": _slug_title(u)} for (u, i) in all_links),
                key=lambda j: j["id"], reverse=True,
            )[:1400]

            new_links = [(u, i) for (u, i) in all_links if i not in seen]
            print(f"{len(all_links)} total in listing, {len(new_links)} not yet seen")
            new_links = new_links[:MAX_NEW_PER_RUN]

            matched = _process_jobs(browser, new_links, seen, jobs, cv_text)
        finally:
            browser.close()

    jobs["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jobs["jobs"] = jobs["jobs"][:100]  # keep the file small
    save_json(JOBS_FILE, jobs)
    save_json(SEEN_FILE, sorted(seen))
    print(f"\nDone. {matched} new match(es) this run.")


def _process_jobs(browser, new_links, seen, jobs, cv_text):
    matched = 0
    ai_fails = 0
    for url, job_id in new_links:
        print(f"\nChecking job {job_id}: {url}")

        job_text, apply_email = fetch_job_detail(browser, url, job_id)
        if not job_text:
            print("  (could not read job — will retry next run)")
            continue  # don't mark seen; a transient render failure gets another chance

        verdict = evaluate_job(job_text, cv_text)
        if not verdict:
            ai_fails += 1
            print("  Skipped (AI call failed — will retry next run)")
            if ai_fails >= 3:
                # Gemini is down/quota-exhausted — stop wasting time this run.
                # The job listing is already saved, so breadth is unaffected.
                print("  Gemini unavailable — stopping AI for this run (listing still updated).")
                break
            continue  # Gemini hiccup; don't mark seen so it's retried
        ai_fails = 0

        # We got a real verdict (pass or fail) — safe to not process it again.
        seen.add(job_id)
        if not verdict.get("pass"):
            print(f"  FILTERED OUT: {verdict.get('reason')}")
            continue

        print(f"  MATCH ({verdict.get('match_score')}%): {verdict.get('title')}")
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
            "email_subject": verdict.get("email_subject", ""),
            "email_body": verdict.get("email_body", ""),
            "cover_letter": verdict.get("cover_letter", ""),
            "cv_highlights": verdict.get("cv_highlights", ""),
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

    return matched


if __name__ == "__main__":
    main()
