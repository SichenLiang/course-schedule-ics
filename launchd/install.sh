#!/usr/bin/env bash
#
# install.sh -- install (or reinstall) the LaunchAgent that runs deploy.sh on a
# schedule. macOS only.
#
# Idempotent: running it again is harmless. It boots out its own label and
# bootstraps it back, and touches nothing else in ~/Library/LaunchAgents.
#
# The label, the hours and the PATH all come from publish.conf; nothing about
# this machine is baked into the repository. See uninstall.sh to stop it.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../publish-config.sh
. "$REPO/publish-config.sh"

LABEL="$LAUNCHD_LABEL"
AGENTS="$HOME/Library/LaunchAgents"
TEMPLATE="$REPO/launchd/agent.plist.template"
GENERATED="$REPO/launchd/$LABEL.plist"        # gitignored; a build artifact
PLIST_LIVE="$AGENTS/$LABEL.plist"
WRAPPER="$REPO/launchd/run.sh"
UID_="$(id -u)"

ok()   { printf '    \033[32mOK\033[0m   %s\n' "$1"; }
warn() { printf '    \033[33mnote\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
die()  { printf '\n\033[31minstall aborted:\033[0m %s\n' "$1" >&2; exit 1; }

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

# ------------------------------------------------------------ 0. preconditions

step "0/4  preconditions"

[ "$(uname -s)" = "Darwin" ] || die "launchd is macOS only. On Linux use a
    systemd timer or cron; the README has a cron line."
[ "$CSI_CONF_LOADED" = yes ] || die "no settings file at $PUBLISH_CONF.
    Copy publish.example.conf to publish.conf and edit it first."
[ -f "$TEMPLATE" ] || die "template missing: $TEMPLATE"
[ -f "$WRAPPER" ]  || die "wrapper missing: $WRAPPER"
[ -x "$REPO/deploy.sh" ] || die "$REPO/deploy.sh is missing or not executable"

case "$LABEL" in
    *[!A-Za-z0-9._-]*) die "LAUNCHD_LABEL contains a character launchd will not
    accept: $LABEL. Use letters, digits, dots, dashes and underscores." ;;
esac

chmod +x "$WRAPPER" "$REPO/launchd/status.sh" 2>/dev/null || true
ok "deploy.sh, run.sh and status.sh are in place"

mkdir -p "$LOG_DIR"
ok "log directory ready: $LOG_DIR"

for tool in git python3 curl; do
    command -v "$tool" >/dev/null 2>&1 \
        || warn "$tool is not on your PATH right now; the job will look for it in $LAUNCHD_PATH"
done

# ------------------------------------------------------------- 1. generate

step "1/4  generate the plist"

HOURS_XML=""
for h in $LAUNCHD_HOURS; do
    case "$h" in
        ''|*[!0-9]*) die "LAUNCHD_HOURS must be whole numbers 0-23; got '$h'" ;;
    esac
    [ "$h" -ge 0 ] && [ "$h" -le 23 ] || die "hour out of range 0-23: $h"
    HOURS_XML="$HOURS_XML        <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>0</integer></dict>
"
done
[ -n "$HOURS_XML" ] || die "LAUNCHD_HOURS is empty; the job would never run"

R="$(xml_escape "$REPO")"
P="$(xml_escape "$LAUNCHD_PATH")"
L="$(xml_escape "$LABEL")"

python3 - "$TEMPLATE" "$GENERATED" "$L" "$R" "$P" "$HOURS_XML" <<'PY'
import sys
template, out, label, repo, path, hours = sys.argv[1:7]
with open(template, encoding="utf-8") as fh:
    text = fh.read()
text = (text.replace("@LABEL@", label)
            .replace("@REPO@", repo)
            .replace("@PATH@", path)
            .replace("@HOURS@", hours.rstrip("\n")))
with open(out, "w", encoding="utf-8") as fh:
    fh.write(text)
PY

plutil -lint "$GENERATED" >/dev/null || die "the generated plist is not valid"
ok "$GENERATED"

# -------------------------------------------------------------- 2. install

step "2/4  install into ~/Library/LaunchAgents/"

mkdir -p "$AGENTS"
cp "$GENERATED" "$PLIST_LIVE"
ok "$PLIST_LIVE"

# ----------------------------------------------------------------- 3. load

step "3/4  load"

if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
    ok "unloaded the previous copy"
fi

launchctl bootstrap "gui/$UID_" "$PLIST_LIVE" || die "bootstrap failed"
ok "loaded gui/$UID_/$LABEL"

# --------------------------------------------------------------- 4. confirm

step "4/4  confirm"

launchctl print "gui/$UID_/$LABEL" \
    | grep -E 'state =|path =|runs =|last exit code|program =' \
    | sed 's/^[[:space:]]*/    /' || true

printf '\n\033[32mInstalled.\033[0m\n'
printf '  runs at    %s (on the hour)\n' "$LAUNCHD_HOURS"
printf '  log        %s (rotated at %s bytes)\n' "$LOG" "$LOG_MAX"
printf '  status     "%s/launchd/status.sh"\n' "$REPO"
printf '  run now    launchctl kickstart -k gui/%s/%s\n' "$UID_" "$LABEL"
printf '  stop       "%s/launchd/uninstall.sh"\n\n' "$REPO"
