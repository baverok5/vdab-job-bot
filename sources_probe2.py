#!/usr/bin/env python3
"""Read-only second look: what do real job links on the sites that answered look like?"""
import re, json
from playwright.sync_api import sync_playwright
import bot
UA = bot.HEADERS["User-Agent"]
def sq(s, n=70): return " ".join((s or "").split())[:n]
TARGETS = [
    ("talent seo",        "https://be.talent.com/jobs?k=seo&l=Belgium"),
    ("talent wordpress",  "https://be.talent.com/jobs?k=wordpress&l=Belgium"),
    ("talent ai strat",   "https://be.talent.com/jobs?k=ai+strategist&l=Belgium"),
    ("talent EN p2",      "https://be.talent.com/jobs?k=digital+marketing&l=Belgium&p=2"),
    ("eurobrussels mkt",  "https://www.eurobrussels.com/jobs/search?keywords=marketing"),
    ("eurobrussels comm", "https://www.eurobrussels.com/jobs/search?keywords=communications"),
    ("eurobrussels cat",  "https://www.eurobrussels.com/job_search"),
    ("careerjet a",       "https://www.careerjet.be/jobs?s=seo&l=Belgi%C3%AB"),
    ("careerjet b",       "https://www.careerjet.be/zoek/jobs?s=seo&l=Belgie"),
    ("randstad jobs",     "https://www.randstad.be/werknemers/jobs/q-seo/"),
    ("jobbird BE",        "https://www.jobbird.com/nl/vacature?s=seo&loc=Belgi%C3%AB"),
]
with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--no-sandbox"])
    for name, url in TARGETS:
        pg = b.new_page(user_agent=UA, locale="en-BE")
        try:
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=35000); pg.wait_for_timeout(5000)
            links = pg.eval_on_selector_all("a[href]", "els=>els.map(e=>[e.href,(e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,70)])")
            body = pg.inner_text("body")
            # group links by their path shape
            shape = {}
            for h, t in links:
                s = re.sub(r"[0-9a-f]{8,}|\d{4,}", "#", h.split("?")[0].split("#")[0])
                s = re.sub(r"/[^/]{25,}", "/<slug>", s)
                shape.setdefault(s, []).append((h, t))
            top = sorted(shape.items(), key=lambda kv: -len(kv[1]))[:4]
            print(f"[{name}] http={resp.status if resp else None} text={len(body)} links={len(links)} title={sq(pg.title(),50)!r}")
            for s, items in top:
                print(f"    x{len(items):3} {s[:90]}")
                print(f"        e.g. {items[0][1]!r} -> {items[0][0][:110]}")
        except Exception as e:
            print(f"[{name}] ERROR {type(e).__name__}: {sq(str(e),100)}")
        finally:
            pg.close()
    b.close()
