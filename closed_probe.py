#!/usr/bin/env python3
"""Look at what an OPEN and a CLOSED posting actually look like on each source.

The bot can tell a closed LinkedIn posting (that took weeks of trial and error)
but nothing checks VDAB, EURES or StepStone, which together are 82% of Ready.
This does not decide anything: it opens a sample of real jobs — the oldest ones
(most likely closed), the newest (most likely open), and a made-up id on each
site as a control — and prints what the site says, so the rules for "closed" can
be written from evidence instead of guessed. Read-only: no commit, no AI call.

Run it from the "Closed-check probe" workflow (the sandbox this is written in
cannot reach any of these sites).
"""
import json
import re
import time
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

import bot

PHRASES = re.compile(
    r"niet (meer|langer) (beschikbaar|online|actief|zichtbaar)|verlopen|bestaat niet|"
    r"(werd|is|zijn) (reeds )?ingevuld|vacature (is )?(gesloten|afgelopen|offline)|"
    r"no longer (available|accepting|online|active|open|exists)|expired|"
    r"(has|have) been (filled|closed|removed)|position (is )?filled|page not found|"
    r"n'est plus|plus disponible|introuvable|pagina niet gevonden|niet gevonden|"
    r"offline|404", re.I)
STATUSISH = re.compile(r"status|expir|clos|deadline|end|valid|active|actief|publi|"
                       r"sluit|einde|verval|online", re.I)


def squash(s, n=240):
    return " ".join((s or "").split())[:n]


def pick(rows, n_old, n_new):
    rows = sorted(rows, key=lambda x: x.get("found_at") or "")
    out = [("oldest", r) for r in rows[:n_old]]
    out += [("newest", r) for r in rows[-n_new:] if r not in rows[:n_old]]
    return out


def show_phrases(text):
    hits = []
    for m in PHRASES.finditer(text or ""):
        a = max(0, m.start() - 40)
        hits.append(squash(text[a:m.end() + 40], 110))
        if len(hits) >= 3:
            break
    return hits


def probe_page(browser, label, url):
    """Open a page the way the bot does and report what it shows."""
    page = browser.new_page(
        user_agent=bot.HEADERS["User-Agent"], locale="nl-BE",
        extra_http_headers={"Accept-Language": bot.HEADERS["Accept-Language"]})
    api = []

    def on_response(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch"):
                body = ""
                if "vacatur" in resp.url.lower() or "job" in resp.url.lower():
                    try:
                        body = resp.text()[:6000]
                    except Exception:
                        body = ""
                api.append((resp.status, resp.url, body))
        except Exception:
            pass

    page.on("response", on_response)
    t0 = time.time()
    status = final = title = None
    n3 = n_end = -1
    body = ""
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = r.status if r else None
        page.wait_for_timeout(3000)
        n3 = len(page.inner_text("body"))
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.length > 800", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        body = page.inner_text("body")
        n_end = len(body)
        title = page.title()
        final = page.url
    except Exception as e:
        print(f"  [{label}] ERROR {type(e).__name__}: {squash(str(e), 120)}")
    finally:
        page.close()
    dt = time.time() - t0
    print(f"  [{label}] http={status} {dt:4.1f}s  text@3s={n3} text@end={n_end}  title={squash(title, 70)!r}")
    if final and final.split("?")[0] != url.split("?")[0]:
        print(f"      redirected to: {final[:140]}")
    print(f"      body starts : {squash(body, 230)!r}")
    for h in show_phrases(body):
        print(f"      PHRASE      : ...{h}...")
    shown = 0
    for st, u, b in api:
        if ("vacatur" in u.lower() or st >= 400) and shown < 5:
            shown += 1
            extra = ""
            if b and b.lstrip().startswith("{"):
                try:
                    d = json.loads(b)
                    keep = {k: str(v)[:50] for k, v in d.items() if STATUSISH.search(k)}
                    extra = f" fields={keep}" if keep else f" keys={list(d)[:10]}"
                except Exception:
                    extra = f" body={squash(b, 90)!r}"
            print(f"      api {st}: {u[:110]}{extra}")
    return {"status": status, "n_end": n_end, "dt": dt}


def probe_eures(label, raw_id):
    t0 = time.time()
    try:
        r = requests.get(bot.EURES_DETAIL_API + quote(raw_id, safe=""),
                         params={"requestLang": "en"}, timeout=25,
                         headers={"Accept": "application/json",
                                  "User-Agent": bot.HEADERS["User-Agent"]})
    except Exception as e:
        print(f"  [{label}] ERROR {type(e).__name__}")
        return
    dt = time.time() - t0
    print(f"  [{label}] api http={r.status_code} {dt:4.1f}s  bytes={len(r.content)}")
    if r.status_code != 200:
        print(f"      body: {squash(r.text, 160)!r}")
        return
    try:
        d = r.json()
    except Exception:
        print(f"      not JSON: {squash(r.text, 160)!r}")
        return
    if isinstance(d, dict):
        inner = d.get("jv") or d.get("data") or d
        if isinstance(inner, dict):
            print(f"      keys: {list(inner)[:30]}")
            keep = {k: str(v)[:60] for k, v in inner.items() if STATUSISH.search(k)}
            print(f"      status-like fields: {keep}")
            desc = bot._first(inner, "description", "jvDescription", "content") or ""
            print(f"      description chars: {len(desc)}")
        else:
            print(f"      top-level: {squash(json.dumps(d), 200)!r}")


def main():
    jobs = json.load(open("docs/jobs.json", encoding="utf-8"))
    ready = jobs.get("jobs", [])
    listing = []
    try:
        listing = json.load(open("docs/listing.json", encoding="utf-8")).get("listing", [])
    except Exception as e:
        print("listing.json not readable:", e)

    def src(u):
        u = u or ""
        return ("LinkedIn" if "linkedin.com" in u else "VDAB" if "vdab.be" in u else
                "EURES" if "europa.eu" in u else "StepStone" if "stepstone" in u else "?")

    by = {}
    for j in ready:
        by.setdefault(src(j.get("url")), []).append(j)
    print("Ready by source:", {k: len(v) for k, v in by.items()})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            # ---- VDAB: oldest/newest in Ready, the very oldest ids ever seen, a control
            print("\n=========== VDAB")
            rows = pick(by.get("VDAB", []), 6, 4)
            ancient = sorted((x for x in listing if "vdab.be" in (x.get("url") or "")),
                             key=lambda x: int(x["id"]) if str(x["id"]).isdigit() else 10**12)[:4]
            rows += [("ancient-id (not in Ready)", a) for a in ancient]
            for tag, j in rows:
                print(f"\n- {tag}  found={str(j.get('found_at'))[:10]}  {j.get('title', '')[:60]!r}\n  {j['url']}")
                probe_page(browser, "vdab", j["url"])
            print("\n- CONTROL: an id that cannot exist")
            probe_page(browser, "vdab-control", "https://www.vdab.be/vindeenjob/vacatures/1/geen-bestaande-vacature")

            # ---- StepStone
            print("\n=========== STEPSTONE")
            for tag, j in pick(by.get("StepStone", []), 4, 2):
                print(f"\n- {tag}  found={str(j.get('found_at'))[:10]}  {j.get('title', '')[:60]!r}\n  {j['url']}")
                probe_page(browser, "stepstone", j["url"])
            print("\n- CONTROL: an id that cannot exist")
            probe_page(browser, "stepstone-control", "https://www.stepstone.be/jobs--Geen-Bestaande-Vacature--1-inline.html")

            # ---- LinkedIn: the existing detector, as a baseline for timing and behaviour
            print("\n=========== LINKEDIN (existing detector)")
            for tag, j in pick(by.get("LinkedIn", []), 3, 3):
                t0 = time.time()
                state = bot.linkedin_is_closed(j["id"], browser=browser)
                print(f"- {tag} found={str(j.get('found_at'))[:10]} id={j['id']} -> closed={state}  ({time.time() - t0:.1f}s)")
        finally:
            browser.close()

    # ---- EURES: JSON API, no browser
    print("\n=========== EURES")
    for tag, j in pick(by.get("EURES", []), 6, 3):
        raw = j["url"].rsplit("/", 1)[-1]
        print(f"\n- {tag}  found={str(j.get('found_at'))[:10]}  {j.get('title', '')[:60]!r}")
        probe_eures("eures", raw)
    print("\n- CONTROL: an id that cannot exist")
    probe_eures("eures-control", "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")


def tree(x, path="", out=None, depth=0):
    """Flatten a JSON value into 'path: short value' lines."""
    out = [] if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            tree(v, f"{path}.{k}" if path else k, out, depth + 1)
    elif isinstance(x, list):
        out.append(f"{path}: list[{len(x)}]")
        for i, v in enumerate(x[:3]):
            tree(v, f"{path}[{i}]", out, depth + 1)
    else:
        out.append(f"{path}: {str(x)[:70]!r}")
    return out


def eures_shape():
    """Show where EURES keeps the language level, for the jobs that state one."""
    jobs = json.load(open("docs/jobs.json", encoding="utf-8")).get("jobs", [])
    shown = 0
    for j in jobs:
        if "europa.eu" not in (j.get("url") or ""):
            continue
        raw = j["url"].rsplit("/", 1)[-1]
        try:
            r = requests.get(bot.EURES_DETAIL_API + quote(raw, safe=""), params={"requestLang": "en"},
                             timeout=25, headers={"Accept": "application/json", "User-Agent": bot.HEADERS["User-Agent"]})
            d = r.json() if r.status_code == 200 else None
        except Exception:
            d = None
        if not d:
            continue
        lines = tree(d)
        langy = [l for l in lines if re.search(r"lang|cefr|level", l, re.I)]
        print(f"\n=== {j.get('title','')[:60]!r}")
        for l in langy[:25]:
            print("   ", l)
        shown += 1
        if shown >= 4:
            break
    if shown:
        print("\n--- full key tree of the last job ---")
        for l in lines[:120]:
            print("   ", l)


def verify():
    pass
    """Run the bot's real checkers on a sample and show the verdicts, so the rules
    can be judged against what the sites actually say for open and closed jobs."""
    from collections import Counter
    jobs = json.load(open("docs/jobs.json", encoding="utf-8")).get("jobs", [])
    by = {}
    for j in jobs:
        by.setdefault(bot._source_of(j), []).append(j)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for src in ("VDAB",):
                rows = sorted(by.get(src, []), key=lambda x: x.get("found_at") or "")
                sample = rows[:30] + rows[-10:] if src == "VDAB" else rows
                if src == "EURES":
                    sample = rows[:30] + rows[-10:]
                print(f"\n=========== VERIFY {src}: {len(sample)} of {len(rows)}")
                tally, fields = Counter(), Counter()
                for j in sample:
                    st, why = bot.check_still_open(browser, j)
                    tally[st] += 1
                    if src == "VDAB":
                        try:
                            http, d = bot._vdab_api_response(browser, j["url"])
                            fields[(http, d.get("status") if d else None, d.get("gepubliceerd") if d else None)] += 1
                        except Exception as e:
                            fields[("err", type(e).__name__)] += 1
                    if st is False:
                        print(f"  CLOSED found={str(j.get('found_at'))[:10]} {j.get('title','')[:50]!r} -> {why}")
                    elif st is None:
                        print(f"  UNKNOWN {j.get('url')}")
                print(f"  verdicts (True=open, False=closed, None=unknown): {dict(tally)}")
                if fields:
                    print(f"  VDAB (status, gepubliceerd): {dict(fields)}")
        finally:
            browser.close()


if __name__ == "__main__":
    import sys
    verify() if "--verify" in sys.argv else main()
