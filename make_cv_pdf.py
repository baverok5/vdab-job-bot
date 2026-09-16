#!/usr/bin/env python3
"""Render cv.md into a clean, dependency-free A4 PDF (Helvetica + Helvetica-Bold)."""
import re, sys, zlib

# The English CV is the default; pass a source/output pair to build the Dutch
# one (cv.nl.md -> docs/cv-nl.pdf). Both are the same document — one CV, two
# languages — so they must come from the same renderer.
SRC = sys.argv[1] if len(sys.argv) > 1 else "cv.md"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/cv.pdf"

# ---- Helvetica AFM widths (1000-unit em) for proper proportional wrapping ----
# Compact width table for the WinAnsi range we use; default 556 for the rest.
W = {' ':278,'!':278,'"':355,'#':556,'$':556,'%':889,'&':667,"'":191,'(':333,')':333,
'*':389,'+':584,',':278,'-':333,'.':278,'/':278,'0':556,'1':556,'2':556,'3':556,'4':556,
'5':556,'6':556,'7':556,'8':556,'9':556,':':278,';':278,'<':584,'=':584,'>':584,'?':556,
'@':1015,'A':667,'B':667,'C':722,'D':722,'E':667,'F':611,'G':778,'H':722,'I':278,'J':500,
'K':667,'L':556,'M':833,'N':722,'O':778,'P':667,'Q':778,'R':722,'S':667,'T':611,'U':722,
'V':667,'W':944,'X':667,'Y':667,'Z':611,'[':278,'\\':278,']':278,'^':469,'_':556,'`':333,
'a':556,'b':556,'c':500,'d':556,'e':556,'f':278,'g':556,'h':556,'i':222,'j':222,'k':500,
'l':222,'m':833,'n':556,'o':556,'p':556,'q':556,'r':333,'s':500,'t':278,'u':556,'v':500,
'w':722,'x':500,'y':500,'z':500,'{':334,'|':260,'}':334,'~':584,'·':278,'–':556,'•':350}

def tw(s, fs):
    return sum(W.get(c, 556) for c in s) * fs / 1000.0

def wrap_idx(text, fs, maxw):
    """Wrap, returning each visual line as a (start, end) range into `text`.

    Ranges rather than strings because links have to stay attached to the piece
    they landed on: searching for the wrapped text afterwards anchors a repeated
    word to the wrong place. Every candidate line is measured as a slice of the
    source, never rebuilt by joining words — a bullet's "\u2022  " prefix contains
    two spaces, and re-joining silently lost one character per line, which cut
    "proteinbox.com.tr" in half and dropped its link.
    """
    out, base = [], 0
    for para in text.split("\n"):
        if not para.strip():
            out.append((base, base))
            base += len(para) + 1
            continue
        starts, off, words = [], 0, para.split(" ")
        for w in words:
            starts.append(off)
            off += len(w) + 1
        line_start, line_end = 0, None
        for k, w in enumerate(words):
            if not w and line_end is None:
                continue                      # leading spaces
            cand_end = starts[k] + len(w)
            if line_end is not None and tw(para[line_start:cand_end], fs) > maxw:
                out.append((base + line_start, base + line_end))
                line_start, line_end = starts[k], cand_end
            else:
                if line_end is None:
                    line_start = starts[k]
                line_end = cand_end
        if line_end is not None:
            out.append((base + line_start, base + line_end))
        base += len(para) + 1
    return out


def wrap(text, fs, maxw):
    return [text[a:b] for (a, b) in wrap_idx(text, fs, maxw)]

LINK_RX = re.compile(r"\[([^\]]+)\]\(((?:https?://|mailto:|tel:)[^)\s]+)\)")


def delink(text):
    """Turn "[label](url)" into the label plus the character spans to link.

    The uploaded CV carried nine live links — the client sites, kxp.biz,
    mirook.com, LinkedIn and the Semrush certificate — and rendering it as plain
    text quietly dropped every one of them. Returns (display_text, [(i, j, url)])
    with i:j indexing into display_text."""
    out, spans, pos = [], [], 0
    for m in LINK_RX.finditer(text):
        out.append(text[pos:m.start()])
        start = sum(len(x) for x in out)
        out.append(m.group(1))
        spans.append((start, start + len(m.group(1)), m.group(2)))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), spans


def clean(s):
    s = (s.replace("\r", "")
          .replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("…", "..."))
    # Turkish letters not in Windows-1252 -> nearest ASCII (ü, ö, ç, ë are fine).
    for a, b in [("ı", "i"), ("İ", "I"), ("ş", "s"), ("Ş", "S"),
                 ("ğ", "g"), ("Ğ", "G")]:
        s = s.replace(a, b)
    return s

def esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

# ---- Layout: build a list of (font, size, text, gap_before) lines --------
PAGE_W, PAGE_H = 595.28, 841.89
L, R, TOP, BOT = 56, 56, 800, 56
USABLE = PAGE_W - L - R

md = clean(open(SRC, encoding="utf-8").read())
lines = md.split("\n")
items = []  # (font, size, text, gap)
def add(font, size, text, gap=0, maxw=USABLE, indent=0):
    """Lay out one logical line, keeping any links attached to the right piece.

    wrap() may split the line, so each span is re-anchored to whichever visual
    line it landed on, by walking the consumed character count."""
    text, spans = delink(text)
    for i, (a0, a1) in enumerate(wrap_idx(text, size, maxw)):
        here = [(a - a0, b - a0, url) for (a, b, url) in spans
                if a >= a0 and b <= a1]
        items.append((font, size, text[a0:a1], gap if i == 0 else 0, indent, here))

i = 0
while i < len(lines):
    ln = lines[i].rstrip()
    if ln.startswith("# ") and not ln.startswith("## "):
        add("HB", 22, ln[2:].strip(), 0)
    elif ln.startswith("### "):
        add("HB", 11.5, ln[4:].strip(), 10)
    elif ln.startswith("## "):
        add("HB", 13.5, ln[3:].strip().upper(), 14)
    elif ln.startswith("- "):
        body = re.sub(r"\*\*(.*?)\*\*", r"\1", ln[2:].strip())
        add("H", 10, "\u2022  " + body, 2, USABLE - 12, 12)
    elif ln.strip():
        body = re.sub(r"\*\*(.*?)\*\*", r"\1", ln.strip())
        # the two lines right under the name are the tagline + contact
        size = 10.5 if len(items) < 3 else 10
        add("H", size, body, 3)
    i += 1

# ---- Paginate + emit content streams -------------------------------------
pages, cur, y = [], [], TOP
for (font, size, text, gap, indent, links) in items:
    y -= gap
    lead = size * 1.32
    if y - lead < BOT:
        pages.append(cur); cur = []; y = TOP
    y -= lead
    cur.append((L + indent, y, font, size, text, links))
if cur:
    pages.append(cur)

def stream_for(pg):
    out = []
    for (x, y, font, size, text, links) in pg:
        out.append(f"BT /{font} {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({esc(text)}) Tj ET")
    return "\n".join(out).encode("cp1252", "replace")


def link_rects(pg):
    """Clickable boxes for one page. The original CV styles links exactly like
    the surrounding text — black, no underline — so only the annotation is
    added and the page looks unchanged."""
    rects = []
    for (x, y, font, size, text, links) in pg:
        for (a, b, url) in links:
            x0 = x + tw(text[:a], size)
            x1 = x + tw(text[:b], size)
            rects.append((x0, y - size * 0.22, x1, y + size * 0.92, url))
    return rects

# ---- PDF object assembly --------------------------------------------------
objs = []
def add_obj(b): objs.append(b); return len(objs)

font_h = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
font_hb = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
res = f"<< /Font << /H {font_h} 0 R /HB {font_hb} 0 R >> >>".encode()
res_obj = add_obj(res)

kids, page_objs = [], []
content_objs = []
for pg in pages:
    raw = stream_for(pg)
    comp = zlib.compress(raw)
    c = add_obj(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream")
    content_objs.append(c)

# Link annotations, created before the page objects so the /Pages reservation
# below still lands on the right object number.
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
        annots = (b" /Annots [%s]"
                  % b" ".join(b"%d 0 R" % n for n in annot_objs[idx]))
    pn = add_obj(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %d 0 R /Contents %d 0 R%s >>"
        % (pages_obj_num, PAGE_W, PAGE_H, res_obj, content_objs[idx], annots))
    page_nums.append(pn)

kids_str = " ".join(f"{n} 0 R" for n in page_nums).encode()
pages_obj = add_obj(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_nums), kids_str))
assert pages_obj == pages_obj_num, (pages_obj, pages_obj_num)
catalog = add_obj(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)

# ---- Serialize with xref --------------------------------------------------
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
buf += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, catalog, xref_pos)

open(OUT, "wb").write(buf)
print(f"wrote {OUT}:", len(buf), "bytes,", len(pages), "page(s)")

# The app builds its own PDF in the browser for upload fields, so it needs the
# same source text this PDF was made from. Publishing it here (rather than
# copying by hand) is what keeps the downloaded CV and the attached CV the same
# document.
if SRC in ("cv.md", "cv.nl.md"):
    pub = "docs/cv.en.md" if SRC == "cv.md" else "docs/cv.nl.md"
    open(pub, "w", encoding="utf-8").write(open(SRC, encoding="utf-8").read())
    print("published", pub)
