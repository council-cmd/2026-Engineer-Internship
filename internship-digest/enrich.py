"""Optionally open each jobright.ai job page to read its description.

We could not verify from the build environment whether jobright serves
descriptions to anonymous visitors, so this module assumes nothing: it tries
several extraction strategies, recognises a login wall, and gives up quietly
after a few consecutive failures. The digest still works with no
descriptions at all - jobs simply land in the "Unclear" visa bucket.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Wording that means "log in to see this", not an actual description.
LOGIN_WALL = re.compile(
    r"sign in to view|log in to view|create an account to|sign up to see|"
    r"please log in|login required|unlock this job",
    re.IGNORECASE,
)

SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL
)
TAG_RE = re.compile(r"<[^>]+>")

# An outbound link to the employer's own posting.
APPLY_RE = re.compile(
    r'href=["\'](https?://(?!(?:www\.)?jobright\.ai)[^"\']+)["\'][^>]*>\s*'
    r'(?:apply|apply now|view job|original posting)',
    re.IGNORECASE,
)


class Fetcher:
    """Fetches job pages politely and stops trying if we are being blocked."""

    def __init__(self, delay: float, max_pages: int, give_up_after: int):
        self.delay = max(0.0, float(delay))
        self.max_pages = int(max_pages)
        self.give_up_after = int(give_up_after)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.fetched = 0
        self.consecutive_failures = 0
        self.blocked = False
        self.block_reason = ""
        self.successes = 0

    @property
    def exhausted(self) -> bool:
        return self.blocked or self.fetched >= self.max_pages

    def _note_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.give_up_after:
            self.blocked = True
            self.block_reason = reason
            log.warning("Giving up on job descriptions after repeated failures: %s", reason)

    def fetch(self, url: str) -> tuple[str, str]:
        """Return (description_text, employer_url). Empty strings on failure."""
        if self.exhausted:
            return "", ""

        # Be polite: wait between requests to jobright.
        if self.fetched and self.delay:
            time.sleep(self.delay)
        self.fetched += 1

        try:
            response = self.session.get(url, timeout=25, allow_redirects=True)
        except requests.RequestException as exc:
            self._note_failure(f"network error: {exc.__class__.__name__}")
            return "", ""

        if response.status_code in (401, 403, 429):
            self._note_failure(f"HTTP {response.status_code} (blocked or rate limited)")
            return "", ""
        if response.status_code >= 400:
            self._note_failure(f"HTTP {response.status_code}")
            return "", ""

        body = response.text or ""
        description = (
            _from_json_ld(body)
            or _from_next_data(body)
            or _from_meta(body)
        )

        if description and LOGIN_WALL.search(description[:800]):
            self._note_failure("login wall")
            return "", ""
        if not description:
            self._note_failure("no description found in page")
            return "", ""

        self.consecutive_failures = 0
        self.successes += 1

        employer = ""
        apply_match = APPLY_RE.search(body)
        if apply_match:
            employer = html.unescape(apply_match.group(1))
        return description, employer


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.IGNORECASE)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _from_json_ld(body: str) -> str:
    """Structured JobPosting data is the most reliable source when present."""
    for raw in SCRIPT_RE.findall(body):
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            if node.get("@type") in ("JobPosting", ["JobPosting"]):
                text = _clean(str(node.get("description", "")))
                if len(text) > 120:
                    return text
    return ""


def _from_next_data(body: str) -> str:
    """Next.js apps embed their page data as JSON; dig out the longest
    description-shaped string."""
    match = NEXT_DATA_RE.search(body)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return ""

    best = ""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(value, str)
                    and len(value) > len(best)
                    and len(value) > 200
                    and key.lower() in (
                        "description", "jobdescription", "job_description",
                        "content", "responsibilities", "requirements",
                    )
                ):
                    best = value
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return _clean(best)


def _from_meta(body: str) -> str:
    """Last resort: the page's own meta description."""
    match = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']{200,})["\']',
        body,
        re.IGNORECASE,
    )
    return _clean(match.group(1)) if match else ""
