# course-schedule-ics — design record

Every decision below is paired with the alternatives that were rejected and
why. Where a rule exists because of something observed on the real pages, the
observation is quoted.

Scope: scrape one or more public course pages named in a config file, emit one
`.ics` file, and make any failure loud. Nothing writes to a calendar server;
the `.ics` is the deliverable.

The observations quoted throughout come from the two pages recorded in
`tests/fixtures/` — a Google Sites course site with a lecture schedule and an
assignments table. That course is the worked example, not the target: nothing
about it is compiled in. See §15.

---

## 1. Zero dependencies, standard library only

**Decision.** No `pip install`, ever. HTML parsing, iCalendar generation, HTTP
and date handling are all hand-rolled on the standard library.

**Why.** The same checkout has to run unchanged on two interpreters that are
five years apart:

| | Python |
|---|---|
| the old end | 3.9.6 — a macOS system interpreter, not upgradable without disturbing the machine |
| the new end | 3.14 — a current Linux box |

A dependency has to satisfy both. That means either a wheel built for 3.9 *and*
3.14 on two different platforms, or a source build with a compiler. Either way
the script acquires a way to break that has nothing to do with the course
schedule — and it breaks on the day a deadline moves, which is the one day it
must not.

The language floor is therefore 3.9: no `match`, no `X | Y` type unions, no
`dict | dict`, no `datetime.UTC`. Nothing newer than 3.9 appears in the source.
The upper bound matters too — `datetime.utcnow()` is deprecated in 3.12+ and is
avoided in favour of `datetime.now(timezone.utc)`.

**Rejected: BeautifulSoup / lxml.** lxml is a C extension; its wheel
availability lags new interpreters by months. BeautifulSoup would pull in a
parser anyway. And the parsing problem here turned out to be a *whitespace*
problem (§3), which a DOM library does not solve for free.

**Rejected: the `icalendar` package.** RFC 5545 output for this feature set is
about 60 lines: escape five characters, fold at 75 octets, emit `VEVENT` and
`VALARM`. Writing it costs less than owning the dependency, and the folding
rule is exactly the sort of thing worth having a test for regardless.

**Rejected: `requests`.** `urllib.request` already does conditional GET,
custom headers and timeouts. `requests` would add nothing this script uses.

**Rejected: `python-dateutil`.** Its fuzzy parser is precisely the wrong tool.
`dateutil.parser.parse("Chapter 17", fuzzy=True)` returns a date. The whole
value of the parser here is *refusing* things (§5), and a permissive parser
would have to be fought rather than used.

**Rejected: `html.parser.HTMLParser` from the stdlib.** Considered, since it
is free of the dependency objection. Rejected because Google Sites emits
unbalanced and duplicated markup that sends a strict-ish event parser off the
rails, while the regex pipeline in `extract.py` only has to answer one
question: does this tag imply a line break? See §3.

---

## 2. The year bug — where a year actually comes from

**The handoff spec was wrong.** It stated that the pages carry no years, and
so a `SemesterAnchor` was built to infer one from a month (Aug–Dec →
`fall_year`, Jan–May → `fall_year + 1`).

**Observed reality.** Every left-hand date on both pages is written with its
year:

```
Sep. 02, 2026 Introduction, History, and Architectures
Oct. 05, 2026 Sampling Based Algorithms II
```

Only the trailing deadlines are yearless — `... Assignment 1 - Robot Building
Due Sep. 13`. So 36 of the 42 records have a year printed on the page and 6 do
not.

The consequence of believing the spec was not a wrong output today (the anchor
happens to agree with the page for this term) but a **silent** wrong output
later: an anchor configured for the wrong term would have quietly overridden
dates the page states outright, and nothing would have said so.

**Decision — the page is the primary source, the anchor is a fallback:**

1. A year written on the page is used, always.
2. Only a date with no year consults the `SemesterAnchor`.
3. If the two disagree, **the page still wins**, but the record is downgraded
   to `uncertain` with a reason naming both years. Exactly one of the two is
   wrong and the text cannot say which, so a human decides.
4. No year on the page and a month the anchor does not cover (June, July) →
   the hit is dropped. We never guess.

Both branches now state their provenance in `reasons`, so `state.json` and the
event description always answer "where did this year come from?":

```
year 2026 taken from the page
year absent from the page; filled 2026 from semester anchor 'fall-2026'
```

**Rejected: delete the anchor now that the page has years.** The six deadlines
have no year at all. Something must supply one.

**Rejected: let the anchor win a conflict.** The anchor is a hand-configured
constant; the page is the source of truth being scraped. A local constant that
can silently override the source it is scraping is the exact failure mode this
section exists to fix.

**Rejected: drop conflicting records entirely.** A missing deadline is worse
than a flagged one. The tool's whole bias is to surface doubt rather than
delete it (§4).

**Rejected: inherit the year from the sibling date on the same line.**
Tempting — `Sep. 09, 2026 ... Due Sep. 13` obviously means 2026. It breaks at
the year boundary: a December assignment due in January would inherit the wrong
year, and that is precisely the case a student cannot afford to get wrong. The
anchor's month→year table handles the roll-over correctly by construction.

**Rejected: "the next Sep 13 after today".** Not reproducible. The same input
would produce different output depending on when the script ran, which makes
the diff report meaningless and the tests date-dependent.

### 2b. The regex had to tolerate tag-injected whitespace

The same bug has a lexical half. Google Sites splits one date across several
`<span>`s, and whatever whitespace sat between the tags survives the tag
removal. The identical logical date arrives as any of:

```
Sep. 02, 2026    Sep . 02 , 2026    Oct .05,2026    Oct .   05 , 2026
```

The old separator `(?:\.\s*|\s+)` required the period to touch the month, so
`Sep . 02, 2026` matched **nothing**. Now every separator is `\s*`-tolerant on
both sides: `(?:\s*\.\s*|\s+)`.

This costs no precision. Whitespace *before* a period does not occur in
ordinary prose, so the looser pattern accepts no English sentence that the
stricter one rejected. The false-positive guards of §5 are re-tested against
the spaced-out forms to prove it.

---

## 3. HTML → text: the one rule that mattered

When deleting a tag, insert a newline for a **block** tag and **nothing at all**
for an inline tag.

A real row on the lectures page is:

```html
<span>Oct</span><span>. 0</span><span>5</span><span>, 2026</span>
```

The obvious `re.sub('<[^>]+>', ' ', html)` yields `Oct . 0 5 , 2026` — the day
is split into two numbers and no date regex can recover it. That single
whitespace decision was the difference between finding **6 of 30** lecture
dates and finding **30 of 30**.

**Rejected: strip tags to spaces and normalise afterwards.** Collapsing
`0 5` back to `05` is indistinguishable from collapsing two genuinely separate
numbers. The information is destroyed at strip time; it cannot be restored.

---

## 4. Three tiers of confidence

| Tier | What happens |
|------|--------------|
| **dropped** | Never becomes a record at all. No date, an impossible day, a false-positive trap, or a year that cannot be resolved. |
| **uncertain** | Emitted as a normal `VEVENT`, `SUMMARY` prefixed `[?]`, with every reason listed in the `DESCRIPTION`. |
| **certain** | Emitted plain. |

The middle tier is the point of the design: anything doubtful is published and
labelled rather than dropped.

Its original example is no longer one. `Labor Day - no class` and
`Fall break - No class` were the two `uncertain` records on the recorded pages,
and they were the wrong tier — see §4b. The current pages produce no
`uncertain` records at all, which is a fact about these two pages and not a
claim that the tier is unused; every trigger below still has its own test.

Downgrade triggers, each with a test:

- year conflict between page and anchor (§2)
- a weekday name next to a date that does not match that date (§6)
- negation or correction words: `not`, `instead of`, `moved to`,
  `rescheduled`, `cancelled`/`canceled`, `postponed`, `no class`, `TBD`/`TBA`,
  `subject to change` — **except** where the word is what identified the row
  as a no-class day in the first place (§4b)
- a relative expression in the same context: `next week`, `end of the semester`
- a second, uncued date on a line that already has one
- a lowercase `may` with nothing corroborating it (see §5)

**Rejected: two tiers (keep or drop).** Silently dropping a real deadline is
the worst outcome this tool can produce. Anything doubtful is emitted and
labelled instead.

**Rejected: a numeric score (0.0–1.0).** A number is not actionable. `0.62`
does not tell a reader what to check. A list of sentences does, and it is what
lands in the event description.

**Rejected: dumping uncertain records to a side file for review.** A file
nobody opens is the same as dropping them. Putting `[?]` in the calendar the
user already looks at is the only placement that gets read.

**A drop is reported; a trap is not.** The distinction is whether a human
meant to publish a date there. `Feb. 30` and `Sep. 32` are both typos over a
real deadline, so both are named on the run log, with the fragment and the
line it came from. `Chapter 17` and `Room 315` were never dates and never will
be; announcing them would put dozens of lines of noise on every run, which is
how a report stops being read.

The two typos used to be treated differently, for a reason that had nothing to
do with either of them: an impossible **day** was filtered inside `_scan_dates`
before the hit list was built, and `parse_line` returns early on an empty hit
list — discarding the rejections along with it. So `Feb. 30`, caught one step
later by `date()`, was reported, while `Sep. 32`, `Sep. 0` and `Sep. 99`
vanished without a word. Impossible days are now carried out of the scan
separately and go through the same reporting path.

---

## 4b. A cancellation is a statement, not a doubt

**The report.** A subscriber sent a screenshot of the live calendar. Two rows
read:

```
[?] Labor Day - no class
[?] Fall break - No class
```

`[?]` means "this program could not work out what this row means". It could:
the row says so, in words. Both were `certain` statements published as doubtful
ones, on the two days of the semester when being wrong means turning up to a
locked room.

**The cause.** `NEGATION_RE` had one list for two different things. Most of its
words mark a page being *unsure* — `TBD`, `may change`, `subject to change`,
`moved to`. Two of them — `no class`, `cancelled` — mark a page being *sure*,
and what it is sure of is that nothing happens. Sharing one rule meant the
phrase that identified the row also downgraded it.

**Decision.** A fifth kind, `no_class`, and three rules that decide when a row
becomes one.

| | rule | scope |
|---|---|---|
| 1 | `NO_CLASS_RE` — an explicit statement: `no class`/`no lecture`/`no lab`, `class does not meet`, `cancelled` | the title, i.e. the text *this* date owns |
| 2 | `RECESS_TITLE_RE` — a closed list of recess names, matched against the **whole** title | the title |
| 3 | `NO_CLASS_HEDGE_RE` — anything conditional, provisional or inverted vetoes both of the above | the whole line |

Plus a restriction: only a page whose plain dates *are* class meetings
(`NO_CLASS_SOURCE_KINDS`) can produce one. On the assignments page,
`Assignment 4 cancelled` cancels an assignment.

A `no_class` record is `certain`, carries no `[?]`, is prefixed `NO CLASS:`,
and gets no reminder (§8b) — it is a thing to *see*, not to be alerted about.

**The asymmetry is the whole design.** A miss costs the reader the `[?]`
lecture they already have and can check in five seconds. A false positive
prints `NO CLASS` at `certain` over a lecture that is happening, and a student
who believes it misses the class. Those two are not comparable, so where the
wording is at all open the rule declines to fire. Three consequences:

- **The recess list is closed and anchored to the whole title.** `break` and
  `holiday` are ordinary English words. `Fall break` as a row's entire title is
  a recess; `Coffee break with the TAs` is not, and an unanchored rule cannot
  tell them apart. This is the same reasoning as `MONTH_TOKENS` in §5: a
  vocabulary whose false positives are expensive is enumerated, not
  approximated.
- **The hedge veto is checked against the whole line, while the cue is checked
  against one date's own title.** Deliberately asymmetric: a cue must belong to
  *this* date before it may promote it, whereas a hedge anywhere in the
  sentence is reason enough to hold back — and holding back is the cheap
  mistake. The shape that motivated it is the announcement
  *"there will be no class if it snows"*, which reads word for word like a
  cancellation and is a statement about the weather.
- **A class that *moved* is not resolved, only flagged.** `moved to`,
  `rescheduled`, `postponed` all veto. The class does still happen; where it
  landed is not something this parser can read off the page, and publishing
  either "it happens here" or "nothing happens" would be a guess.

**The negation excuse is scoped to the cue, not to a word list.** The words
that *produced* the record may not also cast doubt on it, but every other
negation word on the line still may — `Labor Day - no class, notes not posted`
is a no-class day *and* worth a `[?]`. Excusing a fixed list of words would
have missed `class does not meet`, whose `not` is likewise the cue. So the
excuse is computed by running `NEGATION_RE` over the matched cue itself
(`_surviving_negations`), which covers every phrasing the cue vocabulary ever
learns.

**Rejected: stripping the page's words out of the title.** `NO CLASS: Labor
Day - no class` reads redundantly, and `NO CLASS: Labor Day` would read better.
But the title is the page's own text everywhere else in this program, stripping
it can leave a row with no title at all, and the redundancy is not what harms
anybody.

**Rejected: promoting `unknown` pages too.** A page with no established
convention is not a page whose dates are known to be class meetings, and
"cancelled" there could be about anything.

**Rejected: keeping it inside `lecture` with a `[?]` and a better reason
string.** The reason string is in the `DESCRIPTION`, which nobody opens; the
`[?]` is in the `SUMMARY`, which is the whole of what a phone shows. The fix
had to be visible where the mistake was.

**Cost, accepted and stated.** `kind` is part of the UID hash, so the two rows
that change from `lecture` to `no_class` rotate their UIDs once. Subscribers
see one delete and one add per affected row. This is a one-off, and it is
listed in the README's upgrade note next to the lecture-key rotation so a user
reads about both at once.

---

## 5. False-positive exclusions, and where each came from

Layer one: `DATE_RE` requires a month **name**, spelled correctly and ending
there. Bare numerals cannot reach the parser at all, which disposes of most
numeric noise for free.

The "ending there" half was added after an audit. The pattern used to close the
month with an open-ended `[a-z]*` tail, meant to accept `Sep|tember`. It also
accepted `Aug|mented`, `Nov|el`, `Oct|omap`, `Dec|imal` and `Mar|ching` — and
because the tail sat outside the capture group, it ate the front of the title
too:

| Line on the page | What the parser produced |
|------------------|--------------------------|
| `Augmented 3D perception lab` | `2026-08-03` — `D perception lab` — `certain` |
| `Novel 3D printing` | `2026-11-03` — `D printing` — `certain` |
| `Octomap 2 tutorial` | `2026-10-02` — `tutorial` — `certain` |

On a robotics course whose real schedule already contains a row called `Novel
Robot MP`, "Novel 3D Printing for Robots" would have added a thirteenth lecture
that does not exist — at `certain`, the tier the reader is told to trust. A
month name is a closed vocabulary of 24 strings; the alternation is generated
from `MONTH_TOKENS` so the set the regex accepts and the set the lookup
understands cannot drift apart.

Layer two: guards that reject a match a looser pattern would accept. Each of
these is a string actually observed in course-page prose:

| Observed | Rule |
|----------|------|
| `Chapter 17`, `Case 17.2` | `TRAP_PREFIX_RE` — a match preceded by `chapter`, `case`, `question`, `problem`, `section`, `figure`, `table`, `room`, `page`, `part`, `slide`, `unit`, `module`, `version`, `no.`, `#` … is rejected |
| `Room 315`, `Room 804` | same rule (`room`, `rm`, `building`, `bldg`) |
| `ECE-UY 2004`, `ROB UY 3303` | `COURSE_CODE_RE` — a match overlapping a `XXX-UY nnnn` code is rejected |
| `6 Metrotech Center` | no month name → never matches |
| `Copyright 2026 NYU` | no month name → never matches |
| `Sep 2024` | `(?![\d:])` after the day — `2024` cannot be read as day 20 or day 2 |
| `11:59` in prose | same negative lookahead; a clock is not a date |
| `may 5 students attend` | lowercase `may` is a modal verb far more often than a month, so it is only `certain` when corroborated by an explicit year or a due cue |
| `Augmented 3D`, `Novel 3D`, `Decimal 4` | `(?![A-Za-z])` after the month — a month name may not be the prefix of a longer word |

A date-shaped fragment that is genuinely rejected is not the same as one that
could not be resolved. Traps above are *not* dates and are silently excluded, as
intended. A fragment that **is** a date and still produces no record —
`Feb. 30, 2026 Assignment 9`, or a month no semester anchor covers — used to
vanish just as quietly, which meant a real deadline could be missing from the
calendar with exit 0 and not one word anywhere. Those are now collected and
named on stderr, with the fragment, the line it came from, and the reason.

Layer two is tested directly, not just through the end-to-end path, so it stays
honest if layer one is ever loosened — which §2b just did.

**Rejected: a stopword list of whole lines.** Brittle and endless. The traps
are *positional* (a number after `Chapter`), not lexical.

**Rejected: dropping anything that looks ambiguous.** Same reasoning as §4 —
that is what the `uncertain` tier is for.

---

## 6. Weekday × date consistency check

If a weekday name sits within 24 characters before a date, it is checked
against the actual weekday of the resolved date. A mismatch downgrades the
record and says so:

```
weekday conflict: text says Monday but 2026-09-02 is a Wednesday
```

**What it is for.** It is the only *independent* check available. A course page
is edited by hand every term, and the commonest edit error is bumping a date
without touching the weekday beside it. The page contains two statements of the
same fact, and disagreement between them is evidence — evidence that is
otherwise invisible to a parser that only reads the numeral.

It is deliberately advisory. A mismatch never drops the record, because the
weekday is as likely to be the stale half as the date is.

---

## 7. Event identity: each kind keyed on the half that holds still

```
lecture, no_class uid = sha256("<url>\0<date>\0<kind>")[:32]  + "@course-schedule-ics"
everything else   uid = sha256("<url>\0<title>\0<kind>")[:32] + "@course-schedule-ics"
ambiguous         ... with the other half appended, then a "\0#N" tiebreaker
```

One rule, applied with the two halves swapped:

> Hash the source, the kind, and **the half of `(date, title)` that this kind
> holds still**. If that does not identify exactly one row, append the other
> half. If even that repeats, append `#N`.

A UID is a calendar's identity for an event. Under a stable UID an edit is an
*update*: the subscriber's own notes, snoozes and RSVPs survive, and `--diff`
says what changed instead of "1 removed, 1 added". Under a rotated UID the
calendar deletes the event and creates a different one, and everything attached
to it is gone. So the only question worth asking about identity is: **which
field does this kind of row keep still while the other one moves?**

The two kinds of row on a course page answer it in opposite directions.

**An assignment is an artefact; its title holds still.** `Assignment 2` is the
thing. Its date is a deadline, and a deadline moving is the most common edit a
course page ever makes. Keying on the title means a reschedule updates the
event, which is the guarantee this program exists to provide.

**A lecture is a slot; its date holds still.** "The 9 Sep lecture" is the thing.
Its title is the instructor's current summary of what that slot will cover, and
a summary is *drafting*, not identity. Keying it on its title was wrong, and the
recorded course demonstrated how wrong on **2026-09-09**, within a single day:

| time | Sep 02 slot | Sep 09 slot |
|---|---|---|
| before | `Introduction, History, and Architectures` | `Coordinate Frames, Kinematics, Probability` |
| 01:00 | `Introduction, History, and` | gains `Architectures` |
| 13:00 | `Introduction, and History` | `Architectures, Representations, Assignment Teams & Forms` |

The pipeline caught both edits and published both times. Under a title key each
publish rotated two UIDs, so a calendar people subscribe to deleted and rebuilt
two events twice in one day. Two consequences, and the second is the serious
one:

- a deleted-and-recreated event drops whatever the student attached to the old
  one, and nothing in the format stops a subscriber's client from **re-firing
  an alarm that already fired** — not measured here, and not something this
  program can prevent once it has rotated the UID;
- when the title and the date move at the same time, a title-keyed tool
  **cannot tell "this item was rewritten" from "one item vanished and an
  unrelated one appeared"** — which is the exact confusion this program is
  built to prevent. The subject matter really did slide from the Sep 02 slot
  into the Sep 09 slot; nothing in a title comparison can see that.

`no_class` (§4b) is date-keyed for the same reason a lecture is: a holiday row
is a slot on the lecture schedule that happens to be empty, and its identity is
the day. Because `kind` is in the hash, a row that changes from `lecture` to
`no_class` rotates its UID once — a one-off, listed in the README's upgrade
note. It also means a rotated *title* that carries a "no class" onto another
date is not a mere retitle: that day has genuinely changed what it is, and the
UID follows. `tests/test_fetch_and_cli.py` asserts exactly which four of the
thirty lecture-page rows this affects when every title is rotated by one.

`unknown` keeps the title key. It is the kind that exists precisely because the
page stated no convention, so there is nothing to justify assuming its dates are
the stable half; the conservative default is to leave it where it was.

**Why the fallback is the same rule, not a second one.** Two rows a key cannot
tell apart are told apart by the half the key omitted. That mechanism already
existed — the recorded course has two `Project working session` lectures, on
Dec 07 and Dec 09, and numbering duplicates `#0`, `#1` … by sorted position was
fake stability: delete the Dec 07 row and the Dec 09 row slid into position 0
and **inherited the deleted row's UID**. The calendar moved the Dec 07 event
instead of deleting it, `--diff` reported a reschedule that never happened under
the wrong date, and the student's own edits on the real Dec 09 session were
silently reassigned to one that no longer existed.

Generalising the rule keeps it a single rule with the halves swapped per kind,
rather than two mechanisms that can disagree. What changes is *which* case it
catches:

| case | before | now |
|---|---|---|
| two `Project working session` lectures, different dates | ambiguous; date folded in | **not ambiguous**; their dates already differ |
| two lectures on the *same* date | not ambiguous | ambiguous; **title folded in** |
| two same-titled assignments, different dates | ambiguous; date folded in | unchanged |

The real page has a lecture and an assignment release on the same day
(2026-09-09); `kind` is in the hash, so those never collided and still do not.
Two *lectures* on one day is the genuine collision, and the fallback resolves it
to exactly the old behaviour for that pair — identity by title — which also
means those two rows, and only those two, do not get the retitle guarantee. The
cost is stated in §14 and asserted in `TestSameDayLectureCollision`.

**Why not disambiguate same-day lectures by position or by time?** Position is
the fake stability the audit already rejected: deleting the day's first lecture
would hand its UID to the second. Time is absent — every lecture row on the real
page is all-day — so it would resolve nothing on the only page we have, while
adding a field whose absence has to be special-cased anyway.

**Rejected: a stable page anchor such as `Week 15` in the hash.** It does not
exist usefully: `Week 15` is its own text line and *both* `Project working
session` rows sit under it.

**Rejected: remembering in `state.json` which keys were once ambiguous.** It
removes one rotation at the price of making a UID depend on run history: a fresh
clone would emit different UIDs from an incremental run for the same page.
Reproducibility is worth more.

**Rejected: a random UUID per event.** Not reproducible; every run would
republish the whole calendar.

**Rejected: hashing the raw source line.** A typo fix would rotate the UID.

**Rejected: making the choice configurable per source.** The stable half is a
property of what the row *is*, not of the page it came from, and a user who set
it wrongly would get the 2026-09-09 damage back with no way to see why. If a
page ever appears whose lectures are genuinely title-identified, that is the
moment to add the knob, not before.

**Migration cost, once.** The tuple order is `(url, half, kind)` in both
schemes, so the kinds whose stable half is the title hash *exactly* what they
hashed before: on the recorded course all 12 assignment events keep their
existing UIDs, and only the 30 lecture events rotate. Verified against the
pre-change implementation on the real pages, and the formula is pinned by
`test_a_title_keyed_uid_is_the_digest_it_always_was`. Existing subscribers
still see their lecture events replaced once, on the first run after this
change; nothing they attached to a lecture survives it. It is a one-off. No
promise is made that it is the last one — §8's rename was also a one-off — only
that this scheme has no further rotation scheduled in it.

**Known consequence.** `kind` is in the hash, so §8's rename rotated the UID of
the six release-date events exactly once, before this change.

---

## 8. `kind`: naming both halves of an assignment

The assignments page states its own convention:

> Assignments begin on the dates listed left, and are due via Brightspace at
> 11:59PM EST on the day listed below right.

So each assignment row carries **two** dates with different meanings. The
uncued left-hand date was previously labelled `kind: unknown` (6 records) — a
placeholder that told the reader nothing, on a page that had already explained
itself.

**Decision.** Five kinds (`no_class` was added later, §4b):

| kind | meaning | ICS `SUMMARY` | time |
|------|---------|---------------|------|
| `lecture` | a class meeting | *(title only)* | all-day |
| `assignment_out` | the day it is handed out | `ASSIGNED: <title>` | all-day |
| `assignment_due` | the deadline | `DUE: <title>` | 23:59 |
| `no_class` | a listed day on which the class does not meet | `NO CLASS: <title>` | all-day |
| `unknown` | retained for a source whose convention is not established | *(title only)* | all-day |

`no_class` is the one kind a config file cannot select as a page default: it is
derived per row, from the row's own wording, and no page consists only of days
when nothing happens.

The prefixes are load-bearing, not decoration. The release and the deadline of
one assignment share a title and land in the same calendar about a fortnight
apart; two bare `Assignment 3 - Trajectory Following` entries on different days
are worse than no entry at all. `CATEGORIES` carries the same distinction for
anything filtering programmatically.

`unknown` is kept rather than deleted so that a future source with no stated
convention degrades honestly instead of being mislabelled `assignment_out`.

### Recognising a due cue and deleting one are two different decisions

One regex answered both, and widening it for the first broke the second.

*Recognising* asks: does the text just before this date mean the date is a
deadline? Getting it wrong costs the deadline its `DUE:` prefix and its 23:59,
leaving an all-day event indistinguishable from a release date. So the
vocabulary is deliberately wide — a page that renames its "Due" column to
"Submission" has not stopped having deadlines.

*Deleting* asks: is this trailing word the name of the item, or a pointer at
the next date on the line? Getting it wrong truncates a real title — and for a
title-keyed kind (§7), which is every kind a due cue can appear on, the title is
hashed into the UID, so a truncation also **rotates the UID**, and the calendar
deletes the event and creates a new one in its place. That is a silent loss with
a knock-on effect; the recognition error is a visible one.

The two lists were shared, so widening the first widened the second, and real
titles disappeared into it:

| line | title produced |
|------|----------------|
| `Lab report submission` | `Lab report` |
| `Assignment Hand-in` | `Assignment` |
| `Project handed in` | `Project` |
| `How to Submit` | `How to` |

They are now two regexes. `DUE_CUE_RE` recognises and stays wide;
`TRAILING_CUE_RE` deletes and is a strict subset of it, in which everything
except `due` and `deadline` must be followed by a preposition or qualifier —
`submission by`, `handed in on` — because that is exactly what separates an
instruction pointing at a date from a noun ending a title. A bare `Submit` is a
title; `Submit by` is a cue. Recognising more than we delete costs nothing;
deleting more than we recognise costs a title.

**The residual cost, stated plainly.** `Assignment 1 Submission Sep. 13` keeps
`Submission` in its title, because that line and `Lab report submission` are
textually the same shape and nothing in the text distinguishes them. One of the
two has to lose; the one that loses visibly is the right one. The deadline is
still recognised, timed and prefixed either way, and both records on the line
keep the same title, so the UID stays stable.

**Rejected: `assignment_start`.** "Start" suggests the student begins work
then. The page says the assignment *is released* then.

**Rejected: one event spanning release → deadline.** A multi-day all-day block
across two weeks buries every lecture underneath it in month view, and the
alarm can only fire at one end.

**Rejected: dropping the release dates.** They are on the page and they are
useful — they are when the material becomes available.

### 8b. Which kinds get a reminder

**The report.** The same screenshot as §4b. Measured on the live feed:

| kind | events | reminders |
|---|---|---|
| `assignment_due` | 6 | 6 |
| `assignment_out` | 6 | 6 |
| `lecture` | 30 | 30 |
| **total** | **42** | **42** |

Every event carried a `VALARM`, because `build_event` appended one
unconditionally. Six of those 42 were deadlines. The other 36 told a student to
attend lectures already on a fixed weekly timetable, and to note that an
assignment had been handed out — a fact he learns by opening the assignment.

**Why that is a bug and not a preference.** A reminder stream that is 86% noise
is not merely annoying; it trains the reader to dismiss the notification
without looking, and the six it costs when that happens are the six that matter.
This is the same failure the exit codes are designed around (§10): an alarm
that fires when nothing is wrong stops being read, and then does not work when
something is.

**Decision.** Reminders follow the `kind`, and the mapping is configurable.
`ics.alarm_lead(cfg, kind)` is the single place the policy is read; it returns
`None` for "no `VALARM` at all", and otherwise a lead in days.

| kind | default | why |
|---|---|---|
| `assignment_due` | **on** | a deadline is the one thing that passes without announcing itself |
| `assignment_out` | off | the work cannot begin before it exists; advance warning is meaningless |
| `lecture` | off | already known, and recurring |
| `no_class` | off | worth *seeing* in the calendar, not worth an interruption |
| `unknown` | **on** | conservative: a date whose meaning the parser could not establish is the last one to silence |

Same shape as the UID rule in §7: one decision, taken per kind, because the
kinds are genuinely different and one answer for all of them had to be wrong
for most.

**It is a default, not a decision taken for the user.** This is an open-source
tool and the defaults encode one person's course page. `[alarms]` in the config
sets each kind independently to `off`, `on`, or a lead in days;
`lecture = 1` restores the previous behaviour exactly. `alarm_days_before`
keeps its old meaning as the default lead for whichever kinds have a reminder,
which is what leaves `--alarm-days` doing what it always did.

**Rejected: `0` as the off switch.** A lead of zero days is a setting somebody
wants — "remind me at the event itself" — and reading it as false would delete
their reminder while looking like it obeyed them. The off switch is a word
(`off`/`no`/`none`/`false`/`never`).

**Rejected: a global `alarms = due-only` mode.** Three or four named modes
would cover the common cases and none of the others, and a user wanting
"lectures too, but a week ahead" would have to patch the source. Per-kind
settings are barely more code and have no such edge.

**Consequence for `deploy.sh`.** Its structure gate asserted "one `VALARM` per
`VEVENT`", which under this change would block every publish. It now derives
the alarm-bearing kinds from the config through `ics.alarm_lead` — the parser's
own policy, not a second copy of it — and checks each event against its
`CATEGORIES` line. It fails both on an event that should have a reminder and
does not, and on one that carries a reminder the policy does not call for; the
original check only caught the first.

---

## 9. Times and time zones

Deadlines with no explicit time get `23:59`, from the page's own "11:59PM EST"
sentence. `DTSTART` carries `TZID=America/New_York`, and the file ships the
`VTIMEZONE` that defines it.

**This reverses the original decision.** `DTSTART` used to be floating local
time — no `TZID`, no `Z` — on the reasoning that a reminder should fire at
23:59 wherever the student is. That reasoning does not survive contact with
what a deadline *is*. A lecture is a wall-clock appointment; a submission
cutoff is a fixed instant on someone else's clock. Floating time put a student
in Los Angeles at 23:59 Pacific — **three hours after the real cutoff had
already passed**, with the reminder arriving on time for an event that was
over. Being an hour off for a traveller is a nuisance; being late for the one
thing the tool exists to prevent is a failure of the tool.

**The offsets are not the page's words.** The page says "11:59 PM EST", but the
US rule has run second-Sunday-March to first-Sunday-November since 2007, so a
September deadline in New York is **EDT (UTC-4)** and a December one is EST
(UTC-5). Hardcoding either offset is wrong for half the semester. Naming the
zone and shipping both rules gets every date right, and a test reads the rules
back against the real tz database.

**Why the earlier objection no longer holds.** The concern was that clients
have opinions about foreign `VTIMEZONE` blocks. The block shipped here is the
rule-based (`RRULE`) form that Google and Apple emit for themselves, not a
transition table. Its `;` separators are structured syntax and deliberately do
**not** pass through `escape()`, which is for TEXT properties only.

**Degradation, not a dangling reference.** `cfg.timezone` must be a key of
`ics.VTIMEZONES`. An unrecognised zone falls back to floating time rather than
emitting a `TZID` the file never defines, and `cfg.timezone = ""` selects
floating deliberately. All-day events are untouched: a lecture on a date has no
time of day to convert.

**Rejected: converting to UTC.** The deadline would display as some arbitrary
local hour, which reads as a bug to the user.

Release dates and lectures are all-day `VALUE=DATE` events with the RFC-correct
exclusive `DTEND` (next day).

---

## 10. Failure = non-zero exit code

```
0  success
1  unexpected internal error
2  fetch failure (network error, or HTTP other than 200/304)
3  parsed zero records, from any single source or overall
4  record count collapsed by more than the threshold, per source or overall
5  could not write the output files
6  a 304 blocked a re-parse the options had made necessary
```

**A code names what to do, not merely what happened.** Two failures whose
responses are opposite may not share a code. 2 and 6 did. Code 2 says the
network or the site is the problem — check connectivity, check the URLs. Code 6
fires when a page answers `304 Not Modified` to a request that deliberately
carried no validator, which means the fetch worked perfectly and the fix is on
the user's own disk: delete `state.json`. Sending that reader to inspect a
connection that had demonstrably just worked wasted their time in the one
situation where the tool knew exactly what was wrong. README made it stranger
still by defining code 2 as "HTTP other than 200/304" — for a failure that can
only fire on receiving a 304.

**Why an exit code.** Silent failure is the worst outcome for a reminder tool.
A calendar that quietly stops updating looks exactly like a calendar with
nothing new in it — the user finds out by missing a deadline. An exit code is
the one signal every scheduler already understands: cron mails it, systemd
marks the unit failed, `launchd` records it, a shell `&&` stops. No
configuration, no credentials, no extra moving part.

Codes 3 and 4 exist because "the page layout changed" and "the site is gated"
both present as a *successful* fetch of a page with no dates in it. Without the
guard the tool would cheerfully overwrite a good calendar with an empty one.

**The guard is per source, not only on the total.** It used to look at one
number: how many records the run produced altogether. The assignments page
carries all 12 deadlines out of 42 records — **28.6%** — so it could return
nothing whatsoever and the total still fell short of the 50% global threshold.
The run exited 0, republished the calendar, and every deadline in it
disappeared without a word. The thing this tool exists to deliver is a minority
of what it counts, so a number about the whole cannot protect a part. Each
source now fails on its own if it produces no records at all (code 3), or
collapses past `max_source_shrink_ratio` (code 4); the global guard runs
afterwards, unchanged. Per-source counts live in `state.json` under
`sources[url].record_count`, and a state file written before that field existed
recovers its baseline by counting the stored records, so the guard is live on
the very first run after the upgrade.

**Zero is a failure with or without a baseline.** The per-source check began as
`if not before: continue` — no history, nothing to compare against, carry on —
and that exempted precisely the situation the guard is for. On a **first run**,
whether on a new machine or one where `state.json` was just deleted (the
documented way to reset), every baseline is zero. So a page that happened to be
gated that morning yielded a calendar containing not one deadline, at exit 0.
And the zero it then stored excused the next run, and the one after: three
consecutive empty runs, all exit 0. A guard that only protects a source which
has already succeeded once protects nobody at the moment of setup, which is the
moment a scrape is most likely to be misconfigured.

The audit offered two fixes: declare "expected non-empty" per source in the
config file, or fail on zero regardless of history. **The second, because it
needs nothing to be kept in sync.** A source has a `[source:...]` section because
somebody put it there expecting dates on it; that is the declaration already,
and "this page is allowed to be empty" is not a state this program can
distinguish from "the scrape broke". A `may_be_empty` flag would only move the
judgement into a setting that is written once and never revisited — and, like
the `--force` flag rejected in §14, a switch that exists is a switch that ends
up permanently on. If a source is legitimately empty, delete its section from
the config; that is a conscious act, which is the point.

The shrink comparison still skips a source with no baseline: an unknown past
really is not evidence of a fall. Zero needs no past to be judged against.

**Not every network failure is a URLError.** urllib wraps only the
connect/request phase. A read timeout, a `RemoteDisconnected`, a reset — all
raised after the connection is open — escaped the handlers, left a traceback
and exited 1, which this table defines as "a bug in this program". A server
hanging up is not a bug in this program, and the scheduler needs the same
signal it gets for every other network failure. They are code 2.

**Code 5 is new.** A full disk, a quota or a read-only directory is not an
internal error, and sending the reader to look for a bug that is not there
wastes their time.

Code 5 used to carry a promise the other codes do not make — *neither* file
was modified — and that promise was not always true. It holds for every
failure to **write**, because both files are staged and `fsync`ed before
either is renamed. It never held for the **rename** itself: replace
`state.json` with a directory and `os.replace()` raises every time, after
`schedule.ics` has already been swapped. The run printed "neither file was
modified" anyway, left a `state.json.tmp` behind, and exited 5.

The desync was the smaller half of that bug. Every guard in this program —
codes 3, 4, the per-source checks, the parse signature — is built on the
assumption that a bad run says so. An alarm that can be wrong *in the
reassuring direction* is worse than no alarm, because it is believed. So the
message is now derived from what actually happened rather than asserted in
advance: nothing renamed yet says so, and a rename that fails after an earlier
one succeeded prints which file holds this run's content, which still holds
the previous run's, any staged copy that could not be removed, and how to get
the two back in step.

**Rejected: rolling back the renames that succeeded.** The previous content of
a destination that has been `os.replace()`d is gone; a rollback that cannot
restore it would be the same false all-clear one layer down.

**On codes 2, 3, 4 and 6 the previous `schedule.ics` is left untouched** — a stale
calendar is far better than an empty one — while `last_attempted_run` is
updated and `last_successful_run` is preserved, so stderr can say how long it
has been broken.

**Rejected: a log file.** Nobody reads a log that has never had anything
interesting in it.

**Rejected: email / push notification on failure.** Needs credentials, an SMTP
or API endpoint, and network access at exactly the moment the network is the
thing that failed. The scheduler already has a working notification path.

**Rejected: retrying automatically.** A layout change does not heal on retry;
it just delays the alarm and hammers someone else's site.

---

## 11. Politeness and caching

- `User-Agent` names the script, its purpose, its frequency and a contact
  address. An anonymous scraper deserves to be blocked.
- Minimum 1.0 s between requests.
- `ETag` / `Last-Modified` are stored in `state.json` and replayed as
  `If-None-Match` / `If-Modified-Since`. A `304` reuses the stored records and
  parses nothing — the normal outcome between edits, and it costs the site
  almost nothing.
- Two pages, a few times a day. `robots.txt` is not fetched because this is a
  hand-run personal tool over two known public URLs, not a crawler discovering
  links.

---

## 12. Tests never touch the network

`tests/__init__.py` replaces `socket.socket`, `socket.create_connection` and
`socket.getaddrinfo` with functions that raise. Two tests assert that the block
itself works.

**Why enforce rather than promise.** "The tests don't use the network" is a
claim that decays. A single accidental `HttpFetcher()` in a new test would make
the suite slow, flaky, and dependent on a third party's uptime — and would
quietly hit a live site on every run. Here it fails loudly instead.

Ordering matters: `ssl` executes `class SSLSocket(socket.socket)` at import
time, so `ssl`, `http.client` and `urllib.request` are imported *before* the
patch goes in. Patching first breaks the import rather than the network.

`tests/fixtures/*.html` are the real pages as fetched, committed verbatim
(~290 KB). They are the only reason the suite is hermetic, so they are **not**
gitignored, unlike `live/` and `out/`.

**Rejected: hand-written miniature HTML fixtures.** The bugs in §2b and §3 are
both *artifacts of real Google Sites markup*. A tidy hand-written fixture would
not contain them, and the tests would pass while the tool failed.

**Rejected: `unittest.mock.patch` on the fetcher.** Injection covers the code
under test; the socket block covers the code nobody thought about.

---

## 13. Other choices worth recording

- **The calendar and the state are written as one all-or-nothing step.**
  `state.json` was atomic from the start (temp file + `os.replace`);
  `schedule.ics` was not, and a write that failed partway truncated the good
  calendar to whatever fitted — an audit produced 20480 bytes ending
  mid-`VEVENT`, with 33 `BEGIN:VEVENT` against 32 `END:VEVENT` and no
  `END:VCALENDAR`, unparseable by any subscriber. The two files are also one
  fact split in half: a run that updated the calendar and then failed on the
  state left them permanently out of step, so `--diff` re-reported the same
  reschedule on every subsequent run. `state.write_files()` now stages, flushes
  and `fsync`s every file before renaming any of them, so every failure to
  *write* leaves both destinations untouched. The rename loop itself used to
  run unguarded, on the theory that its only exposure was a crash *between*
  two `os.replace()` calls — microseconds, closable only with a journal. That
  was wrong. `os.replace()` can plainly fail: a destination replaced by a
  directory raises `IsADirectoryError` every time, reproducibly, after the
  earlier rename has already gone through. It is guarded now, the leftover
  `.tmp` is cleaned up on that path too, and the failure is reported as what
  it is rather than as the all-or-nothing case — see §10.
- **A corrupt or missing `state.json` degrades to empty state** rather than
  crashing. The consequence is one run reported entirely as `NEW`, which is
  self-explanatory in the diff. This has to mean *structurally* corrupt, not
  merely unparseable: `{"records": null}`, `{"records": 5}` and
  `{"sources": "nope"}` are all valid JSON, and each used to crash several
  modules away at exit 1 — the exact moment the tool most needs to still run.
  Each field is validated on its own, so one bad key costs only that key, and
  unrecognised keys are carried through untouched.
  Validating the *containers* was not enough. `records` being a list said
  nothing about what was in each record, and one mistyped field inside one row
  still exited 1: `{"time": 5}` reached `rec.date + " " + rec.time` in
  `_stamp` — on `--diff`, the command the README puts in its quick start —
  while on the `304` path a non-string `time` reached `.split(":")`,
  `{"date": null}` and mixed-type `date`/`title` reached the record sort,
  `{"date": "junk"}` reached `date.fromisoformat`, and a non-string
  `title`/`kind` reached the `join` inside `assign_uids`. `valid_record()`
  now checks every field against the type its readers assume, at the loader
  rather than in five modules, and drops the row that fails: one bad row costs
  one row, exactly as one bad key costs one key. Exit 1 is reserved for a bug
  in this program, and a hand-edited state file is not one.
- **`state.json` records a parse signature** — the semester anchor, the default
  due time, the sources' default kinds. A `304` skips parsing by design, but
  `--fall-year` is applied *during* parsing, so a student starting a new
  semester was told "304 Not Modified, no changes", exited 0, and kept a
  calendar of last year's dates with nothing saying the flag had been ignored.
  When the signature differs the conditional headers are dropped so the pages
  come back in full; if a server answers 304 anyway, the run fails with code 6
  rather than publish records it knows were parsed under different rules.
  Code 6 rather than 2 because nothing about the fetch failed. `--alarm-days` is
  deliberately *not* in the signature: it changes the ICS, not the parse.
- **Everything tunable lives in `config.py`** — anchor, alarm policy, default due
  time, shrink thresholds, calendar name, sources — and everything
  course-specific among those comes from the config file rather than the
  source. Nothing tunable is hardcoded elsewhere. See §15.
- **Records are sorted by `(date, time, kind, title)`** before UID assignment,
  so the output is byte-identical between runs apart from `DTSTAMP`. There is a
  test for that.
- **Kind constants live in `config.py`, not `parse.py`.** `parse` imports
  `config`, so a source cannot name its default kind by constant unless the
  constants sit in `config`. `parse` re-exports them, so
  `from courseics.parse import KIND_LECTURE` still works.
- **`VTIMEZONE` blocks live in `timezones.py`**, imported by both `config` and
  `ics`. They were in `ics.py`, but the config loader has to validate a zone
  name against the same table, and `ics` imports `config`; a third module is
  cheaper than a lazy import inside a function.

---

## 14. Known limitations

Found by audit, deliberately not fixed. Each is a real defect; each is here
because the fix costs more than the defect, or because the fix is a product
decision rather than a bug fix.

### An unusually worded cancellation is missed

`NO_CLASS_RE` reads explicit statements and `RECESS_TITLE_RE` a closed list of
recess names anchored to the whole title (§4b). A row reading only `Labor Day`,
or one that strikes its topic through in HTML, or a page that writes
`class → asynchronous`, stays an ordinary `lecture` — in the last case a
`certain` one, with nothing to flag.

Not fixed, because this is the direction the rule is deliberately wrong in. The
cost of a miss is the `[?]` lecture the reader already had; the cost of a false
positive is a student who does not turn up to a class that is running. Widening
the vocabulary trades a cheap failure for an expensive one, and the words that
would have to be added — a bare holiday name, `break` unanchored — are exactly
the ambiguous ones.

### A class that moved is flagged, not followed

`moved to`, `rescheduled` and `postponed` veto the `no_class` promotion (§4b),
so the row stays a `[?]` lecture on its original date. The class does happen;
this program does not say where. Resolving it would mean reading a destination
out of free prose ("moved to the following Tuesday", "see Brightspace"), which
is the class of guess §2 and §5 exist to refuse.

### A `no_class` row keeps the page's redundant wording

`NO CLASS: Labor Day - no class` says it twice. Stripping the matched phrase
out of the title would read better and would cost nothing in identity terms —
`no_class` is date-keyed (§7), so the title is not in its UID — but the title
is the page's own text everywhere else in this program, and stripping can leave
a row with no title at all (`Oct. 12 No class`). The redundancy harms nobody.

### A lecture that changes date is a delete plus an add

A lecture is identified by its date (§7), so moving the Sep 09 session to
Sep 11 removes one event and creates another. Anything the student attached to
the original — a note, a snooze, a colour — is lost, and `--diff` reports
`DISAPPEARED` plus `NEW` rather than `DATE CHANGED`.

**Judgement: acceptable, and it is the right side of the trade.** Three
reasons, in order of weight.

1. *It is honest about what actually happened.* When a course moves a lecture
   it does not usually move a slot; it cancels one meeting and holds a
   different one, and the reader has to look at both days. "The 9 Sep lecture
   is now the 11 Sep lecture" is a claim about continuity that the page does
   not make and that the parser cannot check. Reporting a delete plus an add
   asserts less, and asserting less is this program's whole posture.
2. *A retitle is the edit that has actually been observed, and a moved lecture
   is visible anyway.* On this page, retitles happened twice in one day; no
   lecture has yet been observed changing its date. That is one page over one
   term, not a law — but it is the only evidence there is, and it points one
   way. A moved lecture also stays visible: it appears in `--diff` under two
   headings on the run it happens, once. A retitle under the old scheme was
   churn that could recur any number of times, each recurrence discarding
   whatever the student had attached to the event.
3. *The dangerous failure mode is gone either way.* The case that actually
   deceives a reader is title-and-date changing together, and no key can follow
   that. Under a date key it degrades to delete-plus-add, which is what it
   looks like. Under a title key it degraded to a *plausible-looking rename*,
   which is worse: it invites a false inference.

Not fixed, then, because there is no fix — only a choice of which edit loses,
and this is the one whose loss is smallest, rarest and most visible.

### Two lectures on one day lose the retitle guarantee

When a date holds two lectures, the title comes back into their identity (§7),
so retitling one of them is a delete plus an add for that row. The recorded
course has no such day; the case is covered by
`TestSameDayLectureCollision`. There is no third field to fall back on that is
not either position (rejected in §7 as fake stability) or absent (time).

### A retitled assignment is a delete plus an add

The mirror image, and unchanged by this work. An assignment is identified by
its title, so correcting a typo on the page — `Assignment 3 - Trajectory
Followng` → `Following` — rotates the UID. Not fixed for the reason above: the
alternative is to key an assignment by its deadline, which would make the
single most common edit on any course page a delete plus an add.

### An item on both pages produces two events

Deduplication is per source. A row that appears on the lectures page *and* the
assignments page yields two `VEVENT`s with different UIDs. It is visible rather
than silent — the reader sees the same title twice — and merging across sources
needs a rule for whose title, kind and confidence wins when the two disagree,
which is more machinery than the observed problem justifies. The real pages do
not currently overlap.

### Control bytes from the page reach the terminal and the ICS

A page containing raw C0 control bytes (`\x1b`, `\x07`) passes them through
`--list` to the terminal and into the `DESCRIPTION` field. RFC 5545 forbids
them in text values. `escape()` neutralises the characters that matter
structurally — `\`, `;`, `,`, CR and LF — so a control byte cannot forge a
property or a component boundary; the worst outcome is a mangled description or
a terminal escape sequence. Google Sites has never emitted one. Worth a
`str.translate` pass if a real page ever does.

The one property that did *not* go through `escape()` was `URL:`, which was
written straight from `source_url`. It now goes through `uri_value()`, which
percent-encodes control bytes rather than escaping them — `URL` is a URI value,
not a TEXT value, and TEXT escaping would backslash the `,` and `;` that are
legal URI characters. That path was never reachable (`source_url` comes from
`cfg.sources`), but reachability is the half of a security argument that goes
stale first.

### The shrink guard has no override

If a course legitimately halves its schedule — a page rewritten mid-semester,
a summer term — the guard blocks every subsequent run and the only escape is to
delete `state.json`, which also discards the diff baseline and the caching
headers. A `--force` flag is the obvious fix and is deliberately absent: a flag
that exists is a flag that gets added to the cron line, and then the guard
protects nothing. Deleting `state.json` is a conscious act, which is the point.

### The CRASHED handler is unreachable in normal use

`__main__.py` calls `cli.main()` directly, so the top-level
`except Exception -> "course-schedule-ics CRASHED"` guard at the bottom of `cli.py` only
runs when `cli.py` is executed as a script. Both documented entry points miss
it. It is three lines and harmless; wiring it into both entry points is a small
tidy-up rather than a defect with a victim.

### A corrupt `record_count` wedges the run at code 4

`sources[url].record_count` is the baseline the per-source shrink guard
measures against. It is validated as a non-negative integer, which is all a
type check can do — a *plausible but wrong* number, `9999` where the truth is
30, passes every check there is. The run then fails at code 4, every time,
against a page that has not changed. The failure path deliberately does not
rewrite `records` or the counts, so nothing self-corrects; the escape is to
delete `state.json`, exactly as for the shrink guard above.

Not fixed, because the fix is a way to overrule the guard, and §14's `--force`
argument applies unchanged: a switch that exists is a switch that ends up in
the cron line. Cross-checking `record_count` against the stored `records` was
considered and rejected — it would make the two fields disagree silently
instead of loudly, and `record_count` exists precisely so that a source whose
records were never stored still has a baseline.

What *was* fixed is the message. It used to name the page and only the page,
sending the reader to audit a healthy site with no hint that the other number
in the comparison came off their own disk. It now names the baseline, where it
is stored, and that it may be the wrong half.

### Two runs at once will collide over the `.tmp` files

Every staged file is written to `<path>.tmp`, a fixed name. Two `course-schedule-ics`
processes writing the same `--out-dir` at the same time will overwrite each
other's staging file and can rename a half-written one into place. There is no
lock.

Not fixed. The tool is documented and designed as a once-or-twice-a-day cron
job over two pages; a second concurrent run is not a thing that happens by
accident. The fix is either a randomised suffix (which leaks a temp file on
every crash instead of reusing one slot) or a lock file (which then needs stale
detection, which needs a PID check, which is more machinery than the observed
problem). Worth doing the moment anything schedules this more than once at a
time.

### The global shrink threshold is now redundant arithmetic

With the per-source guard in place, the total is the sum of the parts: if no
source has fallen by more than 50%, the total cannot have either — a weighted
average is bounded by its largest term. So while the two thresholds hold the
same value, the set of sources is unchanged, and every source has a baseline,
`max_shrink_ratio` cannot fire on anything `max_source_shrink_ratio` has not
already caught.

Kept anyway, and not merely out of caution. Each of those three conditions is
a thing that changes: the thresholds are separately tunable on purpose
(someone who loosens the per-source bound for a page that churns still wants
one on the whole calendar); adding or removing a source changes the
composition of the total; and the global baseline is counted from the stored
`records` while the per-source ones come from `record_count`, so they can
disagree. It costs three lines and one comparison. Recorded here so a future
reader does not mistake it for a second independent check that, on the current
configuration, it is not.

---

## 15. Making it somebody else's course

The parser began life pointed at one course, with the two URLs, the semester
year, the calendar name and the time zone written into `config.py`. Everything
course-specific now comes from a config file instead. That change was worth
recording, because the tempting shortcuts are all worse.

**The file format is INI, read with `configparser`.** TOML is the modern
answer, and `tomllib` is in the standard library — from 3.11. This project's
floor is 3.9 (§1), so TOML would mean either a dependency or a hand-written
parser, and a hand-written TOML parser is a new source of silent
misinterpretation in the one file whose misreading produces a plausible-looking
wrong calendar. JSON is stdlib everywhere and was rejected for one reason: no
comments. The settings here are ones a user reads once a semester and has to
reason about — which year an undated deadline belongs to, what a page's dates
mean — and the explanation belongs beside the value.

**There is no default course, and no default `fall_year`.** A missing config
file stops the run at exit 7 rather than falling back on anything. A default
that is nearly right is worse than no default here: every setting in this file
governs something that fails *plausibly*. A wrong `fall_year` produces a
complete, well-formed calendar of dates twelve months out. A missing time zone
produces deadlines that float. Neither looks like an error in a calendar app,
which is exactly why neither is allowed to be guessed.

**Validation happens at the boundary, not at the point of use.** `load_config`
checks every value and names the file, the section and the key when it refuses.
A time zone the tool has no `VTIMEZONE` for is rejected there — even though
`ics.timezone_id()` would quietly degrade it to floating time — because a
silent downgrade to floating is precisely the bug §9 exists to prevent, and a
config file is the wrong place to discover it. The message lists the zones that
do work; a rejection the reader cannot act on is only half a message.

**Exit code 7, and argparse.** Adding a config file added a new class of
failure: the run never started. It could not share code 2, which means "fetch
failure, check the network" — nothing had been fetched. That also exposed an
older wart: `argparse` exits 2 on a usage error, so a mistyped flag had been
reporting itself as a network problem all along. `_Parser` overrides `error()`
to exit 7 too.

**Sources keep their file order.** `configparser` preserves section order, and
that order decides which page is fetched first and how the log reads. Sorting
them would make two runs of the same config look gratuitously different.

**Two config files ship, and they are not interchangeable.**
`config.example.ini` is the annotated template, and every URL in it is
obviously fictional — nobody should publish a calendar of a stranger's lectures
because they forgot to edit a line. `examples/recorded-course.ini` describes the
two pages in `tests/fixtures/`; it is what the test suite loads and what makes
`--offline` work out of the box. A test asserts that both still parse, because
a shipped example that no longer loads is worse than none: it is the first
thing a new user copies, and the error names their edit rather than our
staleness.

**The recordings are the real pages.** Replacing them with synthetic HTML would
have made de-identification trivial and the tests worthless: the whitespace rule
in §3 exists *because* of how Google Sites actually splits a date across
`<span>` elements, and a fixture written by the same person who wrote the parser
cannot falsify it. The one edit made to them is the instructor's name and email
address in the page footer, replaced with a placeholder — a line that carries no
date and produces no record, so nothing the tests assert on depends on it.

**Publishing and scheduling are templates.** `deploy.sh` and `launchd/` read
`publish.conf`, and the shipped `publish.example.conf` is a template. The
`launchd` label, the schedule, the paths and the public repository are all
settings; the plist is generated from `launchd/agent.plist.template` at install
time rather than committed, because a committed plist is a file full of one
person's paths that everyone else has to remember to edit.

The one gate that could not be generalised by parameterising it is the privacy
gate. Its allow-list of hostnames is *derived* from the source URLs in the
parser config, so it cannot go stale when the config changes; anything
institution-specific — the name of an LMS, the shape of a student ID — goes in
`FORBIDDEN_PATTERNS`, where it is the user's own declaration of what must never
reach the public web.
