#!/bin/zsh
# Installs the monthly update job (5th of the month, 02:30 local) for the current user (macOS launchd).
set -e
cd "$(dirname "$0")/.."
PLIST=~/Library/LaunchAgents/com.ogdata.update.plist
sed "s#/Users/td/oil-and-gas#$(pwd)#g" scripts/com.ogdata.update.plist > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed $PLIST — runs on the 5th of each month at 02:30 (a run missed while asleep fires on wake; one missed while powered off is skipped). Test now with: launchctl start com.ogdata.update"
