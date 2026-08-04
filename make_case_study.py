#!/usr/bin/env python3
"""Render the mirook.com one-page work sample into docs/mirook-case-study.pdf.

Dependency-free (same approach as make_cv_pdf.py): Helvetica + Helvetica-Bold,
WinAnsi, real glyph widths so lines wrap properly. Every claim here comes from
cv.md — nothing about the project is invented.
"""
import zlib

W_REG = {" ":278,"!":278,'"':355,"#":556,"$":556,"%":889,"&":667,"'":191,"(":333,")":333,
"*":389,"+":584,",":278,"-":333,".":278,"/":278,"0":556,"1":556,"2":556,"3":556,"4":556,
"5":556,"6":556,"7":556,"8":556,"9":556,":":278,";":278,"<":584,"=":584,">":584,"?":556,
"@":1015,"A":667,"B":667,"C":722,"D":722,"E":667,"F":611,"G":778,"H":722,"I":278,"J":500,
"K":667,"L":556,"M":833,"N":722,"O":778,"P":667,"Q":778,"R":722,"S":667,"T":611,"U":722,
"V":667,"W":944,"X":667,"Y":667,"Z":611,"[":278,"\\":278,"]":278,"^":469,"_":556,"`":333,
"a":556,"b":556,"c":500,"d":556,"e":556,"f":278,"g":556,"h":556,"i":222,"j":222,"k":500,
"l":222,"m":833,"n":556,"o":556,"p":556,"q":556,"r":333,"s":500,"t":278,"u":556,"v":500,
"w":722,"x":500,"y":500,"z":500,"{":334,"|":260,"}":334,"~":584,"•":350,"·":278}
W_BOLD = dict(W_REG)
W_BOLD.update({"A":722,"B":722,"C":722,"D":722,"E":667,"F":611,"G":778,"J":556,"K":722,
"L":611,"S":667,"b":611,"c":556,"d":611,"e":556,"f":333,"g":611,"h":611,"k":556,"m":889,
"n":611,"o":611,"p":611,"q":611,"r":389,"s":556,"t":333,"u":611,"v":556,"w":778,"x":556,
"y":556,"'":238,"?":611,"&":722})

def tw(s, fs, bold=False):
    t = W_BOLD if bold else W_REG
    return sum(t.get(c, 611 if bold else 556) for c in s) * fs / 1000.0

def wrap(text, fs, maxw, bold=False):
    out, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word).strip()
        if tw(cand, fs, bold) > maxw and cur:
            out.append(cur); cur = word
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out

def esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

PAGE_W, PAGE_H = 595.28, 841.89
L, R, TOP, BOT = 54, 54, 792, 54
USABLE = PAGE_W - L - R

ops = []          # (x, y, font, size, text, gray)
y = TOP

def line(text, size=10, bold=False, gap=0, indent=0, gray=0.15):
    global y
    y -= gap
    for wl in wrap(text, size, USABLE - indent, bold):
        y -= size * 1.34
        ops.append((L + indent, y, "HB" if bold else "H", size, wl, gray))

def rule(gap=8, gray=0.75):
    global y
    y -= gap
    ops.append(("RULE", y, gray))
    y -= 2

# ---- content (every claim traceable to cv.md) -----------------------------
line("mirook.com", 24, bold=True, gray=0.1)
line("Website design, SEO & GEO  ·  work sample by Baver Ok", 10.5, gray=0.35)
rule(10)

line("THE PROJECT", 10.5, bold=True, gap=10, gray=0.4)
line("A multi-page WordPress site for a Belgian youth chess champion — built from an empty "
     "domain to a complete, search-optimised presence. I handled everything: site structure, "
     "design, content, technical SEO and the social media integration.", 10.5)

line("WHAT I BUILT", 10.5, bold=True, gap=12, gray=0.4)
for b in [
    "Full multi-page site in WordPress using Elementor and the Astra theme — structure, "
    "layout, responsive design and all page content.",
    "Instagram and Facebook integration so tournament news reaches the site and the social "
    "channels together.",
]:
    line("•  " + b, 10.5, gap=2, indent=10)

line("SEO & GEO WORK", 10.5, bold=True, gap=12, gray=0.4)
for b in [
    "On-page SEO across every page: titles, headings, internal linking and keyword-targeted copy.",
    "Structured data in JSON-LD, including SportsEvent markup for tournaments and FAQPage markup, "
    "so results can appear as rich results rather than plain links.",
    "Entity and knowledge-graph markup to connect the player to the wider chess entity graph — "
    "the groundwork search engines and AI assistants use to identify who someone is.",
    "An llms.txt file so AI-powered search engines can read and cite the site correctly (GEO/AEO), "
    "not just traditional crawlers.",
    "Iterative technical SEO/GEO audits, fixing issues and re-auditing each round.",
]:
    line("•  " + b, 10.5, gap=2, indent=10)

line("RESULT", 10.5, bold=True, gap=12, gray=0.4)
line("The iterative audit cycle measurably raised the site's overall search-health score, and the "
     "site now serves as the player's primary online presence — visible both in traditional "
     "search and to AI-powered search engines.", 10.5)

line("WHY IT'S RELEVANT", 10.5, bold=True, gap=12, gray=0.4)
line("This project is where I taught myself the work I now do professionally at Episto: writing "
     "clear content for a real audience, publishing and maintaining it in WordPress, and making "
     "sure it can actually be found. I ran it end to end — no brief handed to me, no team to "
     "hand it off to.", 10.5)

rule(14)
line("Live site: mirook.com    ·    Baver Ok  ·  baverok@gmail.com  ·  "
     "+32 470 42 48 36  ·  linkedin.com/in/baverok", 9.5, gray=0.35)

# ---- emit PDF -------------------------------------------------------------
stream = []
for op in ops:
    if op[0] == "RULE":
        _, ry, gray = op
        stream.append(f"{gray:.2f} G 0.6 w {L} {ry:.1f} m {PAGE_W - R} {ry:.1f} l S")
    else:
        x, oy, font, size, text, gray = op
        stream.append(
            f"BT {gray:.2f} g /{font} {size:.1f} Tf 1 0 0 1 {x:.1f} {oy:.1f} Tm ({esc(text)}) Tj ET")
raw = "\n".join(stream).encode("cp1252", "replace")
comp = zlib.compress(raw)

objs = []
def add(b):
    objs.append(b); return len(objs)

f_h = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
f_hb = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
res = add(f"<< /Font << /H {f_h} 0 R /HB {f_hb} 0 R >> >>".encode())
content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream")
pages_num = len(objs) + 2
page = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %d 0 R /Contents %d 0 R >>"
           % (pages_num, PAGE_W, PAGE_H, res, content))
pages = add(b"<< /Type /Pages /Count 1 /Kids [%d 0 R] >>" % page)
assert pages == pages_num
cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages)

buf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
offs = [0]
for n, body in enumerate(objs, 1):
    offs.append(len(buf))
    buf += b"%d 0 obj\n" % n + body + b"\nendobj\n"
xref = len(buf)
buf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
for o in offs[1:]:
    buf += b"%010d 00000 n \n" % o
buf += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, cat, xref)

open("docs/mirook-case-study.pdf", "wb").write(buf)
print("wrote docs/mirook-case-study.pdf:", len(buf), "bytes; text ends at y =", round(y))
