#!/usr/bin/env bash
#
# publish-config.sh -- shared settings loader for deploy.sh and launchd/*.
#
# Sourced, never executed. It defines REPO, loads publish.conf if there is one,
# and fills in a default for every setting. Values already set in the
# environment win over the file, so a one-off override works without editing
# anything:
#
#     MIN_EVENTS=1 ./deploy.sh
#
# Nothing here talks to the network or writes a file.

# The repository root: the directory holding this script.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PUBLISH_CONF="${PUBLISH_CONF:-$REPO/publish.conf}"

# Every setting the conf file may set. Listed explicitly so that a typo in the
# conf file stays a typo rather than silently becoming a new setting.
CSI_SETTINGS="CONFIG WORK_DIR PARSER_ARGS PUB_REPO PUB_BRANCH ALLOWED_FILES PUBLIC_URL
              MIN_EVENTS SHRINK_PCT VERIFY_TIMEOUT EXTRA_ALLOWED_HOSTS
              FORBIDDEN_PATTERNS LAUNCHD_LABEL LAUNCHD_HOURS LAUNCHD_PATH
              LOG_MAX GAP_HOURS STALE_HOURS FORCE"

# Remember what the caller put in the environment before the file overwrites it.
for _v in $CSI_SETTINGS; do
    eval "_csi_env_$_v=\${$_v-}"
done

if [ -f "$PUBLISH_CONF" ]; then
    # shellcheck disable=SC1090
    . "$PUBLISH_CONF"
    CSI_CONF_LOADED=yes
else
    CSI_CONF_LOADED=no
fi

# Put the environment back on top.
for _v in $CSI_SETTINGS; do
    eval "_csi_e=\${_csi_env_$_v}"
    [ -n "$_csi_e" ] && eval "$_v=\$_csi_e"
done
unset _v _csi_e

# Defaults. `:=` assigns only when unset or empty, so anything the conf file or
# the environment provided survives.
: "${CONFIG:=course-schedule.ini}"
: "${WORK_DIR:=live}"
: "${PARSER_ARGS:=}"
: "${PUB_REPO:=}"
: "${PUB_BRANCH:=main}"
: "${ALLOWED_FILES:=README.md schedule.ics}"
: "${PUBLIC_URL:=}"
: "${MIN_EVENTS:=10}"
: "${SHRINK_PCT:=25}"
: "${VERIFY_TIMEOUT:=180}"
: "${EXTRA_ALLOWED_HOSTS:=}"
: "${FORBIDDEN_PATTERNS:=}"
: "${LAUNCHD_LABEL:=local.course-schedule-ics.publish}"
: "${LAUNCHD_HOURS:=1 7 13 19}"
: "${LAUNCHD_PATH:=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin}"
: "${LOG_MAX:=1048576}"
: "${GAP_HOURS:=30}"
: "${STALE_HOURS:=26}"
: "${FORCE:=0}"

# Relative paths are relative to the repository, not to whatever directory the
# job happened to start in. launchd starts jobs in / unless told otherwise.
case "$CONFIG"   in /*) ;; *) CONFIG="$REPO/$CONFIG"     ;; esac
case "$WORK_DIR" in /*) ;; *) WORK_DIR="$REPO/$WORK_DIR" ;; esac

LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/publish.log"
LIVE_ICS="$WORK_DIR/schedule.ics"
