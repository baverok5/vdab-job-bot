"""Daily closed-posting check: no AI, no new jobs, nothing but "is it still open?".

Re-checks every job in Ready (VDAB, LinkedIn, EURES, StepStone and the job boards)
and moves the ones that stopped accepting applications into Closed, which the app
no longer lists. Runs on its own schedule (.github/workflows/closed_check.yml), so
closed jobs leave Ready every morning even when the long bot run is late or
fails. The bot's own run does the same check at its start.
"""
import os

from playwright.sync_api import sync_playwright

import bot

HEAVY = ("listing", "verdicts", "dropped")


def main():
    jobs = bot.load_json(bot.JOBS_FILE, {"updated": "", "jobs": []})
    jobs.update(bot.load_json(bot.LISTING_FILE, {}))      # verdicts live in listing.json
    jobs.setdefault("rejected", [])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            moved = bot.sweep_closed(browser, jobs,
                                     max_minutes=float(os.environ.get("CLOSED_SWEEP_MINUTES", "100")))
        finally:
            browser.close()
    bot.prune_tailored_cvs(jobs)
    # jobs.json always (it carries each job's last-check date); the 38 MB
    # listing.json only when something closed, so a quiet day adds nothing to git.
    if moved:
        bot.save_jobs_split(jobs)
    else:
        bot.save_json(bot.JOBS_FILE, {k: v for k, v in jobs.items() if k not in HEAVY})
    print(f"Done: {moved} job(s) moved to Closed, {len(jobs['jobs'])} left in Ready.")


if __name__ == "__main__":
    main()
