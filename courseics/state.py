"""state.json read/write and the human-readable diff."""

import datetime as dt
import json
import os
import re
from typing import Dict, List, Optional, Tuple

from .parse import Record

STATE_VERSION = 1

# Exactly what parse.py writes: `date.isoformat()` and "%02d:%02d".
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# One-digit hours are tolerated because `Config.default_due_time` is a string a
# user may set to "9:00"; the range is checked separately.
_CLOCK_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")

# Every field a stored record must carry, and the type each reader assumes.
_RECORD_TEXT_FIELDS = ("date", "title", "kind", "source_url", "confidence")
_RECORD_OPTIONAL_TEXT = ("context", "uid")


def _valid_clock(value) -> bool:
    if value is None:
        return True                      # an all-day record, by design
    if not isinstance(value, str):
        return False
    m = _CLOCK_RE.match(value)
    if not m:
        return False
    return int(m.group(1)) <= 23 and int(m.group(2)) <= 59


def valid_record(raw) -> bool:
    """Can this stored record survive every path that reads it?

    Checking that `records` is a list was not enough. A single mistyped field
    inside one record crashed the tool at exit 1 -- "unexpected internal
    error", i.e. "this is a bug, read the traceback" -- for a file the README
    invites the user to inspect and promises is treated as empty when corrupt:

      * `--diff`, the everyday path, formats `rec.date + " " + rec.time`, so
        `time: 5` was a TypeError before anything was written;
      * `date` or `title` of mixed types across records made the diff's sort
        compare an int with a str, another TypeError;
      * on the 304 path a non-string `time` reached `"...".split(":")`,
        `date: null` reached the record sort, `date: "junk"` reached
        `dt.date.fromisoformat`, and a non-string `title`/`kind` reached the
        `"\\x00".join(...)` inside `assign_uids`.

    Each is validated here, at the boundary, rather than defended against in
    five different modules. A record that fails is dropped on its own: one bad
    row costs one row, exactly as one bad key costs one key.
    """
    if not isinstance(raw, dict):
        return False
    for key in _RECORD_TEXT_FIELDS:
        if not isinstance(raw.get(key), str):
            return False
    if not _ISO_DATE_RE.match(raw["date"]):
        return False
    try:
        dt.date.fromisoformat(raw["date"])
    except ValueError:
        return False                     # "2026-02-30" parses as a shape only
    if not _valid_clock(raw.get("time")):
        return False
    for key in _RECORD_OPTIONAL_TEXT:
        if key in raw and not isinstance(raw[key], str):
            return False
    reasons = raw.get("reasons")
    if reasons is not None:
        if not isinstance(reasons, list):
            return False
        if not all(isinstance(r, str) for r in reasons):
            return False
    return True


def empty_state() -> Dict:
    return {
        "version": STATE_VERSION,
        "last_successful_run": None,
        "last_attempted_run": None,
        # url -> {etag, last_modified, fetched_at, status, record_count}
        "sources": {},
        "records": [],
        # Which parse-affecting options produced `records`. See cli.py.
        "parse_signature": "",
    }


def load(path: str) -> Dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return empty_state()
    return sanitize(data)


def sanitize(data) -> Dict:
    """Coerce whatever was on disk into a state shape the rest of the code
    can rely on, discarding anything of the wrong type.

    Valid JSON is not a valid state file. `{"records": null}`,
    `{"records": 5}` and `{"sources": "nope"}` all parsed cleanly and then
    exploded with a TypeError or a ValueError several modules away, at exit 1
    -- but README promises that a corrupt state file is treated as empty
    rather than crashing, and a state file that cannot be read is exactly the
    moment the tool most needs to still run.

    Each field is validated on its own, so one bad key costs only that key.
    Unrecognised keys are carried through untouched: they are nobody's
    business here, and dropping them would break a future reader.

    That goes one level deeper than the top-level keys: a record is only kept
    if every field inside it has the type its readers assume -- see
    `valid_record`.
    """
    base = empty_state()
    if not isinstance(data, dict):
        return base

    for key, value in data.items():
        if key not in base:
            base[key] = value

    records = data.get("records")
    if isinstance(records, list):
        base["records"] = [r for r in records if valid_record(r)]

    sources = data.get("sources")
    if isinstance(sources, dict):
        base["sources"] = {k: v for k, v in sources.items()
                           if isinstance(k, str) and isinstance(v, dict)}

    for key in ("last_successful_run", "last_attempted_run"):
        value = data.get(key)
        if value is None or isinstance(value, str):
            base[key] = value

    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        base["version"] = version

    signature = data.get("parse_signature")
    if isinstance(signature, str):
        base["parse_signature"] = signature

    return base


def dumps(state: Dict) -> str:
    return json.dumps(state, indent=2, ensure_ascii=False,
                      sort_keys=True) + "\n"


class PartialWrite(OSError):
    """A rename succeeded and a later one did not.

    The files are now out of step and there is no way back: the earlier
    destination has already been replaced. This exists so the caller can say
    what actually happened instead of falling back on the all-or-nothing
    message, which would be a lie -- and a tool whose whole safety story rests
    on "a failure tells you the truth" cannot afford one.
    """

    def __init__(self, cause: BaseException, renamed: List[str],
                 not_renamed: List[str], leftovers: List[str]):
        super().__init__("%s: %s" % (type(cause).__name__, cause))
        self.cause = cause
        self.renamed = renamed          # already replaced; the new content
        self.not_renamed = not_renamed  # untouched; still the old content
        self.leftovers = leftovers      # .tmp files that could not be removed


def _discard(paths: List[str]) -> List[str]:
    """Remove temp files; return the ones that would not go away."""
    stuck = []
    for tmp in paths:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            stuck.append(tmp)
    return stuck


def write_files(items: List[Tuple[str, str, Optional[str]]]) -> None:
    """Write several files as one step, and never misreport the outcome.

    Each item is (path, text, newline). Every temp file is written, flushed
    and fsynced FIRST; only when all of them are safely on disk are they
    renamed into place. So a full disk, a quota, a permission error or an
    interrupt leaves every destination exactly as it was, and raises a plain
    OSError.

    This matters because the two files are one fact split in half. Writing
    schedule.ics with a plain open() truncated the good calendar to whatever
    fitted before the failure -- 20 KB ending mid-VEVENT, no END:VCALENDAR, a
    file no subscriber can parse -- which is precisely what README's "a broken
    run never overwrites a good calendar" promises cannot happen. And a run
    that updated the calendar but failed to update state.json left the two
    permanently out of step, so --diff re-reported the same change forever.

    THE RENAME PHASE CAN FAIL TOO, and it used to run bare. os.replace() is
    not merely "microseconds during which the machine might crash": replace a
    destination with a non-empty directory and the call raises, every time,
    reproducibly. What happened then was worse than the desync itself --
    schedule.ics had already been replaced, state.json had not, the temp file
    was left behind, and the caller printed "neither file was modified".

    A false all-clear is the one failure this design cannot absorb. Every
    guard here is built on the assumption that a bad run says so; an alarm
    that can be wrong in the reassuring direction is not an alarm.

    So the renames are guarded too. Nothing renamed yet -> plain OSError, and
    the all-or-nothing promise still holds. Something renamed already ->
    PartialWrite, which names what moved, what did not, and any temp file that
    survived, so the caller can report the state the disk is actually in.
    Rolling back is not attempted: the previous content of an os.replace()d
    destination is gone, and a rollback that cannot restore it would be the
    same lie one layer down.
    """
    staged = []  # type: List[Tuple[str, str]]
    try:
        for path, text, newline in items:
            tmp = path + ".tmp"
            staged.append((tmp, path))
            with open(tmp, "w", encoding="utf-8", newline=newline) as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
    except BaseException:
        _discard([tmp for tmp, _ in staged])
        raise

    renamed = []  # type: List[str]
    for index, (tmp, path) in enumerate(staged):
        try:
            os.replace(tmp, path)
        except BaseException as exc:
            # Whatever is left staged is now worthless; do not leave it lying
            # next to the real files where the next reader will wonder.
            stuck = _discard([t for t, _ in staged[index:]])
            # KeyboardInterrupt and SystemExit are propagated as themselves:
            # wrapping a Ctrl-C in an OSError subclass would make the program
            # ignore the one instruction the user gave it directly.
            if renamed and isinstance(exc, Exception):
                raise PartialWrite(
                    exc, renamed=renamed,
                    not_renamed=[p for _, p in staged[index:]],
                    leftovers=stuck) from exc
            raise
        renamed.append(path)


def save(path: str, state: Dict) -> None:
    write_files([(path, dumps(state), None)])


def records_from_state(state: Dict) -> List[Record]:
    out = []
    for raw in state.get("records") or []:
        try:
            out.append(Record.from_dict(raw))
        except (KeyError, TypeError):
            continue
    return out


def caching_for(state: Dict, url: str) -> Dict[str, str]:
    entry = (state.get("sources") or {}).get(url) or {}
    return {"etag": entry.get("etag") or "",
            "last_modified": entry.get("last_modified") or ""}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def diff(old: List[Record], new: List[Record]) -> Dict[str, List]:
    """Compare by UID. Returns added / moved / changed / removed."""
    old_by_uid = {r.uid: r for r in old if r.uid}
    new_by_uid = {r.uid: r for r in new if r.uid}

    added = [new_by_uid[u] for u in new_by_uid if u not in old_by_uid]
    removed = [old_by_uid[u] for u in old_by_uid if u not in new_by_uid]
    moved = []      # type: List[Tuple[Record, Record]]
    changed = []    # type: List[Tuple[Record, Record]]
    for uid in new_by_uid:
        if uid not in old_by_uid:
            continue
        o, n = old_by_uid[uid], new_by_uid[uid]
        if o.date != n.date or o.time != n.time:
            moved.append((o, n))
        if o.title != n.title or o.confidence != n.confidence:
            # A title CAN change under a stable UID now that a lecture is
            # keyed by its date (parse.DATE_KEYED_KINDS): the recorded course
            # retitled one slot twice in a day, and that has to read as an
            # edit to one event rather than a deletion plus an arrival.
            #
            # Not an `elif`. A lecture's UID carries its date but not its
            # clock time, so a slot that is retitled AND moved to a different
            # hour is a real input that belongs in both lists; saying only
            # half of it would be the same silence this section exists to
            # break. A title-keyed row cannot reach both (its title is in the
            # UID), and neither can a lecture that changed date.
            changed.append((o, n))

    added.sort(key=lambda r: (r.date, r.title))
    removed.sort(key=lambda r: (r.date, r.title))
    moved.sort(key=lambda p: (p[1].date, p[1].title))
    changed.sort(key=lambda p: (p[1].date, p[1].title))
    return {"added": added, "removed": removed, "moved": moved,
            "changed": changed}


def _stamp(rec: Record) -> str:
    return "%s%s" % (rec.date, " " + rec.time if rec.time else "")


def format_diff(d: Dict[str, List], old_count: int, new_count: int) -> str:
    out = []
    out.append("Schedule diff: %d record(s) previously, %d now."
               % (old_count, new_count))
    total = sum(len(v) for v in d.values())
    if not total:
        out.append("No changes.")
        return "\n".join(out)

    if d["added"]:
        out.append("")
        out.append("NEW (%d):" % len(d["added"]))
        for r in d["added"]:
            flag = " [?]" if r.confidence == "uncertain" else ""
            out.append("  + %s  %s (%s)%s" % (_stamp(r), r.title, r.kind, flag))
    if d["moved"]:
        out.append("")
        out.append("DATE CHANGED (%d):" % len(d["moved"]))
        for o, n in d["moved"]:
            out.append("  ~ %s (%s)" % (n.title, n.kind))
            out.append("      was %s  ->  now %s" % (_stamp(o), _stamp(n)))
    if d["changed"]:
        out.append("")
        out.append("DETAILS CHANGED (%d):" % len(d["changed"]))
        for o, n in d["changed"]:
            out.append("  ~ %s  %s" % (_stamp(n), n.title))
            if o.title != n.title:
                # The old title has to be printed, not merely implied: the
                # whole point of keeping the UID through a retitle is that the
                # reader can see it was the SAME event that was renamed.
                out.append("      title %r  ->  %r" % (o.title, n.title))
            if o.confidence != n.confidence:
                out.append("      confidence %s -> %s"
                           % (o.confidence, n.confidence))
    if d["removed"]:
        out.append("")
        out.append("DISAPPEARED (%d):" % len(d["removed"]))
        for r in d["removed"]:
            out.append("  - %s  %s (%s)" % (_stamp(r), r.title, r.kind))
    return "\n".join(out)
