"""One test case per exclusion rule, as required.

Two layers of defense are exercised here:
  1. DATE_RE requires a month NAME, so bare numerals cannot reach the parser.
  2. TRAP_PREFIX_RE / COURSE_CODE_RE reject a match that a looser pattern
     would otherwise accept.
Layer 2 is tested directly so it stays honest even if layer 1 is loosened.
"""

import unittest

from courseics.config import Config
from courseics.parse import DATE_RE, _is_trapped, parse_line

URL = "https://example.invalid/page"


def records(line, kind="lecture"):
    return parse_line(line, URL, kind, Config())


class TestObservedTrapStrings(unittest.TestCase):
    """Real prose fragments from course pages that must yield no records."""

    def test_chapter_17(self):
        self.assertEqual(records("Read Chapter 17 before class"), [])

    def test_case_17_2(self):
        self.assertEqual(records("Discussion of Case 17.2"), [])

    def test_questions_1_3(self):
        self.assertEqual(records("Answer Questions 1-3"), [])

    def test_room_315(self):
        self.assertEqual(records("Lecture in Room 315"), [])

    def test_six_metrotech(self):
        self.assertEqual(records("6 Metrotech Center"), [])

    def test_room_804(self):
        self.assertEqual(records("Office hours in Room 804"), [])

    def test_course_code_ece_uy_2004(self):
        self.assertEqual(records("Prerequisite: ECE-UY 2004"), [])

    def test_course_code_rob_uy_3303(self):
        self.assertEqual(records("ROB UY 3303 Robot Motion and Planning"), [])

    def test_problem_set_number(self):
        self.assertEqual(records("Problem 12 is optional"), [])

    def test_section_number(self):
        self.assertEqual(records("Section 3 covers kinematics"), [])

    def test_figure_number(self):
        self.assertEqual(records("See Fig 4 for the diagram"), [])

    def test_bare_year_is_not_a_date(self):
        self.assertEqual(records("Copyright 2026 NYU"), [])

    def test_month_with_four_digit_number_is_not_a_day(self):
        # "Sep 2024" must not be read as day=20 or day=2.
        self.assertEqual(records("Sep 2024"), [])

    def test_time_is_not_a_date(self):
        self.assertEqual(records("Class meets at 11:59 in the lab"), [])


class TestMonthPrefixIsNotAMonth(unittest.TestCase):
    """A month name is a closed vocabulary, not a prefix.

    An open-ended "[a-z]*" tail after the abbreviation turned ordinary robotics
    prose into confident dates AND ate the front of the title. This is a
    robotics course whose real schedule already contains a row called "Novel
    Robot MP", so "Novel 3D Printing for Robots" would have quietly added a
    thirteenth lecture on 2026-11-03.
    """

    def test_augmented_is_not_august(self):
        self.assertEqual(records("Augmented 3D perception lab"), [])

    def test_novel_is_not_november(self):
        self.assertEqual(records("Novel 3D printing"), [])

    def test_octomap_is_not_october(self):
        self.assertEqual(records("Octomap 2 tutorial"), [])

    def test_decimal_is_not_december(self):
        self.assertEqual(records("Decimal 4 places of precision"), [])

    def test_januarys_is_not_january(self):
        self.assertEqual(records("Januarys 3 whatever"), [])

    def test_marching_is_not_march(self):
        self.assertEqual(records("Marching cubes 5 demo"), [])

    def test_junction_is_not_june(self):
        self.assertEqual(records("Junction 3 of the manipulator"), [])

    def test_the_title_is_not_eaten(self):
        # The old regex produced title "D perception lab" from this line.
        self.assertEqual(records("Augmented 3D perception lab"), [])

    def test_legal_spellings_still_parse(self):
        for form in ("Sep. 03, 2026", "Sept. 03, 2026", "September 03, 2026",
                     "SEPTEMBER 03, 2026", "Sep 03, 2026"):
            rs = records("%s Sampling" % form)
            self.assertEqual(len(rs), 1, form)
            self.assertEqual(rs[0].date, "2026-09-03", form)
            self.assertEqual(rs[0].title, "Sampling", form)

    def test_every_month_abbreviation_and_full_name_is_accepted(self):
        from courseics.parse import DATE_RE, MONTH_TOKENS
        for name, number in MONTH_TOKENS:
            m = DATE_RE.search("%s 04, 2026 Thing" % name.capitalize())
            self.assertIsNotNone(m, name)
            self.assertEqual(m.group("month").lower(), name)


class TestPrefixGuardDirectly(unittest.TestCase):
    """Each keyword rejects a match that would otherwise be accepted."""

    def _trapped(self, line):
        m = DATE_RE.search(line)
        self.assertIsNotNone(m, "regex should match in %r" % line)
        return _is_trapped(line, m)

    def test_chapter_prefix(self):
        self.assertIn("Chapter", self._trapped("See Chapter Sep. 3"))

    def test_case_prefix(self):
        self.assertIn("Case", self._trapped("Review Case Sep. 3"))

    def test_question_prefix(self):
        self.assertIn("Question", self._trapped("Answer Question Sep. 3"))

    def test_room_prefix(self):
        self.assertIn("Room", self._trapped("Meet in Room Sep. 3"))

    def test_problem_prefix(self):
        self.assertIn("Problem", self._trapped("Do Problem Sep. 3"))

    def test_section_prefix(self):
        self.assertIn("Section", self._trapped("Read Section Sep. 3"))

    def test_ch_dot_prefix(self):
        self.assertIn("Ch", self._trapped("Read Ch. Sep. 3"))

    def test_fig_prefix(self):
        self.assertIn("Fig", self._trapped("See Fig Sep. 3"))

    def test_no_trap_on_a_clean_line(self):
        self.assertIsNone(self._trapped("Sep. 03, 2026 Lecture"))


class TestDropsAreReportedNotSwallowed(unittest.TestCase):
    """A date-shaped fragment that yields no record must say so.

    "Feb. 30, 2026 Assignment 9" is a plausible typo. It used to vanish
    entirely: no record, no message, exit 0 -- a deadline that is simply not in
    the calendar, with nothing anywhere to notice.
    """

    def _drops(self, line, kind="lecture"):
        from courseics.parse import parse_line
        out = []
        recs = parse_line(line, URL, kind, Config(), out)
        return recs, out

    def test_impossible_calendar_date_is_recorded(self):
        recs, drops = self._drops("Feb. 30, 2026 Assignment 9")
        self.assertEqual(recs, [])
        self.assertEqual(len(drops), 1)
        self.assertIn("Feb. 30, 2026", drops[0].describe())
        self.assertIn("Assignment 9", drops[0].describe())
        self.assertIn("not a real calendar date", drops[0].describe())

    def test_month_outside_the_anchor_is_recorded(self):
        recs, drops = self._drops("Jul. 04 Independence Day")
        self.assertEqual(recs, [])
        self.assertEqual(len(drops), 1)
        self.assertIn("semester anchor", drops[0].describe())

    def test_a_clean_line_drops_nothing(self):
        recs, drops = self._drops("Sep. 03, 2026 Sampling")
        self.assertEqual(len(recs), 1)
        self.assertEqual(drops, [])

    def test_the_collector_is_optional(self):
        from courseics.parse import parse_line
        self.assertEqual(parse_line("Feb. 30, 2026 A9", URL, "lecture",
                                    Config()), [])

    def test_a_drop_does_not_suppress_the_good_date_on_the_same_line(self):
        recs, drops = self._drops("Sep. 09, 2026 A1 Due Feb. 30")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].date, "2026-09-09")
        self.assertEqual(len(drops), 1)


class TestImpossibleDaysAreReportedToo(unittest.TestCase):
    """"Feb. 30" was reported; "Sep. 32" was silent. Same typo, same cost.

    An out-of-range day was filtered inside _scan_dates, before the hit list
    was built -- and parse_line returns early on an empty hit list, throwing
    the rejection away with it. So the two halves of one class of human error
    got opposite treatment for a reason that is purely about where in the
    pipeline each was caught. Both are a deadline that is simply not in the
    calendar, with exit 0 saying everything went fine.
    """

    def _drops(self, line, kind="lecture"):
        from courseics.parse import parse_line
        out = []
        recs = parse_line(line, URL, kind, Config(), out)
        return recs, out

    def test_day_32_is_reported(self):
        recs, drops = self._drops("Sep. 32, 2026 Assignment 9")
        self.assertEqual(recs, [])
        self.assertEqual(len(drops), 1)
        self.assertIn("Sep. 32, 2026", drops[0].describe())
        self.assertIn("Assignment 9", drops[0].describe())

    def test_day_zero_is_reported(self):
        recs, drops = self._drops("Sep. 0 Assignment 9")
        self.assertEqual(recs, [])
        self.assertEqual(len(drops), 1)
        self.assertIn("Sep. 0", drops[0].describe())

    def test_day_double_zero_is_reported(self):
        _, drops = self._drops("Sep. 00, 2026 Assignment 9")
        self.assertEqual(len(drops), 1)

    def test_day_99_is_reported(self):
        _, drops = self._drops("Sep. 99 Assignment 9")
        self.assertEqual(len(drops), 1)

    def test_the_reason_names_the_range(self):
        _, drops = self._drops("Sep. 47, 2026 Nonsense")
        self.assertIn("1-31", drops[0].describe())

    def test_it_is_reported_exactly_like_an_impossible_calendar_date(self):
        """The two are the same class of mistake and read the same way."""
        _, out_of_range = self._drops("Sep. 32, 2026 Assignment 9")
        _, impossible = self._drops("Feb. 30, 2026 Assignment 9")
        self.assertEqual(len(out_of_range), len(impossible))
        for drop in (out_of_range[0], impossible[0]):
            self.assertIn("Assignment 9", drop.describe())
            self.assertTrue(drop.text)
            self.assertTrue(drop.reason)

    def test_it_does_not_suppress_a_good_date_on_the_same_line(self):
        recs, drops = self._drops("Sep. 09, 2026 A1 Due Sep. 32")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].date, "2026-09-09")
        self.assertEqual(len(drops), 1)

    def test_a_false_positive_trap_stays_silent(self):
        """Traps are not typos. Reporting them would bury the real drops."""
        for line in ("Chapter 17 and Room 315", "Case 17.2", "ECE-UY 2004",
                     "Problem 3", "Figure 12"):
            recs, drops = self._drops(line)
            self.assertEqual(recs, [], line)
            self.assertEqual(drops, [], line)

    def test_the_collector_is_still_optional(self):
        from courseics.parse import parse_line
        self.assertEqual(parse_line("Sep. 32, 2026 A9", URL, "lecture",
                                    Config()), [])

    def test_a_clean_line_drops_nothing(self):
        recs, drops = self._drops("Sep. 03, 2026 Sampling")
        self.assertEqual(len(recs), 1)
        self.assertEqual(drops, [])


class TestNoDateMeansNoRecord(unittest.TestCase):
    def test_undated_prose_is_discarded_not_guessed(self):
        self.assertEqual(
            records("Assignments will be done in pairs of two students."), [])

    def test_empty_line(self):
        self.assertEqual(records("   "), [])

    def test_navigation_noise(self):
        self.assertEqual(records("Skip to main content"), [])

    def test_impossible_day_rejected(self):
        self.assertEqual(records("Sep. 47, 2026 Nonsense"), [])


if __name__ == "__main__":
    unittest.main()
