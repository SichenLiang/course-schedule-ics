#!/usr/bin/env bash
#
# status.sh -- one command, three questions:
#
#   When did it last succeed? Has it failed since? How often?
#
# The worst outcome for a scheduled reminder job is not "it errored". It is
# "it went quiet and nobody noticed". So the first line is always a verdict,
# not data -- and so is the exit code: 0 healthy, 1 somebody should look.
#
# Read-only. It changes nothing and triggers no run.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../publish-config.sh
. "$REPO/publish-config.sh"

LABEL="$LAUNCHD_LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_="$(id -u)"

b()      { printf '\033[1m%s\033[0m' "$1"; }
green()  { printf '\033[32m%s\033[0m' "$1"; }
yellow() { printf '\033[33m%s\033[0m' "$1"; }
red()    { printf '\033[31m%s\033[0m' "$1"; }

epoch_of() { date -j -f '%Y-%m-%dT%H:%M:%S%z' "$1" +%s 2>/dev/null; }
pretty()   { date -j -f '%Y-%m-%dT%H:%M:%S%z' "$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf '%s' "$1"; }

ago() {
    local s=$1
    if   [ "$s" -lt 60 ]    ; then echo "${s}s ago"
    elif [ "$s" -lt 3600 ]  ; then echo "$((s/60))m ago"
    elif [ "$s" -lt 172800 ]; then echo "$((s/3600))h ago"
    else echo "$((s/86400))d $(( (s%86400)/3600 ))h ago"
    fi
}

# ------------------------------------------------------ collect: the log

# The rotated file first, the current one second: that is chronological order.
RESULTS="$(
    { [ -f "$LOG.1" ] && cat "$LOG.1"; [ -f "$LOG" ] && cat "$LOG"; } 2>/dev/null \
    | grep ' RESULT rc=' || true
)"

TOTAL=0; FAILS=0; SKIPS=0
LAST_OK_TS=""; LAST_TS=""; LAST_STATUS=""; LAST_RC=""; LAST_WHY=""
if [ -n "$RESULTS" ]; then
    TOTAL=$(printf '%s\n' "$RESULTS" | wc -l | tr -d ' ')
    FAILS=$(printf '%s\n' "$RESULTS" | awk '$5=="FAIL"' | wc -l | tr -d ' ')
    SKIPS=$(printf '%s\n' "$RESULTS" | awk '$5=="SKIP"' | wc -l | tr -d ' ')
    LAST_OK_TS=$(printf '%s\n' "$RESULTS" | awk '$5=="OK"{t=$2} END{print t}')
    LAST_LINE=$(printf '%s\n' "$RESULTS" | tail -1)
    LAST_TS=$(printf '%s' "$LAST_LINE"     | awk '{print $2}')
    LAST_RC=$(printf '%s' "$LAST_LINE"     | awk '{print $4}' | sed 's/rc=//')
    LAST_STATUS=$(printf '%s' "$LAST_LINE" | awk '{print $5}')
    LAST_WHY=$(printf '%s' "$LAST_LINE"    | awk '{$1=$2=$3=$4=$5=""; print}' | sed 's/^ *//')
fi

# ------------------------------------------------------ collect: launchd

LOADED=no
PRINT_OUT="$(launchctl print "gui/$UID_/$LABEL" 2>/dev/null)"
[ -n "$PRINT_OUT" ] && LOADED=yes
LC_STATE=$(printf '%s\n' "$PRINT_OUT" | awk -F'= *' '/^[[:space:]]*state = /{print $2; exit}')
LC_EXIT=$(printf '%s\n'  "$PRINT_OUT" | awk -F'= *' '/last exit code/{print $2; exit}')
LC_RUNS=$(printf '%s\n'  "$PRINT_OUT" | awk -F'= *' '/^[[:space:]]*runs = /{print $2; exit}')

SCHED=""
if [ -f "$PLIST" ]; then
    SCHED="$(plutil -extract StartCalendarInterval json -o - "$PLIST" 2>/dev/null \
        | tr -d ' "' | tr '}' '\n' | sed -n 's/.*Hour:\([0-9]*\).*/\1/p' \
        | awk '{printf "%02d:00 ", $1}')"
    [ -z "$SCHED" ] && SCHED="(StartCalendarInterval unreadable)"
fi

# ------------------------------------------------------ the verdict

NOW=$(date +%s)
OK_AGO=""
if [ -n "$LAST_OK_TS" ]; then
    E=$(epoch_of "$LAST_OK_TS"); [ -n "$E" ] && OK_AGO=$(( NOW - E ))
fi

if [ "$LOADED" != yes ] && [ -z "$LAST_OK_TS" ] && [ -z "$LAST_TS" ]; then
    VERDICT="$(yellow 'NOT SET UP') the job is not loaded and has never run. Install it with launchd/install.sh, or run deploy.sh by hand."
elif [ "$LOADED" != yes ]; then
    VERDICT="$(red 'BROKEN')   the job is not loaded -- it will not run again on its own. The calendar is frozen wherever the last successful run left it."
elif [ -z "$LAST_TS" ]; then
    VERDICT="$(yellow 'UNKNOWN')  loaded, but the log has no run in it yet."
elif [ -z "$OK_AGO" ]; then
    VERDICT="$(red 'BROKEN')   ran ${TOTAL} time(s) and never once succeeded. The calendar has never been updated."
elif [ "$OK_AGO" -gt $(( STALE_HOURS * 3600 )) ]; then
    VERDICT="$(red 'BROKEN')   the last success was $(ago "$OK_AGO") -- over ${STALE_HOURS}h. The calendar is almost certainly stale."
elif [ "$LAST_STATUS" = FAIL ]; then
    VERDICT="$(yellow 'ATTENTION') the most recent run failed (rc=$LAST_RC), but it did succeed $(ago "$OK_AGO")."
else
    VERDICT="$(green 'HEALTHY')  loaded; last run ${LAST_STATUS}, last success $(ago "$OK_AGO")."
fi

# ------------------------------------------------------ output

printf '\n%s\n' "$(b 'course-schedule-ics scheduled publishing')"
printf '%s\n\n' "$VERDICT"

printf '  %-12s %s\n' "job" "$LABEL"
if [ "$LOADED" = yes ]; then
    printf '  %-12s %s\n' "loaded" "yes (gui/$UID_)  state=${LC_STATE:-?}  runs=${LC_RUNS:-?}  launchd last exit code=${LC_EXIT:-?}"
else
    printf '  %-12s %s\n' "loaded" "$(red no). Reinstall: \"$REPO/launchd/install.sh\""
fi
printf '  %-12s %s\n' "schedule" "${SCHED:-(no plist at $PLIST)}"

if [ -n "$LAST_OK_TS" ]; then
    printf '  %-12s %s  (%s)\n' "last success" "$(pretty "$LAST_OK_TS")" "$(ago "$OK_AGO")"
else
    printf '  %-12s %s\n' "last success" "$(red 'never')"
fi
if [ -n "$LAST_TS" ]; then
    printf '  %-12s %s  rc=%s %s  %s\n' "last run" "$(pretty "$LAST_TS")" "$LAST_RC" "$LAST_STATUS" "$LAST_WHY"
fi
printf '  %-12s %s run(s), %s failure(s), %s skipped for no network (only what is still in the logs)\n' \
    "totals" "$TOTAL" "$( [ "$FAILS" -gt 0 ] && red "$FAILS" || echo "$FAILS" )" "$SKIPS"

LOGSIZE="(none yet)"
[ -f "$LOG" ] && LOGSIZE="$(du -h "$LOG" | awk '{print $1}')"
ROT="none"
[ -f "$LOG.1" ] && ROT="publish.log.1 ($(du -h "$LOG.1" | awk '{print $1}'))"
printf '  %-12s %s  (%s)\n' "log" "$LOG" "$LOGSIZE"
printf '  %-12s rotated at %s bytes into .1, older discarded. Rotated copy: %s\n' "" "$LOG_MAX" "$ROT"

if [ -n "$RESULTS" ]; then
    printf '\n  %s\n' "$(b 'last 8 runs')"
    printf '%s\n' "$RESULTS" | tail -8 | while read -r line; do
        t=$(printf '%s' "$line" | awk '{print $2}')
        s=$(printf '%s' "$line" | awk '{print $5}')
        r=$(printf '%s' "$line" | awk '{print $4}' | sed 's/rc=//')
        w=$(printf '%s' "$line" | awk '{$1=$2=$3=$4=$5=""; print}' | sed 's/^ *//')
        case "$s" in
            OK)   mark="$(green OK)  " ;;
            FAIL) mark="$(red FAIL)" ;;
            *)    mark="$(yellow SKIP)" ;;
        esac
        printf '    %s  rc=%-3s %s  %s\n' "$(pretty "$t")" "$r" "$mark" "$w"
    done
fi

printf '\n  failures in full:  grep -n -A20 %s %s\n' "'RESULT rc=[^0]'" "\"$LOG\""
printf '  run it now:        launchctl kickstart -k gui/%s/%s\n' "$UID_" "$LABEL"
printf '  stop it:           launchctl bootout gui/%s/%s\n\n' "$UID_" "$LABEL"

case "$VERDICT" in
    *HEALTHY*) exit 0 ;;
    *)         exit 1 ;;
esac
