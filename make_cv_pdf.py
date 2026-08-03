#!/usr/bin/env python3
"""Render cv.md into a clean, dependency-free A4 PDF (Helvetica + Helvetica-Bold)."""
import re, zlib

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

def wrap(text, fs, maxw):
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        cur = ""
        for word in para.split(" "):
            cand = (cur + " " + word).strip()
            if tw(cand, fs) > maxw and cur:
                out.append(cur); cur = word
            else:
                cur = cand
        if cur:
            out.append(cur)
    return out

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

md = clean(open("cv.md", encoding="utf-8").read())
lines = md.split("\n")
items = []  # (font, size, text, gap)
def add(font, size, text, gap=0, maxw=USABLE, indent=0):
    for i, wl in enumerate(wrap(text, size, maxw)):
        items.append((font, size, wl, gap if i == 0 else 0, indent))

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
for (font, size, text, gap, indent) in items:
    y -= gap
    lead = size * 1.32
    if y - lead < BOT:
        pages.append(cur); cur = []; y = TOP
    y -= lead
    cur.append((L + indent, y, font, size, text))
if cur:
    pages.append(cur)

def stream_for(pg):
    out = []
    for (x, y, font, size, text) in pg:
        out.append(f"BT /{font} {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({esc(text)}) Tj ET")
    return "\n".join(out).encode("cp1252", "replace")

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

pages_obj_num = len(objs) + len(pages) + 1  # reserve
page_nums = []
for idx, pg in enumerate(pages):
    pn = add_obj(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %d 0 R /Contents %d 0 R >>"
        % (pages_obj_num, PAGE_W, PAGE_H, res_obj, content_objs[idx]))
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

open("docs/cv.pdf", "wb").write(buf)
print("wrote docs/cv.pdf:", len(buf), "bytes,", len(pages), "page(s)")
