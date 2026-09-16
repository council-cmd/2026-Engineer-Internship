"""Read the jobright README and turn its markdown table into clean records.

Deliberately strict: if the table's shape stops matching what we expect we
raise FormatError so the caller can email a failure notice instead of
quietly reporting "nothing new today" forever.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TABLE_START = "TABLE_START"
TABLE_END = "TABLE_END"

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
JOB_ID_RE = re.compile(r"jobright\.ai/jobs/info/([A-Za-z0-9]+)")
CONTINUATION = "↳"  # the "same company as above" arrow

# Minimum rows we expect between the markers. The table has held exactly 100
# for its whole history; far fewer means something changed upstream.
MIN_EXPECTED_ROWS = 10
# If more than this share of rows fail to parse, treat the format as broken.
MAX_BAD_ROW_RATIO = 0.30

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU",
}

CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}

US_NAMES = {
    "united states", "united states of america", "usa", "us", "u.s.", "u.s.a.",
    "america", "u.s. of a.",
}
CA_NAMES = {"canada", "ca", "can"}

# Full state / province names, since the feed mixes "NC" and "North Carolina".
FULL_REGION_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "puerto rico": "PR",
    # Canadian provinces, so they are never mistaken for US states
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "northwest territories": "NT", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "qu\u00e9bec": "QC", "saskatchewan": "SK", "yukon": "YT",
}

# Metro-area phrases that imply a state without naming one.
METRO_AREAS = {
    "greater chicago area": ("Chicago", "IL"),
    "chicago metropolitan area": ("Chicago", "IL"),
    "greater philadelphia": ("Philadelphia", "PA"),
    "philadelphia metropolitan area": ("Philadelphia", "PA"),
    "denver metropolitan area": ("Denver", "CO"),
    "detroit metropolitan area": ("Detroit", "MI"),
    "greater richmond region": ("Richmond", "VA"),
    "honolulu metropolitan area": ("Honolulu", "HI"),
    "greater houston": ("Houston", "TX"),
    "greater boston": ("Boston", "MA"),
    "greater seattle area": ("Seattle", "WA"),
    "greater minneapolis-st. paul area": ("Minneapolis", "MN"),
    "san francisco bay area": ("San Francisco", "CA"),
    "southern california": ("", "CA"),
    "northern california": ("", "CA"),
    "manhattan": ("New York", "NY"),
    "brooklyn": ("New York", "NY"),
    "queens": ("New York", "NY"),
    "greater new york city area": ("New York", "NY"),
}

# Address noise that should never be mistaken for a city name.
_JUNK_RE = re.compile(
    r"^(suite|ste\.?|unit|floor|fl\.?|bldg|building|apt|apartment|po box|p\.o\.)\b"
    r"|^[\d\s\-#.]+$",
    re.IGNORECASE,
)

REGIONS = {
    "Southeast": {"FL", "GA", "AL", "MS", "TN", "SC", "NC", "KY", "VA", "WV", "AR", "LA", "PR", "VI"},
    "Northeast": {"NY", "NJ", "PA", "CT", "RI", "MA", "VT", "NH", "ME", "MD", "DE", "DC"},
    "Midwest": {"OH", "MI", "IN", "IL", "WI", "MN", "IA", "MO", "KS", "NE", "SD", "ND"},
    "Southwest": {"TX", "OK", "NM", "AZ"},
    "West": {"CA", "NV", "UT", "CO", "WY", "MT", "ID", "OR", "WA", "AK", "HI", "GU"},
}


class FormatError(RuntimeError):
    """The README no longer looks the way this tool expects."""


@dataclass
class Job:
    job_id: str
    company: str
    company_url: str
    title: str
    url: str
    location_raw: str
    city: str = ""
    state: str = ""
    country: str = "US"
    area_group: str = "Unknown"
    work_model: str = ""
    date_posted: date | None = None
    # filled in later by the filtering / enrichment stages
    description: str = ""
    employer_url: str = ""
    visa_bucket: str = ""
    visa_reason: str = ""
    fit_score: int = 0
    extras: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Same company + title + place = same job, whatever the link says."""
        parts = [
            _squash(self.company),
            _squash(self.title),
            _squash(f"{self.city}|{self.state}|{self.country}"),
        ]
        return hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()[:16]


def _squash(text: str) -> str:
    """Lowercase and strip anything that is not a letter or digit."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def extract_table(markdown: str, min_rows: int = MIN_EXPECTED_ROWS) -> list[str]:
    """Return the raw table row lines that sit between the two marker lines."""
    start = markdown.find(TABLE_START)
    end = markdown.find(TABLE_END)
    if start == -1 or end == -1:
        raise FormatError(
            "Could not find the TABLE_START / TABLE_END markers in the README. "
            "The upstream file layout has changed."
        )
    if end <= start:
        raise FormatError("TABLE_END appears before TABLE_START in the README.")

    block = markdown[start:end]
    rows = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("|") and "jobright.ai/jobs/info/" in line
    ]
    if len(rows) < min_rows:
        raise FormatError(
            f"Only {len(rows)} job rows found between the markers "
            f"(expected at least {min_rows}). The table format may have changed."
        )
    return rows


def _split_row(line: str) -> list[str] | None:
    """Split one markdown table row into its five cells.

    Company names and job titles can contain a '|' character of their own
    (e.g. "Veolia | North America"), which would break a naive split. So we
    first hide every markdown link behind a placeholder, split the simplified
    line, then put the links back.
    """
    stash: list[str] = []

    def hide(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00{len(stash) - 1}\x00"

    masked = LINK_RE.sub(hide, line)

    def restore(text: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)

    parts = masked.split("|")
    # A well-formed row is: '' | company | title | location | model | date | ''
    if len(parts) < 7:
        return None
    company = restore(parts[1]).strip()
    date_cell = restore(parts[-2]).strip()
    model = restore(parts[-3]).strip()
    location = restore(parts[-4]).strip()
    title = restore("|".join(parts[2:-4])).strip()
    if not title:
        return None
    return [company, title, location, model, date_cell]


def resolve_year(month_day: str, today: date) -> date | None:
    """Turn 'Sep 16' into a real date, handling the New Year rollover.

    Dates in the README carry no year. We pick the most recent year for which
    the date is not in the future, so on 2 January a 'Dec 28' row correctly
    resolves to the previous year.
    """
    month_day = month_day.strip()
    if not month_day:
        return None
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            parsed = datetime.strptime(f"{month_day} {year}", "%b %d %Y").date()
        except ValueError:
            continue
        # Allow a few days of slack for time-zone differences between the
        # upstream publisher and our runner.
        if parsed <= today + timedelta(days=3):
            candidates.append(parsed)
    return max(candidates) if candidates else None


def normalize_location(raw: str) -> tuple[str, str, str, str]:
    """Return (city, state, country, area_group) from messy location text.

    The upstream data uses at least a dozen shapes, so rather than trusting
    field positions we look for a recognisable state or province anywhere in
    the string. Examples that all have to work:

        "Jacksonville, FL, United States"        -> Jacksonville / FL / US
        "Raleigh, North Carolina, United States" -> Raleigh / NC / US
        "Norcross, GA 30092, United States"      -> Norcross / GA / US
        "1 Main St, Suite 300, Atlanta, GA, US"  -> Atlanta / GA / US
        "El Paso, Texas"                         -> El Paso / TX / US
        "US-FL-Jacksonville"                     -> Jacksonville / FL / US
        "CA-BC-Victoria"                         -> Victoria / BC / CANADA
        "Toronto, ON, Canada"                    -> Toronto / ON / CANADA

    The dashed form is the dangerous one: there the FIRST field is the
    country, so "CA-BC-..." is Canada/British Columbia and must never be
    read as California.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", "", "", "Unknown"

    low = raw.lower()
    if "remote" in low:
        country = "CA" if "canada" in low else "US"
        return "Remote", "", country, "Remote"

    # --- dashed form: COUNTRY-STATE-CITY -----------------------------------
    dashed = re.match(r"^([A-Za-z]{2})-([A-Za-z]{2})-(.+)$", raw)
    if dashed:
        country_code = dashed.group(1).upper()
        region = dashed.group(2).upper()
        city = dashed.group(3).strip()
        if country_code == "US":
            country = "US"
        elif country_code == "CA":
            country = "CA"
        else:
            country = country_code
        return city, region, country, _region_for(region, country)

    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return "", "", "", "Unknown"

    # --- named metro areas, which imply a state without printing one -------
    for phrase, (metro_city, metro_state) in METRO_AREAS.items():
        if phrase in low:
            return metro_city or tokens[0], metro_state, "US", _region_for(metro_state, "US")

    # --- country, taken from an explicit country word ----------------------
    country = ""
    country_idx = None
    for idx, token in enumerate(tokens):
        key = token.lower().strip(".")
        if key in CA_NAMES:
            country, country_idx = "CA", idx
        elif key in US_NAMES:
            country, country_idx = "US", idx

    searchable = [t for i, t in enumerate(tokens) if i != country_idx]

    # --- state / province, searched from the most specific end backwards ---
    state = ""
    state_idx = None
    for idx in range(len(searchable) - 1, -1, -1):
        token = searchable[idx]
        head = token.split()[0].upper() if token.split() else ""
        if head in US_STATES or head in CA_PROVINCES:
            state, state_idx = head, idx
            break
        full = FULL_REGION_NAMES.get(token.lower().strip("."))
        if full:
            state, state_idx = full, idx
            break

    if not country:
        country = "CA" if state in CA_PROVINCES and state not in US_STATES else "US"
    elif state in CA_PROVINCES and state not in US_STATES:
        country = "CA"

    # --- city: the nearest meaningful token before the state ---------------
    def usable(token: str) -> bool:
        key = token.lower()
        if _JUNK_RE.match(key):
            return False
        return bool(re.search(r"[a-z]", key))

    city = ""
    if state_idx is not None:
        for idx in range(state_idx - 1, -1, -1):
            if usable(searchable[idx]):
                city = searchable[idx]
                break
    if not city:
        for token in searchable:
            if usable(token) and token.upper() != state:
                key = token.lower().strip(".")
                if key in FULL_REGION_NAMES or key in US_NAMES or key in CA_NAMES:
                    continue
                city = token
                break

    return city, state, country, _region_for(state, country)


def _region_for(state: str, country: str) -> str:
    if country != "US":
        return "Non-US"
    for name, members in REGIONS.items():
        if state in members:
            return name
    return "Unknown"


def parse(
    markdown: str, today: date | None = None, min_rows: int = MIN_EXPECTED_ROWS
) -> tuple[list[Job], int]:
    """Parse the whole README. Returns (jobs, number_of_unparseable_rows)."""
    today = today or date.today()
    rows = extract_table(markdown, min_rows=min_rows)

    jobs: list[Job] = []
    bad_rows = 0
    last_company = ""
    last_company_url = ""

    for line in rows:
        cells = _split_row(line)
        if cells is None:
            bad_rows += 1
            continue
        company_cell, title_cell, location_cell, model_cell, date_cell = cells

        # --- company, resolving the "same as above" arrow ------------------
        company_link = LINK_RE.search(company_cell)
        if company_link:
            last_company = company_link.group(1).strip()
            last_company_url = company_link.group(2).strip()
        elif CONTINUATION in company_cell:
            pass  # keep the previous company
        else:
            stripped = company_cell.replace("*", "").strip()
            if stripped and stripped != CONTINUATION:
                last_company = stripped
                last_company_url = ""

        # --- title and link ------------------------------------------------
        title_link = LINK_RE.search(title_cell)
        if not title_link:
            bad_rows += 1
            continue
        title = title_link.group(1).strip()
        url = title_link.group(2).strip()

        id_match = JOB_ID_RE.search(url)
        if not id_match:
            bad_rows += 1
            continue

        if not last_company:
            bad_rows += 1
            continue

        city, state, country, area = normalize_location(location_cell)

        jobs.append(
            Job(
                job_id=id_match.group(1),
                company=last_company,
                company_url=last_company_url,
                title=title,
                url=url.split("?")[0],
                location_raw=location_cell,
                city=city,
                state=state,
                country=country,
                area_group=area,
                work_model=model_cell.replace("*", "").strip(),
                date_posted=resolve_year(date_cell, today),
            )
        )

    total = len(rows)
    if total and bad_rows / total > MAX_BAD_ROW_RATIO:
        raise FormatError(
            f"{bad_rows} of {total} table rows could not be parsed. "
            "The README column layout has probably changed."
        )
    return jobs, bad_rows
