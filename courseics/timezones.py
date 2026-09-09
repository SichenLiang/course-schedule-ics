"""The `VTIMEZONE` blocks this tool is willing to emit.

A `TZID` names a zone; the file has to define it, or the reference dangles and
a client is free to interpret the time however it likes. So the set of zones
this tool supports is exactly the set it can write a definition for -- and that
set is deliberately short, because every entry here is a correctness claim
about somebody's clock.

The definitions are rule-based (`RRULE`) rather than a table of transitions, so
they stay correct for every year rather than for the years somebody remembered
to tabulate. They describe the rules currently in force; they are not a history
of the zone, and a legislature that changes the rules invalidates them (see
DESIGN.md §9).

Adding a zone is a small, self-contained edit: copy the closest block, change
the `TZID`, the offsets, the `TZNAME`s and the two `RRULE`s. An unlisted zone
is not a crash -- `ics.timezone_id()` degrades it to floating local time -- but
the config loader refuses it up front, because a silent downgrade to floating
time is exactly the failure this table exists to prevent.
"""

# --- United States: second Sunday in March to first Sunday in November ---
# The rule has been in force since 2007 (Energy Policy Act of 2005).

def _us(tzid, std_name, dst_name, std_offset, dst_offset):
    return (
        "BEGIN:VTIMEZONE",
        "TZID:%s" % tzid,
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:%s" % std_offset,
        "TZOFFSETTO:%s" % dst_offset,
        "TZNAME:%s" % dst_name,
        "DTSTART:20070311T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:%s" % dst_offset,
        "TZOFFSETTO:%s" % std_offset,
        "TZNAME:%s" % std_name,
        "DTSTART:20071104T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    )


# --- European Union: last Sunday in March to last Sunday in October ---
# Both transitions happen at 01:00 UTC, which is why the local DTSTART hours
# differ between zones: 01:00 GMT in London, 02:00 CET in Paris and Berlin.

def _eu(tzid, std_name, dst_name, std_offset, dst_offset,
        spring_local, autumn_local):
    return (
        "BEGIN:VTIMEZONE",
        "TZID:%s" % tzid,
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:%s" % std_offset,
        "TZOFFSETTO:%s" % dst_offset,
        "TZNAME:%s" % dst_name,
        "DTSTART:19810329T%s" % spring_local,
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:%s" % dst_offset,
        "TZOFFSETTO:%s" % std_offset,
        "TZNAME:%s" % std_name,
        "DTSTART:19961027T%s" % autumn_local,
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    )


# --- Zones with no daylight saving at all ---
# One STANDARD component, no RRULE. DTSTART is the RFC 5545 floor rather than a
# real transition: nothing ever changes, so there is nothing to recur.

def _fixed(tzid, name, offset):
    return (
        "BEGIN:VTIMEZONE",
        "TZID:%s" % tzid,
        "BEGIN:STANDARD",
        "TZOFFSETFROM:%s" % offset,
        "TZOFFSETTO:%s" % offset,
        "TZNAME:%s" % name,
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    )


VTIMEZONES = {
    "America/New_York":    _us("America/New_York", "EST", "EDT", "-0500", "-0400"),
    "America/Chicago":     _us("America/Chicago", "CST", "CDT", "-0600", "-0500"),
    "America/Denver":      _us("America/Denver", "MST", "MDT", "-0700", "-0600"),
    "America/Los_Angeles": _us("America/Los_Angeles", "PST", "PDT", "-0800", "-0700"),

    "Europe/London": _eu("Europe/London", "GMT", "BST", "+0000", "+0100",
                         "010000", "020000"),
    "Europe/Dublin": _eu("Europe/Dublin", "GMT", "IST", "+0000", "+0100",
                         "010000", "020000"),
    "Europe/Paris":  _eu("Europe/Paris", "CET", "CEST", "+0100", "+0200",
                         "020000", "030000"),
    "Europe/Berlin": _eu("Europe/Berlin", "CET", "CEST", "+0100", "+0200",
                         "020000", "030000"),
    "Europe/Madrid": _eu("Europe/Madrid", "CET", "CEST", "+0100", "+0200",
                         "020000", "030000"),
    "Europe/Rome":   _eu("Europe/Rome", "CET", "CEST", "+0100", "+0200",
                         "020000", "030000"),

    "UTC":              _fixed("UTC", "UTC", "+0000"),
    "Asia/Shanghai":    _fixed("Asia/Shanghai", "CST", "+0800"),
    "Asia/Hong_Kong":   _fixed("Asia/Hong_Kong", "HKT", "+0800"),
    "Asia/Singapore":   _fixed("Asia/Singapore", "+08", "+0800"),
    "Asia/Taipei":      _fixed("Asia/Taipei", "CST", "+0800"),
    "Asia/Tokyo":       _fixed("Asia/Tokyo", "JST", "+0900"),
    "Asia/Seoul":       _fixed("Asia/Seoul", "KST", "+0900"),
    "Asia/Kolkata":     _fixed("Asia/Kolkata", "IST", "+0530"),
    "Asia/Dubai":       _fixed("Asia/Dubai", "+04", "+0400"),
}

SUPPORTED_TIMEZONES = tuple(sorted(VTIMEZONES))
