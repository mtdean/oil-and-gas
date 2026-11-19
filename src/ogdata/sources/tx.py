"""Texas Railroad Commission (RRC).

Two feeds, both free, both served from RRC's GoAnywhere MFT shares:

1. Production Data Query dump (PDQ_DSV.zip, ~3.5 GB, monthly on the last Saturday):
   OG_LEASE_CYCLE = monthly production per *lease* from 1993-01. Texas does not
   report oil production per well: oil is reported per lease (one lease = many
   wells). Gas "leases" are one well each, so gas rows are effectively well-level
   and we attach the API via OG_WELL_COMPLETION. OG_WELL_COMPLETION also gives the
   lease -> well (API) membership we store in entity_wells so lease volumes can
   be allocated to wells downstream.
   Column mapping: oil leases  -> oil_bbl = LEASE_OIL_PROD_VOL, gas_mcf = LEASE_CSGD_PROD_VOL (casinghead gas)
                   gas wells   -> gas_mcf = LEASE_GAS_PROD_VOL,  oil_bbl = LEASE_COND_PROD_VOL (condensate)
2. Digital Map Information well layers (one shapefile zip per county, twice weekly):
   surface locations with NAD83 lat/lon and an 8-digit API (county+unique) -> wells table.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd
import shapefile

from .. import storage
from ..mft import GoDrive
from .base import Frame, RunContext, Source

PDQ_LINK = "1f5ddb8d-329a-4459-b7f8-177b4f5ee60d"
WELLS_LINK = "d551fb20-442e-4b67-84fa-ac3f23ecabb4"
PDQ_MEMBERS = ("OG_LEASE_CYCLE.dsv", "OG_WELL_COMPLETION.dsv")

SYMNUM = {2: "Permitted Location", 3: "Dry Hole", 4: "Oil Well", 5: "Gas Well", 6: "Oil/Gas Well", 7: "Plugged Oil Well",
          8: "Plugged Gas Well", 9: "Canceled Location", 10: "Plugged Oil/Gas Well", 11: "Injection/Disposal Well",
          12: "Core Test", 16: "Sulfur Core Test", 17: "Storage from Oil", 18: "Storage from Gas", 19: "Shut-In Well (Oil)",
          20: "Shut-In Well (Gas)", 21: "Injection/Disposal from Oil", 22: "Injection/Disposal from Gas",
          23: "Injection/Disposal from Oil/Gas", 36: "Geothermal Well", 73: "Brine Mining Well", 74: "Water Supply Well",
          75: "Water Supply from Oil", 76: "Water Supply from Gas", 77: "Water Supply from Oil/Gas", 78: "Observation Well",
          79: "Observation from Oil", 80: "Observation from Gas", 81: "Observation from Oil/Gas"}


class Texas(Source):
    name = "tx"
    title = "Texas RRC lease production (PDQ dump) + well locations"
    state = "TX"
    url = "https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/"
    tables = ("production_monthly", "entity_wells", "wells")
    cadence = "production monthly (last Saturday); locations twice weekly"
    granularity = "lease (oil) / well (gas)"
    history = "1993-01"
    has_latlon = True
    notes = "Oil is lease-level only; use entity_wells to allocate. PDQ zip is 3.5 GB."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed: list[Path] = []
        # county well shapefiles
        shp = GoDrive(WELLS_LINK, ctx.sess)
        for f in shp.listing():
            if not f.name.lower().startswith("well") or not f.name.lower().endswith(".zip"):
                continue
            dest = self.raw / "shp" / f.name
            if shp.download(f, dest, force=ctx.force):
                changed.append(dest)
        # PDQ dump
        pdq = GoDrive(PDQ_LINK, ctx.sess)
        for f in pdq.listing():
            if f.name == "PDQ_DSV.zip":
                dest = self.raw / f.name
                if pdq.download(f, dest, force=ctx.force):
                    changed.append(dest)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        do_wells = changed is None or any(p.parent.name == "shp" for p in changed)
        do_pdq = changed is None or any(p.name == "PDQ_DSV.zip" for p in changed)
        if do_wells:
            yield "wells", "data", self._wells()
        if do_pdq and (self.raw / "PDQ_DSV.zip").exists():
            yield from self._pdq(ctx)

    # ---- well locations -----------------------------------------------------
    def _wells(self) -> pd.DataFrame:
        frames = []
        for zp in sorted((self.raw / "shp").glob("well*.zip")):
            fips = zp.stem[4:]
            with zipfile.ZipFile(zp) as z:
                names = {n.lower(): n for n in z.namelist()}
                base = f"well{fips}s"
                if f"{base}.dbf" not in names:
                    continue
                r = shapefile.Reader(shp=io.BytesIO(z.read(names[f"{base}.shp"])), dbf=io.BytesIO(z.read(names[f"{base}.dbf"])),
                                     shx=io.BytesIO(z.read(names[f"{base}.shx"])) if f"{base}.shx" in names else None)
                fields = [f[0] for f in r.fields[1:]]
                recs = pd.DataFrame([list(rec) for rec in r.iterRecords()], columns=fields)
            if recs.empty:
                continue
            recs["fips"] = fips
            frames.append(recs)
        df = pd.concat(frames, ignore_index=True)
        api8 = df["API"].astype("string").str.strip().str.replace(r"\D", "", regex=True)
        api8 = api8.where(api8.str.len() == 8)
        sym = pd.to_numeric(df["SYMNUM"], errors="coerce")
        out = pd.DataFrame({
            "well_id": ("42" + api8).where(api8.notna(), "SURF" + df["SURFACE_ID"].astype(str)),
            "api10": "42" + api8, "api14": "42" + api8 + "0000", "state": "TX",
            "well_number": df["WELLID"].astype("string").str.strip(),
            "lat": pd.to_numeric(df["LAT83"], errors="coerce"), "lon": pd.to_numeric(df["LONG83"], errors="coerce"),
            "crs_note": "NAD83 (LAT83/LONG83 from RRC Digital Map Information)",
            "status": sym.map(SYMNUM),
            "well_type": sym.map(lambda s: "OIL" if s in (4, 7, 19) else "GAS" if s in (5, 8, 20) else "OIL/GAS" if s in (6, 10) else None),
            "extra": [json.dumps({"symnum": None if pd.isna(s) else int(s), "reliab": rb, "surface_id": sid, "rrc_county_fips": fp})
                      for s, rb, sid, fp in zip(sym, df["RELIAB"], df["SURFACE_ID"], df["fips"])],
        })
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out

    # ---- PDQ production -----------------------------------------------------
    def _pdq(self, ctx: RunContext) -> Iterator[Frame]:
        work = self.raw / "pdq_work"
        work.mkdir(exist_ok=True)
        zp = self.raw / "PDQ_DSV.zip"
        with zipfile.ZipFile(zp) as z:
            # members are named like OG_LEASE_CYCLE_DATA_TABLE.dsv; match on the table-name prefix
            members = {}
            for n in z.namelist():
                base = n.split("/")[-1]
                for m in PDQ_MEMBERS:
                    if base.upper().startswith(m[:-4].upper()) and base.lower().endswith(".dsv") and not base.upper().startswith("OG_LEASE_CYCLE_DISP"):
                        members[m] = n
            for m in PDQ_MEMBERS:
                if m not in members:
                    raise RuntimeError(f"{m} missing from PDQ zip; members: {z.namelist()[:30]}")
                target = work / m
                if not target.exists() or target.stat().st_size != z.getinfo(members[m]).file_size:
                    self.log.info("extracting %s (%.1f GB)", m, z.getinfo(members[m]).file_size / 1e9)
                    with z.open(members[m]) as src, target.open("wb") as dst:
                        while chunk := src.read(1 << 24):
                            dst.write(chunk)
        dbp = work / "pdq.duckdb"
        if dbp.exists():
            dbp.unlink()
        con = duckdb.connect(str(dbp))
        con.execute("SET memory_limit='8GB'; SET preserve_insertion_order=false;")
        con.execute(f"SET temp_directory='{work / 'tmp'}'")
        opts = "delim='}', header=true, quote='', escape='', all_varchar=true, ignore_errors=true, sample_size=-1"
        con.execute(f"CREATE TABLE wc AS SELECT * FROM read_csv('{work / 'OG_WELL_COMPLETION.dsv'}', {opts})")
        con.execute(f"CREATE TABLE lc AS SELECT * FROM read_csv('{work / 'OG_LEASE_CYCLE.dsv'}', {opts})")
        n = con.execute("SELECT count(*) FROM lc").fetchone()[0]
        self.log.info("PDQ lease cycles: %s rows", f"{n:,}")

        con.execute("""
            CREATE TABLE wc2 AS
            SELECT trim(OIL_GAS_CODE) AS og, lpad(trim(DISTRICT_NO),2,'0') AS dist, trim(LEASE_NO) AS lease_no, trim(WELL_NO) AS well_no,
                   '42' || lpad(trim(API_COUNTY_CODE),3,'0') || lpad(trim(API_UNIQUE_NO),5,'0') AS api10,
                   COUNTY_NAME, WELL_ROOT_NO, WELLBORE_SHUTIN_DT, WELL_SHUTIN_DT, WELL_14B2_STATUS_CODE, WELL_SUBJECT_14B2_FLAG, WELLBORE_LOCATION_CODE
            FROM wc WHERE API_UNIQUE_NO IS NOT NULL AND trim(API_UNIQUE_NO) <> ''
        """)
        # entity -> wells mapping
        yield_frames = []
        storage.write_sql("entity_wells", self.name, con, """
            SELECT og || '-' || dist || '-' || lease_no AS entity_id,
                   CASE WHEN og = 'G' THEN 'well' ELSE 'lease' END AS entity_type,
                   api10 || '0000' AS api14, api10, 'TX' AS state, well_no AS well_number,
                   json_object('county', COUNTY_NAME, 'well_root_no', WELL_ROOT_NO, 'wellbore_shutin_dt', WELLBORE_SHUTIN_DT,
                               'well_shutin_dt', WELL_SHUTIN_DT, '14b2_status', WELL_14B2_STATUS_CODE,
                               'wellbore_location_code', WELLBORE_LOCATION_CODE)::VARCHAR AS extra
            FROM wc2
        """, "pdq")
        self.log.info("entity_wells written")
        # production per year
        con.execute("""
            CREATE TABLE prod AS
            SELECT trim(l.OIL_GAS_CODE) AS og, lpad(trim(l.DISTRICT_NO),2,'0') AS dist, trim(l.LEASE_NO) AS lease_no,
                   trim(l.CYCLE_YEAR) AS yr, trim(l.CYCLE_MONTH) AS mo, trim(l.GAS_WELL_NO) AS gas_well_no,
                   TRY_CAST(l.LEASE_OIL_PROD_VOL AS DOUBLE) AS oil, TRY_CAST(l.LEASE_GAS_PROD_VOL AS DOUBLE) AS gas,
                   TRY_CAST(l.LEASE_COND_PROD_VOL AS DOUBLE) AS cond, TRY_CAST(l.LEASE_CSGD_PROD_VOL AS DOUBLE) AS csgd,
                   TRY_CAST(l.LEASE_OIL_TOT_DISP AS DOUBLE) AS oil_disp, TRY_CAST(l.LEASE_GAS_TOT_DISP AS DOUBLE) AS gas_disp,
                   TRY_CAST(l.LEASE_COND_TOT_DISP AS DOUBLE) AS cond_disp, TRY_CAST(l.LEASE_CSGD_TOT_DISP AS DOUBLE) AS csgd_disp,
                   l.OPERATOR_NAME, l.LEASE_NAME, l.FIELD_NAME, trim(l.FIELD_NO) AS field_no, trim(l.OPERATOR_NO) AS operator_no,
                   w.api10
            FROM lc l
            LEFT JOIN wc2 w ON trim(l.OIL_GAS_CODE) = 'G' AND w.og = 'G' AND w.dist = lpad(trim(l.DISTRICT_NO),2,'0')
                           AND w.lease_no = trim(l.LEASE_NO) AND w.well_no = trim(l.GAS_WELL_NO)
        """)
        years = [r[0] for r in con.execute("SELECT DISTINCT yr FROM prod WHERE yr ~ '^[0-9]{4}$' ORDER BY yr").fetchall()]
        for yr in years:
            storage.write_sql("production_monthly", self.name, con, f"""
                SELECT og || '-' || dist || '-' || lease_no AS entity_id,
                       CASE WHEN og = 'G' THEN 'well' ELSE 'lease' END AS entity_type,
                       api10 || '0000' AS api14, api10, 'TX' AS state,
                       make_timestamp(CAST(yr AS INT), CAST(mo AS INT), 1, 0, 0, 0) AS period,
                       CASE WHEN og = 'O' THEN oil ELSE cond END AS oil_bbl,
                       CASE WHEN og = 'O' THEN csgd ELSE gas END AS gas_mcf,
                       CASE WHEN og = 'O' THEN oil_disp ELSE cond_disp END AS oil_sold_bbl,
                       CASE WHEN og = 'O' THEN csgd_disp ELSE gas_disp END AS gas_sold_mcf,
                       OPERATOR_NAME AS operator, LEASE_NAME AS well_name, FIELD_NAME AS field,
                       json_object('oil_gas_code', og, 'district', dist, 'lease_no', lease_no, 'gas_well_no', gas_well_no,
                                   'field_no', field_no, 'operator_no', operator_no)::VARCHAR AS extra
                FROM prod WHERE yr = '{yr}' AND mo ~ '^[0-9]{{1,2}}$'
            """, yr)
        con.close()
        dbp.unlink(missing_ok=True)
        for m in PDQ_MEMBERS:  # reclaim ~25 GB; the zip stays
            (work / m).unlink(missing_ok=True)
        return
        yield  # pragma: no cover
