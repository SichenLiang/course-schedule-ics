"""The config file: what it accepts, and what it refuses to accept quietly.

The point of this module is the refusals. Every setting here decides something
about a calendar that will look perfectly plausible when it is wrong -- a year,
a time zone, which page means what -- so the failure mode being defended
against is not a crash, it is a tidy calendar full of the wrong dates.
"""

import os
import shutil
import tempfile
import unittest

from tests import RECORDED_CONFIG, demo_config
from courseics import cli as cli_mod
from courseics.config import (DEFAULT_ALARM_KINDS, KIND_ASSIGNMENT_DUE,
                              KIND_ASSIGNMENT_OUT, KIND_LECTURE,
                              KIND_NO_CLASS, KIND_UNKNOWN,
                              ConfigError, load_config)
from courseics.timezones import SUPPORTED_TIMEZONES, VTIMEZONES

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, "config.example.ini")

MINIMAL = """\
[calendar]
name = Example Course

[semester]
fall_year = 2026

[source:lectures]
url = https://example.invalid/lectures
kind = lecture
"""


class TempConfig(object):
    """Writes a config file to a scratch directory and hands back its path."""

    def __init__(self, text):
        self.text = text

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "course-schedule.ini")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.text)
        self.path = path
        return path

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestShippedFilesParse(unittest.TestCase):
    """The two config files in the repo have to be loadable.

    A shipped example that no longer parses is worse than none: it is the first
    thing a new user copies, and the error they get names their edit rather
    than our staleness.
    """

    def test_the_annotated_template_loads(self):
        cfg = load_config(EXAMPLE)
        self.assertEqual(len(cfg.sources), 2)
        self.assertTrue(cfg.calendar_name)
        # Deliberately not a real course: nobody should publish a calendar of
        # somebody else's lectures because they forgot to edit a URL.
        for src in cfg.sources:
            self.assertIn("example", src.url)

    def test_the_recorded_course_loads(self):
        cfg = load_config(RECORDED_CONFIG)
        self.assertEqual(cfg.calendar_name, "ROB-UY 3303")
        self.assertEqual(cfg.timezone, "America/New_York")
        self.assertEqual(cfg.anchor.name, "fall-2026")
        self.assertEqual([s.default_kind for s in cfg.sources],
                         [KIND_LECTURE, KIND_ASSIGNMENT_OUT])

    def test_fixture_paths_are_resolved_against_the_config_file(self):
        cfg = load_config(RECORDED_CONFIG)
        for src in cfg.sources:
            self.assertTrue(os.path.isabs(src.fixture), src.fixture)
            self.assertTrue(os.path.exists(src.fixture), src.fixture)

    def test_sections_keep_their_file_order(self):
        # The order decides which page is fetched first, and shows up in every
        # log line; shuffling it would make two runs look different for no
        # reason.
        cfg = load_config(RECORDED_CONFIG)
        self.assertEqual([s.name for s in cfg.sources],
                         ["lectures", "assignments"])


class TestMinimal(unittest.TestCase):
    def test_a_minimal_file_is_enough(self):
        with TempConfig(MINIMAL) as path:
            cfg = load_config(path)
        self.assertEqual(cfg.calendar_name, "Example Course")
        self.assertEqual(cfg.anchor.year_for_month(9), 2026)
        self.assertEqual(cfg.anchor.year_for_month(2), 2027)
        self.assertEqual(len(cfg.sources), 1)
        self.assertIsNone(cfg.sources[0].fixture)
        self.assertEqual(cfg.timezone, "")        # floating unless asked for
        self.assertEqual(cfg.default_due_time, "23:59")
        self.assertEqual(cfg.alarm_days_before, 1)

    def test_the_path_is_remembered_for_error_messages(self):
        with TempConfig(MINIMAL) as path:
            self.assertEqual(load_config(path).path, os.path.abspath(path))


class TestAlarmPolicy(unittest.TestCase):
    """[alarms]: which kinds get a reminder, and how far ahead.

    The default silences lectures, release dates and holidays, which is a
    judgement about one person's course page and not a fact about everybody's.
    It is therefore a default rather than a decision: a user who does want a
    nudge before every lecture writes one line.
    """

    def _load(self, section):
        with TempConfig(MINIMAL + section) as path:
            return load_config(path)

    def test_no_section_means_the_documented_default(self):
        cfg = self._load("")
        self.assertEqual(cfg.alarm_kinds, DEFAULT_ALARM_KINDS)
        self.assertEqual(set(cfg.alarm_kinds),
                         {KIND_ASSIGNMENT_DUE, KIND_UNKNOWN})
        self.assertEqual(cfg.alarm_days_by_kind, {})

    def test_a_kind_can_be_switched_on(self):
        cfg = self._load("\n[alarms]\nlecture = on\n")
        self.assertIn(KIND_LECTURE, cfg.alarm_kinds)
        # "on" means the default lead, not a lead of its own.
        self.assertNotIn(KIND_LECTURE, cfg.alarm_days_by_kind)

    def test_a_kind_can_be_switched_off(self):
        cfg = self._load("\n[alarms]\nassignment_due = off\n")
        self.assertNotIn(KIND_ASSIGNMENT_DUE, cfg.alarm_kinds)

    def test_a_number_switches_it_on_and_sets_the_lead(self):
        cfg = self._load("\n[alarms]\nlecture = 3\n")
        self.assertIn(KIND_LECTURE, cfg.alarm_kinds)
        self.assertEqual(cfg.alarm_days_by_kind[KIND_LECTURE], 3)

    def test_zero_is_a_lead_not_an_off_switch(self):
        # The reason the off-switch is a word: "0" is a setting somebody wants
        # -- remind me at the event itself -- and reading it as false would
        # silently delete their reminder.
        cfg = self._load("\n[alarms]\nno_class = 0\n")
        self.assertIn(KIND_NO_CLASS, cfg.alarm_kinds)
        self.assertEqual(cfg.alarm_days_by_kind[KIND_NO_CLASS], 0)

    def test_every_kind_is_addressable(self):
        cfg = self._load("\n[alarms]\nlecture = 1\nassignment_out = 1\n"
                         "assignment_due = 1\nno_class = 1\nunknown = 1\n")
        self.assertEqual(len(cfg.alarm_kinds), 5)

    def test_a_kind_that_does_not_exist_is_refused(self):
        with TempConfig(MINIMAL + "\n[alarms]\nseminar = 2\n") as path:
            with self.assertRaises(ConfigError) as caught:
                load_config(path)
        message = str(caught.exception)
        self.assertIn("seminar", message)
        self.assertIn("assignment_due", message)   # names the valid ones

    def test_a_value_that_is_neither_a_number_nor_a_switch_is_refused(self):
        with TempConfig(MINIMAL + "\n[alarms]\nlecture = maybe\n") as path:
            with self.assertRaises(ConfigError) as caught:
                load_config(path)
        self.assertIn("maybe", str(caught.exception))

    def test_an_out_of_range_lead_is_refused(self):
        with TempConfig(MINIMAL + "\n[alarms]\nlecture = 4000\n") as path:
            with self.assertRaises(ConfigError) as caught:
                load_config(path)
        self.assertIn("0-365", str(caught.exception))


class TestRefusals(unittest.TestCase):
    def _rejects(self, text, *expected):
        with TempConfig(text) as path:
            with self.assertRaises(ConfigError) as caught:
                load_config(path)
        message = str(caught.exception)
        # Always name the file: a config error nobody can locate is barely
        # better than no message.
        self.assertIn(".ini", message)
        for fragment in expected:
            self.assertIn(fragment, message)
        return message

    def test_a_missing_file(self):
        with self.assertRaises(ConfigError) as caught:
            load_config("/nonexistent/nowhere/course-schedule.ini")
        self.assertIn("config.example.ini", str(caught.exception))

    def test_a_file_that_is_not_ini_at_all(self):
        self._rejects("{\"calendar\": {\"name\": \"x\"}}", "INI")

    def test_no_fall_year(self):
        text = MINIMAL.replace("fall_year = 2026", "")
        self._rejects(text, "fall_year")

    def test_a_fall_year_that_is_not_a_number(self):
        self._rejects(MINIMAL.replace("2026", "next year"), "fall_year")

    def test_an_absurd_fall_year(self):
        self._rejects(MINIMAL.replace("fall_year = 2026", "fall_year = 20266"),
                      "fall_year")

    def test_no_calendar_name(self):
        self._rejects(MINIMAL.replace("name = Example Course", "name ="),
                      "name")

    def test_a_time_zone_with_no_definition_to_ship(self):
        text = MINIMAL.replace("[semester]",
                               "timezone = Mars/Olympus_Mons\n\n[semester]")
        message = self._rejects(text, "timezone", "Mars/Olympus_Mons")
        # The message has to say which zones DO work, or the user is guessing.
        self.assertIn("America/New_York", message)

    def test_a_nonsense_due_time(self):
        text = MINIMAL.replace("[semester]",
                               "default_due_time = 25:00\n\n[semester]")
        self._rejects(text, "default_due_time")

    def test_a_negative_alarm_lead(self):
        text = MINIMAL.replace("[semester]",
                               "alarm_days_before = -1\n\n[semester]")
        self._rejects(text, "alarm_days_before")

    def test_a_shrink_ratio_outside_zero_to_one(self):
        self._rejects(MINIMAL + "\n[health]\nmax_shrink_ratio = 1.5\n",
                      "max_shrink_ratio")

    def test_an_unknown_section(self):
        self._rejects(MINIMAL + "\n[calender]\nname = typo\n", "calender")

    def test_no_sources_at_all(self):
        text = MINIMAL.split("[source:lectures]")[0]
        self._rejects(text, "source:")

    def test_a_source_with_no_url(self):
        self._rejects(MINIMAL.replace("url = https://example.invalid/lectures",
                                      "url ="), "url")

    def test_a_source_that_is_not_http(self):
        self._rejects(
            MINIMAL.replace("https://example.invalid/lectures",
                            "file:///etc/passwd"), "url")

    def test_two_sources_pointing_at_one_page(self):
        text = MINIMAL + ("\n[source:again]\n"
                          "url = https://example.invalid/lectures\n"
                          "kind = lecture\n")
        self._rejects(text, "same page")

    def test_a_source_with_no_kind(self):
        self._rejects(MINIMAL.replace("kind = lecture", "kind ="), "kind")

    def test_a_kind_the_tool_does_not_know(self):
        message = self._rejects(MINIMAL.replace("kind = lecture",
                                                "kind = midterm"), "midterm")
        # Naming the four it does know turns a rejection into an instruction.
        self.assertIn("assignment_due", message)


class TestTimezoneTable(unittest.TestCase):
    """Every zone offered is a claim about somebody's clock."""

    def test_each_block_defines_the_zone_it_is_filed_under(self):
        for tzid, block in VTIMEZONES.items():
            self.assertIn("TZID:%s" % tzid, block, tzid)

    def test_each_block_is_balanced_and_complete(self):
        for tzid, block in VTIMEZONES.items():
            self.assertEqual(block[0], "BEGIN:VTIMEZONE", tzid)
            self.assertEqual(block[-1], "END:VTIMEZONE", tzid)
            for begin, end in (("BEGIN:STANDARD", "END:STANDARD"),
                               ("BEGIN:DAYLIGHT", "END:DAYLIGHT")):
                self.assertEqual(block.count(begin), block.count(end), tzid)
            # A zone with no STANDARD component is not a zone.
            self.assertEqual(block.count("BEGIN:STANDARD"), 1, tzid)

    def test_every_offset_is_a_signed_four_digit_offset(self):
        import re
        offset = re.compile(r"^TZOFFSET(FROM|TO):[+-][0-9]{4}$")
        for tzid, block in VTIMEZONES.items():
            for line in block:
                if line.startswith("TZOFFSET"):
                    self.assertRegex(line, offset, tzid)

    def test_a_recurring_transition_is_paired_with_another(self):
        # A zone that springs forward and never falls back would drift by an
        # hour a year, for ever.
        for tzid, block in VTIMEZONES.items():
            if "BEGIN:DAYLIGHT" in block:
                self.assertEqual(len([l for l in block
                                      if l.startswith("RRULE:")]), 2, tzid)

    def test_the_advertised_list_is_the_table(self):
        self.assertEqual(set(SUPPORTED_TIMEZONES), set(VTIMEZONES))


class TestCliRefusesToRunUnconfigured(unittest.TestCase):
    """A run with no course is a configuration problem, not a network one.

    All of these exit 7. They used to be indistinguishable from a fetch
    failure (argparse exits 2, and 2 means "check the network"), which sends
    the reader to look at a connection that is working perfectly.
    """

    def _run(self, argv):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_mod.main(argv, log=io.StringIO())
        return code, err.getvalue()

    def test_no_config_file_anywhere(self):
        with TempConfig(MINIMAL) as path:
            missing = os.path.join(os.path.dirname(path), "not-here.ini")
            code, err = self._run(["--config", missing, "--out-dir",
                                   os.path.dirname(path)])
        self.assertEqual(code, cli_mod.EXIT_CONFIG)
        self.assertIn("config.example.ini", err)

    def test_an_unusable_config_file(self):
        with TempConfig(MINIMAL.replace("fall_year = 2026", "")) as path:
            code, err = self._run(["--config", path, "--out-dir",
                                   os.path.dirname(path)])
        self.assertEqual(code, cli_mod.EXIT_CONFIG)
        self.assertIn("fall_year", err)

    def test_offline_without_a_recorded_page(self):
        with TempConfig(MINIMAL) as path:
            code, err = self._run(["--config", path, "--offline", "--out-dir",
                                   os.path.dirname(path)])
        self.assertEqual(code, cli_mod.EXIT_CONFIG)
        self.assertIn("fixture", err)
        self.assertIn("lectures", err)

    def test_offline_replays_the_recorded_pages_without_a_socket(self):
        # The whole suite runs with sockets disabled, so reaching this exit
        # code at all proves nothing was fetched.
        with tempfile.TemporaryDirectory() as d:
            import io
            code = cli_mod.main(["--config", RECORDED_CONFIG, "--offline",
                                 "--out-dir", d],
                                log=io.StringIO())
            self.assertEqual(code, cli_mod.EXIT_OK)
            self.assertTrue(os.path.exists(os.path.join(d, "schedule.ics")))

    def test_a_mistyped_flag_is_not_reported_as_a_fetch_failure(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli_mod.main(["--no-such-flag"])
        self.assertEqual(caught.exception.code, cli_mod.EXIT_CONFIG)
        self.assertNotEqual(caught.exception.code, cli_mod.EXIT_FETCH)


class TestInjectedConfigWins(unittest.TestCase):
    def test_a_passed_config_is_used_instead_of_any_file(self):
        cfg = demo_config()
        cfg.calendar_name = "Injected"
        import io
        with tempfile.TemporaryDirectory() as d:
            code = cli_mod.main(["--out-dir", d, "--offline"],
                                cfg=cfg, log=io.StringIO())
            self.assertEqual(code, cli_mod.EXIT_OK)
            with open(os.path.join(d, "schedule.ics"), encoding="utf-8") as fh:
                self.assertIn("X-WR-CALNAME:Injected", fh.read())


if __name__ == "__main__":
    unittest.main()
