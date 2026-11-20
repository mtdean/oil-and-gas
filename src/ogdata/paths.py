"""Filesystem layout and environment loading."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("OGDATA_ROOT", Path(__file__).resolve().parents[2]))


# Keys ogdata understands. Only these are imported from a shared .env (see load_shared_dotenv),
# so unrelated secrets in another project's file never land in this process.
KNOWN_KEYS = ("EIA_API_KEY", "FRED_API_KEY", "OGDATA_DATA")


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides existing env vars."""
    for k, v in _parse_dotenv(path or ROOT / ".env").items():
        os.environ.setdefault(k, v)


def load_shared_dotenv(path: Path | None = None) -> None:
    """Fallback for keys missing from ./.env: the sibling situation-monitor repo's .env
    (override with OGDATA_SHARED_ENV). Only KNOWN_KEYS are imported, and never overridden."""
    path = path or Path(os.environ.get("OGDATA_SHARED_ENV", ROOT.parent / "situation-monitor" / ".env"))
    for k, v in _parse_dotenv(path).items():
        if k in KNOWN_KEYS and v:
            os.environ.setdefault(k, v)


load_dotenv()
load_shared_dotenv()


def raise_fd_limit(target: int = 65536) -> None:
    """launchd/cron shells start with a 256 open-file soft limit; DuckDB opens every parquet file
    behind a view at once, so a nightly run can die with 'Too many open files'. Raise to the hard cap."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass


raise_fd_limit()

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
