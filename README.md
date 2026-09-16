# vdab-job-bot

## The CV

There is **one** CV. It is not rewritten, reordered or "tailored" for individual
jobs — that was removed deliberately.

| file | what it is |
| --- | --- |
| `cv.md` | the CV, English. The one that goes out with every English application. |
| `cv.nl.md` | the same CV translated to Dutch. Nothing else differs. |
| `docs/cv.pdf`, `docs/cv-nl.pdf` | rendered by `make_cv_pdf.py`, with clickable links |
| `docs/cv.en.md`, `docs/cv.nl.md` | published copies the app reads to build its own PDF |

After editing `cv.md` or `cv.nl.md`, rebuild both:

```
python3 make_cv_pdf.py                       # -> docs/cv.pdf  + docs/cv.en.md
python3 make_cv_pdf.py cv.nl.md docs/cv-nl.pdf   # -> docs/cv-nl.pdf + docs/cv.nl.md
```

`make_cv_pdf.py` reproduces the layout of the CV Baver supplied, measured off
it: 9.5pt body, 12.5pt title-case section headings, 10.5pt bold job titles,
bold run-in labels (`**SEO & GEO:**`), links in his blue with a real clickable
annotation, Turkish letters via a /Differences encoding rather than
transliteration, and one page — if a translation runs long the whole document
is re-flowed a few percent smaller rather than spilling onto a second page. The app
strips the markdown for anything shown on screen or pasted into a form, and its
⬇️ CV button downloads `docs/cv.pdf` itself rather than rebuilding one, because
a PDF assembled in the browser cannot carry the links.

A change to the CV reaches every letter at once, including ones written months
ago, because the app serves the CV from those published files rather than from
each stored letter.

**When building a Gmail draft (the 📮 signal):** attach the CV PDF built from
`cv.md` for an English posting, `cv.nl.md` for a Dutch one. Two separate PDFs —
CV and cover letter are never merged. Never regenerate or reword the CV for the
job.

## Data files — do not hand-edit

`seen.json`, `screen.json`, `docs/jobs.json` and `docs/listing.json` are written
by the bot during a run. Editing them in the repo does not work: a run already in
flight writes its own copy back over the change. To alter bot state, add a
flag-driven migration inside `bot.py` (see `crm_requeue_v1`,
`new_cv_exp_requeue_v1`) so the bot performs it during its own run.

A storage change is not finished until **both** the save and the load path have
been verified by a real run.
