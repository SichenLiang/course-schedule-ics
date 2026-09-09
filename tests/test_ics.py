"""ICS shape, escaping, folding, UID stability, alarms."""

import datetime as dt
import unittest

from courseics.config import Config
from courseics.ics import build_calendar, escape, fold, uri_value
from courseics.parse import (CERTAIN, KIND_ASSIGNMENT_DUE, KIND_ASSIGNMENT_OUT,
                           KIND_LECTURE, UNCERTAIN, Record, assign_uids)

URL = "https://example.invalid/lectures"
NOW = dt.datetime(2026, 9, 2, 12, 0, 0, tzinfo=dt.timezone.utc)


def rec(date="2026-09-02", title="Intro", kind=KIND_LECTURE,
        confidence=CERTAIN, time=None, context="ctx", reasons=None):
    return Record(date=date, title=title, kind=kind, source_url=URL,
                  confidence=confidence, time=time, context=context,
                  reasons=reasons or [])


# No course is compiled into the program, so a bare Config() names no time
# zone -- there is no sensible default zone for "some course somewhere". Tests
# about zoned output therefore have to say which zone they mean; this is that
# zone, and it is the one the recorded course uses.
ZONE = "America/New_York"


def zoned():
    return Config(timezone=ZONE)


def cal(records, cfg=None):
    records = list(records)
    assign_uids(records)
    return build_calendar(records, cfg if cfg is not None else zoned(), now=NOW)


class TestEscapingAndFolding(unittest.TestCase):
    def test_escape_specials(self):
        self.assertEqual(escape("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")

    def test_fold_short_line_untouched(self):
        self.assertEqual(fold("SHORT:value"), ["SHORT:value"])

    def test_fold_respects_75_octets(self):
        for part in fold("DESCRIPTION:" + "x" * 400):
            self.assertLessEqual(len(part.encode("utf-8")), 75)

    def test_continuation_lines_start_with_space(self):
        parts = fold("DESCRIPTION:" + "x" * 400)
        self.assertTrue(all(p.startswith(" ") for p in parts[1:]))

    def test_multibyte_is_not_split_mid_character(self):
        parts = fold("SUMMARY:" + "作业" * 60)
        rejoined = parts[0] + "".join(p[1:] for p in parts[1:])
        self.assertEqual(rejoined, "SUMMARY:" + "作业" * 60)


class TestUriValuedProperties(unittest.TestCase):
    """`URL:` was the one property emitted raw.

    Every other value goes through escape(), which neutralises CR and LF --
    the only characters that can forge a property or a component boundary.
    Unreachable today (source_url comes from cfg.sources), but reachability is
    the part of an argument that goes stale, and closing it costs a line.

    escape() is deliberately NOT what closes it: URL is a URI value, not a
    TEXT value, so TEXT escaping would backslash the `,` and `;` that are
    legal, meaningful URI characters. A URI cannot hold a control character at
    all, so those are percent-encoded instead.
    """

    def test_an_ordinary_url_is_emitted_unchanged(self):
        self.assertEqual(uri_value("https://a.example/x?y=1&z=2"),
                         "https://a.example/x?y=1&z=2")

    def test_commas_and_semicolons_are_left_alone(self):
        """They are legal URI characters; escaping them corrupts the link."""
        self.assertEqual(uri_value("https://a.example/p,q;r"),
                         "https://a.example/p,q;r")

    def test_newlines_cannot_forge_a_structure_line(self):
        for injected in ("\r\n", "\n", "\r"):
            uri = "https://a.example/%sBEGIN:VEVENT" % injected
            value = uri_value(uri)
            self.assertNotIn("\n", value)
            self.assertNotIn("\r", value)

    def test_a_url_carrying_a_newline_injects_nothing(self):
        record = rec()
        record.source_url = "https://a.example/\r\nBEGIN:VALARM\r\nX-EVIL:1"
        text = cal([record])
        unfolded = text.replace("\r\n ", "")
        structure = [ln for ln in unfolded.split("\r\n")
                     if ln.startswith(("BEGIN:", "END:", "X-EVIL"))]
        self.assertEqual(structure, ["BEGIN:VCALENDAR", "BEGIN:VEVENT",
                                     "BEGIN:VALARM", "END:VALARM",
                                     "END:VEVENT", "END:VCALENDAR"])

    def test_control_bytes_are_percent_encoded(self):
        self.assertEqual(uri_value("https://a.example/\x00\x1b\x7f"),
                         "https://a.example/%00%1B%7F")

    def test_the_real_source_url_is_untouched(self):
        text = cal([rec()])
        self.assertIn("URL:%s" % URL, text)


class TestCalendarStructure(unittest.TestCase):
    def test_envelope(self):
        out = cal([rec()])
        self.assertTrue(out.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(out.endswith("END:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", out)
        self.assertIn("CALSCALE:GREGORIAN", out)

    def test_crlf_everywhere(self):
        out = cal([rec()])
        self.assertEqual(out.count("\r\n"), out.count("\n"))

    def test_every_event_is_balanced(self):
        out = cal([rec(), rec(date="2026-09-09", title="Two")])
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)
        self.assertEqual(out.count("END:VEVENT"), 2)
        self.assertEqual(out.count("BEGIN:VALARM"), 2)
        self.assertEqual(out.count("END:VALARM"), 2)

    def test_all_day_event_uses_value_date(self):
        out = cal([rec()])
        self.assertIn("DTSTART;VALUE=DATE:20260902", out)
        self.assertIn("DTEND;VALUE=DATE:20260903", out)

    def test_timed_event_is_anchored_to_the_courses_zone(self):
        out = cal([rec(date="2026-09-13", time="23:59",
                       kind=KIND_ASSIGNMENT_DUE)])
        self.assertIn("DTSTART;TZID=America/New_York:20260913T235900", out)
        self.assertIn("DTEND;TZID=America/New_York:20260914T002900", out)

    def test_due_events_are_labelled(self):
        out = cal([rec(title="A1", kind=KIND_ASSIGNMENT_DUE, time="23:59")])
        self.assertIn("SUMMARY:DUE: A1", out)

    def test_release_events_are_labelled_differently(self):
        out = cal([rec(title="A1", kind=KIND_ASSIGNMENT_OUT)])
        self.assertIn("SUMMARY:ASSIGNED: A1", out)
        self.assertNotIn("DUE:", out)

    def test_release_and_deadline_are_distinguishable_in_one_calendar(self):
        out = cal([rec(date="2026-09-09", title="A1",
                       kind=KIND_ASSIGNMENT_OUT),
                   rec(date="2026-09-13", title="A1",
                       kind=KIND_ASSIGNMENT_DUE, time="23:59")])
        self.assertIn("SUMMARY:ASSIGNED: A1", out)
        self.assertIn("SUMMARY:DUE: A1", out)
        self.assertIn("CATEGORIES:assignment_out", out)
        self.assertIn("CATEGORIES:assignment_due", out)

    def test_lectures_get_no_prefix(self):
        self.assertIn("SUMMARY:Intro", cal([rec(kind=KIND_LECTURE)]))


class TestTimeZone(unittest.TestCase):
    """A deadline is an instant, not a wall clock that follows the reader.

    Floating time meant "DTSTART:20260913T235900" with no zone at all, so a
    student in Los Angeles saw the deadline three hours AFTER it had actually
    passed. Naming the zone -- and shipping the VTIMEZONE that defines it --
    is the whole fix.
    """

    DUE = dict(date="2026-09-13", time="23:59", kind=KIND_ASSIGNMENT_DUE)

    def test_the_zone_is_defined_before_it_is_referenced(self):
        out = cal([rec(**self.DUE)])
        self.assertIn("BEGIN:VTIMEZONE", out)
        self.assertLess(out.index("BEGIN:VTIMEZONE"),
                        out.index("BEGIN:VEVENT"))
        self.assertEqual(out.count("BEGIN:VTIMEZONE"), 1)
        self.assertEqual(out.count("END:VTIMEZONE"), 1)

    def test_the_zone_is_not_hardcoded_to_est(self):
        # The page writes "11:59PM EST", but September in New York is EDT.
        # Both halves of the rule have to be in the file.
        out = cal([rec(**self.DUE)])
        self.assertIn("TZNAME:EDT", out)
        self.assertIn("TZOFFSETTO:-0400", out)
        self.assertIn("TZNAME:EST", out)
        self.assertIn("TZOFFSETTO:-0500", out)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", out)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", out)

    def test_september_resolves_to_edt_and_december_to_est(self):
        """Read the shipped rules back with the real tz database."""
        import zoneinfo
        zone = zoneinfo.ZoneInfo("America/New_York")
        sept = dt.datetime(2026, 9, 13, 23, 59, tzinfo=zone)
        december = dt.datetime(2026, 12, 7, 23, 59, tzinfo=zone)
        self.assertEqual(sept.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(december.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(sept.tzname(), "EDT")
        self.assertEqual(december.tzname(), "EST")

    def test_all_day_events_carry_no_zone(self):
        # A lecture on a date has no time of day to convert.
        out = cal([rec()])
        self.assertIn("DTSTART;VALUE=DATE:20260902", out)
        self.assertNotIn("TZID", out)
        self.assertNotIn("VTIMEZONE", out)

    def test_no_dangling_tzid_when_the_zone_is_unknown(self):
        cfg = zoned()
        cfg.timezone = "Mars/Olympus_Mons"
        out = cal([rec(**self.DUE)], cfg)
        self.assertNotIn("TZID", out)
        self.assertNotIn("VTIMEZONE", out)
        self.assertIn("DTSTART:20260913T235900", out)

    def test_floating_time_is_still_reachable(self):
        cfg = zoned()
        cfg.timezone = ""
        out = cal([rec(**self.DUE)], cfg)
        self.assertIn("DTSTART:20260913T235900", out)
        self.assertNotIn("VTIMEZONE", out)

    def test_the_zone_block_does_not_disturb_folding_or_escaping(self):
        out = cal([rec(**self.DUE),
                   rec(title="A, b; c\\ d" + "\u4f5c\u4e1a" * 40,
                       date="2026-09-14", time="23:59",
                       kind=KIND_ASSIGNMENT_DUE)])
        for line in out.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        self.assertEqual(out.count("\r\n"), out.count("\n"))
        unfolded = out.replace("\r\n ", "")
        self.assertIn("SUMMARY:DUE: A\\, b\\; c\\\\ d" + "\u4f5c\u4e1a" * 40, unfolded)
        # The RRULE's semicolons are structured syntax and must NOT be escaped.
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", out)


class TestUncertainRendering(unittest.TestCase):
    def test_uncertain_is_still_emitted(self):
        out = cal([rec(confidence=UNCERTAIN)])
        self.assertEqual(out.count("BEGIN:VEVENT"), 1)

    def test_uncertain_summary_is_prefixed(self):
        out = cal([rec(title="Labor Day", confidence=UNCERTAIN)])
        self.assertIn("SUMMARY:[?] Labor Day", out)

    def test_certain_summary_is_not_prefixed(self):
        self.assertNotIn("[?]", cal([rec()]))

    def test_raw_context_lands_in_description(self):
        out = cal([rec(confidence=UNCERTAIN,
                       context="Sep. 07, 2026 Labor Day - no class",
                       reasons=["negation word 'no class' near the date"])])
        unfolded = out.replace("\r\n ", "")
        self.assertIn("source line: Sep. 07\\, 2026 Labor Day - no class",
                      unfolded)
        self.assertIn("why uncertain", unfolded)
        self.assertIn("no class", unfolded)


class TestAlarms(unittest.TestCase):
    def test_default_is_one_day_before(self):
        self.assertIn("TRIGGER:-P1D", cal([rec()]))

    def test_alarm_lead_is_configurable(self):
        cfg = zoned()
        cfg.alarm_days_before = 3
        self.assertIn("TRIGGER:-P3D", cal([rec()], cfg))

    def test_zero_days_means_at_start(self):
        cfg = zoned()
        cfg.alarm_days_before = 0
        self.assertIn("TRIGGER:-PT0M", cal([rec()], cfg))


class TestUidStability(unittest.TestCase):
    def test_uid_ignores_the_date(self):
        a = [rec(date="2026-09-02")]
        b = [rec(date="2026-10-20")]
        assign_uids(a)
        assign_uids(b)
        self.assertEqual(a[0].uid, b[0].uid)

    def test_uid_is_reproducible_across_runs(self):
        a, b = [rec()], [rec()]
        assign_uids(a)
        assign_uids(b)
        self.assertEqual(a[0].uid, b[0].uid)

    def test_uid_differs_by_kind(self):
        # The release and the deadline of one assignment share a title; only
        # the kind separates them, so the kind has to be in the UID.
        rs = [rec(title="A1", kind=KIND_ASSIGNMENT_OUT),
              rec(title="A1", kind=KIND_ASSIGNMENT_DUE)]
        assign_uids(rs)
        self.assertNotEqual(rs[0].uid, rs[1].uid)

    def test_uid_differs_by_title(self):
        rs = [rec(title="One"), rec(title="Two")]
        assign_uids(rs)
        self.assertNotEqual(rs[0].uid, rs[1].uid)

    def test_identical_rows_get_distinct_uids(self):
        # ROB-UY 3303 really has two "Project working session" lectures.
        rs = [rec(date="2026-12-07", title="Project working session"),
              rec(date="2026-12-09", title="Project working session")]
        assign_uids(rs)
        self.assertNotEqual(rs[0].uid, rs[1].uid)


class TestAmbiguousRowIdentity(unittest.TestCase):
    """Two rows sharing (url, title, kind) are told apart by their date.

    Numbering them by position was fake stability. Deleting the earlier row
    handed its UID to the later one, so the calendar moved the wrong event,
    --diff invented a reschedule, and the student's own edits on the surviving
    event were silently reassigned to a different session.
    """

    PAIR = ("2026-12-07", "2026-12-09")

    def _pair(self):
        rs = [rec(date=d, title="Project working session") for d in self.PAIR]
        assign_uids(rs)
        return rs

    def test_deleting_the_earlier_row_does_not_rename_the_later_one(self):
        before = self._pair()
        after = [rec(date="2026-12-09", title="Project working session")]
        assign_uids(after)
        # The whole point: the survivor must NOT inherit the deleted row's UID.
        self.assertNotEqual(after[0].uid, before[0].uid)

    def test_deleting_the_later_row_does_not_rename_the_earlier_one(self):
        before = self._pair()
        after = [rec(date="2026-12-07", title="Project working session")]
        assign_uids(after)
        self.assertNotEqual(after[0].uid, before[1].uid)

    def test_an_ambiguous_row_is_keyed_by_its_date(self):
        a = self._pair()
        b = self._pair()
        self.assertEqual([r.uid for r in a], [r.uid for r in b])
        # Reordering the input must not move identity between the two rows.
        rs = [rec(date=d, title="Project working session")
              for d in reversed(self.PAIR)]
        assign_uids(rs)
        self.assertEqual(rs[1].uid, a[0].uid)
        self.assertEqual(rs[0].uid, a[1].uid)

    def test_a_third_identical_row_does_not_disturb_the_other_two(self):
        two = self._pair()
        three = [rec(date=d, title="Project working session")
                 for d in ("2026-12-07", "2026-12-09", "2026-12-11")]
        assign_uids(three)
        self.assertEqual(three[0].uid, two[0].uid)
        self.assertEqual(three[1].uid, two[1].uid)

    def test_rows_sharing_a_date_still_get_distinct_uids(self):
        rs = [rec(date="2026-12-07", title="Project working session",
                  time="10:00"),
              rec(date="2026-12-07", title="Project working session",
                  time="14:00")]
        assign_uids(rs)
        self.assertEqual(len({r.uid for r in rs}), 2)

    def test_a_unique_row_keeps_a_date_free_uid(self):
        # The hardened core path: an ordinary reschedule is still an update.
        a = [rec(date="2026-12-07", title="Project working session")]
        b = [rec(date="2026-12-14", title="Project working session")]
        assign_uids(a)
        assign_uids(b)
        self.assertEqual(a[0].uid, b[0].uid)

    def test_a_new_duplicate_rotates_the_original_uid_once(self):
        # The accepted cost, asserted so it stays a decision and not a
        # surprise: the first appearance of a second same-titled row moves the
        # original off its date-free UID. It is stable from then on.
        solo = [rec(date="2026-12-07", title="Project working session")]
        assign_uids(solo)
        pair = self._pair()
        self.assertNotEqual(solo[0].uid, pair[0].uid)
        again = self._pair()
        self.assertEqual([r.uid for r in pair], [r.uid for r in again])


if __name__ == "__main__":
    unittest.main()
