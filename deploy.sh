#!/usr/bin/env bash
#
# deploy.sh -- build the calendar and publish it to a public repository.
#
#     ./deploy.sh
#
# Settings come from publish.conf (copy publish.example.conf); every one of
# them can be overridden in the environment. This script is the only path from
# a local file to the public web, so it is deliberately more conservative than
# the parser: it would rather publish nothing than publish something wrong.
# The parser has to exit 0 cleanly, and then the output still has to pass a
# structure gate, a privacy gate and a scale gate before anything is pushed.
#
# Exit codes
#   0  published, or nothing had changed
#   1  usage or environment problem (no publish.conf, PUB_REPO missing,
#      dirty working tree, no gh/git)
#   2  the parser failed -- see its own codes in README; nothing is published
#   3  the output did not pass a gate (structure, privacy, scale, whitelist)
#   4  git push failed
#   5  pushed, but the published URL never served the new bytes
#
# The parser's exit code is deliberately NOT passed through: from here, every
# parser failure calls for the same response (look at the pages, publish
# nothing), and the reason is printed in words instead.

set -euo pipefail

SRC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=publish-config.sh
. "$SRC_REPO/publish-config.sh"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mOK\033[0m   %s\n' "$1"; }
info() { printf '         %s\n' "$1"; }
warn() { printf '    \033[33mnote\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mdeploy aborted:\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

PUB_ICS="$PUB_REPO/schedule.ics"

# --------------------------------------------------------------- 0. environment

step "0/6  environment"

if [ "$CSI_CONF_LOADED" != yes ]; then
    die "no settings file at $PUBLISH_CONF.
    Copy publish.example.conf to publish.conf and edit it. At minimum it has
    to say PUB_REPO -- the local clone of the public repository that will
    serve the calendar." 1
fi
ok "settings from $PUBLISH_CONF"

command -v git >/dev/null 2>&1 || die "git is not on PATH" 1
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH" 1
[ -n "$PUB_REPO" ] || die "PUB_REPO is not set in $PUBLISH_CONF" 1
[ -d "$PUB_REPO/.git" ] || die "PUB_REPO is not a git repository: $PUB_REPO" 1
[ -f "$CONFIG" ] || die "no parser config at $CONFIG (see config.example.ini)" 1

if [ "$(cd "$PUB_REPO" && pwd)" = "$SRC_REPO" ]; then
    die "PUB_REPO is this repository. The calendar has to be published from a
    SEPARATE repo: this one holds state.json and any pages you recorded, and
    the public one should hold nothing but the .ics." 1
fi
ok "git and python3 present; publishing into $PUB_REPO"

# A dirty public repo means somebody else's uncommitted work would be pushed
# along with the calendar.
if [ -n "$(git -C "$PUB_REPO" status --porcelain --untracked-files=all)" ]; then
    git -C "$PUB_REPO" status --short >&2
    die "the public repo has uncommitted changes (above). Deal with them first." 1
fi
ok "public repo working tree is clean"

if git -C "$PUB_REPO" remote | grep -q .; then
    git -C "$PUB_REPO" fetch -q origin "$PUB_BRANCH"
    LOCAL_HEAD=$(git -C "$PUB_REPO" rev-parse HEAD)
    REMOTE_HEAD=$(git -C "$PUB_REPO" rev-parse "origin/$PUB_BRANCH")
    if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
        die "the public repo and origin/$PUB_BRANCH have diverged
    (local ${LOCAL_HEAD:0:8}, remote ${REMOTE_HEAD:0:8}).
    Run git pull --ff-only there first." 1
    fi
    ok "in sync with origin/$PUB_BRANCH (${LOCAL_HEAD:0:8})"
else
    die "the public repo has no remote, so there is nothing to publish to." 1
fi

# ------------------------------------------------------------------- 1. parse

step "1/6  run the parser"

set +e
# shellcheck disable=SC2086
python3 "$SRC_REPO/__main__.py" --config "$CONFIG" --out-dir "$WORK_DIR" $PARSER_ARGS
PARSER_RC=$?
set -e

if [ "$PARSER_RC" -ne 0 ]; then
    case "$PARSER_RC" in
        2) WHY="a page could not be fetched" ;;
        3) WHY="a source parsed to zero records" ;;
        4) WHY="the record count collapsed against the last successful run" ;;
        5) WHY="the output files could not be written" ;;
        6) WHY="a stubborn 304: stored records cannot be re-parsed" ;;
        7) WHY="the run was not configured (bad flags or config file)" ;;
        *) WHY="an unexpected internal error" ;;
    esac
    die "the parser exited $PARSER_RC ($WHY). Nothing is published; the live
    calendar keeps whatever it had. Run the parser by hand to see its full
    message." 2
fi
ok "parser exited 0"

[ -s "$LIVE_ICS" ] || die "the parser reported success but $LIVE_ICS is missing
    or empty." 3

# ------------------------------------------------------------------- 2. gates

step "2/6  pre-publication gates (structure / privacy / scale)"

PREV_EVENTS=0
if [ -f "$PUB_ICS" ]; then
    PREV_EVENTS=$(grep -c '^BEGIN:VEVENT' "$PUB_ICS" || true)
fi

# The hosts the .ics is allowed to mention are the hosts of the pages the
# parser was told to read, plus anything explicitly added. Derived rather than
# listed, so it cannot go stale when the config changes.
ALLOWED_HOSTS=$(python3 - "$CONFIG" <<'PYHOSTS'
import sys
try:
    from urllib.parse import urlsplit
    import configparser
    p = configparser.ConfigParser(interpolation=None)
    with open(sys.argv[1], encoding="utf-8") as fh:
        p.read_file(fh)
    hosts = set()
    for section in p.sections():
        if section.startswith("source:") and p.has_option(section, "url"):
            host = urlsplit(p.get(section, "url").strip()).hostname
            if host:
                hosts.add(host.lower())
    print(" ".join(sorted(hosts)))
except Exception as exc:                       # noqa: BLE001
    print("ERROR:%s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
PYHOSTS
) || die "could not read the source hosts out of $CONFIG: $ALLOWED_HOSTS" 1
ALLOWED_HOSTS="$ALLOWED_HOSTS $EXTRA_ALLOWED_HOSTS"
[ -n "${ALLOWED_HOSTS// /}" ] || die "no hosts could be derived from $CONFIG" 1

# Which kinds of event are supposed to carry a reminder. Not every event does
# any more -- a lecture and a holiday are things to SEE, not to be alerted
# about -- so "one VALARM per VEVENT" is no longer the invariant, and asserting
# it would block every publish. What still has to hold is that the events which
# ARE meant to have a reminder all have one, so the gate asks the parser's own
# policy rather than keeping a second copy of it here that could drift.
ALARM_KINDS=$(python3 - "$SRC_REPO" "$CONFIG" <<'PYALARMS'
import sys
try:
    sys.path.insert(0, sys.argv[1])
    from courseics.config import KINDS, load_config
    from courseics.ics import alarm_lead
    cfg = load_config(sys.argv[2])
    print(" ".join(k for k in KINDS if alarm_lead(cfg, k) is not None))
except Exception as exc:                       # noqa: BLE001
    print("ERROR:%s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
PYALARMS
) || die "could not read the alarm policy out of $CONFIG: $ALARM_KINDS" 1

set +e
GATE_OUT=$(MIN_EVENTS="$MIN_EVENTS" SHRINK_PCT="$SHRINK_PCT" FORCE="$FORCE" \
    PREV_EVENTS="$PREV_EVENTS" ALLOWED_HOSTS="$ALLOWED_HOSTS" \
    FORBIDDEN_PATTERNS="$FORBIDDEN_PATTERNS" ALARM_KINDS="$ALARM_KINDS" \
    python3 - "$LIVE_ICS" <<'PYGATE'
import os, re, sys

path = sys.argv[1]
raw  = open(path, 'rb').read().decode('utf-8')
u    = re.sub(r'\r\n[ \t]', '', raw)          # RFC 5545: unfold
L    = u.split('\r\n')
n    = lambda p: sum(1 for x in L if x.startswith(p))

fail = []

# --- structure: never skippable -------------------------------------------
ev, evend = n('BEGIN:VEVENT'), n('END:VEVENT')
al, alend = n('BEGIN:VALARM'), n('END:VALARM')
uid       = n('UID:')

if L[0] != 'BEGIN:VCALENDAR':
    fail.append("first line is %r, not BEGIN:VCALENDAR" % L[0])
if [x for x in L if x][-1] != 'END:VCALENDAR':
    fail.append("last line is not END:VCALENDAR")
if ev != evend:
    fail.append("VEVENT unbalanced: %d BEGIN, %d END" % (ev, evend))
if al != alend:
    fail.append("VALARM unbalanced: %d BEGIN, %d END" % (al, alend))
if uid != ev:
    fail.append("%d UIDs for %d VEVENTs" % (uid, ev))
# Every event that is SUPPOSED to have a reminder needs exactly one, and no
# other event may have any. Counting BEGIN/END pairs is not enough: deleting a
# whole VALARM block leaves the counts balanced and the reminder gone. So the
# check walks the events and compares each one against the policy the parser
# was configured with, rather than against a number.
alarm_kinds = set(os.environ.get('ALARM_KINDS', '').split())
events, cur = [], None
for x in L:
    if x == 'BEGIN:VEVENT':
        cur = []
    elif x == 'END:VEVENT':
        if cur is not None:
            events.append(cur)
        cur = None
    elif cur is not None:
        cur.append(x)

missing, spurious, unlabelled = 0, 0, 0
for e in events:
    kinds = [x[len('CATEGORIES:'):] for x in e if x.startswith('CATEGORIES:')]
    alarms = sum(1 for x in e if x == 'BEGIN:VALARM')
    if len(kinds) != 1:
        unlabelled += 1
        continue
    want = 1 if kinds[0] in alarm_kinds else 0
    if alarms < want:
        missing += 1
    elif alarms > want:
        spurious += 1
if unlabelled:
    fail.append("%d VEVENT(s) carry no single CATEGORIES line, so their "
                "reminder cannot be checked" % unlabelled)
if missing:
    fail.append("%d event(s) of a kind that should be reminded about (%s) "
                "have no VALARM"
                % (missing, ", ".join(sorted(alarm_kinds)) or "none"))
if spurious:
    fail.append("%d event(s) carry a VALARM the configured alarm policy does "
                "not call for" % spurious)

uids = [x[4:] for x in L if x.startswith('UID:')]
if len(set(uids)) != len(uids):
    fail.append("duplicate UIDs: %d lines, %d distinct" % (len(uids), len(set(uids))))

depth = 0
for x in L:
    if   x.startswith('BEGIN:'): depth += 1
    elif x.startswith('END:'):   depth -= 1
    if depth < 0:
        fail.append("BEGIN/END nesting went negative")
        break
if depth != 0:
    fail.append("BEGIN/END nesting ends at depth %d" % depth)

if raw.count('\r\n') != raw.count('\n'):
    fail.append("a bare LF is present; line endings are not pure CRLF")
over = [x for x in raw.split('\r\n') if len(x.encode()) > 75]
if over:
    fail.append("%d line(s) exceed 75 octets (folding is broken)" % len(over))

# --- privacy: never skippable ---------------------------------------------
# This is the only path to the public web. Anything that should not leave the
# machine is stopped here.
allowed = set(h for h in os.environ.get('ALLOWED_HOSTS', '').split() if h)
for m in re.finditer(r'https?://([^/\s\\,;:]+)', u):
    host = m.group(1).lower()
    if host not in allowed:
        fail.append("host not on the allow-list: %s" % host)

# The UID suffix is not a mail domain; everything else that looks like an
# address is treated as one.
for m in re.finditer(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)*', u):
    addr = m.group(0)
    if addr.endswith('@course-schedule-ics'):
        continue
    fail.append("what looks like an email address: %s" % addr)

for pat, label in [
    (r'(?i)(api[_-]?key|access[_-]?token|secret|bearer\s)', 'a credential-ish word'),
    (r'\b(gh[pousr]_[A-Za-z0-9]{16,})', 'a GitHub token'),
    (r'\b(AKIA[0-9A-Z]{16})', 'an AWS key id'),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', 'a private key'),
    (r'/Users/[^/\s]+/', 'a macOS home directory path'),
    (r'/home/[^/\s]+/', 'a Linux home directory path'),
    (r'\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', 'a private IP address'),
    (r'\b192\.168\.\d{1,3}\.\d{1,3}\b', 'a private IP address'),
]:
    hits = re.findall(pat, u)
    if hits:
        fail.append("%s (%d occurrence(s))" % (label, len(hits)))

for extra in os.environ.get('FORBIDDEN_PATTERNS', '').splitlines():
    extra = extra.strip()
    if not extra:
        continue
    try:
        rx = re.compile(extra, re.IGNORECASE)
    except re.error as exc:
        fail.append("FORBIDDEN_PATTERNS entry %r is not a valid regex: %s"
                    % (extra, exc))
        continue
    hits = rx.findall(u)
    if hits:
        fail.append("forbidden pattern %r matched %d time(s)" % (extra, len(hits)))

# --- scale: FORCE=1 waives this one, and only this one --------------------
force  = os.environ.get('FORCE') == '1'
min_ev = int(os.environ['MIN_EVENTS'])
shrink = int(os.environ['SHRINK_PCT'])
prev   = int(os.environ['PREV_EVENTS'])
scale  = []

if ev < min_ev:
    scale.append("%d events is below the floor of %d" % (ev, min_ev))
if prev > 0:
    floor = prev * (100 - shrink) / 100.0
    if ev < floor:
        scale.append("%d events is more than %d%% below the published %d "
                     "(floor %.1f)" % (ev, shrink, prev, floor))

if scale and not force:
    fail.extend(scale)
elif scale and force:
    for s in scale:
        print("SKIPPED::%s" % s)

print("COUNT::%d" % ev)
for f in fail:
    print("FAIL::%s" % f)
sys.exit(1 if fail else 0)
PYGATE
)
GATE_RC=$?
set -e

echo "$GATE_OUT" | grep '^SKIPPED::' | sed 's/^SKIPPED::/    \x1b[33mwaived\x1b[0m scale gate: /' || true
NEW_EVENTS=$(echo "$GATE_OUT" | sed -n 's/^COUNT:://p')

if [ "$GATE_RC" -ne 0 ]; then
    echo "$GATE_OUT" | sed -n 's/^FAIL::/    - /p' >&2
    die "the output did not pass the gates (above). Nothing is published." 3
fi

ok "structure sound ($NEW_EVENTS VEVENTs, a unique UID each, a VALARM on exactly the kinds the config asks for ($ALARM_KINDS); CRLF and folding correct)"
ok "privacy gate passed (hosts limited to: $ALLOWED_HOSTS)"
if [ "$PREV_EVENTS" -gt 0 ]; then
    ok "scale plausible ($NEW_EVENTS events; $PREV_EVENTS published)"
else
    ok "scale plausible ($NEW_EVENTS events; nothing published yet)"
fi

# ------------------------------------------------------------------ 3. change

step "3/6  compare against what is already published"

# The fingerprint ignores DTSTAMP on purpose: it changes on every run because
# it is the generation time, not the schedule. Comparing raw bytes would
# produce an empty commit every single run.
fingerprint() { grep -v '^DTSTAMP:' "$1" | shasum -a 256 | cut -d' ' -f1; }

NEW_FP=$(fingerprint "$LIVE_ICS")
if [ -f "$PUB_ICS" ]; then
    OLD_FP=$(fingerprint "$PUB_ICS")
else
    OLD_FP="(nothing published yet)"
fi
info "published  ${OLD_FP:0:16}"
info "this run   ${NEW_FP:0:16}"

if [ "$NEW_FP" = "$OLD_FP" ]; then
    ok "the schedule has not changed; no empty commit. What is live is current."
    printf '\n\033[32mdeploy finished: nothing to do.\033[0m\n'
    [ -n "$PUBLIC_URL" ] && printf 'subscribe at %s\n' "$PUBLIC_URL"
    exit 0
fi
ok "the schedule changed; continuing"

# ------------------------------------------------------------------ 4. commit

step "4/6  commit and push"

cp "$LIVE_ICS" "$PUB_ICS"

# Only whitelisted files may be committed. This gate catches the day somebody
# drops something else into the public repo.
# (macOS ships bash 3.2, whose parser trips over a `case` arm's `)` inside
# $( ), so this is done with grep rather than a case statement.)
ALLOW_ARGS=""
for f in $ALLOWED_FILES; do
    ALLOW_ARGS="$ALLOW_ARGS -e $f"
done
# shellcheck disable=SC2086
UNEXPECTED=$(git -C "$PUB_REPO" status --porcelain --untracked-files=all \
    | sed 's/^...//' \
    | grep -v -x -F $ALLOW_ARGS || true)
if [ -n "$UNEXPECTED" ]; then
    echo "$UNEXPECTED" | sed 's/^/    - /' >&2
    git -C "$PUB_REPO" checkout -- schedule.ics 2>/dev/null || true
    die "files that are not on the whitelist are about to be committed to the
    public repo (above). Rolled back; nothing was pushed." 3
fi
ok "everything staged is on the whitelist"

git -C "$PUB_REPO" add schedule.ics
git -C "$PUB_REPO" commit -q -m "Update schedule ($NEW_EVENTS events)"
COMMIT=$(git -C "$PUB_REPO" rev-parse HEAD)
ok "commit ${COMMIT:0:8}"

if ! git -C "$PUB_REPO" push -q origin "$PUB_BRANCH"; then
    die "push failed. The commit is already made locally; re-run once the
    network is back." 4
fi
ok "pushed to origin/$PUB_BRANCH"

# ------------------------------------------------------------------ 5. verify

step "5/6  verify what is actually being served"

if [ -z "$PUBLIC_URL" ]; then
    warn "PUBLIC_URL is not set, so there is nothing to check. The push
         succeeded, but whether the file is really being served is unverified."
else
    WANT=$(shasum -a 256 "$PUB_ICS" | cut -d' ' -f1)
    info "expecting ${WANT:0:16}"
    info "polling $PUBLIC_URL (up to ${VERIFY_TIMEOUT}s; static hosts often take 30-60s)"

    TMP=$(mktemp -t csi-verify)
    trap 'rm -f "$TMP"' EXIT

    DEADLINE=$(( $(date +%s) + VERIFY_TIMEOUT ))
    GOT=""
    while [ "$(date +%s)" -lt "$DEADLINE" ]; do
        if curl -fsS --max-time 20 -H 'Cache-Control: no-cache' -o "$TMP" "$PUBLIC_URL" 2>/dev/null; then
            GOT=$(shasum -a 256 "$TMP" | cut -d' ' -f1)
            [ "$GOT" = "$WANT" ] && break
        fi
        printf '.'
        sleep 5
    done
    printf '\n'

    if [ "$GOT" != "$WANT" ]; then
        die "the published file did not become the pushed version within
    ${VERIFY_TIMEOUT}s. Expected ${WANT:0:16}, got ${GOT:0:16}.
    The commit IS on origin/$PUB_BRANCH; the host may still be building.
    Check by hand later:  curl -s $PUBLIC_URL | shasum -a 256" 5
    fi
    ok "the served file is byte-for-byte what was pushed (${GOT:0:16})"

    CT=$(curl -fsS -o /dev/null -w '%{content_type}' "$PUBLIC_URL")
    if [ "$CT" != "text/calendar" ]; then
        warn "Content-Type is $CT, not text/calendar. The file is correct, but
         some calendar clients are fussy about this."
    else
        ok "Content-Type: text/calendar"
    fi
fi

# ------------------------------------------------------------------- 6. done

step "6/6  done"
printf '\n\033[32mdeploy succeeded.\033[0m %s events published.\n' "$NEW_EVENTS"
if [ -n "$PUBLIC_URL" ]; then
    printf 'subscribe at  %s\n' "${PUBLIC_URL/https:/webcal:}"
fi
