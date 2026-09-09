"""Text lines -> schedule records, with a three-tier confidence judgement."""

import datetime as dt
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import (KIND_ASSIGNMENT_DUE, KIND_ASSIGNMENT_OUT, KIND_LECTURE,
                     KIND_UNKNOWN, Config, SemesterAnchor)

CERTAIN = "certain"
UNCERTAIN = "uncertain"

__all__ = [
    "CERTAIN", "UNCERTAIN",
    "KIND_LECTURE", "KIND_ASSIGNMENT_OUT", "KIND_ASSIGNMENT_DUE",
    "KIND_UNKNOWN", "Record", "Dropped", "parse_line", "parse_page",
    "assign_uids", "dedupe",
]

# Every legal spelling of a month, longest first. The regex alternation is
# built from this tuple, so the set the matcher accepts and the set the lookup
# understands cannot drift apart.
MONTH_TOKENS = (
    ("january", 1), ("jan", 1),
    ("february", 2), ("feb", 2),
    ("march", 3), ("mar", 3),
    ("april", 4), ("apr", 4),
    ("may", 5),
    ("june", 6), ("jun", 6),
    ("july", 7), ("jul", 7),
    ("august", 8), ("aug", 8),
    ("september", 9), ("sept", 9), ("sep", 9),
    ("october", 10), ("oct", 10),
    ("november", 11), ("nov", 11),
    ("december", 12), ("dec", 12),
)
MONTHS = dict(MONTH_TOKENS)
_MONTH_ALT = "|".join(name for name, _ in MONTH_TOKENS)
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

# "Sep. 02, 2026" / "Sep 2" / "Sep.02" / "September 13".  Year optional.
# Requiring a month NAME is the primary false-positive defense: "Chapter 17",
# "Room 315" and "ECE-UY 2004" cannot reach this pattern at all.
#
# Every separator is \s*-tolerant on BOTH sides. Google Sites splits a date
# across <span>s and the surviving text keeps whatever whitespace sat between
# the tags, so the same logical date arrives as any of
#     "Sep. 02, 2026"  "Sep . 02 , 2026"  "Oct .05,2026"  "Oct .  05 , 2026"
# and all of them must parse identically. Whitespace before a period never
# occurs in ordinary prose, so tolerating it costs no precision.
DATE_RE = re.compile(
    r"(?P<month>" + _MONTH_ALT + r")"
    # The month must be spelled correctly and end there. An open-ended "[a-z]*"
    # tail swallows any suffix, so on a robotics page "Augmented 3D perception
    # lab" became 2026-08-03 titled "D perception lab" -- at confidence
    # `certain`, next to a real row called "Novel Robot MP". A month name is a
    # closed vocabulary; treat it as one.
    r"(?![A-Za-z])"
    r"(?:\s*\.\s*|\s+)"            # a period (with any spacing), or whitespace
    r"(?P<day>\d{1,2})"
    r"(?:\s*,\s*(?P<year>\d{4}))?"
    r"(?![\d:])",
    re.IGNORECASE,
)

WEEKDAY_RE = re.compile(
    r"\b(Mon|Tues?|Tue|Wednes|Wed|Thurs?|Thu|Fri|Satur|Sat|Sun)(?:day|nesday|rsday|urday)?\b",
    re.IGNORECASE,
)

# Numbers that look like dates but are not.  Every one of these is a real
# false positive observed in course-page prose.
TRAP_PREFIX_RE = re.compile(
    r"(?i)\b(?:chapter|chap|ch|case|question|questions|q|problem|problems|"
    r"section|sec|figure|fig|table|room|rm|building|bldg|page|pg|part|"
    r"exercise|slide|lab|unit|module|version|v|no|number|#)\.?\s*$"
)
COURSE_CODE_RE = re.compile(r"[A-Z]{2,4}[-\s]?UY[-\s]?\d{4}", re.IGNORECASE)

# Two different questions, and they were being asked with one regex.
#
#   RECOGNISING a deadline: "does the text just before this date mean the date
#   is a due date?" Being wrong here costs a deadline its `DUE:` prefix and its
#   23:59 time, turning it into an all-day event indistinguishable from a
#   release date -- the one confusion the prefixes exist to prevent. So the
#   vocabulary is deliberately WIDE: a page that renames its "Due" column to
#   "Submission" has not stopped having deadlines.
#
#   STRIPPING a cue off the end of a title: "is this trailing word part of the
#   name of the item, or is it pointing at the next date on the line?" Being
#   wrong here silently truncates a title -- and the title is hashed into the
#   UID, so a truncation also rotates the UID and the calendar deletes the
#   event and creates a new one. So the vocabulary must be NARROW.
#
# Sharing one regex made the second question inherit the first one's width,
# and the widened list ate real titles:
#     "Lab report submission" -> "Lab report"
#     "Assignment Hand-in"    -> "Assignment"
#     "Project handed in"     -> "Project"
#     "How to Submit"         -> "How to"
# all of them titles that simply END in a word the deadline vocabulary knows.
#
# The cue has to sit immediately before the date (the trailing `$`), so a
# sentence that merely contains the word cannot promote an unrelated date.
DUE_CUE_RE = re.compile(
    r"(?i)\b("
    r"due(?:\s+(?:by|on|date))?|"
    r"deadline|dues?\s+on|"
    r"submissions?(?:\s+(?:by|on|due|date|deadline))?|"
    r"submit(?:ted|s|ting)?(?:\s+(?:by|on))?|"
    r"hand(?:ed|s|ing)?\s*-?\s*in(?:\s+(?:by|on))?|"
    r"turn(?:ed|s|ing)?\s*-?\s*in(?:\s+(?:by|on))?"
    r")\b[\s:.-]*$"
)

# Removable from the end of a title. Everything except "due" and "deadline"
# must be followed by a preposition or a qualifier, because that is what
# separates an instruction pointing at a date ("submission by", "handed in
# on") from a noun that ends a perfectly ordinary title ("submission",
# "hand-in"). A bare "Submit" is a title; "Submit by" is a cue.
#
# Deliberately a subset of DUE_CUE_RE: recognising more cues than we strip
# costs nothing, while stripping more than we recognise costs a title.
TRAILING_CUE_RE = re.compile(
    r"(?i)\b("
    r"due(?:\s+(?:by|on|date))?|"
    r"deadline|dues?\s+on|"
    r"submissions?\s+(?:by|on|due|date|deadline)|"
    r"submit(?:ted|s|ting)?\s+(?:by|on)|"
    r"hand(?:ed|s|ing)?\s*-?\s*in\s+(?:by|on)|"
    r"turn(?:ed|s|ing)?\s*-?\s*in\s+(?:by|on)"
    r")\b[\s:.-]*$"
)
NEGATION_RE = re.compile(
    r"(?i)\b(not|instead\s+of|moved\s+to|rescheduled|reschedule|cancell?ed|"
    r"cancel|postponed|no\s+class|tbd|tba|may\s+change|subject\s+to\s+change)\b"
)
RELATIVE_ONLY_RE = re.compile(
    r"(?i)\b(next\s+week|this\s+week|last\s+week|tomorrow|today|"
    r"end\s+of\s+(?:the\s+)?(?:week|month|semester))\b"
)
TIME_RE = re.compile(
    r"(?i)\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b"
    r"|(?P<h24>[01]?\d|2[0-3]):(?P<m24>[0-5]\d)\b"
)

# Lines that are page furniture, never schedule content.
NOISE_LINES = {
    "search this site", "embedded files", "skip to main content",
    "skip to navigation", "report abuse", "page details", "page updated",
    "more", "home", "lectures", "assignments", "projects", "google sites",
}


@dataclass
class Record:
    date: str                     # ISO YYYY-MM-DD
    title: str
    kind: str
    source_url: str
    confidence: str
    time: Optional[str] = None    # "HH:MM" local, or None for an all-day item
    context: str = ""             # raw source line, for human review
    reasons: List[str] = field(default_factory=list)
    uid: str = ""

    def identity(self) -> Tuple[str, str, str]:
        return (self.source_url, self.title, self.kind)

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "Record":
        return Record(
            date=d["date"], title=d["title"], kind=d["kind"],
            source_url=d["source_url"], confidence=d["confidence"],
            time=d.get("time"), context=d.get("context", ""),
            reasons=list(d.get("reasons", [])), uid=d.get("uid", ""),
        )


@dataclass
class Dropped:
    """A fragment that looked like a date but could not become a record.

    Silence here is the dangerous case: "Feb. 30, 2026 Assignment 9" is a real
    typo shape, and dropping it wordlessly means the deadline simply is not in
    the calendar and nothing ever says so. Every drop is surfaced.
    """

    text: str        # the matched fragment, e.g. "Feb. 30, 2026"
    reason: str
    context: str     # the source line it was found on

    def describe(self) -> str:
        return "%r in %r: %s" % (self.text, _snippet(self.context), self.reason)


def _snippet(line: str, limit: int = 90) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= limit else line[:limit - 1] + "\u2026"


def _clean_title(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    t = t.strip(" \t-–—:,;.")
    return t.strip()


def _is_trapped(line: str, match: "re.Match") -> Optional[str]:
    """Return a reason string if this match is a known false positive."""
    before = line[:match.start()]
    if TRAP_PREFIX_RE.search(before):
        kw = TRAP_PREFIX_RE.search(before).group(0).strip()
        return "preceded by non-date keyword %r" % kw
    # A course code overlapping the match, e.g. "ROB UY 3303" / "ECE-UY 2004".
    for cc in COURSE_CODE_RE.finditer(line):
        if cc.start() <= match.start() < cc.end() or \
           cc.start() < match.end() <= cc.end():
            return "inside course code %r" % cc.group(0)
    return None


def _find_time(text: str) -> Optional[str]:
    m = TIME_RE.search(text)
    if not m:
        return None
    if m.group("h24") is not None:
        return "%02d:%s" % (int(m.group("h24")), m.group("m24"))
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ap = m.group("ap").lower()
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return "%02d:%02d" % (hour, minute)


@dataclass
class _Hit:
    match: "re.Match"
    month: int
    day: int
    year: Optional[int]
    cued_due: bool


def _scan_dates(line: str) -> Tuple[List[_Hit], List[str], List[Tuple[str, str]]]:
    """All real date hits on a line, plus what was rejected and why.

    Two kinds of rejection, and they must not be reported the same way.

    A TRAP -- "Chapter 17", "Room 315", "ECE-UY 2004" -- is not a date and was
    never meant to be one. Announcing those would bury the real signal under
    dozens of lines of noise per page, which is how a drop report stops being
    read. They stay in `rejected`, which only annotates records produced from
    the same line.

    An IMPOSSIBLE DAY -- "Sep. 32" -- is a date the writer meant, spelled
    wrongly. It is the same human typo as "Feb. 30", and had the same
    consequence: no record, no message, exit 0. But "Feb. 30" was reported and
    "Sep. 32" was not, purely because of where in the pipeline each was
    caught: an impossible day was filtered out here, before the hit list, and
    `parse_line` returns early on an empty hit list and discards `rejected`
    with it. Same class of mistake, same invisible deadline, opposite
    treatment. They are returned separately so both reach the drop report.
    """
    hits = []  # type: List[_Hit]
    rejected = []  # type: List[str]
    impossible = []  # type: List[Tuple[str, str]]
    for m in DATE_RE.finditer(line):
        trap = _is_trapped(line, m)
        if trap:
            rejected.append("dropped %r: %s" % (m.group(0), trap))
            continue
        month = MONTHS[m.group("month").lower()]
        day = int(m.group("day"))
        if not 1 <= day <= 31:
            impossible.append((
                m.group(0),
                "day %d is outside 1-31, so it is not a day of any month"
                % day))
            continue
        year = int(m.group("year")) if m.group("year") else None
        cued = bool(DUE_CUE_RE.search(line[:m.start()]))
        hits.append(_Hit(match=m, month=month, day=day, year=year, cued_due=cued))
    return hits, rejected, impossible


def _resolve_year(hit: _Hit,
                  anchor: SemesterAnchor) -> Tuple[Optional[int], List[str], bool]:
    """Decide the year for one date hit.

    Precedence, in order:
      1. A year written on the page wins, always.
      2. Otherwise the SemesterAnchor fills one in.
      3. If neither applies (no page year, month outside the anchor) the hit is
         dropped -- we never guess.

    Returns (year, reasons, conflict). `conflict` is True when the page states
    a year that disagrees with the anchor. The page still wins -- it is the
    primary source -- but the record is downgraded to `uncertain` so a human
    looks at it, because exactly one of the two is wrong and we cannot tell
    which from the text alone.
    """
    if hit.year is not None:
        reasons = ["year %d taken from the page" % hit.year]
        try:
            anchored = anchor.year_for_month(hit.month)
        except ValueError:
            # The anchor has no opinion about this month, so there is nothing
            # to disagree with.
            return hit.year, reasons, False
        if anchored != hit.year:
            reasons.append(
                "year conflict: the page says %d but semester anchor %r puts "
                "month %02d in %d; keeping the page's year and flagging the "
                "record" % (hit.year, anchor.name, hit.month, anchored))
            return hit.year, reasons, True
        return hit.year, reasons, False

    try:
        filled = anchor.year_for_month(hit.month)
    except ValueError as exc:
        return None, [str(exc)], False
    return filled, ["year absent from the page; filled %d from semester "
                    "anchor %r" % (filled, anchor.name)], False


def _weekday_check(line: str, hit: _Hit, date: dt.date) -> List[str]:
    """If a weekday name sits just before the date, verify it."""
    window = line[max(0, hit.match.start() - 24):hit.match.start()]
    names = WEEKDAY_RE.findall(window)
    if not names:
        return []
    raw = names[-1].lower()
    expected = None
    for key in (raw, raw + "day", raw + "nesday", raw + "rsday", raw + "urday"):
        if key in WEEKDAYS:
            expected = WEEKDAYS[key]
            break
    if expected is None:
        return []
    if expected != date.weekday():
        got = ["Monday", "Tuesday", "Wednesday", "Thursday",
               "Friday", "Saturday", "Sunday"]
        return ["weekday conflict: text says %s but %s is a %s"
                % (raw.capitalize(), date.isoformat(), got[date.weekday()])]
    return []


def parse_line(line: str, source_url: str, default_kind: str,
               cfg: Config,
               dropped: Optional[List[Dropped]] = None) -> List[Record]:
    """Turn one text line into zero or more records.

    `dropped`, if given, collects every fragment that matched the date pattern
    but could not be turned into a record, so the caller can report it instead
    of losing it.
    """
    stripped = line.strip()
    if not stripped or stripped.lower() in NOISE_LINES:
        return []

    hits, rejected, impossible = _scan_dates(stripped)
    # Reported whether or not anything else on the line survived: an
    # impossible day is a typo over a date somebody meant to publish, and the
    # only place it can be noticed is the drop report.
    if dropped is not None:
        for text, reason in impossible:
            dropped.append(Dropped(text=text, context=stripped, reason=reason))
    if not hits:
        return []  # No date -> discard. We never guess.

    records = []  # type: List[Record]
    multi = len(hits) > 1
    negated = bool(NEGATION_RE.search(stripped))
    relative = bool(RELATIVE_ONLY_RE.search(stripped))

    # Pass 1: the text segment each date owns (from its end to the next date).
    segments = []  # type: List[str]
    for idx, hit in enumerate(hits):
        seg_end = hits[idx + 1].match.start() if idx + 1 < len(hits) else len(stripped)
        seg = stripped[hit.match.end():seg_end]
        # A trailing due-cue introduces the NEXT date; it is not this title.
        # Stripped with the NARROW vocabulary -- see TRAILING_CUE_RE. Getting
        # this wrong truncates a title, and the title is in the UID.
        segments.append(TRAILING_CUE_RE.sub("", seg))

    for idx, hit in enumerate(hits):
        year, year_reasons, year_conflict = _resolve_year(hit, cfg.anchor)
        if year is None:
            if dropped is not None:
                dropped.append(Dropped(
                    text=hit.match.group(0), context=stripped,
                    reason="no year on the page and " +
                           (year_reasons[0] if year_reasons
                            else "no semester anchor covers that month")))
            continue
        try:
            date = dt.date(year, hit.month, hit.day)
        except ValueError:
            if dropped is not None:
                dropped.append(Dropped(
                    text=hit.match.group(0), context=stripped,
                    reason="%04d-%02d-%02d is not a real calendar date"
                           % (year, hit.month, hit.day)))
            continue

        reasons = list(year_reasons)
        conf = CERTAIN

        # --- title: the text this date owns ---
        segment = segments[idx]
        title = _clean_title(segment)
        if not title:
            # A trailing "... Due Sep. 13" owns no text of its own; it refers
            # to the item named by the previous date on the same line.
            for back in range(idx - 1, -1, -1):
                title = _clean_title(segments[back])
                if title:
                    break
        if not title:
            title = _clean_title(stripped[:hit.match.start()]) or "(untitled)"

        # --- kind ---
        # A due cue ("Due ...", "Deadline ...") always names a deadline. With
        # no cue the date takes whatever the source page was configured to
        # mean. That is a per-page setting because pages state their own
        # convention -- the recorded assignments page says "Assignments begin
        # on the dates listed left, and are due ... on the day listed below
        # right", which makes an un-cued date there a release date, not a
        # deadline.
        if hit.cued_due:
            kind = KIND_ASSIGNMENT_DUE
        else:
            kind = default_kind
        if kind == KIND_ASSIGNMENT_OUT:
            reasons.append("no due cue on this date; this page is configured "
                           "as one whose plain dates are release dates")
        elif kind == KIND_UNKNOWN and re.search(r"(?i)\bassignment\b", stripped):
            reasons.append("assignment-related date with no explicit due cue "
                           "and no page convention to fall back on")

        # --- confidence ---
        if year_conflict:
            # The explaining reason is already in year_reasons.
            conf = UNCERTAIN
        if multi and not hit.cued_due and idx > 0:
            # A trailing date with no cue of its own really is ambiguous.
            conf = UNCERTAIN
            reasons.append("multiple dates in one context and this one carries "
                           "no disambiguating cue")
        # "may 5 students" reads as a date to any month-name matcher. A
        # lowercase "may" only counts as a month when something else on the
        # line corroborates it (an explicit year, or a due cue).
        if hit.match.group("month").lower() == "may" \
                and hit.match.group("month") != "May" \
                and hit.year is None and not hit.cued_due:
            conf = UNCERTAIN
            reasons.append("lowercase 'may' is more often a verb than a month")
        if negated:
            conf = UNCERTAIN
            kw = NEGATION_RE.search(stripped).group(0)
            reasons.append("negation/correction word %r near the date" % kw)
        if relative:
            conf = UNCERTAIN
            reasons.append("relative date expression in the same context")
        wd = _weekday_check(stripped, hit, date)
        if wd:
            conf = UNCERTAIN
            reasons.extend(wd)
        if rejected:
            reasons.extend(rejected)

        # --- time ---
        clock = _find_time(segment) or _find_time(stripped[:hit.match.start()])
        if clock is None and kind == KIND_ASSIGNMENT_DUE:
            clock = cfg.default_due_time
            reasons.append("no explicit time; applied default due time %s"
                           % cfg.default_due_time)

        records.append(Record(
            date=date.isoformat(), title=title, kind=kind,
            source_url=source_url, confidence=conf, time=clock,
            context=stripped, reasons=reasons,
        ))
    return records


def assign_uids(records: List[Record]) -> None:
    """Stable, reproducible UIDs.

    The date is excluded whenever (url, title, kind) identifies exactly one
    row, which is the overwhelmingly common case: a lecture that moves keeps
    its UID, so the calendar UPDATES the event instead of deleting it and
    creating a second one, and --diff reports "moved" rather than
    "removed + added".

    When the key does NOT identify one row the date goes in. ROB-UY 3303 has
    two rows called "Project working session", on Dec 07 and Dec 09. Numbering
    them by sorted position was fake stability: delete the Dec 07 row and the
    Dec 09 row inherits its UID, so --diff invents a reschedule that never
    happened, reports the vanished item under the wrong date, and any local
    edit the student made to the Dec 09 event is silently reassigned. For rows
    that are otherwise indistinguishable the date IS the identity, so it
    belongs in the hash.

    The cost is that a same-titled row which moves reads as delete + add. That
    is the price of not being able to tell two identical rows apart at all --
    and deletion, the more common and more harmful case, becomes correct.
    A "#N" tiebreaker still guarantees uniqueness if two such rows also share
    a date.
    """
    ambiguous = {}  # type: Dict[Tuple[str, str, str], int]
    for rec in records:
        key = rec.identity()
        ambiguous[key] = ambiguous.get(key, 0) + 1

    seen = {}  # type: Dict[Tuple, int]
    for rec in records:
        key = rec.identity()
        parts = [rec.source_url, rec.title, rec.kind]
        if ambiguous[key] > 1:
            parts.append(rec.date)
        dedupe_key = tuple(parts)
        n = seen.get(dedupe_key, 0)
        seen[dedupe_key] = n + 1
        raw = "\x00".join(parts)
        if n:
            raw += "\x00#%d" % n
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        rec.uid = "%s@course-schedule-ics" % digest


def parse_page(page_html: str, source_url: str, default_kind: str,
               cfg: Config,
               dropped: Optional[List[Dropped]] = None) -> List[Record]:
    from .extract import to_lines
    out = []  # type: List[Record]
    for line in to_lines(page_html):
        out.extend(parse_line(line, source_url, default_kind, cfg, dropped))
    return out


def dedupe(records: List[Record]) -> List[Record]:
    """Drop byte-identical duplicates while preserving document order."""
    seen = set()
    out = []
    for rec in records:
        key = (rec.source_url, rec.date, rec.title, rec.kind, rec.time)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out
