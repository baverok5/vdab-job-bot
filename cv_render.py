#!/usr/bin/env python3
"""Build Baver's CV from structured data: master or tailored to one job.

The CV lives in cv_master.en.json / cv_master.nl.json (same ids in both). From
that one source this file produces

  * an HTML page in the new design (photo, teal headings, two A4 pages at most),
    printed to PDF with Chromium,
  * the plain-markdown copy the letter writer and the app read,
  * a *tailored* copy for one job: the bot's DeepSeek call picks and orders the
    CV's own bullets and rewrites the headline and summary; merge_tailored()
    below then checks that nothing was invented before it is used.

Run `python3 cv_render.py` to rebuild the master files after editing a JSON:
  cv.md, cv.nl.md, docs/cv.en.md, docs/cv.nl.md, docs/cv.pdf, docs/cv-nl.pdf
"""
import base64
import copy
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PHOTO = os.path.join(HERE, "cv_photo.jpg")
TEAL = "#0f6e6e"
MASTER = {"en": os.path.join(HERE, "cv_master.en.json"),
          "nl": os.path.join(HERE, "cv_master.nl.json")}


def load_master(lang):
    with open(MASTER["nl" if lang == "nl" else "en"], encoding="utf-8") as f:
        return json.load(f)


# ---- inline markup: **bold** and [text](url) ------------------------------
_TOKEN = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)]+)\)")


def inline_html(s):
    out, pos = [], 0
    for m in _TOKEN.finditer(s):
        out.append(html.escape(s[pos:m.start()]))
        if m.group(1) is not None:
            out.append("<b>" + inline_html(m.group(1)) + "</b>")
        else:
            out.append('<a href="%s">%s</a>' % (html.escape(m.group(3), quote=True),
                                                html.escape(m.group(2))))
        pos = m.end()
    out.append(html.escape(s[pos:]))
    return "".join(out)


def plain(s):
    return _TOKEN.sub(lambda m: m.group(1) or m.group(2), s)


# ---- markdown (what the letter writer and the app read) -------------------
def to_markdown(d):
    h = d["headings"]
    L = ["# " + d["name"], d["headline"]] + list(d["contact"][:1])
    L += d["contact"][1:]
    L += ["", "## " + h["summary"], d["summary"], "", "## " + h["experience"]]
    for e in d["experience"]:
        L += ["", "### " + e["title"]] + ["- " + b["t"] for b in e["bullets"]]
    L += ["", "## " + h["projects"]]
    for p in d["projects"]:
        L += ["", "### " + p["title"]] + ["- " + b["t"] for b in p["bullets"]]
    L += ["", "## " + h["skills"]]
    L += ["- **%s:** %s" % (s["label"], ", ".join(s["items"])) for s in d["skills"]]
    L += ["", "## " + h["education"]] + ["- " + x for x in d["education"]]
    L += ["", "## " + h["certifications"]] + ["- " + x for x in d["certifications"]]
    return "\n".join(L) + "\n"


# ---- HTML / PDF -----------------------------------------------------------
CSS = """
@page { size: A4; margin: 14mm 20mm 13mm 20mm; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Liberation Sans", Arial, Helvetica, sans-serif;
       font-size: 9.5pt; line-height: 1.33; color: #1b1b1b; }
a { color: %(teal)s; text-decoration: none; }
header { display: flex; align-items: center; gap: 7mm; padding-bottom: 3.6mm;
         border-bottom: 0.5mm solid %(teal)s; }
header img { width: 26mm; height: 26mm; border-radius: 50%%; border: 0.5mm solid %(teal)s;
             object-fit: cover; flex: none; }
h1 { margin: 0; font-size: 25pt; line-height: 1.05; font-weight: 700; }
.headline { margin: 1mm 0 1.4mm; color: %(teal)s; font-weight: 700; font-size: 11pt; }
.contact { color: #555; font-size: 9.5pt; line-height: 1.4; }
.contact a { color: #555; }
.contact .sep { color: #9bb; }
h2 { margin: 4.2mm 0 1.4mm; padding-bottom: 0.5mm; color: %(teal)s; font-size: 12pt;
     font-weight: 700; border-bottom: 0.25mm solid #c9dcdc; break-after: avoid; }
p { margin: 0; }
h3 { margin: 2.1mm 0 0.6mm; font-size: 10pt; font-weight: 700; break-after: avoid; }
ul { margin: 0; padding: 0; list-style: none; }
li { position: relative; padding-left: 4.2mm; margin: 0.35mm 0; break-inside: avoid; }
li::before { content: "\\2022"; position: absolute; left: 0.6mm; }
.edu { font-weight: 700; font-size: 10pt; margin: 1.4mm 0; }
.block { break-inside: avoid; }
""" % {"teal": TEAL}


def _photo_tag():
    try:
        with open(PHOTO, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        return '<img src="data:image/jpeg;base64,%s" alt="">' % b
    except OSError:
        return ""


def _contact_html(lines):
    return "<br>".join(inline_html(x).replace(" | ", ' <span class="sep">|</span> ')
                       for x in lines)


def to_html(d):
    h = d["headings"]
    parts = ['<!doctype html><html lang="%s"><head><meta charset="utf-8">'
             "<title>%s - CV</title><style>%s</style></head><body>"
             % (d.get("lang", "en"), html.escape(d["name"]), CSS)]
    parts.append("<header>%s<div><h1>%s</h1><div class=\"headline\">%s</div>"
                 '<div class="contact">%s</div></div></header>'
                 % (_photo_tag(), html.escape(d["name"]), inline_html(d["headline"]),
                    _contact_html(d["contact"])))
    parts.append("<h2>%s</h2><p>%s</p>" % (h["summary"], inline_html(d["summary"])))

    def blocks(items):
        for e in items:
            yield ('<div class="block"><h3>%s</h3><ul>%s</ul></div>'
                   % (inline_html(e["title"]),
                      "".join("<li>%s</li>" % inline_html(b["t"]) for b in e["bullets"])))

    parts.append("<h2>%s</h2>" % h["experience"] + "".join(blocks(d["experience"])))
    if d["projects"]:
        parts.append("<h2>%s</h2>" % h["projects"] + "".join(blocks(d["projects"])))
    parts.append("<h2>%s</h2><ul>%s</ul>" % (h["skills"], "".join(
        "<li><b>%s:</b> %s</li>" % (html.escape(s["label"]),
                                     html.escape(", ".join(s["items"])))
        for s in d["skills"])))
    parts.append("<h2>%s</h2>%s" % (h["education"], "".join(
        '<div class="edu">%s</div>' % inline_html(x) for x in d["education"])))
    parts.append("<h2>%s</h2><ul>%s</ul>" % (h["certifications"], "".join(
        "<li>%s</li>" % inline_html(x) for x in d["certifications"])))
    parts.append("</body></html>")
    return "".join(parts)


def pdf_pages(pdf_bytes):
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))


def render_pdf(browser, d, out_path=None):
    """Print the CV to PDF. Returns (bytes, page_count); writes out_path if given."""
    page = browser.new_page()
    try:
        page.set_content(to_html(d), wait_until="load")
        data = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
    finally:
        page.close()
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
    return data, pdf_pages(data)


# ---- tailoring: take the model's pick, keep only what is true -------------
_NUM = re.compile(r"\d[\d.,]*\d|\d")


def _numbers(s):
    return {n.strip(".,") for n in _NUM.findall(s)}


def _all_text(d):
    t = [d["headline"], d["summary"]]
    for sec in ("experience", "projects"):
        for e in d[sec]:
            t.append(e["title"])
            t += [b["t"] for b in e["bullets"]]
    for s in d["skills"]:
        t += s["items"]
    return " ".join(t)


def merge_tailored(master, t, lang="en"):
    """Build a tailored CV from `master` and the model's answer `t`.

    The model may reorder and drop the CV's own bullets, reword them lightly,
    and rewrite the headline and summary. Everything it returns is checked:
    unknown ids are ignored, and a headline, summary or rewritten bullet with a
    number or web address that is not in the real CV is thrown away (the master
    text is used instead). The three main jobs always stay. Returns None only if
    the answer is not a JSON object."""
    if not isinstance(t, dict):
        return None
    allowed_nums = _numbers(_all_text(master)) | _numbers(master["contact"][1]) \
        | _numbers(" ".join(master["contact"]))
    master_text = _all_text(master).lower()
    master_links = set(re.findall(r"\(([^)]+)\)", json.dumps(master, ensure_ascii=False)))

    def clean_text(s, maxlen=900):
        s = re.sub(r"\s+", " ", str(s or "")).strip()
        if not s or len(s) > maxlen:
            return None
        if _numbers(s) - allowed_nums:                 # an invented figure
            return None
        if re.search(r"https?://|www\.", s):
            if set(re.findall(r"\(([^)]+)\)", s)) - master_links or \
                    re.search(r"(?<!\()https?://", s):
                return None
        return s

    out = copy.deepcopy(master)

    headline = clean_text(t.get("headline"), 120)
    if headline:
        out["headline"] = headline
    summary = clean_text(t.get("summary"), 1100)
    if summary and 40 <= len(summary.split()) <= 150:
        out["summary"] = summary

    def pick(section, given, always=(), max_items=None, min_bullets=1):
        by_id = {e["id"]: e for e in master[section]}
        chosen = []
        for g in (given if isinstance(given, list) else []):
            e = by_id.get(g.get("id")) if isinstance(g, dict) else None
            if e is None or any(c["id"] == e["id"] for c in chosen):
                continue
            bm = {b["id"]: b for b in e["bullets"]}
            bullets = []
            for gb in (g.get("bullets") or []):
                bid = gb.get("id") if isinstance(gb, dict) else gb
                if bid not in bm or any(b["id"] == bid for b in bullets):
                    continue
                text = bm[bid]["t"]
                if isinstance(gb, dict) and gb.get("text"):
                    new = clean_text(gb["text"], 400)
                    # a rewrite must stay close to the real line: same length
                    # class and still mention the same tools/sites
                    if new and 0.6 <= len(new) / max(len(text), 1) <= 1.5:
                        text = new
                bullets.append({"id": bid, "t": text})
            if len(bullets) >= min_bullets:
                chosen.append({"id": e["id"], "title": e["title"], "bullets": bullets})
        for aid in always:                      # the jobs that must never vanish
            if not any(c["id"] == aid for c in chosen):
                chosen.append(copy.deepcopy(by_id[aid]))
        return chosen if max_items is None else chosen[:max(max_items, len(always))]

    order = [e["id"] for e in master["experience"]]
    exp = pick("experience", t.get("experience"), always=("episto", "freelance", "kxp"))
    exp.sort(key=lambda e: order.index(e["id"]))      # still newest first
    out["experience"] = exp
    out["projects"] = pick("projects", t.get("projects"), max_items=3) or \
        copy.deepcopy(master["projects"][:2])

    skills = []
    by_label = {s["label"].lower(): s for s in master["skills"]}
    for g in (t.get("skills") if isinstance(t.get("skills"), list) else []):
        base = by_label.get(str(g.get("label", "")).lower()) if isinstance(g, dict) else None
        if not base or any(s["label"] == base["label"] for s in skills):
            continue
        have = {i.lower(): i for i in base["items"]}
        items = []
        for i in (g.get("items") or []):
            real = have.get(str(i).lower())
            if real and real not in items:
                items.append(real)
        if items:
            skills.append({"label": base["label"], "items": items})
    for s in master["skills"]:                         # never lose a whole line
        if not any(x["label"] == s["label"] for x in skills):
            skills.append(copy.deepcopy(s))
    out["skills"] = skills
    return out


# ---- command line: rebuild the master CV files ----------------------------
def _launch(pw):
    try:
        return pw.chromium.launch(args=["--no-sandbox"])
    except Exception:
        return pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                  args=["--no-sandbox"])


def build_masters():
    from playwright.sync_api import sync_playwright
    jobs = (("en", "cv.md", "docs/cv.en.md", "docs/cv.pdf"),
            ("nl", "cv.nl.md", "docs/cv.nl.md", "docs/cv-nl.pdf"))
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            for lang, md, pub_md, pdf in jobs:
                d = load_master(lang)
                text = to_markdown(d)
                for p in (md, pub_md):
                    with open(os.path.join(HERE, p), "w", encoding="utf-8") as f:
                        f.write(text)
                _, pages = render_pdf(browser, d, os.path.join(HERE, pdf))
                print(f"{lang}: {pdf} ({pages} page(s)), {md}, {pub_md}")
        finally:
            browser.close()


if __name__ == "__main__":
    build_masters()
