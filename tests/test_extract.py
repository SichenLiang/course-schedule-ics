"""The whitespace rule that decides whether we see 6 dates or 30."""

import unittest

from tests import FIXTURES
from courseics.extract import to_lines, to_text
import os


class TestInlineTagWhitespace(unittest.TestCase):
    def test_span_split_date_is_rejoined(self):
        # Exactly how Google Sites emits "Oct. 05, 2026".
        html = ('<p><span>Oct</span><span>. 0</span><span>5</span>'
                '<span>, 2026</span><span>\t</span>'
                '<span>Sampling Based Algorithms II</span></p>')
        self.assertEqual(to_lines(html),
                         ["Oct. 05, 2026 Sampling Based Algorithms II"])

    def test_span_split_word_is_rejoined(self):
        html = "<p><span>Novel R</span><span>obot MP</span></p>"
        self.assertEqual(to_lines(html), ["Novel Robot MP"])

    def test_block_tags_separate_lines(self):
        html = "<li><p>Week 1</p></li><li><p>Week 2</p></li>"
        self.assertEqual(to_lines(html), ["Week 1", "Week 2"])

    def test_br_separates_lines(self):
        self.assertEqual(to_lines("<p>a<br>b</p>"), ["a", "b"])

    def test_scripts_and_styles_dropped(self):
        html = "<p>keep</p><script>var d='Sep. 09, 2026';</script><style>x{}</style>"
        self.assertEqual(to_lines(html), ["keep"])

    def test_entities_and_nbsp(self):
        self.assertEqual(to_lines("<p>A&nbsp;&amp;&nbsp;B</p>"), ["A & B"])


class TestRealFixtures(unittest.TestCase):
    def test_lectures_fixture_yields_all_thirty_rows(self):
        with open(os.path.join(FIXTURES, "lectures.html"), encoding="utf-8") as fh:
            text = to_text(fh.read())
        for expected in ("Sep. 02, 2026 Introduction, History, and Architectures",
                         "Oct. 05, 2026 Sampling Based Algorithms II",
                         "Nov. 23, 2026 Project Kickoff",
                         "Dec. 14, 2026 Project Final Presentations"):
            self.assertIn(expected, text)

    def test_assignments_fixture_keeps_due_dates_on_one_line(self):
        with open(os.path.join(FIXTURES, "assignments.html"), encoding="utf-8") as fh:
            text = to_text(fh.read())
        self.assertIn(
            "Sep. 09, 2026 Assignment 1 - Robot Building Due Sep. 13", text)


if __name__ == "__main__":
    unittest.main()
