#!/usr/bin/env python3
"""Daily mechanical-engineering internship digest.

What one run does:
  1. download the jobright README
  2. parse its table into clean job records
  3. drop anything already seen, and anything that is not mechanical
  4. optionally read each job description to judge visa eligibility
  5. append everything kept to data/jobs.csv
  6. send exactly one email - a digest, a "nothing new" note, or a failure alert

Run it by hand with:   python3 digest.py
Preview without sending or saving:   python3 digest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml

import enrich
import filters
import jobparser
import mailer
import store

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOG_PATH = DATA / "errors.log"

log = logging.getLogger("digest")


def setup_logging(verbose: bool) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Failures are also appended to a file that is committed back to the repo,
    # so there is a durable record even if the Actions logs expire.
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(file_handler)


def load_config() -> dict:
    with (HERE / "config.yaml").open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def download(url: str) -> str:
    """Fetch the README, retrying a few times on network trouble."""
    last = ""
    for attempt in range(1, 5):
        try:
            response = requests.get(
                url, timeout=30, headers={"User-Agent": "internship-digest/1.0"}
            )
            if response.status_code == 200:
                if "TABLE_START" not in response.text:
                    raise jobparser.FormatError(
                        "The downloaded README does not contain the expected table markers."
                    )
                return response.text
            last = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last = f"network error: {exc}"
        if attempt < 4:
            import time
            wait = 2 ** attempt
            log.warning("Download attempt %d failed (%s); retrying in %ss", attempt, last, wait)
            time.sleep(wait)
    raise RuntimeError(f"Could not download the job list after 4 attempts. Last error: {last}")


def run(dry_run: bool = False, skip_email: bool = False) -> int:
    config = load_config()
    today = datetime.now(timezone.utc).date()
    stats: dict = {"date": today.isoformat(), "warnings": []}

    markdown = download(config["source"]["readme_url"])
    jobs, bad_rows = jobparser.parse(markdown, today=today)
    stats["parsed"] = len(jobs)
    log.info("Parsed %d postings (%d unreadable rows)", len(jobs), bad_rows)
    if bad_rows:
        stats["warnings"].append(
            f"{bad_rows} rows in the source table could not be read and were skipped."
        )

    seen = store.Seen(DATA / "seen.json")
    sheet = store.Spreadsheet(DATA / "jobs.csv")
    first_run = not seen.ids

    # --- what is new since last time --------------------------------------
    fresh = []
    batch_keys: set[str] = set()
    for job in jobs:
        if not seen.is_new(job) or job.dedupe_key in batch_keys:
            continue
        batch_keys.add(job.dedupe_key)
        fresh.append(job)
    stats["new_total"] = len(fresh)
    log.info("%d of %d postings are new", len(fresh), len(jobs))

    # Because the source keeps only the newest ~100 postings and replaces them
    # several times a day, a full turnover means jobs appeared and vanished
    # between runs. Say so rather than letting it pass silently.
    if jobs and len(fresh) == len(jobs) and not first_run:
        stats["warnings"].append(
            "Every posting on the list was new since the last run, so some jobs were "
            "almost certainly posted and removed in between. Running the collector more "
            "often would catch them (see README.md, 'Checking more often')."
        )

    # --- mechanical titles only -------------------------------------------
    candidates = []
    for job in fresh:
        keep, strength = filters.title_verdict(job.title, filters.Rules(config))
        if keep:
            candidates.append((job, strength))
    log.info("%d of %d new postings look mechanical", len(candidates), len(fresh))

    rules = filters.Rules(config)

    # --- read descriptions where possible ---------------------------------
    fetcher = None
    if config["jobright"].get("fetch_descriptions", True) and candidates and not dry_run:
        fetcher = enrich.Fetcher(
            delay=config["jobright"].get("delay_seconds", 3.0),
            max_pages=config["jobright"].get("max_pages_per_run", 60),
            give_up_after=config["jobright"].get("give_up_after_failures", 8),
        )
        for job, _ in candidates:
            if fetcher.exhausted:
                break
            description, employer_url = fetcher.fetch(job.url)
            job.description = description
            job.employer_url = employer_url
        log.info(
            "Read %d job descriptions from %d page loads", fetcher.successes, fetcher.fetched
        )
        if fetcher.blocked and fetcher.successes == 0:
            stats["warnings"].append(
                "Job descriptions could not be read from jobright.ai "
                f"({fetcher.block_reason}), so visa status is marked "
                '"Unclear" rather than confirmed. Check these yourself before applying.'
            )
        elif fetcher.blocked:
            stats["warnings"].append(
                f"Stopped reading job descriptions partway ({fetcher.block_reason}); "
                "later jobs show less visa detail."
            )

    # --- visa classification, scoring, saving ------------------------------
    for job, _ in candidates:
        job.visa_bucket, job.visa_reason = filters.classify_visa(job, rules)

    # Employers repost the same role with and without "(US Person Required)".
    # Make one copy's restriction apply to all of them.
    spread = filters.apply_sibling_exclusions([job for job, _ in candidates])
    if spread:
        log.info("%d job(s) excluded because a duplicate posting restricts them", spread)

    kept = []
    excluded = 0
    for job, strength in candidates:
        if job.visa_bucket == filters.EXCLUDED:
            excluded += 1
            seen.remember(job, today)  # never show it again
            continue
        if job.visa_bucket == filters.UNCLEAR and not rules.include_unclear:
            seen.remember(job, today)
            continue
        job.fit_score = filters.score(job, rules, strength, today=today)
        kept.append(job)

    kept.sort(key=lambda j: (-j.fit_score, j.company.lower(), j.title.lower()))
    stats["kept"] = len(kept)
    stats["excluded"] = excluded
    log.info("%d jobs kept, %d excluded on visa grounds", len(kept), excluded)

    if dry_run:
        entries = mailer._group(kept)
        print(f"\n--- DRY RUN: {len(entries)} role(s) / {len(kept)} posting(s) would be emailed ---")
        for entry in entries[: config["email"].get("max_in_email", 25)]:
            job = entry["job"]
            place = mailer._places_label(entry)
            if entry["count"] > 1:
                place += f' ({entry["count"]} locations)'
            print(f"  [{job.fit_score:3d}] {job.visa_bucket[:1]} {job.title[:60]}")
            print(f"        {job.company} - {place}")
        for warning in stats["warnings"]:
            print(f"  NOTE: {warning}")
        print("--- nothing was saved or emailed ---")
        return 0

    # Record every new posting, mechanical or not, so it is never re-reported.
    for job in fresh:
        seen.remember(job, today)
    for job in kept:
        sheet.add(job, today)

    seen.prune()
    seen.save()
    sheet.save()
    log.info("Saved: %d rows in the spreadsheet", len(sheet.rows))

    if skip_email:
        log.info("Email skipped (--no-email).")
        return 0

    # --- exactly one email -------------------------------------------------
    subject_date = today.strftime("%b %d")
    if kept:
        html_body, text_body = mailer.build_digest(kept, stats, config)
        subject = config["email"]["subject_found"].format(n=len(kept), date=subject_date)
    else:
        html_body, text_body = mailer.build_empty(stats)
        subject = config["email"]["subject_empty"].format(n=0, date=subject_date)
    mailer.send(subject, html_body, text_body, config)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily mechanical internship digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent, without saving or emailing",
    )
    parser.add_argument(
        "--no-email", action="store_true", help="save results but do not send email"
    )
    parser.add_argument("--verbose", action="store_true", help="print more detail")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        return run(dry_run=args.dry_run, skip_email=args.no_email)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        detail = traceback.format_exc()
        log.error("Run failed: %s", exc)
        log.error(detail)

        # A silent failure is the worst outcome: it looks exactly like
        # "no jobs today". Always try to raise the alarm.
        if not (args.dry_run or args.no_email):
            try:
                config = load_config()
                stats = {"date": datetime.now(timezone.utc).date().isoformat()}
                html_body, text_body = mailer.build_failure(f"{exc}\n\n{detail}", stats)
                subject = config["email"]["subject_error"].format(
                    n=0, date=datetime.now(timezone.utc).strftime("%b %d")
                )
                mailer.send(subject, html_body, text_body, config)
                log.info("Failure alert emailed.")
            except Exception as mail_exc:  # noqa: BLE001
                log.error("Could not even send the failure email: %s", mail_exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
