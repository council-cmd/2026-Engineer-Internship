"""Keeps the two files that live in your repo between runs:

  data/seen.json - every job we have already told you about
  data/jobs.csv  - your running spreadsheet

The CSV is append-only on purpose. You will be typing your own notes into
the "application status" column, and a rewrite would wipe them out, so
existing rows are read back and written out unchanged.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "date_found",
    "company",
    "title",
    "location",
    "area_group",
    "visa_bucket",
    "fit_score",
    "link",
    "application_status",
    "visa_reason",
    "work_model",
    "date_posted",
    "job_id",
]

# Columns you fill in yourself; never overwritten once set.
USER_COLUMNS = {"application_status"}


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file so a crash cannot leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Seen:
    """The memory of which jobs have already been reported."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.ids = dict(data.get("job_ids", {}))
            self.keys = dict(data.get("dedupe_keys", {}))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt memory file must not stop the run; we would rather
            # re-report a few jobs than crash.
            log.warning("Could not read %s (%s) - starting a fresh record", self.path, exc)

    def is_new(self, job) -> bool:
        return job.job_id not in self.ids and job.dedupe_key not in self.keys

    def remember(self, job, when: date) -> None:
        stamp = when.isoformat()
        self.ids[job.job_id] = stamp
        self.keys.setdefault(job.dedupe_key, stamp)

    def save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job_ids": self.ids,
            "dedupe_keys": self.keys,
        }
        _atomic_write(self.path, json.dumps(payload, indent=1, sort_keys=True))

    def prune(self, keep: int = 60000) -> None:
        """Stop the memory file growing without bound. Oldest entries go first;
        the repo only ever shows a rolling window so they cannot reappear."""
        for attr in ("ids", "keys"):
            mapping = getattr(self, attr)
            if len(mapping) > keep:
                ordered = sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)
                setattr(self, attr, dict(ordered[:keep]))


class Spreadsheet:
    """The running CSV of every mechanical-relevant job ever found."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        self.existing_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    self.rows.append(row)
                    if row.get("job_id"):
                        self.existing_ids.add(row["job_id"])
        except OSError as exc:
            log.warning("Could not read %s (%s) - a new sheet will be started", self.path, exc)

    def add(self, job, found_on: date) -> bool:
        if job.job_id in self.existing_ids:
            return False
        self.rows.append(
            {
                "date_found": found_on.isoformat(),
                "company": job.company,
                "title": job.title,
                "location": _location_label(job),
                "area_group": job.area_group,
                "visa_bucket": job.visa_bucket,
                "fit_score": str(job.fit_score),
                "link": job.url,
                "application_status": "",
                "visa_reason": job.visa_reason,
                "work_model": job.work_model,
                "date_posted": job.date_posted.isoformat() if job.date_posted else "",
                "job_id": job.job_id,
            }
        )
        self.existing_ids.add(job.job_id)
        return True

    def save(self) -> None:
        import io

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
        _atomic_write(self.path, buffer.getvalue())


def _location_label(job) -> str:
    if job.city and job.state:
        return f"{job.city}, {job.state}"
    if job.state:
        return job.state
    if job.city:
        return job.city
    return job.location_raw or "Unknown"
