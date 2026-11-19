#!/bin/zsh
# Incremental update of all active sources. Safe to run daily; sources skip unchanged files.
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/update-$(date +%Y%m%d-%H%M%S).log"
echo "== ogdata update start $(date)" | tee -a "$LOG"
.venv/bin/ogdata update "$@" >> "$LOG" 2>&1
RC=$?
.venv/bin/ogdata status >> "$LOG" 2>&1
echo "== done rc=$RC $(date)" | tee -a "$LOG"
# keep 60 days of logs
find logs -name 'update-*.log' -mtime +60 -delete
exit $RC
