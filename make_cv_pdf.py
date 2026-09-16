#!/usr/bin/env python3
"""Render cv.md into an A4 PDF that matches Baver's own layout.

Dependency-free: Helvetica + Helvetica-Bold, hand-rolled PDF objects.

Measured off the CV he supplied, so the output is the same document rather than
an approximation of it: 9.5pt body, 12.5pt title-case section headings, 10.5pt
bold job titles, bold run-in labels ("SEO & GEO:"), links in his blue with a
real clickable annotation, and the whole thing on one page.
"""
import re, sys, zlib

SRC = sys.argv[1] if len(sys.argv) > 1 else "cv.md"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/cv.pdf"

# ---- Helvetica / Helvetica-Bold AFM widths (1000-unit em) -----------------
# Both tables are needed: bold is wider, and guessing it mis-wraps every line
# with a run-in label and puts the link rectangles in the wrong place.
WR = {' ':278,'!':278,'"':355,'#':556,'$':556,'%':889,'&':667,"'":191,'(':333,')':333,
'*':389,'+':584,',':278,'-':333,'.':278,'/':278,'0':556,'1':556,'2':556,'3':556,'4':556,
'5':556,'6':556,'7':556,'8':556,'9':556,':':278,';':278,'<':584,'=':584,'>':584,'?':556,
'@':1015,'A':667,'B':667,'C':722,'D':722,'E':667,'F':611,'G':778,'H':722,'I':278,'J':500,
'K':667,'L':556,'M':833,'N':722,'O':778,'P':667,'Q':778,'R':722,'S':667,'T':611,'U':722,
'V':667,'W':944,'X':667,'Y':667,'Z':611,'[':278,'\\':278,']':278,'^':469,'_':556,'`':333,
'a':556,'b':556,'c':500,'d':556,'e':556,'f':278,'g':556,'h':556,'i':222,'j':222,'k':500,
'l':222,'m':833,'n':556,'o':556,'p':556,'q':556,'r':333,'s':500,'t':278,'u':556,'v':500,
'w':722,'x':500,'y':500,'z':500,'{':334,'|':260,'}':334,'~':584,'·':278,'–':556,'•':350}

WB = {' ':278,'!':333,'"':474,'#':556,'$':556,'%':889,'&':722,"'":238,'(':333,')':333,
'*':389,'+':584,',':278,'-':333,'.':278,'/':278,'0':556,'1':556,'2':556,'3':556,'4':556,
'5':556,'6':556,'7':556,'8':556,'9':556,':':333,';':333,'<':584,'=':584,'>':584,'?':611,
'@':975,'A':722,'B':722,'C':722,'D':722,'E':667,'F':611,'G':778,'H':722,'I':278,'J':556,
'K':722,'L':611,'M':833,'N':722,'O':778,'P':667,'Q':778,'R':722,'S':667,'T':611,'U':722,
'V':667,'W':944,'X':667,'Y':667,'Z':611,'[':333,'\\':278,']':333,'^':584,'_':556,'`':333,
'a':556,'b':611,'c':556,'d':611,'e':556,'f':333,'g':611,'h':611,'i':278,'j':278,'k':556,
'l':278,'m':889,'n':611,'o':611,'p':611,'q':611,'r':389,'s':556,'t':333,'u':611,'v':556,
'w':778,'x':556,'y':556,'z':500,'{':389,'|':280,'}':389,'~':584,'·':278,'–':556,'•':350}


# The remapped Turkish codes measure as their closest Latin letter.
for _t in (WR, WB):
    for _c, _like in ((0x8E, "I"), (0x8F, "S"), (0x90, "i"),
                      (0x9D, "g"), (0x9E, "G"), (0x9F, "s"), (0x95, "•")):
        _t[chr(_c)] = _t[_like]


def tw(s, fs, bold=False):
    t = WB if bold else WR
    return sum(t.get(c, 556) for c in s) * fs / 1000.0


# Characters whose byte differs from Latin-1, mapped to the code WinAnsi (or the
# /Differences array below) gives them. The bullet is a WinAnsi character
# already; the Turkish letters take slots WinAnsi leaves undefined.
TURKISH = {"Ş": 0x8F, "ş": 0x9F, "ı": 0x90, "İ": 0x8E,
           "Ğ": 0x9E, "ğ": 0x9D, "•": 0x95}
TURKISH_GLYPH = {0x8E: "Idotaccent", 0x8F: "Scedilla", 0x90: "dotlessi",
                 0x9D: "gbreve", 0x9E: "Gbreve", 0x9F: "scedilla"}


def clean(s):
    s = (s.replace("\r", "")
          .replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("…", "...")
          # His CV uses plain hyphens throughout; em/en dashes render as a
          # different document.
          .replace("—", "-").replace("–", "-"))
    # Turkish letters have no Windows-1252 code point, and transliterating them
    # renamed a real employer ("SOK Market") and his home city ("Diyarbakir").
    # They are mapped instead onto byte slots WinAnsi leaves undefined, and the
    # font's /Differences array names the glyph for each one.
    for ch, code in TURKISH.items():
        s = s.replace(ch, chr(code))
    return s


def esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# ---- Inline markdown -> styled runs ---------------------------------------
TOKEN = re.compile(r"\*\*|\[([^\]]+)\]\(((?:https?://|mailto:|tel:)[^)\s]+)\)")
LINK_BLUE = (0.10, 0.31, 0.55)   # sampled from his CV


def runs_of(s):
    """Split one line into [(text, bold, url)] runs, honouring ** and [](...)."""
    out, bold, pos = [], False, 0
    for m in TOKEN.finditer(s):
        if m.start() > pos:
            out.append((s[pos:m.start()], bold, None))
        if m.group(0) == "**":
            bold = not bold
        else:
            label, url = m.group(1), m.group(2)
            lb = bold
            if label.startswith("**") and label.endswith("**"):
                label, lb = label[2:-2], True
            out.append((label, lb, url))
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], bold, None))
    return [r for r in out if r[0]]


def wrap_runs(runs, fs, maxw):
    """Lay runs out into visual lines of (text, bold, url, x_offset).

    Splitting happens at word boundaries across run edges, so a bold label that
    runs into ordinary text wraps as one sentence and every piece keeps the
    x it is actually drawn at — which is what the link rectangles are built
    from."""
    lines, cur, x = [], [], 0.0
    for (txt, bold, url) in runs:
        for word in re.findall(r"\s*\S+\s*|\s+", txt):
            bare = word.strip()
            lead = word[:len(word) - len(word.lstrip())]
            if x + tw(lead + bare, fs, bold) > maxw and cur:
                lines.append(cur)
                cur, x, word = [], 0.0, bare + word[len(lead) + len(bare):]
            if not word:
                continue
            cur.append((word, bold, url, x))
            x += tw(word, fs, bold)
    if cur:
        lines.append(cur)
    # Merge neighbouring words that share styling back into one string. Drawing
    # word by word looks identical but extracts as "BaverOk" — and a CV is read
    # by applicant-tracking systems through exactly that text layer.
    out = []
    for line in lines:
        merged = []
        for (word, bold, url, x) in line:
            if merged and merged[-1][1] == bold and merged[-1][2] == url:
                merged[-1][0] += word
            else:
                merged.append([word, bold, url, x])
        out.append([tuple(m) for m in merged])
    return out or [[]]


# ---- Page geometry, measured off the original -----------------------------
PAGE_W, PAGE_H = 595.28, 841.89
L, R, TOP, BOT = 57.0, 57.0, 782.0, 44.0
USABLE = PAGE_W - L - R
BULLET_INDENT = 11.0           # hanging indent: glyph at L, text at L+11

GREY = (0.27, 0.27, 0.27)
md = clean(open(SRC, encoding="utf-8").read())


def build(scale=1.0):
    """Lay the whole document out at `scale`.

    Vertical advances are copied from his CV, baseline to baseline: body lines
    12.8 apart with no extra gap between bullets, a section heading 25.5 below
    the line above it, a job title 19.0. Guessing these put the CV onto a second
    page, which is a different document from the one-pager he wrote.

    `scale` exists for the Dutch translation, which runs about 10% longer than
    the English and spilled four lines onto a second page. Shrinking everything
    a couple of percent and re-flowing holds one page and is invisible unless
    the two are held side by side; squeezing only the gaps left the headings
    sitting on top of the text.
    """
    items = []

    def add(size, text, gap=0.0, indent=0.0, leading=None, color=None, bullet=False):
        size *= scale
        runs = runs_of(text)
        for i, ln in enumerate(wrap_runs(runs, size, USABLE - indent)):
            items.append({"indent": indent, "size": size, "line": ln,
                          "gap": gap * scale,
                          "leading": (leading or size * 1.35) * scale,
                          "color": color, "bullet": bullet and i == 0})

    plain = 0
    for raw in md.split("\n"):
        ln = raw.rstrip()
        if ln.startswith("# ") and not ln.startswith("## "):
            add(20, "**" + ln[2:].strip() + "**", 0, leading=20.0)
        elif ln.startswith("### "):
            add(10.5, "**" + ln[4:].strip() + "**", gap=6.0, leading=13.0)
        elif ln.startswith("## "):
            # Title case, exactly as he wrote it — not upper-cased.
            add(12.5, "**" + ln[3:].strip() + "**", gap=11.5, leading=14.0)
        elif ln.startswith("- "):
            add(9.5, ln[2:].strip(), gap=0.0, indent=BULLET_INDENT,
                leading=12.8, bullet=True)
        elif ln.strip():
            if plain == 0:      # the tagline under the name
                add(10.5, ln.strip(), gap=0.0, leading=12.5, color=GREY)
            elif plain == 1:    # contact line
                add(9.5, ln.strip(), gap=3.0, leading=12.8)
            else:
                add(9.5, ln.strip(), gap=0.0, leading=12.8)
            plain += 1
    return items


items = build()
for _s in (0.985, 0.97, 0.955, 0.94):
    if sum(i["gap"] + i["leading"] for i in items) <= TOP - BOT:
        break
    items = build(_s)
    if sum(i["gap"] + i["leading"] for i in items) <= TOP - BOT:
        print(f"  scaled to {_s:.0%} to hold one page")
        break

# ---- Paginate -------------------------------------------------------------
pages, cur, y = [], [], TOP
for it in items:
    y -= it["gap"]
    y -= it["leading"]
    if y < BOT:
        pages.append(cur); cur = []; y = TOP - it["leading"]
    cur.append((y, it))
if cur:
    pages.append(cur)


def stream_for(pg):
    out, col = [], None

    def setcol(c):
        nonlocal col
        c = c or (0.0, 0.0, 0.0)
        if c != col:
            out.append("%.2f %.2f %.2f rg" % c)
            col = c

    for (y, it) in pg:
        if it["bullet"]:
            setcol(it["color"])
            out.append(f"BT /H 8.0 Tf 1 0 0 1 {L:.1f} {y:.1f} Tm ({esc(chr(0x95))}) Tj ET")
        x0 = L + it["indent"]
        for (text, bold, url, dx) in it["line"]:
            setcol(LINK_BLUE if url else it["color"])
            f = "HB" if bold else "H"
            out.append(f"BT /{f} {it['size']:.1f} Tf 1 0 0 1 {x0 + dx:.1f} {y:.1f} Tm "
                       f"({esc(text)}) Tj ET")
    # latin-1 maps chr(n) straight to byte n; cp1252 refuses the slots above.
    return "\n".join(out).encode("latin-1", "replace")


def link_rects(pg):
    """One clickable box per linked run. Merged across adjacent runs of the same
    URL so a link split by a bold marker is still a single target."""
    rects = []
    for (y, it) in pg:
        x0 = L + it["indent"]
        for (text, bold, url, dx) in it["line"]:
            if not url:
                continue
            a, b = x0 + dx, x0 + dx + tw(text.rstrip(), it["size"], bold)
            s = it["size"]
            if rects and rects[-1][4] == url and abs(rects[-1][3] - (y + s * 0.92)) < 0.1 \
                    and abs(rects[-1][2] - a) < 1.5:
                rects[-1] = (rects[-1][0], rects[-1][1], b, rects[-1][3], url)
            else:
                rects.append((a, y - s * 0.24, b, y + s * 0.92, url))
    return rects


# ---- PDF object assembly --------------------------------------------------
objs = []


def add_obj(b):
    objs.append(b); return len(objs)


diffs = b" ".join(b"%d /%s" % (c, TURKISH_GLYPH[c].encode())
                  for c in sorted(TURKISH_GLYPH))
enc = add_obj(b"<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [%s] >>" % diffs)
font_h = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding %d 0 R >>" % enc)
font_hb = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding %d 0 R >>" % enc)
res_obj = add_obj(f"<< /Font << /H {font_h} 0 R /HB {font_hb} 0 R >> >>".encode())

content_objs = []
for pg in pages:
    comp = zlib.compress(stream_for(pg))
    content_objs.append(add_obj(
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream"))

annot_objs = []
for pg in pages:
    nums = []
    for (x0, y0, x1, y1, url) in link_rects(pg):
        uri = url.encode("ascii", "ignore").replace(b"\\", b"").replace(b")", b"")
        nums.append(add_obj(
            b"<< /Type /Annot /Subtype /Link /Border [0 0 0] "
            b"/Rect [%.2f %.2f %.2f %.2f] /A << /S /URI /URI (%s) >> >>"
            % (x0, y0, x1, y1, uri)))
    annot_objs.append(nums)

pages_obj_num = len(objs) + len(pages) + 1  # reserve
page_nums = []
for idx, pg in enumerate(pages):
    annots = b""
    if annot_objs[idx]:
        annots = b" /Annots [%s]" % b" ".join(b"%d 0 R" % n for n in annot_objs[idx])
    page_nums.append(add_obj(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %d 0 R "
        b"/Contents %d 0 R%s >>"
        % (pages_obj_num, PAGE_W, PAGE_H, res_obj, content_objs[idx], annots)))

kids = b" ".join(b"%d 0 R" % n for n in page_nums)
pages_obj = add_obj(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_nums), kids))
assert pages_obj == pages_obj_num, (pages_obj, pages_obj_num)
catalog = add_obj(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)

buf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
offsets = [0]
for n, body in enumerate(objs, 1):
    offsets.append(len(buf))
    buf += b"%d 0 obj\n" % n + body + b"\nendobj\n"
xref_pos = len(buf)
buf += b"xref\n0 %d\n" % (len(objs) + 1)
buf += b"0000000000 65535 f \n"
for off in offsets[1:]:
    buf += b"%010d 00000 n \n" % off
buf += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objs) + 1, catalog, xref_pos))

open(OUT, "wb").write(buf)
print(f"wrote {OUT}:", len(buf), "bytes,", len(pages), "page(s)")

# The app builds nothing itself any more, but it does show this text, so publish
# the exact source the PDF was made from.
if SRC in ("cv.md", "cv.nl.md"):
    pub = "docs/cv.en.md" if SRC == "cv.md" else "docs/cv.nl.md"
    open(pub, "w", encoding="utf-8").write(open(SRC, encoding="utf-8").read())
    print("published", pub)
