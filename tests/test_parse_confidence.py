"""Year inference, weekday cross-check, and the three-tier confidence rules."""

import unittest

from courseics.config import Config, fall_anchor
from courseics.parse import (CERTAIN, KIND_ASSIGNMENT_DUE, KIND_ASSIGNMENT_OUT,
                           KIND_LECTURE, UNCERTAIN, parse_line)

URL = "https://example.invalid/page"


def records(line, kind=KIND_LECTURE, cfg=None):
    return parse_line(line, URL, kind, cfg or Config())


def one(line, kind=KIND_LECTURE, cfg=None):
    rs = records(line, kind, cfg)
    assert len(rs) == 1, "expected 1 record, got %d: %r" % (len(rs), rs)
    return rs[0]


class TestYearSource(unittest.TestCase):
    """Where a year comes from, and what happens when the two sources differ.

    The original spec said the pages carry no years. They do: every left-hand
    date on both ROB-UY 3303 pages is written "Sep. 02, 2026". Only the
    trailing "Due Sep. 13" deadlines are yearless. So the page is the primary
    source and the anchor is strictly a fallback.
    """

    def test_page_year_is_used_when_present(self):
        r = one("Sep. 02, 2026 Intro")
        self.assertEqual(r.date, "2026-09-02")
        self.assertTrue(any("taken from the page" in x for x in r.reasons),
                        r.reasons)
        self.assertFalse(any("semester anchor" in x for x in r.reasons),
                         r.reasons)

    def test_page_year_beats_the_anchor_even_far_from_it(self):
        # The anchor would say 2031 for a February date; the page says 2029.
        cfg = Config()
        cfg.anchor = fall_anchor(2030)
        self.assertEqual(one("Feb. 03, 2029 Something", cfg=cfg).date,
                         "2029-02-03")

    def test_anchor_is_only_a_fallback(self):
        r = one("Due Sep. 13", KIND_LECTURE)
        self.assertEqual(r.date, "2026-09-13")
        self.assertTrue(any("year absent from the page" in x
                            for x in r.reasons), r.reasons)
        self.assertTrue(any("semester anchor" in x for x in r.reasons),
                        r.reasons)

    def test_conflict_keeps_the_page_year_but_downgrades(self):
        # Anchor fall-2026 puts September in 2026; the page says 2027.
        r = one("Sep. 02, 2027 Intro")
        self.assertEqual(r.date, "2027-09-02")
        self.assertEqual(r.confidence, UNCERTAIN)
        joined = " | ".join(r.reasons)
        self.assertIn("year conflict", joined)
        self.assertIn("2027", joined)
        self.assertIn("2026", joined)
        self.assertIn("keeping the page's year", joined)

    def test_agreement_stays_certain_and_says_nothing_about_a_conflict(self):
        r = one("Sep. 02, 2026 Intro")
        self.assertEqual(r.confidence, CERTAIN)
        self.assertFalse(any("conflict" in x for x in r.reasons), r.reasons)

    def test_page_year_outside_the_anchor_is_not_a_conflict(self):
        # July is not in the fall anchor at all, so there is no second opinion
        # to disagree with -- the page's year simply stands.
        r = one("Jul. 04, 2026 Summer session")
        self.assertEqual(r.date, "2026-07-04")
        self.assertEqual(r.confidence, CERTAIN)


class TestTagInjectedWhitespace(unittest.TestCase):
    """Google Sites splits a date across <span>s; the spacing that survives is
    arbitrary. Every one of these is the same logical date."""

    FORMS = [
        "Sep. 02, 2026",
        "Sep . 02, 2026",
        "Sep . 02 , 2026",
        "Sep .02,2026",
        "Sep .   02  ,  2026",
        "Sep.02, 2026",
        "September . 02 , 2026",
    ]

    def test_all_spacings_parse_to_the_same_date(self):
        for form in self.FORMS:
            r = one("%s Introduction" % form)
            self.assertEqual(r.date, "2026-09-02", form)
            self.assertEqual(r.title, "Introduction", form)
            self.assertEqual(r.confidence, CERTAIN, form)

    def test_odd_spacing_on_a_yearless_due_date_still_falls_back(self):
        rs = records("Sep . 09 , 2026 Assignment 1 Due Oct .  04",
                     kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(len(rs), 2)
        self.assertEqual([r.date for r in rs], ["2026-09-09", "2026-10-04"])
        self.assertEqual(rs[1].kind, KIND_ASSIGNMENT_DUE)

    def test_odd_spacing_does_not_defeat_the_conflict_check(self):
        r = one("Oct . 05 , 2027 Sampling Based Algorithms II")
        self.assertEqual(r.date, "2027-10-05")
        self.assertEqual(r.confidence, UNCERTAIN)
        self.assertTrue(any("year conflict" in x for x in r.reasons),
                        r.reasons)

    def test_spacing_tolerance_does_not_resurrect_a_trap(self):
        # Loosening the separator must not let the false-positive guards slip.
        self.assertEqual(records("Read Chapter . 17 before class"), [])
        self.assertEqual(records("Sep 2024"), [])


class TestYearAnchor(unittest.TestCase):
    def test_explicit_year_wins(self):
        self.assertEqual(one("Sep. 02, 2026 Intro").date, "2026-09-02")

    def test_fall_month_without_year_uses_anchor_year(self):
        r = one("Due Sep. 13", KIND_LECTURE)
        self.assertEqual(r.date, "2026-09-13")
        self.assertTrue(any("semester anchor" in x for x in r.reasons))

    def test_spring_month_without_year_rolls_over(self):
        self.assertEqual(one("Jan. 20 Spring kickoff").date, "2027-01-20")
        self.assertEqual(one("May 04 Finals").date, "2027-05-04")

    def test_month_outside_anchor_is_dropped_not_guessed(self):
        self.assertEqual(records("Jul. 04 Independence Day"), [])

    def test_anchor_is_configurable(self):
        cfg = Config()
        cfg.anchor = fall_anchor(2030)
        self.assertEqual(one("Sep. 13 Something", cfg=cfg).date, "2030-09-13")
        self.assertEqual(one("Feb. 03 Something", cfg=cfg).date, "2031-02-03")

    def test_anchor_never_depends_on_todays_date(self):
        a = fall_anchor(2026)
        self.assertEqual(a.year_for_month(9), 2026)
        self.assertEqual(a.year_for_month(12), 2026)
        self.assertEqual(a.year_for_month(1), 2027)
        with self.assertRaises(ValueError):
            a.year_for_month(7)


class TestWeekdayConsistency(unittest.TestCase):
    def test_matching_weekday_stays_certain(self):
        # 2026-09-02 really is a Wednesday.
        r = one("Wednesday Sep. 02, 2026 Introduction")
        self.assertEqual(r.confidence, CERTAIN)

    def test_conflicting_weekday_downgrades_and_explains(self):
        r = one("Monday Sep. 02, 2026 Introduction")
        self.assertEqual(r.confidence, UNCERTAIN)
        self.assertTrue(any("weekday conflict" in x for x in r.reasons),
                        r.reasons)
        self.assertTrue(any("Wednesday" in x for x in r.reasons), r.reasons)

    def test_abbreviated_weekday_also_checked(self):
        self.assertEqual(one("Wed Sep. 02, 2026 Intro").confidence, CERTAIN)
        self.assertEqual(one("Fri Sep. 02, 2026 Intro").confidence, UNCERTAIN)

    def test_no_weekday_present_is_not_a_downgrade(self):
        self.assertEqual(one("Sep. 02, 2026 Intro").confidence, CERTAIN)


class TestNegationAndCorrection(unittest.TestCase):
    def _uncertain(self, line):
        r = one(line)
        self.assertEqual(r.confidence, UNCERTAIN, line)
        return r

    def test_not(self):
        self._uncertain("Sep. 02, 2026 Intro (this is not the exam)")

    def test_instead_of(self):
        self._uncertain("Sep. 02, 2026 Intro instead of the lab")

    def test_moved_to(self):
        self._uncertain("Sep. 02, 2026 Intro moved to a later slot")

    def test_rescheduled(self):
        self._uncertain("Sep. 02, 2026 Intro rescheduled")

    def test_cancelled_both_spellings(self):
        self._uncertain("Sep. 02, 2026 Intro cancelled")
        self._uncertain("Sep. 02, 2026 Intro canceled")

    def test_no_class(self):
        r = self._uncertain("Sep. 07, 2026 Labor Day - no class")
        self.assertTrue(any("no class" in x for x in r.reasons), r.reasons)

    def test_tbd(self):
        self._uncertain("Sep. 02, 2026 Guest lecture TBD")

    def test_relative_expression(self):
        r = self._uncertain("Sep. 02, 2026 Intro, details next week")
        self.assertTrue(any("relative" in x for x in r.reasons), r.reasons)


class TestAssignmentKinds(unittest.TestCase):
    """The assignments page says: "Assignments begin on the dates listed left,
    and are due via Brightspace at 11:59 PM EST on the day listed below right."
    Both halves of that sentence get a name; neither is "unknown"."""

    LINE = "Sep. 09, 2026 Assignment 1 - Robot Building Due Sep. 13"

    def test_left_hand_date_is_a_release_not_unknown(self):
        out = records(self.LINE, kind=KIND_ASSIGNMENT_OUT)[0]
        self.assertEqual(out.kind, KIND_ASSIGNMENT_OUT)
        self.assertEqual(out.kind, "assignment_out")
        self.assertNotEqual(out.kind, "unknown")

    def test_release_explains_which_page_rule_named_it(self):
        out = records(self.LINE, kind=KIND_ASSIGNMENT_OUT)[0]
        self.assertTrue(any("release date" in x for x in out.reasons),
                        out.reasons)

    def test_release_is_all_day_but_the_deadline_is_timed(self):
        out, due = records(self.LINE, kind=KIND_ASSIGNMENT_OUT)
        self.assertIsNone(out.time)
        self.assertEqual(due.time, "23:59")

    def test_the_two_kinds_are_distinct(self):
        out, due = records(self.LINE, kind=KIND_ASSIGNMENT_OUT)
        self.assertNotEqual(out.kind, due.kind)

    def test_unknown_survives_for_a_page_with_no_stated_convention(self):
        out = records(self.LINE, kind="unknown")[0]
        self.assertEqual(out.kind, "unknown")
        self.assertTrue(any("no page convention" in x for x in out.reasons),
                        out.reasons)


class TestMultipleDates(unittest.TestCase):
    def test_due_cue_disambiguates_a_second_date(self):
        rs = records("Sep. 09, 2026 Assignment 1 - Robot Building Due Sep. 13",
                     kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(len(rs), 2)
        start, due = rs
        self.assertEqual((start.date, start.kind),
                         ("2026-09-09", KIND_ASSIGNMENT_OUT))
        self.assertEqual((due.date, due.kind),
                         ("2026-09-13", KIND_ASSIGNMENT_DUE))
        self.assertEqual(start.confidence, CERTAIN)
        self.assertEqual(due.confidence, CERTAIN)

    def test_due_date_inherits_the_item_title(self):
        rs = records("Sep. 09, 2026 Assignment 1 - Robot Building Due Sep. 13",
                     kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(rs[1].title, "Assignment 1 - Robot Building")

    def test_uncued_second_date_is_uncertain(self):
        rs = records("Sep. 09, 2026 Workshop Sep. 13 lab")
        self.assertEqual(len(rs), 2)
        self.assertEqual(rs[0].confidence, CERTAIN)
        self.assertEqual(rs[1].confidence, UNCERTAIN)
        self.assertTrue(any("multiple dates" in x for x in rs[1].reasons))

    def test_due_gets_the_default_time(self):
        rs = records("Sep. 09, 2026 A1 Due Sep. 13", kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(rs[1].time, "23:59")
        self.assertIsNone(rs[0].time)

    def test_default_due_time_is_configurable(self):
        cfg = Config()
        cfg.default_due_time = "17:00"
        rs = records("Sep. 09, 2026 A1 Due Sep. 13",
                     kind=KIND_ASSIGNMENT_OUT, cfg=cfg)
        self.assertEqual(rs[1].time, "17:00")

    def test_explicit_time_beats_the_default(self):
        r = one("Sep. 02, 2026 Lecture at 2:30pm in the lab")
        self.assertEqual(r.time, "14:30")


class TestDueCueSynonyms(unittest.TestCase):
    """A page that renames its "Due" column still has deadlines.

    With a narrow cue list the deadline silently degrades into an all-day
    event with no `DUE:` prefix and no 23:59 -- indistinguishable from a
    release date, which is exactly the confusion the two prefixes exist to
    prevent. Each synonym below must produce a real, timed deadline.
    """

    SYNONYMS = [
        "Due", "Deadline", "Due by", "Due on", "Due date",
        "Submission", "Submissions", "Submission due", "Submission by",
        "Submit", "Submit by", "Submitted", "Submitted by",
        "Hand in", "Hand-in", "Hand in by", "Handed in",
        "Turn in", "Turn-in", "Turn in by", "Turned in", "Turned in by",
    ]

    def test_every_synonym_produces_a_timed_deadline(self):
        for cue in self.SYNONYMS:
            rs = records("Sep. 09, 2026 Assignment 1 %s Sep. 13" % cue,
                         kind=KIND_ASSIGNMENT_OUT)
            self.assertEqual(len(rs), 2, cue)
            self.assertEqual(rs[1].kind, KIND_ASSIGNMENT_DUE, cue)
            self.assertEqual(rs[1].date, "2026-09-13", cue)
            self.assertEqual(rs[1].time, "23:59", cue)
            self.assertEqual(rs[1].confidence, CERTAIN, cue)

    def test_an_unambiguous_cue_is_stripped_from_the_title(self):
        for cue in ("Due", "Due by", "Deadline", "Submission by",
                    "Submitted by", "Handed in by", "Turn in by"):
            rs = records("Sep. 09, 2026 Assignment 1 %s Sep. 13" % cue,
                         kind=KIND_ASSIGNMENT_OUT)
            self.assertEqual(rs[0].title, "Assignment 1", cue)
            self.assertEqual(rs[1].title, "Assignment 1", cue)

    def test_an_ambiguous_cue_word_is_left_in_the_title(self):
        """Recognising a cue and deleting one are different decisions.

        "Assignment 1 Submission Sep. 13" and "Lab report submission" are the
        same shape: a bare noun that may be a cue or may be the last word of
        the title. Guessing wrong in the deleting direction truncates a real
        title silently -- and the title is hashed into the UID, so the
        calendar drops the event and creates a new one. Guessing wrong in the
        keeping direction leaves a slightly noisy title that the reader can
        see. The deadline itself is still recognised either way.
        """
        rs = records("Sep. 09, 2026 Assignment 1 Submission Sep. 13",
                     kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(rs[0].title, "Assignment 1 Submission")
        self.assertEqual(rs[1].title, "Assignment 1 Submission")
        # ...and the deadline is still a real, timed deadline.
        self.assertEqual(rs[1].kind, KIND_ASSIGNMENT_DUE)
        self.assertEqual(rs[1].time, "23:59")

    def test_a_title_ending_in_a_cue_word_survives_intact(self):
        """The M5 widening ate these four. Every one is a plausible title."""
        for text, expected in (
                ("Sep. 02, 2026 Lab report submission", "Lab report submission"),
                ("Sep. 02, 2026 Assignment Hand-in", "Assignment Hand-in"),
                ("Sep. 02, 2026 Project handed in", "Project handed in"),
                ("Sep. 02, 2026 How to Submit", "How to Submit"),
                ("Sep. 02, 2026 Turn-in procedures", "Turn-in procedures"),
                ("Sep. 02, 2026 Submissions", "Submissions"),
                ("Sep. 02, 2026 Group project submissions",
                 "Group project submissions")):
            rs = records(text, kind=KIND_ASSIGNMENT_OUT)
            self.assertEqual(len(rs), 1, text)
            self.assertEqual(rs[0].title, expected, text)

    def test_a_truncated_title_would_have_moved_the_uid(self):
        """Why the truncation mattered: the title is hashed into the UID."""
        from courseics.parse import assign_uids
        full = records("Sep. 02, 2026 Lab report submission",
                       kind=KIND_ASSIGNMENT_OUT)
        clipped = records("Sep. 02, 2026 Lab report",
                          kind=KIND_ASSIGNMENT_OUT)
        assign_uids(full)
        assign_uids(clipped)
        self.assertNotEqual(full[0].uid, clipped[0].uid)

    def test_the_stripping_vocabulary_is_a_subset_of_the_recognising_one(self):
        """Stripping more than we recognise is what cost the titles."""
        from courseics.parse import DUE_CUE_RE, TRAILING_CUE_RE
        for cue in self.SYNONYMS:
            text = "Assignment 1 %s " % cue
            if TRAILING_CUE_RE.search(text):
                self.assertTrue(DUE_CUE_RE.search(text), cue)

    def test_the_cue_must_sit_next_to_the_date(self):
        # The word appears, but it introduces prose rather than the date, so
        # the date must NOT be promoted to a deadline.
        rs = records("Sep. 09, 2026 Submit your report to the TA by hand",
                     kind=KIND_ASSIGNMENT_OUT)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0].kind, KIND_ASSIGNMENT_OUT)
        self.assertIsNone(rs[0].time)


class TestLowercaseMay(unittest.TestCase):
    def test_lowercase_may_without_corroboration_is_uncertain(self):
        r = one("Room 4 may 5 students attend")
        self.assertEqual(r.confidence, UNCERTAIN)
        self.assertTrue(any("lowercase 'may'" in x for x in r.reasons))

    def test_capitalised_may_with_year_is_certain(self):
        self.assertEqual(one("May 05, 2027 Final Exam").confidence, CERTAIN)


class TestTitles(unittest.TestCase):
    def test_title_excludes_the_date(self):
        self.assertEqual(one("Sep. 02, 2026 Introduction, History").title,
                         "Introduction, History")

    def test_title_keeps_internal_periods(self):
        self.assertEqual(one("Nov. 30, 2026 Project Lit. Review I").title,
                         "Project Lit. Review I")


if __name__ == "__main__":
    unittest.main()
