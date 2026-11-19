#!/bin/zsh
# Installs the daily 02:30 update job for the current user (macOS launchd).
set -e
cd "$(dirname "$0")/.."
PLIST=~/Library/LaunchAgents/com.ogdata.update.plist
sed "s#/Users/td/oil-and-gas#$(pwd)#g" scripts/com.ogdata.update.plist > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed $PLIST — runs daily at 02:30. Test now with: launchctl start com.ogdata.update"
