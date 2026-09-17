"""Read job descriptions, in two rounds.

Round 1 asks jobright.ai, which is the hub that lists the job.
Round 2 follows that page's link to the employer's own posting and reads
that instead, because employer sites and applicant-tracking systems
publish far more complete and reliable text than an aggregator does.

Round 2 exists because round 1 alone is unreliable: in the first real
production run jobright served 8 usable descriptions out of 19 pages.
The other 11 were fetched successfully but contained nothing we could
extract, which is precisely the case the employer site can rescue.
"""

from __future__ import annotations

import collections
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field

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

# A description shorter than this is not worth trusting.
MIN_DESCRIPTION = 200

LOGIN_WALL = re.compile(
    r"sign in to view|log in to view|create an account to|sign up to see|"
    r"please log in|login required|unlock this job",
    re.IGNORECASE,
)

JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL
)
# Next.js app router streams page data through these calls rather than a
# single __NEXT_DATA__ blob, so they have to be read too.
NEXT_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)')
STATE_RE = re.compile(
    r'window\.(?:__INITIAL_STATE__|__NUXT__|__DATA__)\s*=\s*({.*?})\s*[;<]', re.DOTALL
)
TAG_RE = re.compile(r"<[^>]+>")

# Keys that hold description text in embedded page data.
DESCRIPTION_KEYS = {
    "description", "jobdescription", "job_description", "descriptionhtml",
    "jobdescriptionhtml", "content", "jobcontent", "responsibilities",
    "requirements", "qualifications", "jobsummary", "summary", "details",
    "jobdetails", "fulldescription", "body", "text",
}

# Keys that hold a link to the employer's own posting.
APPLY_KEYS = {
    "applyurl", "applylink", "apply_url", "apply_link", "joburl", "job_url",
    "originalurl", "original_url", "sourceurl", "source_url", "externalurl",
    "external_url", "externalapplyurl", "employerurl", "companyjoburl",
    "redirecturl", "applicationurl",
}

APPLY_ANCHOR_RE = re.compile(
    r'href=["\'](https?://[^"\']+)["\'][^>]*>(?:[^<]{0,40})'
    r'(?:apply|view (?:job|posting)|original posting|company site|employer site)',
    re.IGNORECASE,
)

# Text that suggests we really are looking at a job description.
JD_MARKERS = re.compile(
    r"responsibilit|qualification|requirement|what you'?ll do|about the role|"
    r"who you are|your role|the opportunity|essential duties|skills",
    re.IGNORECASE,
)

# Other aggregators - following these just moves the problem sideways.
AGGREGATORS = re.compile(
    r"(?:^|\.)(?:jobright\.ai|linkedin\.com|indeed\.com|ziprecruiter\.com|"
    r"glassdoor\.com|simplyhired\.com|monster\.com|dice\.com|talent\.com|"
    r"jooble\.org|adzuna\.|google\.com|facebook\.com|twitter\.com|x\.com)",
    re.IGNORECASE,
)

# Only these mean "stop trying"; a page we fetched but could not parse does not.
HARD_BLOCK = "blocked"
SOFT_MISS = "unparseable"


@dataclass
class Attempt:
    """What happened for one job, for diagnosis after the fact."""
    url: str
    description: str = ""
    employer_url: str = ""
    source: str = ""          # jobright / employer / none
    notes: list[str] = field(default_factory=list)


class Fetcher:
    """Fetches job pages politely, in two rounds, and records what happened."""

    def __init__(
        self,
        delay: float,
        max_pages: int,
        give_up_after: int,
        follow_employer: bool = True,
        max_employer_pages: int = 40,
    ):
        self.delay = max(0.0, float(delay))
        self.max_pages = int(max_pages)
        self.give_up_after = int(give_up_after)
        self.follow_employer = bool(follow_employer)
        self.max_employer_pages = int(max_employer_pages)

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.fetched = 0                 # round 1 pages
        self.employer_fetched = 0        # round 2 pages
        self.successes = 0               # descriptions obtained, either round
        self.from_employer = 0           # of those, how many came from round 2
        self.consecutive_blocks = 0
        self.blocked = False
        self.block_reason = ""
        self.outcomes: collections.Counter = collections.Counter()
        self.attempts: list[Attempt] = []

    @property
    def exhausted(self) -> bool:
        return self.blocked or self.fetched >= self.max_pages

    # -- internals ---------------------------------------------------------

    def _sleep(self) -> None:
        if (self.fetched + self.employer_fetched) > 1 and self.delay:
            time.sleep(self.delay)

    def _note_block(self, reason: str) -> None:
        """A real block: refused, rate limited, or unreachable."""
        self.outcomes[reason] += 1
        self.consecutive_blocks += 1
        if self.consecutive_blocks >= self.give_up_after:
            self.blocked = True
            self.block_reason = reason
            log.warning("Stopping description reads - repeatedly blocked: %s", reason)

    def _note_miss(self, reason: str) -> None:
        """Page arrived but held no description. Not a reason to stop: the
        very next job may well work, and in production most did."""
        self.outcomes[reason] += 1
        self.consecutive_blocks = 0

    def _get(self, url: str, kind: str) -> str:
        self._sleep()
        try:
            response = self.session.get(url, timeout=25, allow_redirects=True)
        except requests.RequestException as exc:
            self._note_block(f"{kind}: network error ({exc.__class__.__name__})")
            return ""
        if response.status_code in (401, 403, 429):
            self._note_block(f"{kind}: HTTP {response.status_code}")
            return ""
        if response.status_code >= 400:
            self._note_miss(f"{kind}: HTTP {response.status_code}")
            return ""
        return response.text or ""

    # -- public ------------------------------------------------------------

    def fetch(self, url: str) -> tuple[str, str]:
        """Return (description, employer_url) for one job."""
        attempt = Attempt(url=url)
        self.attempts.append(attempt)

        if self.exhausted:
            attempt.notes.append("skipped - budget spent or blocked")
            return "", ""

        # --- round 1: the jobright page ----------------------------------
        self.fetched += 1
        body = self._get(url, "jobright")
        description = ""
        employer_url = ""

        if body:
            description = extract_description(body)
            employer_url = extract_employer_url(body)
            if description and LOGIN_WALL.search(description[:800]):
                self._note_block("jobright: login wall")
                description = ""
            elif description:
                self.consecutive_blocks = 0

        if description:
            attempt.source = "jobright"
            attempt.notes.append("description from jobright")
        elif body:
            self._note_miss("jobright: no description in page")
            attempt.notes.append("jobright page had no readable description")

        # --- round 2: the employer's own posting --------------------------
        # Worth doing even when round 1 succeeded only marginally, because
        # the employer's text is the authoritative one for visa wording.
        if (
            self.follow_employer
            and employer_url
            and not self.blocked
            and self.employer_fetched < self.max_employer_pages
            and len(description) < MIN_DESCRIPTION
        ):
            self.employer_fetched += 1
            attempt.employer_url = employer_url
            employer_body = self._get(employer_url, "employer")
            if employer_body:
                better = extract_description(employer_body)
                if len(better) > len(description):
                    description = better
                    attempt.source = "employer"
                    attempt.notes.append(f"description from employer site: {employer_url}")
                    self.from_employer += 1
                    self.consecutive_blocks = 0
                else:
                    self._note_miss("employer: no description in page")
                    attempt.notes.append("employer page had no readable description")
        elif employer_url:
            attempt.employer_url = employer_url

        if description:
            self.successes += 1
            self.outcomes["description read"] += 1
        else:
            attempt.source = "none"

        attempt.description = description
        return description, employer_url

    def report(self) -> str:
        """One plain sentence describing how description reading went."""
        total = self.fetched
        if not total:
            return "Job descriptions: not attempted this run."
        detail = ", ".join(f"{k} x{v}" for k, v in self.outcomes.most_common())
        extra = ""
        if self.from_employer:
            extra = (
                f" {self.from_employer} of those came from the employer's own site "
                "rather than jobright."
            )
        if self.successes == total:
            return (
                f"Job descriptions: read all {total} job pages successfully.{extra}"
            )
        if self.successes:
            return (
                f"Job descriptions: read {self.successes} of {total} job pages "
                f"({detail}).{extra}"
            )
        return (
            f"Job descriptions: none of the {total} job pages could be read "
            f"({detail}). Visa status could not be confirmed from descriptions, so "
            "every job below is marked Unclear - check each one yourself."
        )

    def diagnostics(self) -> dict:
        """Detail for the run record, so failures can be investigated later
        without needing to reach the sites by hand."""
        misses = [
            {"url": a.url, "employer_url": a.employer_url, "notes": a.notes}
            for a in self.attempts
            if a.source in ("none", "")
        ]
        return {
            "jobright_pages": self.fetched,
            "employer_pages": self.employer_fetched,
            "descriptions_read": self.successes,
            "from_employer_site": self.from_employer,
            "outcomes": dict(self.outcomes),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "unresolved_samples": misses[:8],
        }


# --------------------------------------------------------------------------
#  Extraction
# --------------------------------------------------------------------------

def clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", text, flags=re.IGNORECASE)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_description(body: str) -> str:
    """Try every strategy, best first, and take the first solid result."""
    for strategy in (
        _from_json_ld,
        _from_next_data,
        _from_next_flight,
        _from_window_state,
        _from_meta,
        _from_text_block,
    ):
        try:
            text = strategy(body)
        except (ValueError, TypeError, AttributeError, RecursionError):
            continue
        if text and len(text) >= MIN_DESCRIPTION:
            return text
    return ""


def _walk(node, want_keys: set, best: str = "", min_len: int = MIN_DESCRIPTION) -> str:
    """Depth-first search of decoded JSON for the longest matching string."""
    stack = [node]
    seen = 0
    while stack and seen < 20000:
        seen += 1
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                flat = re.sub(r"[^a-z]", "", str(key).lower())
                if isinstance(value, str):
                    if flat in want_keys and len(value) > len(best) and len(value) >= min_len:
                        best = value
                else:
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return best


def _json_ld_nodes(data):
    """Yield every dict in a JSON-LD document, including @graph members."""
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            if "@graph" in current:
                stack.append(current["@graph"])
        elif isinstance(current, list):
            stack.extend(current)


def _from_json_ld(body: str) -> str:
    """Structured JobPosting data - the most reliable source, and what most
    applicant-tracking systems publish."""
    for raw in JSON_LD_RE.findall(body):
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _json_ld_nodes(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            looks_like_job = "JobPosting" in types or (
                "hiringOrganization" in node and "title" in node
            )
            if looks_like_job and node.get("description"):
                text = clean(str(node["description"]))
                if len(text) >= MIN_DESCRIPTION:
                    return text
    return ""


def _from_next_data(body: str) -> str:
    match = NEXT_DATA_RE.search(body)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return ""
    return clean(_walk(data, DESCRIPTION_KEYS))


def _from_next_flight(body: str) -> str:
    """Next.js app router ships page data as escaped string chunks; join them
    and pull the description out of the reassembled payload."""
    chunks = NEXT_FLIGHT_RE.findall(body)
    if not chunks:
        return ""
    joined = ""
    for chunk in chunks:
        try:
            joined += json.loads(f'"{chunk}"')
        except (json.JSONDecodeError, ValueError):
            continue
    if not joined:
        return ""

    best = ""
    for key in ("description", "jobDescription", "job_description", "content"):
        for match in re.finditer(rf'"{key}"\s*:\s*"', joined):
            start = match.end()
            try:
                value, _ = json.JSONDecoder().raw_decode(joined[start - 1:])
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, str) and len(value) > len(best):
                best = value
    return clean(best)


def _from_window_state(body: str) -> str:
    match = STATE_RE.search(body)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return ""
    return clean(_walk(data, DESCRIPTION_KEYS))


def _from_meta(body: str) -> str:
    match = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+'
        r'content=["\']([^"\']{200,})["\']',
        body,
        re.IGNORECASE,
    )
    return clean(match.group(1)) if match else ""


def _from_text_block(body: str) -> str:
    """Last resort: the page's own visible text, accepted only when it reads
    like a job description, so we never classify visa status off a cookie
    banner or a navigation menu."""
    text = clean(body)
    if len(text) < MIN_DESCRIPTION or not JD_MARKERS.search(text):
        return ""
    return text[:20000]


def extract_employer_url(body: str) -> str:
    """Find a link from the aggregator page to the employer's own posting."""
    candidates: list[str] = []

    for raw in JSON_LD_RE.findall(body):
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _json_ld_nodes(data):
            for key in ("applyUrl", "url", "sameAs"):
                value = node.get(key)
                if isinstance(value, str):
                    candidates.append(value)
            org = node.get("hiringOrganization")
            if isinstance(org, dict):
                for key in ("sameAs", "url"):
                    if isinstance(org.get(key), str):
                        candidates.append(org[key])

    match = NEXT_DATA_RE.search(body)
    if match:
        try:
            candidates.append(_walk(json.loads(match.group(1)), APPLY_KEYS, min_len=8))
        except (json.JSONDecodeError, ValueError):
            pass

    for key in APPLY_KEYS:
        for found in re.finditer(rf'"{key}"\s*:\s*"(https?://[^"\\]+)"', body, re.IGNORECASE):
            candidates.append(found.group(1))

    anchor = APPLY_ANCHOR_RE.search(body)
    if anchor:
        candidates.append(anchor.group(1))

    for candidate in candidates:
        url = html.unescape((candidate or "").strip())
        if not url.startswith("http"):
            continue
        host = re.sub(r"^https?://", "", url).split("/")[0]
        if AGGREGATORS.search(host):
            continue
        return url
    return ""
