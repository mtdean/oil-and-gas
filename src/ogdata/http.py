"""HTTP download with conditional requests, content hashing, retries, and a manifest.

Every raw file we hold is recorded in data/manifest.json keyed by URL, with the
ETag / Last-Modified the server gave us and the sha256 of the bytes. Re-fetching
is cheap: we send If-None-Match / If-Modified-Since, and even when a server
ignores those we compare hashes so downstream steps only re-run on real change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .paths import MANIFEST

log = logging.getLogger(__name__)

UA = "ogdata/0.1 (research data pipeline; python-requests)"
DEFAULT_TIMEOUT = (30, 180)  # connect, read


class Manifest:
    def __init__(self, path: Path = MANIFEST):
        self.path = path
        self._d: dict[str, dict] = {}
        if path.exists():
            try:
                self._d = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("manifest unreadable, starting fresh: %s", path)

    def get(self, url: str) -> dict | None:
        return self._d.get(url)

    def put(self, url: str, rec: dict) -> None:
        """Read-merge-write under an exclusive lock so concurrent ogdata processes don't lose entries."""
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        with lock.open("a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    try:
                        self._d = json.loads(self.path.read_text())
                    except json.JSONDecodeError:
                        pass
                self._d[url] = rec
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(self._d, indent=1, sort_keys=True))
                tmp.replace(self.path)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def save(self) -> None:
        pass


_manifest: Manifest | None = None


def manifest() -> Manifest:
    global _manifest
    if _manifest is None:
        _manifest = Manifest()
    return _manifest


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


@dataclass
class Fetched:
    path: Path
    changed: bool
    status: int | str  # HTTP status or 'cached'/'skipped'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(
    url: str,
    dest: Path,
    *,
    sess: requests.Session | None = None,
    force: bool = False,
    immutable: bool = False,
    max_age_hours: float | None = None,
    headers: dict | None = None,
    retries: int = 4,
    timeout=DEFAULT_TIMEOUT,
    allow_404: bool = False,
) -> Fetched:
    """Download url to dest unless unchanged.

    immutable: if dest exists, never re-request (historical files that don't change).
    max_age_hours: if we fetched within this window, don't re-request.
    allow_404: return Fetched(status=404, changed=False) instead of raising.
    """
    sess = sess or session()
    man = manifest()
    rec = man.get(url) or {}
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        if immutable:
            return Fetched(dest, False, "cached")
        if max_age_hours is not None and rec.get("fetched_at"):
            age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["fetched_at"])
            if age.total_seconds() < max_age_hours * 3600:
                return Fetched(dest, False, "cached")

    hdrs = dict(headers or {})
    if dest.exists() and not force:
        if rec.get("etag"):
            hdrs["If-None-Match"] = rec["etag"]
        if rec.get("last_modified"):
            hdrs["If-Modified-Since"] = rec["last_modified"]

    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with sess.get(url, headers=hdrs, stream=True, timeout=timeout, allow_redirects=True) as r:
                if r.status_code == 304:
                    man.put(url, {**rec, "checked_at": datetime.now(timezone.utc).isoformat()})
                    return Fetched(dest, False, 304)
                if r.status_code == 404 and allow_404:
                    return Fetched(dest, False, 404)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code} for {url}", response=r)
                r.raise_for_status()
                tmp = dest.with_name(dest.name + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
                new_hash = _sha256(tmp)
                changed = new_hash != rec.get("sha256") or not dest.exists()
                tmp.replace(dest)
                man.put(
                    url,
                    {
                        "path": str(dest),
                        "etag": r.headers.get("ETag"),
                        "last_modified": r.headers.get("Last-Modified"),
                        "content_type": r.headers.get("Content-Type"),
                        "sha256": new_hash,
                        "size": dest.stat().st_size,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "status": r.status_code,
                    },
                )
                log.info("%s %s (%s, %s)", "fetched" if changed else "unchanged", url, r.status_code, _fmt_size(dest.stat().st_size))
                return Fetched(dest, changed, r.status_code)
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt >= retries:
                raise
            log.warning("retry %d/%d for %s: %s", attempt + 1, retries, url, e)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def get_json(url: str, *, sess: requests.Session | None = None, params: dict | None = None, retries: int = 4, timeout=DEFAULT_TIMEOUT):
    sess = sess or session()
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} for {r.url}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt >= retries:
                raise
            log.warning("retry %d/%d for %s: %s", attempt + 1, retries, url, e)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def ftp_download(host: str, remote_path: str, dest: Path, *, force: bool = False, max_age_hours: float | None = None) -> Fetched:
    """Anonymous FTP download with size+MDTM change detection recorded in the manifest."""
    import ftplib

    man = manifest()
    url = f"ftp://{host}{remote_path}"
    rec = man.get(url) or {}
    if dest.exists() and not force and max_age_hours is not None and rec.get("fetched_at"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["fetched_at"])
        if age.total_seconds() < max_age_hours * 3600:
            return Fetched(dest, False, "cached")
    dest.parent.mkdir(parents=True, exist_ok=True)
    ftp = ftplib.FTP(host, timeout=120)
    ftp.login()
    try:
        d, name = remote_path.rsplit("/", 1)
        ftp.cwd(d)
        size = ftp.size(name)
        try:
            mdtm = ftp.voidcmd(f"MDTM {name}")[4:].strip()
        except ftplib.error_perm:
            mdtm = None
        if dest.exists() and not force and rec.get("size") == size and rec.get("mdtm") == mdtm:
            man.put(url, {**rec, "checked_at": datetime.now(timezone.utc).isoformat()})
            return Fetched(dest, False, "unchanged")
        tmp = dest.with_name(dest.name + ".part")
        log.info("ftp fetching %s (%s)", url, _fmt_size(size or 0))
        with tmp.open("wb") as f:
            ftp.retrbinary(f"RETR {name}", f.write, blocksize=1 << 20)
        tmp.replace(dest)
        man.put(url, {"path": str(dest), "size": size, "mdtm": mdtm, "fetched_at": datetime.now(timezone.utc).isoformat()})
        return Fetched(dest, True, 200)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
