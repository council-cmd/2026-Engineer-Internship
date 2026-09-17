"""Sends the daily email through Resend (https://resend.com).

The API key is read from the RESEND_API_KEY environment variable, which on
GitHub Actions comes from a repository secret. The key is never written to
the repo, the log, or the email itself.
"""

from __future__ import annotations

import html
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.resend.com/emails"


class MailError(RuntimeError):
    pass


def send(subject: str, html_body: str, text_body: str, config: dict) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise MailError(
            "RESEND_API_KEY is not set. Add it as a repository secret named "
            "RESEND_API_KEY (Settings -> Secrets and variables -> Actions)."
        )

    payload = {
        "from": config["email"]["from"],
        "to": [config["email"]["to"]],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    last_error = ""
    for attempt in range(1, 5):
        try:
            response = requests.post(
                API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if response.status_code < 300:
                log.info("Email sent: %s", subject)
                return
            # Rate limiting and server faults are worth retrying; a bad key
            # or a rejected address is not.
            if response.status_code not in (408, 429) and response.status_code < 500:
                raise MailError(
                    f"Resend rejected the email (HTTP {response.status_code}): "
                    f"{response.text[:400]}"
                )
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"

        if attempt < 4:
            wait = 2 ** attempt
            log.warning("Email attempt %d failed (%s); retrying in %ss", attempt, last_error, wait)
            time.sleep(wait)

    raise MailError(f"Could not send email after 4 attempts. Last error: {last_error}")


# --------------------------------------------------------------------------
#  Email bodies
# --------------------------------------------------------------------------

STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    "color:#1a1a1a;line-height:1.5;"
)

BUCKET_COLOR = {"A": "#1a7f37", "B": "#9a6700", "C": "#b35900"}


def _bucket_badge(bucket: str) -> str:
    letter = (bucket or "B")[0]
    color = BUCKET_COLOR.get(letter, "#57606a")
    return (
        f'<span style="background:{color};color:#fff;border-radius:3px;'
        f'padding:1px 6px;font-size:11px;font-weight:600;">{html.escape(bucket)}</span>'
    )


def _group(jobs: list) -> list[dict]:
    """Collapse the same role posted in many cities into one entry.

    One employer routinely lists an identical internship in twenty cities.
    Those are separate rows in your spreadsheet, but showing twenty copies
    would bury everything else in the email.
    """
    groups: dict[tuple, dict] = {}
    for job in jobs:
        key = (job.company.lower().strip(), job.title.lower().strip())
        place = ", ".join(x for x in (job.city, job.state) if x) or (
            job.location_raw or "Location not stated"
        )
        entry = groups.get(key)
        if entry is None:
            groups[key] = {
                "job": job,
                "places": [place],
                "count": 1,
            }
        else:
            entry["count"] += 1
            if place not in entry["places"]:
                entry["places"].append(place)
            if job.fit_score > entry["job"].fit_score:
                entry["job"] = job
    ordered = sorted(
        groups.values(),
        key=lambda g: (-g["job"].fit_score, g["job"].company.lower(), g["job"].title.lower()),
    )
    return ordered


def _places_label(entry: dict, limit: int = 4) -> str:
    places = entry["places"]
    if len(places) <= limit:
        return " / ".join(places)
    return " / ".join(places[:limit]) + f" and {len(places) - limit} more"


def build_digest(jobs: list, stats: dict, config: dict) -> tuple[str, str]:
    """Return (html, plain text) for a digest with at least one job."""
    limit = int(config["email"].get("max_in_email", 25))
    entries = _group(jobs)
    shown = entries[:limit]
    hidden_roles = len(entries) - len(shown)

    heading = f"{len(entries)} new mechanical internship{'s' if len(entries) != 1 else ''}"
    if len(jobs) != len(entries):
        heading += f" ({len(jobs)} postings)"

    parts = [
        f'<div style="{STYLE}max-width:680px;">',
        f'<h2 style="margin:0 0 4px;">{heading}</h2>',
        f'<p style="margin:0 0 16px;color:#57606a;font-size:13px;">'
        f'{html.escape(stats["date"])} &middot; '
        f'{stats["parsed"]} postings scanned &middot; '
        f'{stats["new_total"]} new since last run</p>',
    ]

    if stats.get("warnings"):
        for warning in stats["warnings"]:
            parts.append(
                '<p style="background:#fff8c5;border-left:3px solid #d4a72c;'
                'padding:8px 12px;margin:0 0 14px;font-size:13px;">'
                f'{html.escape(warning)}</p>'
            )

    for entry in shown:
        job = entry["job"]
        location = _places_label(entry)
        if entry["count"] > 1:
            location += f' ({entry["count"]} locations)'
        parts.append(
            '<div style="border:1px solid #d0d7de;border-radius:6px;'
            'padding:12px 14px;margin:0 0 10px;">'
            f'<div style="font-size:15px;font-weight:600;">'
            f'<a href="{html.escape(job.url)}" style="color:#0969da;text-decoration:none;">'
            f'{html.escape(job.title)}</a></div>'
            f'<div style="font-size:13px;color:#24292f;margin-top:2px;">'
            f'{html.escape(job.company)}</div>'
            f'<div style="font-size:12px;color:#57606a;margin-top:4px;">'
            f'{html.escape(location)} &middot; {html.escape(job.work_model or "n/a")} '
            f'&middot; fit {job.fit_score}/100</div>'
            f'<div style="margin-top:8px;">{_bucket_badge(job.visa_bucket)} '
            f'<span style="font-size:12px;color:#57606a;">'
            f'{html.escape(job.visa_reason)}</span></div>'
            '</div>'
        )

    if hidden_roles > 0:
        parts.append(
            f'<p style="font-size:13px;color:#57606a;">'
            f'+ {hidden_roles} more role(s) in your spreadsheet (data/jobs.csv in your repo).</p>'
        )

    parts.append(
        '<hr style="border:none;border-top:1px solid #d0d7de;margin:18px 0 10px;">'
        '<p style="font-size:11px;color:#57606a;margin:0;">'
        'A = likely OK for international students &middot; '
        'B = no visa information found &middot; '
        'C = excluded (not shown).<br>'
        'Always confirm sponsorship with the employer before applying.</p></div>'
    )

    text_lines = [
        f"{heading} - {stats['date']}",
        f"{stats['parsed']} postings scanned, {stats['new_total']} new since last run",
        "",
    ]
    for warning in stats.get("warnings", []):
        text_lines += [f"NOTE: {warning}", ""]
    for entry in shown:
        job = entry["job"]
        location = _places_label(entry)
        if entry["count"] > 1:
            location += f' ({entry["count"]} locations)'
        text_lines += [
            f"* {job.title}",
            f"  {job.company} - {location} - {job.work_model or 'n/a'} - fit {job.fit_score}/100",
            f"  Visa: {job.visa_bucket} ({job.visa_reason})",
            f"  {job.url}",
            "",
        ]
    if hidden_roles > 0:
        text_lines.append(f"+ {hidden_roles} more role(s) in data/jobs.csv")
    if stats.get("fetch_report"):
        text_lines += ["", stats["fetch_report"]]

    return "".join(parts), "\n".join(text_lines)


def build_empty(stats: dict) -> tuple[str, str]:
    warnings = "".join(
        '<p style="background:#fff8c5;border-left:3px solid #d4a72c;padding:8px 12px;'
        f'margin:10px 0 0;font-size:13px;">{html.escape(w)}</p>'
        for w in stats.get("warnings", [])
    )
    body = (
        f'<div style="{STYLE}max-width:680px;">'
        f'<p style="margin:0;">No new mechanical internships today.</p>'
        f'<p style="margin:8px 0 0;color:#57606a;font-size:13px;">'
        f'Checked {stats["parsed"]} postings on {html.escape(stats["date"])}. '
        f'The tool ran normally.</p>{warnings}</div>'
    )
    text = (
        f"No new mechanical internships today.\n"
        f"Checked {stats['parsed']} postings on {stats['date']}. The tool ran normally.\n"
    )
    for warning in stats.get("warnings", []):
        text += f"\nNOTE: {warning}\n"
    return body, text


def build_failure(error: str, stats: dict) -> tuple[str, str]:
    body = (
        f'<div style="{STYLE}max-width:680px;">'
        '<h2 style="margin:0 0 8px;color:#b35900;">Your internship digest failed</h2>'
        '<p style="margin:0 0 10px;">The tool could not finish its run today, so '
        'there is no job list. This usually means the source page changed '
        'format or was unreachable.</p>'
        '<pre style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;'
        'padding:10px;font-size:12px;white-space:pre-wrap;">'
        f'{html.escape(error)[:3000]}</pre>'
        '<p style="font-size:13px;color:#57606a;">Full logs are in the Actions tab '
        'of your repository.</p></div>'
    )
    text = (
        "Your internship digest failed.\n\n"
        f"{error[:3000]}\n\n"
        "Full logs are in the Actions tab of your repository.\n"
    )
    return body, text
