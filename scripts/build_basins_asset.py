#!/usr/bin/env python3
"""Build src/ogdata/analytics/assets/basins.json: basin and play outlines for the dashboard map.

Inputs (public, downloaded once into a work directory; see docs/sources.md "Map layers"):
  EIA  TightOil_ShaleGas_Plays_Lower48_EIA.zip   -> plays  (Shale_play, Basin)   https://www.eia.gov/maps/map_data/
  EIA  SedimentaryBasins_US_EIA.zip             -> basins (NAME)
  BC   WHSE_MINERAL_TENURE.OG_UNCONVENTNL_PLAY_TRENDS_SP (GeoJSON from openmaps.gov.bc.ca WFS) -> plays
  AGS  DIG 2013-0024 Alberta shale/siltstone unit outlines (optional)             -> plays

Usage: python scripts/build_basins_asset.py <workdir>
Needs pyshp (installed with ogdata) and node/npx for the topojson CLIs.
"""
from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

import shapefile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "ogdata" / "analytics" / "assets" / "basins.json"
TITLE = {"TX-LA-MS SALT": "Gulf Coast Salt Basin", "UINTA-PICEANCE": "Uinta-Piceance", "CHEROKEE PLATFORM": "Cherokee Platform"}
KEEP_BASINS = {"PERMIAN", "WILLISTON", "ANADARKO", "APPALACHIAN", "DENVER", "POWDER RIVER", "WESTERN GULF", "FORT WORTH", "ARKOMA",
               "SAN JUAN", "UINTA-PICEANCE", "GREATER GREEN RIVER", "TX-LA-MS SALT", "SAN JOAQUIN BASIN", "MICHIGAN", "ILLINOIS", "PARADOX", "BIGHORN", "RATON BASIN", "ARDMORE", "BLACK WARRIOR", "MONTANA THRUST BELT", "NORTH PARK"}
# EIA play polygons worth labelling (the rest are minor / historical)
KEEP_PLAYS = {"Wolfcamp", "Spraberry", "Bone Spring", "Delaware", "Wolfcamp - Midland", "Bakken", "Three Forks", "Eagle Ford", "Marcellus", "Utica",
              "Haynesville-Bossier", "Niobrara", "Woodford", "Barnett", "Fayetteville", "Mancos", "Mowry", "Niobrara- Mowry", "Monterey", "Tuscaloosa",
              "Austin Chalk", "Cody", "Lewis", "Abo-Yeso", "Glorieta-Yeso", "Antrim", "New Albany", "Hermosa", "Heath"}


def shp_features(path: str, kind: str, name_fn, keep_fn=lambda rec: True, extra=None) -> list[dict]:
    r = shapefile.Reader(path)
    flds = [f[0] for f in r.fields[1:]]
    feats = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(flds, sr.record))
        if not keep_fn(rec):
            continue
        geom = sr.shape.__geo_interface__
        props = {"name": name_fn(rec), "kind": kind, "country": "US"}
        if extra:
            props.update(extra(rec))
        feats.append({"type": "Feature", "properties": props, "geometry": geom})
    return feats


def main(work: Path) -> None:
    basins, plays = [], []
    for shp in glob.glob(str(work / "sedbasins" / "**" / "*.shp"), recursive=True):
        basins += shp_features(shp, "basin", lambda r: TITLE.get(r["NAME"], r["NAME"].title().replace(" Basin", "")), lambda r: r["NAME"] in KEEP_BASINS)
    for shp in glob.glob(str(work / "plays" / "**" / "*.shp"), recursive=True):
        plays += shp_features(shp, "play", lambda r: r["Shale_play"].replace("Wolfcamp - Midland", "Wolfcamp (Midland)").replace("Niobrara- Mowry", "Niobrara-Mowry"),
                              lambda r: r["Shale_play"] in KEEP_PLAYS, lambda r: {"basin": r["Basin"], "age": r["Age_shale"]})
    bc = work / "bc_plays.json"
    if bc.exists():
        for f in json.load(bc.open())["features"]:
            plays.append({"type": "Feature", "properties": {"name": f["properties"]["PLAY_TREND_NAME"].replace(" Basin", ""), "kind": "play", "country": "CA", "basin": "WCSB"}, "geometry": f["geometry"]})
    for shp in glob.glob(str(work / "ab_shale" / "**" / "*.shp"), recursive=True):
        r = shapefile.Reader(shp)
        flds = [f[0] for f in r.fields[1:]]
        name_fld = next((f for f in flds if f.upper() in ("UNIT", "UNIT_NAME", "NAME", "FORMATION", "FM", "SHALE_UNIT", "STRAT_UNIT")), flds[0])
        for sr in r.iterShapeRecords():
            rec = dict(zip(flds, sr.record))
            nm = str(rec[name_fld]).strip()
            if nm:
                plays.append({"type": "Feature", "properties": {"name": nm, "kind": "play", "country": "CA", "basin": "WCSB"}, "geometry": sr.shape.__geo_interface__})
    work.joinpath("basins.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": basins}))
    work.joinpath("plays.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": plays}))
    print(f"{len(basins)} basins, {len(plays)} plays")
    raw = work / "basins_raw.topo.json"
    with raw.open("w") as fh:
        subprocess.run(["npx", "-y", "-p", "topojson-server", "geo2topo", f"basins={work / 'basins.geojson'}", f"plays={work / 'plays.geojson'}"], check=True, stdout=fh)
    simp = work / "basins_simp.topo.json"
    with simp.open("w") as fh:
        subprocess.run(["npx", "-y", "-p", "topojson-simplify", "toposimplify", "-p", "0.005", "-f", str(raw)], check=True, stdout=fh)
    with OUT.open("w") as fh:
        subprocess.run(["npx", "-y", "-p", "topojson-client", "topoquantize", "1e4", str(simp)], check=True, stdout=fh)
    print("wrote", OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
