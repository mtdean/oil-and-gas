"""Client for Texas RRC's GoAnywhere 'GoDrive' public shares (mft.rrc.texas.gov/link/<uuid>).

The share page is a JSF (PrimeFaces) table; downloading a file means posting the
row's command-link id with the page's ViewState, then following the redirect to
/link/godrivedownload with the same cookies. ViewState is per page load, so we
reload the listing before each download.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from .http import UA, manifest, _sha256, _fmt_size

log = logging.getLogger(__name__)
HOST = "https://mft.rrc.texas.gov"

ROW_RE = re.compile(
    r'<a id="(fileTable:(\d+):[A-Za-z0-9_]+)"[^>]*>([^<]+)</a></td>'
    r'<td role="gridcell" class="ModifiedOnColumn">([^<]*)</td>'
    r'<td role="gridcell" class="SizeColumn">([^<]*)</td>'
)
VS_RE = re.compile(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"')


@dataclass
class MftFile:
    row_id: str
    index: int
    name: str
    modified: datetime | None
    size_text: str


class GoDrive:
    def __init__(self, link: str, sess: requests.Session | None = None):
        self.link = link if link.startswith("http") else f"{HOST}/link/{link}"
        self.sess = sess or requests.Session()
        self.sess.headers.setdefault("User-Agent", UA)
        self._viewstate: str | None = None

    def listing(self) -> list[MftFile]:
        r = self.sess.get(self.link, timeout=(30, 120))
        r.raise_for_status()
        m = VS_RE.search(r.text)
        if not m:
            raise RuntimeError(f"no ViewState on {self.link}")
        self._viewstate = m.group(1)
        files = []
        for row_id, idx, name, mod, size in ROW_RE.findall(r.text):
            try:
                dt = datetime.strptime(mod.strip(), "%m/%d/%y %I:%M:%S %p")
            except ValueError:
                dt = None
            files.append(MftFile(row_id, int(idx), name.strip(), dt, size.strip()))
        return files

    def download(self, f: MftFile, dest: Path, *, force: bool = False) -> bool:
        """Download f to dest. Skips when the manifest shows the same server timestamp. Returns changed."""
        man = manifest()
        key = f"{self.link}#{f.name}"
        rec = man.get(key) or {}
        mod = f.modified.isoformat() if f.modified else None
        if dest.exists() and not force and mod and rec.get("modified") == mod:
            return False
        if self._viewstate is None:
            self.listing()
        dest.parent.mkdir(parents=True, exist_ok=True)
        form = {"fileList": "fileList", "fileList_SUBMIT": "1", f.row_id: f.row_id, "javax.faces.ViewState": self._viewstate}
        for attempt in range(4):
            try:
                r = self.sess.post(f"{HOST}/webclient/godrive/PublicGoDrive.xhtml", data=form, allow_redirects=False, timeout=(30, 120))
                if r.status_code not in (302, 303):
                    raise RuntimeError(f"expected redirect, got {r.status_code} for {f.name}")
                url = r.headers.get("Location", "/link/godrivedownload")
                if url.startswith("/"):
                    url = HOST + url
                with self.sess.get(url, stream=True, timeout=(30, 3600)) as dl:
                    dl.raise_for_status()
                    ctype = dl.headers.get("Content-Type", "")
                    if "text/html" in ctype:
                        raise RuntimeError(f"got HTML instead of file for {f.name} (session expired?)")
                    tmp = dest.with_name(dest.name + ".part")
                    with tmp.open("wb") as fh:
                        for chunk in dl.iter_content(1 << 22):
                            if chunk:
                                fh.write(chunk)
                    tmp.replace(dest)
                sha = _sha256(dest) if dest.stat().st_size < 2_000_000_000 else None
                changed = sha != rec.get("sha256") if sha else True
                man.put(key, {"path": str(dest), "modified": mod, "size": dest.stat().st_size, "sha256": sha,
                              "fetched_at": datetime.utcnow().isoformat()})
                log.info("mft %s %s (%s)", "fetched" if changed else "unchanged", f.name, _fmt_size(dest.stat().st_size))
                self._viewstate = None  # one download per page load
                return changed
            except (requests.RequestException, RuntimeError) as e:
                log.warning("mft retry %d for %s: %s", attempt + 1, f.name, e)
                time.sleep(3 * (attempt + 1))
                self.listing()
        raise RuntimeError(f"failed to download {f.name}")
