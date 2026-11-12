#!/usr/bin/env python3
"""Read-only: try candidate job sources the way the bot would, and print what each
returns, so collectors are only written for sources that really answer. No commit."""
import re, json, time
import requests
from playwright.sync_api import sync_playwright
import bot

UA = bot.HEADERS["User-Agent"]
JOBLINK = re.compile(r"/job|vacan|viewjob|jk=|/vacature|/emploi|/offre|/position|/ad/|/jobs/", re.I)

PAGES = [
    ("indeed.be",        "https://be.indeed.com/jobs?q=seo&l=Belgi%C3%AB&sort=date"),
    ("indeed.com",       "https://www.indeed.com/jobs?q=seo&l=Belgium&sort=date"),
    ("jobat.be",         "https://www.jobat.be/nl/jobs?keywords=seo"),
    ("references.be",    "https://www.references.be/en/jobs?q=seo"),
    ("jobrapido",        "https://be.jobrapido.com/?w=seo&l=Belgium"),
    ("monster.be",       "https://www.monster.be/jobs/search?q=seo&where=belgium"),
    ("talent.com",       "https://be.talent.com/jobs?k=seo&l=Belgium"),
    ("jooble",           "https://be.jooble.org/SearchResult?ukw=seo"),
    ("careerjet",        "https://www.careerjet.be/search/jobs?s=seo&l=Belgium"),
    ("jobbird",          "https://www.jobbird.com/nl/vacature?s=seo"),
    ("randstad.be",      "https://www.randstad.be/jobs/q-seo/"),
    ("eurobrussels",     "https://www.eurobrussels.com/jobs/search?keywords=marketing"),
    ("welcometothejungle","https://www.welcometothejungle.com/en/jobs?query=seo&refinementList%5Boffices.country_code%5D%5B%5D=BE"),
    ("glassdoor.be",     "https://www.glassdoor.be/Job/belgium-seo-jobs-SRCH_IL.0,7_IN25_KO8,11.htm"),
    ("werk.be",          "https://www.vdab.be/vindeenjob/vacatures?trefwoord=seo"),   # control: known good
]

APIS = [
    ("arbeitnow",  "https://www.arbeitnow.com/api/job-board-api"),
    ("remotive",   "https://remotive.com/api/remote-jobs?category=marketing&limit=50"),
    ("remoteok",   "https://remoteok.com/api"),
    ("jobicy",     "https://jobicy.com/api/v2/remote-jobs?count=50&geo=europe&industry=marketing"),
    ("himalayas",  "https://himalayas.app/jobs/api?limit=20"),
    ("adzuna(no key)", "https://api.adzuna.com/v1/api/jobs/be/search/1?results_per_page=5&what=seo"),
]

def sq(s, n=160): return " ".join((s or "").split())[:n]

print("=========== PLAIN HTTP")
for name, url in PAGES:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en,nl;q=0.8"}, timeout=25, allow_redirects=True)
        t = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        n = len(re.findall(r'href="[^"]*(?:/job|vacan|viewjob|jk=|/vacature)[^"]*"', r.text, re.I))
        blocked = bool(re.search(r"captcha|cloudflare|just a moment|access denied|verify you are|robot", r.text[:4000], re.I))
        print(f"[{name}] HTTP {r.status_code} bytes={len(r.content)} jobish-links={n} blocked-words={blocked} title={sq(t.group(1) if t else '', 80)!r}")
    except Exception as e:
        print(f"[{name}] ERROR {type(e).__name__}: {sq(str(e),100)}")

print("\n=========== JSON APIS")
for name, url in APIS:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25)
        print(f"[{name}] HTTP {r.status_code} bytes={len(r.content)}")
        if r.status_code == 200:
            d = r.json()
            rows = d if isinstance(d, list) else d.get("data") or d.get("jobs") or d.get("results") or []
            rows = [x for x in rows if isinstance(x, dict) and (x.get("title") or x.get("position") or x.get("jobTitle"))]
            print(f"    {len(rows)} postings; keys={list(rows[0].keys())[:14] if rows else '-'}")
            for x in rows[:3]:
                print("    -", sq(str(x.get("title") or x.get("position") or x.get("jobTitle")), 60), "|",
                      sq(str(x.get("location") or x.get("candidate_required_location") or x.get("jobGeo") or ""), 40))
            if name == "arbeitnow":
                be = [x for x in rows if re.search(r"belg|brussel|antwerp|gent|ghent|leuven|bruxelles", str(x.get("location")), re.I)]
                print("    Belgium-located:", len(be))
        else:
            print("    body:", sq(r.text, 140))
    except Exception as e:
        print(f"[{name}] ERROR {type(e).__name__}: {sq(str(e),100)}")

print("\n=========== BROWSER (what a real visitor sees)")
with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--no-sandbox"])
    for name, url in PAGES:
        pg = b.new_page(user_agent=UA, locale="en-BE")
        try:
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=35000)
            pg.wait_for_timeout(5000)
            body = pg.inner_text("body")
            links = pg.eval_on_selector_all("a[href]", "els=>els.map(e=>[e.href,(e.innerText||'').trim().slice(0,60)])")
            jl = [l for l in links if JOBLINK.search(l[0])]
            print(f"[{name}] http={resp.status if resp else None} final={pg.url[:70]} text={len(body)} jobish-links={len(jl)} title={sq(pg.title(),60)!r}")
            print(f"     starts: {sq(body, 130)!r}")
            for h, t in jl[:3]: print("     link:", sq(t, 50), "->", h[:90])
        except Exception as e:
            print(f"[{name}] ERROR {type(e).__name__}: {sq(str(e),100)}")
        finally:
            pg.close()
    b.close()
