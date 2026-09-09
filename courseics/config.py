"""Configuration.

Two rules govern this module:

  * Nothing about any particular course is compiled in. Which pages to fetch,
    what the calendar is called, which time zone its deadlines are written in
    and which year an undated "Due Sep. 13" belongs to all come from a config
    file the user writes. `config.example.ini` is the annotated template.

  * Everything tunable lives here rather than being sprinkled through the code.

The file format is INI, read with `configparser` from the standard library.
TOML would be the modern choice, but `tomllib` only arrived in Python 3.11 and
this project supports 3.9 (see DESIGN.md §1); a config file the user has to
hand-edit also wants comments, which JSON does not have.
"""

import configparser
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .timezones import SUPPORTED_TIMEZONES, VTIMEZONES

NAME = "course-schedule-ics"
VERSION = "0.1"
PROJECT_URL = "https://github.com/SichenLiang/course-schedule-ics"


def build_user_agent(contact: str = "") -> str:
    """The User-Agent this tool identifies itself with.

    It names the tool and gives a way to reach whoever is running it, because
    an unattributed scraper is the kind a site operator blocks first. The
    project URL is always there; `contact` adds an address, and is a setting
    rather than a constant because the person to contact is the person running
    it, not the person who wrote it.
    """
    who = "; contact: %s" % contact if contact else ""
    return ("%s/%s (personal course-schedule sync script; not a crawler; "
            "runs a few times per day; +%s%s)" % (NAME, VERSION, PROJECT_URL, who))


USER_AGENT = build_user_agent()

# Seconds to sleep between two consecutive network requests (politeness).
MIN_REQUEST_INTERVAL = 1.0

HTTP_TIMEOUT = 30.0

# Only a placeholder, so that `Config()` is constructible without a file for
# programmatic use and in tests. Every config file must state its own
# `fall_year` -- `load_config` refuses one that does not -- so no real run is
# ever governed by this number.
DEFAULT_FALL_YEAR = 2026


# Record kinds. Defined here rather than in parse.py so that config can name
# a source's default kind without importing the parser (which imports config).
KIND_LECTURE = "lecture"
# A page may say something like "assignments are handed out on the dates listed
# left and are due on the date listed right". The left-hand date is the release
# date, the cued one is the deadline; these two names keep them apart.
KIND_ASSIGNMENT_OUT = "assignment_out"
KIND_ASSIGNMENT_DUE = "assignment_due"
# For a source whose convention has not been established. Dates on such a page
# get no `SUMMARY` prefix and no default time.
KIND_UNKNOWN = "unknown"

KINDS = (KIND_LECTURE, KIND_ASSIGNMENT_OUT, KIND_ASSIGNMENT_DUE, KIND_UNKNOWN)


class ConfigError(Exception):
    """A config file that cannot be used. The message names the file, the
    section and the key, because a config error the user cannot locate is
    barely better than no message at all."""


@dataclass(frozen=True)
class SemesterAnchor:
    """Fallback rule for filling in a year on a date that lacks one.

    THE PAGE ALWAYS WINS. This table is consulted only when a date carries no
    year of its own -- typically a trailing "Due Sep. 13" whose year the page
    never spells out.

    Guessing "the next Sep 13 from today" would be date-dependent and therefore
    not reproducible, so instead we pin an explicit month -> year table for the
    term. Months outside the table are an error, not a guess.
    """

    name: str
    # month number -> year
    month_to_year: Dict[int, int]

    def year_for_month(self, month: int) -> int:
        try:
            return self.month_to_year[month]
        except KeyError:
            raise ValueError(
                "month %d is outside semester anchor %r (covers months %s)"
                % (month, self.name, sorted(self.month_to_year))
            )


def fall_anchor(fall_year: int) -> SemesterAnchor:
    """Aug-Dec -> fall_year, Jan-May -> fall_year + 1 (the spring carry-over).

    One anchor covers a whole academic year, so a spring-semester course set to
    the preceding fall year needs no separate rule. June and July are covered
    by neither half on purpose: a date in them is far more likely to be a
    misparse than a class meeting, and `year_for_month` turns it into a
    reported drop rather than a silent guess.
    """
    m2y = {}
    for m in (8, 9, 10, 11, 12):
        m2y[m] = fall_year
    for m in (1, 2, 3, 4, 5):
        m2y[m] = fall_year + 1
    return SemesterAnchor(name="fall-%d" % fall_year, month_to_year=m2y)


DEFAULT_ANCHOR = fall_anchor(DEFAULT_FALL_YEAR)


@dataclass(frozen=True)
class Source:
    """One page to fetch.

    `name` is the config section's own label, used only in messages.
    `fixture` is a path to a recorded copy of the page, used by `--offline`;
    a source without one simply cannot be replayed offline.
    """

    url: str
    # Which kind un-cued dates on this page default to.
    default_kind: str
    name: str = ""
    fixture: Optional[str] = None


@dataclass
class Config:
    anchor: SemesterAnchor = DEFAULT_ANCHOR
    sources: Tuple[Source, ...] = ()
    # VALARM lead time, in days before the event.
    alarm_days_before: int = 1
    # Clock time given to assignment_due events that carry no explicit time.
    default_due_time: str = "23:59"
    # IANA zone the page's wall-clock deadlines are written in. Must be a key
    # of timezones.VTIMEZONES, because a TZID with no VTIMEZONE to define it is
    # worse than no TZID at all. Set to "" for floating local time.
    #
    # A page that writes "11:59PM EST" usually means "the course's local wall
    # clock", not literally UTC-5: a September deadline in New York is EDT.
    # Naming the zone rather than an offset is what makes both halves of a
    # semester correct.
    timezone: str = ""
    # Abort with a non-zero exit if the TOTAL record count falls by more than
    # this fraction versus the previous successful run.
    max_shrink_ratio: float = 0.5
    # The same guard, applied to each source on its own. A page that carries a
    # minority of the records can lose every one of them without moving the
    # total far enough to trip the global threshold, so the global number
    # alone cannot protect a single page.
    max_source_shrink_ratio: float = 0.5
    calendar_name: str = "Course schedule"
    # Goes into the User-Agent. Empty is allowed; the project URL is still sent.
    contact: str = ""
    min_request_interval: float = MIN_REQUEST_INTERVAL
    http_timeout: float = HTTP_TIMEOUT
    # Where this Config was read from, for messages. Empty when built in code.
    path: str = ""

    @property
    def user_agent(self) -> str:
        return build_user_agent(self.contact)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")
_SOURCE_PREFIX = "source:"
_KNOWN_SECTIONS = ("calendar", "semester", "fetch", "health")


def _fail(path: str, section: str, key: str, problem: str) -> "ConfigError":
    where = "[%s]" % section
    if key:
        where += " %s" % key
    return ConfigError("%s: %s %s" % (path, where, problem))


def _get(parser, path, section, key, default):
    if not parser.has_section(section):
        return default
    if not parser.has_option(section, key):
        return default
    return parser.get(section, key).strip()


def _get_int(parser, path, section, key, default, low=None, high=None):
    raw = _get(parser, path, section, key, None)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _fail(path, section, key, "is %r, which is not a whole number" % raw)
    if low is not None and value < low:
        raise _fail(path, section, key, "is %d, below the minimum of %d" % (value, low))
    if high is not None and value > high:
        raise _fail(path, section, key, "is %d, above the maximum of %d" % (value, high))
    return value


def _get_float(parser, path, section, key, default, low=None, high=None):
    raw = _get(parser, path, section, key, None)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise _fail(path, section, key, "is %r, which is not a number" % raw)
    if low is not None and value < low:
        raise _fail(path, section, key, "is %g, below the minimum of %g" % (value, low))
    if high is not None and value > high:
        raise _fail(path, section, key, "is %g, above the maximum of %g" % (value, high))
    return value


def load_config(path: str) -> Config:
    """Read a config file, or explain exactly why it cannot be used.

    Every value is validated here, at the boundary, rather than being allowed
    to fail somewhere downstream where the message would name a symptom
    instead of the setting that caused it. A time zone this tool cannot write
    a VTIMEZONE for is rejected rather than silently downgraded to floating
    local time; a `fall_year` that is absent is rejected rather than defaulted,
    because a wrong year produces a calendar that looks perfectly fine.
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ConfigError(
            "no config file at %s. Copy config.example.ini, edit it, and pass "
            "it with --config." % path)

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parser.read_file(fh)
    except (configparser.Error, OSError) as exc:
        raise ConfigError("%s: cannot be parsed as an INI file: %s: %s"
                          % (path, type(exc).__name__, exc))

    for section in parser.sections():
        if section in _KNOWN_SECTIONS or section.startswith(_SOURCE_PREFIX):
            continue
        raise ConfigError(
            "%s: unknown section [%s]. Expected one of %s, or a page section "
            "named [source:something]." % (path, section,
                                           ", ".join(_KNOWN_SECTIONS)))

    # --- semester -------------------------------------------------------
    if not parser.has_section("semester") or \
            not parser.has_option("semester", "fall_year"):
        raise _fail(path, "semester", "fall_year",
                    "is missing. It decides the year of any date the page "
                    "prints without one (Aug-Dec -> that year, Jan-May -> the "
                    "next). There is no safe default: a wrong year produces a "
                    "calendar that looks entirely plausible.")
    fall_year = _get_int(parser, path, "semester", "fall_year", None,
                         low=1970, high=2999)

    # --- calendar -------------------------------------------------------
    calendar_name = _get(parser, path, "calendar", "name", "").strip()
    if not calendar_name:
        raise _fail(path, "calendar", "name",
                    "is missing. It becomes the calendar's display name.")

    tz = _get(parser, path, "calendar", "timezone", "").strip()
    if tz and tz not in VTIMEZONES:
        raise _fail(path, "calendar", "timezone",
                    "is %r, which this tool has no VTIMEZONE definition for. A "
                    "TZID the file never defines is worse than none at all. "
                    "Supported: %s. Leave it empty for floating local time, or "
                    "add the zone to courseics/timezones.py."
                    % (tz, ", ".join(SUPPORTED_TIMEZONES)))

    due_time = _get(parser, path, "calendar", "default_due_time", "23:59")
    m = _TIME_RE.match(due_time)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise _fail(path, "calendar", "default_due_time",
                    "is %r, which is not a 24-hour HH:MM clock time" % due_time)

    alarm_days = _get_int(parser, path, "calendar", "alarm_days_before", 1,
                          low=0, high=365)

    # --- fetch ----------------------------------------------------------
    contact = _get(parser, path, "fetch", "contact", "")
    interval = _get_float(parser, path, "fetch", "min_request_interval",
                          MIN_REQUEST_INTERVAL, low=0.0, high=3600.0)
    timeout = _get_float(parser, path, "fetch", "timeout", HTTP_TIMEOUT,
                         low=1.0, high=3600.0)

    # --- health ---------------------------------------------------------
    max_shrink = _get_float(parser, path, "health", "max_shrink_ratio", 0.5,
                            low=0.0, high=1.0)
    max_source_shrink = _get_float(parser, path, "health",
                                   "max_source_shrink_ratio", 0.5,
                                   low=0.0, high=1.0)

    # --- sources --------------------------------------------------------
    base_dir = os.path.dirname(path)
    sources = []  # type: List[Source]
    seen_urls = {}  # type: Dict[str, str]
    for section in parser.sections():
        if not section.startswith(_SOURCE_PREFIX):
            continue
        label = section[len(_SOURCE_PREFIX):].strip() or section

        url = _get(parser, path, section, "url", "")
        if not url:
            raise _fail(path, section, "url", "is missing")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise _fail(path, section, "url",
                        "is %r; only http:// and https:// pages are fetched" % url)
        if url in seen_urls:
            raise _fail(path, section, "url",
                        "is the same page as [source:%s]. Two sections for one "
                        "URL would fetch it twice and collide in state.json."
                        % seen_urls[url])
        seen_urls[url] = label

        kind = _get(parser, path, section, "kind", "")
        if not kind:
            raise _fail(path, section, "kind",
                        "is missing. It is what a date on this page means when "
                        "nothing on the line says otherwise. One of: %s."
                        % ", ".join(KINDS))
        if kind not in KINDS:
            raise _fail(path, section, "kind",
                        "is %r, which this tool does not know. One of: %s."
                        % (kind, ", ".join(KINDS)))

        fixture = _get(parser, path, section, "fixture", "") or None
        if fixture and not os.path.isabs(fixture):
            fixture = os.path.normpath(os.path.join(base_dir, fixture))

        sources.append(Source(url=url, default_kind=kind, name=label,
                              fixture=fixture))

    if not sources:
        raise ConfigError(
            "%s: no [source:...] section. There is nothing to fetch. Each page "
            "needs its own section, for example:\n"
            "    [source:lectures]\n"
            "    url = https://sites.google.com/example.edu/a-course/lectures\n"
            "    kind = lecture" % path)

    return Config(
        anchor=fall_anchor(fall_year),
        sources=tuple(sources),
        alarm_days_before=alarm_days,
        default_due_time=due_time,
        timezone=tz,
        max_shrink_ratio=max_shrink,
        max_source_shrink_ratio=max_source_shrink,
        calendar_name=calendar_name,
        contact=contact,
        min_request_interval=interval,
        http_timeout=timeout,
        path=path,
    )
