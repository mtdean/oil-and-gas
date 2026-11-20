"""Pennsylvania DEP — well production extracts (monthly unconventional 2015+, semiannual 2010-2014, annual conventional 1980+).

Scripted via the Greenport ReportExtracts form: GET the form for an anti-forgery
token + the list of reporting periods, POST a period id, get CSV.
The server often answers 400. We deliberately go easy on it: one attempt per period
per run, 5 s between requests, at most 15 new periods per run, and we stop for the
run after 3 consecutive failures; missing periods are picked up by later daily runs.
Rows carry lat/lon, so wells are derived from production. period_months marks 1/6/12-month periods.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from .base import Frame, RunContext, Source

FORM = "https://greenport.pa.gov/ReportExtracts/OG/OilGasWellProdReport"
EXPORT = "https://greenport.pa.gov/ReportExtracts/OG/ExportOGProdReport"
INV_FORM = "https://greenport.pa.gov/ReportExtracts/OG/OilGasWellInventoryReport"
INV_EXPORT = "https://greenport.pa.gov/ReportExtracts/OG/WellIvventoryExport"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _parse_label(label: str):
    """-> (start_date, months) or None."""
    l = label.strip().lower()
    m = re.match(r"^([a-z]{3})\s*[-–]\s*([a-z]{3})\s+(\d{4})", l)  # 'Jan - Jun 2012', 'Jan - Dec 2015'
    if m:
        a, b, y = MONTHS[m.group(1)], MONTHS[m.group(2)], int(m.group(3))
        return date(y, a, 1), b - a + 1
    m = re.match(r"^([a-z]{3})\s+(\d{4})\s*[-–]\s*([a-z]{3})\s+(\d{4})", l)  # 'Jul 2009 - Jun 2010'
    if m:
        a, y1, b, y2 = MONTHS[m.group(1)], int(m.group(2)), MONTHS[m.group(3)], int(m.group(4))
        return date(y1, a, 1), (y2 - y1) * 12 + b - a + 1
    m = re.match(r"^([a-z]{3})\s+(\d{4})", l)  # 'Jan 2016'
    if m:
        return date(int(m.group(2)), MONTHS[m.group(1)], 1), 1
    return None


class Pennsylvania(Source):
    name = "pa"
    title = "Pennsylvania DEP well production (monthly unconventional, annual conventional)"
    state = "PA"
    url = "https://www.pa.gov/agencies/dep/data-and-tools/reports/oil-and-gas-reports.html"
    tables = ("production_monthly", "wells")
    cadence = "monthly (unconventional); annual (conventional)"
    granularity = "well"
    history = "1980 (annual), 2010 (semiannual), 2015-01 (monthly)"
    has_latlon = True
    notes = ("Greenport export often answers 400. Low-touch: 1 attempt per period, 5 s apart, <=15 new periods per run, "
             "back off for the run after 3 consecutive failures; missing periods fill in over later daily runs.")
    MAX_NEW_PERIODS = 15      # new/unsettled periods requested per run
    MAX_CONSECUTIVE_FAIL = 3  # stop requesting periods for this run after this many failures in a row
    PAUSE_S = 5               # between period requests
    INVENTORY_DAYS = 14

    def _token(self, ctx, url):
        for attempt in range(5):
            r = ctx.sess.get(url, timeout=60)
            if r.ok:
                m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text) or re.search(r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', r.text)
                if m:
                    return m.group(1), r.text
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"no anti-forgery token from {url}")

    def _post(self, ctx, form_url, export_url, data: list[tuple[str, str]], dest: Path) -> bool | None:
        """Returns True if dest changed, False if unchanged, None if the server refused.

        One attempt only. Greenport is load-balanced and its anti-forgery check fails when
        the cookie and token come from different backends, so the GET (cookie + token) and
        the POST use one fresh session. A refusal is left for the next daily run.
        """
        import requests
        from ..http import UA
        s = requests.Session()
        s.headers["User-Agent"] = UA
        try:
            g = s.get(form_url, timeout=60)
            m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', g.text) or re.search(r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', g.text)
            if not m:
                raise RuntimeError("no token")
            r = s.post(export_url, data=data + [("__RequestVerificationToken", m.group(1))], headers={"Referer": form_url}, timeout=900)
            status = r.status_code
            if r.status_code == 200 and r.content[:1] != b"<":
                old = dest.read_bytes() if dest.exists() else None
                dest.write_bytes(r.content)
                return old != r.content
        except (requests.RequestException, RuntimeError) as e:
            status = str(e)
        self.log.warning("PA export %s -> %s; skipped this run, will retry next update", dest.name, status)
        return None

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        _, html = self._token(ctx, FORM)
        sel = re.search(r'<select[^>]*id="RptPeriodsid".*?</select>', html, re.S)
        if not sel:
            raise RuntimeError("period select not found")
        opts = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>', sel.group(0))
        periods = []
        for pid, label in opts:
            parsed = _parse_label(label)
            if parsed:
                periods.append((int(pid), label.strip(), parsed[0], parsed[1]))
        (self.raw / "periods.json").write_text(json.dumps([{"id": p, "label": l, "start": s.isoformat(), "months": m} for p, l, s, m in periods], indent=1))
        today = date.today()
        requested = fails = 0
        for pid, label, start, months in periods:
            if ctx.limit_years and start.year < today.year - ctx.limit_years:
                continue
            dest = self.raw / f"period_{pid}.csv"
            end_age_months = (today.year - start.year) * 12 + today.month - start.month - months
            if dest.exists() and not ctx.force and end_age_months > 24:
                continue  # settled history
            if requested >= self.MAX_NEW_PERIODS:
                self.log.info("PA: reached %d period requests this run; remaining periods wait for the next daily run", self.MAX_NEW_PERIODS)
                break
            if requested:
                time.sleep(self.PAUSE_S)
            requested += 1
            got = self._post(ctx, FORM, EXPORT, [("RptPeriodsid", str(pid))], dest)
            if got is None:
                fails += 1
                if fails >= self.MAX_CONSECUTIVE_FAIL:
                    self.log.warning("PA: %d consecutive refusals; backing off until the next daily run", fails)
                    break
                continue
            fails = 0
            if got:
                changed.append(dest)
        for flag in ("Y", "N"):
            dest = self.raw / f"inventory_{flag}.csv"
            if ctx.force or not dest.exists() or (time.time() - dest.stat().st_mtime) > self.INVENTORY_DAYS * 86400:
                time.sleep(self.PAUSE_S)
                if self._post(ctx, INV_FORM, INV_EXPORT, [("Operator", "-1"), ("Unconvid", flag)], dest):
                    changed.append(dest)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        periods = {p["id"]: p for p in json.loads((self.raw / "periods.json").read_text())}
        files = sorted(self.raw.glob("period_*.csv")) if changed is None else [p for p in changed if p.name.startswith("period_")]
        for p in files:
            pid = int(p.stem.split("_")[1])
            meta = periods.get(pid)
            if not meta:
                continue
            df = self._prod(p, meta)
            if not df.empty:
                yield "production_monthly", p.stem, df
        yield "wells", "data", self._wells()

    @staticmethod
    def _api(permit: pd.Series) -> pd.Series:
        d = permit.astype("string").str.replace(r"\D", "", regex=True)
        return ("37" + d.str.zfill(8)).where(d.str.len().between(6, 8))

    def _prod(self, path: Path, meta: dict) -> pd.DataFrame:
        raw = pd.read_csv(path, dtype=str, low_memory=False, encoding_errors="replace")
        raw.columns = [c.strip().upper() for c in raw.columns]
        if "PERMIT_NUM" not in raw.columns:
            return pd.DataFrame()
        api10 = self._api(raw["PERMIT_NUM"])
        start = pd.to_datetime(raw.get("PRODUCTION_PERIOD_START_DATE"), errors="coerce", format="mixed")
        period = start.dt.to_period("M").dt.to_timestamp().fillna(pd.Timestamp(meta["start"]))
        oil = pd.to_numeric(raw.get("OIL_QUANTITY"), errors="coerce").fillna(0) + pd.to_numeric(raw.get("CONDENSATE_QUANTITY"), errors="coerce").fillna(0)
        df = pd.DataFrame({
            "entity_id": api10, "entity_type": "well", "api14": api10 + "0000", "api10": api10, "state": "PA",
            "period": period, "period_months": meta["months"],
            "oil_bbl": oil, "gas_mcf": pd.to_numeric(raw.get("GAS_QUANTITY"), errors="coerce"),
            "days_on": pd.to_numeric(raw.get("GAS_OPERATING_DAYS"), errors="coerce").fillna(pd.to_numeric(raw.get("OIL_OPERATING_DAYS"), errors="coerce")),
            "operator": raw.get("CLIENT"), "well_name": (raw.get("FARM").fillna("") + " " + raw.get("WELL_NO").fillna("")).str.strip(),
            "county": raw.get("COUNTY"),
            "extra": [json.dumps({"lat": a, "lon": b, "unconventional": u, "well_status": s, "well_type": t, "config": c, "spud_date": sd, "period_label": meta["label"]})
                      for a, b, u, s, t, c, sd in zip(pd.to_numeric(raw.get("LATITUDE_DECIMAL"), errors="coerce").astype(object).where(lambda x: x.notna(), None),
                                                      pd.to_numeric(raw.get("LONGITUDE_DECIMAL"), errors="coerce").astype(object).where(lambda x: x.notna(), None),
                                                      raw.get("UNCONVENTIONAL_IND"), raw.get("WELL_STATUS"), raw.get("WELL_CODE_DESC"), raw.get("CONFIG_CODE"), raw.get("SPUD_DATE"))],
        })
        return df[df.api10.notna()].drop_duplicates(["entity_id", "period"], keep="last")

    def _wells(self) -> pd.DataFrame:
        frames = []
        for flag in ("Y", "N"):
            p = self.raw / f"inventory_{flag}.csv"
            if p.exists():
                frames.append(pd.read_csv(p, dtype=str, low_memory=False, encoding_errors="replace"))
        if not frames:
            return pd.DataFrame()
        w = pd.concat(frames, ignore_index=True)
        w.columns = [c.strip().upper() for c in w.columns]
        api10 = self._api(w["API"])
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "PA",
            "well_name": w.get("FARM"), "operator": w.get("OPERATOR"), "county": w.get("COUNTY"),
            "lat": pd.to_numeric(w.get("LATITUDE_DECIMAL"), errors="coerce"), "lon": pd.to_numeric(w.get("LONGITUDE_DECIMAL"), errors="coerce"),
            "crs_note": "decimal degrees as published by PA DEP", "well_type": w.get("WELL_TYPE"), "status": w.get("WELL_STATUS"),
            "trajectory": w.get("CONFIGURATION"), "spud_date": pd.to_datetime(w.get("SPUD_DATE"), errors="coerce", format="mixed"),
            "extra": [json.dumps({"unconventional": u, "well_pad_id": wp, "municipality": m, "ogo": o}) for u, wp, m, o in zip(w.get("UNCONVENTIONAL"), w.get("WELL_PAD_ID"), w.get("MUNICIPALITY"), w.get("OGO"))],
        }).dropna(subset=["well_id"]).drop_duplicates("well_id", keep="last")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
