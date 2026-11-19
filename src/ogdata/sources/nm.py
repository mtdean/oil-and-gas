"""New Mexico OCD.

Production: anonymous FTP ftp.emnrd.nm.gov /Public/OCD/OCD Interface v1.1/volumes/wcproduction/wcproduction.zip
(~1 GB zip -> >4 GB UTF-16 SQL-rowset XML). One <wcproduction> element per
well x pool x month x product (O/G/W/...) from 1973. We stream-parse it to a CSV,
then pivot with DuckDB into one row per well-pool-month and write yearly partitions.
Wells: ArcGIS Hub CSV export of "OCD Oil and Gas Wells" (142k rows, NAD83 lat/lon).
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

import duckdb
import pandas as pd

from .. import storage
from ..http import download, ftp_download
from .base import Frame, RunContext, Source

FTP_HOST = "ftp.emnrd.nm.gov"
FTP_PROD = "/Public/OCD/OCD Interface v1.1/volumes/wcproduction/wcproduction.zip"
WELLS_CSV = "https://ocd-hub-nm-emnrd.hub.arcgis.com/api/download/v1/items/387f397ebf164c7aa6a752aac7d22b17/csv?layers=0"
FIELDS = ["api_st_cde", "api_cnty_cde", "api_well_idn", "pool_idn", "prodn_mth", "prodn_yr", "ogrid_cde", "prd_knd_cde",
          "eff_dte", "amend_ind", "c115_wc_stat_cde", "prod_amt", "prodn_day_num", "mod_dte"]


class NewMexico(Source):
    name = "nm"
    title = "New Mexico OCD C-115 monthly production by well/pool + well header"
    state = "NM"
    url = "https://www.emnrd.nm.gov/ocd/ocd-data/"
    tables = ("production_monthly", "wells")
    cadence = "FTP refreshed at least monthly (observed nightly); wells nightly"
    granularity = "well_formation"
    history = "1973"
    has_latlon = True
    notes = "Production zip ~1 GB; XML parse takes a while."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        # ~1 GB download + long XML parse: re-check weekly (OCD volumes are reported monthly anyway)
        f = ftp_download(FTP_HOST, FTP_PROD, self.raw / "wcproduction.zip", force=ctx.force, max_age_hours=24 * 6)
        if f.changed:
            changed.append(f.path)
        f = download(WELLS_CSV, self.raw / "wells.csv", sess=ctx.sess, force=ctx.force, max_age_hours=20)
        if f.changed:
            changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        if changed is None or any(p.name == "wcproduction.zip" for p in changed):
            yield from self._production()
        if changed is None or any(p.name == "wells.csv" for p in changed):
            yield "wells", "data", self._wells()

    def _xml_to_csv(self, csv_path: Path) -> None:
        zp = self.raw / "wcproduction.zip"
        with zipfile.ZipFile(zp) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            self.log.info("streaming %s (%.1f GB uncompressed) to csv", name, z.getinfo(name).file_size / 1e9)
            with z.open(name) as fh, csv_path.open("w", newline="") as out:
                w = csv.writer(out)
                w.writerow(FIELDS)
                n = 0
                for _, el in ET.iterparse(fh, events=("end",)):
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag != "wcproduction":
                        continue
                    rec = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in el}
                    w.writerow([rec.get(f, "") for f in FIELDS])
                    el.clear()
                    n += 1
                    if n % 2_000_000 == 0:
                        self.log.info("  %s rows", f"{n:,}")
        self.log.info("xml rows: %s", f"{n:,}")

    def _production(self) -> Iterator[Frame]:
        work = self.raw / "work"
        work.mkdir(exist_ok=True)
        csv_path = work / "wcproduction.csv"
        self._xml_to_csv(csv_path)
        con = duckdb.connect()
        con.execute("SET memory_limit='8GB'")
        con.execute(f"SET temp_directory='{work / 'tmp'}'")
        con.execute(f"""
            CREATE TABLE raw AS
            SELECT lpad(api_st_cde,2,'0') || lpad(api_cnty_cde,3,'0') || lpad(api_well_idn,5,'0') AS api10,
                   pool_idn, TRY_CAST(prodn_yr AS INT) AS yr, TRY_CAST(prodn_mth AS INT) AS mo, ogrid_cde, prd_knd_cde,
                   TRY_CAST(prod_amt AS DOUBLE) AS amt, TRY_CAST(prodn_day_num AS INT) AS days, c115_wc_stat_cde, amend_ind, mod_dte
            FROM read_csv('{csv_path}', header=true, all_varchar=true)
            WHERE TRY_CAST(prodn_yr AS INT) BETWEEN 1900 AND 2100 AND TRY_CAST(prodn_mth AS INT) BETWEEN 1 AND 12
        """)
        con.execute("""
            CREATE TABLE piv AS
            SELECT api10, pool_idn, yr, mo,
                   sum(amt) FILTER (WHERE prd_knd_cde = 'O') AS oil_bbl,
                   sum(amt) FILTER (WHERE prd_knd_cde = 'G') AS gas_mcf,
                   sum(amt) FILTER (WHERE prd_knd_cde = 'W') AS water_bbl,
                   max(days) FILTER (WHERE days BETWEEN 0 AND 31) AS days_on,
                   arg_max(ogrid_cde, mod_dte) AS ogrid, arg_max(c115_wc_stat_cde, mod_dte) AS stat,
                   string_agg(DISTINCT prd_knd_cde, ',') AS kinds
            FROM raw GROUP BY ALL
        """)
        years = [r[0] for r in con.execute("SELECT DISTINCT yr FROM piv ORDER BY yr").fetchall()]
        for yr in years:
            storage.write_sql("production_monthly", self.name, con, f"""
                SELECT api10 || ':' || pool_idn AS entity_id, 'well_formation' AS entity_type, api10 || '0000' AS api14, api10, 'NM' AS state,
                       make_timestamp(yr, mo, 1, 0, 0, 0) AS period, oil_bbl, gas_mcf, water_bbl, days_on,
                       ogrid AS operator, pool_idn AS formation,
                       json_object('ogrid', ogrid, 'wc_status', stat, 'product_kinds', kinds)::VARCHAR AS extra
                FROM piv WHERE yr = {yr}
            """, str(yr))
        con.close()
        csv_path.unlink(missing_ok=True)
        return
        yield

    def _wells(self) -> pd.DataFrame:
        w = pd.read_csv(self.raw / "wells.csv", dtype=str, low_memory=False)
        w.columns = [c.strip().lower() for c in w.columns]
        api10 = w["id"].astype("string").str.replace(r"\D", "", regex=True).str[:10]
        dt = lambda c: pd.to_datetime(w[c], errors="coerce").where(lambda s: s.dt.year < 9000) if c in w.columns else pd.NaT
        traj = w.get("directional_status", pd.Series(dtype=str)).astype("string").str.upper().str[:1]
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "NM",
            "well_name": w.get("name"), "operator": w.get("ogrid_name"), "county": w.get("county"),
            "lat": pd.to_numeric(w.get("latitude"), errors="coerce"), "lon": pd.to_numeric(w.get("longitude"), errors="coerce"),
            "crs_note": "NAD83 (EPSG:4269)", "well_type": w.get("type"), "status": w.get("status"), "trajectory": traj,
            "spud_date": dt("spud_date"), "last_prod_date": dt("last_production_date"),
            "td_ft": pd.to_numeric(w.get("measured_vertical_depth"), errors="coerce"),
            "extra": w[[c for c in ("ogrid", "district", "lease_type", "true_vertical_depth", "pool_id_list", "plug_date", "ulstr") if c in w.columns]].to_json(orient="records", lines=True).splitlines(),
        })
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
