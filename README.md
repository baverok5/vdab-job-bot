# vdab-job-bot

## The CV

The CV is **structured data** (new design with photo, two A4 pages), and every
Ready job gets its **own copy, tailored to the posting**.

| file | what it is |
| --- | --- |
| `cv_master.en.json`, `cv_master.nl.json` | the source of truth: the real CV in English and Dutch, every bullet with an id (same ids in both) |
| `cv_photo.jpg` | the photo in the header |
| `cv_render.py` | HTML/PDF design, markdown export and the check that keeps tailored CVs honest |
| `cv.md`, `cv.nl.md`, `docs/cv.en.md`, `docs/cv.nl.md` | generated markdown (what the evaluator, letter writer and app read) |
| `docs/cv.pdf`, `docs/cv-nl.pdf` | generated general CV (used when no tailored one exists) |
| `docs/prepared/cv/<job id>.pdf` | the CV tailored to one job, plus `cv_pdf` / `cv_text` in `docs/prepared/<id>.json` |

After editing a `cv_master.*.json`, rebuild the generated files:

```
python3 cv_render.py     # -> cv.md, cv.nl.md, docs/cv.*.md, docs/cv.pdf, docs/cv-nl.pdf
```

**Tailoring** (`tailor_cv` in `bot.py`): the writer model gets the posting and the
CV's bullets by id. It returns a headline, a rewritten summary, which bullets to
keep and in what order (lightly reworded to the posting's vocabulary), which
projects to show and the skills order. `cv_render.merge_tailored` then drops
anything that is not true: unknown ids, any number or web address not in the real
CV, skills that are not in the CV. The three main jobs (Episto, freelance, KXP)
always stay, and a tailored CV is at most two pages. The language follows the
posting (Dutch posting -> Dutch CV). Each run tailors up to `CV_TAILOR_BUDGET`
(120) Ready jobs that do not have one yet, best fits first, new matches get theirs
with the letter, and PDFs of jobs that left Ready are deleted.

**When building a Gmail draft (the 📮 signal):** attach the tailored CV from the
signal comment (`prepared/cv/<id>.pdf`) if it is named there, otherwise the general
`docs/cv.pdf` (English) / `docs/cv-nl.pdf` (Dutch). CV and cover letter are never
merged into one file.

## What a run costs, and the switches

DeepSeek is the only paid part. Every run ends with a line saying what it spent
(calls, tokens, how much came from cache, reasoning tokens, which model answered,
and cost by purpose: reads, letters, titles). Prices are third-party figures held
in `DS_PRICE`, so the total is approximate; check it against
platform.deepseek.com/usage.

- **One run a day**, 10:00 UTC (GitHub starts it ~3-5 h late), by schedule only. It is
  deliberately thorough: every search term runs every time, and it can take 3+ hours. Pushes do **not** start a run
  (they used to, and each one cost money). To run now: Actions → *VDAB job bot*
  → *Run workflow*.
- **Peak pricing.** DeepSeek bills double 01:00-04:00 and 06:00-10:00 UTC on
  weekdays. The slot is 13:00 because GitHub fires jobs up to ~4.5 h late and a
  full run takes ~5 h; the bot also refuses to call DeepSeek during peak
  (`DS_PEAK_GUARD=0` to disable; manual runs are exempt).
- **Run budget.** A run stops spending at `DS_RUN_BUDGET` dollars (default 0.25,
  a hard ceiling of about $7.50 a month) and leaves the rest of the queue for
  tomorrow.
- **Thinking.** Requests carry `{"thinking": {"type": "disabled"}}`. Measured with
  the probe: reasoning was only ~15% of output on the screening prompt, so this
  is a modest saving, and verdicts matched on both samples.
  `DS_THINKING=enabled` or `default` changes it.
- **Model name.** `deepseek-flash` and `deepseek-v4-flash` are the same model:
  asked for either, the API answers `served-by=deepseek-flash`, and the usage
  dashboard only ever shows that name. There is no cheaper model behind the
  other spelling.
- **Re-reads.** `REVET_MAX_PRIORITY` (default 2.5) keeps warehouse and
  customer-service postings out of the re-vet queue.

To measure instead of guess, run *Actions → DeepSeek probe*: it sends the real
screening prompt to the live API with thinking default vs disabled and under both
model names, and prints what DeepSeek reports. About a cent, makes no commit.

## Search coverage

LinkedIn, VDAB and EURES are searched by keyword (`LI_PRIORITY_KEYWORDS`,
`LI_ROTATING_KEYWORDS`, `PRIORITY_SEARCH_URLS`, `EURES_KEYWORDS` in `bot.py`). A job
whose title contains none of the keywords is never collected. Example: "Jr AI
Strategist" (BOI, Antwerp) was open for weeks and missing from the app until AI
queries were added. If a good job is missing, check its title against these lists
first. Everything the candidate actually does belongs in the *every run* lists
(`LI_PRIORITY_KEYWORDS`, `PRIORITY_SEARCH_URLS`); vague or niche terms go in the
rotating ones, which each run only walks a slice of. The goal field the AI judges against (`CANDIDATE_ONELINE`,
`CANDIDATE_PROFILE`, the eval prompt) also has to name the kind of job, or it is
dropped at the title screen.

## Data files — do not hand-edit

`seen.json`, `screen.json`, `docs/jobs.json` and `docs/listing.json` are written
by the bot during a run. Editing them in the repo does not work: a run already in
flight writes its own copy back over the change. To alter bot state, add a
flag-driven migration inside `bot.py` (see `crm_requeue_v1`,
`new_cv_exp_requeue_v1`) so the bot performs it during its own run.

A storage change is not finished until **both** the save and the load path have
been verified by a real run.
