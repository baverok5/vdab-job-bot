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
# Two models on purpose:
#  - EVAL is the high-volume yes/no language filter run every hour on dozens of
#    jobs. flash-lite has a much bigger free daily quota (~1000 req/day vs ~250
#    for flash), so the hourly bot stops hitting 429 quota walls by evening.
#  - WRITE is only used on demand when you actually apply, so quality matters
#    more than volume — it stays on the stronger flash model.
GEMINI_EVAL_MODEL = os.environ.get("GEMINI_EVAL_MODEL", "gemini-2.5-flash-lite")
GEMINI_WRITE_MODEL = os.environ.get("GEMINI_WRITE_MODEL", "gemini-2.5-flash")
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key=" + GEMINI_KEY
)

# DeepSeek (OpenAI-compatible) — paid but very cheap and, unlike Gemini's free
# tier, no tiny daily request cap. When a key is present it becomes the default
# engine for the high-volume job evaluation, so the bot can screen the whole
# English job set instead of ~25 jobs/day. ~$0.0005 per job → $5 ≈ 8-10k jobs.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# Which engine screens jobs / writes letters. Prefer DeepSeek for the heavy
# screening when its key exists; letters stay on Gemini when that key exists
# (on-demand + rare, so the free tier is plenty) and fall back otherwise.
EVAL_PROVIDER = os.environ.get(
    "EVAL_PROVIDER", "deepseek" if DEEPSEEK_API_KEY else "gemini")
WRITE_PROVIDER = os.environ.get(
    "WRITE_PROVIDER", "gemini" if GEMINI_KEY else "deepseek")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
}

MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "150"))  # paid engines have no tiny daily cap

# Bump this whenever the fit criteria in evaluate_job change. Saved matches that
# were judged under an older version get re-vetted (a one-time migration) so the
# pool reflects the newest rules instead of leaving stale bad matches around.
CRITERIA_VERSION = 3
REJECTED_CAP = 120    # keep the most recent "not a fit" jobs for the audit tab

# Jobs to always exclude (candidate only has a B driver's licence and does not
# want cleaning/domestic roles). Matched against the job title/slug.
EXCLUDE_RX = re.compile(
    r"poets|huishoud|schoonma|kuis|cleaner|cleaning|household\s*help|"
    r"domestic|"                                    # cleaning / household
    r"truck\s*driver|vrachtwagen|\bce[-\s]?(driver|chauffeur|truck)|"
    r"chauffeur\s*ce|rijbewijs\s*c\b|rijbewijs\s*ce|\bc/ce\b|\bce\b\s*truck",  # C/CE truck
    re.I,
)


def is_excluded(title):
    return bool(EXCLUDE_RX.search(title or ""))


# Roles the candidate clearly can't do, recognisable from the title alone:
# skilled/manual trades, licensed, medical, aviation, production-line work.
# Cheap pre-filter so we never spend the scarce Gemini quota on obvious non-fits
# — and so they can't sneak back into the pool. Nuanced cases (senior / finance /
# analyst / engineer titles) are left to evaluate_job, which actually reads the CV.
INELIGIBLE_RX = re.compile(
    r"machine\s*operator|machineoperator|production\s*(operator|worker)|"
    r"productiemedewerker|productie[-\s]?operator|meat\s*sector|slacht|"
    r"\bgrinder\b|\bwelder\b|\blasser\b|\bcnc\b|heftruck|reachtruck|forklift|"
    r"maintenance\s*technician|onderhoudstechnicus|onderhoudstechnieker|"
    r"medical\s*technologist|laborant|\bnurse\b|verpleeg|"
    r"first\s*officer|\bpilot\b|piloot|cabin\s*crew|"
    r"\bwelding\b|metaalbewerker",
    re.I,
)

INELIGIBLE_REASON = ("This role needs hands-on trade/production experience, a "
                     "licence, or a qualification your CV doesn't show.")


def is_ineligible(title):
    return bool(INELIGIBLE_RX.search(title or ""))


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


class QuotaExhausted(Exception):
    """Raised when Gemini keeps returning 429 — the daily/per-minute quota is spent,
    so retrying just burns more of it. Callers stop the run gracefully."""


def ask_gemini(prompt, expect_json=False, model=None):
    """Send a prompt to Gemini, return the text reply (or parsed JSON).

    On a 429 we retry only briefly. A 429 usually means the free-tier quota is
    exhausted, in which case hammering it 4× (the old behaviour) wasted ~2 min
    and 4 requests per job for nothing — so we raise QuotaExhausted fast and let
    the run bail while the listing/pool it already has stays intact."""
    url = GEMINI_URL_TMPL.format(model=model or GEMINI_WRITE_MODEL)
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    rate_limited = 0
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=60)
            if r.status_code == 429:            # quota / rate limit
                rate_limited += 1
                if rate_limited >= 2:           # two in a row → quota is gone
                    raise QuotaExhausted()
                time.sleep(6)                   # one short retry for a per-minute blip
                continue
            if r.status_code == 503:            # server busy — transient
                time.sleep(4 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if expect_json:
                text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
                return json.loads(text)
            return text
        except QuotaExhausted:
            raise
        except Exception as e:
            print(f"  Gemini error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return None


def ask_deepseek(prompt, expect_json=False):
    """Call DeepSeek's OpenAI-compatible chat endpoint. Returns text or parsed
    JSON. DeepSeek is paid (cheap) with no tiny daily cap, so no QuotaExhausted
    dance — a 429 here is a brief rate blip, not a wall."""
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "stream": False,
    }
    if expect_json:
        body["response_format"] = {"type": "json_object"}
    for attempt in range(4):
        try:
            r = requests.post(DEEPSEEK_URL, headers=headers, json=body, timeout=120)
            if r.status_code == 429:
                time.sleep(4 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            if expect_json:
                text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
                return json.loads(text)
            return text
        except Exception as e:
            print(f"  DeepSeek error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return None


def ask_llm(prompt, expect_json=False, provider=None, gemini_model=None):
    """Route a prompt to the chosen engine. DeepSeek for cheap high-volume
    screening; Gemini otherwise (with the caller's chosen Gemini model)."""
    provider = provider or EVAL_PROVIDER
    if provider == "deepseek" and DEEPSEEK_API_KEY:
        return ask_deepseek(prompt, expect_json)
    return ask_gemini(prompt, expect_json, model=gemini_model or GEMINI_WRITE_MODEL)


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


def collect_links(browser, search_url, cap=1400, budget_s=95, max_pages=45):
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

def generate_application(job_text, cv_text, job_info=None):
    """On-demand: write the full application email + cover letter + CV
    highlights for ONE job the user chose to apply to. Used by prepare.py."""
    job_info = job_info or {}
    prompt = f"""You are an expert career writer. Write application documents for this job,
based ONLY on the real CV below. NEVER invent experience, education, or skills
not in the CV. English, professional but warm, no clichés.

Reply ONLY with JSON:
{{
  "email_subject": "short email subject line",
  "email_body": "complete application email, 120-180 words, ready to send, ends with the signature block",
  "cover_letter": "full cover letter, 250-350 words",
  "cv_highlights": "5 bullet points (one newline-separated string) reordering the CV's most relevant points for THIS job"
}}

THE JOB ({job_info.get('title', '')} at {job_info.get('company', '')}):
{job_text[:6000]}

THE REAL CV:
{cv_text}

Signature block to end email_body with:
Baver Ok
+32 470 42 48 36
baverok@gmail.com
linkedin.com/in/baverok"""
    return ask_llm(prompt, expect_json=True, provider=WRITE_PROVIDER,
                   gemini_model=GEMINI_WRITE_MODEL)


# A blunt, honest summary of what the candidate can and cannot realistically
# apply to, so the model stops stretching ("web dev → can operate machines").
# Grounded strictly in cv.md.
CANDIDATE_PROFILE = """WHO THE CANDIDATE IS (be strict, do not stretch):
- Early-career / junior. Real experience: digital marketing & SEO intern
  (WordPress/Elementor, on-page SEO, keyword research, content writing, Google
  Analytics/Ads, SEMrush/Ahrefs), a junior front-end developer stint
  (AngularJS/JavaScript, 2018-2019), and GENERAL warehouse/logistics work
  (order-picking / high-volume handling — NOT operating production machinery).
- Coursework in Applied Computer Science (no completed degree stated).
- Languages: English (professional), Turkish (native), Dutch A2 (learning),
  no French.

WHAT THE CANDIDATE DOES NOT HAVE (jobs needing these must FAIL):
- No experience operating/setting production or CNC machines, no metalworking,
  welding, grinding, assembly, manufacturing, or skilled manual trade.
- No trade licences/certificates: no forklift/reachtruck cert, no C/CE licence,
  no electrical/mechanical/technical qualification, no nursing/medical/lab
  certification, no pilot licence, no professional finance/accounting
  certification.
- No specialised professional background: not a finance/KYC/compliance/treasury/
  tax analyst, not an engineer, not R&D, not medical/healthcare, not aviation.
- Not senior. No "Senior / Lead / Manager / Director / Head" roles and nothing
  demanding several years of dedicated professional experience."""


def evaluate_job(job_text, cv_text):
    """One Gemini call: judge whether the candidate could REALISTICALLY apply
    (language + genuine eligibility), and if so summarise the fit. If not, say
    plainly why it's not for them (why_bad). Does NOT write the email/cover
    letter — those are generated on demand when the user taps Apply."""
    prompt = f"""You screen Belgian job postings for one specific candidate.

{CANDIDATE_PROFILE}

STEP 1 — Decide PASS/FAIL for this early-career candidate. Be inclusive for
accessible roles, but keep the hard walls.

FAIL the job if ANY of these is true (hard walls — no exceptions):
- LANGUAGE: Dutch or French is a hard requirement (more than "a plus"), and the
  role is not otherwise open to an English speaker.
- SKILLED TRADE / PRODUCTION / MANUAL role: machine/production/CNC operator,
  metalwork, welding, grinding, assembly, manufacturing, chocolatier, print/line
  operator, construction, electrical, mechanical, maintenance technician.
- LICENCE / CERTIFICATE the CV lacks: forklift/reachtruck, C/CE, nursing,
  medical/lab, pilot, professional finance/engineering certification.
- MANDATORY SPECIALIST DEGREE: the role clearly requires a specific degree the
  candidate doesn't have — engineering, finance/accounting, data science,
  software/IT, science, law, medicine.
- SENIORITY: titled Senior / Lead / Manager / Director / Head, OR requiring
  roughly 3+ years of dedicated professional experience in a specialist field.
- Cleaning / domestic-help role (poetshulp, huishoudhulp, schoonmaak, cleaner).

Otherwise PASS — the candidate may apply even if it's a stretch. Treat as PASS
the accessible roles: customer service, administration / office support,
reception, data entry, sales / account / commercial support, digital marketing /
SEO / content, junior web or front-end, general warehouse & logistics, and
"no experience needed" roles — EVEN IF they ask for ~1-2 years of experience or
"some experience" (just score it lower). For these accessible roles, when unsure,
PASS with a low score rather than fail.

STEP 2 — Summarise, honestly, either way. For a PASS that is a stretch, still say
in why_good what the candidate would be leaning on and note the gap frankly.

Reply ONLY with JSON:
{{
  "pass": true or false,
  "reason": "one short sentence: the single main reason for the pass/fail decision",
  "title": "the job title",
  "company": "the company name or 'Unknown'",
  "location": "city or 'Unknown'",
  "match_score": 0-100 — 75-100 = clearly qualified; 50-74 = can apply, minor gaps; 30-49 = a reach (wants a bit more experience than the CV shows) but still worth trying; below 30 should usually be a FAIL,
  "details": "4-6 short bullets (one newline-separated string): role, main tasks, contract type, schedule, language, pay if stated (or '')",
  "why_good": "if pass: 3-5 short bullets (one newline-separated string) on why it fits, grounded ONLY in the real CV. If fail: ''",
  "why_bad": "if fail: 2-4 short bullets (one newline-separated string) naming exactly which required experience / licence / qualification / seniority / language the candidate is MISSING for this job. If pass: ''"
}}

THE REAL CV:
{cv_text}

JOB POSTING:
{job_text[:8000]}"""
    return ask_llm(prompt, expect_json=True, provider=EVAL_PROVIDER,
                   gemini_model=GEMINI_EVAL_MODEL)


# ---------------------------------------------------------------- main

def main():
    if not GEMINI_KEY and not DEEPSEEK_API_KEY:
        raise SystemExit("No AI key set — add GEMINI_API_KEY or DEEPSEEK_API_KEY as a GitHub secret.")
    print(f"Engines: eval={EVAL_PROVIDER}, write={WRITE_PROVIDER}, "
          f"max_new_per_run={MAX_NEW_PER_RUN}")

    cv_text = open(CV_FILE, encoding="utf-8").read()
    seen = set(load_json(SEEN_FILE, []))
    jobs = load_json(JOBS_FILE, {"updated": "", "jobs": []})
    jobs.setdefault("rejected", [])   # "not a fit" pool (with why_bad reasons)

    matched = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            # First: re-check already-saved matches under the current criteria,
            # so jobs that only ever passed the old language-only filter (e.g.
            # machine operator, senior analyst) get moved to "not a fit".
            revet_saved(browser, jobs, cv_text)

            print("Collecting job links from VDAB...")
            all_links = set()
            for url in SEARCH_URLS:
                links = collect_links(browser, url)
                print(f"  {len(links)} links from {url}")
                all_links |= links

            # Breadth: record every English job we can see (title from the URL
            # slug), minus the ones the candidate never wants (cleaning + C/CE
            # truck roles). Independent of the slower AI pipeline below.
            listing = [
                {"id": i, "url": u, "title": _slug_title(u)}
                for (u, i) in all_links
                if not is_excluded(_slug_title(u)) and not is_ineligible(_slug_title(u))
            ]
            listing.sort(key=lambda j: j["id"], reverse=True)
            jobs["listing"] = listing[:1400]

            new_links = [
                (u, i) for (u, i) in all_links
                if i not in seen and not is_excluded(_slug_title(u))
                and not is_ineligible(_slug_title(u))
            ]
            print(f"{len(all_links)} total, {len(jobs['listing'])} after filter, "
                  f"{len(new_links)} not yet seen")
            new_links = new_links[:MAX_NEW_PER_RUN]

            matched = _process_jobs(browser, new_links, seen, jobs, cv_text)
        finally:
            browser.close()

    jobs["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Drop cleaning/truck matches entirely (never wanted); move clearly-ineligible
    # trade/licensed matches into the "not a fit" pool with a reason.
    kept = []
    for j in jobs["jobs"]:
        title = j.get("title", "")
        if is_excluded(title):
            continue
        if is_ineligible(title):
            j["why_bad"] = j.get("why_bad") or INELIGIBLE_REASON
            j["reason"] = j["why_bad"].split("\n")[0]
            j["match_score"] = min(j.get("match_score", 0), 20)
            jobs["rejected"].insert(0, j)
            continue
        kept.append(j)
    jobs["jobs"] = kept[:100]
    jobs["rejected"] = jobs.get("rejected", [])[:REJECTED_CAP]
    save_json(JOBS_FILE, jobs)
    save_json(SEEN_FILE, sorted(seen))
    print(f"\nDone. {matched} new match(es) this run. "
          f"{len(jobs['jobs'])} in Ready, {len(jobs['rejected'])} not-a-fit.")


def _apply_verdict(jobs, job_id, url, verdict, apply_email, found_at=None):
    """Place a job into the matched pool or the 'rejected' (not-a-fit) pool
    based on the verdict, de-duplicating by id across both pools so a job never
    appears twice or lingers in the wrong list after being re-evaluated.
    Returns True if it landed in the matched pool."""
    entry = {
        "id": job_id,
        "url": url,
        "title": verdict.get("title", "Unknown"),
        "company": verdict.get("company", "Unknown"),
        "location": verdict.get("location", "Unknown"),
        "match_score": verdict.get("match_score", 0),
        "reason": verdict.get("reason", ""),
        "apply_email": apply_email or "",
        "found_at": found_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "status": "new",
        "details": verdict.get("details", ""),
        "why_good": verdict.get("why_good", ""),
        "why_bad": verdict.get("why_bad", ""),
        "cv_fit_v": CRITERIA_VERSION,
    }
    jobs["jobs"] = [j for j in jobs["jobs"] if j.get("id") != job_id]
    jobs["rejected"] = [j for j in jobs.get("rejected", []) if j.get("id") != job_id]
    if verdict.get("pass"):
        jobs["jobs"].insert(0, entry)
        return True
    jobs["rejected"].insert(0, entry)
    return False


def revet_saved(browser, jobs, cv_text, budget=40):
    """Re-check saved jobs (both matched AND rejected) against the current
    criteria version. Ones that no longer fit move to 'rejected'; ones that now
    fit (e.g. after loosening the rules) move back to matched. Only touches jobs
    stamped with an older CRITERIA_VERSION, so it's a one-time migration per bump."""
    stale = [j for j in (jobs["jobs"] + jobs.get("rejected", []))
             if j.get("cv_fit_v") != CRITERIA_VERSION][:budget]
    if not stale:
        return 0
    print(f"\nRe-vetting {len(stale)} saved match(es) against criteria v{CRITERIA_VERSION}...")
    moved = 0
    for j in stale:
        job_id, url = j.get("id"), j.get("url")
        print(f"\nRe-vetting {job_id}: {j.get('title')}")
        job_text, apply_email = fetch_job_detail(browser, url, job_id)
        if not job_text:
            print("  (could not read — leaving as-is for now)")
            continue
        try:
            verdict = evaluate_job(job_text, cv_text)
        except QuotaExhausted:
            print("  Gemini quota exhausted — stopping re-vet for this run.")
            break
        if not verdict:
            print("  (AI call failed — leaving as-is)")
            continue
        kept = _apply_verdict(jobs, job_id, url, verdict,
                              apply_email or j.get("apply_email"),
                              found_at=j.get("found_at"))
        print(f"  {'FITS' if kept else 'NOT A FIT'} "
              f"({verdict.get('match_score')}%): {verdict.get('reason')}")
        moved += 1
        time.sleep(1)
    print(f"Re-vet done: {moved} re-checked.")
    return moved


def _process_jobs(browser, new_links, seen, jobs, cv_text):
    matched = 0
    ai_fails = 0
    for url, job_id in new_links:
        print(f"\nChecking job {job_id}: {url}")

        job_text, apply_email = fetch_job_detail(browser, url, job_id)
        if not job_text:
            print("  (could not read job — will retry next run)")
            continue  # don't mark seen; a transient render failure gets another chance

        try:
            verdict = evaluate_job(job_text, cv_text)
        except QuotaExhausted:
            # Free-tier quota is spent — stop now instead of burning time/quota.
            # The listing + already-banked matches stay intact for the dashboard.
            print("  Gemini quota exhausted — stopping AI for this run (listing still updated).")
            break
        if not verdict:
            ai_fails += 1
            print("  Skipped (AI call failed — will retry next run)")
            if ai_fails >= 4:
                print("  Too many AI failures — stopping AI for this run.")
                break
            continue  # Gemini hiccup; don't mark seen so it's retried
        ai_fails = 0

        # We got a real verdict (pass or fail) — safe to not process it again.
        seen.add(job_id)
        kept = _apply_verdict(jobs, job_id, url, verdict, apply_email)
        if not kept:
            print(f"  NOT A FIT: {verdict.get('reason')}")
            continue

        print(f"  MATCH ({verdict.get('match_score')}%): {verdict.get('title')}")
        matched += 1

        send_telegram(
            f"<b>New job match ({verdict.get('match_score')}%)</b>\n"
            f"{verdict.get('title')} — {verdict.get('company')}\n"
            f"{verdict.get('location')}\n\n"
            f"{verdict.get('reason')}\n\n"
            f'<a href="{url}">View on VDAB</a>'
        )
        time.sleep(2)  # light pacing to stay under the per-minute request rate

    return matched


if __name__ == "__main__":
    main()
