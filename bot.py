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
import subprocess
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- settings

# Several searches, not just "english", to widen the net for jobs open to an
# English speaker (many don't literally contain the word "english"). We collect
# titles+URLs cheaply here; the AI then screens each one. The list accumulates
# across runs, so coverage keeps growing instead of being capped at one search.
# We deliberately do NOT scan all ~200k VDAB jobs: ~95% require Dutch/French, so
# rendering+screening them would burn the budget on guaranteed rejections.
def _search(term):
    return f"https://www.vdab.be/vindeenjob/vacatures?trefwoord={term.replace(' ', '%20')}"

# The kind of work the candidate actually wants — searched FIRST on every run so
# marketing/SEO/web jobs are found and screened before anything else.
PRIORITY_SEARCH_URLS = [
    _search(t) for t in
    ("digital marketing", "marketing", "seo", "wordpress", "web design",
     "web developer", "content", "social media", "online marketing",
     "e-commerce", "communication", "copywriter")
]
# Everything else, walked a rotating slice at a time (collection is slow).
ROTATING_SEARCH_URLS = (
    [_search(t) for t in
     ("english", "english speaking", "fluent english", "international",
      "customer service", "content", "copywriter", "communication",
      "logistics", "warehouse", "sales support", "administrative",
      "junior", "data entry", "front-end")]
    + ["https://www.vdab.be/vindeenjob/jobs/english-jobs"]
)
SEARCHES_PER_RUN = int(os.environ.get("SEARCHES_PER_RUN", "3"))

# Titles that look like the candidate's target field — screened first so they
# reach the Ready tab ahead of the filler jobs.
MARKETING_RX = re.compile(
    r"seo\b|sea\b|sem\b|marketing|marketeer|marketer|content|wordpress|copywrit|"
    r"social\s*media|communicat|digital|\bweb\b|website|web\s*design|webdesign|"
    r"front[-\s]?end|\bux\b|\bui\b|e-?commerce|growth|\bbrand|campaign|advertis", re.I)


def is_marketing(title):
    return bool(MARKETING_RX.search(title or ""))

# Title pre-screen: the AI reads plain job titles in cheap batches (no page
# render) to shortlist the ones worth a full look, so rendering + full screening
# is spent only on plausible jobs. This is what lets coverage scale.
TITLE_SCREEN_CAP = int(os.environ.get("TITLE_SCREEN_CAP", "1500"))  # titles/run
TITLE_BATCH = 40                                                   # titles per AI call

CANDIDATE_ONELINE = (
    "Early-career, ~4 months experience. GOAL FIELD (keep eagerly): digital "
    "marketing, SEO/SEA, content, copywriting, social media, WordPress/web/web "
    "design, front-end, e-commerce, online marketing, communication. Also fits: "
    "office/admin, customer service, reception, data entry, sales/commercial "
    "support, warehouse/logistics. NOT skilled trades/production/machine "
    "operators, NOT senior/manager/director, NOT licensed professions, NOT "
    "specialist-degree roles, NOT 2+ years required. English + Turkish, Dutch A2 "
    "(basic Dutch OK), no French."
)

JOBS_FILE = "docs/jobs.json"      # matched jobs (dashboard reads this)
SEEN_FILE = "seen.json"           # every job ID we fully evaluated (render + AI)
SCREEN_FILE = "screen.json"       # cheap title-screen state: shortlist + rejects
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

MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "300"))  # big chunk per run; progress is checkpointed
CHECKPOINT_EVERY = 25  # save + git-push progress this often so a long run can't lose its work

# Bump this whenever the fit criteria in evaluate_job change. Saved matches that
# were judged under an older version get re-vetted (a one-time migration) so the
# pool reflects the newest rules instead of leaving stale bad matches around.
CRITERIA_VERSION = 5
REJECTED_CAP = 120    # keep the most recent "not a fit" jobs for the audit tab

# Jobs to always exclude (candidate only has a B driver's licence and does not
# want cleaning/domestic roles). Matched against the job title/slug.
EXCLUDE_RX = re.compile(
    r"poets|huishoud|schoonma|kuis|cleaner|cleaning|household\s*help|"
    r"domestic|"                                    # cleaning / household
    r"truck\s*driver|vrachtwagen|\bce[-\s]?(driver|chauffeur|truck)|"
    r"chauffeur\s*ce|rijbewijs\s*c\b|rijbewijs\s*ce|\bc/ce\b|\bce\b\s*truck|"  # C/CE truck
    r"\bstudent|jobstudent|studenten|vakantie(job|werk)|vacation\s*job",  # student jobs
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


def collect_links(browser, search_url, cap=5000, budget_s=40, max_pages=25):
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
CANDIDATE_PROFILE = """WHO THE CANDIDATE IS:
- Early-career. GOAL FIELD: digital marketing / SEO / content / WordPress & web /
  web design. Real experience: a ~3-month digital-marketing & SEO internship plus
  ~1 month on the job (WordPress/Elementor, on-page SEO, keyword research, content
  writing, Google Analytics/Ads, SEMrush/Ahrefs), an older junior front-end dev
  stint (AngularJS/JavaScript), and general warehouse/logistics work.
- Coursework in Applied Computer Science (no completed degree).
- Languages: English (professional), Turkish (native), Dutch A2 / basic
  (improving), no French.

EXPERIENCE RULE (important): the candidate has only ~4 months of professional
experience. Jobs asking for UP TO ~2 years are acceptable (a reach, score lower).
Jobs that clearly require 2+ years of dedicated experience → FAIL.

LANGUAGE RULE: the candidate works in English and has A2 (basic) Dutch. PASS jobs
that are in English, accept English, or need only BASIC/elementary Dutch (A2) or
Dutch "as a plus". FAIL jobs that require FLUENT/professional/native Dutch or any
French.

WHAT THE CANDIDATE CANNOT DO (must FAIL):
- Skilled trades / production / machine operation / metalwork / construction.
- Roles needing a licence/certificate (forklift, C/CE, nursing, medical/lab,
  pilot, professional finance/engineering cert).
- Roles that STRICTLY require an unrelated specialist degree with no
  "or equivalent experience" option (engineer, doctor, nurse, lawyer, licensed
  accountant), or a specialised senior background (finance/tax/KYC, R&D, medical,
  aviation). A bachelor "or equivalent by experience", or a marketing /
  communication / business / IT bachelor, does NOT disqualify — keep those.
- Senior / Lead / Manager / Director / Head roles, or anything needing 2+ years."""


def title_prescreen(titles):
    """Cheap batch filter over plain job titles (no page render). Returns the set
    of indices (into `titles`) worth a full look. Deliberately inclusive — it only
    drops titles that are clearly non-fits; the full evaluate_job does the precise
    language/experience call. On any parse/quota failure it keeps the batch, so no
    job is ever silently lost at this stage."""
    keep = set()
    for start in range(0, len(titles), TITLE_BATCH):
        batch = titles[start:start + TITLE_BATCH]
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
        prompt = f"""Belgian job titles. Decide which are worth a full check for this candidate.
CANDIDATE: {CANDIDATE_ONELINE}

KEEP a title if it could plausibly be an accessible junior/entry/office/admin/
customer-service/marketing/SEO/content/web/sales-support/warehouse/logistics role.
DROP only titles that are clearly: a skilled trade or production/machine operator;
a senior/lead/manager/director/head role; or a licensed/degree profession
(engineer, doctor, nurse, lawyer, licensed accountant). Do NOT drop a title just
because it might want a bachelor. When unsure, KEEP.

Reply ONLY as JSON: {{"keep": [the numbers to keep]}}.
TITLES:
{numbered}"""
        try:
            res = ask_llm(prompt, expect_json=True, provider=EVAL_PROVIDER,
                          gemini_model=GEMINI_EVAL_MODEL)
        except QuotaExhausted:
            print("  Title screen: quota exhausted — keeping the rest for next run.")
            for i in range(len(batch)):
                keep.add(start + i)
            break
        if not res or "keep" not in res:
            for i in range(len(batch)):    # safe: don't lose jobs on a parse miss
                keep.add(start + i)
            continue
        for num in res.get("keep", []):
            try:
                idx = int(num) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(batch):
                keep.add(start + idx)
        time.sleep(1)
    return keep


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
- LANGUAGE: the role requires FLUENT / professional / native Dutch, or ANY
  French. (Jobs in English, or that accept English, or that need only BASIC /
  elementary Dutch — A2 — or Dutch "as a plus" are FINE: the candidate has A2.)
- EXPERIENCE: the role clearly requires 2+ years of dedicated experience. (Up to
  ~2 years is acceptable — the candidate has ~4 months; just score it lower.)
- SKILLED TRADE / PRODUCTION / MANUAL role: machine/production/CNC operator,
  metalwork, welding, grinding, assembly, manufacturing, chocolatier, print/line
  operator, construction, electrical, mechanical, maintenance technician.
- LICENCE / CERTIFICATE the CV lacks: forklift/reachtruck, C/CE, nursing,
  medical/lab, pilot, professional finance/engineering certification.
- HARD UNRELATED DEGREE: the role STRICTLY requires a specific degree in an
  unrelated technical/professional field (engineering, medicine, nursing,
  pharmacy, law, accounting) AND does NOT accept equivalent experience.
- SENIORITY: titled Senior / Lead / Manager / Director / Head.
- Cleaning / domestic-help / student job.

DO NOT FAIL a job just because it mentions a bachelor/degree. If it says
"bachelor OR equivalent by experience", or asks for a general / marketing /
communication / business / IT bachelor, PASS it (score to taste) — the candidate
wants to see and decide on these himself.

Otherwise PASS — the candidate may apply even if it's a stretch. Especially KEEP
anything in or near the GOAL FIELD: digital marketing, SEO/SEA, content,
copywriting, social media, WordPress / web / web design, front-end, e-commerce,
online marketing, communication. For GOAL-FIELD jobs be MAXIMALLY inclusive —
pass unless a hard wall above truly applies (fluent Dutch/French, 2+ years, a
skilled trade/licence, or an unrelated specialist degree with no experience
option). Also PASS accessible roles: customer service, administration / office
support, reception, data entry, sales / commercial support, general warehouse &
logistics, and "no experience needed" roles. When unsure, PASS with a low score.

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
    screen = load_json(SCREEN_FILE, {"title_no": [], "shortlist": []})
    title_no = set(screen.get("title_no", []))     # dropped at the cheap title stage
    shortlist = set(screen.get("shortlist", []))   # passed title stage, await full eval

    def checkpoint():
        """Persist current progress and push it, so a long run that dies partway
        (or is stopped) keeps everything screened so far. Best-effort: never let a
        git hiccup crash the scan."""
        # Refresh the timestamp on every checkpoint so the app shows the scan is
        # live and working, not frozen at the last full-run's time.
        jobs["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        save_json(JOBS_FILE, jobs)
        save_json(SEEN_FILE, sorted(seen))
        save_json(SCREEN_FILE, {"title_no": sorted(title_no), "shortlist": sorted(shortlist)})
        try:
            subprocess.run(["git", "add", JOBS_FILE, SEEN_FILE, SCREEN_FILE],
                           check=False, capture_output=True)
            r = subprocess.run(
                ["git", "-c", "user.name=job-bot",
                 "-c", "user.email=bot@users.noreply.github.com",
                 "commit", "-q", "-m", "Update jobs (checkpoint)"],
                check=False, capture_output=True)
            if r.returncode == 0:
                # HEAD:main works even if checkout left us on a detached HEAD.
                p = subprocess.run(["git", "push", "origin", "HEAD:main"],
                                   check=False, capture_output=True, text=True)
                print("  [checkpoint pushed]" if p.returncode == 0
                      else f"  [checkpoint push failed: {p.stderr.strip()[:200]}]")
        except Exception as e:
            print(f"  (checkpoint push skipped: {e})")

    matched = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            # First: re-check already-saved matches under the current criteria,
            # so jobs that only ever passed the old language-only filter (e.g.
            # machine operator, senior analyst) get moved to "not a fit".
            revet_saved(browser, jobs, cv_text, budget=200, checkpoint=checkpoint)

            # Always search the target field (marketing/SEO/web) first, then walk
            # a rotating slice of the rest so every term is covered over time.
            cursor = jobs.get("search_cursor", 0)
            n = len(ROTATING_SEARCH_URLS)
            rot = [ROTATING_SEARCH_URLS[(cursor + k) % n]
                   for k in range(min(SEARCHES_PER_RUN, n))]
            jobs["search_cursor"] = (cursor + len(rot)) % n
            todays = PRIORITY_SEARCH_URLS + rot
            print(f"Collecting from {len(todays)} search(es) this run "
                  f"({len(PRIORITY_SEARCH_URLS)} priority + {len(rot)} rotating)...")
            all_links = set()
            for url in todays:
                # Page DEEP on the marketing/goal-field searches so we actually
                # cover them (VDAB has ~1000 "digital marketing" jobs); stay lean
                # on the broad rotating searches to keep the run's length sane.
                priority = url in PRIORITY_SEARCH_URLS
                links = collect_links(browser, url,
                                      budget_s=130 if priority else 60,
                                      max_pages=70 if priority else 30)
                print(f"  {len(links)} links from {url}")
                all_links |= links

            # Accumulate the master listing across runs (union by id), dropping
            # roles the candidate never wants. This is what keeps coverage growing
            # instead of being pinned to a single search's results.
            listing = {j["id"]: j for j in jobs.get("listing", [])}
            for (u, i) in all_links:
                t = _slug_title(u)
                if is_excluded(t) or is_ineligible(t):
                    continue
                listing[i] = {"id": i, "url": u, "title": t}
            # Keep the newest ~4000 in the browse list the phone downloads (bigger
            # would bloat jobs.json); the funnel state below tracks what's screened.
            jobs["listing"] = sorted(
                listing.values(), key=lambda j: j["id"], reverse=True)[:4000]
            by_id = {j["id"]: j for j in jobs["listing"]}
            checkpoint()   # save the freshly-collected listing before screening

            # Cheap title pre-screen: shortlist plausible titles, drop clear
            # non-fits — WITHOUT rendering — so the expensive render+full-eval is
            # spent only on jobs worth it. This is what makes wide coverage cheap.
            cand = [j for j in jobs["listing"]
                    if j["id"] not in seen and j["id"] not in title_no
                    and j["id"] not in shortlist]
            # Screen target-field titles first, then newest.
            cand.sort(key=lambda j: (not is_marketing(j["title"]), -int(j["id"])))
            cand = cand[:TITLE_SCREEN_CAP]
            if cand:
                print(f"Title pre-screening {len(cand)} titles...")
                kept = title_prescreen([c["title"] for c in cand])
                for i, c in enumerate(cand):
                    (shortlist if i in kept else title_no).add(c["id"])
                print(f"  shortlisted {len(kept)}, dropped {len(cand) - len(kept)} at title stage")

            # Full render + AI evaluation, drawn from the shortlist only —
            # target-field (marketing/SEO/web) titles first, then newest.
            ready_ids = [i for i in shortlist if i in by_id and i not in seen]
            ready_ids.sort(key=lambda i: (not is_marketing(by_id[i]["title"]), -int(i)))
            new_links = [(by_id[i]["url"], i) for i in ready_ids][:MAX_NEW_PER_RUN]
            print(f"{len(all_links)} collected, {len(jobs['listing'])} in listing, "
                  f"{len(shortlist)} shortlisted, {len(title_no)} title-dropped, "
                  f"{len(new_links)} queued for full screening")

            matched = _process_jobs(browser, new_links, seen, jobs, cv_text,
                                    checkpoint=checkpoint)
            shortlist -= seen   # drop the ones we just fully evaluated
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
    save_json(SCREEN_FILE, {"title_no": sorted(title_no), "shortlist": sorted(shortlist)})
    print(f"\nDone. {matched} new match(es) this run. "
          f"{len(jobs['jobs'])} in Ready, {len(jobs['rejected'])} not-a-fit. "
          f"Screen state: {len(shortlist)} shortlisted, {len(title_no)} title-dropped.")


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


def revet_saved(browser, jobs, cv_text, budget=40, checkpoint=None):
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
        if checkpoint and moved % CHECKPOINT_EVERY == 0:
            checkpoint()
        time.sleep(1)
    print(f"Re-vet done: {moved} re-checked.")
    return moved


def _process_jobs(browser, new_links, seen, jobs, cv_text, checkpoint=None):
    matched = 0
    ai_fails = 0
    processed = 0
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
        processed += 1
        kept = _apply_verdict(jobs, job_id, url, verdict, apply_email)
        if kept:
            print(f"  MATCH ({verdict.get('match_score')}%): {verdict.get('title')}")
            matched += 1
            send_telegram(
                f"<b>New job match ({verdict.get('match_score')}%)</b>\n"
                f"{verdict.get('title')} — {verdict.get('company')}\n"
                f"{verdict.get('location')}\n\n"
                f"{verdict.get('reason')}\n\n"
                f'<a href="{url}">View on VDAB</a>'
            )
        else:
            print(f"  NOT A FIT: {verdict.get('reason')}")

        if checkpoint and processed % CHECKPOINT_EVERY == 0:
            checkpoint()
        time.sleep(1)  # light pacing to stay under the per-minute request rate

    return matched


if __name__ == "__main__":
    main()
