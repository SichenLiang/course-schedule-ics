#!/usr/bin/env bash
#
# uninstall.sh -- stop the scheduled publishing.
#
# Nothing else breaks: deploy.sh still runs by hand, and the published calendar
# simply stays at its last good version rather than being deleted.
#
# It touches only the label named in publish.conf. Everything else in
# ~/Library/LaunchAgents is left alone.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../publish-config.sh
. "$REPO/publish-config.sh"

LABEL="$LAUNCHD_LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_="$(id -u)"

if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_/$LABEL" \
        && printf '    \033[32mOK\033[0m   unloaded gui/%s/%s\n' "$UID_" "$LABEL"
else
    printf '         it was not loaded\n'
fi

if [ -f "$PLIST" ]; then
    rm -f "$PLIST" && printf '    \033[32mOK\033[0m   removed %s\n' "$PLIST"
else
    printf '         %s was not there\n' "$PLIST"
fi

printf '\nScheduled publishing is off. Logs under %s are untouched.\n' "$LOG_DIR"
printf 'deploy.sh still works by hand. To start again: "%s/launchd/install.sh"\n\n' "$REPO"
