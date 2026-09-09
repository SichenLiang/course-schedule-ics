"""Fetch injection, politeness, and the failure-alarm exit codes.

Everything here runs against injected fetchers. tests/__init__.py has already
disabled sockets, so a regression that reaches for the network fails loudly.
"""

import contextlib
import email.message
import http.client
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error

from tests import FIXTURES, RECORDED_CONFIG, NetworkAccessDenied, demo_config
from courseics import cli as cli_mod
from courseics import config as config_mod
from courseics import state as state_mod
from courseics.parse import Record
from courseics.cli import (EXIT_CONFIG, EXIT_EMPTY, EXIT_FETCH, EXIT_OK,
                           EXIT_SHRANK, EXIT_STALE, EXIT_WRITE)
from courseics.fetch import (FetchError, FetchResult, Fetcher, FixtureFetcher,
                           HttpFetcher, StringFetcher)

# The URLs of the two recorded pages, as `examples/recorded-course.ini` names
# them. Read from the file rather than retyped, so the constants and the config
# the tests run against cannot drift apart.
LEC, ASG = [src.url for src in demo_config().sources]


def main(argv, **kwargs):
    """`cli.main`, with the recorded course supplied.

    No course is compiled into the program any more -- a real run reads one
    from a config file -- so every CLI test has to say which one it means.
    Injecting it here rather than at each of the call sites below keeps the
    tests about the behaviour they were written for. Tests that are ABOUT
    configuration call `cli_mod.main` directly.
    """
    kwargs.setdefault("cfg", demo_config())
    return cli_mod.main(argv, **kwargs)


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@contextlib.contextmanager
def quiet_stderr():
    """The alarm path writes to the real stderr on purpose; hush it in tests."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


class RecordingFetcher(Fetcher):
    """Returns canned results and remembers the caching headers it was given."""

    def __init__(self, results):
        self.results = results
        self.seen = []

    def fetch(self, url, caching=None):
        self.seen.append((url, dict(caching or {})))
        out = self.results[url]
        if isinstance(out, Exception):
            raise out
        return out


class BoomFetcher(Fetcher):
    def fetch(self, url, caching=None):
        raise FetchError("HTTP 503 for %s" % url)


class TestNetworkIsBlocked(unittest.TestCase):
    def test_sockets_raise(self):
        with self.assertRaises(NetworkAccessDenied):
            socket.socket()

    def test_dns_raises(self):
        with self.assertRaises(NetworkAccessDenied):
            socket.getaddrinfo("example.com", 80)


class TestFixtureFetcher(unittest.TestCase):
    def test_serves_recorded_page(self):
        f = FixtureFetcher({LEC: "lectures.html"}, base_dir=FIXTURES)
        res = f.fetch(LEC)
        self.assertEqual(res.status, 200)
        self.assertIn("Lecture Schedule", res.body)

    def test_unknown_url_is_an_error(self):
        f = FixtureFetcher({}, base_dir=FIXTURES)
        with self.assertRaises(FetchError):
            f.fetch("https://example.invalid/nope")

    def test_missing_file_is_an_error(self):
        f = FixtureFetcher({LEC: "does-not-exist.html"}, base_dir=FIXTURES)
        with self.assertRaises(FetchError):
            f.fetch(LEC)


class TestPoliteness(unittest.TestCase):
    def test_user_agent_identifies_the_script_and_a_way_to_reach_someone(self):
        ua = config_mod.USER_AGENT
        self.assertIn("course-schedule-ics", ua)
        self.assertIn("personal", ua.lower())
        self.assertIn("not a crawler", ua)
        # The contact address belongs to whoever is running the tool, so it is
        # configuration rather than a constant -- but an unattributed scraper
        # is the kind a site operator blocks first, so the project URL is
        # always there whether or not an address has been configured.
        self.assertIn("https://github.com/", ua)

    def test_a_configured_contact_reaches_the_user_agent(self):
        cfg = config_mod.Config(contact="someone@example.edu")
        self.assertIn("someone@example.edu", cfg.user_agent)
        self.assertIn("course-schedule-ics", cfg.user_agent)

    def test_the_fetcher_sends_the_configured_user_agent(self):
        cfg = config_mod.Config(contact="someone@example.edu")
        seen = {}

        class _Resp(object):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def getcode(self_inner):
                return 200

            def read(self_inner):
                return b"<p>hi</p>"

            headers = email.message.Message()

        def opener(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return _Resp()

        f = HttpFetcher(opener=opener, user_agent=cfg.user_agent)
        f.fetch("https://example.invalid/page")
        self.assertEqual(seen["ua"], cfg.user_agent)

    def test_minimum_interval_is_at_least_one_second(self):
        self.assertGreaterEqual(config_mod.MIN_REQUEST_INTERVAL, 1.0)

    def test_http_fetcher_sleeps_between_requests(self):
        slept = []
        f = HttpFetcher(min_interval=1.0, sleeper=slept.append)
        # No request has happened yet, so the first turn is free.
        f._wait_turn()
        self.assertEqual(slept, [])
        import time
        f._last_request_at = time.monotonic()
        f._wait_turn()
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.0)
        self.assertLessEqual(slept[0], 1.0)

    def test_conditional_headers_are_sent_when_state_has_them(self):
        rf = RecordingFetcher({
            LEC: FetchResult(LEC, 200, "<p>Sep. 02, 2026 Intro</p>"),
            ASG: FetchResult(ASG, 200, "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"),
        })
        with TempDir() as d:
            st = state_mod.empty_state()
            st["sources"][LEC] = {"etag": '"v1"', "last_modified": "GMT-ish"}
            state_mod.save(os.path.join(d, "state.json"), st)
            main(["--out-dir", d], fetcher=rf, log=io.StringIO())
        sent = dict((u, c) for u, c in rf.seen)
        self.assertEqual(sent[LEC]["etag"], '"v1"')
        self.assertEqual(sent[ASG]["etag"], "")

    def test_caching_metadata_is_persisted(self):
        rf = RecordingFetcher({
            LEC: FetchResult(LEC, 200, "<p>Sep. 02, 2026 Intro</p>",
                             etag='"new"', last_modified="Mon, 01 Sep 2026"),
            ASG: FetchResult(ASG, 200, "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"),
        })
        with TempDir() as d:
            main(["--out-dir", d], fetcher=rf, log=io.StringIO())
            st = read_json(os.path.join(d, "state.json"))
        self.assertEqual(st["sources"][LEC]["etag"], '"new"')
        self.assertEqual(st["sources"][LEC]["last_modified"], "Mon, 01 Sep 2026")
        self.assertEqual(st["sources"][LEC]["status"], 200)


class TempDir(object):
    def __enter__(self):
        self.path = tempfile.mkdtemp()
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


class TestHttpErrorClassification(unittest.TestCase):
    """Network failures that urllib does not wrap in a URLError.

    urllib only wraps the connect/request phase. A read timeout, a server that
    hangs up mid-answer, or a reset after the connection is open all escape
    raw. Uncaught they produced a traceback and exit 1 -- "unexpected internal
    error, read the traceback" -- for the most ordinary thing a network does.
    README promises exit 2 for a fetch failure; these are fetch failures.

    No socket is opened: the opener is injected, exactly as the fetcher is.
    """

    def _raising(self, exc):
        def opener(req, timeout=None):
            raise exc
        return HttpFetcher(min_interval=0.0, sleeper=lambda s: None,
                           opener=opener)

    def _fetch_error(self, exc):
        with self.assertRaises(FetchError) as caught:
            self._raising(exc).fetch(LEC)
        return str(caught.exception)

    def test_socket_timeout_is_a_fetch_error(self):
        msg = self._fetch_error(socket.timeout("timed out"))
        self.assertIn("timed out", msg)
        self.assertIn(LEC, msg)

    def test_builtin_timeout_error_is_a_fetch_error(self):
        self.assertIn("timed out", self._fetch_error(TimeoutError("slow")))

    def test_remote_disconnected_is_a_fetch_error(self):
        msg = self._fetch_error(
            http.client.RemoteDisconnected("Remote end closed connection"))
        self.assertIn("closed the connection", msg)
        self.assertIn("RemoteDisconnected", msg)

    def test_incomplete_read_is_a_fetch_error(self):
        msg = self._fetch_error(http.client.IncompleteRead(b"half"))
        self.assertIn("IncompleteRead", msg)

    def test_connection_reset_is_a_fetch_error(self):
        msg = self._fetch_error(ConnectionResetError("reset by peer"))
        self.assertIn("ConnectionResetError", msg)

    def test_url_error_still_reports_its_reason(self):
        msg = self._fetch_error(urllib.error.URLError("nodename nor servname"))
        self.assertIn("nodename", msg)

    def test_http_500_is_still_a_fetch_error(self):
        msg = self._fetch_error(
            urllib.error.HTTPError(LEC, 500, "Server Error", {}, None))
        self.assertIn("HTTP 500", msg)

    def test_a_dropped_connection_exits_two_not_one(self):
        """The end-to-end promise: no traceback, exit 2, a readable message."""
        def opener(req, timeout=None):
            raise http.client.RemoteDisconnected("Remote end closed connection")
        fetcher = HttpFetcher(min_interval=0.0, sleeper=lambda s: None,
                              opener=opener)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with TempDir() as d:
                code = main(["--out-dir", d], fetcher=fetcher,
                            log=io.StringIO())
        self.assertEqual(code, EXIT_FETCH)
        self.assertIn("FAILED", err.getvalue())
        self.assertIn("closed the connection", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_timeout_exits_two_not_one(self):
        def opener(req, timeout=None):
            raise socket.timeout("timed out")
        fetcher = HttpFetcher(min_interval=0.0, sleeper=lambda s: None,
                              opener=opener)
        with contextlib.redirect_stderr(io.StringIO()):
            with TempDir() as d:
                code = main(["--out-dir", d], fetcher=fetcher,
                            log=io.StringIO())
        self.assertEqual(code, EXIT_FETCH)


class TestNotModified(unittest.TestCase):
    def test_304_reuses_stored_records(self):
        with TempDir() as d:
            fixtures = FixtureFetcher(
                {LEC: "lectures.html", ASG: "assignments.html"},
                base_dir=FIXTURES)
            self.assertEqual(main(["--out-dir", d], fetcher=fixtures,
                                  log=io.StringIO()), EXIT_OK)
            first = read_json(os.path.join(d, "state.json"))

            unchanged = RecordingFetcher({
                LEC: FetchResult(LEC, 304, None),
                ASG: FetchResult(ASG, 304, None),
            })
            self.assertEqual(main(["--out-dir", d], fetcher=unchanged,
                                  log=io.StringIO()), EXIT_OK)
            second = read_json(os.path.join(d, "state.json"))
        self.assertEqual(len(first["records"]), len(second["records"]))
        self.assertEqual([r["uid"] for r in first["records"]],
                         [r["uid"] for r in second["records"]])


class AlwaysNotModified(Fetcher):
    """A server that answers 304 no matter what it is asked."""

    def __init__(self):
        self.seen = []

    def fetch(self, url, caching=None):
        self.seen.append(dict(caching or {}))
        return FetchResult(url, 304, None)


class TestParseAffectingOptions(unittest.TestCase):
    """A 304 skips parsing; --fall-year is applied during parsing.

    So a student starting a new semester ran --fall-year 2027, was told "304
    Not Modified, no changes", got exit 0 -- and kept a calendar full of the
    previous year's deadlines, with nothing anywhere saying the flag had been
    ignored. The page had not changed; the question being asked of it had.
    """

    def _fixtures(self):
        return FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                              base_dir=FIXTURES)

    def _seed(self, d):
        with quiet_stderr():
            main(["--out-dir", d], fetcher=self._fixtures(), log=io.StringIO())

    def test_unchanged_options_still_use_conditional_gets(self):
        with TempDir() as d:
            self._seed(d)
            server = AlwaysNotModified()
            with quiet_stderr():
                code = main(["--out-dir", d], fetcher=server,
                            log=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(server.seen[0]["etag"], '"fixture"')

    def test_a_changed_anchor_forces_an_unconditional_fetch(self):
        with TempDir() as d:
            self._seed(d)
            server = AlwaysNotModified()
            with quiet_stderr():
                main(["--out-dir", d, "--fall-year", "2027"], fetcher=server,
                     log=io.StringIO())
        self.assertEqual(server.seen[0], {})

    def test_a_changed_anchor_actually_moves_the_dates(self):
        with TempDir() as d:
            self._seed(d)
            log = io.StringIO()
            with quiet_stderr():
                code = main(["--out-dir", d, "--fall-year", "2027"],
                            fetcher=self._fixtures(), log=log)
            st = read_json(os.path.join(d, "state.json"))
        self.assertEqual(code, EXIT_OK)
        dues = [r["date"] for r in st["records"]
                if r["kind"] == "assignment_due"]
        self.assertTrue(dues)
        self.assertTrue(all(d.startswith("2027") for d in dues), dues)
        self.assertIn("parse options changed", log.getvalue())

    def test_a_stubborn_304_fails_instead_of_publishing_stale_years(self):
        with TempDir() as d:
            self._seed(d)
            before = read_text(os.path.join(d, "schedule.ics"))
            with quiet_stderr() as err:
                code = main(["--out-dir", d, "--fall-year", "2027"],
                            fetcher=AlwaysNotModified(), log=io.StringIO())
            after = read_text(os.path.join(d, "schedule.ics"))
        self.assertEqual(code, EXIT_STALE)
        self.assertEqual(after, before)
        self.assertIn("304 Not Modified", err.getvalue())
        self.assertIn("fall-2027", err.getvalue())
        self.assertIn("delete state.json", err.getvalue())

    def test_a_stubborn_304_is_not_reported_as_a_fetch_failure(self):
        """Code 2 and code 6 call for opposite actions.

        Code 2 means the network or the site is the problem: check
        connectivity. This is the opposite -- the fetch worked perfectly, and
        the fix is on the user's own disk. They shared code 2, so the reader
        was sent to inspect a connection that had demonstrably just worked.
        README made it stranger still by defining code 2 as "HTTP other than
        200/304" for a failure that fires only on receiving a 304.
        """
        with TempDir() as d:
            self._seed(d)
            with quiet_stderr() as err:
                code = main(["--out-dir", d, "--fall-year", "2027"],
                            fetcher=AlwaysNotModified(), log=io.StringIO())
            report = err.getvalue()
        self.assertNotEqual(code, EXIT_FETCH)
        self.assertEqual(code, EXIT_STALE)
        self.assertIn("network is not the problem", report)

    def test_a_real_fetch_failure_still_exits_two(self):
        """The other half of the split must not have moved."""
        with TempDir() as d:
            with quiet_stderr():
                code = main(["--out-dir", d], fetcher=BoomFetcher(),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_FETCH)

    def test_the_signature_is_persisted_and_readable(self):
        with TempDir() as d:
            self._seed(d)
            st = read_json(os.path.join(d, "state.json"))
        self.assertIn("anchor=fall-2026", st["parse_signature"])

    def test_alarm_days_does_not_force_a_reparse(self):
        """It changes the ICS, not the parse, so a 304 is still safe to reuse."""
        with TempDir() as d:
            self._seed(d)
            server = AlwaysNotModified()
            with quiet_stderr():
                code = main(["--out-dir", d, "--alarm-days", "7"],
                            fetcher=server, log=io.StringIO())
            ics = read_text(os.path.join(d, "schedule.ics"))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(server.seen[0]["etag"], '"fixture"')
        self.assertIn("TRIGGER:-P7D", ics)

    def test_a_state_file_with_no_signature_re_parses_once(self):
        with TempDir() as d:
            self._seed(d)
            path = os.path.join(d, "state.json")
            st = read_json(path)
            st.pop("parse_signature", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            server = AlwaysNotModified()
            with quiet_stderr():
                main(["--out-dir", d], fetcher=server, log=io.StringIO())
        self.assertEqual(server.seen[0], {})


class TestCorruptRecordFieldsDoNotCrashTheRun(unittest.TestCase):
    """A mistyped field inside one stored record used to exit 1.

    Exit 1 means "a bug in this program, read the traceback", which sent the
    reader looking for a defect that was not there. And the worst of them,
    `time`, fired on `--diff` -- the command the README puts in the quick
    start -- via `rec.date + " " + rec.time` in state._stamp.
    """

    PAGES = {LEC: "<p>Sep. 02, 2026 Intro</p><p>Sep. 09, 2026 Second</p>",
             ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}

    # Every corruption the audit reached a crash with, plus the two that
    # needed a second record of a different type to trip the diff's sort.
    CORRUPTIONS = (
        ("time is an int", {"time": 5}),
        ("time is not a clock", {"time": "noon"}),
        ("date is null", {"date": None}),
        ("date is junk", {"date": "junk"}),
        ("date is an int", {"date": 5}),
        ("title is an int", {"title": 5}),
        ("kind is an int", {"kind": 5}),
        ("source_url is an int", {"source_url": 5}),
        ("confidence is an int", {"confidence": 5}),
        ("reasons is a string", {"reasons": "why"}),
        ("uid is an int", {"uid": 5}),
        ("context is an int", {"context": 5}),
    )

    def _seed(self, d):
        with quiet_stderr():
            main(["--out-dir", d], fetcher=StringFetcher(self.PAGES),
                 log=io.StringIO())

    def _corrupt(self, d, overrides, every=False):
        path = os.path.join(d, "state.json")
        st = read_json(path)
        targets = st["records"] if every else st["records"][:1]
        for record in targets:
            record.update(overrides)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(st, fh)

    def test_diff_survives_every_corrupt_field(self):
        for label, overrides in self.CORRUPTIONS:
            with self.subTest(label):
                with TempDir() as d:
                    self._seed(d)
                    self._corrupt(d, overrides)
                    with quiet_stderr(), \
                            contextlib.redirect_stdout(io.StringIO()):
                        code = main(["--out-dir", d, "--diff"],
                                    fetcher=StringFetcher(self.PAGES),
                                    log=io.StringIO())
                self.assertEqual(code, EXIT_OK, label)

    def test_a_304_survives_every_corrupt_field(self):
        """The 304 path publishes the stored records without re-parsing."""
        for label, overrides in self.CORRUPTIONS:
            with self.subTest(label):
                with TempDir() as d:
                    self._seed(d)
                    self._corrupt(d, overrides)
                    with quiet_stderr():
                        code = main(["--out-dir", d],
                                    fetcher=AlwaysNotModified(),
                                    log=io.StringIO())
                    ics = read_text(os.path.join(d, "schedule.ics"))
                self.assertEqual(code, EXIT_OK, label)
                self.assertTrue(ics.rstrip().endswith("END:VCALENDAR"), label)

    def test_mixed_types_across_records_do_not_break_the_diff_sort(self):
        """Two dropped records of different types is what tripped the sort."""
        for field in ("date", "title"):
            with self.subTest(field):
                with TempDir() as d:
                    self._seed(d)
                    self._corrupt(d, {field: 5}, every=True)
                    with quiet_stderr(), \
                            contextlib.redirect_stdout(io.StringIO()) as out:
                        code = main(["--out-dir", d, "--diff"],
                                    fetcher=StringFetcher(self.PAGES),
                                    log=io.StringIO())
                self.assertEqual(code, EXIT_OK, field)
                self.assertIn("NEW", out.getvalue())

    def test_the_run_is_never_reported_as_an_internal_error(self):
        """Exit 1 says "bug in this program". A bad state file is not that."""
        for label, overrides in self.CORRUPTIONS:
            with self.subTest(label):
                with TempDir() as d:
                    self._seed(d)
                    self._corrupt(d, overrides, every=True)
                    with quiet_stderr(), \
                            contextlib.redirect_stdout(io.StringIO()):
                        code = main(["--out-dir", d, "--diff", "--list"],
                                    fetcher=StringFetcher(self.PAGES),
                                    log=io.StringIO())
                self.assertNotEqual(code, 1, label)

    def test_the_sound_records_are_kept_not_the_whole_file_discarded(self):
        with TempDir() as d:
            self._seed(d)
            seeded = len(read_json(os.path.join(d, "state.json"))["records"])
            self._corrupt(d, {"time": 5})
            with quiet_stderr(), contextlib.redirect_stdout(io.StringIO()):
                main(["--out-dir", d, "--diff"], fetcher=AlwaysNotModified(),
                     log=io.StringIO())
            surviving = read_json(os.path.join(d, "state.json"))["records"]
        # One bad row costs one row, not the whole file.
        self.assertEqual(len(surviving), seeded - 1)
        self.assertGreater(len(surviving), 0)


class TestExitCodes(unittest.TestCase):
    """Every method here exercises the alarm path, which writes to the real
    stderr by design. Capture it so a passing run stays readable."""

    def setUp(self):
        self._redirect = contextlib.redirect_stderr(io.StringIO())
        self.stderr = self._redirect.__enter__()
        self.addCleanup(self._redirect.__exit__, None, None, None)

    def test_success_is_zero(self):
        with TempDir() as d:
            f = FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                               base_dir=FIXTURES)
            self.assertEqual(main(["--out-dir", d], fetcher=f,
                                  log=io.StringIO()), EXIT_OK)
            self.assertTrue(os.path.exists(os.path.join(d, "schedule.ics")))
            self.assertTrue(os.path.exists(os.path.join(d, "state.json")))

    def test_fetch_failure_exits_two(self):
        with TempDir() as d:
            code = main(["--out-dir", d], fetcher=BoomFetcher(),
                        log=io.StringIO())
        self.assertEqual(code, EXIT_FETCH)

    def test_failure_explains_itself_on_stderr(self):
        with TempDir() as d:
            main(["--out-dir", d], fetcher=BoomFetcher(), log=io.StringIO())
        msg = self.stderr.getvalue()
        self.assertIn("FAILED", msg)
        self.assertIn("503", msg)
        self.assertIn("last successful run", msg)

    def test_empty_parse_names_the_likely_cause(self):
        empty = StringFetcher({LEC: "<p>nothing</p>", ASG: "<p>nothing</p>"})
        with TempDir() as d:
            main(["--out-dir", d], fetcher=empty, log=io.StringIO())
        msg = self.stderr.getvalue()
        self.assertIn("parsed 0 records", msg)
        self.assertIn("Refusing to overwrite", msg)

    def test_zero_records_exits_three(self):
        empty = StringFetcher({LEC: "<p>No dates here at all</p>",
                               ASG: "<p>Nor here</p>"})
        with TempDir() as d:
            self.assertEqual(main(["--out-dir", d], fetcher=empty,
                                  log=io.StringIO()), EXIT_EMPTY)

    def test_zero_records_does_not_overwrite_the_calendar(self):
        with TempDir() as d:
            good = FixtureFetcher({LEC: "lectures.html",
                                   ASG: "assignments.html"}, base_dir=FIXTURES)
            main(["--out-dir", d], fetcher=good, log=io.StringIO())
            before = read_text(os.path.join(d, "schedule.ics"))
            empty = StringFetcher({LEC: "<p>nothing</p>", ASG: "<p>nothing</p>"})
            main(["--out-dir", d], fetcher=empty, log=io.StringIO())
            after = read_text(os.path.join(d, "schedule.ics"))
        self.assertEqual(before, after)

    def test_big_shrink_exits_four(self):
        with TempDir() as d:
            good = FixtureFetcher({LEC: "lectures.html",
                                   ASG: "assignments.html"}, base_dir=FIXTURES)
            main(["--out-dir", d], fetcher=good, log=io.StringIO())
            tiny = StringFetcher({LEC: "<p>Sep. 02, 2026 Intro</p>",
                                  ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"})
            code = main(["--out-dir", d], fetcher=tiny, log=io.StringIO())
        self.assertEqual(code, EXIT_SHRANK)

    def test_small_shrink_is_allowed(self):
        with TempDir() as d:
            first = StringFetcher({
                LEC: "<p>Sep. 02, 2026 A</p><p>Sep. 09, 2026 B</p>"
                     "<p>Sep. 16, 2026 C</p><p>Sep. 23, 2026 D</p>",
                ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"})
            main(["--out-dir", d], fetcher=first, log=io.StringIO())
            second = StringFetcher({
                LEC: "<p>Sep. 02, 2026 A</p><p>Sep. 09, 2026 B</p>"
                     "<p>Sep. 16, 2026 C</p>",
                ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"})
            code = main(["--out-dir", d], fetcher=second, log=io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_failure_records_last_attempt_but_keeps_last_success(self):
        with TempDir() as d:
            good = FixtureFetcher({LEC: "lectures.html",
                                   ASG: "assignments.html"}, base_dir=FIXTURES)
            main(["--out-dir", d], fetcher=good, log=io.StringIO())
            success_stamp = read_json(os.path.join(d, "state.json"))["last_successful_run"]
            main(["--out-dir", d], fetcher=BoomFetcher(), log=io.StringIO())
            after = read_json(os.path.join(d, "state.json"))
        self.assertEqual(after["last_successful_run"], success_stamp)
        self.assertIsNotNone(after["last_attempted_run"])
        self.assertEqual(len(after["records"]), 42)


class TestPerSourceHealth(unittest.TestCase):
    """A source may not silently go to zero.

    The health check only looked at the TOTAL. The assignments page carries
    all 12 deadlines out of 42 records -- 28.6% -- so it could return nothing
    at all and the total still fell short of the 50% global threshold. The run
    exited 0, republished the calendar, and every deadline vanished with
    nothing said. Deadlines are the entire point of the tool, and they are a
    minority of what it counts.
    """

    GATED = "<html><body><p>Sign in to continue</p></body></html>"

    def _fixtures(self):
        return FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                              base_dir=FIXTURES)

    def _lectures_html(self):
        return read_text(os.path.join(FIXTURES, "lectures.html"))

    def _seed(self, d):
        with quiet_stderr():
            main(["--out-dir", d], fetcher=self._fixtures(), log=io.StringIO())
        return read_text(os.path.join(d, "schedule.ics"))

    def test_a_source_going_to_zero_fails_even_though_the_total_survives(self):
        with TempDir() as d:
            self._seed(d)
            gated = StringFetcher({LEC: self._lectures_html(),
                                   ASG: self.GATED})
            with quiet_stderr() as err:
                code = main(["--out-dir", d], fetcher=gated, log=io.StringIO())
        # 42 -> 30 is a 28.6% fall: comfortably inside the global threshold.
        self.assertEqual(code, EXIT_EMPTY)
        self.assertIn("produced 0 records", err.getvalue())
        self.assertIn("assignments", err.getvalue())
        self.assertIn("12", err.getvalue())

    def test_every_deadline_survives_in_the_calendar(self):
        with TempDir() as d:
            before = self._seed(d)
            gated = StringFetcher({LEC: self._lectures_html(),
                                   ASG: self.GATED})
            with quiet_stderr():
                main(["--out-dir", d], fetcher=gated, log=io.StringIO())
            after = read_text(os.path.join(d, "schedule.ics"))
        self.assertEqual(after, before)
        self.assertEqual(after.count("SUMMARY:DUE: "), 6)

    def test_a_per_source_collapse_fails_below_the_global_threshold(self):
        pages = {LEC: "".join("<p>Sep. %02d, 2026 Lecture %d</p>" % (i, i)
                              for i in range(1, 11)),
                 ASG: "".join("<p>Oct. %02d, 2026 A%d Due Oct. %02d</p>"
                              % (i, i, i + 1) for i in range(1, 6))}
        shrunk = dict(pages)
        shrunk[ASG] = "<p>Oct. 01, 2026 A1 Due Oct. 02</p>"
        with TempDir() as d:
            with quiet_stderr():
                main(["--out-dir", d], fetcher=StringFetcher(pages),
                     log=io.StringIO())
                # 20 -> 12 overall is -40%, inside the global 50% threshold;
                # the assignments source alone is 10 -> 2, which is -80%.
                code = main(["--out-dir", d], fetcher=StringFetcher(shrunk),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_SHRANK)

    def test_a_mild_per_source_drop_is_still_allowed(self):
        pages = {LEC: "".join("<p>Sep. %02d, 2026 Lecture %d</p>" % (i, i)
                              for i in range(1, 11)),
                 ASG: "<p>Oct. 01, 2026 A1 Due Oct. 02</p>"}
        fewer = dict(pages)
        fewer[LEC] = "".join("<p>Sep. %02d, 2026 Lecture %d</p>" % (i, i)
                             for i in range(1, 9))
        with TempDir() as d:
            with quiet_stderr():
                main(["--out-dir", d], fetcher=StringFetcher(pages),
                     log=io.StringIO())
                code = main(["--out-dir", d], fetcher=StringFetcher(fewer),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_a_source_with_no_history_and_records_is_not_an_alarm(self):
        """No baseline means no evidence of a fall -- as long as it is not 0.

        An unknown past cannot be compared against, so the shrink guard has
        nothing to say. Zero is different: it needs no comparison.
        """
        with TempDir() as d:
            with quiet_stderr():
                code = main(["--out-dir", d],
                            fetcher=StringFetcher(
                                {LEC: "<p>Sep. 02, 2026 A</p>",
                                 ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_a_gated_source_on_the_very_first_run_fails(self):
        """The case the old guard exempted was the case that needed it most.

        `if not before: continue` meant a source could only be protected once
        it had already succeeded. On a new machine -- or one where state.json
        was deleted -- every baseline is zero, so a page that happened to be
        gated that morning produced a calendar with none of its deadlines in
        it, and said exit 0.
        """
        with TempDir() as d:
            with quiet_stderr() as err:
                code = main(["--out-dir", d],
                            fetcher=StringFetcher(
                                {LEC: "<p>Sep. 02, 2026 A</p>",
                                 ASG: self.GATED}),
                            log=io.StringIO())
            wrote_anything = os.path.exists(os.path.join(d, "schedule.ics"))
        self.assertEqual(code, EXIT_EMPTY)
        self.assertFalse(wrote_anything)
        self.assertIn("produced 0 records", err.getvalue())
        self.assertIn("no successful run to compare", err.getvalue())

    def test_the_zero_baseline_does_not_perpetuate_itself(self):
        """Three consecutive empty runs all exited 0, each excusing the next."""
        gated = StringFetcher({LEC: "<p>Sep. 02, 2026 A</p>", ASG: self.GATED})
        with TempDir() as d:
            codes = []
            for _ in range(3):
                with quiet_stderr():
                    codes.append(main(["--out-dir", d], fetcher=gated,
                                      log=io.StringIO()))
        self.assertEqual(codes, [EXIT_EMPTY] * 3)

    def test_a_deleted_state_file_does_not_reopen_the_hole(self):
        """Deleting state.json is the documented reset; it must stay guarded."""
        with TempDir() as d:
            self._seed(d)
            os.remove(os.path.join(d, "state.json"))
            with quiet_stderr():
                code = main(["--out-dir", d],
                            fetcher=StringFetcher(
                                {LEC: self._lectures_html(),
                                 ASG: self.GATED}),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_EMPTY)

    def test_a_first_run_with_records_everywhere_still_succeeds(self):
        with TempDir() as d:
            with quiet_stderr():
                code = main(["--out-dir", d], fetcher=self._fixtures(),
                            log=io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_304_is_not_mistaken_for_a_collapse(self):
        with TempDir() as d:
            self._seed(d)
            unchanged = RecordingFetcher({LEC: FetchResult(LEC, 304, None),
                                          ASG: FetchResult(ASG, 304, None)})
            with quiet_stderr():
                code = main(["--out-dir", d], fetcher=unchanged,
                            log=io.StringIO())
            st = read_json(os.path.join(d, "state.json"))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(st["sources"][ASG]["record_count"], 12)

    def test_per_source_counts_are_persisted(self):
        with TempDir() as d:
            self._seed(d)
            st = read_json(os.path.join(d, "state.json"))
        self.assertEqual(st["sources"][LEC]["record_count"], 30)
        self.assertEqual(st["sources"][ASG]["record_count"], 12)

    def test_a_state_file_written_before_record_count_still_has_a_baseline(self):
        """The upgrade path: recover the baseline by counting stored records."""
        with TempDir() as d:
            self._seed(d)
            path = os.path.join(d, "state.json")
            st = read_json(path)
            for entry in st["sources"].values():
                entry.pop("record_count", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            self.assertEqual(
                cli_mod.previous_counts(state_mod.load(path))[ASG], 12)
            gated = StringFetcher({LEC: self._lectures_html(),
                                   ASG: self.GATED})
            with quiet_stderr():
                code = main(["--out-dir", d], fetcher=gated, log=io.StringIO())
        self.assertEqual(code, EXIT_EMPTY)

    def test_the_per_source_threshold_is_configurable_and_separate(self):
        cfg = config_mod.Config()
        cfg.max_source_shrink_ratio = 0.9
        cfg.max_shrink_ratio = 0.5
        records = [Record(date="2026-09-02", title="x", kind="lecture",
                          source_url=LEC, confidence="certain")]
        # -80% on one source passes a 90% per-source threshold...
        cli_mod.check_health(records, {LEC: 2}, {LEC: 10}, 1, cfg)
        # ...and the global guard is untouched by that setting.
        cfg.max_source_shrink_ratio = 0.99
        with self.assertRaises(cli_mod.RunFailure) as caught:
            cli_mod.check_health(records, {LEC: 2}, {LEC: 10}, 100, cfg)
        self.assertEqual(caught.exception.code, EXIT_SHRANK)

    def test_zero_is_a_failure_at_any_threshold(self):
        cfg = config_mod.Config()
        cfg.max_source_shrink_ratio = 1.0
        records = [Record(date="2026-09-02", title="x", kind="lecture",
                          source_url=LEC, confidence="certain")]
        with self.assertRaises(cli_mod.RunFailure) as caught:
            cli_mod.check_health(records, {LEC: 1, ASG: 0},
                                 {LEC: 1, ASG: 12}, 13, cfg)
        self.assertEqual(caught.exception.code, EXIT_EMPTY)


class SabotagedWrite(object):
    """Make one file's write fail partway, the way a full disk does.

    `state_mod.open` shadows the builtin at module scope, so this replaces the
    name the writer actually looks up -- no global patching, no real disk
    limit, and it is undone on exit.
    """

    def __init__(self, suffix, after_bytes=20480, errno=27,
                 message="File too large"):
        self.suffix = suffix
        self.after_bytes = after_bytes
        self.errno = errno
        self.message = message

    def __enter__(self):
        real = open

        def sabotaged(path, *args, **kwargs):
            handle = real(path, *args, **kwargs)
            if str(path).endswith(self.suffix):
                original = handle.write

                def write(text):
                    original(text[:self.after_bytes])
                    raise OSError(self.errno, self.message)

                handle.write = write
            return handle

        state_mod.open = sabotaged
        return self

    def __exit__(self, *exc):
        del state_mod.open


class TestOutputIsAllOrNothing(unittest.TestCase):
    """README: "a broken run never overwrites a good calendar."

    state.json was already written atomically; schedule.ics was written with a
    plain open(), so a failure partway through truncated the good calendar to
    whatever fitted -- ending mid-VEVENT, with no END:VCALENDAR, unparseable
    by any subscriber. And because the two files were written separately, a
    run could update one and fail on the other, leaving them permanently out
    of step so --diff re-reported the same change on every run.
    """

    LEC_PAGE = "<p>Sep. 02, 2026 Intro</p>"
    ASG_PAGE = "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"

    def _good(self):
        return FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                              base_dir=FIXTURES)

    def _seed(self, d):
        with quiet_stderr():
            main(["--out-dir", d], fetcher=self._good(), log=io.StringIO())
        return (read_text(os.path.join(d, "schedule.ics")),
                read_text(os.path.join(d, "state.json")))

    def test_a_failed_calendar_write_leaves_the_calendar_intact(self):
        with TempDir() as d:
            ics_before, state_before = self._seed(d)
            with SabotagedWrite("schedule.ics.tmp"):
                with quiet_stderr() as err:
                    code = main(["--out-dir", d], fetcher=self._good(),
                                log=io.StringIO())
            ics_after = read_text(os.path.join(d, "schedule.ics"))
            state_after = read_text(os.path.join(d, "state.json"))
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        self.assertEqual(code, EXIT_WRITE)
        self.assertEqual(ics_after, ics_before)
        self.assertEqual(state_after, state_before)
        self.assertEqual(leftovers, [])
        self.assertIn("FAILED", err.getvalue())
        self.assertIn("nothing was renamed into place", err.getvalue())

    def test_the_surviving_calendar_is_still_parseable(self):
        """The exact shape the audit produced: 20480 bytes, no END:VCALENDAR."""
        with TempDir() as d:
            self._seed(d)
            with SabotagedWrite("schedule.ics.tmp"):
                with quiet_stderr():
                    main(["--out-dir", d], fetcher=self._good(),
                         log=io.StringIO())
            ics = read_text(os.path.join(d, "schedule.ics"))
        self.assertTrue(ics.rstrip().endswith("END:VCALENDAR"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), ics.count("END:VEVENT"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 42)
        self.assertGreater(len(ics), 20480)

    def test_a_failed_state_write_does_not_advance_the_calendar(self):
        """The two files are one fact; neither may move without the other."""
        with TempDir() as d:
            with quiet_stderr():
                main(["--out-dir", d],
                     fetcher=StringFetcher({LEC: self.LEC_PAGE,
                                            ASG: self.ASG_PAGE}),
                     log=io.StringIO())
            ics_before = read_text(os.path.join(d, "schedule.ics"))
            state_before = read_text(os.path.join(d, "state.json"))
            moved = StringFetcher({LEC: "<p>Oct. 05, 2026 Intro</p>",
                                   ASG: self.ASG_PAGE})
            with SabotagedWrite("state.json.tmp"):
                with quiet_stderr():
                    code = main(["--out-dir", d], fetcher=moved,
                                log=io.StringIO())
            self.assertEqual(code, EXIT_WRITE)
            self.assertEqual(read_text(os.path.join(d, "schedule.ics")),
                             ics_before)
            self.assertEqual(read_text(os.path.join(d, "state.json")),
                             state_before)

    def test_a_change_is_reported_once_not_forever(self):
        """The symptom of a desynced pair: --diff repeating the same news."""
        with TempDir() as d:
            with quiet_stderr():
                main(["--out-dir", d],
                     fetcher=StringFetcher({LEC: self.LEC_PAGE,
                                            ASG: self.ASG_PAGE}),
                     log=io.StringIO())
            moved = {LEC: "<p>Oct. 05, 2026 Intro</p>", ASG: self.ASG_PAGE}
            with SabotagedWrite("state.json.tmp"):
                with quiet_stderr():
                    main(["--out-dir", d], fetcher=StringFetcher(moved),
                         log=io.StringIO())
            reports = []
            for _ in range(2):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    main(["--out-dir", d, "--diff"],
                         fetcher=StringFetcher(moved), log=io.StringIO())
                reports.append(buf.getvalue())
        self.assertIn("DATE CHANGED", reports[0])
        self.assertIn("No changes", reports[1])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_read_only_directory_fails_loudly_and_changes_nothing(self):
        with TempDir() as d:
            ics_before, state_before = self._seed(d)
            os.chmod(d, 0o500)
            try:
                with quiet_stderr() as err:
                    code = main(["--out-dir", d], fetcher=self._good(),
                                log=io.StringIO())
            finally:
                os.chmod(d, 0o700)
            self.assertEqual(code, EXIT_WRITE)
            self.assertEqual(read_text(os.path.join(d, "schedule.ics")),
                             ics_before)
            self.assertEqual(read_text(os.path.join(d, "state.json")),
                             state_before)
        self.assertIn("FAILED", err.getvalue())


class TestRenamePhaseTellsTheTruth(unittest.TestCase):
    """The staging loop was guarded; the rename loop ran bare.

    os.replace() is not merely a microsecond window during which the machine
    might crash. Put a directory where state.json belongs and it raises every
    single time -- and what the tool then printed was "neither file was
    modified", while schedule.ics had in fact been replaced, state.json had
    not, and state.json.tmp was left behind. A permanent desync announced as a
    clean abort.

    The desync is the smaller problem. Every guard in this program rests on
    the assumption that a failed run says so; an alarm that can be wrong in
    the reassuring direction is worse than no alarm, because it is believed.
    """

    PAGES = {LEC: "<p>Sep. 02, 2026 Intro</p>",
             ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}
    MOVED = {LEC: "<p>Oct. 05, 2026 Intro</p>",
             ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}

    def _seed(self, d):
        with quiet_stderr():
            main(["--out-dir", d], fetcher=StringFetcher(self.PAGES),
                 log=io.StringIO())
        return (read_text(os.path.join(d, "schedule.ics")),
                read_text(os.path.join(d, "state.json")))

    @staticmethod
    def _block_with_a_directory(path):
        """The audit's sabotage: os.replace() onto a directory always fails."""
        os.remove(path)
        os.makedirs(os.path.join(path, "in-the-way"))

    def _run_blocked(self, d, which):
        self._seed(d)
        self._block_with_a_directory(os.path.join(d, which))
        with quiet_stderr() as err:
            code = main(["--out-dir", d], fetcher=StringFetcher(self.MOVED),
                        log=io.StringIO())
        return code, err.getvalue()

    def test_a_blocked_state_rename_does_not_claim_nothing_happened(self):
        with TempDir() as d:
            code, err = self._run_blocked(d, "state.json")
        self.assertEqual(code, EXIT_WRITE)
        self.assertNotIn("neither file was modified", err)
        self.assertNotIn("nothing was renamed into place", err)
        self.assertIn("OUT OF STEP", err)

    def test_it_names_which_file_moved_and_which_did_not(self):
        with TempDir() as d:
            code, err = self._run_blocked(d, "state.json")
            ics_line = [ln for ln in err.splitlines() if "REPLACED" in ln]
        self.assertEqual(code, EXIT_WRITE)
        replaced = [ln for ln in ics_line if ln.strip().startswith("REPLACED")]
        untouched = [ln for ln in ics_line
                     if ln.strip().startswith("NOT REPLACED")]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(len(untouched), 1)
        self.assertIn("schedule.ics", replaced[0])
        self.assertIn("state.json", untouched[0])

    def test_the_report_matches_what_is_actually_on_disk(self):
        """The claim and the filesystem are checked against each other."""
        with TempDir() as d:
            ics_before, _ = self._seed(d)
            self._block_with_a_directory(os.path.join(d, "state.json"))
            with quiet_stderr() as err:
                main(["--out-dir", d], fetcher=StringFetcher(self.MOVED),
                     log=io.StringIO())
            report = err.getvalue()
            ics_after = read_text(os.path.join(d, "schedule.ics"))
            still_a_directory = os.path.isdir(os.path.join(d, "state.json"))
        # stderr says schedule.ics was replaced -- so it must have been.
        self.assertIn("REPLACED     ", report)
        self.assertIn("schedule.ics", report)
        self.assertNotEqual(ics_after, ics_before)
        self.assertIn("20261005", ics_after)
        self.assertTrue(still_a_directory)

    def test_it_says_how_to_recover(self):
        with TempDir() as d:
            _, err = self._run_blocked(d, "state.json")
        self.assertIn("To recover", err)
        self.assertIn("delete", err)
        self.assertIn("state.json", err)

    def test_no_tmp_file_is_left_behind_by_a_failed_rename(self):
        with TempDir() as d:
            self._run_blocked(d, "state.json")
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_failure_on_the_first_rename_is_still_all_or_nothing(self):
        """Nothing renamed yet means the old promise is still true."""
        with TempDir() as d:
            self._seed(d)
            state_before = read_text(os.path.join(d, "state.json"))
            self._block_with_a_directory(os.path.join(d, "schedule.ics"))
            with quiet_stderr() as err:
                code = main(["--out-dir", d], fetcher=StringFetcher(self.MOVED),
                            log=io.StringIO())
            state_after = read_text(os.path.join(d, "state.json"))
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        self.assertEqual(code, EXIT_WRITE)
        self.assertEqual(state_after, state_before)
        self.assertEqual(leftovers, [])
        self.assertIn("nothing was renamed into place", err.getvalue())
        self.assertNotIn("OUT OF STEP", err.getvalue())

    def test_the_desync_is_reported_every_run_until_it_is_fixed(self):
        """It must not go quiet on the second attempt."""
        with TempDir() as d:
            self._seed(d)
            self._block_with_a_directory(os.path.join(d, "state.json"))
            codes, reports = [], []
            for _ in range(2):
                with quiet_stderr() as err:
                    codes.append(main(["--out-dir", d],
                                      fetcher=StringFetcher(self.MOVED),
                                      log=io.StringIO()))
                reports.append(err.getvalue())
        self.assertEqual(codes, [EXIT_WRITE, EXIT_WRITE])
        for report in reports:
            self.assertIn("OUT OF STEP", report)


class TestPartialWriteHelper(unittest.TestCase):
    """write_files() at unit level, with os.replace made to fail on demand."""

    class FailingReplace(object):
        def __init__(self, fail_on):
            self.fail_on = fail_on
            self.calls = 0

        def __enter__(self):
            real = os.replace

            def replace(src, dst):
                self.calls += 1
                if self.calls == self.fail_on:
                    raise OSError(13, "Permission denied")
                return real(src, dst)

            self._real = real
            state_mod.os.replace = replace
            return self

        def __exit__(self, *exc):
            state_mod.os.replace = self._real

    def _three(self, d):
        return [(os.path.join(d, name), name, None)
                for name in ("a.txt", "b.txt", "c.txt")]

    def test_a_later_rename_failure_raises_partial_write(self):
        with TempDir() as d:
            with self.FailingReplace(fail_on=2):
                with self.assertRaises(state_mod.PartialWrite) as caught:
                    state_mod.write_files(self._three(d))
            exc = caught.exception
            self.assertEqual([os.path.basename(p) for p in exc.renamed],
                             ["a.txt"])
            self.assertEqual([os.path.basename(p) for p in exc.not_renamed],
                             ["b.txt", "c.txt"])
            self.assertEqual(exc.leftovers, [])
            self.assertEqual(sorted(os.listdir(d)), ["a.txt"])

    def test_a_partial_write_is_still_an_oserror(self):
        """Callers that only knew about OSError must not start crashing."""
        self.assertTrue(issubclass(state_mod.PartialWrite, OSError))

    def test_the_first_rename_failing_is_a_plain_oserror(self):
        with TempDir() as d:
            with self.FailingReplace(fail_on=1):
                with self.assertRaises(OSError) as caught:
                    state_mod.write_files(self._three(d))
            self.assertNotIsInstance(caught.exception, state_mod.PartialWrite)
            self.assertEqual(os.listdir(d), [])

    def test_every_staged_file_is_cleaned_up_after_a_rename_failure(self):
        with TempDir() as d:
            with self.FailingReplace(fail_on=2):
                with self.assertRaises(OSError):
                    state_mod.write_files(self._three(d))
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")],
                             [])

    def test_a_keyboard_interrupt_is_not_swallowed(self):
        """Ctrl-C must stay Ctrl-C, not become a write error."""
        real = os.replace
        calls = []

        def replace(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(src, dst)

        with TempDir() as d:
            state_mod.os.replace = replace
            try:
                with self.assertRaises(KeyboardInterrupt):
                    state_mod.write_files(self._three(d))
            finally:
                state_mod.os.replace = real
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")],
                             [])


class TestWriteFilesHelper(unittest.TestCase):
    def test_nothing_is_renamed_when_a_later_write_fails(self):
        with TempDir() as d:
            first = os.path.join(d, "one.txt")
            with open(first, "w") as fh:
                fh.write("original")
            second = os.path.join(d, "subdir", "two.txt")  # no such directory
            with self.assertRaises(OSError):
                state_mod.write_files([(first, "replacement", None),
                                       (second, "anything", None)])
            self.assertEqual(read_text(first), "original")
            self.assertEqual(sorted(os.listdir(d)), ["one.txt"])

    def test_all_files_appear_when_every_write_succeeds(self):
        with TempDir() as d:
            a, b = os.path.join(d, "a.txt"), os.path.join(d, "b.txt")
            state_mod.write_files([(a, "A", None), (b, "B\n", "")])
            self.assertEqual(read_text(a), "A")
            self.assertEqual(read_text(b), "B\n")
            self.assertEqual(sorted(os.listdir(d)), ["a.txt", "b.txt"])

    def test_crlf_survives_the_newline_argument(self):
        with TempDir() as d:
            path = os.path.join(d, "c.ics")
            state_mod.write_files([(path, "A\r\nB\r\n", "")])
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"A\r\nB\r\n")


class TestCliOptions(unittest.TestCase):
    def _fixtures(self):
        return FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                              base_dir=FIXTURES)

    def test_dry_run_writes_nothing(self):
        with TempDir() as d:
            code = main(["--out-dir", d, "--dry-run"], fetcher=self._fixtures(),
                        log=io.StringIO())
            self.assertEqual(code, EXIT_OK)
            self.assertFalse(os.path.exists(os.path.join(d, "schedule.ics")))
            self.assertFalse(os.path.exists(os.path.join(d, "state.json")))

    def test_alarm_days_flag_reaches_the_ics(self):
        with TempDir() as d:
            main(["--out-dir", d, "--alarm-days", "7"],
                 fetcher=self._fixtures(), log=io.StringIO())
            ics = read_text(os.path.join(d, "schedule.ics"))
        self.assertIn("TRIGGER:-P7D", ics)
        self.assertNotIn("TRIGGER:-P1D", ics)

    def test_fall_year_flag_shifts_undated_records(self):
        with TempDir() as d:
            main(["--out-dir", d, "--fall-year", "2030"],
                 fetcher=StringFetcher({LEC: "<p>Sep. 02, 2026 Intro</p>",
                                        ASG: "<p>A1 Due Sep. 13</p>"}),
                 log=io.StringIO())
            st = read_json(os.path.join(d, "state.json"))
        dues = [r for r in st["records"] if r["kind"] == "assignment_due"]
        self.assertEqual(dues[0]["date"], "2030-09-13")

    def test_diff_mode_reports_a_moved_lecture(self):
        with TempDir() as d:
            main(["--out-dir", d],
                 fetcher=StringFetcher({LEC: "<p>Sep. 02, 2026 Intro</p>",
                                        ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}),
                 log=io.StringIO())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["--out-dir", d, "--diff"],
                     fetcher=StringFetcher({LEC: "<p>Sep. 04, 2026 Intro</p>",
                                            ASG: "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"}),
                     log=io.StringIO())
        report = buf.getvalue()
        self.assertIn("DATE CHANGED", report)
        self.assertIn("2026-09-02", report)
        self.assertIn("2026-09-04", report)


class TestRescheduledDeadlineKeepsItsUid(unittest.TestCase):
    """The core path, pinned end to end.

    A UID that survives a reschedule is what makes the calendar UPDATE the
    event instead of deleting it and creating a second one, and what makes
    --diff say "moved" instead of "removed + added". Every other guarantee in
    this program is downstream of it, and every parser change is capable of
    breaking it silently, because the title is hashed into the UID. So the
    named case is asserted directly rather than inferred from the diff text.
    """

    LEC_PAGE = "<p>Sep. 02, 2026 Intro</p>"
    TITLE = "Assignment 1 - Trajectory Following"

    def _asg(self, due):
        return "<p>Sep. 09, 2026 %s Due %s</p>" % (self.TITLE, due)

    def _run(self, d, due, argv=()):
        out = io.StringIO()
        with quiet_stderr(), contextlib.redirect_stdout(out):
            code = main(["--out-dir", d] + list(argv),
                        fetcher=StringFetcher({LEC: self.LEC_PAGE,
                                               ASG: self._asg(due)}),
                        log=io.StringIO())
        records = read_json(os.path.join(d, "state.json"))["records"]
        return code, {r["uid"]: r for r in records}, out.getvalue()

    def test_sep_13_to_sep_20_does_not_move_a_single_uid(self):
        with TempDir() as d:
            code_a, before, _ = self._run(d, "Sep. 13")
            code_b, after, report = self._run(d, "Sep. 20", ["--diff"])
        self.assertEqual((code_a, code_b), (EXIT_OK, EXIT_OK))
        self.assertEqual(set(before), set(after))
        due_uids = [u for u, r in before.items()
                    if r["kind"] == "assignment_due"]
        self.assertEqual(len(due_uids), 1)
        uid = due_uids[0]
        self.assertEqual(before[uid]["date"], "2026-09-13")
        self.assertEqual(after[uid]["date"], "2026-09-20")
        self.assertEqual(before[uid]["title"], after[uid]["title"])
        self.assertIn("DATE CHANGED", report)
        self.assertNotIn("DISAPPEARED", report)
        self.assertNotIn("NEW (", report)

    def test_the_ics_carries_the_same_uid_before_and_after(self):
        def uids(path):
            return sorted(ln[4:].strip() for ln in read_text(path).splitlines()
                          if ln.startswith("UID:"))
        with TempDir() as d:
            self._run(d, "Sep. 13")
            first = uids(os.path.join(d, "schedule.ics"))
            self._run(d, "Sep. 20")
            second = uids(os.path.join(d, "schedule.ics"))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_shifting_the_whole_lecture_schedule_keeps_every_unique_uid(self):
        """30 real rows, all one day later. Only the two twins may churn."""
        import datetime as dt
        months = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 8: "Aug",
                  9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
        with TempDir() as d:
            with quiet_stderr():
                main(["--out-dir", d],
                     fetcher=FixtureFetcher({LEC: "lectures.html",
                                             ASG: "assignments.html"},
                                            base_dir=FIXTURES),
                     log=io.StringIO())
            stored = read_json(os.path.join(d, "state.json"))["records"]
            lectures = [r for r in stored if r["source_url"] == LEC]
            self.assertEqual(len(lectures), 30)

            rows = []
            for r in lectures:
                day = dt.date.fromisoformat(r["date"]) + dt.timedelta(days=1)
                rows.append("<p>%s. %02d, %d %s</p>"
                            % (months[day.month], day.day, day.year,
                               r["title"]))
            page = "<html><body>%s</body></html>" % "".join(rows)
            asg = read_text(os.path.join(FIXTURES, "assignments.html"))
            with quiet_stderr():
                code = main(["--out-dir", d],
                            fetcher=StringFetcher({LEC: page, ASG: asg}),
                            log=io.StringIO())
            after = read_json(os.path.join(d, "state.json"))["records"]

        self.assertEqual(code, EXIT_OK)
        was = {r["uid"]: r for r in lectures}
        now = {r["uid"]: r for r in after if r["source_url"] == LEC}
        kept = [u for u in was if u in now]
        lost = sorted(was[u]["title"] for u in was if u not in now)
        # Every row whose title identifies it keeps its UID and moves.
        self.assertEqual(len(kept), 28)
        for uid in kept:
            self.assertNotEqual(was[uid]["date"], now[uid]["date"])
            self.assertEqual(was[uid]["title"], now[uid]["title"])
        # The only churn is the pair the date is part of the identity for.
        self.assertEqual(lost, ["Project working session"] * 2)


class TestDuplicateRowDeletion(unittest.TestCase):
    """The audit's H3, end to end.

    The real lectures page carries two rows called "Project working session",
    on Dec 07 and Dec 09. When the Dec 07 one is dropped, the report must not
    claim the Dec 09 session was rescheduled.
    """

    BOTH = ("<p>Dec. 07, 2026 Project working session</p>"
            "<p>Dec. 09, 2026 Project working session</p>")
    ONLY_LATER = "<p>Dec. 09, 2026 Project working session</p>"
    ASG_PAGE = "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"

    def _run(self, out_dir, lectures, argv=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--out-dir", out_dir] + list(argv),
                 fetcher=StringFetcher({LEC: lectures, ASG: self.ASG_PAGE}),
                 log=io.StringIO())
        return buf.getvalue()

    def test_deleting_one_row_is_not_reported_as_a_reschedule(self):
        with TempDir() as d:
            self._run(d, self.BOTH)
            report = self._run(d, self.ONLY_LATER, ["--diff"])
        self.assertNotIn("DATE CHANGED", report)
        self.assertIn("DISAPPEARED", report)
        self.assertIn("2026-12-07", report)

    def test_the_surviving_row_keeps_its_own_date(self):
        with TempDir() as d:
            self._run(d, self.BOTH)
            self._run(d, self.ONLY_LATER)
            st = read_json(os.path.join(d, "state.json"))
        sessions = [r for r in st["records"]
                    if r["title"] == "Project working session"]
        self.assertEqual([r["date"] for r in sessions], ["2026-12-09"])

    def test_an_unrelated_reschedule_is_still_an_update_not_a_replacement(self):
        # The hardened core path, guarded end to end.
        with TempDir() as d:
            self._run(d, "<p>Dec. 07, 2026 Sampling Based Algorithms</p>")
            report = self._run(
                d, "<p>Dec. 09, 2026 Sampling Based Algorithms</p>", ["--diff"])
        self.assertIn("DATE CHANGED", report)
        self.assertNotIn("DISAPPEARED", report)


class TestDropReporting(unittest.TestCase):
    """An entry that looks like a date but cannot be parsed must reach a human.

    Exit 0 plus a silently missing deadline is the worst combination this tool
    can produce, so the run log names the fragment and the line it came from.
    """

    GOOD_ROW = "<p>Sep. 09, 2026 A1 Due Sep. 13</p>"

    def test_unparseable_date_is_named_on_the_log(self):
        f = StringFetcher({LEC: "<p>Sep. 02, 2026 Intro</p>",
                           ASG: "<p>Feb. 30, 2026 Assignment 9</p>"
                                + self.GOOD_ROW})
        log = io.StringIO()
        with TempDir() as d:
            self.assertEqual(main(["--out-dir", d], fetcher=f, log=log),
                             EXIT_OK)
        msg = log.getvalue()
        self.assertIn("1 date-like fragment(s) dropped", msg)
        self.assertIn("Feb. 30, 2026", msg)
        self.assertIn("Assignment 9", msg)
        self.assertIn("not a real calendar date", msg)

    def test_a_clean_run_says_nothing_about_drops(self):
        f = FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                           base_dir=FIXTURES)
        log = io.StringIO()
        with TempDir() as d:
            main(["--out-dir", d], fetcher=f, log=log)
        self.assertNotIn("dropped", log.getvalue())

    def test_an_out_of_range_day_reaches_the_log_too(self):
        """"Sep. 32" used to be dropped in silence while "Feb. 30" was named."""
        for fragment in ("Sep. 32, 2026", "Sep. 0", "Sep. 00, 2026",
                         "Sep. 99"):
            with self.subTest(fragment):
                f = StringFetcher(
                    {LEC: "<p>Sep. 02, 2026 Intro</p>",
                     ASG: "<p>%s Assignment 9</p>"
                          "<p>Sep. 09, 2026 A1 Due Sep. 13</p>" % fragment})
                log = io.StringIO()
                with TempDir() as d:
                    code = main(["--out-dir", d], fetcher=f, log=log)
                msg = log.getvalue()
                self.assertEqual(code, EXIT_OK, fragment)
                self.assertIn("1 date-like fragment(s) dropped", msg)
                self.assertIn(fragment, msg)
                self.assertIn("Assignment 9", msg)

    def test_a_long_list_of_drops_is_summarised(self):
        page = "".join("<p>Feb. 30, 2026 Assignment %d</p>" % i
                       for i in range(0, 7)) + self.GOOD_ROW
        f = StringFetcher({LEC: "<p>Sep. 02, 2026 Intro</p>", ASG: page})
        log = io.StringIO()
        with TempDir() as d:
            self.assertEqual(main(["--out-dir", d], fetcher=f, log=log),
                             EXIT_OK)
        msg = log.getvalue()
        self.assertIn("7 date-like fragment(s) dropped", msg)
        self.assertIn("... and 2 more", msg)


class TestEndToEndAgainstFixtures(unittest.TestCase):
    def test_full_run_produces_the_expected_shape(self):
        with TempDir() as d:
            f = FixtureFetcher({LEC: "lectures.html", ASG: "assignments.html"},
                               base_dir=FIXTURES)
            main(["--out-dir", d], fetcher=f, log=io.StringIO())
            st = read_json(os.path.join(d, "state.json"))
            ics = read_text(os.path.join(d, "schedule.ics"))

        recs = st["records"]
        self.assertEqual(len(recs), 42)
        # Both pages spell out 2026 on their left-hand dates; the six deadlines
        # inherit it from the anchor. No record may land in another year.
        self.assertEqual(sorted({r["date"][:4] for r in recs}), ["2026"])
        # A reader must be able to tell a release from a deadline.
        self.assertIn("SUMMARY:ASSIGNED: Assignment 1 - Robot Building", ics)
        self.assertIn("SUMMARY:DUE: Assignment 1 - Robot Building", ics)
        kinds = {}
        for r in recs:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        self.assertEqual(kinds["lecture"], 30)
        self.assertEqual(kinds["assignment_due"], 6)
        self.assertEqual(kinds["assignment_out"], 6)
        # Nothing is left semantically undecided: both assignment pages state
        # their own convention, so "unknown" must not appear at all.
        self.assertNotIn("unknown", kinds)
        self.assertEqual(len({r["uid"] for r in recs}), 42)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 42)
        self.assertIsNotNone(st["last_successful_run"])

    def test_run_is_deterministic_apart_from_timestamps(self):
        import re
        outs = []
        for _ in range(2):
            with TempDir() as d:
                f = FixtureFetcher({LEC: "lectures.html",
                                    ASG: "assignments.html"},
                                   base_dir=FIXTURES)
                main(["--out-dir", d], fetcher=f, log=io.StringIO())
                text = read_text(os.path.join(d, "schedule.ics"))
                outs.append(re.sub(r"DTSTAMP:[0-9TZ]+", "DTSTAMP:X", text))
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
