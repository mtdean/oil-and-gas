#!/bin/zsh
# Full rebuild from a fresh clone: downloads everything, builds the lake, analytics and dashboard.
# Safe to re-run: downloads are conditional and sources skip unchanged files, so a second run resumes.
# Usage: ./scripts/rebuild_all.sh [--skip-tx] [--no-analytics]
set -u
cd "$(dirname "$0")/.."
ulimit -n 65536 2>/dev/null || ulimit -n 10240 2>/dev/null
mkdir -p logs
LOG="logs/rebuild-$(date +%Y%m%d-%H%M%S).log"
SKIP_TX=0; ANALYTICS=1
for a in "$@"; do
  case "$a" in --skip-tx) SKIP_TX=1;; --no-analytics) ANALYTICS=0;; esac
done
if [[ ! -x .venv/bin/ogdata ]]; then
  echo "== creating venv" | tee -a "$LOG"
  python3 -m venv .venv && .venv/bin/pip install -q -e . || { echo "venv/install failed"; exit 1; }
fi
[[ -f .env ]] || { cp .env.example .env; echo "created .env from .env.example — add FRED_API_KEY (and EIA_API_KEY) for full macro coverage" | tee -a "$LOG"; }
run() { echo "== $(date '+%F %T') ogdata $*" | tee -a "$LOG"; .venv/bin/ogdata "$@" >> "$LOG" 2>&1 || echo "   (ogdata $* exited non-zero; see $LOG)" | tee -a "$LOG"; }

run check
# cheap and useful first
run update eia_bulk cftc dallasfed onrr fred camacro
# well-level states, small to large
run update nd co wy ut mt ks oh wv pa
run update bc sk ab
run update nm
[[ $SKIP_TX -eq 0 ]] && run update tx
# stubs / crosswalks / manual drop-ins (fail softly)
run update ok la ca ak bsee fracfocus bakerhughes
run build-db
run status
if [[ $ANALYTICS -eq 1 ]]; then
  run analytics
  run dashboard
fi
echo "== done $(date '+%F %T'); log: $LOG" | tee -a "$LOG"
echo "Verify with the checklist in AGENTS.md §8."
