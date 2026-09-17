#!/usr/bin/env python3
"""Tests for the internship digest.

Run them with:   python3 test_digest.py
Everything should say OK. If something says FAIL, the tool has a problem and
you should not trust that day's email until it is fixed.
"""

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

import filters
import jobparser as jp
import mailer
import store

CONFIG = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
RULES = filters.Rules(CONFIG)


def make_readme(rows):
    header = (
        "TABLE_START\n\n"
        "| Company | Job Title | Location | Work Model | Date Posted |\n"
        "| --- | --- | --- | --- | --- |\n"
    )
    return header + "\n".join(rows) + "\nTABLE_END"


def row(company, title, location, model="On Site", posted="Sep 16", job_id="a" * 24):
    company_cell = "↳" if company is None else f"**[{company}](http://example.com)**"
    return (
        f"| {company_cell} | **[{title}](https://jobright.ai/jobs/info/{job_id}"
        f"?utm_campaign=1048)** | {location} | {model} | {posted} |"
    )


class TestTableParsing(unittest.TestCase):
    def test_reads_a_basic_row(self):
        jobs, bad = jp.parse(make_readme([row("Acme", "Mechanical Intern", "Tampa, FL, United States")]), min_rows=1)
        self.assertEqual(bad, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].title, "Mechanical Intern")
        self.assertEqual(jobs[0].work_model, "On Site")

    def test_arrow_inherits_the_company_above(self):
        jobs, _ = jp.parse(make_readme([
            row("Garver", "Intern A", "Austin, TX, United States", job_id="b" * 24),
            row(None, "Intern B", "Frisco, TX, United States", job_id="c" * 24),
            row(None, "Intern C", "Dallas, TX, United States", job_id="d" * 24),
        ]), min_rows=1)
        self.assertEqual([j.company for j in jobs], ["Garver"] * 3)

    def test_pipe_inside_a_company_name(self):
        """'Veolia | North America' must not break the column split."""
        line = (
            "| **[Veolia | North America](http://v.com)** | "
            "**[R&D Intern](https://jobright.ai/jobs/info/" + "e" * 24 + ")** | "
            "Plainfield, IN, United States | On Site | Sep 15 |"
        )
        jobs, bad = jp.parse(make_readme([line]), min_rows=1)
        self.assertEqual(bad, 0)
        self.assertEqual(jobs[0].company, "Veolia | North America")
        self.assertEqual(jobs[0].title, "R&D Intern")

    def test_missing_markers_is_an_error(self):
        with self.assertRaises(jp.FormatError):
            jp.parse("| Company | Job Title |\n| Acme | Intern |")

    def test_too_few_rows_is_an_error(self):
        """A near-empty table means the format changed; we must not call it
        'nothing new today'."""
        with self.assertRaises(jp.FormatError):
            jp.parse("TABLE_START\n\n| a | b |\n\nTABLE_END")
        # one row where a hundred are expected also means something changed
        with self.assertRaises(jp.FormatError):
            jp.parse(make_readme([row("Acme", "Intern", "Tampa, FL, US")]))


class TestLocations(unittest.TestCase):
    def test_standard_us(self):
        self.assertEqual(jp.normalize_location("Jacksonville, FL, United States"),
                         ("Jacksonville", "FL", "US", "Southeast"))

    def test_dashed_us(self):
        self.assertEqual(jp.normalize_location("US-FL-Jacksonville"),
                         ("Jacksonville", "FL", "US", "Southeast"))

    def test_dashed_ca_is_canada_not_california(self):
        """The trap: CA-BC-Victoria is British Columbia, Canada."""
        city, state, country, area = jp.normalize_location("CA-BC-Victoria")
        self.assertEqual((city, state, country), ("Victoria", "BC", "CA"))
        self.assertEqual(area, "Non-US")

    def test_ontario_california_is_still_california(self):
        """...but 'Ontario, CA, United States' is a city in California."""
        self.assertEqual(jp.normalize_location("Ontario, CA, United States"),
                         ("Ontario", "CA", "US", "West"))

    def test_full_state_name(self):
        self.assertEqual(jp.normalize_location("Raleigh, North Carolina, United States"),
                         ("Raleigh", "NC", "US", "Southeast"))

    def test_zip_code_in_the_state_field(self):
        self.assertEqual(jp.normalize_location("Norcross, GA 30092, United States"),
                         ("Norcross", "GA", "US", "Southeast"))

    def test_street_address_is_not_used_as_the_city(self):
        self.assertEqual(jp.normalize_location("1 Main St, Suite 300, Atlanta, GA, United States"),
                         ("Atlanta", "GA", "US", "Southeast"))

    def test_metro_area(self):
        self.assertEqual(jp.normalize_location("Greater Chicago Area, United States"),
                         ("Chicago", "IL", "US", "Midwest"))

    def test_canada_named_in_full(self):
        self.assertEqual(jp.normalize_location("Toronto, ON, Canada")[2], "CA")

    def test_remote(self):
        self.assertEqual(jp.normalize_location("Remote, United States")[3], "Remote")


class TestDates(unittest.TestCase):
    def test_current_year(self):
        self.assertEqual(jp.resolve_year("Sep 16", date(2026, 9, 16)), date(2026, 9, 16))

    def test_new_year_rollover(self):
        """On 2 January, 'Dec 28' belongs to the year before."""
        self.assertEqual(jp.resolve_year("Dec 28", date(2027, 1, 2)), date(2026, 12, 28))

    def test_leap_day(self):
        self.assertEqual(jp.resolve_year("Feb 29", date(2028, 3, 1)), date(2028, 2, 29))

    def test_nonsense_returns_nothing(self):
        self.assertIsNone(jp.resolve_year("banana", date(2026, 9, 16)))


class TestDeduplication(unittest.TestCase):
    def test_same_job_different_links_collapses(self):
        jobs, _ = jp.parse(make_readme([
            row("Acme", "Mechanical Intern", "Tampa, FL, United States", job_id="1" * 24),
            row("Acme", "Mechanical Intern", "Tampa, FL, US", job_id="2" * 24),
            row("Acme", "Mechanical  Intern", "US-FL-Tampa", job_id="3" * 24),
        ]), min_rows=1)
        self.assertEqual(len({j.dedupe_key for j in jobs}), 1)

    def test_different_cities_stay_separate(self):
        jobs, _ = jp.parse(make_readme([
            row("Acme", "Mechanical Intern", "Tampa, FL, United States", job_id="1" * 24),
            row("Acme", "Mechanical Intern", "Miami, FL, United States", job_id="2" * 24),
        ]), min_rows=1)
        self.assertEqual(len({j.dedupe_key for j in jobs}), 2)


class TestTitleFilter(unittest.TestCase):
    def test_keeps_mechanical(self):
        for title in ["Mechanical Engineering Intern", "Manufacturing Engineer Co-Op",
                      "HVAC Design Intern", "Robotics Intern", "Thermal Engineer Intern",
                      "R&D Engineering Intern", "Mechatronics Intern"]:
            self.assertTrue(filters.title_verdict(title, RULES)[0], title)

    def test_drops_other_disciplines(self):
        for title in ["Software Engineering Intern", "Civil Engineering Intern",
                      "Cybersecurity Intern", "Data Science Intern",
                      "Structural Engineering Intern", "Construction Intern",
                      "Research Nurse", "Biology Research Intern",
                      "Tunnels Engineering Internship", "Field Engineer Intern"]:
            self.assertFalse(filters.title_verdict(title, RULES)[0], title)

    def test_keeps_multi_discipline_when_mechanical_is_named(self):
        self.assertTrue(filters.title_verdict("Mechanical/Electrical Engineering Intern", RULES)[0])

    def test_generic_engineering_intern_is_kept_but_ranked_lower(self):
        self.assertEqual(filters.title_verdict("Engineering Intern", RULES), (True, "generic"))

    def test_always_dropped(self):
        for title in ["High School Intern - Engineering", "PhD Research Intern",
                      "Military SkillBridge Internship - Engineering"]:
            self.assertFalse(filters.title_verdict(title, RULES)[0], title)


class _FakeJob:
    def __init__(self, title="Mechanical Intern", company="Acme", description="", country="US",
                 state="FL", city="Tampa", work_model="On Site", date_posted=None):
        self.title, self.company, self.description = title, company, description
        self.country, self.state, self.city = country, state, city
        self.work_model, self.date_posted = work_model, date_posted
        self.visa_bucket = ""
        self.url = "https://jobright.ai/jobs/info/x"
        self.fit_score = 0
        self.location_raw = ""


class TestVisa(unittest.TestCase):
    def test_citizenship_phrases_exclude(self):
        for text in ["Applicants must be a U.S. Citizen.", "US citizens only.",
                     "Requires an active security clearance.", "Subject to ITAR.",
                     "This role is export controlled.", "We do not provide sponsorship."]:
            bucket, _ = filters.classify_visa(_FakeJob(description=text), RULES)
            self.assertEqual(bucket, filters.EXCLUDED, text)

    def test_us_persons_plural_is_caught(self):
        bucket, _ = filters.classify_visa(_FakeJob(description="Open to US Persons only."), RULES)
        self.assertEqual(bucket, filters.EXCLUDED)

    def test_similar_wording_does_not_falsely_exclude(self):
        """'us personally' must not be read as 'US person'."""
        for text in ["Come meet us personally at the career fair.",
                     "Optional benefits are available.",
                     "We adopt a hands-on approach."]:
            bucket, _ = filters.classify_visa(_FakeJob(description=text), RULES)
            self.assertEqual(bucket, filters.LIKELY_OK, text)

    def test_defense_employer_excluded_without_a_description(self):
        for company in ["Northrop Grumman", "RTX", "L3Harris Technologies",
                        "Sandia National Laboratories", "Anduril Industries"]:
            bucket, _ = filters.classify_visa(_FakeJob(company=company), RULES)
            self.assertEqual(bucket, filters.EXCLUDED, company)

    def test_non_us_excluded(self):
        bucket, _ = filters.classify_visa(_FakeJob(country="CA"), RULES)
        self.assertEqual(bucket, filters.EXCLUDED)

    def test_positive_wording_is_bucket_a(self):
        bucket, reason = filters.classify_visa(
            _FakeJob(description="We welcome CPT and OPT students."), RULES)
        self.assertEqual(bucket, filters.LIKELY_OK)

    def test_no_description_is_unclear_not_excluded(self):
        bucket, _ = filters.classify_visa(_FakeJob(description=""), RULES)
        self.assertEqual(bucket, filters.UNCLEAR)

    def test_title_alone_can_exclude(self):
        bucket, _ = filters.classify_visa(
            _FakeJob(title="Mechanical Engineering Intern (US Person Required)"), RULES)
        self.assertEqual(bucket, filters.EXCLUDED)


class TestScoring(unittest.TestCase):
    def test_preferred_state_beats_other_state(self):
        a = _FakeJob(state="FL"); a.visa_bucket = filters.UNCLEAR
        b = _FakeJob(state="ND"); b.visa_bucket = filters.UNCLEAR
        self.assertGreater(filters.score(a, RULES, "strong"), filters.score(b, RULES, "strong"))

    def test_likely_ok_beats_unclear(self):
        a = _FakeJob(); a.visa_bucket = filters.LIKELY_OK
        b = _FakeJob(); b.visa_bucket = filters.UNCLEAR
        self.assertGreater(filters.score(a, RULES, "strong"), filters.score(b, RULES, "strong"))

    def test_score_stays_in_range(self):
        j = _FakeJob(description="CPT OPT sponsorship", work_model="Remote", state="FL")
        j.visa_bucket = filters.LIKELY_OK
        self.assertLessEqual(filters.score(j, RULES, "strong", today=date.today()), 100)


class TestSpreadsheet(unittest.TestCase):
    def test_your_notes_are_never_overwritten(self):
        """The whole point: typing in the status column must survive a rerun."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.csv"
            sheet = store.Spreadsheet(path)
            job = _FakeJob(); job.job_id = "job1"; job.area_group = "Southeast"
            job.visa_bucket = filters.UNCLEAR; job.visa_reason = "n/a"; job.fit_score = 50
            job.date_posted = date(2026, 9, 16)
            sheet.add(job, date(2026, 9, 16)); sheet.save()

            # the student types a note
            rows = list(csv.DictReader(path.open(encoding="utf-8")))
            rows[0]["application_status"] = "Applied 9/17"
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=store.CSV_COLUMNS)
                writer.writeheader(); writer.writerows(rows)

            # next day's run
            sheet2 = store.Spreadsheet(path)
            job2 = _FakeJob(); job2.job_id = "job2"; job2.area_group = "Southeast"
            job2.visa_bucket = filters.UNCLEAR; job2.visa_reason = "n/a"; job2.fit_score = 40
            job2.date_posted = date(2026, 9, 17)
            sheet2.add(job2, date(2026, 9, 17)); sheet2.save()

            final = list(csv.DictReader(path.open(encoding="utf-8")))
            self.assertEqual(len(final), 2)
            self.assertEqual(final[0]["application_status"], "Applied 9/17")

    def test_the_same_job_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.csv"
            sheet = store.Spreadsheet(path)
            job = _FakeJob(); job.job_id = "dup"; job.area_group = "Southeast"
            job.visa_bucket = filters.UNCLEAR; job.visa_reason = ""; job.fit_score = 1
            job.date_posted = None
            self.assertTrue(sheet.add(job, date(2026, 9, 16)))
            self.assertFalse(sheet.add(job, date(2026, 9, 16)))


class TestSeen(unittest.TestCase):
    def test_a_job_is_only_new_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            seen = store.Seen(path)
            jobs, _ = jp.parse(make_readme([row("Acme", "Mechanical Intern", "Tampa, FL, US")] * 1 + [
                row("Beta", "Design Engineer Intern", "Miami, FL, US", job_id="9" * 24)]), min_rows=1)
            for job in jobs:
                self.assertTrue(seen.is_new(job))
                seen.remember(job, date(2026, 9, 16))
            seen.save()
            reloaded = store.Seen(path)
            for job in jobs:
                self.assertFalse(reloaded.is_new(job))

    def test_corrupt_memory_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text("{ this is not json", encoding="utf-8")
            self.assertEqual(store.Seen(path).ids, {})


class TestEmailGrouping(unittest.TestCase):
    def test_one_role_in_many_cities_becomes_one_entry(self):
        jobs = []
        for city in ["Austin", "Dallas", "Houston", "Waco"]:
            j = _FakeJob(company="BigCo", title="Field Intern", city=city, state="TX")
            j.visa_bucket = filters.UNCLEAR; j.visa_reason = ""; j.fit_score = 50
            jobs.append(j)
        groups = mailer._group(jobs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 4)

    def test_digest_body_mentions_every_shown_job(self):
        j = _FakeJob(title="Mechanical Intern", company="Acme")
        j.visa_bucket = filters.UNCLEAR; j.visa_reason = "no description"; j.fit_score = 60
        html_body, text_body = mailer.build_digest(
            [j], {"date": "2026-09-16", "parsed": 100, "new_total": 5, "warnings": []}, CONFIG)
        self.assertIn("Mechanical Intern", html_body)
        self.assertIn("Acme", text_body)

    def test_warnings_reach_the_reader(self):
        stats = {"date": "2026-09-16", "parsed": 100, "new_total": 0,
                 "warnings": ["Descriptions could not be read."]}
        html_body, text_body = mailer.build_empty(stats)
        self.assertIn("could not be read", html_body)
        self.assertIn("could not be read", text_body)




class TestDuplicatePostingsWithDifferentTitles(unittest.TestCase):
    """Employers repost one role with and without a visa disclaimer.

    Honeywell really did post "Manufacturing & Industrial Engineering -
    Summer 2027 Intern" four times: twice with "(US Person Required)" and
    twice without. Judged alone, the copies without it look open.
    """

    def _pair(self):
        restricted = _FakeJob(
            title="Manufacturing Intern - Summer 2027 (US Person Required)",
            company="Honeywell", city="", state="")
        open_looking = _FakeJob(
            title="Manufacturing Intern - Summer 2027",
            company="Honeywell", city="", state="")
        for job in (restricted, open_looking):
            job.visa_bucket, job.visa_reason = filters.classify_visa(job, RULES)
        return restricted, open_looking

    def test_alone_the_unmarked_copy_looks_fine(self):
        restricted, open_looking = self._pair()
        self.assertEqual(restricted.visa_bucket, filters.EXCLUDED)
        self.assertEqual(open_looking.visa_bucket, filters.UNCLEAR)

    def test_the_restriction_spreads_to_the_twin(self):
        restricted, open_looking = self._pair()
        spread = filters.apply_sibling_exclusions([restricted, open_looking])
        self.assertEqual(spread, 1)
        self.assertEqual(open_looking.visa_bucket, filters.EXCLUDED)
        self.assertIn("identical posting", open_looking.visa_reason)

    def test_unrelated_roles_are_not_dragged_down(self):
        restricted, _ = self._pair()
        other = _FakeJob(title="Thermal Intern", company="Honeywell", city="", state="")
        other.visa_bucket, other.visa_reason = filters.classify_visa(other, RULES)
        filters.apply_sibling_exclusions([restricted, other])
        self.assertEqual(other.visa_bucket, filters.UNCLEAR)

    def test_same_title_at_a_different_employer_is_untouched(self):
        restricted, _ = self._pair()
        elsewhere = _FakeJob(title="Manufacturing Intern - Summer 2027",
                             company="Trane", city="", state="")
        elsewhere.visa_bucket, elsewhere.visa_reason = filters.classify_visa(elsewhere, RULES)
        filters.apply_sibling_exclusions([restricted, elsewhere])
        self.assertEqual(elsewhere.visa_bucket, filters.UNCLEAR)

if __name__ == "__main__":
    unittest.main(verbosity=2)
