"""state.json round-tripping and the --diff report."""

import json
import os
import shutil
import tempfile
import unittest

from courseics import state as state_mod
from courseics.parse import CERTAIN, UNCERTAIN, Record, assign_uids

URL = "https://example.invalid/lectures"


def rec(date, title, kind="lecture", confidence=CERTAIN, time=None):
    return Record(date=date, title=title, kind=kind, source_url=URL,
                  confidence=confidence, time=time, context="ctx")


def uidify(records):
    records = list(records)
    assign_uids(records)
    return records


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_file_gives_empty_state(self):
        st = state_mod.load(self.path)
        self.assertEqual(st["records"], [])
        self.assertIsNone(st["last_successful_run"])

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(state_mod.load(self.path)["records"], [])

    def test_round_trip(self):
        records = uidify([rec("2026-09-02", "Intro")])
        st = state_mod.empty_state()
        st["records"] = [r.to_dict() for r in records]
        st["last_successful_run"] = "2026-09-02T12:00:00+00:00"
        state_mod.save(self.path, st)
        back = state_mod.load(self.path)
        self.assertEqual(back["last_successful_run"],
                         "2026-09-02T12:00:00+00:00")
        got = state_mod.records_from_state(back)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].uid, records[0].uid)
        self.assertEqual(got[0].title, "Intro")

    def test_caching_metadata_is_stored(self):
        st = state_mod.empty_state()
        st["sources"][URL] = {"etag": '"abc"', "last_modified": "Tue, 01 Sep 2026 00:00:00 GMT"}
        state_mod.save(self.path, st)
        caching = state_mod.caching_for(state_mod.load(self.path), URL)
        self.assertEqual(caching["etag"], '"abc"')
        self.assertTrue(caching["last_modified"].startswith("Tue"))

    def test_unknown_url_yields_blank_caching(self):
        caching = state_mod.caching_for(state_mod.empty_state(), URL)
        self.assertEqual(caching, {"etag": "", "last_modified": ""})

    def test_save_is_valid_json(self):
        state_mod.save(self.path, state_mod.empty_state())
        with open(self.path) as fh:
            json.load(fh)


class TestTypeLevelCorruption(unittest.TestCase):
    """Valid JSON is not a valid state file.

    README: "corrupt it and the next run treats it as empty rather than
    crashing." Syntactic corruption was handled; structural corruption was
    not. {"records": null} and {"sources": "nope"} parsed cleanly and then
    raised a TypeError or ValueError several modules away, at exit 1 -- the
    one moment the tool most needs to still run.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _load(self, payload):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return state_mod.load(self.path)

    def _usable(self, st):
        """Everything the rest of the program does with a loaded state."""
        list(state_mod.records_from_state(st))
        state_mod.caching_for(st, URL)
        for entry in st["sources"].values():
            entry.get("record_count")
        return st

    def test_records_null(self):
        self.assertEqual(self._usable(self._load({"records": None}))["records"],
                         [])

    def test_records_is_a_number(self):
        self.assertEqual(self._usable(self._load({"records": 5}))["records"],
                         [])

    def test_records_is_a_string(self):
        self.assertEqual(self._usable(self._load({"records": "42"}))["records"],
                         [])

    def test_sources_is_a_string(self):
        st = self._usable(self._load({"sources": "nope"}))
        self.assertEqual(st["sources"], {})
        self.assertEqual(state_mod.caching_for(st, URL),
                         {"etag": "", "last_modified": ""})

    def test_sources_is_a_list(self):
        self.assertEqual(self._usable(self._load({"sources": [1, 2]}))["sources"],
                         {})

    def test_a_source_entry_is_not_an_object(self):
        st = self._usable(self._load({"sources": {URL: "etag-ish"}}))
        self.assertEqual(st["sources"], {})

    def test_non_dict_records_are_dropped_individually(self):
        good = uidify([rec("2026-09-02", "Intro")])[0].to_dict()
        st = self._usable(self._load({"records": [1, "two", None, good]}))
        self.assertEqual(len(st["records"]), 1)
        self.assertEqual(len(state_mod.records_from_state(st)), 1)

    def test_top_level_is_a_list(self):
        self.assertEqual(self._usable(self._load([1, 2, 3]))["records"], [])

    def test_top_level_is_a_scalar(self):
        self.assertEqual(self._usable(self._load(42))["records"], [])

    def test_wrong_typed_timestamps_fall_back_to_none(self):
        st = self._usable(self._load({"last_successful_run": 7,
                                      "last_attempted_run": ["nope"]}))
        self.assertIsNone(st["last_successful_run"])
        self.assertIsNone(st["last_attempted_run"])

    def test_wrong_typed_version_falls_back(self):
        st = self._usable(self._load({"version": "one"}))
        self.assertEqual(st["version"], state_mod.STATE_VERSION)

    def test_unknown_keys_are_carried_through(self):
        st = self._load({"records": [], "future_field": {"a": 1}})
        self.assertEqual(st["future_field"], {"a": 1})

    def test_a_corrupt_state_still_produces_a_calendar(self):
        """End to end: the run must succeed, not exit 1."""
        import contextlib
        import io as _io
        from courseics.cli import EXIT_OK, main
        from courseics.fetch import StringFetcher
        from tests import demo_config
        cfg = demo_config()
        lec, asg = [src.url for src in cfg.sources]
        for payload in ({"records": None}, {"records": 5},
                        {"sources": "nope"}, [1, 2, 3]):
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            with contextlib.redirect_stderr(_io.StringIO()):
                code = main(["--out-dir", self.dir],
                            fetcher=StringFetcher(
                                {lec: "<p>Sep. 02, 2026 Intro</p>",
                                 asg: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}),
                            log=_io.StringIO(), cfg=demo_config())
            self.assertEqual(code, EXIT_OK, payload)
            self.assertTrue(os.path.exists(
                os.path.join(self.dir, "schedule.ics")), payload)


class TestFieldLevelCorruption(unittest.TestCase):
    """Structural validation stopped at the container.

    `records` being a list and `sources` being a dict were checked; the fields
    inside a single record were not. One mistyped field crashed the tool at
    exit 1 -- and one of them, `time`, did it on `--diff`, the everyday path.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _load_with(self, **overrides):
        """One good record plus one carrying the given field values."""
        good = uidify([rec("2026-09-02", "Intro")])[0].to_dict()
        bad = uidify([rec("2026-09-09", "Second")])[0].to_dict()
        bad.update(overrides)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": [good, bad]}, fh)
        return state_mod.load(self.path)

    def _only_the_good_one_survives(self, **overrides):
        st = self._load_with(**overrides)
        self.assertEqual([r["title"] for r in st["records"]], ["Intro"])
        # And it must survive every reader, not just the loader.
        records = state_mod.records_from_state(st)
        self.assertEqual(len(records), 1)
        state_mod.format_diff(state_mod.diff([], records), 0, 1)

    def test_time_is_a_number(self):
        self._only_the_good_one_survives(time=5)

    def test_time_is_not_a_clock(self):
        self._only_the_good_one_survives(time="tea time")

    def test_time_is_out_of_range(self):
        self._only_the_good_one_survives(time="25:00")

    def test_date_is_null(self):
        self._only_the_good_one_survives(date=None)

    def test_date_is_a_number(self):
        self._only_the_good_one_survives(date=5)

    def test_date_is_junk(self):
        self._only_the_good_one_survives(date="junk")

    def test_date_is_not_a_real_calendar_day(self):
        self._only_the_good_one_survives(date="2026-02-30")

    def test_date_is_a_different_format(self):
        self._only_the_good_one_survives(date="20260909")

    def test_title_is_a_number(self):
        self._only_the_good_one_survives(title=5)

    def test_kind_is_a_number(self):
        self._only_the_good_one_survives(kind=5)

    def test_source_url_is_a_number(self):
        self._only_the_good_one_survives(source_url=5)

    def test_confidence_is_a_number(self):
        self._only_the_good_one_survives(confidence=5)

    def test_context_is_a_number(self):
        self._only_the_good_one_survives(context=5)

    def test_uid_is_a_number(self):
        self._only_the_good_one_survives(uid=5)

    def test_reasons_is_a_string(self):
        self._only_the_good_one_survives(reasons="one big reason")

    def test_reasons_holds_a_non_string(self):
        self._only_the_good_one_survives(reasons=["fine", 7])

    def test_a_missing_required_field_is_dropped(self):
        good = uidify([rec("2026-09-02", "Intro")])[0].to_dict()
        bad = uidify([rec("2026-09-09", "Second")])[0].to_dict()
        del bad["kind"]
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": [good, bad]}, fh)
        st = state_mod.load(self.path)
        self.assertEqual([r["title"] for r in st["records"]], ["Intro"])

    def test_an_all_day_record_keeps_its_null_time(self):
        st = self._load_with(time=None)
        self.assertEqual(len(st["records"]), 2)

    def test_a_one_digit_hour_is_accepted(self):
        """`Config.default_due_time` is a string a user may set to "9:00"."""
        st = self._load_with(time="9:00")
        self.assertEqual(len(st["records"]), 2)

    def test_an_absent_optional_field_is_not_corruption(self):
        good = uidify([rec("2026-09-02", "Intro")])[0].to_dict()
        for key in ("context", "uid", "reasons", "time"):
            payload = dict(good)
            payload.pop(key)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"records": [payload]}, fh)
            self.assertEqual(len(state_mod.load(self.path)["records"]), 1, key)

    def test_a_sound_record_is_untouched(self):
        good = uidify([rec("2026-09-02", "Intro", time="23:59")])[0].to_dict()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": [good]}, fh)
        self.assertEqual(state_mod.load(self.path)["records"], [good])

    def test_unknown_record_keys_are_carried_through(self):
        good = uidify([rec("2026-09-02", "Intro")])[0].to_dict()
        good["future_field"] = {"a": 1}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": [good]}, fh)
        st = state_mod.load(self.path)
        self.assertEqual(st["records"][0]["future_field"], {"a": 1})


class TestDiff(unittest.TestCase):
    def test_no_change(self):
        old = uidify([rec("2026-09-02", "Intro")])
        new = uidify([rec("2026-09-02", "Intro")])
        d = state_mod.diff(old, new)
        self.assertEqual([len(v) for v in
                          (d["added"], d["removed"], d["moved"], d["changed"])],
                         [0, 0, 0, 0])
        self.assertIn("No changes", state_mod.format_diff(d, 1, 1))

    def test_added(self):
        old = uidify([rec("2026-09-02", "Intro")])
        new = uidify([rec("2026-09-02", "Intro"), rec("2026-09-09", "Frames")])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(d["added"][0].title, "Frames")
        self.assertIn("NEW (1)", state_mod.format_diff(d, 1, 2))

    def test_date_moved_is_not_reported_as_add_plus_remove(self):
        # A deadline, not a lecture: an assignment is the kind whose identity
        # is its title, so it is the kind for which a date change is an edit.
        # A lecture keyed by its date reads a date change as a replacement --
        # see test_a_moved_lecture_is_a_replacement below.
        old = uidify([rec("2026-09-02", "A1", "assignment_due")])
        new = uidify([rec("2026-10-20", "A1", "assignment_due")])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["added"]), 0)
        self.assertEqual(len(d["removed"]), 0)
        self.assertEqual(len(d["moved"]), 1)
        report = state_mod.format_diff(d, 1, 1)
        self.assertIn("DATE CHANGED", report)
        self.assertIn("was 2026-09-02", report)
        self.assertIn("now 2026-10-20", report)

    def test_a_retitled_lecture_is_an_edit_not_a_replacement(self):
        # 2026-09-09, from the real page: one slot, retitled in place. The
        # report has to name the old title, because the whole value of keeping
        # the UID is that the reader can see it was the same event.
        old = uidify([rec("2026-09-02",
                          "Introduction, History, and Architectures")])
        new = uidify([rec("2026-09-02", "Introduction, and History")])
        d = state_mod.diff(old, new)
        self.assertEqual([len(v) for v in
                          (d["added"], d["removed"], d["moved"], d["changed"])],
                         [0, 0, 0, 1])
        report = state_mod.format_diff(d, 1, 1)
        self.assertIn("DETAILS CHANGED", report)
        self.assertIn("Introduction, History, and Architectures", report)
        self.assertIn("Introduction, and History", report)
        self.assertNotIn("DISAPPEARED", report)
        self.assertNotIn("NEW (", report)

    def test_a_moved_lecture_is_a_replacement(self):
        # The accepted cost of the date key, asserted rather than assumed: a
        # lecture that changes day is a different slot. DESIGN.md section 14.
        old = uidify([rec("2026-09-09", "Architectures")])
        new = uidify([rec("2026-09-11", "Architectures")])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["moved"]), 0)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(len(d["removed"]), 1)

    def test_a_lecture_retitled_and_moved_to_another_hour_reports_both(self):
        # A lecture's UID carries its date but not its clock time, so this is
        # the one input that lands in `moved` AND `changed`. Reporting only
        # half of it would hide a real edit.
        old = uidify([rec("2026-09-02", "Intro", time="13:00")])
        new = uidify([rec("2026-09-02", "Intro and History", time="14:00")])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["moved"]), 1)
        self.assertEqual(len(d["changed"]), 1)
        report = state_mod.format_diff(d, 1, 1)
        self.assertIn("DATE CHANGED", report)
        self.assertIn("DETAILS CHANGED", report)

    def test_removed(self):
        old = uidify([rec("2026-09-02", "Intro"), rec("2026-09-09", "Frames")])
        new = uidify([rec("2026-09-02", "Intro")])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["removed"]), 1)
        self.assertIn("DISAPPEARED (1)", state_mod.format_diff(d, 2, 1))

    def test_confidence_change_is_reported(self):
        old = uidify([rec("2026-09-02", "Intro")])
        new = uidify([rec("2026-09-02", "Intro", confidence=UNCERTAIN)])
        d = state_mod.diff(old, new)
        self.assertEqual(len(d["changed"]), 1)
        self.assertIn("confidence certain -> uncertain",
                      state_mod.format_diff(d, 1, 1))

    def test_time_change_counts_as_moved(self):
        old = uidify([rec("2026-09-13", "A1", "assignment_due", time="23:59")])
        new = uidify([rec("2026-09-13", "A1", "assignment_due", time="17:00")])
        self.assertEqual(len(state_mod.diff(old, new)["moved"]), 1)

    def test_first_run_reports_everything_as_new(self):
        new = uidify([rec("2026-09-02", "Intro"), rec("2026-09-09", "Frames")])
        d = state_mod.diff([], new)
        self.assertEqual(len(d["added"]), 2)


if __name__ == "__main__":
    unittest.main()
