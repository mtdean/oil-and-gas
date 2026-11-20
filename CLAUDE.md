# CLAUDE.md

Read `AGENTS.md` first: it is the handoff playbook (setup, full rebuild order and timings, sources that
must not be scraped, manual drop-ins, data semantics, verification queries, troubleshooting).

Non-negotiables, repeated here so they are never missed:

* Do not spoof browser headers or otherwise evade bot walls. A refused source gets one polite attempt and
  a documented manual drop-in (`AGENTS.md` §6).
* Do not loosen the Pennsylvania throttle; do not scrape Louisiana SONRIS or Oklahoma OCC for production.
* Kansas pre-1987 production is IHS-licensed: never redistribute it.
* Keys go in the git-ignored `.env`, never in code or commits.
* `data/` and `dashboards/` are git-ignored outputs; rebuild them with `./scripts/rebuild_all.sh`.

Everyday commands: `ogdata check`, `ogdata update [source...]`, `ogdata analytics`, `ogdata dashboard`,
`ogdata forecast --lat .. --lon ..`, `ogdata sql "..."`, `.venv/bin/python -m pytest -q`.
