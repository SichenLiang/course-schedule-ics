# course-schedule-ics

Turns a public course web page into an `.ics` calendar, with reminders on the
deadlines and not on everything else.

You point it at the pages your course posts its schedule on. It reads the
dates, works out which are lectures and which are deadlines, and writes one
iCalendar file you can subscribe to or import. Then you can put it on a
schedule, and it will tell you when it stops working.

- **No dependencies.** Standard library only. The test suite passes on
  CPython 3.9.6, 3.12.13 and 3.14.7.
- **No install step.** Clone, write one config file, run.
- **Fails loudly, and accurately.** Every degraded outcome exits non-zero and
  explains itself on stderr. A broken run never leaves a half-written calendar.

Try it right now, with no network and no configuration, against the two
recorded pages in `tests/fixtures/`:

```sh
python3 -m courseics --config examples/recorded-course.ini \
                     --offline --out-dir out --list
```

The design record — every decision paired with the alternatives that were
rejected and why — is in [`DESIGN.md`](DESIGN.md).

---

## What it can and cannot read

This is the part worth reading before you invest any time.

**It works on** a public HTML page that prints dates as text, in the shape
`Mon. DD, YYYY` or `Mon DD` — `Sep. 02, 2026`, `Oct 5`, `September 13`. That
is the shape Google Sites course pages tend to use, which is what it was
written against and what the two recorded fixtures are. Both a lecture list
("date — topic") and an assignment table ("out date … Due date") work.

**It cannot read:**

- **Dates that are only in an attachment.** A syllabus PDF, a Word document, a
  linked spreadsheet — none of it is fetched or opened. Only the HTML of the
  page you name.
- **Dates in an image.** No OCR. A schedule posted as a screenshot is invisible
  to this tool, and — this matters — it will look like an empty page, which
  makes the run *fail* rather than silently produce nothing.
- **Anything behind a login.** No cookies, no authentication, ever. An LMS
  page, a course shell, anything gated: not reachable. If a page starts
  requiring a login, the run fails rather than publishing what the login wall
  says.
- **Content a browser builds with JavaScript.** The page is fetched with
  `urllib` and read as text; no script is executed. Google Sites happens to
  serve its text in the HTML, which is why it works there. A site that renders
  its schedule client-side will parse to nothing.
- **Date formats it does not know.** `2026-09-02`, `02/09/2026` and
  `2 September` are not matched. Requiring a spelled month name *before* the
  day is the single biggest reason it does not invent dates out of `Chapter
  17`, `Room 315` or `ECE-UY 2004` — so widening it is not free. If your pages
  use another format, `DATE_RE` in `courseics/parse.py` is where you change it,
  and `tests/test_parse_falsepositives.py` is where you find out what you broke.
- **A page whose layout is unlike the ones it was built against.** It reads the
  page as lines of text and assumes a date and the thing it refers to share a
  line. A layout that separates them will produce wrong titles.

**It does not write to your calendar.** It writes a file. Nothing here talks to
Google Calendar, iCloud or Exchange, and nothing needs an account or a token.
Publishing that file somewhere your calendar app can subscribe to is a separate,
optional step — see [Publishing](#publishing).

**One page, one meaning.** Each page you list declares what an un-cued date on
it means (a lecture, a release date, a deadline). A page mixing several kinds
with no wording to tell them apart will be labelled by that default.

---

## Quick start

### 1. Look at what it does, offline

```sh
git clone https://github.com/SichenLiang/course-schedule-ics
cd course-schedule-ics

python3 -m courseics --config examples/recorded-course.ini \
                     --offline --out-dir out --list
```

That replays two recorded pages from disk and writes `out/schedule.ics` and
`out/state.json`. No network is involved.

### 2. Point it at your own course

```sh
cp config.example.ini course-schedule.ini
$EDITOR course-schedule.ini          # the file explains every setting
python3 -m courseics --out-dir live --list --dry-run
```

`--dry-run` fetches and reports but writes nothing, which is how you check the
titles and dates before anything lands in a calendar. When it looks right:

```sh
python3 -m courseics --out-dir live          # write the files
python3 -m courseics --out-dir live --diff   # what changed since last time?
```

`course-schedule.ini` in the working directory is the default; anything else
needs `--config PATH`. Either entry point works:

```sh
python3 -m courseics --out-dir live   # with the repo on sys.path
python3 __main__.py --out-dir live    # from inside the repo, from anywhere
```

### 3. Subscribe

Open `live/schedule.ics` in your calendar app to import it once, or publish it
and subscribe to the URL so it keeps updating — see
[Publishing](#publishing).

---

## Configuration

Everything course-specific lives in one INI file.
[`config.example.ini`](config.example.ini) is the annotated template; this is
the shape of it:

```ini
[calendar]
name = EXAMPLE 1234 -- Introduction to Examples
timezone = America/New_York
default_due_time = 23:59
alarm_days_before = 1

[alarms]
assignment_due = on
assignment_out = off
lecture = off
no_class = off
unknown = on

[semester]
fall_year = 2026

[source:lectures]
url = https://sites.google.com/example.edu/example-course/lectures
kind = lecture

[source:assignments]
url = https://sites.google.com/example.edu/example-course/assignments
kind = assignment_out
```

Four settings decide more than they look like they do:

**`fall_year` is required and has no default.** Course pages routinely write a
deadline as "Due Sep. 13" with no year. Guessing "the next September 13" would
make the same page parse differently depending on the day you ran it, so
instead this number pins the academic year: August–December → `fall_year`,
January–May → `fall_year + 1`. For a spring 2027 course, set 2026. A year
printed on the page always wins over it; when the two disagree the event is
still published, flagged `[?]`. June and July belong to neither half — a date
there is dropped and reported, on the grounds that it is far likelier to be a
misparse than a class meeting.

**`timezone` decides whether a deadline is an instant or a wall clock.** Leave
it empty and 23:59 means "23:59 wherever you happen to be", which is wrong the
moment you travel. Name a zone and the file carries the matching `VTIMEZONE`,
so a September deadline in New York resolves to EDT and a December one to EST
— which is what a page writing "11:59PM EST" for both actually means. Only
zones the tool ships a definition for are accepted, and it lists them if you
name one it does not have. Adding one is a small edit to
`courseics/timezones.py`.

**`kind` says what a date on that page means** when nothing on the line says
otherwise: `lecture`, `assignment_out` (a release date), `assignment_due` (a
deadline), or `unknown`. A due cue next to a date — `Due`, `Deadline`,
`Submit by`, `Hand in by` — overrides the page default for that one date. This
is how one assignments row produces two events: a release date and a deadline
a fortnight later, labelled `ASSIGNED:` and `DUE:` so they are not confused for
each other.

**`[alarms]` decides what you are interrupted about**, per kind. The default is
deliberately not "everything". Measured on the recorded course: 42 events, and
before this section existed, 42 reminders — 30 of them telling a student to
attend lectures that were already on a fixed timetable, 6 announcing that an
assignment had been handed out, and 6 that were actually deadlines. A reminder
stream that is 86% noise stops being read, and what it costs when that happens
is the deadline. So reminders default to the kinds you can silently miss:

| kind | reminder | why |
|---|---|---|
| `assignment_due` | **on** | a deadline is the one thing that passes without announcing itself |
| `assignment_out` | off | the work cannot start before it exists; no advance warning is useful |
| `lecture` | off | the timetable is already known, and recurring |
| `no_class` | off | worth *seeing* on the calendar, not worth an alert |
| `unknown` | **on** | conservative: a date the parser could not classify is the last one to silence |

Every one of those is a line in `[alarms]`, and each takes `off`, `on`, or a
number of days: `lecture = 1` restores the previous behaviour for lectures,
`assignment_due = 3` moves deadline reminders three days out. `0` is a lead
("at the event itself"), not an off switch — use `off` for that.
`alarm_days_before` remains the default lead for every kind that has a reminder
and no number of its own.

### Options

| Flag | Effect |
|------|--------|
| `--config PATH` | The config file. Default `./course-schedule.ini`. |
| `--out-dir DIR` | Where `state.json` and `schedule.ics` are written. Default `.`. |
| `--offline` | Replay each source's recorded `fixture` instead of fetching. Nothing is downloaded. |
| `--list` | Print every parsed record to stdout: date, time, confidence, kind, title. |
| `--diff` | Print a change report against the stored `state.json` (new / date changed / details changed / disappeared). |
| `--dry-run` | Parse and report, write no files. Combines with `--list` and `--diff`. |
| `--fall-year N` | Override the config's semester anchor. Changing it forces a full re-fetch, because a `304` would otherwise skip the re-parse. |
| `--alarm-days N` | Override the default reminder lead time, in days. `0` means at the event itself. It changes *when* reminders fire, not *which* kinds get one — that is `[alarms]`. |

`--diff` and `--list` print to **stdout**; progress and failures go to
**stderr**, so `python3 -m courseics --out-dir live --diff > report.txt` gives
a clean report.

---

## Output

Both files land in `--out-dir`. Neither is committed — `live/` and `out/` are
gitignored as run artifacts.

### `schedule.ics`

RFC 5545, CRLF line endings, folded at 75 octets. One `VEVENT` per record; a
`VALARM` on the kinds `[alarms]` says to remind you about, and on no others. On
the recorded course that is 42 events carrying 6 reminders, one per deadline.

- Lectures, release dates and no-class days are all-day events. Deadlines are
  timed.
- `SUMMARY` says which kind of date it is:

  | Record | `SUMMARY` |
  |--------|-----------|
  | lecture | `Sampling Based Algorithms II` |
  | assignment released | `ASSIGNED: Assignment 3 - Trajectory Following` |
  | assignment deadline | `DUE: Assignment 3 - Trajectory Following` |
  | a day with no class | `NO CLASS: Labor Day - no class` |
  | anything uncertain | prefixed `[?]`, e.g. `[?] Guest lecture TBD` |

- `DESCRIPTION` carries the kind, the confidence, every reason the parser
  recorded (including where the year came from), the raw source line, and the
  source URL. When something looks wrong in your calendar, that field says why
  the parser thought otherwise.
- `CATEGORIES` is the raw kind, for programmatic filtering.

`UID`s are derived from the source, the kind, and **the half of (date, title)
that that kind holds still**. Import or subscribe to the same file repeatedly
and it stays one calendar.

| kind | identified by | so this is an in-place update | and this replaces the event |
|---|---|---|---|
| `assignment_due`, `assignment_out`, `unknown` | source + **title** + kind | the deadline moves | the item is renamed |
| `lecture`, `no_class` | source + **date** + kind | the session is retitled | the session moves to another day |

An assignment is an artefact — `Assignment 2` rescheduled is still
`Assignment 2`. A lecture is a slot — the 9 Sep session is the 9 Sep session,
and its title is the instructor's running summary of what it will cover. The
recorded course rewrote two lecture titles twice each in a single day; keyed on
the title, that deleted and rebuilt both events both times.

When either key does not identify exactly one row — two lectures on the same
day, or two assignments sharing a title — the other half joins the UID for
those rows, because it is the only thing that tells them apart. Those rows lose
the guarantee in the third column above. See [`DESIGN.md`](DESIGN.md) §7
and §14.

> **Upgrading from an earlier version:** two changes rotate `UID`s, both once
> and both on the first run after upgrading.
>
> 1. **The lecture key changed** (title → date), so every existing **lecture**
>    event is replaced once.
> 2. **Rows that say the class does not meet became their own kind**
>    (`lecture` → `no_class`), and the kind is part of the hash. On the
>    recorded course that is exactly two rows — `Labor Day - no class` and
>    `Fall break - No class`. In a subscribed calendar they are removed and
>    re-added, now reading `NO CLASS: …` instead of `[?] …`.
>
> Anything you attached to a replaced event is lost. Assignment events are
> unaffected by either change — they hash exactly what they hashed before.

### `state.json`

The previous run's records; per source an `ETag` / `Last-Modified` and a
`record_count`; `last_successful_run` / `last_attempted_run`; and a
`parse_signature` naming the options the stored records were parsed under. It
powers `--diff`, conditional GETs (a `304` skips parsing entirely), and the
health checks below. Delete it to start fresh; corrupt it — syntactically, or
structurally, or by giving a single field inside a single record the wrong type
— and the next run drops what it cannot read rather than crashing. A record
with a bad field costs you that record, not the run.

It is written together with `schedule.ics`: both files are staged and `fsync`ed
before either is renamed into place, so **any failure to write** — a full disk,
a quota, a read-only directory — leaves both files exactly as they were, and
says so. The rename itself is a separate, much narrower risk: if the first
rename succeeds and the second does not (a destination replaced by a directory,
say), the two files really are out of step, and the tool prints which file was
replaced, which was not, and how to get them back in step. It never claims that
nothing happened.

---

## Confidence

Every record is `certain` or `uncertain`; anything the parser could not stand
behind at all was dropped before becoming a record, and every drop is reported
on stderr rather than swallowed.

`uncertain` records **are still published**, prefixed `[?]`, with the reasons
in the description. Missing a real deadline is worse than showing a doubtful
one, and a flagged event is something you can check in five seconds; an event
that was never emitted is invisible. The recorded pages currently produce no
uncertain records at all: the only two they used to produce were the holidays,
which are now stated rather than flagged (below).

A record is downgraded when:

- the page's year disagrees with the semester anchor (the page still wins);
- a weekday name next to the date does not match that date;
- negation or correction words are nearby — `moved to`, `TBD`, `rescheduled`,
  `subject to change`, …;
- a relative expression is in the same context — `next week`, `tomorrow`;
- a second, un-cued date shares the line;
- a lowercase `may` is not corroborated by a year or a due cue, because it is
  a modal verb far more often than a month.

### "No class" is a statement, not a doubt

`cancelled` and `no class` used to sit in that list beside `TBD` and
`may change`, and they do not belong there. `TBD` is a page being unsure;
`no class` is a page being certain, about the one thing you most need to read
correctly. The recorded schedule published its two holidays as
`[?] Labor Day - no class` and `[?] Fall break - No class`, where `[?]` means
"this program could not work out what this row is" — on the two days of the
semester when being wrong means turning up to a locked room.

A row on a `lecture` page becomes a **`no_class`** record — `certain`, no
`[?]`, `NO CLASS:` in the summary, no reminder — when its own text says the
class does not meet:

- an explicit negation: `no class`, `no classes`, `no lecture`, `no lab`,
  `class does not meet`, `we will not meet`;
- a cancellation: `cancelled` or `canceled`;
- a named academic recess **as the row's entire title**: `Fall break`,
  `Spring recess`, `Thanksgiving break`, `Holiday`.

It **declines to fire** — leaving the row exactly where it was, a `[?]`
lecture with the reason recorded — when:

- the sentence is conditional or provisional: `if`, `unless`, `in case`,
  `might`, `possibly`, `tentative`, `subject to change`, `TBD`. An announcement
  reading *"there will be no class if it snows"* states a possibility, not a
  fact;
- the cancellation is denied: *"the class is **not** cancelled"*;
- the class **moved** rather than vanished (`moved to`, `rescheduled`,
  `postponed`), because where it landed is not something this parser can read
  off the page;
- the page is not one whose plain dates are class meetings. On an assignments
  page, `Assignment 4 cancelled` cancels an assignment.

The two lists are not symmetric, on purpose. A miss costs you the flagged
lecture you already had and can read in five seconds. A false positive prints
`NO CLASS` at confidence `certain` over a lecture that is happening, and a
student who believes it misses the class. So the recess vocabulary is a closed
list anchored to the whole title — `break` and `holiday` are ordinary English
words, and `Coffee break with the TAs` is not a recess — and anything hedged is
declined. Both directions are tested, including the mutation tests that assert
each rule is load-bearing: `TestNoClassIsADefiniteStatement`,
`TestNoClassDoesNotOverTrigger` and `TestTheNoClassRulesAreLoadBearing`.

What it does **not** do: it does not strip the page's own words out of the
title, so the summary reads `NO CLASS: Labor Day - no class` rather than
`NO CLASS: Labor Day`. And a bare `Labor Day` with no further wording stays an
ordinary lecture — a proper noun is not a statement that nothing happens.

---

## Exit codes

The worst outcome for a reminder tool is not a wrong reminder. It is one that
quietly stopped — a job that has been failing for three months and a calendar
frozen three months back. So every degraded outcome is non-zero, and the codes
say what to **do**, not merely what went wrong.

| Code | Meaning | What to do |
|------|---------|------------|
| `0` | Success. | — |
| `1` | Unexpected internal error. | Bug. Read the traceback. |
| `2` | Fetch failed — network error, timeout, dropped connection, or an HTTP status that is neither 200 nor 304. | Check connectivity, then whether the URLs still resolve. |
| `3` | Parsed **zero** records — overall, or from any one source, with or without a previous run to compare against. | That page's layout changed, or it is now gated. Open it by hand. |
| `4` | Record count collapsed by more than the configured threshold versus the last successful run — overall, or for any one source. | Same as 3, partial. Compare the page against `state.json`. |
| `5` | The output files could not be written or renamed into place (disk full, quota, read-only directory, a destination replaced by a directory). | Free space or fix permissions, then re-run. stderr says which files were replaced and which were not. |
| `6` | A page answered `304 Not Modified` to a request that carried no validator, so records parsed under the old options cannot be refreshed. | **Not a network problem** — the fetch worked. Delete `state.json` and run again. |
| `7` | The run was never configured: no config file, a config file that cannot be used, or a mistyped flag. | The message names the file, the section and the key. |

Two of those are less obvious than they look:

**The health checks are per source, not only on the total.** On the recorded
course the assignments page carries every deadline but only 28.6% of the
records — so it can empty out completely without moving the overall count far
enough to notice. That happened: the run "succeeded" at exit 0 and republished
a calendar with every deadline removed.

**A source producing zero records fails even on the very first run**, before
any baseline exists. A page is in your config because you expect dates on it,
so an empty one is a broken scrape until proven otherwise — and a first run, or
one straight after deleting `state.json`, is exactly when a scrape is most
likely to be misconfigured. If a source is legitimately empty, delete its
section.

On codes 2, 3, 4, 6 and 7 the existing `schedule.ics` is **left untouched** — a
stale calendar beats an empty or a half-written one — and stderr names the
failure and the last successful run:

```
course-schedule-ics FAILED: parsed 0 records from 2 source(s). ...
last successful run: 2026-09-02T18:47:12+00:00
```

Code 5 is the one that can leave the two files disagreeing, and only if a
rename fails after an earlier one succeeded. That case is reported line by
line rather than summarised, because a wrong reassurance is worse than none:

```
course-schedule-ics FAILED: the output files were staged but could not all be renamed ...
THE TWO FILES ARE NOW OUT OF STEP. What is on disk:
    REPLACED     .../schedule.ics  (this run's content)
    NOT REPLACED .../state.json  (still the previous run's content)
```

That is what makes it safe to schedule. Under cron a non-zero exit mails you;
under `launchd` or systemd it marks the job failed.

```sh
# every day at 07:00
0 7 * * * cd /path/to/course-schedule-ics && /usr/bin/python3 __main__.py --out-dir live
```

---

## Publishing

Everything above produces a local file. `./deploy.sh` is the optional second
half: it regenerates the calendar and, **only if every gate passes**, commits
and pushes it to a separate public repository — GitHub Pages or any other host
that serves a file over HTTPS — so a calendar app can subscribe to a URL and
stay up to date on its own.

```sh
cp publish.example.conf publish.conf
$EDITOR publish.conf        # PUB_REPO is the one setting with no default
PARSER_ARGS=--offline ./deploy.sh   # rehearse the whole thing with no network
./deploy.sh
```

It is the only path from your machine to the public web, so it is deliberately
more conservative than the parser:

| Gate | Refuses to publish when | Waivable |
|------|-------------------------|----------|
| Parser | The parser exits non-zero for any reason. Nothing is copied; the live calendar is left exactly as it was. | no |
| Structure | `VEVENT` / `VALARM` / `UID` counts disagree, `BEGIN`/`END` are unbalanced, `UID`s repeat, line endings are not pure CRLF, or folding exceeds 75 octets. | no |
| Privacy | The `.ics` mentions a host that is not one of your configured sources, or contains something shaped like an email address, a credential, a home-directory path, a private IP, or any of your own `FORBIDDEN_PATTERNS`. | no |
| Scale | Fewer than `MIN_EVENTS` events, or a shrink of more than `SHRINK_PCT` against what is already published. | `FORCE=1` |
| Whitelist | Anything outside `ALLOWED_FILES` is about to be committed to the public repo. | no |

The privacy gate's host allow-list is **derived** from the source URLs in your
parser config rather than written down separately, so it cannot go stale when
you change courses. `FORBIDDEN_PATTERNS` is where anything institution-specific
belongs — the name of your LMS, the shape of a student ID, a classmate's name.

Change is detected on a fingerprint that **ignores `DTSTAMP`**, which moves on
every run: an unchanged schedule produces no commit rather than a daily stream
of empty ones. If `PUBLIC_URL` is set, the script then polls it until the file
served there hashes identically to what it just pushed, and checks the
`Content-Type` — so "it published" and "it is actually published" are the same
claim. Leave `PUBLIC_URL` empty and it says plainly that this went unverified.

`deploy.sh`'s own exit codes: `1` environment or settings, `2` the parser
failed, `3` a gate refused, `4` push failed, `5` pushed but unverifiable.

---

## Scheduling (macOS `launchd`)

`launchd/` runs `deploy.sh` on a schedule and — this is the part that matters —
makes sure you find out when it stops.

```sh
launchd/install.sh          # generate the plist, install it, load it
launchd/status.sh           # is it still alive?
launchd/uninstall.sh        # stop it
```

Install is idempotent, reads its label, hours and `PATH` from `publish.conf`,
generates the plist from `launchd/agent.plist.template` with your paths in it,
and touches exactly one label in `~/Library/LaunchAgents/`. `RunAtLoad` is off:
loading a job should not have the side effect of pushing to the internet from a
login moment when the network may not be up. To run it now:

```sh
launchctl kickstart -k gui/$(id -u)/local.course-schedule-ics.publish
```

Three independent backstops, because one is not enough:

1. **A notification on every non-zero exit**, carrying the code, what it means,
   and `deploy.sh`'s last few lines. Best-effort and never fatal: it runs in
   the background under a 10-second watchdog, so a permission dialog cannot
   wedge the job, and any failure to notify is itself logged. **A real caveat:**
   macOS attributes `osascript` notifications to *Script Editor*. If Script
   Editor's notifications are off in System Settings, these banners disappear
   silently — which is why the other two exist.
2. **A gap check.** If more than `GAP_HOURS` have passed since the previous run
   — machine off, job unloaded, plist broken — the next run says so and
   notifies. That is the "it stopped and nobody noticed" case, caught the
   moment it resumes.
3. **`launchd/status.sh`**, which answers it on demand. Its first line is a
   verdict, not data, and so is its exit code: `0` healthy, `1` a human should
   look. It calls the job unhealthy when it is not loaded, when nothing has
   succeeded in `STALE_HOURS`, or when there has never been a successful run.

No network is recorded as `SKIP`, not a failure. A job that cries wolf at 01:00
over one Wi-Fi hiccup gets ignored, and an ignored alarm is the same as no
alarm; a genuine outage still surfaces through the gap check and through "last
success".

Every run leaves one greppable verdict line:

```
[course-schedule-ics] 2026-09-05T13:00:04-0400 RESULT rc=0 OK   published, or the schedule had not changed [2s]
[course-schedule-ics] 2026-09-05T19:00:11-0400 RESULT rc=3 FAIL a pre-publication gate refused [9s]
```

macOS has no `logrotate`, so `run.sh` does it: over `LOG_MAX` bytes,
`logs/publish.log` becomes `publish.log.1` and the previous `.1` is discarded.
Never more than two files. Rotation happens *before* the run and the wrapper
appends by path rather than through an inherited descriptor, so nothing from
the new run lands in the file that was just rotated away.

Stopping breaks nothing: `deploy.sh` still runs by hand and the published
calendar stays at its last good version.

---

## Tests

```sh
python3 -m unittest discover -s tests -t .          # 407 tests, well under a second
python3 -m unittest discover -s tests -t . -v       # verbose
python3 -m unittest tests.test_parse_confidence     # one module
```

The suite is hermetic **by enforcement, not by convention**: `tests/__init__.py`
replaces `socket.socket`, `socket.create_connection` and `socket.getaddrinfo`
before anything else imports, and two tests assert that the block works. Any
regression that reaches for the network fails immediately.

`tests/fixtures/*.html` are the two real recorded pages, committed. They are
what makes the suite offline, so unlike `live/` and `out/` they belong in the
repo. They are kept as recorded, with one edit: the instructor's name and email
address in the page footer are replaced with a placeholder. That line carries
no date and produces no record, so nothing the tests assert on depends on it.

Nothing in this repository is verified beyond what those tests cover.
`deploy.sh` and `launchd/` are shell and are **not** covered by the suite; they
have been exercised by hand against a local repository, which is not the same
thing.

---

## Layout

```
__main__.py                 run from inside the repo
courseics/
  config.py                 the config file: reading it, and refusing it
  timezones.py              the VTIMEZONE blocks it is willing to emit
  fetch.py                  HTTP (conditional GET, rate limit) + injectable fakes
  extract.py                HTML -> text lines; the block/inline whitespace rule
  parse.py                  lines -> records; dates, kinds, confidence, UIDs
  ics.py                    RFC 5545 output, escaping and folding
  state.py                  state.json I/O and the diff report
  cli.py                    argument handling and the exit codes
config.example.ini          annotated template -- copy to course-schedule.ini
examples/
  recorded-course.ini       the course the fixtures were recorded from
publish.example.conf        annotated template -- copy to publish.conf
publish-config.sh           settings loader shared by deploy.sh and launchd/
deploy.sh                   the only path from here to the public web
launchd/                    run deploy.sh on a schedule, and notice when it stops
  agent.plist.template      placeholders filled in at install time
  install.sh                generate the plist, install it, load it (idempotent)
  uninstall.sh              unload it and delete the plist
  run.sh                    what launchd runs: rotate, run, log, alert
  status.sh                 last success / recent failures / how many, in one line
tests/                      407 tests + the two recorded pages
DESIGN.md                   why every one of these is the way it is
```

---

## Known limitations

[`DESIGN.md` §14](DESIGN.md) lists them in full, with the reasoning for leaving
each one. The ones most likely to bite:

- **Renaming an assignment is a delete plus an add**, because an assignment is
  identified by its title. So is **moving a lecture to another day**, because a
  lecture is identified by its date. Anything you attached to the old event is
  lost. Retitling a lecture and rescheduling an assignment are both in-place
  updates.
- **An item appearing on two of your pages produces two events.** Deduplication
  is per source.
- **Two runs writing the same `--out-dir` at once will collide.** There is no
  lock. It is designed as a once-or-twice-a-day job.
- **The `VTIMEZONE` rules are the ones currently in force**, not a history of
  the zone. A legislature that changes daylight-saving rules invalidates them.
- **Control characters from a page reach the terminal**, though not the ICS
  structure — `URL:` is percent-encoded and text properties are escaped.
- **A cancellation the page words unusually is missed**, and stays a `[?]`
  lecture. The `no_class` rule reads explicit statements and a closed list of
  recess names, and declines anything hedged; `Labor Day` alone, or a row that
  only strikes the topic through, is not recognised. This is the direction the
  rule is deliberately wrong in — see
  ["No class" is a statement](#no-class-is-a-statement-not-a-doubt).
- **A row whose class *moved* is not resolved**, only flagged. `moved to`,
  `rescheduled` and `postponed` leave the row a `[?]` lecture on its original
  date; the tool does not try to work out where it landed.

---

## License

[MIT](LICENSE). It is a small, self-contained utility that people should be
able to copy a function out of, vendor into something else, or fork for their
own university without reading a compliance checklist first — which is what MIT
is for. Apache-2.0's patent grant buys nothing here: there is nothing patentable
in "match a month name with a regular expression".
