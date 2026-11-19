"""Source base class. Subclasses implement fetch() and transform()."""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

from .. import storage
from ..http import session
from ..paths import raw_dir

Frame = tuple[str, str, pd.DataFrame]  # (table, partition, df)


@dataclass
class RunContext:
    force: bool = False            # ignore caches; refetch + rebuild everything
    fetch_only: bool = False
    transform_only: bool = False
    limit_years: int | None = None  # for quick smoke tests: only the last N years of history
    sess: requests.Session = field(default_factory=session)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("ogdata"))


class Source:
    name: str = ""                 # short id, used in paths (lowercase)
    title: str = ""
    state: str | None = None       # 2-letter code for state sources
    url: str = ""                  # landing page for humans
    tables: tuple[str, ...] = ()
    cadence: str = ""              # how often the publisher updates
    granularity: str = ""          # well | lease | luw | pun | n/a
    history: str = ""              # earliest data
    has_latlon: bool = False
    requires_key: str | None = None  # env var name
    status: str = "active"         # active | planned | needs-key
    notes: str = ""

    def __init__(self):
        self.log = logging.getLogger(f"ogdata.{self.name}")

    @property
    def raw(self) -> Path:
        return raw_dir(self.name)

    # --- to implement -------------------------------------------------------
    def fetch(self, ctx: RunContext) -> list[Path]:
        """Download raw files. Return the list of files that changed (new or modified)."""
        raise NotImplementedError

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        """Yield (table, partition, DataFrame). `changed` is None for a full rebuild,
        otherwise the raw files that changed in this run; sources with per-file
        partitions can transform only those."""
        raise NotImplementedError

    # --- orchestration ------------------------------------------------------
    def has_output(self) -> bool:
        return any(storage.list_partitions(t, self.name) for t in self.tables)

    def run(self, ctx: RunContext) -> dict:
        t0 = time.time()
        info: dict = {"fetched": 0, "frames": 0, "rows": 0}
        try:
            changed: list[Path] | None = []
            if not ctx.transform_only:
                changed = self.fetch(ctx)
                info["fetched"] = len(changed)
            if ctx.fetch_only:
                status = "fetched"
            else:
                if ctx.force or ctx.transform_only or not self.has_output():
                    changed = None  # full rebuild
                if changed is not None and len(changed) == 0:
                    status = "unchanged"
                else:
                    for table, partition, df in self.transform(ctx, changed):
                        storage.write_table(table, self.name, df, partition)
                        info["frames"] += 1
                        info["rows"] += len(df)
                    status = "updated"
            info["seconds"] = round(time.time() - t0, 1)
            storage.log_run(self.name, status, **info)
            self.log.info("%s: %s %s", self.name, status, info)
            return {"source": self.name, "status": status, **info}
        except Exception as e:
            info["seconds"] = round(time.time() - t0, 1)
            info["error"] = f"{type(e).__name__}: {e}"
            storage.log_run(self.name, "error", **info)
            self.log.error("%s failed: %s\n%s", self.name, e, traceback.format_exc())
            return {"source": self.name, "status": "error", **info}

    def describe(self) -> dict:
        return {
            "name": self.name, "title": self.title, "state": self.state, "status": self.status,
            "tables": ",".join(self.tables), "granularity": self.granularity, "history": self.history,
            "latlon": self.has_latlon, "cadence": self.cadence, "key": self.requires_key or "", "url": self.url,
        }
