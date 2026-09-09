"""Hand-rolled RFC 5545 output. No third-party iCalendar library."""

import datetime as dt
from typing import List, Optional

from .config import Config
from .parse import (KIND_ASSIGNMENT_DUE, KIND_ASSIGNMENT_OUT, UNCERTAIN,
                    Record)
from .timezones import VTIMEZONES

PRODID = "-//course-schedule-ics//course schedule 0.1//EN"

# When a page carries both halves of an assignment, the two dates land in the
# same calendar a fortnight apart, so the SUMMARY has to say which is which at
# a glance -- an unlabelled "Assignment 3" on two different days is worse than
# no event at all.
SUMMARY_PREFIX = {
    KIND_ASSIGNMENT_DUE: "DUE: ",
    KIND_ASSIGNMENT_OUT: "ASSIGNED: ",
}

# A TZID names a zone; the file has to define it. The definitions live in
# timezones.py, keyed by IANA name; `config.load_config` refuses a zone that is
# not in that table, so an unknown TZID cannot reach this module from a config
# file. Re-exported here because `ics.VTIMEZONES` is where a reader looks.
#
# Structured values (RRULE, offsets) are NOT escaped -- escape() is for TEXT
# properties, and escaping the ";" in an RRULE would corrupt it.


def timezone_id(cfg: Config) -> str:
    """The zone to stamp on timed events, or "" for floating local time.

    A TZID the file never defines is worse than no TZID, so an unknown zone
    degrades to floating rather than emitting a dangling reference.
    """
    tzid = getattr(cfg, "timezone", "") or ""
    return tzid if tzid in VTIMEZONES else ""


def escape(text: str) -> str:
    """RFC 5545 section 3.3.11 text escaping."""
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,")
                .replace("\r\n", "\\n")
                .replace("\n", "\\n")
                .replace("\r", "\\n"))


def uri_value(uri: str) -> str:
    """Make a string safe to emit as a URI-valued property.

    Every other property here goes through escape(), which neutralises CR and
    LF -- the only characters that can forge a property or a component
    boundary. `URL:` did not, so a source_url containing a newline would have
    written a structure line straight into the file. Unreachable today
    (`source_url` comes from `cfg.sources`, and a record smuggled in through a
    corrupt state.json never survives the 304 path's health check), but the
    gap costs a line to close and reasoning about reachability is exactly what
    goes stale.

    escape() is deliberately NOT used: `URL` is a URI value, not a TEXT value
    (RFC 5545 3.3.13 vs 3.3.11), and TEXT escaping would put backslashes in
    front of the `,` and `;` that are legal, meaningful URI characters. A URI
    cannot contain a control character at all (RFC 3986), so the right
    treatment for one is percent-encoding, not escaping. Space goes too, for
    the same reason.
    """
    out = []
    for ch in uri:
        if ch <= " " or ch == "\x7f":
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
        else:
            out.append(ch)
    return "".join(out)


def fold(line: str) -> List[str]:
    """Fold to <=75 octets per line, continuations start with one space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out = []
    chunk = bytearray()
    limit = 75
    for ch in line:
        enc = ch.encode("utf-8")
        if len(chunk) + len(enc) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
            limit = 74  # continuation lines carry a leading space
        chunk += enc
    if chunk:
        out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + p for p in out[1:]]


def _utc_stamp(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def _trigger(days: int) -> str:
    if days <= 0:
        return "-PT0M"
    return "-P%dD" % days


def build_event(rec: Record, cfg: Config, stamp: str) -> List[str]:
    date = dt.date.fromisoformat(rec.date)
    lines = ["BEGIN:VEVENT", "UID:%s" % rec.uid, "DTSTAMP:%s" % stamp]

    if rec.time:
        hh, mm = rec.time.split(":")
        # A deadline is a fixed instant, not a wall-clock time that follows the
        # reader around. Floating time put a traveller's 23:59 three hours
        # after the real cutoff had passed.
        tzid = timezone_id(cfg)
        param = ";TZID=%s" % tzid if tzid else ""
        lines.append("DTSTART%s:%sT%s%s00"
                     % (param, date.strftime("%Y%m%d"), hh, mm))
        end = dt.datetime.combine(date, dt.time(int(hh), int(mm))) + \
            dt.timedelta(minutes=30)
        lines.append("DTEND%s:%s" % (param, end.strftime("%Y%m%dT%H%M00")))
    else:
        lines.append("DTSTART;VALUE=DATE:%s" % date.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:%s" %
                     (date + dt.timedelta(days=1)).strftime("%Y%m%d"))

    summary = SUMMARY_PREFIX.get(rec.kind, "") + rec.title
    if rec.confidence == UNCERTAIN:
        summary = "[?] %s" % summary
    lines.append("SUMMARY:%s" % escape(summary))

    desc = ["kind: %s" % rec.kind, "confidence: %s" % rec.confidence]
    if rec.confidence == UNCERTAIN and rec.reasons:
        desc.append("why uncertain:")
        desc.extend("  - %s" % r for r in rec.reasons)
    elif rec.reasons:
        desc.extend("note: %s" % r for r in rec.reasons)
    desc.append("source line: %s" % rec.context)
    desc.append("source: %s" % rec.source_url)
    lines.append("DESCRIPTION:%s" % escape("\n".join(desc)))
    lines.append("URL:%s" % uri_value(rec.source_url))
    lines.append("CATEGORIES:%s" % escape(rec.kind))

    lines.append("BEGIN:VALARM")
    lines.append("ACTION:DISPLAY")
    lines.append("TRIGGER:%s" % _trigger(cfg.alarm_days_before))
    lines.append("DESCRIPTION:%s" % escape(summary))
    lines.append("END:VALARM")
    lines.append("END:VEVENT")
    return lines


def build_calendar(records: List[Record], cfg: Config,
                   now: Optional[dt.datetime] = None) -> str:
    stamp = _utc_stamp(now)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:%s" % PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:%s" % escape(cfg.calendar_name),
    ]
    # The zone must be defined before anything references it.
    tzid = timezone_id(cfg)
    if tzid and any(rec.time for rec in records):
        lines.extend(VTIMEZONES[tzid])
    for rec in records:
        lines.extend(build_event(rec, cfg, stamp))
    lines.append("END:VCALENDAR")

    folded = []
    for line in lines:
        folded.extend(fold(line))
    return "\r\n".join(folded) + "\r\n"
