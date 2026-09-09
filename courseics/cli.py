"""Command line entry point, including the failure-alarm exit codes.

Silent failure is the worst outcome for a reminder tool, so every degraded
outcome exits non-zero with an explanation on stderr.

  0  success
  1  unexpected internal error
  2  fetch failure (network error, or HTTP other than 200/304)
  3  parsed zero records, from any single source or overall
  4  record count collapsed by more than the configured threshold
  5  could not write the output files
  6  the stored records cannot be refreshed under the options asked for
  7  the run was not configured: bad flags, or a config file that cannot be used

A code names what to DO about a failure, not merely what went wrong, so two
failures that call for opposite actions may not share one. Codes 2 and 6 were
one code, and their responses have nothing in common: code 2 means the network
or the site is the problem, so check connectivity; code 6 means the fetch
worked perfectly -- a 304, sent in answer to a request carrying no validator --
and the fix is to delete state.json. Reading "fetch failure, check the network"
sends the reader looking at a network that is fine.

Code 7 exists for the same reason. `argparse` exits 2 on a usage error by
default, and 2 already meant "fetch failure, check the network" -- so a
mistyped flag sent the reader to look at their Wi-Fi. Nothing has been fetched
when the arguments or the config file are wrong, so it cannot be a fetch
failure.
"""

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple

from . import ics, state as state_mod
from .config import Config, ConfigError, fall_anchor, load_config
from .fetch import FetchError, Fetcher, FixtureFetcher, HttpFetcher
from .parse import (CERTAIN, UNCERTAIN, Dropped, Record, assign_uids, dedupe,
                    parse_page)

# How many dropped fragments to spell out before summarising the rest.
MAX_DROPS_LISTED = 5

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_FETCH = 2
EXIT_EMPTY = 3
EXIT_SHRANK = 4
EXIT_WRITE = 5
EXIT_STALE = 6
EXIT_CONFIG = 7

# Looked for in the working directory when --config is not given.
DEFAULT_CONFIG_NAME = "course-schedule.ini"


class RunFailure(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def parse_signature(cfg: Config) -> str:
    """Everything about the configuration that changes how a page is PARSED.

    A 304 skips parsing and reuses the stored records, which is the whole
    point of a conditional GET -- but the year a dateless "Due Sep. 13" gets
    is decided while parsing, not while fetching. So a student who moved to
    the next semester and ran --fall-year 2027 was told "304 Not Modified, no
    changes", exited 0, and kept a calendar full of last year's dates. The
    page had not changed; the question being asked of it had.

    Recorded in state.json so the next run can notice the difference. Kept
    readable rather than hashed: state.json is a file users are invited to
    inspect.
    """
    kinds = ",".join(sorted("%s=%s" % (src.url, src.default_kind)
                            for src in cfg.sources))
    return "anchor=%s due=%s kinds=%s" % (cfg.anchor.name, cfg.default_due_time,
                                          kinds)


def previous_counts(st: Dict) -> Dict[str, int]:
    """How many records each source produced on the last successful run.

    Read from the per-source `record_count` written by the previous run, and
    for a state file written before that field existed, recovered by counting
    the stored records. A source with no history at all is simply absent --
    an unknown baseline is not evidence of a collapse.
    """
    out = {}  # type: Dict[str, int]
    for url, entry in (st.get("sources") or {}).items():
        count = entry.get("record_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            out[url] = count
    counted = {}  # type: Dict[str, int]
    for rec in state_mod.records_from_state(st):
        counted[rec.source_url] = counted.get(rec.source_url, 0) + 1
    for url, count in counted.items():
        out.setdefault(url, count)
    return out


def collect(fetcher: Fetcher, cfg: Config, st: Dict,
            log=sys.stderr) -> Tuple[List[Record], Dict, Dict[str, int]]:
    """Fetch + parse every source. Raises RunFailure on any fetch problem."""
    records = []  # type: List[Record]
    counts = {}  # type: Dict[str, int]
    src_meta = dict(st.get("sources") or {})
    previous = state_mod.records_from_state(st)
    prev_by_url = {}  # type: Dict[str, List[Record]]
    for rec in previous:
        prev_by_url.setdefault(rec.source_url, []).append(rec)

    signature = parse_signature(cfg)
    stored_signature = st.get("parse_signature") or ""
    reparse = bool(previous) and stored_signature != signature
    if reparse:
        print("parse options changed (%s -> %s): re-fetching both pages in "
              "full, because the stored records were parsed under the old "
              "ones" % (stored_signature or "unrecorded", signature), file=log)

    for source in cfg.sources:
        # A conditional GET would be answered with a 304, and a 304 skips
        # parsing -- which is exactly what must not happen when the parse
        # itself is what changed.
        caching = ({} if reparse
                   else state_mod.caching_for(st, source.url))
        try:
            result = fetcher.fetch(source.url, caching)
        except FetchError as exc:
            raise RunFailure(EXIT_FETCH, "fetch failed: %s" % exc)

        entry = dict(src_meta.get(source.url) or {})
        entry["fetched_at"] = state_mod.now_iso()
        entry["status"] = result.status

        if result.not_modified:
            if reparse:
                # NOT a fetch failure. The fetch worked; the answer is just
                # unusable for the question being asked. Sharing code 2 with
                # a real network failure told the reader to go and check a
                # connection that had demonstrably just worked, when the fix
                # is on their own disk.
                raise RunFailure(
                    EXIT_STALE,
                    "%s answered 304 Not Modified even though no validator "
                    "was sent, so its records cannot be re-parsed. They were "
                    "produced with %s and you asked for %s, so publishing "
                    "them would silently keep the old dates. The network is "
                    "not the problem: delete state.json and run again, which "
                    "forces a full parse under the new options."
                    % (source.url, stored_signature or "unrecorded options",
                       signature))
            # Reuse what we already parsed; nothing on the page changed.
            reused = prev_by_url.get(source.url, [])
            print("%s: 304 Not Modified (reusing %d stored record(s))"
                  % (source.url, len(reused)), file=log)
            records.extend(reused)
            entry["record_count"] = len(reused)
            counts[source.url] = len(reused)
            src_meta[source.url] = entry
            continue

        if result.etag:
            entry["etag"] = result.etag
        if result.last_modified:
            entry["last_modified"] = result.last_modified
        src_meta[source.url] = entry

        dropped = []  # type: List[Dropped]
        parsed = parse_page(result.body or "", source.url,
                            source.default_kind, cfg, dropped)
        parsed = dedupe(parsed)
        print("%s: 200 OK, %d record(s)" % (source.url, len(parsed)), file=log)
        report_drops(source.url, dropped, log)
        records.extend(parsed)
        entry["record_count"] = len(parsed)
        counts[source.url] = len(parsed)
        src_meta[source.url] = entry

    records.sort(key=lambda r: (r.date, r.time or "", r.kind, r.title))
    assign_uids(records)
    return records, src_meta, counts


def report_drops(url: str, dropped: List[Dropped], log) -> None:
    """Name every fragment that looked like a date but produced no record.

    A dropped deadline is invisible in the calendar: it simply is not there,
    and an exit code of 0 says everything went fine. "Feb. 30" is a typo a
    human makes, and the only place it can be noticed is here.
    """
    if not dropped:
        return
    print("%s: %d date-like fragment(s) dropped, no record produced:"
          % (url, len(dropped)), file=log)
    for item in dropped[:MAX_DROPS_LISTED]:
        print("    - %s" % item.describe(), file=log)
    if len(dropped) > MAX_DROPS_LISTED:
        print("    ... and %d more" % (len(dropped) - MAX_DROPS_LISTED),
              file=log)


def check_health(records: List[Record], counts: Dict[str, int],
                 prev_counts: Dict[str, int], previous_count: int,
                 cfg: Config) -> None:
    """Refuse to publish a result that looks like a scraping failure.

    Checked per source, not only in total. On this course the assignments page
    carries all 12 deadlines out of 42 records -- 28.6%. When that page
    returned nothing, the total fell by less than the 50% global threshold, so
    the run "succeeded" at exit 0 and quietly republished a calendar with
    every single deadline removed. The one thing this tool exists to deliver
    can be a minority of what it counts.
    """
    if not records:
        raise RunFailure(
            EXIT_EMPTY,
            "parsed 0 records from %d source(s). The page layout probably "
            "changed, or the pages are gated. Refusing to overwrite the "
            "calendar with nothing." % len(cfg.sources))

    for url in sorted(counts):
        before = prev_counts.get(url)
        now = counts[url]
        # Zero is a failure with or without a baseline. `if not before:
        # continue` exempted exactly the case that needed the guard most: on a
        # first run -- a new machine, or one where state.json was deleted --
        # every source has a zero baseline, so a page that happened to be
        # gated that morning produced a calendar with none of its deadlines
        # in it, at exit 0. The next run then compared against that zero and
        # skipped the check again; three consecutive empty runs all exited 0.
        # The guard could only ever protect a source that had already
        # succeeded once, which is to say it protected nobody at setup time.
        #
        # A source is in config.py because somebody put it there expecting
        # dates on it. "It is allowed to be empty" is not a state this program
        # has any way to distinguish from "the scrape broke", and inventing a
        # per-source `may_be_empty` flag would only move the judgement into a
        # setting nobody revisits. If a source is legitimately empty, take it
        # out of `SOURCES`. See DESIGN.md §10.
        if now == 0:
            if before:
                detail = ("but state.json records %d on the last successful "
                          "run. Either that page's layout changed or it is "
                          "now gated -- or, if the page looks fine, that "
                          "baseline is wrong; check state.json too." % before)
            else:
                detail = ("and there is no successful run to compare it "
                          "against. Either that page is gated or its layout "
                          "is not what the parser expects -- open it by hand "
                          "and check. A source is only listed in config.py "
                          "because it is expected to carry dates.")
            raise RunFailure(
                EXIT_EMPTY,
                "%s produced 0 records %s Refusing to publish a calendar "
                "with everything it contributes missing." % (url, detail))
        if not before:
            continue   # No baseline: an unknown past is not evidence of a fall.
        shrink = 1.0 - (now / float(before))
        if shrink > cfg.max_source_shrink_ratio:
            # Both numbers in this comparison can be wrong, and the message
            # used to name only one of them. `before` is read from
            # state.json's `sources[url].record_count`; corrupt that field and
            # the run stops with an accusation against a page that is
            # perfectly healthy, and no hint that the number it is being
            # measured against came off the disk. Name where the baseline
            # comes from so the reader can check both ends.
            raise RunFailure(
                EXIT_SHRANK,
                "%s collapsed from %d to %d record(s) (-%.0f%%, per-source "
                "threshold %.0f%%). Refusing to publish. Inspect that page by "
                "hand -- and if it looks unchanged, the baseline of %d in "
                "state.json (sources[url].record_count) is the other half of "
                "this comparison and may be the wrong number."
                % (url, before, now, shrink * 100,
                   cfg.max_source_shrink_ratio * 100, before))

    if previous_count > 0:
        shrink = 1.0 - (len(records) / float(previous_count))
        if shrink > cfg.max_shrink_ratio:
            raise RunFailure(
                EXIT_SHRANK,
                "record count collapsed from %d to %d (-%.0f%%, threshold "
                "%.0f%%). Refusing to publish. Inspect the pages by hand -- "
                "and if they look unchanged, the baseline of %d comes from "
                "the records stored in state.json and may itself be wrong."
                % (previous_count, len(records), shrink * 100,
                   cfg.max_shrink_ratio * 100, previous_count))


def report_partial_write(exc: "state_mod.PartialWrite",
                         previous_success: str) -> None:
    """Say exactly which files moved and which did not.

    The staging design makes "both files, or neither" true for every failure
    to WRITE. It was never true of the renames, and that path printed the
    all-or-nothing message anyway. Replacing state.json with a directory
    produced: exit 5, "neither file was modified", schedule.ics replaced,
    state.json not, a state.json.tmp left behind -- a permanent desync
    announced as a clean abort.

    Every guard in this program assumes a failed run says so. An alarm that
    can be wrong in the reassuring direction is worse than no alarm, because
    it is trusted. So when the invariant cannot be kept, the report says what
    is actually on disk, and what to do about it.
    """
    print("course-schedule-ics FAILED: the output files were staged but could not all be "
          "renamed into place: %s" % exc, file=sys.stderr)
    print("THE TWO FILES ARE NOW OUT OF STEP. What is on disk:",
          file=sys.stderr)
    for path in exc.renamed:
        print("    REPLACED     %s  (this run's content)" % path,
              file=sys.stderr)
    for path in exc.not_renamed:
        print("    NOT REPLACED %s  (still the previous run's content)" % path,
              file=sys.stderr)
    for path in exc.leftovers:
        print("    LEFT BEHIND  %s  (staged copy; could not be removed)" % path,
              file=sys.stderr)
    print("Until they agree, --diff will keep re-reporting the same changes.",
          file=sys.stderr)
    print("To recover: clear whatever blocked the file(s) listed as NOT "
          "REPLACED, then run again. If that fails, delete %s and run again "
          "-- the next run rebuilds it from the pages and reports the whole "
          "schedule as NEW once."
          % ", ".join(exc.not_renamed or ["state.json"]), file=sys.stderr)
    print("last successful run: %s" % previous_success, file=sys.stderr)


def summarize(records: List[Record]) -> str:
    certain = sum(1 for r in records if r.confidence == CERTAIN)
    uncertain = len(records) - certain
    by_kind = {}  # type: Dict[str, int]
    for r in records:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
    kinds = ", ".join("%s=%d" % kv for kv in sorted(by_kind.items()))
    return ("%d record(s): %d certain, %d uncertain  [%s]"
            % (len(records), certain, uncertain, kinds))


class _Parser(argparse.ArgumentParser):
    """argparse, but a usage error exits 7 instead of 2.

    argparse's default is `sys.exit(2)`, and 2 is this program's "fetch
    failure -- check the network" code. A typo in a flag is not a network
    problem, and under a scheduler the exit code is often all anyone sees.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="course-schedule-ics",
        description="Scrape public course pages and emit an ICS calendar.")
    p.add_argument("--config", default=None,
                   help="path to the INI config naming the pages to fetch "
                        "(default: ./course-schedule.ini). See config.example.ini.")
    p.add_argument("--out-dir", default=".",
                   help="where state.json and schedule.ics are written")
    p.add_argument("--offline", action="store_true",
                   help="replay each source's recorded `fixture` instead of "
                        "hitting the network")
    p.add_argument("--diff", action="store_true",
                   help="report changes against the stored state.json")
    p.add_argument("--dry-run", action="store_true",
                   help="parse and report, but write no files")
    p.add_argument("--fall-year", type=int, default=None,
                   help="override the config's semester anchor: Aug-Dec map "
                        "to this year, Jan-May to the next")
    p.add_argument("--alarm-days", type=int, default=None,
                   help="override the config's VALARM lead time, in days")
    p.add_argument("--list", action="store_true",
                   help="print every parsed record to stdout")
    return p


def resolve_config(args, injected: Optional[Config]) -> Config:
    """The Config this run will use, or a RunFailure explaining why there is none.

    An injected Config wins: that is how the tests, and any caller embedding
    this as a library, supply a course without writing a file. Otherwise the
    file named by --config is read, and failing that ./course-schedule.ini -- and if
    neither exists the run stops, because there is no course compiled in to
    fall back on.
    """
    if injected is not None:
        return injected
    path = args.config or os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if args.config is None and not os.path.exists(path):
        raise RunFailure(
            EXIT_CONFIG,
            "no config file. This tool has no course built into it: it needs "
            "one file saying which pages to fetch. Copy config.example.ini to "
            "%s and edit it, or pass --config PATH." % path)
    try:
        return load_config(path)
    except ConfigError as exc:
        raise RunFailure(EXIT_CONFIG, str(exc))


def build_fetcher(cfg: Config, offline: bool) -> Fetcher:
    """The real fetcher, or a replay of each source's recorded page."""
    if not offline:
        return HttpFetcher(min_interval=cfg.min_request_interval,
                           timeout=cfg.http_timeout,
                           user_agent=cfg.user_agent)
    missing = [src.name or src.url for src in cfg.sources if not src.fixture]
    if missing:
        raise RunFailure(
            EXIT_CONFIG,
            "--offline replays a recorded copy of each page, and %s has no "
            "`fixture` in %s. Either record one and point `fixture` at it, or "
            "drop --offline." % (", ".join(missing), cfg.path or "the config"))
    return FixtureFetcher({src.url: src.fixture for src in cfg.sources})


def main(argv: Optional[List[str]] = None, fetcher: Optional[Fetcher] = None,
         log=None, cfg: Optional[Config] = None) -> int:
    log = log or sys.stderr
    args = build_parser().parse_args(argv)

    try:
        cfg = resolve_config(args, cfg)
        if fetcher is None:
            fetcher = build_fetcher(cfg, args.offline)
    except RunFailure as exc:
        print("course-schedule-ics FAILED: %s" % exc, file=sys.stderr)
        return exc.code

    if args.fall_year is not None:
        cfg.anchor = fall_anchor(args.fall_year)
    if args.alarm_days is not None:
        cfg.alarm_days_before = args.alarm_days

    out_dir = os.path.abspath(args.out_dir)
    state_path = os.path.join(out_dir, "state.json")
    ics_path = os.path.join(out_dir, "schedule.ics")

    st = state_mod.load(state_path)
    old_records = state_mod.records_from_state(st)

    prev_counts = previous_counts(st)

    try:
        records, src_meta, counts = collect(fetcher, cfg, st, log=log)
        check_health(records, counts, prev_counts, len(old_records), cfg)
    except RunFailure as exc:
        st["last_attempted_run"] = state_mod.now_iso()
        if not args.dry_run:
            try:
                os.makedirs(out_dir, exist_ok=True)
                state_mod.save(state_path, st)
            except OSError:
                pass
        print("course-schedule-ics FAILED: %s" % exc, file=sys.stderr)
        print("last successful run: %s"
              % (st.get("last_successful_run") or "never"), file=sys.stderr)
        return exc.code

    print(summarize(records), file=log)

    if args.list:
        for r in records:
            print("%s %-5s %-14s %-16s %s"
                  % (r.date, r.time or "", r.confidence, r.kind, r.title))

    if args.diff:
        d = state_mod.diff(old_records, records)
        print(state_mod.format_diff(d, len(old_records), len(records)))

    if args.dry_run:
        print("dry run: no files written", file=log)
        return EXIT_OK

    calendar = ics.build_calendar(records, cfg)

    now = state_mod.now_iso()
    previous_success = st.get("last_successful_run") or "never"
    st["version"] = state_mod.STATE_VERSION
    st["sources"] = src_meta
    st["records"] = [r.to_dict() for r in records]
    st["last_attempted_run"] = now
    st["last_successful_run"] = now
    st["parse_signature"] = parse_signature(cfg)

    # The calendar and the state are one fact in two files. Written
    # separately, a disk that fills up between them leaves a half-written
    # calendar no subscriber can parse, or a calendar that has moved on while
    # state.json has not -- which makes --diff re-report the same change on
    # every run, forever. Both are staged and fsynced before either is
    # renamed, so a failure to WRITE leaves both exactly as they were.
    try:
        os.makedirs(out_dir, exist_ok=True)
        state_mod.write_files([
            (ics_path, calendar, ""),
            (state_path, state_mod.dumps(st), None),
        ])
    except state_mod.PartialWrite as exc:
        report_partial_write(exc, previous_success)
        return EXIT_WRITE
    except OSError as exc:
        print("course-schedule-ics FAILED: could not write %s and %s: %s: %s"
              % (ics_path, state_path, type(exc).__name__, exc),
              file=sys.stderr)
        print("nothing was renamed into place: both files still hold exactly "
              "what they held before this run", file=sys.stderr)
        print("last successful run: %s" % previous_success, file=sys.stderr)
        return EXIT_WRITE

    print("wrote %s and %s" % (state_path, ics_path), file=log)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERNAL)
    except Exception as exc:  # noqa: BLE001 - top level guard
        print("course-schedule-ics CRASHED: %r" % exc, file=sys.stderr)
        sys.exit(EXIT_INTERNAL)
