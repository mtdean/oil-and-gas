"""Filesystem layout and environment loading."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("OGDATA_ROOT", Path(__file__).resolve().parents[2]))


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides existing env vars."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

DATA = Path(os.environ.get("OGDATA_DATA", ROOT / "data"))
RAW = DATA / "raw"
PARQUET = DATA / "parquet"
DB = DATA / "warehouse.duckdb"
MANIFEST = DATA / "manifest.json"
RUNS = DATA / "runs.jsonl"
LOGS = ROOT / "logs"


def ensure_dirs() -> None:
    for p in (DATA, RAW, PARQUET, LOGS):
        p.mkdir(parents=True, exist_ok=True)


def raw_dir(source: str) -> Path:
    p = RAW / source
    p.mkdir(parents=True, exist_ok=True)
    return p
