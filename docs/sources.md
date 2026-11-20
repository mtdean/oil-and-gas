# Source catalogue (verified 2026-11-19)

Status: **active** = fetched automatically · **manual** = file must be dropped in by hand · **planned** = known source, connector not built.
All URLs were tested on 2026-11-19. Sizes are download sizes.

## State regulators — well/entity-level production + locations

### Texas — Railroad Commission (`tx`, active)
* Index: https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/ (all free, no login).
  Files are served from GoAnywhere "GoDrive" shares at `https://mft.rrc.texas.gov/link/<uuid>`; `ogdata.mft` scripts the JSF form posts.
* **Production**: PDQ dump `PDQ_DSV.zip` (3.45 GB, monthly, last Saturday) — Oracle delimited text, delimiter `}`. Tables used: `OG_LEASE_CYCLE`
  (lease-month volumes, 1993-01+) and `OG_WELL_COMPLETION` (lease→API). Manual: https://www.rrc.texas.gov/media/50ypu2cg/pdq-dump-user-manual.pdf
  **Oil is lease-level; gas "leases" are single wells.** Pre-1993 only on microfilm.
* **Locations**: Digital Map Information well layers, one `well<FIPS>.zip` shapefile per county (250 files, twice weekly) with `API` (8-digit) and `LAT83/LONG83`.
  Guide: https://www.rrc.texas.gov/media/kmld3uzj/digital-map-information-user-guide.pdf
* Also available (not yet used): full wellbore dump `dbf900.txt.gz` (370 MB, weekly, fixed-width COBOL layout, includes WGS84 lat/lon per wellbore);
  `OG_WELLBORE_EWA_Report.csv` (457 MB monthly snapshots since 2020-09); drilling permits nightly; completions nightly.

### New Mexico — OCD (`nm`, active)
* FTP `ftp.emnrd.nm.gov` anonymous: `/Public/OCD/OCD Interface v1.1/volumes/wcproduction/wcproduction.zip` (~1 GB; UTF-16 SQL-rowset XML, >4 GB uncompressed).
  One row per well × pool × month × product (O/G/W) from 1973. Data dictionary: https://www.emnrd.nm.gov/ocd/wp-content/uploads/sites/6/OCD-Interface-v1.1-Data-Dictionary-Protected.xlsx
  Also on FTP: `core/wellhistory` (SCD well history w/ lat/lon), `core/wchistory` (completions, perfs), `volumes/wcinjection`, `volumes/podvolume` (sales).
* Wells: ArcGIS hub CSV https://ocd-hub-nm-emnrd.hub.arcgis.com/api/download/v1/items/387f397ebf164c7aa6a752aac7d22b17/csv?layers=0 (142k wells, NAD83).
  Feature service: https://gis.emnrd.nm.gov/arcgis/rest/services/OCDView/Wells_Public/FeatureServer/0. Also C-115B venting/flaring by well-month on the hub.

### North Dakota — NDIC (`nd`, active)
* https://www.dmr.nd.gov/oilgas/mpr/YYYY_MM.xlsx — monthly, 2015-05+, sheet `Oil`: API14, operator, well, field, pool, oil/water/gas/gas sold/flared/days/runs, **Lat/Long**.
  ~45-day lag; recent months revised. Pre-2015 well history only via NDIC paid premium services.

### Colorado — ECMC (`co`, active)
* Monthly production by well × formation: `https://ecmc.state.co.us/documents/data/downloads/production/{YYYY}_prod_reports.zip` (1999 is `.csv`), 1999+,
  plus rolling `monthly_prod.csv`. Dictionary: .../production/production_record_data_dictionary.htm. 2025 file has drifted column names — parsed by name.
* Wells: `https://ecmc.state.co.us/documents/data/downloads/gis/WELLS_SHP.ZIP` (daily, 124k, lat/lon, spud, MD/TVD, status). Related: directional bottom-holes, permits, locations shapefiles.
* "Annual Production Summary" zips are MS Access .mdb with **annual** totals only — not used.

### Wyoming — WOGCC (`wy`, active)
* `https://pipeline.wyo.gov/RAllcountyprodbyyear.cfm?nYr=YYYY` — tab-separated, one row per well (× formation) per year with Jan..Dec oil/gas/water/days, lat/lon, 1978+.
* Bulk alternatives: `https://pipeline.wyo.gov/downloads/TTLProd_MMDDYY.zip` (430 MB xlsx), well header `MMDDYY WH.zip` (DBF, 143k wells) — names date-stamped; scrape `urecordsMenu.cfm`.

### Utah — DOGM (`ut`, active)
* `https://oilgas.ogm.utah.gov/pub/Database/Production{1984To1998,...,2025To2029}.zip` — monthly by well × wellbore × formation, 1984+, regenerated daily.
* `Wells.zip` (40k wells, 14-digit API, NAD83 lat/lon, status/type, cumulative volumes, first prod), `WellHistory.zip` (spud/completion/TD), `LatLongCoordinates.zip`.
* Readmes: https://oilgas.ogm.utah.gov/pub/database_download_read/

### Montana — MBOGC (`mt`, active)
* `https://bogfiles.dnrc.mt.gov/Reporting/Production/Historical/MT_Historical_Production.zip` (74 MB; well × formation × month, 1986+, 5.9M rows; also PRU/lease dispositions).
* `https://bogfiles.dnrc.mt.gov/Reporting/Wells/MT_CompleteWellList.zip` (42k wells, lat/lon, spud/completion, TD). Monthly delta: `Reporting/MonthlyUpdates/MT_MonthlyUpdate.zip`.
* Index pages use backslashes in hrefs; files are on `bogfiles.dnrc.mt.gov`.

### Pennsylvania — DEP (`pa`, active, deliberately low-touch)
* Form https://greenport.pa.gov/ReportExtracts/OG/OilGasWellProdReport → POST `ExportOGProdReport` with `RptPeriodsid` (one period per request) + anti-forgery token (fresh session per request) → CSV with **lat/lon**.
  Monthly unconventional 2015-01+, semiannual 2010–2014, annual conventional 1980+.
* The server answers 400 for many requests. To avoid hammering it the connector makes **one attempt per period per run, 5 s apart, at most 15 new periods per run, and stops for the run after 3 consecutive refusals**. Periods already on disk whose end is >24 months old are never re-requested. Missing periods fill in over the daily runs (as of 2026-11-19: 45 of 197 periods loaded).
* Well inventory: POST `WellIvventoryExport` (sic) with `Operator=-1&Unconvid=Y|N`, refreshed every 14 days.
* Dictionary: https://files.dep.state.pa.us/OilGas/BOGM/BOGMPortalFiles/OilGasReports/HelpDocs/SSRS_Report_Data_Dictionary/DEP_Oil_and_GAS_Reports_Data_Dictionary.pdf

### Ohio — ODNR (`oh`, active, URL list maintained by hand)
* Annual all-well and quarterly horizontal xlsx files on `dam.assets.ohio.gov` / `ohiodnr.gov/static` (list in `sources/oh.py`; file names carry a date suffix, so add new ones as posted).
  Columns: API14, year, quarter, owner, county, oil/gas/brine, days, first production. 2010+ online; 1984+ on request / in RBDMS.
* Wells: ODNR ArcGIS hub export `https://gis-odnr.opendata.arcgis.com/api/download/v1/items/e03ec46046af49c49d879ab9be07f980/csv?layers=3` (202 until job ready; weekly).
* RBDMS Access DB: `.../oil-gas/rbdms/Rbdmsd97Weekly.zip` (304 MB, weekly) — not used. ohiodnr.gov HTML only renders for browsers.

### West Virginia — DEP (`wv`, active)
* Page https://dep.wv.gov/oil-and-gas/databaseinfo/Pages/default.aspx lists `apps.dep.wv.gov/Documents/OOG/ProductionReports/<decade>/<year>Production.{xls,xlsx,zip}` 1985–2025:
  one row per well-year with Jan..Dec gas/oil/water/NGL columns. Quarterly H6A horizontal files also exist (skipped: duplicates annual).
* Wells: TAGIS REST `https://tagis.dep.wv.gov/arcgis/rest/services/WVDEP_enterprise/oil_gas/MapServer/7` (all DEP wells) with `outSR=4326`.

### Kansas — KGS (`ks`, active)
* `https://www.kgs.ku.edu/PRS/Ora_Archive/{oil,gas}_leases_{1980_1989,...,2020_present}.zip` — **lease-level** monthly (plus annual rows), 1980+, monthly refresh.
  Lease rows list member API numbers → `entity_wells`. Pre-1987 licensed from IHS: no redistribution.
* `ks_wells.zip` (master well list, NAD27 lat/lon, spud/completion/plug, status, IP). `all_kansas_leases.zip` (lease master). Format: https://www.kgs.ku.edu/Magellan/Field/lease_file_format.html

### Oklahoma — OCC (`ok`, active: wells + crosswalk only)
* Wells: https://oklahoma.gov/content/dam/ok/en/occ/documents/og/ogdatafiles/rbdms-wells.csv (127 MB nightly, 457k wells, WGS84 lat/lon).
* Completions (Form 1002A, 2010+): `.../ogdatafiles/completions-wells-formations-base.xlsx` (77 MB) incl. spud/completion/first-prod, TVD/MD, **OTC_Prod_Unit_No (PUN)**.
  Legacy pre-2010: `completions-wells-legacy.xlsx`.
* **Production is not published by OCC.** Oklahoma Tax Commission holds it at PUN level (July 1990+) and releases it via the
  Production History Request Form: https://oklahoma.gov/tax/businesses/gross-production.html . Once obtained, load against `entity_wells` (entity_type=pun).

### Louisiana — DENR SONRIS (`la`, active: wells + crosswalk only)
* Wells via ArcGIS REST https://sonris-gis.dnr.la.gov/arcgis/rest/services/DNRSvc/OC/MapServer/0 (247k wells, lat/lon, status, dates, **current LUW code**).
* **Production (by LUW, 1977+) is only available in bulk through DENR's paid Data Subscription Service**; the SONRIS web app forbids automated access
  (CAPTCHA, 7-day IP bans). Research guide: https://www.denr.louisiana.gov/assets/OC/eng_div/ConsResearchGuide.2026.pdf

### California — CalGEM (`ca`, active: wells only)
* Wells: `https://gis.conservation.ca.gov/portal/sharing/rest/content/items/0d30c4d9ac8f4f84a53a145e7d68eb6b/data` (AllWells_table.zip, nightly).
* Production: well-level monthly files 1977–2021 exist at https://filerequest.conservation.ca.gov/?q=production_injection_data but are **emailed on request**
  (name + email form). 2022+ only in the WellSTAR Power BI dashboard. Not automated.

### Alaska — AOGCC (`ak`, manual)
* Both entry points refuse scripted clients (checked 2026-11-19): https://www.commerce.alaska.gov/web/aogcc/Data → HTTP 403 for non-browser clients (also from other networks); Data Miner https://aogweb.state.ak.us/DataMiner3/ → connection reset. Not worked around.
* Manual: in a browser open Data Miner → **Production** page → `CSV Production` button (no filters) → save as `data/raw/ak/production.csv`; **Well info** page → CSV export → `data/raw/ak/wells.csv`. Then `ogdata update ak --transform-only`. Columns are matched fuzzily and logged; parser is untested until real exports are present. Production is well-level monthly back to 1958, APIs are 14-digit (prefix 50).
* The daily Access extract https://aogcccdnstorage.blob.core.windows.net/webextract/AOGCC_DataExtract.zip (39 MB .mdb, needs `mdbtools`) is an alternative if it becomes reachable.

### Other states (planned, lower priority)
Arkansas (AOGC), Mississippi (MSOGB), Alabama (GSA/SOGB), Michigan (EGLE), Illinois (ISGS), Kentucky (KGS), Nebraska (NOGCC), Virginia (DMME), New York (DEC), Nevada, South Dakota.

## Federal

### BSEE — Gulf of America OCS (`bsee`, active)
* OGOR-A completion-level monthly production: `https://www.data.bsee.gov/Production/Files/ogora{YEAR}delimit.zip` (1996+), `ogora1947-1979delimit.zip`,
  `ogora1980-1989delimit.zip`, `ogora1990-1995delimit.zip`, current-year `ogoradelimit.zip`. No header; layout at https://www.data.bsee.gov/Main/HtmlPage.aspx?page=ogorA
* Boreholes: `https://www.data.bsee.gov/Well/Files/BoreholeRawData.zip` (daily; surface + bottom-hole lat/lon, spud, TD, water depth).
* Also: lease-level `ProductionRawData.zip`, platforms, leases, directional surveys. Wrong filenames return 200 HTML (soft 404).

### ONRR (`onrr`, active)
* https://revenuedata.onrr.gov/downloads/monthly_production.csv (national federal/Native American by commodity, 2003+) and `calendar_year_production.csv` (state/county).

### FracFocus (`fracfocus`, active — blocked from this IP as of 2026-11-19)
* https://www.fracfocusdata.org/digitaldownload/FracFocusCSV.zip (444 MB, 5×/week; `www.` required). `DisclosureList_1.csv` → completions; registry CSVs (chemicals) left raw.
* The host completes the TLS handshake and then resets the connection for every request from this machine (curl and requests, HTTP/1.1 and HTTP/2) while answering 200 from other networks: an IP-level block or WAF. We do not evade it. The daily job makes **one** attempt; if it fails and a zip is already on disk that zip is used.
* Manual drop-in works: download `FracFocusCSV.zip` in a browser from https://fracfocus.org/data-download, save as `data/raw/fracfocus/FracFocusCSV.zip`, run `ogdata update fracfocus --transform-only`.

## Macro

### EIA bulk (`eia_bulk`, active, no key)
* https://api.eia.gov/bulk/{PET,NG,STEO}.zip (manifest https://www.eia.gov/opendata/bulk/manifest.txt), twice daily. Curated series regex in `sources/eia_bulk.py`.
* STEO Table 10a/10b ids: rigs `RIGS{AP,BK,EF,HA,PM,R48}`, DUCs `DUCS..`, new wells drilled `NWD..`, completed `NWC..`, tight oil `TOPR..`, shale gas `SNGPR..`.
  Excel: https://www.eia.gov/outlooks/steo/xls/STEO_m.xlsx . DPR discontinued 2024.

### EIA API v2 (`eia`, needs `EIA_API_KEY` — register free at https://www.eia.gov/opendata/register.php)
* Routes used: `petroleum/pri/spt` (RWTC, RBRTE), `natural-gas/pri/fut` (RNGWHHD), `petroleum/crd/crpdn` (MCRFP{ST}2), `natural-gas/prod/sum` (N9010/N9050 by state),
  `petroleum/stoc/wstk`, `petroleum/sum/sndw` (weekly), `petroleum/crd/drill` (rigs), `steo`. DEMO_KEY is shared and rate-limited.
* WPSR tables: https://ir.eia.gov/wpsr/table1.csv … table9.csv (signed redirect; use -L).

### FRED (`fred`, needs `FRED_API_KEY` — https://fredaccount.stlouisfed.org/apikeys)
* fred.stlouisfed.org CSV endpoint is bot-blocked from many hosts; api.stlouisfed.org works. Series list in `sources/fred.py` (prices, Treasuries, HY/IG/CCC OAS, dollar, O&G PPI/IP/employment).
* Loaded 2026-11-20 with the key shared from `../situation-monitor/.env` (ogdata reads `EIA_API_KEY`/`FRED_API_KEY` from there when missing in its own `.env`; override the path with `OGDATA_SHARED_ENV`). ICE BofA series on FRED only carry ~3 years of history (licence change); everything else is full history.
* Feeds the dashboard ticker (HY/CCC OAS, HY yield, UST 10y, broad dollar) and the Credit & cost row on the Leading indicators tab.

### CFTC COT (`cftc`, active)
* https://www.cftc.gov/files/dea/history/fut_disagg_txt_YYYY.zip (2010+). Key codes: WTI NYMEX 067651, Henry Hub NYMEX 023651, ICE WTI 1st line 06741Q.

### Dallas Fed Energy Survey (`dallasfed`, active)
* https://www.dallasfed.org/~/media/Documents/research/surveys/des/documents/all_data_qq.xlsx (2016Q1+). Also `all_data_price_forecasts.xlsx`, `index_qq.xlsx`.

### Baker Hughes rig count (`bakerhughes`, manual)
* https://rigcount.bakerhughes.com/na-rig-count blocks scripted clients. Download "North America Rig Count Report" xlsx (weekly, 2024+) and the
  "New Report (2013 – Aug 2025)" xlsx into `data/raw/bakerhughes/` as `current_report.xlsx` / `history_2013_2025.xlsx`, then `ogdata update bakerhughes --transform-only`.
  Rig counts also arrive automatically via EIA (`eia:PET.E_ERTRR*`, `eia:STEO.RIGS*`).

## Dead ends checked
* National Oil & Gas Gateway (noggateway.org): offline since 2024.
* Texas PDQ web query: no export. RRC ArcGIS REST: 403.
* EIA annual "Wells by Production Rate": discontinued Oct 2026.
* FRED CSV from this host: Akamai-blocked.
