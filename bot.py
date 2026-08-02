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

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

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
    ("digital marketing", "marketing", "seo", "seo specialist", "seo manager",
     "search engine optimization", "zoekmachine optimalisatie", "content manager",
     "sea", "google ads", "wordpress",
     "web design", "web designer", "webdesigner", "ux designer", "ui designer",
     "front-end", "webflow", "web developer", "webshop", "content", "content marketing",
     "social media", "online marketing", "growth marketing", "e-commerce",
     "e-mail marketing", "communication", "copywriter", "marketing assistant",
     "stage marketing", "internship marketing", "stage communicatie",
     "web design stage", "digital", "creative",
     # Belgian spelling ("marketeer") + digital-marketing subfields the old list
     # missed, plus English-intersection terms — most Flemish marketing roles
     # need Dutch, so we cast a wider net to grow the English-friendly subset.
     "marketeer", "digital marketeer", "content marketeer", "performance marketing",
     "marketing automation", "paid media", "google analytics", "marketing officer",
     "marketing coordinator", "junior marketing", "campaign", "advertising",
     "employer branding", "crm marketing", "conversion", "brand", "media",
     "marketing english", "international marketing", "growth hacker",
     "digital strategist", "influencer marketing")
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
SEARCHES_PER_RUN = int(os.environ.get("SEARCHES_PER_RUN", "5"))

# Titles that look like the candidate's target field — screened first so they
# reach the Ready tab ahead of the filler jobs.
MARKETING_RX = re.compile(
    r"seo\b|sea\b|sem\b|google\s*ads|marketing|marketeer|marketer|content|"
    r"wordpress|copywrit|social\s*media|communicat|digital|\bweb\b|website|"
    r"web\s*design|webdesign|webshop|front[-\s]?end|\bux\b|\bui\b|e-?commerce|"
    r"growth|\bbrand(?:ing|s)?\b|campaign|advertis", re.I)


def is_marketing(title):
    return bool(MARKETING_RX.search(title or ""))


# is_marketing is a yes/no flag, and it lumps an SEO internship together with a
# brand-campaign job. The AI budget only stretches to a handful of full reads per
# run, so the ORDER inside that yes group decides what actually gets looked at.
# These tiers spend the budget nearest the candidate's core first.
_PRIORITY_TIERS = (
    # "sea" is not usable as a bare word here: Belgian listings are full of "Sea
    # Logistics" / "Sea Freight" shipping roles, and one of them ("Stage Sea
    # Logistics") took the single full read a run could afford. Only match SEA
    # where the surrounding word makes it the marketing discipline.
    re.compile(r"\bseo\b|search\s*engine\s*(?:optimi|advertis|market)|\bsem\b|"
               r"\bsea[-\s/&]*(?:seo|specialist|manager|expert|marketeer|consultant|"
               r"campaign|advertis|social|ads)|(?:seo|sem)[-\s/&]+sea\b|google\s*ads|"
               r"zoekmachine",
               re.I),
    re.compile(r"digital\s*market|digitale\s*market|online\s*market|marketeer|"
               r"content|copywrit|social\s*media|e-?commerce|webshop|wordpress|"
               r"web\s*design|webdesign|front[-\s]?end|\bux\b|\bui\b|growth", re.I),
    re.compile(r"marketing|communicat|\bbrand(?:ing|s)?\b|campaign|campagne|"
               r"advertis|\bpr\b", re.I),
)
_ENTRY_RX = re.compile(r"\bintern\b|internship|\bstage\b|stagiair|junior|\bjr\.?\b|"
                       r"trainee|starter|entry[-\s]level", re.I)


def title_priority(title):
    """Lower sorts first. Tier by how close the title is to the target field,
    then pull entry-level wording half a tier forward — a junior/intern posting
    is a better use of a scarce full read than a senior one in the same field."""
    t = title or ""
    tier = next((n for n, rx in enumerate(_PRIORITY_TIERS) if rx.search(t)), len(_PRIORITY_TIERS))
    return tier - 0.5 if _ENTRY_RX.search(t) else tier

# Title pre-screen: the AI reads plain job titles in cheap batches (no page
# render) to shortlist the ones worth a full look, so rendering + full screening
# is spent only on plausible jobs. This is what lets coverage scale.
TITLE_SCREEN_CAP = int(os.environ.get("TITLE_SCREEN_CAP", "2200"))  # titles/run
TITLE_BATCH = 40                                                   # titles per AI call

CANDIDATE_ONELINE = (
    "Early-career, ~4 months experience. GOAL FIELD (keep eagerly): digital "
    "marketing, SEO/SEA, content, copywriting, social media, WordPress/web/web "
    "design, front-end, e-commerce, online marketing, communication. Also fits: "
    "office/admin, customer service, reception, data entry, sales/commercial "
    "support, warehouse/logistics. NOT skilled trades/production/machine "
    "operators, NOT senior/manager/director, NOT licensed professions, NOT "
    "specialist-degree roles, NOT 2+ years required. English + Turkish, Dutch B1 "
    "(conversational Dutch OK), no French."
)

JOBS_FILE = "docs/jobs.json"      # matched jobs (dashboard reads this)
SEEN_FILE = "seen.json"           # every job ID we fully evaluated (render + AI)
SCREEN_FILE = "screen.json"       # cheap title-screen state: shortlist + rejects
CV_FILE = "cv.md"                 # your master CV
PREPARED_DIR = "docs/prepared"    # pre-written email + cover letter per match

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

# Which engine screens jobs / writes letters. Prefer DeepSeek for both when its
# key exists: letters are now pre-written for EVERY match (dozens per run), which
# would blow through Gemini's tiny free daily quota.
EVAL_PROVIDER = os.environ.get(
    "EVAL_PROVIDER", "deepseek" if DEEPSEEK_API_KEY else "gemini")
WRITE_PROVIDER = os.environ.get(
    "WRITE_PROVIDER", "deepseek" if DEEPSEEK_API_KEY else "gemini")

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

# LinkedIn issues job ids in order, so the gap between a posting's id and the
# newest id we hold is its age — measured at ~496,570 ids/day between 14 Jul and
# 1 Aug 2026. This is the only reliable staleness signal available: the "no
# longer accepting applications" banner is invisible from GitHub's runners
# (LinkedIn answers them with a login wall), and a months-old posting is almost
# always closed. A 45-day gate keeps a genuinely open 5-week-old internship while
# dropping the likes of a 2-year-old listing that surfaced looking "new today".
LI_IDS_PER_DAY = 496570
LI_MAX_AGE_DAYS = int(os.environ.get("LI_MAX_AGE_DAYS", "45"))


def li_age_days(job_id, newest_id):
    """Estimated age in days of a LinkedIn posting; 0 for anything else."""
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return 0.0
    if not newest_id or jid < 1_000_000_000:      # VDAB (8) / board (9) ids
        return 0.0
    return max(0.0, (newest_id - jid) / LI_IDS_PER_DAY)


def newest_linkedin_id(jobs):
    ids = [int(j["id"]) for j in list(jobs.get("listing", [])) + list(jobs.get("jobs", []))
           if str(j.get("id", "")).isdigit() and int(j["id"]) >= 1_000_000_000]
    return max(ids) if ids else 0
CHECKPOINT_EVERY = 25  # save + git-push progress this often so a long run can't lose its work

# Bump this whenever the fit criteria in evaluate_job change. Saved matches that
# were judged under an older version get re-vetted (a one-time migration) so the
# pool reflects the newest rules instead of leaving stale bad matches around.
CRITERIA_VERSION = 18
# Bump when the cheap title-screen rules change, to force a one-time re-screen of
# every previously title-dropped job under the new rules.
TITLE_SCREEN_VERSION = 2
# Bump when the closed-posting check itself gets smarter, so every job checked by
# the older, weaker version is queued for a fresh look instead of coasting on a
# verdict that method could not actually reach. Version 1 read LinkedIn over
# plain HTTP and called genuinely closed jobs open; version 2 renders the page.
CLOSED_CHECK_VERSION = 2
# Sentinel returned by the detail fetchers when a posting exists but is closed
# (LinkedIn "No longer accepting applications"). Distinct from None (= unreadable,
# retry later) so callers actively drop it instead of leaving it in Ready.
LI_CLOSED = "__CLOSED__"
REJECTED_CAP = 2000   # show (almost) every not-a-fit so coverage is auditable

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


class DeepSeekOutOfCredit(Exception):
    """Raised on DeepSeek's 402 Payment Required — the prepaid balance is empty.
    Unlike a 429 this never clears by itself, so ask_llm switches to Gemini for
    the rest of the run rather than failing every call."""


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
            # 402 = the prepaid balance is empty. Retrying cannot fix that, and
            # every later call would burn 4 more attempts while the run quietly
            # screens nothing. Raise so ask_llm can switch to Gemini instead.
            if r.status_code == 402:
                raise DeepSeekOutOfCredit(
                    "DeepSeek returned 402 Payment Required — the account balance is empty.")
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            if expect_json:
                text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
                return json.loads(text)
            return text
        except DeepSeekOutOfCredit:
            raise
        except Exception as e:
            print(f"  DeepSeek error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return None


# Set once a 402 is seen, so the rest of the run goes straight to Gemini instead
# of re-discovering the empty balance on every single call.
_DEEPSEEK_DEAD = False


def ask_llm(prompt, expect_json=False, provider=None, gemini_model=None):
    """Route a prompt to the chosen engine. DeepSeek for cheap high-volume
    screening; Gemini otherwise (with the caller's chosen Gemini model).

    If DeepSeek's balance runs out mid-run it would otherwise fail every call
    and the run would screen nothing while still reporting success — the bot
    looked healthy for three days that way. Fall back to Gemini instead: its
    free tier is small, so far fewer jobs get through, but the feed keeps
    moving until the balance is topped up."""
    global _DEEPSEEK_DEAD
    provider = provider or EVAL_PROVIDER
    if provider == "deepseek" and DEEPSEEK_API_KEY and not _DEEPSEEK_DEAD:
        try:
            return ask_deepseek(prompt, expect_json)
        except DeepSeekOutOfCredit as e:
            _DEEPSEEK_DEAD = True
            print(f"  ⚠️  {e}\n"
                  f"  ⚠️  Falling back to Gemini for the rest of this run "
                  f"(small free quota — top up DeepSeek to restore full speed).")
            send_telegram(
                "<b>⚠️ DeepSeek balance empty</b>\nThe job bot fell back to "
                "Gemini's small free quota, so far fewer jobs get screened per "
                "run. Top up DeepSeek to restore full speed.")
    if not GEMINI_KEY:
        return None
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


# ---- LinkedIn (secondary source) -------------------------------------------
# LinkedIn has no open API and blocks scrapers hard (HTTP 999/429 on datacenter
# IPs like GitHub Actions). The only public path is the "jobs-guest" endpoints,
# which return listings + descriptions WITHOUT login. Best-effort: every failure
# is swallowed so the VDAB run is never affected, and on a rate-limit block we
# back off for the rest of the run. LinkedIn ids are ~10 digits (VDAB ~8), so
# they don't collide; jobs carry src="linkedin" and are applied to via LinkedIn.
# Split like the VDAB searches: the on-target queries run every time, the rest
# rotate a slice per run. Asking for all 40 in one run is ~180 requests, which
# earned an HTTP 429 and cost us the tail of the list anyway — half the volume
# per run covers the same ground over two runs without the block.
LI_PRIORITY_KEYWORDS = [
    # SEO first + several variants so we never miss an SEO posting (distinct
    # LinkedIn queries return different result sets).
    "seo", "seo specialist", "seo manager", "search engine optimization",
    "seo consultant",
    # Entry-level phrasings, early because they are exactly this candidate's
    # level and they are cheap: a broad term like "digital marketing" returns
    # hundreds of Belgian hits and we only read the first pages of it, so an
    # internship posted weeks ago never surfaced under it. These narrow queries
    # return few enough results that we see the whole set.
    "marketing intern", "digital marketing intern", "marketing internship",
    "seo intern", "content intern", "communications intern",
    "stage marketing", "stage digitale marketing", "stage communicatie",
    "marketing stagiair", "junior marketing", "junior digital marketing",
    "marketing assistant", "marketing medewerker", "trainee marketing",
]
# Adjacent fields and broad terms — a slice of these each run.
LI_ROTATE_PER_RUN = 6   # 4 runs/day -> every broad term still swept daily
LI_ROTATING_KEYWORDS = [
    "content marketing", "content manager", "copywriter",
    "digital marketing", "digital marketeer", "online marketing",
    # "sea" alone returns sea-freight jobs; the marketing sense needs a partner
    # word. "google ads" covers the same discipline cleanly.
    "social media", "growth marketing", "sea specialist", "google ads", "e-commerce",
    # Web / UX / front-end design — the candidate's WordPress/Elementor/Canva
    # background fits these, and they're often LinkedIn-only (missed before).
    "web designer", "web design", "wordpress", "ux designer", "ui designer",
    "front-end", "webflow",
    "marketing", "communications",
]
LI_GUEST_SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/"
                   "seeMoreJobPostings/search")
LI_GUEST_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
# The guest endpoint above returns ONLY the description body, so the "No longer
# accepting applications" banner — which lives in the posting's top card — can
# never appear in it. That is why closed LinkedIn jobs kept sitting in the feed.
# The public job page does render that banner for logged-out visitors.
LI_PUBLIC_JOB = "https://www.linkedin.com/jobs/view/"
LI_CLOSED_MARKERS = (
    "no longer accepting applications",
    "aanvaardt geen sollicitaties meer",
    "no longer available",
    "closed-job",
)
LI_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}


def _li_job_id(card):
    urn = card.get("data-entity-urn", "") or ""
    m = re.search(r"jobPosting:(\d+)", urn)
    if m:
        return m.group(1)
    a = card.select_one("a[href*='/jobs/view/']")
    if a and a.get("href"):
        m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", a["href"])
        if m:
            return m.group(1)
    return None


def collect_linkedin(keywords=None, pages_per_kw=6, budget_s=420):
    """Scrape LinkedIn's public guest job search for Belgium. Returns
    {id: {id,url,title,company,location,src}}. Swallows all errors; backs off
    on a rate-limit/blocked response so we don't get the IP fully banned.

    sortBy=DD asks for newest-first. LinkedIn's default is relevance, which for a
    crawler that only reads the first pages means a fresh posting can sit behind
    a hundred older "more relevant" ones and never be collected at all."""
    keywords = keywords or (LI_PRIORITY_KEYWORDS + LI_ROTATING_KEYWORDS)
    found, t0 = {}, time.time()
    for kw in keywords:
        if time.time() - t0 > budget_s:
            print(f"  linkedin: budget spent, {kw!r} onwards skipped this run")
            break
        dry = 0
        for pg in range(pages_per_kw):
            if time.time() - t0 > budget_s:
                break
            try:
                r = requests.get(LI_GUEST_SEARCH,
                                 params={"keywords": kw, "location": "Belgium",
                                         # 60 days, not 30: postings a month old
                                         # are still open and were falling off
                                         # the edge of the window unseen.
                                         "f_TPR": "r5184000", "sortBy": "DD",
                                         "start": pg * 10},
                                 headers=LI_HEADERS, timeout=25)
            except Exception as e:
                print(f"  linkedin search error ({kw}): {e}")
                break
            if r.status_code in (429, 999, 403):
                print(f"  linkedin blocked (HTTP {r.status_code}) — backing off for this run")
                return found
            if r.status_code != 200 or not r.text.strip():
                break
            soup = BeautifulSoup(r.text, "html.parser")
            added = 0
            for c in soup.select("li"):
                jid = _li_job_id(c)
                if not jid or jid in found:
                    continue
                te = c.select_one(".base-search-card__title, h3")
                title = te.get_text(strip=True) if te else ""
                if not title:
                    continue
                ce = c.select_one(".base-search-card__subtitle, h4")
                le = c.select_one(".job-search-card__location")
                found[jid] = {
                    "id": jid,
                    "url": f"https://www.linkedin.com/jobs/view/{jid}",
                    "title": title,
                    "company": ce.get_text(strip=True) if ce else "",
                    "location": le.get_text(strip=True) if le else "",
                    "src": "linkedin",
                }
                added += 1
            print(f"  linkedin '{kw}' p{pg}: +{added} (total {len(found)})")
            # A page of pure duplicates usually means this keyword overlaps one we
            # already ran, not that its results are exhausted — so give it one
            # more page before moving on. Two dry pages in a row does mean done.
            dry = dry + 1 if added == 0 else 0
            if dry >= 2:
                break
            time.sleep(2.2)   # gentler: 1.5s over ~180 requests drew a 429
    print(f"  linkedin: collected {len(found)} jobs in {int(time.time() - t0)}s")
    return found


# ---------------------------------------------------------------------------
# Third source: the English-language Belgian job boards. LinkedIn and VDAB miss
# jobs that are only advertised on these, and several of them are where the
# English-speaking-in-Belgium postings actually live.
#
# Their HTML cannot be inspected from here (the sandbox is firewalled from every
# one of them, same as VDAB and LinkedIn), so this deliberately avoids per-site
# CSS selectors that I would only be guessing at. Instead it renders a listing
# page and keeps the links that look like job postings, which is a shape every
# board shares. Each board logs what it fetched and the first titles it found,
# so the run log says exactly which config needs correcting.
JOB_BOARDS = [
    # No "www." on this one — that hostname does not resolve at all, which is
    # what the first run's "failed" was. Listing paths below are the site's own.
    {"name": "englishjobs.be",
     # This board doesn't host postings: every vacancy is a /clickout/<hash>
     # redirect to the employer's own page, which is where applying happens
     # anyway. Rendering the redirect lands on the real posting.
     "job_rx": r"^/clickout(?:_alt)?/[0-9a-f]{6,}",
     "pages": ["https://englishjobs.be/jobs/marketing",
               "https://englishjobs.be/jobs/intern_internship",
               "https://englishjobs.be/in/brussels/marketing"]},
    {"name": "jobinbelgium.com",
     # Postings live under /vacancies/<slug> — 141 of them on the search page.
     "job_rx": r"^/vacancies/[^/]{3,}",
     "pages": ["https://www.jobinbelgium.com/",
               "https://www.jobinbelgium.com/jobs/",
               "https://www.jobinbelgium.com/?s=marketing"]},
    {"name": "stepstone.be",
     # Confirmed working: /jobs--<Title>-<City>-<Company>--<id>-inline.html.
     "pages": ["https://www.stepstone.be/jobs/marketing",
               "https://www.stepstone.be/jobs/digital-marketing",
               "https://www.stepstone.be/jobs/seo",
               "https://www.stepstone.be/jobs/content"]},
    # Two attempts each, both evidence-based, both dead. jobsinbrussels.com went
    # from 181 links to serving the runner a page with zero anchors — it is
    # blocking us or rendering entirely in JS. findajobinbelgium.com links only
    # /search and /alllocations, never a posting. Left in with a single cheap
    # page each in case they change; they cost a couple of seconds a run.
    {"name": "jobsinbrussels.com",
     "pages": ["https://www.jobsinbrussels.com/search?q=marketing"]},
    {"name": "findajobinbelgium.com",
     "pages": ["http://www.findajobinbelgium.com/search?language=English+only"]},
]
# Generic shape of a job-detail URL across boards: a /job/, /vacature/, /vacancy/,
# /offre/... segment followed by a slug with some substance to it.
BOARD_JOB_RX = re.compile(
    r"/(?:job|jobs|vacature|vacatures|vacancy|vacancies|offre|offres|emploi|"
    r"emplois|position|opening|listing)[a-z-]*[-/][^?#]{8,}", re.I)
# Editorial and browse pages that sit under the same /job... prefix as the real
# vacancies. Round two pulled in "/blog/job-description/project-manager" and
# "/blog/job-sector/be-or-become-a-marketing-and-sales-employee" as if they were
# jobs; they are career-advice articles.
BOARD_SKIP_RX = re.compile(
    r"/(?:blog|news|article|advice|guide|tips|about|faq|press|company|companies|"
    r"employer|employers|recruiter|category|categories|sector|sectors)(?:/|$)", re.I)
# What actually distinguishes a posting from a browse page: postings carry an id.
# Slug shape alone is not enough — "/jobs/Government-and-Social-Profit" and
# "/jobs/construction-material-and-real-estate" are categories that look exactly
# like role slugs. A board whose postings have no id in the URL needs its own
# "job_rx" in JOB_BOARDS, taken from the link census the misses print below.
BOARD_SLUG_RX = re.compile(r"\d{4,}")
BOARD_PER_PAGE = 60          # links kept per listing page
BOARD_BUDGET_S = 240


def board_job_id(url):
    """Stable numeric id for a board posting. Numeric because the whole pipeline
    sorts on int(id); 9 digits keeps it clear of VDAB's 8 and LinkedIn's 10."""
    h = int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:12], 16)
    return str(100_000_000 + h % 900_000_000)


def dedupe_key(title, company):
    """What counts as 'the same job' across two boards. Titles get punctuation and
    the usual (m/v/x)-style noise stripped; without a company name the title alone
    is too weak a key ("Digital Marketing Intern" is a dozen different jobs), so
    those are kept rather than silently dropped."""
    t = re.sub(r"\(.*?\)|\bm/v/x\b|\bm/f\b|\bh/f\b", " ", (title or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    c = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    c = re.sub(r"\b(nv|sa|bv|bvba|sprl|srl|vzw|asbl|group|belgium|belgie)\b", "", c).strip()
    return f"{t}|{c}" if t and c else None


def _full(parts):
    """Path plus query, for the link census. jobinbelgium.com linked 141 things
    that all share the path /vacancies/ — the difference between them has to be
    in the query string, and a census that drops it can't show that."""
    return parts.path + (f"?{parts.query}" if parts.query else "")


def board_url_is_posting(path, board=None):
    """Is this board path one vacancy, rather than a category listing or a
    career-advice article? A board with a confirmed detail-URL shape says so
    with its own job_rx; otherwise we fall back to "the URL carries an id"."""
    if BOARD_SKIP_RX.search(path):
        return False
    own = (board or {}).get("job_rx")
    if own:
        return bool(re.search(own, path, re.I))
    return bool(BOARD_SLUG_RX.search(path))


# Jobs that are simply not in Belgium. LinkedIn's Belgium search returns plenty
# of them — a Vietnamese-language SEO post, a Barcelona agency role, call-centre
# work in Lisbon. They are only worth showing if the work can actually be done
# from here, which means remote AND in English.
BE_PLACE_RX = re.compile(
    r"belgi|brussel|bruxelles|vlaan|flemish|wallon|antwerp|gent\b|ghent|leuven|louvain|"
    r"li[eè]ge|charleroi|brugge|bruges|hasselt|mechelen|namur|namen|kortrijk|aalst|genk|"
    r"oostende|ostend|sint-niklaas|roeselare|turnhout|zaventem|diegem|waterloo|wavre|"
    r"mons|tournai|ieper|lier\b|herentals|deinze|dendermonde|halle|vilvoorde|tienen|"
    r"geel\b|mol\b|beveren|wilrijk|berchem|kontich|aartselaar|edegem|schoten|ranst|"
    r"limburg|brabant|hainaut|west-vl|oost-vl|kempen|ardennes|eupen|arlon|verviers", re.I)
FOREIGN_PLACE_RX = re.compile(
    r"\b(spain|espa[nñ]a|portugal|greece|france|germany|deutschland|netherlands|holland|"
    r"austria|[oö]sterreich|vietnam|vi[eệ]t\s*nam|india|poland|polska|romania|bulgaria|"
    r"czech|hungary|turkey|t[uü]rkiye|morocco|egypt|u\.?a\.?e\.?|united arab|"
    r"united states|u\.?s\.?a\.?|canada|brazil|mexico|philippines|indonesia|malaysia|"
    r"singapore|china|japan|korea|ukraine|serbia|croatia|slovenia|slovakia|lithuania|"
    r"latvia|estonia|finland|sweden|norway|denmark|iceland|ireland|united kingdom|"
    r"england|scotland|switzerland|italy|italia|malta|cyprus|israel|south africa|"
    r"nigeria|kenya|argentina|colombia|chile|peru|australia|new zealand|pakistan|"
    r"bangladesh|sri lanka|nepal|thailand|taiwan|hong kong|"
    r"barcelona|madrid|valencia|sevilla|lisbon|lisboa|porto|athens|thessaloniki|"
    r"paris|lyon|marseille|bordeaux|toulouse|nantes|berlin|munich|m[uü]nchen|hamburg|"
    r"cologne|k[oö]ln|frankfurt|d[uü]sseldorf|stuttgart|amsterdam|rotterdam|utrecht|"
    r"eindhoven|the hague|den haag|maastricht|breda|tilburg|vienna|wien|zurich|z[uü]rich|"
    r"geneva|gen[eè]ve|basel|london|manchester|birmingham|dublin|milan|milano|rome|roma|"
    r"turin|naples|warsaw|warszawa|krak[oó]w|bucharest|sofia|prague|praha|budapest|"
    r"istanbul|ankara|izmir|hanoi|ho chi minh|saigon|bangalore|bengaluru|mumbai|"
    r"new delhi|hyderabad|chennai|cairo|casablanca|tunis|lagos|nairobi|"
    r"s[aã]o paulo|buenos aires|toronto|vancouver|new york|san francisco|dubai)\b", re.I)
# Writing systems no Belgian vacancy uses. This is what catches a posting whose
# location field says "Unknown" but whose title is Vietnamese.
NONLOCAL_SCRIPT_RX = re.compile(
    "[ĂăƠơƯưẠ-ỹĐđ]"   # Vietnamese
    "|[Ѐ-ӿ֐-׿؀-ۿ]"                        # Cyrillic/Hebrew/Arabic
    "|[ऀ-ॿ฀-๿]"                                     # Devanagari/Thai
    "|[぀-ヿ一-鿿가-힯]")                       # JP/CN/KR
REMOTE_RX = re.compile(r"\bremote\b|work from home|telewerk|thuiswerk|\banywhere\b|"
                       r"\bhybrid\b", re.I)
ENGLISH_OK_RX = re.compile(r"\benglish\b|\bengels\b", re.I)


def is_far_away(title, company, location):
    """Is this posting outside Belgium?"""
    if NONLOCAL_SCRIPT_RX.search(f"{title or ''} {company or ''} {location or ''}"):
        return True
    if BE_PLACE_RX.search(location or ""):
        return False
    return bool(FOREIGN_PLACE_RX.search(location or "")
                or FOREIGN_PLACE_RX.search(company or ""))


def job_is_reachable(job):
    """Somewhere abroad is only worth offering if you could do it from Belgium:
    remote, and in English."""
    if not is_far_away(job.get("title"), job.get("company"), job.get("location")):
        return True
    blob = f"{job.get('location') or ''} {job.get('details') or ''}"
    return bool(REMOTE_RX.search(blob) and ENGLISH_OK_RX.search(blob))


def drop_unreachable_matches(jobs):
    """Move abroad-and-not-remote matches out of Ready."""
    keep, dropped = [], 0
    for j in jobs.get("jobs", []):
        if job_is_reachable(j):
            keep.append(j)
            continue
        j["why_bad"] = (f"Not in Belgium ({j.get('location') or 'location unclear'}) "
                        f"and not an English-language remote role.")
        j["reason"] = j["why_bad"]
        jobs.setdefault("rejected", []).insert(0, j)
        dropped += 1
    if dropped:
        jobs["jobs"] = keep
        print(f"  dropped {dropped} match(es) abroad without remote+English from Ready")
    return dropped


def drop_stale_matches(jobs):
    """Take LinkedIn postings too old to still be open out of Ready. They keep
    their evaluation in `rejected`, so nothing is re-screened from scratch, but
    they stop being offered as something to apply to."""
    newest = newest_linkedin_id(jobs)
    if not newest:
        return 0
    keep, dropped = [], 0
    for j in jobs.get("jobs", []):
        age = li_age_days(j.get("id"), newest)
        if age > LI_MAX_AGE_DAYS:
            j["why_bad"] = (f"Posted about {int(age)} days ago — LinkedIn postings "
                            f"this old are no longer accepting applications.")
            j["reason"] = j["why_bad"]
            jobs.setdefault("rejected", []).insert(0, j)
            dropped += 1
            continue
        keep.append(j)
    if dropped:
        jobs["jobs"] = keep
        print(f"  dropped {dropped} match(es) older than {LI_MAX_AGE_DAYS} days from Ready")
    return dropped


def prune_board_listings(jobs):
    """Drop board entries already saved that the current rules reject. Round two
    of this feature let blog posts and category pages into the listing, where
    they would sit forever eating screening budget; this cleans them out on the
    next run instead of needing a hand-edit of the data file."""
    by_host = {b["name"]: b for b in JOB_BOARDS}
    bad = {j["id"] for j in jobs.get("listing", [])
           if j.get("src") in by_host
           and not board_url_is_posting(urlsplit(j.get("url", "")).path,
                                        by_host[j["src"]])}
    if not bad:
        return 0
    jobs["listing"] = [j for j in jobs.get("listing", []) if j["id"] not in bad]
    jobs["jobs"] = [j for j in jobs.get("jobs", []) if j["id"] not in bad]
    print(f"  boards: pruned {len(bad)} saved non-vacancy links "
          f"(category/blog pages)")
    return bad


def collect_boards(browser, budget_s=BOARD_BUDGET_S):
    """Render each board's listing pages and return {id: {...}} for the postings
    found. Best-effort per board: one that changes its markup or blocks us logs a
    line and the others carry on."""
    found, t0 = {}, time.time()
    for board in JOB_BOARDS:
        if time.time() - t0 > budget_s:
            print(f"  boards: budget spent, {board['name']} onwards skipped this run")
            break
        rx = re.compile(board["job_rx"], re.I) if board.get("job_rx") else BOARD_JOB_RX
        host = board["name"]
        got = 0
        for url in board["pages"]:
            if time.time() - t0 > budget_s:
                break
            page = None
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-GB")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)     # let the listing hydrate
                html = page.content()
            except Exception as e:
                # The message matters: ERR_NAME_NOT_RESOLVED (wrong domain) and a
                # timeout are different problems, and the type name alone said
                # nothing on the first run.
                print(f"  board {host}: {url} failed — "
                      f"{' '.join(str(e).split())[:130]}")
                continue
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
            soup = BeautifulSoup(html, "html.parser")
            anchors = soup.select("a[href]")
            added, sample = 0, []
            host_paths = Counter()          # same-host paths, for the miss diagnostic
            host_examples = {}              # one full path per shape, as evidence
            for a in anchors:
                href = urljoin(url, (a.get("href") or "").strip())
                parts = urlsplit(href)
                if parts.scheme not in ("http", "https"):
                    continue
                if not parts.netloc.endswith(host):
                    continue
                # Match on the PATH only. Matching the whole URL would fire on
                # the hostname itself for a board like "jobs-in-brussels.com".
                if not rx.search(parts.path):
                    # Record the shape (first two path segments) of the links we
                    # skipped, so a run where nothing matches still reveals what
                    # the real posting URLs look like — the config can then be
                    # corrected instead of guessed at a second time.
                    seg = "/".join(parts.path.split("/")[:3]) or "/"
                    host_paths[seg] += 1
                    host_examples.setdefault(seg, _full(parts))
                    continue
                # Looks like a job URL, but the board's own browse pages and
                # career-advice articles sit under the same prefix.
                if not board_url_is_posting(parts.path, board):
                    host_paths[seg] += 1
                    host_examples.setdefault(seg, _full(parts))
                    continue
                href = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   parts.query, ""))
                jid = board_job_id(href)
                if jid in found:
                    continue
                title = " ".join((a.get_text(" ", strip=True) or "").split())[:140]
                if len(title) < 6:              # nav/logo links, not a posting
                    continue
                found[jid] = {"id": jid, "url": href, "title": title,
                              "company": "", "location": "", "src": host}
                added += 1
                if len(sample) < 3:
                    sample.append(title[:48])
                if added >= BOARD_PER_PAGE:
                    break
            got += added
            if added:
                print(f"  board {host}: {url} -> +{added} e.g. {sample}")
            else:
                # No posting matched: was the page even reachable (anchor count),
                # and what internal-link shapes did it actually carry?
                top = [f"{seg}({n})" for seg, n in host_paths.most_common(6)]
                print(f"  board {host}: {url} -> +0  "
                      f"[{len(anchors)} links, {sum(host_paths.values())} on-site] "
                      f"paths: {top or 'none on-site'}")
                # Frequency alone hides the postings: a vacancy URL appears once,
                # so it never reaches the top of that list while nav links do.
                # Print full one-off paths too — that is where the real posting
                # shape shows up.
                ones = [p for seg, p in host_examples.items() if host_paths[seg] == 1]
                if ones:
                    print("      one-off paths: "
                          + " | ".join(p[:70] for p in ones[:6]))
            time.sleep(1.5)
        print(f"  board {host}: {got} postings")
    print(f"  boards: collected {len(found)} postings in {int(time.time() - t0)}s")
    return found


def linkedin_guest_gone(job_id):
    """True only when LinkedIn's guest description endpoint says the posting no
    longer exists (404/410). Anything else — 200, a rate-limit, a network blip —
    returns False, because this is used to *drop* jobs and a wrong True throws
    away a live vacancy."""
    try:
        r = requests.get(LI_GUEST_JOB + str(job_id), headers=LI_HEADERS, timeout=20)
    except Exception:
        return False
    return r.status_code in (404, 410)


def linkedin_is_closed(job_id, browser=None):
    """True if LinkedIn's public job page says the posting stopped taking
    applications, False if it clearly still accepts them, None if we could not
    tell (blocked, throttled, login wall, network error).

    None is deliberate: an unreadable page must never drop a live job, so the
    caller keeps anything it cannot positively confirm as closed.

    Plain HTTP gets a stripped page from LinkedIn: the first version of this
    check read one and reported a genuinely closed posting as open. So when a
    browser is available, render the page like a real visitor — that is how the
    banner actually reaches the DOM — and keep raw HTTP only as a fallback."""
    jid = str(job_id)
    if browser is not None:
        body = title = ""
        page = None
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-US")
            page.goto(LI_PUBLIC_JOB + jid, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)          # let the top card settle
            body = page.inner_text("body").lower()
            title = (page.title() or "").lower()
        except Exception as e:
            print(f"  linkedin closed-check {jid}: render failed ({e})")
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
        if len(body) > 400:
            hit = next((m for m in LI_CLOSED_MARKERS if m in body), None)
            if hit:
                print(f"  linkedin closed-check {jid}: CLOSED (rendered, matched {hit!r})")
                return True
            if any(w in body or w in title for w in ("sign in", "join linkedin", "aanmelden")):
                # The banner lives behind the wall, but a posting that has been
                # taken down entirely also disappears from the guest endpoint —
                # and that answer is readable without an account.
                if linkedin_guest_gone(jid):
                    print(f"  linkedin closed-check {jid}: GONE (guest endpoint 404)")
                    return True
                print(f"  linkedin closed-check {jid}: login wall — cannot tell")
                return None
            print(f"  linkedin closed-check {jid}: open (rendered, {len(body)} chars)")
            return False
        print(f"  linkedin closed-check {jid}: rendered {len(body)} chars — cannot tell")
        return None

    try:
        r = requests.get(LI_PUBLIC_JOB + jid, headers=LI_HEADERS, timeout=25)
    except Exception as e:
        print(f"  linkedin closed-check {jid}: unreachable ({e})")
        return None
    if r.status_code != 200 or len(r.text) < 2000:
        print(f"  linkedin closed-check {jid}: HTTP {r.status_code}, "
              f"{len(r.text)} bytes — cannot tell")
        return None
    page_l = r.text.lower()
    hit = next((m for m in LI_CLOSED_MARKERS if m in page_l), None)
    if hit:
        print(f"  linkedin closed-check {jid}: CLOSED (http, matched {hit!r})")
        return True
    return False


def fetch_linkedin_detail(job_id, check_closed=False):
    """Fetch one LinkedIn guest job description (no login). Returns (text, email).

    With check_closed=True it also asks the public page whether the posting is
    still open — used for jobs already in the feed, so closed ones drop out
    instead of lingering. It costs one extra request, so the wide screening
    pass leaves it off."""
    jid = str(job_id)
    try:
        r = requests.get(LI_GUEST_JOB + jid, headers=LI_HEADERS, timeout=25)
    except Exception as e:
        print(f"  linkedin detail error {jid}: {e}")
        return None, None
    if r.status_code != 200 or not r.text.strip():
        print(f"  linkedin detail {jid}: HTTP {r.status_code}")
        return None, None
    # The description fragment itself occasionally carries a closed marker.
    page_l = r.text.lower()
    if any(m in page_l for m in LI_CLOSED_MARKERS):
        print(f"  linkedin detail {jid}: closed (marker in description)")
        return LI_CLOSED, None
    if check_closed and linkedin_is_closed(jid) is True:
        return LI_CLOSED, None
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.select_one(".show-more-less-html__markup, .description__text")
    text = (node.get_text("\n", strip=True) if node
            else soup.get_text("\n", strip=True))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    time.sleep(1)  # be gentle — LinkedIn rate-limits aggressively
    if len(text) < 200:
        return None, None
    # Emails hide two ways: as visible text, or inside a mailto: link whose
    # anchor text is "apply here" (so the plain-text regex misses it). Grab both.
    mailtos = []
    for a in soup.select('a[href^="mailto:"]'):
        addr = a.get("href", "")[7:].split("?")[0].strip()
        if addr:
            mailtos.append(addr)
    emails = mailtos + re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    clean = [e for e in emails if "@" in e and not any(
        b in e.lower() for b in ("linkedin.com", "example.", "noreply", "no-reply"))]
    return text[:15000], (clean[0] if clean else None)


def fetch_job_detail(browser, url, job_id, check_closed=False):
    """Render one job page in a headless browser and return its readable text
    + any apply email. VDAB is a JS app with a bot-protected API, so a real
    browser is the only reliable way to see the posting. LinkedIn jobs use the
    guest HTTP endpoint instead (no browser render).

    check_closed is for jobs already in the feed: it additionally verifies the
    posting still accepts applications (LinkedIn only — VDAB drops closed
    vacancies from its own pages)."""
    if "linkedin.com" in (url or ""):
        return fetch_linkedin_detail(job_id, check_closed=check_closed)
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

    soup = BeautifulSoup(html, "html.parser")
    emails = []
    # Clickable mailto: links are the most reliable signal.
    for a in soup.select('a[href^="mailto:"]'):
        addr = a.get("href", "").replace("mailto:", "").split("?")[0].strip()
        if addr:
            emails.append(addr)
    # ...but VDAB postings very often list the application address as PLAIN TEXT
    # at the bottom ("Solliciteer via voornaam@bedrijf.be"), with NO mailto link.
    # The old code only read mailto links, so those jobs came back with an empty
    # recipient and the app's Gmail button had nowhere to send. Also scan the
    # rendered text so we catch the plain-text addresses too.
    for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        emails.append(m)
    # Dedupe case-insensitively, drop VDAB's own / noise addresses, keep order
    # (so a real mailto beats a stray text match). Keep up to two — some
    # postings list two contacts.
    seen_e, clean = set(), []
    for e in emails:
        el = e.strip().strip(".,;:()<>[]").lower()
        if not el or el in seen_e:
            continue
        if any(bad in el for bad in ("vdab.be", "example.", "noreply", "no-reply",
                                     "sentry", ".png", ".jpg", ".gif", ".svg")):
            continue
        seen_e.add(el)
        clean.append(el)
    apply_email = ", ".join(clean[:2]) if clean else None

    return text[:15000], apply_email


# ---------------------------------------------------------------- AI steps

# Distinctive stopwords — only words that belong to ONE of the two languages.
# Shared spellings (we, in, is, of, team, ...) are deliberately left out, they
# carry no signal. The same two lists live in docs/index.html so the app and the
# bot always agree on a posting's language.
_NL_WORDS = {
    "de", "het", "een", "van", "en", "met", "voor", "wij", "jij", "jouw", "je",
    "ons", "onze", "bij", "naar", "niet", "ook", "wordt", "worden", "zijn",
    "heeft", "hebben", "dat", "deze", "die", "als", "maar", "kennis", "ervaring",
    "functie", "vacature", "werken", "medewerker", "opleiding", "taken",
    "profiel", "aanbod", "binnen", "jaar", "sollicitatie", "solliciteren",
    "zoeken", "bieden", "klanten", "bedrijf", "werk", "goede",
}
_EN_WORDS = {
    "the", "and", "you", "your", "our", "with", "for", "will", "are", "this",
    "have", "role", "experience", "skills", "work", "working", "about", "join",
    "looking", "candidate", "responsibilities", "requirements", "offer",
    "knowledge", "company", "years", "strong", "ability", "including", "please",
    "apply", "we're", "you'll",
}


def job_lang(*parts):
    """'en' if the posting is written in English, else 'nl'.

    Flanders is Dutch-first, so Dutch is the default: English only wins on a
    clear majority of distinctive stopwords. That keeps a Dutch posting with a
    few English buzzwords ("SEO specialist", "content marketing") in Dutch."""
    words = re.findall(r"[a-z']+", " ".join(p or "" for p in parts).lower())
    nl = sum(1 for w in words if w in _NL_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    return "en" if en > nl * 1.3 and en >= 8 else "nl"


def generate_application(job_text, cv_text, job_info=None, lang="nl"):
    """On-demand: write the full application email + cover letter + CV
    highlights for ONE job the user chose to apply to. Used by prepare.py.

    `lang` follows the posting: a Dutch vacancy gets Dutch documents, an English
    one gets English documents — the employer reads what they wrote in."""
    job_info = job_info or {}
    if lang == "en":
        style = """This posting is written in ENGLISH, so write EVERY document in
ENGLISH ONLY — no Dutch, and no "--- English version ---" separator."""
        fields = """  "email_subject": "short subject line in English (e.g. Application - <role>)",
  "email_body": "the complete application email in ENGLISH (110-160 words) ending with the signature block",
  "cover_letter": "the full cover letter in ENGLISH (250-330 words)","""
    else:
        style = """This posting is written in DUTCH and the employer is Flemish, so
write EVERY document in DUTCH ONLY — no English, and no "--- English version ---"
separator. Natural, correct Nederlands (the candidate has B1 Dutch and gets help
with writing — that is normal)."""
        fields = """  "email_subject": "short subject line in Dutch (e.g. Sollicitatie — <functie>)",
  "email_body": "the complete application email in DUTCH (110-160 words) ending with the signature block",
  "cover_letter": "the full cover letter (motivatiebrief) in DUTCH (250-330 words)","""
    prompt = f"""You are an expert career writer. Write application documents for this job,
based ONLY on the real CV below. NEVER invent experience, education, or skills
not in the CV. Professional but warm, no clichés. {style} Write all web
addresses as bare text (mirook.com, linkedin.com/in/baverok) — never markdown
links, never http(s):// prefixes.

Reply ONLY with JSON:
{{
{fields}
  "cv_highlights": "5 bullet points (one newline-separated string) reordering the CV's most relevant points for THIS job",
  "tailored_cv": "the FULL CV tailored to THIS job, written in the SAME language as the documents above, as plain text with the same sections (name/contact, PROFILE, CORE SKILLS, EXPERIENCE, PROJECTS, EDUCATION). Reorder skills and bullets so the most relevant for this job come first, and reword the profile paragraph toward this role. Keep every fact identical to the real CV — same employers, dates, titles, tools; NOTHING invented, nothing removed except trimming clearly irrelevant bullets."
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


def write_letter(job_id, url, job_text, apply_email, cv_text, info=None):
    """Pre-write the application email + cover letter for one matched job and
    save it where the app's '✍️ Write my letter' button reads it
    (docs/prepared/<id>.json). Returns True on success. Never raises — a failed
    letter must not break the screening run; the next run retries it."""
    out = os.path.join(PREPARED_DIR, f"{job_id}.json")
    info = info or {}
    lang = job_lang(info.get("title"), job_text)
    try:
        docs = generate_application(job_text, cv_text, info, lang)
    except QuotaExhausted:
        return False
    if not docs:
        return False
    save_json(out, {
        "id": job_id,
        "url": url,
        "status": "ready",
        "apply_email": apply_email or "",
        "lang": lang,
        "email_subject": docs.get("email_subject", ""),
        "email_body": docs.get("email_body", ""),
        "cover_letter": docs.get("cover_letter", ""),
        "cv_highlights": docs.get("cv_highlights", ""),
        "tailored_cv": docs.get("tailored_cv", ""),
        "fmt": 3,
    })
    return True


def has_letter(job_id):
    """A letter counts as complete only if it includes the tailored CV — older
    letters without one get regenerated by backfill_letters.

    fmt 2 letters are the old bilingual ones (Dutch + '--- English version ---'
    + English). They no longer count: backfill_letters re-reads the posting and
    rewrites them in the posting's OWN language (fmt 3). Until a letter's turn
    comes the app keeps showing the bilingual text, which still works."""
    p = os.path.join(PREPARED_DIR, f"{job_id}.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        return bool(d.get("tailored_cv")) and d.get("fmt", 0) >= 3
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _sync_letter_email(job_id, apply_email):
    """Patch an already-written letter's recipient when we later recover the
    application address (the first scrape missed a plain-text email). Without
    this, a job with an existing letter would keep an empty recipient in its
    prepared file even after jobs.json gets backfilled."""
    if not apply_email:
        return
    p = os.path.join(PREPARED_DIR, f"{job_id}.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if (d.get("apply_email") or "").strip():
        return  # already has one — don't overwrite
    d["apply_email"] = apply_email
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"  📧 backfilled recipient for {job_id}: {apply_email}")


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
- Languages: English (professional), Turkish (native), Dutch B1
  (conversational — taking classes, improving fast), no French.

EXPERIENCE RULE (important): the candidate has only ~4 months of professional
experience. Jobs asking for UP TO ~2 years are acceptable (a reach, score lower).
Jobs that clearly require 2+ years of dedicated experience → FAIL.

LANGUAGE RULE: the candidate works in English and has B1 (conversational) Dutch.
PASS jobs that are in English, accept English, or need Dutch up to B1 /
conversational / "goede kennis" or Dutch "as a plus". Jobs needing FLUENT/native
Dutch are a stretch, not a fail. FAIL only jobs that require any French.

WHAT THE CANDIDATE CANNOT DO (must FAIL):
- Skilled trades / production / machine operation / metalwork / construction.
- Roles needing a licence/certificate (forklift, C/CE, nursing, medical/lab,
  pilot, professional finance/engineering cert).
- Roles that require a completed bachelor or master degree (ANY field — the
  candidate has NO degree) unless the posting explicitly accepts "or equivalent
  by experience" or lists the degree only as a plus. A stated required diploma
  (e.g. "STUDIEVEREISTEN: Master: Marketing") disqualifies.
- Specialised senior backgrounds (finance/tax/KYC, R&D, medical, aviation).
- Senior / Lead / Director / Head leadership roles (a plain "<function> Manager"
  in marketing/SEO/content is NOT auto-excluded), or anything needing 5+ years."""


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
a TRUE leadership role (Senior, Sr., Lead, Team Lead, Head of, Director, VP, Chief,
Principal); or a licensed/degree profession (engineer, doctor, nurse, lawyer,
licensed accountant). IMPORTANT: do NOT drop a "<function> Manager / Specialist /
Coordinator" title in marketing / SEO / content / social / digital / web /
e-commerce / campaign / brand / community / account — e.g. "SEO Manager",
"Marketing Manager", "Content Manager", "Social Media Manager" are usually
individual-contributor or first-line roles and MUST be kept for a full check.
Do NOT drop a title just because it might want a bachelor. When unsure, KEEP.

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

LANGUAGE (read the must-haves / talenkennis section CAREFULLY — postings are in
Dutch/French, so recognise the wording):
- FRENCH required at ANY level → FAIL. Trigger words: "Frans", "français",
  "goede/basis kennis Frans", "bonne connaissance du français", "tweetalig
  NL/FR", "NL/FR", "FR/NL", "Nederlands én Frans". (Candidate has NO French.)
- English / accepts English / Dutch up to B1: "basis Nederlands", "goede kennis
  Nederlands", conversational Dutch, or Dutch "een pluspunt" → language is fine,
  NOT a stretch (the candidate has B1 Dutch).
- FLUENT Dutch required but NO French and the job otherwise fits → do NOT fail;
  PASS as a STRETCH with "dutch_stretch": true and match_score ≤ 35. Trigger
  words for fluent Dutch: "vlot Nederlands", "vloeiend Nederlands", "zeer goed
  Nederlands", "uitstekend Nederlands", "moedertaal Nederlands",
  "Nederlandstalig", "perfect Nederlands".
- IMPORTANT: a job that needs BOTH fluent Dutch AND French (e.g. "Vlot Nederlands,
  goede kennis Frans en Engels") requires French → FAIL, not a stretch.

INTERNSHIP: if this posting is an internship / stage / stagiair(e) / traineeship,
set "internship": true and be LENIENT — an intern learns on the job, so do NOT
fail it for lacking years of experience, a degree, or specific software/skills.
(Still FAIL an internship only if it requires French, OR requires a school
internship convention — see next.) A professional internship is NOT the same as a
"studentenjob / jobstudent / vakantiejob" side-job (those are still excluded).
- SCHOOL-CONVENTION internships → FAIL. The candidate is NOT an enrolled student,
  so any internship that requires a school internship agreement / convention, or
  that the applicant currently be a student, is out of reach. Trigger words (any
  language): "internship convention", "internship agreement", "convention through
  school", "school convention", "tripartite agreement", "must be enrolled",
  "enrolled student", "student status required", "you must be a student",
  "stageovereenkomst", "stage-overeenkomst", "schoolstage", "via je (hoge)school",
  "ingeschreven student", "onderwijsinstelling vereist", "je bent student",
  "convention de stage". A paid/professional internship open to non-students
  (no school agreement mentioned) is still FINE — only fail when a school
  convention or current student status is actually required.

FAIL the job if ANY of these is true (hard walls — no exceptions, but the
INTERNSHIP leniency above overrides the experience/degree/skill walls):
- FRENCH required at any level (see LANGUAGE above).
- EXPERIENCE: only a clear 5+ years requirement is a hard FAIL. Up to ~2 years →
  normal PASS (score lower). A MID ask of ~2-4 years that is NOT senior → do NOT
  fail; PASS as a STRETCH with "exp_stretch": true and match_score ≤ 40 (the
  candidate has ~4 months but may still apply). (Seniority titles are handled by
  the SENIORITY wall below — those still FAIL.)
- DESIGN SOFTWARE / GRAPHIC DESIGN the CV lacks: FAIL only when the posting
  EXPLICITLY REQUIRES graphic-design competency or professional design software the
  candidate does not have — it names Adobe Illustrator / InDesign / Photoshop /
  Creative Suite as required, OR lists "grafisch design", "grafische vormgeving",
  "graphic design", "print design", "grafisch vormgever" as a required skill (e.g.
  "sterke kennis van grafisch design (must)"). Do NOT invent this reason: producing
  visuals, social-media graphics, banners, brochures, beeldmateriaal or simple
  video is Canva-doable and the candidate CAN do it — such tasks alone are NOT a
  reason to fail, and unrelated things (e.g. applying screen protectors, product
  photos) are NOT graphic design. The candidate does WEB design + Canva
  (WordPress/Elementor/Canva), not professional graphic design. A web / WordPress /
  UX / content / social-media / customer-service role that merely produces visuals
  is FINE; only a clearly stated graphic-design / Adobe requirement fails.
  IMPORTANT — WEB / UX / UI / front-end DESIGNER roles: naming Figma, Adobe XD,
  Sketch, or "Adobe Creative Suite" as a *design tool* is NORMAL for web/UX work
  and is NOT a graphic-design hard-fail — the candidate does web design and can
  pick these up, especially when the posting says "and/or", "familiarity",
  "willing to learn", "a plus", or "basic". A "portfolio" request is likewise NOT
  a hard fail (the candidate has real web/WordPress work at mirook.com). Basic
  HTML/CSS "familiarity or interest" is fine too — it is NOT a developer wall.
  Treat such a web-design role as a PASS (exp_stretch if it asks for ~2+ years).
- SKILLED TRADE / PRODUCTION / MANUAL role: machine/production/CNC operator,
  metalwork, welding, grinding, assembly, manufacturing, chocolatier, print/line
  operator, construction, electrical, mechanical, maintenance technician.
- LICENCE / CERTIFICATE the CV lacks: forklift/reachtruck, C/CE, nursing,
  medical/lab, pilot, professional finance/engineering certification.
- REQUIRED DEGREE the candidate lacks: the candidate has NO completed degree
  (only some coursework). So FAIL if the posting HARD-REQUIRES a completed
  bachelor OR master (ANY field, including marketing/communication/business) and
  does NOT offer an "or equivalent by experience" route. Watch for an explicit
  study-requirement field — e.g. "STUDIEVEREISTEN: Master: Marketing",
  "Bachelor: ...", "vereist diploma", "must hold a Bachelor/Master" — that is a
  hard requirement → FAIL.
- SENIORITY: a TRUE leadership title FAILs — Senior / Sr. / Lead / Team Lead /
  Head of / Director / VP / Vice President / Chief / C-level / Principal. BUT do
  NOT fail on the word "Manager" alone: a "<function> Manager" in marketing / SEO /
  content / social / digital / web / e-commerce / campaign / brand / community /
  account (e.g. "SEO Manager", "Marketing Manager", "Content Manager") is usually
  an individual-contributor / first-line role — judge it on its real requirements
  (if it asks for a few years' experience, PASS as exp_stretch, do NOT hard-fail).
- SCHOOL INTERNSHIP CONVENTION or current-student status required (see INTERNSHIP
  above) — the candidate is not enrolled in a school and cannot provide one.
- Cleaning / domestic-help / studentenjob side-job.

DEGREE NUANCE (this matters): only KEEP a degree-mentioning job when the degree
is NOT strictly mandatory — i.e. it says "bachelor OR equivalent by experience",
or the degree is "a plus" / "preferred", or no specific completed diploma is
actually required. A firm "Master/Bachelor in X required" with no experience
alternative must FAIL, even for a marketing role.

Otherwise PASS — the candidate may apply even if it's a stretch. Especially KEEP
anything in or near the GOAL FIELD: digital marketing, SEO/SEA, content,
copywriting, social media, WordPress / web / web design, front-end, e-commerce,
online marketing, communication. For GOAL-FIELD jobs be inclusive — pass unless a
hard wall above truly applies (fluent Dutch/French, 2+ years, a skilled
trade/licence, seniority, or a mandatory bachelor/master with no experience
alternative). Also PASS accessible roles: customer service, administration /
office support, reception, data entry, sales / commercial support, general
warehouse & logistics, and "no experience needed" roles. When unsure about an
accessible role, PASS with a low score; when a required degree is clearly stated
with no experience route, FAIL.

STEP 2 — Summarise, honestly, either way. For a PASS that is a stretch, still say
in why_good what the candidate would be leaning on and note the gap frankly.

Reply ONLY with JSON:
{{
  "pass": true or false,
  "reason": "one short sentence: the single main reason for the pass/fail decision",
  "title": "the job title",
  "company": "the company name or 'Unknown'",
  "location": "city or 'Unknown'",
  "match_score": 0-100 — 75-100 = clearly qualified; 50-74 = can apply, minor gaps; 30-49 = a reach; a dutch_stretch or exp_stretch job is capped at 40,
  "dutch_stretch": true or false — true ONLY when the job would fit but requires Dutch above A2 (and no French); false otherwise,
  "exp_stretch": true or false — true when the main gap is a ~2-4 year experience ask (not senior, not 5+) the junior candidate could still apply to; false otherwise,
  "internship": true or false — true if this is an internship / stage / traineeship,
  "details": "4-6 short bullets (one newline-separated string): role, main tasks, contract type, schedule, language, pay if stated (or '')",
  "why_good": "if pass: 3-5 short bullets (one newline-separated string) on why it fits, grounded ONLY in the real CV; for a dutch_stretch job also state plainly that it needs stronger Dutch than A2; for an exp_stretch job state plainly it asks for more years than the candidate has but is still worth a shot. If fail: ''",
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
    # When the title-screen rules change (e.g. now keeping "<function> Manager"
    # marketing roles), wipe the dropped set once so every past title is re-screened
    # under the new rules and newly-eligible roles resurface.
    if screen.get("title_screen_v") != TITLE_SCREEN_VERSION:
        print(f"Title-screen rules updated (v{TITLE_SCREEN_VERSION}) — re-screening "
              f"{len(title_no)} previously dropped titles.")
        title_no = set()

    def checkpoint():
        """Persist current progress and push it, so a long run that dies partway
        (or is stopped) keeps everything screened so far. Best-effort: never let a
        git hiccup crash the scan."""
        # Refresh the timestamp on every checkpoint so the app shows the scan is
        # live and working, not frozen at the last full-run's time.
        jobs["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        save_json(JOBS_FILE, jobs)
        save_json(SEEN_FILE, sorted(seen))
        save_json(SCREEN_FILE, {"title_no": sorted(title_no), "shortlist": sorted(shortlist), "title_screen_v": TITLE_SCREEN_VERSION})
        try:
            subprocess.run(["git", "add", JOBS_FILE, SEEN_FILE, SCREEN_FILE, PREPARED_DIR],
                           check=False, capture_output=True)
            r = subprocess.run(
                ["git", "-c", "user.name=job-bot",
                 "-c", "user.email=bot@users.noreply.github.com",
                 "commit", "-q", "-m", "Update jobs (checkpoint)"],
                check=False, capture_output=True)
            if r.returncode == 0:
                # HEAD:main works even if checkout left us on a detached HEAD.
                # Resilient push: if another run/deploy advanced main, a plain
                # push is rejected non-fast-forward. Sync (keep our authoritative
                # outputs) and retry a couple of times instead of giving up.
                pushed = False
                for _ in range(3):
                    p = subprocess.run(["git", "push", "origin", "HEAD:main"],
                                       check=False, capture_output=True, text=True)
                    if p.returncode == 0:
                        pushed = True
                        break
                    subprocess.run(["git", "fetch", "origin", "main"],
                                   check=False, capture_output=True)
                    # -X ours (not -s ours): resolve conflicts in OUR favour but
                    # keep unrelated changes from main (e.g. a mid-run app deploy).
                    m = subprocess.run(["git", "-c", "user.name=job-bot",
                                        "-c", "user.email=bot@users.noreply.github.com",
                                        "merge", "-X", "ours", "--no-edit", "origin/main"],
                                       check=False, capture_output=True)
                    if m.returncode != 0:
                        subprocess.run(["git", "merge", "--abort"],
                                       check=False, capture_output=True)
                print("  [checkpoint pushed]" if pushed
                      else "  [checkpoint push skipped — will retry next checkpoint]")
        except Exception as e:
            print(f"  (checkpoint push skipped: {e})")

    matched = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            # FIRST: write letters for any matched job still missing one, so the
            # app's letter button and Gmail drafts are ready minutes into the run
            # instead of hours (user shouldn't wait on the long screening pass).
            # Only a few, though: with DeepSeek out of credit the whole run rides
            # on Gemini's small free quota, and letters used to drain it before a
            # single new job was screened. The rest of the backfill runs at the
            # end, on whatever quota screening leaves.
            backfill_letters(browser, jobs, cv_text, budget=6, checkpoint=checkpoint)

            # Then drop any matched LinkedIn posting that has closed since we
            # saved it, so the Ready feed only ever offers jobs you can apply to.
            sweep_closed(browser, jobs, budget=40, checkpoint=checkpoint)

            # Then: screen NEW marketing jobs (below); re-vet the already-saved
            # pool LAST with a small budget so screening is never starved.

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
                # Light collection: the listing is already comprehensive, so we
                # only need the newest results each run. Keep it fast so the run's
                # time goes to SCREENING the shortlist, not re-collecting.
                priority = url in PRIORITY_SEARCH_URLS
                # Page marketing searches deeper (they're the target field — we
                # want the FULL result set), fillers stay light.
                links = collect_links(browser, url,
                                      budget_s=75 if priority else 25,
                                      max_pages=40 if priority else 12)
                print(f"  {len(links)} links from {url}")
                all_links |= links

            # Secondary source: LinkedIn public guest search (marketing/SEO/
            # content keywords, Belgium). Best-effort — a block is swallowed and
            # the VDAB results stand on their own. The on-target keywords run
            # every time; the broader ones rotate a slice per run so one run
            # never fires enough requests to earn a rate-limit block.
            li_cur = jobs.get("li_cursor", 0)
            li_n = len(LI_ROTATING_KEYWORDS)
            li_rot = [LI_ROTATING_KEYWORDS[(li_cur + k) % li_n]
                      for k in range(min(LI_ROTATE_PER_RUN, li_n))]
            jobs["li_cursor"] = (li_cur + len(li_rot)) % li_n
            li_meta = collect_linkedin(LI_PRIORITY_KEYWORDS + li_rot)
            for _m in li_meta.values():
                all_links.add((_m["url"], _m["id"]))

            # Third source: the English-language Belgian job boards. A posting
            # that also exists on LinkedIn is dropped here — same job, and the
            # LinkedIn copy is the one already wired into the app. What survives
            # is the set you can ONLY apply to on that board.
            drop_stale_matches(jobs)
            drop_unreachable_matches(jobs)
            bad_board = prune_board_listings(jobs)
            if bad_board:
                for _s in (shortlist, title_no):
                    _s -= bad_board
            board_meta = collect_boards(browser)
            if board_meta:
                known = {dedupe_key(j.get("title"), j.get("company"))
                         for j in jobs.get("listing", [])}
                known |= {dedupe_key(m["title"], m.get("company"))
                          for m in li_meta.values()}
                known.discard(None)
                dropped = 0
                for _m in list(board_meta.values()):
                    k = dedupe_key(_m["title"], _m.get("company"))
                    if k and k in known:
                        board_meta.pop(_m["id"], None)
                        dropped += 1
                        continue
                    if k:
                        known.add(k)
                    all_links.add((_m["url"], _m["id"]))
                print(f"  boards: {len(board_meta)} new, {dropped} already covered "
                      f"by LinkedIn/VDAB")

            # Accumulate the master listing across runs (union by id), dropping
            # roles the candidate never wants. This is what keeps coverage growing
            # instead of being pinned to a single search's results.
            listing = {j["id"]: j for j in jobs.get("listing", [])}
            for (u, i) in all_links:
                m = li_meta.get(i) or board_meta.get(i)
                if m:  # LinkedIn / job-board item — it carries a real title
                    if is_excluded(m["title"]) or is_ineligible(m["title"]):
                        continue
                    listing[i] = {"id": i, "url": u, "title": m["title"],
                                  "company": m.get("company", ""),
                                  "location": m.get("location", ""),
                                  "src": m.get("src", "linkedin")}
                    continue
                t = _slug_title(u)
                if is_excluded(t) or is_ineligible(t):
                    continue
                listing[i] = {"id": i, "url": u, "title": t}
            # Keep EVERY marketing/SEO/web listing we've ever collected (the
            # candidate's target field — never evict it) PLUS the newest ~8000 of
            # everything else, so the browse list covers all digital-marketing
            # jobs on VDAB, not just a recent window that fillers push them out of.
            allv = list(listing.values())
            mkt = [j for j in allv if is_marketing(j["title"])]
            rest = sorted((j for j in allv if not is_marketing(j["title"])),
                          key=lambda j: j["id"], reverse=True)[:8000]
            jobs["listing"] = sorted(mkt + rest, key=lambda j: j["id"], reverse=True)
            by_id = {j["id"]: j for j in jobs["listing"]}
            checkpoint()   # save the freshly-collected listing before screening

            # Cheap title pre-screen: shortlist plausible titles, drop clear
            # non-fits — WITHOUT rendering — so the expensive render+full-eval is
            # spent only on jobs worth it. This is what makes wide coverage cheap.
            cand = [j for j in jobs["listing"]
                    if j["id"] not in seen and j["id"] not in title_no
                    and j["id"] not in shortlist]
            # Screen target-field titles first, then newest.
            cand.sort(key=lambda j: (title_priority(j["title"]), -int(j["id"])))
            cand = cand[:TITLE_SCREEN_CAP]
            if cand:
                print(f"Title pre-screening {len(cand)} titles...")
                kept = title_prescreen([c["title"] for c in cand])
                for i, c in enumerate(cand):
                    (shortlist if i in kept else title_no).add(c["id"])
                print(f"  shortlisted {len(kept)}, dropped {len(cand) - len(kept)} at title stage")

            # Full render + AI evaluation, drawn from the shortlist only —
            # closest to the target field first, then newest. Sorting by "is it
            # marketing at all" left ~2000 shortlisted jobs in id order, so an SEO
            # posting from last month sat behind every newer generic marketing one
            # and, at a couple of reads per run, was never reached.
            ready_ids = [i for i in shortlist if i in by_id and i not in seen]
            # A posting too old to still be open is not worth a read, and must
            # never reach Ready — the feed is for jobs you can apply to today.
            newest_li = newest_linkedin_id(jobs)
            stale = [i for i in ready_ids if li_age_days(i, newest_li) > LI_MAX_AGE_DAYS]
            if stale:
                ready_ids = [i for i in ready_ids if i not in set(stale)]
                title_no |= set(stale)          # don't re-queue them next run
                shortlist -= set(stale)
                print(f"  skipped {len(stale)} LinkedIn posting(s) older than "
                      f"{LI_MAX_AGE_DAYS} days (almost certainly closed)")
            # Abroad and not advertised as remote: don't spend a read on it. The
            # listing already carries the location, so this costs nothing.
            away = [i for i in ready_ids
                    if is_far_away(by_id[i].get("title"), by_id[i].get("company"),
                                   by_id[i].get("location"))
                    and not REMOTE_RX.search(by_id[i].get("location") or "")]
            if away:
                ready_ids = [i for i in ready_ids if i not in set(away)]
                title_no |= set(away)
                shortlist -= set(away)
                print(f"  skipped {len(away)} posting(s) outside Belgium "
                      f"with no remote option")
            ready_ids.sort(key=lambda i: (title_priority(by_id[i]["title"]), -int(i)))
            new_links = [(by_id[i]["url"], i) for i in ready_ids][:MAX_NEW_PER_RUN]
            print(f"{len(all_links)} collected, {len(jobs['listing'])} in listing, "
                  f"{len(shortlist)} shortlisted, {len(title_no)} title-dropped, "
                  f"{len(new_links)} queued for full screening")

            matched = _process_jobs(browser, new_links, seen, jobs, cv_text,
                                    checkpoint=checkpoint)
            shortlist -= seen   # drop the ones we just fully evaluated

            # Now the rest of the letter backfill, on the quota screening left
            # over. Finding a job the candidate can apply to comes before having
            # its letter pre-written.
            backfill_letters(browser, jobs, cv_text, budget=40, checkpoint=checkpoint)

            # LAST, with whatever time/quota remains: re-check a small slice of the
            # saved pool under the current criteria (e.g. move Dutch-required
            # marketing jobs into the stretch section). Small budget so it never
            # starves the new-job screening above.
            revet_saved(browser, jobs, cv_text, budget=80, checkpoint=checkpoint)
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
    # Clean fits first, then stretch jobs (needs-better-Dutch OR needs-more-years);
    # keep plenty so the stretch section isn't truncated. Highest score first.
    kept.sort(key=lambda j: (bool(j.get("dutch_stretch")) or bool(j.get("exp_stretch")),
                             -int(j.get("match_score", 0) or 0)))
    jobs["jobs"] = kept[:600]
    jobs["rejected"] = jobs.get("rejected", [])[:REJECTED_CAP]
    save_json(JOBS_FILE, jobs)
    save_json(SEEN_FILE, sorted(seen))
    save_json(SCREEN_FILE, {"title_no": sorted(title_no), "shortlist": sorted(shortlist), "title_screen_v": TITLE_SCREEN_VERSION})
    print(f"\nDone. {matched} new match(es) this run. "
          f"{len(jobs['jobs'])} in Ready, {len(jobs['rejected'])} not-a-fit. "
          f"Screen state: {len(shortlist)} shortlisted, {len(title_no)} title-dropped.")


def _apply_verdict(jobs, job_id, url, verdict, apply_email, found_at=None,
                   lang=None):
    """Place a job into the matched pool or the 'rejected' (not-a-fit) pool
    based on the verdict, de-duplicating by id across both pools so a job never
    appears twice or lingers in the wrong list after being re-evaluated.
    Returns True if it landed in the matched pool.

    `lang` is the posting's own language ('nl'/'en'). It has to be recorded
    here, from the real posting text: `details` is an English AI summary, so the
    app cannot work the language out from jobs.json on its own."""
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
        "dutch_stretch": bool(verdict.get("dutch_stretch")),
        "exp_stretch": bool(verdict.get("exp_stretch")),
        "internship": bool(verdict.get("internship")),
        "lang": lang or "nl",
        "cv_fit_v": CRITERIA_VERSION,
    }
    jobs["jobs"] = [j for j in jobs["jobs"] if j.get("id") != job_id]
    jobs["rejected"] = [j for j in jobs.get("rejected", []) if j.get("id") != job_id]
    if verdict.get("pass"):
        jobs["jobs"].insert(0, entry)
        return True
    jobs["rejected"].insert(0, entry)
    return False


def _drop_job(jobs, job_id):
    """Remove a job from BOTH the matched and rejected pools (e.g. a posting that
    has closed). It stays in `seen`, so it is not re-collected on later runs."""
    n0 = len(jobs["jobs"]) + len(jobs.get("rejected", []))
    jobs["jobs"] = [j for j in jobs["jobs"] if j.get("id") != job_id]
    jobs["rejected"] = [j for j in jobs.get("rejected", []) if j.get("id") != job_id]
    return n0 != len(jobs["jobs"]) + len(jobs.get("rejected", []))


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
        job_text, apply_email = fetch_job_detail(browser, url, job_id,
                                                 check_closed=True)
        if job_text == LI_CLOSED:
            _drop_job(jobs, job_id)
            print("  CLOSED — removed (no longer accepting applications)")
            moved += 1
            if checkpoint and moved % CHECKPOINT_EVERY == 0:
                checkpoint()
            continue
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
                              found_at=j.get("found_at"),
                              lang=job_lang(j.get("title"), job_text))
        print(f"  {'FITS' if kept else 'NOT A FIT'} "
              f"({verdict.get('match_score')}%): {verdict.get('reason')}")
        if kept and not has_letter(job_id):
            if write_letter(job_id, url, job_text,
                            apply_email or j.get("apply_email"), cv_text,
                            {"title": verdict.get("title", ""),
                             "company": verdict.get("company", "")}):
                print("  ✍️ letter written")
        elif kept and apply_email:
            # Letter already exists — just backfill a recipient we newly found.
            _sync_letter_email(job_id, apply_email)
        moved += 1
        if checkpoint and moved % CHECKPOINT_EVERY == 0:
            checkpoint()
        time.sleep(1)
    print(f"Re-vet done: {moved} re-checked.")
    return moved


def sweep_closed(browser, jobs, budget=40, checkpoint=None):
    """Re-check matched LinkedIn jobs and drop the ones that stopped accepting
    applications.

    This needs its own pass. The other two paths that re-read a posting go
    dormant: revet_saved only touches jobs stamped with an older
    CRITERIA_VERSION, and backfill_letters only touches jobs still missing a
    letter — so once the pool is settled, nothing would ever notice a posting
    closing and the feed would slowly fill with dead jobs again.

    Round-robins by `closed_check` (never-checked first, then oldest), so a
    small per-run budget still covers the whole feed every day. Jobs last seen by
    an older CLOSED_CHECK_VERSION queue with the never-checked ones: their verdict
    came from a method that could not see the banner at all, so it carries no
    information and must not keep them at the back of the line. VDAB is skipped:
    it takes closed vacancies off its own pages, so they stop resolving anyway."""
    li = [j for j in jobs["jobs"] if "linkedin.com" in (j.get("url") or "")]
    if not li:
        return 0
    li.sort(key=lambda j: (j.get("closed_check") or "")
            if j.get("closed_check_v") == CLOSED_CHECK_VERSION else "")
    todo = li[:budget]
    print(f"\nChecking {len(todo)} of {len(li)} LinkedIn match(es) for closed postings...")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    dropped = checked = 0
    for j in todo:
        state = linkedin_is_closed(j.get("id"), browser=browser)
        j["closed_check"] = now          # don't retry the same job next run
        j["closed_check_v"] = CLOSED_CHECK_VERSION
        checked += 1
        if state is True:
            _drop_job(jobs, j.get("id"))
            print(f"  dropped (closed): {j.get('title', '')[:60]}")
            dropped += 1
        if checkpoint and checked % 10 == 0:
            checkpoint()
        time.sleep(1)                    # be gentle — LinkedIn rate-limits
    print(f"Closed sweep done: {dropped} dropped of {checked} checked.")
    return dropped


def backfill_letters(browser, jobs, cv_text, budget=30, checkpoint=None):
    """Write letters for matched jobs that don't have one yet (e.g. matched
    before letters existed). Best fits first; re-renders the posting to get its
    text. Budgeted so it never starves the screening pass."""
    todo = [j for j in jobs["jobs"] if not has_letter(j.get("id"))]
    todo.sort(key=lambda j: (bool(j.get("dutch_stretch")),
                             -int(j.get("match_score", 0) or 0)))
    todo = todo[:budget]
    if not todo:
        return 0
    print(f"\nWriting letters for {len(todo)} matched job(s) without one...")
    done = 0
    for j in todo:
        job_id, url = j.get("id"), j.get("url")
        job_text, apply_email = fetch_job_detail(browser, url, job_id,
                                                 check_closed=True)
        if job_text == LI_CLOSED:
            _drop_job(jobs, job_id)
            print(f"  CLOSED — removed {j.get('title','')[:50]}")
            continue
        if not job_text:
            continue
        if apply_email and not (j.get("apply_email") or "").strip():
            j["apply_email"] = apply_email  # keep the card's recipient in sync
        # We have the real posting here — record its language on the card too,
        # so the app can label it without re-reading the posting.
        j["lang"] = job_lang(j.get("title"), job_text)
        if write_letter(job_id, url, job_text,
                        apply_email or j.get("apply_email"), cv_text,
                        {"title": j.get("title", ""), "company": j.get("company", "")}):
            done += 1
            print(f"  ✍️ {j.get('title', '')[:50]}")
            if checkpoint and done % 10 == 0:
                checkpoint()
        time.sleep(1)
    print(f"Letters done: {done}.")
    return done


def _process_jobs(browser, new_links, seen, jobs, cv_text, checkpoint=None):
    matched = 0
    ai_fails = 0
    processed = 0
    for url, job_id in new_links:
        print(f"\nChecking job {job_id}: {url}")

        job_text, apply_email = fetch_job_detail(browser, url, job_id)
        if job_text == LI_CLOSED:
            print("  (closed — no longer accepting applications; skipping)")
            seen.add(job_id)  # settled state, no point re-checking
            continue
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
        kept = _apply_verdict(jobs, job_id, url, verdict, apply_email,
                              lang=job_lang(verdict.get("title"), job_text))
        if kept:
            print(f"  MATCH ({verdict.get('match_score')}%): {verdict.get('title')}")
            matched += 1
            # Pre-write the application letter now, while we have the posting
            # text in hand — the app's button then works instantly, no setup.
            if write_letter(job_id, url, job_text, apply_email, cv_text,
                            {"title": verdict.get("title", ""),
                             "company": verdict.get("company", "")}):
                print("  ✍️ letter written")
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
