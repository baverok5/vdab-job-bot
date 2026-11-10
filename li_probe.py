#!/usr/bin/env python3
"""Read-only: run the bot's exact LinkedIn guest searches and print what comes back,
so we can see whether a given posting (here: BOI "Jr AI Strategist", Antwerp) is
reachable by any of the search terms. No commit, no AI call."""
import re, time
import requests
from bs4 import BeautifulSoup
import bot

WANT = re.compile(r"\bBOI\b|board of innovation|ai strategist", re.I)
TERMS = ["ai strategist", "jr ai strategist", "junior ai", "ai specialist", "ai consultant",
         "ai", "strategist", "junior consultant ai", "innovation", "ai marketing"]

def search(kw, start, tpr="r5184000", loc="Belgium"):
    r = requests.get(bot.LI_GUEST_SEARCH, params={"keywords": kw, "location": loc, "f_TPR": tpr,
                     "sortBy": "DD", "start": start}, headers=bot.LI_HEADERS, timeout=25)
    return r.status_code, r.text

for kw in TERMS:
    hit_at = None; total = 0
    for pg in range(8):
        code, html = search(kw, pg * 10)
        if code != 200 or not html.strip():
            print(f"[{kw}] page {pg}: HTTP {code}, empty={not html.strip()}"); break
        soup = BeautifulSoup(html, "html.parser"); rows = []
        for c in soup.select("li"):
            jid = bot._li_job_id(c); te = c.select_one(".base-search-card__title, h3")
            ce = c.select_one(".base-search-card__subtitle, h4")
            if jid and te: rows.append((jid, te.get_text(strip=True), ce.get_text(strip=True) if ce else ""))
        total += len(rows)
        for jid, t, co in rows:
            if WANT.search(t + " " + co) and hit_at is None:
                hit_at = (pg, jid, t, co)
        if pg < 2 or hit_at:
            print(f"[{kw}] p{pg}: {len(rows)} results; first titles: {[r[1][:30] for r in rows[:3]]}")
        if not rows: break
        time.sleep(3)
    print(f"==> [{kw}] total read {total}; BOI/AI-strategist hit: {hit_at}\n")
