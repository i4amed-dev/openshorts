#!/bin/bash
# Keep a dedicated Klippo Mac from sleeping while Autopilot runs.
#
# This is a SECOND line of defence. The primary one is:
#   System Settings → Battery → Options →
#     "Prevent automatic sleeping on power adapter when display is off" = ON
#
# Read ops/macos/README.md before relying on this. In particular: caffeinate
# does NOT keep the machine awake with the lid physically closed. Clamshell
# sleep needs power + an external display + external input to be suppressed,
# and macOS exposes no supported override. If you cannot run clamshell, leave
# the lid open and set a short display timeout instead.
#
# Usage:
#   ops/macos/keepawake.sh              # foreground, Ctrl-C to stop
#   nohup ops/macos/keepawake.sh &      # background for this login session
#
# To run it at login, wrap it in a user LaunchAgent the same way
# com.klippo.autopilot.plist does. Do not run it as root: it does not need
# elevated privileges, and a root assertion is harder to notice and clear.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "keepawake.sh is macOS-only (this is $(uname -s))." >&2
    exit 1
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "Refusing to run as root — caffeinate needs no privileges here." >&2
    exit 1
fi

# Warn rather than silently doing nothing useful: on battery, macOS overrides
# these assertions regardless.
if command -v pmset >/dev/null 2>&1; then
    if pmset -g batt 2>/dev/null | grep -q "Battery Power"; then
        echo "⚠️  This Mac is on BATTERY. Autopilot needs the power adapter —" >&2
        echo "    macOS will sleep and throttle no matter what this script asserts." >&2
    fi
fi

echo "☕ Holding the system awake (display may still sleep — that is fine)."
echo "   Assertions: -d display  -i idle  -m disk  -s system  -u user"
echo "   Stop with Ctrl-C, or: pkill -f 'caffeinate -dimsu'"

# -u without a timeout would expire after the default 5 seconds; -t keeps the
# user assertion alive for the life of the process (~1 year).
exec caffeinate -dimsu -t 31536000
