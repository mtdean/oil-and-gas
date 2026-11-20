#!/bin/zsh
# Incremental update of all active sources, then rebuild analytics tables and the HTML monitor.
# Safe to run daily; sources skip unchanged files. Pass --no-analytics to skip the analytics rebuild.
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/update-$(date +%Y%m%d-%H%M%S).log"
ANALYTICS=1
ARGS=()
for a in "$@"; do
  if [[ "$a" == "--no-analytics" ]]; then ANALYTICS=0; else ARGS+=("$a"); fi
done
echo "== ogdata update start $(date)" | tee -a "$LOG"
.venv/bin/ogdata update "${ARGS[@]}" >> "$LOG" 2>&1
RC=$?
.venv/bin/ogdata status >> "$LOG" 2>&1
if [[ $ANALYTICS -eq 1 ]]; then
  echo "== analytics $(date)" | tee -a "$LOG"
  .venv/bin/ogdata analytics >> "$LOG" 2>&1 || { echo "analytics failed" | tee -a "$LOG"; RC=1; }
  .venv/bin/ogdata dashboard >> "$LOG" 2>&1 || { echo "dashboard failed" | tee -a "$LOG"; RC=1; }
fi
echo "== done rc=$RC $(date)" | tee -a "$LOG"
# keep 60 days of logs
find logs -name 'update-*.log' -mtime +60 -delete
exit $RC
