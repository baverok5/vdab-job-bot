"""
On-demand application generator (Phase 2).

Triggered by the .github/workflows/prepare.yml workflow with JOB_ID + JOB_URL.
Renders that one job, writes a tailored email + cover letter + CV highlights to
docs/prepared/<JOB_ID>.json, which the app polls after you tap "Apply".

This keeps the hourly bot cheap (it never writes letters); we only spend a
Gemini call on jobs you actually choose to apply to.
"""

import json
import os

from playwright.sync_api import sync_playwright

import bot  # reuse rendering + Gemini helpers

OUT_DIR = "docs/prepared"


def main():
    job_id = os.environ.get("JOB_ID", "").strip()
    job_url = os.environ.get("JOB_URL", "").strip()
    if not job_id or not job_url:
        raise SystemExit("JOB_ID and JOB_URL are required")
    if not bot.GEMINI_KEY:
        raise SystemExit("GEMINI_API_KEY is not set")

    cv_text = open(bot.CV_FILE, encoding="utf-8").read()
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{job_id}.json")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            job_text, apply_email = bot.fetch_job_detail(browser, job_url, job_id)
        finally:
            browser.close()

    if not job_text:
        result = {"id": job_id, "url": job_url, "status": "error",
                  "error": "Could not read the job page."}
        bot.save_json(out_path, result)
        raise SystemExit("Could not render job page")

    docs = bot.generate_application(job_text, cv_text) or {}
    result = {
        "id": job_id,
        "url": job_url,
        "status": "ready" if docs else "error",
        "apply_email": apply_email or "",
        "email_subject": docs.get("email_subject", ""),
        "email_body": docs.get("email_body", ""),
        "cover_letter": docs.get("cover_letter", ""),
        "cv_highlights": docs.get("cv_highlights", ""),
    }
    bot.save_json(out_path, result)
    print(f"Wrote {out_path} (status={result['status']})")


if __name__ == "__main__":
    main()
