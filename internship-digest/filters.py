"""Decide which jobs to keep, what visa bucket they fall in, and how well
they fit. All the word lists live in config.yaml so they can be edited
without touching code.
"""

from __future__ import annotations

import re
from datetime import date

# Buckets, using the labels from the project brief.
LIKELY_OK = "A - Likely OK"
UNCLEAR = "B - Unclear"
EXCLUDED = "C - Excluded"


def normalize_text(text: str) -> str:
    """Flatten text so phrase matching is not defeated by punctuation.

    "U.S. Citizenship Required!" and "us citizenship required" both end up
    as the same string. Hyphens become spaces on both sides of every
    comparison, so "export-controlled" and "export controlled" match each
    other, and "h-1b" still matches "H-1B". Slashes survive for "ts/sci".
    """
    low = (text or "").lower()
    low = low.replace("u.s.a.", "usa").replace("u. s.", "us").replace("u.s.", "us")
    low = low.replace("’", "'").replace("–", "-").replace("—", "-")
    low = low.replace("-", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/&' ]+", " ", low)).strip()


def _phrase_re(phrase: str, stem: bool = False) -> re.Pattern:
    """Build a matcher for one phrase from config.yaml.

    Two different behaviours are needed:

    stem=True (title and company words) matches from the start of a word
    onwards, so the short stems in the config do what they look like they do:
    "manufactur" catches manufacturing/manufacture, "biolog" catches
    biology/biological, "tunnel" catches tunnels.

    stem=False (visa phrases) matches whole words, with an optional plural,
    so "us person" catches "US Persons" but never "us personally" - which
    would wrongly exclude a job you are eligible for.
    """
    body = re.escape(normalize_text(phrase))
    if not body:
        return re.compile(r"(?!)")  # matches nothing
    prefix = r"\b" if body[:1].isalnum() else ""
    if stem:
        return re.compile(prefix + body)
    # Tolerate ordinary word endings: "export control" also catches
    # "export controlled", and "us person" catches "US Persons" - while
    # still refusing to match "us personally".
    suffix = r"(?:s|es|ed|ing)?\b" if body[-1:].isalnum() else ""
    return re.compile(prefix + body + suffix)


class Rules:
    """Compiled version of the word lists in config.yaml."""

    def __init__(self, config: dict):
        t = config["titles"]
        self.keep_strong = [_phrase_re(x, stem=True) for x in t.get("keep_strong", [])]
        self.keep_moderate = [_phrase_re(x, stem=True) for x in t.get("keep_moderate", [])]
        self.drop = [_phrase_re(x, stem=True) for x in t.get("drop", [])]
        self.drop_always = [_phrase_re(x, stem=True) for x in t.get("drop_always", [])]

        v = config["visa"]
        self.exclude_phrases = [(x, _phrase_re(x)) for x in v.get("exclude_phrases", [])]
        self.positive_phrases = [(x, _phrase_re(x)) for x in v.get("positive_phrases", [])]
        self.exclude_companies = [(x, _phrase_re(x, stem=True)) for x in v.get("exclude_companies", [])]
        self.include_unclear = bool(v.get("include_unclear", True))

        self.us_only = bool(config["location"].get("us_only", True))
        self.preferred_states = {
            s.strip().upper() for s in config["location"].get("preferred_states", []) or []
        }
        self.scoring = config["scoring"]


def title_verdict(title: str, rules: Rules) -> tuple[bool, str]:
    """Return (keep?, strength) where strength is strong / moderate / no."""
    text = normalize_text(title)

    for pattern in rules.drop_always:
        if pattern.search(text):
            return False, "no"

    # A strong mechanical term overrides a competing discipline only when it
    # appears in the role name itself - the part before the first dash,
    # comma or bracket. Otherwise "Structural Engineering Intern - Global
    # Facilities, Aerospace & Industrial" would be kept on the strength of
    # "Aerospace", which there is a business unit, not the job.
    head = re.split(r"\s[-–—]\s|,|\(|\||/{2}", title or "", maxsplit=1)[0]
    head_text = normalize_text(head)

    strong_in_head = any(p.search(head_text) for p in rules.keep_strong)
    if strong_in_head:
        return True, "strong"

    for pattern in rules.drop:
        if pattern.search(text):
            return False, "no"

    # No competing discipline named, so a strong term anywhere still counts.
    if any(p.search(text) for p in rules.keep_strong):
        return True, "strong"

    if any(p.search(text) for p in rules.keep_moderate):
        # A bare "Engineering Intern" with no discipline named is worth less
        # than something like "Quality Engineer Intern".
        generic = re.fullmatch(r"(19|20)?\d{0,4}\s*engineer(ing)? intern(ship)?\s*.{0,25}", text)
        return True, "generic" if generic else "moderate"

    return False, "no"


def classify_visa(job, rules: Rules) -> tuple[str, str]:
    """Return (bucket, human-readable reason)."""
    if rules.us_only and job.country != "US":
        return EXCLUDED, f"Outside the US ({job.country}) - CPT/OPT do not apply"

    company_text = normalize_text(job.company)
    for raw, pattern in rules.exclude_companies:
        if pattern.search(company_text):
            return EXCLUDED, f"Employer matches '{raw}' - these roles are normally US-person only"

    haystack = normalize_text(f"{job.title} {job.description}")
    for raw, pattern in rules.exclude_phrases:
        if pattern.search(haystack):
            where = "title" if pattern.search(normalize_text(job.title)) else "description"
            return EXCLUDED, f"{where.capitalize()} contains '{raw}'"

    if job.description:
        for raw, pattern in rules.positive_phrases:
            if pattern.search(haystack):
                return LIKELY_OK, f"Description mentions '{raw}'"
        return LIKELY_OK, "Description read; nothing rules out international students"

    return UNCLEAR, "No job description available - visa status not stated"


def score(job, rules: Rules, strength: str, today: date | None = None) -> int:
    """A 0-100 fit score used only to sort your email, highest first."""
    s = rules.scoring
    total = 0

    total += {
        "strong": s.get("strong_title", 40),
        "moderate": s.get("moderate_title", 26),
        "generic": s.get("generic_title", 18),
    }.get(strength, 0)

    if job.visa_bucket == LIKELY_OK:
        total += s.get("visa_likely_ok", 30)
    elif job.visa_bucket == UNCLEAR:
        total += s.get("visa_unclear", 12)

    if job.state and job.state in rules.preferred_states:
        total += s.get("preferred_state", 10)

    model = (job.work_model or "").lower()
    if "remote" in model:
        total += s.get("remote", 10)
    elif "hybrid" in model:
        total += s.get("hybrid", 8)
    elif "site" in model:
        total += s.get("onsite", 4)

    body = normalize_text(job.description)
    if re.search(r"\bcpt\b|\bopt\b|practical training", body):
        total += s.get("mentions_cpt_opt", 12)

    if today and job.date_posted == today:
        total += s.get("posted_today", 5)

    return max(0, min(100, total))


# Words that mark a note some copies of a posting carry and others omit,
# such as "(US Person Required)".
_QUALIFIER_WORDS = (
    r"us persons?|u s persons?|citizens?|citizenship|clearance|itar|"
    r"export[ -]control\w*|sponsorship|green card|onsite|on site|remote|hybrid"
)

# Only the bracketed note itself is removed - not everything that precedes
# it - so "Intern - Summer 2027 (US Person Required)" and
# "Intern - Summer 2027" reduce to the same text.
_BRACKETED_QUALIFIER = re.compile(
    rf"[\(\[][^\)\]]*\b(?:{_QUALIFIER_WORDS})\b[^\)\]]*[\)\]]?", re.IGNORECASE
)

# The same note written without brackets, at the end of the title.
_TRAILING_QUALIFIER = re.compile(
    rf"[\-–—,]\s*[^\-–—,\(\)\[\]]*\b(?:{_QUALIFIER_WORDS})\b"
    r"[^\-–—,\(\)\[\]]*$",
    re.IGNORECASE,
)


def base_title(title: str) -> str:
    """Strip such notes so near-identical repostings group together.

    "Manufacturing Intern (US Person Required)" and "Manufacturing Intern"
    reduce to the same thing, while "Manufacturing Intern - Summer 2027"
    stays distinct from "Manufacturing Intern - Summer 2028".
    """
    stripped = _BRACKETED_QUALIFIER.sub(" ", title or "")
    stripped = _TRAILING_QUALIFIER.sub(" ", stripped)
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def apply_sibling_exclusions(jobs: list) -> int:
    """Carry a visa exclusion across duplicate postings of the same role.

    Employers routinely post one job several times, and only some copies
    mention "(US Person Required)". Judging each copy alone would show you
    the copy that left the restriction out - the worst possible outcome,
    since it looks open and is not. When any copy of a role is excluded,
    every copy is.

    Returns how many jobs this newly excluded.
    """
    groups: dict[tuple, list] = {}
    for job in jobs:
        key = (
            re.sub(r"[^a-z0-9]+", "", job.company.lower()),
            job.city.lower(),
            job.state,
            base_title(job.title),
        )
        groups.setdefault(key, []).append(job)

    newly_excluded = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        culprit = next((m for m in members if m.visa_bucket == EXCLUDED), None)
        if culprit is None:
            continue
        for job in members:
            if job.visa_bucket != EXCLUDED:
                job.visa_bucket = EXCLUDED
                job.visa_reason = (
                    "An identical posting from this employer says: "
                    f"{culprit.visa_reason}"
                )
                newly_excluded += 1
    return newly_excluded
