#!/usr/bin/env bash
#
# run.sh -- what launchd actually executes.
#
# It wraps deploy.sh and does the three things deploy.sh should not have to
# care about:
#
#   1. Log rotation. macOS has no logrotate, so an application log nobody
#      manages grows for ever. Before each run, a log over LOG_MAX is moved to
#      .1 and anything older is dropped. There are never more than two files.
#
#   2. Making failure noticeable. A scheduled job that fails silently for three
#      months leaves a calendar frozen three months back, which is far worse
#      than a wrong reminder. A non-zero exit posts a macOS notification with
#      the code and what it means. Notifying is best-effort and never fatal --
#      the log is the source of truth.
#
#   3. Leaving one greppable verdict line per run, so status.sh can answer
#      "when did it last work, and has it failed since?" without parsing prose.
#
# Exit code: deploy.sh's, unchanged, so `launchctl print`'s "last exit code"
# means something.

set -uo pipefail   # deliberately no -e: a deploy failure is a case to handle

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../publish-config.sh
. "$REPO/publish-config.sh"

DEPLOY="$REPO/deploy.sh"
LABEL="$LAUNCHD_LABEL"
NOTIFY_TIMEOUT=10

ts()  { date '+%Y-%m-%dT%H:%M:%S%z'; }
say() { printf '[course-schedule-ics] %s %s\n' "$(ts)" "$*"; }

# deploy.sh's exit codes, in words. Unknown codes get a sentence too: deploy.sh
# runs under `set -e`, so a failing external command can surface its own code.
rc_meaning() {
    case "$1" in
        0)   echo "published, or the schedule had not changed" ;;
        1)   echo "environment problem (settings, public repo missing, dirty tree, out of sync)" ;;
        2)   echo "the parser failed; the live calendar is untouched" ;;
        3)   echo "a pre-publication gate refused (structure/privacy/scale/whitelist)" ;;
        4)   echo "git push failed; the commit exists locally, re-run when the network is back" ;;
        5)   echo "pushed, but the published URL never served the new bytes" ;;
        126) echo "deploy.sh exists but is not executable" ;;
        127) echo "deploy.sh not found -- renamed or deleted?" ;;
        *)   echo "unexpected exit code (a command inside deploy.sh aborted under set -e)" ;;
    esac
}

# Always returns 0: a notification that cannot be sent is not a job failure.
notify() {
    local title="$1" msg="$2"

    if [ "${CSI_NO_NOTIFY:-0}" = "1" ]; then
        say "NOTIFY suppressed by CSI_NO_NOTIFY=1 (would have said: $title / $msg)"
        return 0
    fi
    if [ ! -x /usr/bin/osascript ]; then
        say "WARN   no /usr/bin/osascript; notifications are unavailable. The log is the record."
        return 0
    fi

    # Background plus a watchdog: if the system throws up a permission dialog
    # and wedges osascript, it must not wedge the scheduled job with it.
    /usr/bin/osascript -e 'on run argv
display notification (item 2 of argv) with title (item 1 of argv)
end run' "$title" "$msg" &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$NOTIFY_TIMEOUT" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        say "WARN   the notification did not return within ${NOTIFY_TIMEOUT}s; gave up (probably a permission dialog). The log is still written."
        return 0
    fi
    wait "$pid"
    local nrc=$?
    if [ "$nrc" -ne 0 ]; then
        say "WARN   notification failed (osascript rc=$nrc). The log is still written; the exit code is unaffected."
    else
        say "NOTIFY sent (macOS files these under Script Editor)"
    fi
    return 0
}

# The last RESULT line. Just after a rotation it lives in .1.
last_result_line() {
    local l=""
    [ -f "$LOG" ] && l="$(grep -h ' RESULT rc=' "$LOG" 2>/dev/null | tail -1)"
    if [ -z "$l" ] && [ -f "$LOG.1" ]; then
        l="$(grep -h ' RESULT rc=' "$LOG.1" 2>/dev/null | tail -1)"
    fi
    printf '%s' "$l"
}

# ----------------------------------------------------------------- 1. rotate

mkdir -p "$LOG_DIR" || {
    echo "[course-schedule-ics] $(ts) FATAL cannot create $LOG_DIR" >&2
    exit 1
}

PREV_RESULT="$(last_result_line)"

ROTATED=""
if [ -f "$LOG" ]; then
    SIZE="$(stat -f %z "$LOG" 2>/dev/null || stat -c %s "$LOG" 2>/dev/null || echo 0)"
    if [ "$SIZE" -gt "$LOG_MAX" ]; then
        mv -f "$LOG" "$LOG.1" && ROTATED="$SIZE"
    fi
fi
: >> "$LOG"

# From here everything this script prints is appended to the CURRENT log, by
# path rather than through an inherited descriptor -- so the rotation above
# cannot end up catching this run's output.
exec >> "$LOG" 2>&1

printf '\n'
say "=== START  label=$LABEL pid=$$ ==="
[ -n "$ROTATED" ] && say "ROTATE previous log was ${ROTATED} bytes > ${LOG_MAX}; moved to publish.log.1 (older discarded)"

# ------------------------------------------------------------- 2. missed runs

# If far more than the scheduled gap has passed, the windows in between never
# ran: the machine was off, the job was unloaded, or the plist broke. That is
# precisely the "the reminders stopped and nobody noticed" failure, so it is
# worth a notification the moment it is noticed.
if [ -n "$PREV_RESULT" ]; then
    PREV_TS="$(printf '%s' "$PREV_RESULT" | awk '{print $2}')"
    PREV_EPOCH="$(date -j -f '%Y-%m-%dT%H:%M:%S%z' "$PREV_TS" +%s 2>/dev/null || true)"
    if [ -n "$PREV_EPOCH" ]; then
        GAP=$(( ( $(date +%s) - PREV_EPOCH ) / 3600 ))
        if [ "$GAP" -ge "$GAP_HOURS" ]; then
            say "GAP    ${GAP} hours since the last run; the windows in between did not run"
            notify "course-schedule-ics was silent for ${GAP}h" \
                   "Last run ${PREV_TS}. No scheduled window ran in between. It is running again now, but the calendar was stale for that whole stretch."
        fi
    fi
fi

# ------------------------------------------------------------ 3. network check

# No network is not a failure, it is "nothing to do this time". Treating it as
# a failure means a scary notification at 01:00 for one Wi-Fi hiccup, and an
# alarm that cries wolf stops being read. A genuine outage still surfaces --
# through the gap check above and through "last success" in status.sh.
if ! /usr/bin/curl -fsS --max-time 10 -o /dev/null https://github.com 2>/dev/null; then
    say "RESULT rc=0 SKIP no network (github.com unreachable); not running, not a failure [0s]"
    say "=== END ==="
    exit 0
fi

# -------------------------------------------------------------- 4. run deploy

# deploy.sh colours its output. Escape codes in a log make grep painful, so the
# output is captured, stripped and then appended.
OUT="$(mktemp -t csi-run 2>/dev/null || echo "$LOG_DIR/.last-run.out")"
trap 'rm -f "$OUT"' EXIT

T0=$(date +%s)
( cd "$REPO" && "$DEPLOY" ) > "$OUT" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - T0 ))

LC_ALL=C sed -E "s/$(printf '\033')\[[0-9;]*[a-zA-Z]//g" "$OUT" >> "$LOG" 2>/dev/null \
    || cat "$OUT" >> "$LOG"

MEANING="$(rc_meaning "$RC")"

if [ "$RC" -eq 0 ]; then
    say "RESULT rc=0 OK   $MEANING [${ELAPSED}s]"
else
    say "RESULT rc=$RC FAIL $MEANING [${ELAPSED}s]"
    # Carry deploy.sh's own last words into the banner, so the reader does not
    # have to open the log to find out which step it was.
    DETAIL="$(sed -E "s/$(printf '\033')\[[0-9;]*[a-zA-Z]//g" "$OUT" 2>/dev/null \
              | grep -v '^[[:space:]]*$' | tail -3 | tr '\n' ' ' | cut -c1-180)"
    notify "course-schedule-ics failed (exit $RC)" \
           "$MEANING. The calendar was not updated. $DETAIL"
fi

say "=== END ==="
exit "$RC"
