# tools — FARSITE weather/wind data-prep scripts

Six command-line utilities that prep FARSITE/FlamMap inputs from CAL
FIRE/IRWIN, HRRR, and WindNinja-computed data (and compare the simulated
perimeter against the real one):

| Script | Input | Output |
|---|---|---|
| `calfire_ignition.py` | CAL FIRE perimeter + IRWIN ignition point (network) | `ignition` + `reference_perimeter` shapefiles + `fire.json` |
| `hrrr_to_wxs.py` | HRRR sfc analysis archive (network) | FARSITE `.wxs` weather stream |
| `runroot_to_atm.py` | WindNinja run2 wind grids (`_vel.asc`/`_ang.asc`, offline) | FARSITE `.atm` + resampled wind grids |
| `ingest_landscape.py` | LANDFIRE Product Service (LFPS) landscape (network) | FARSITE/WindNinja multi-band landscape `.tif` (+ optional elevation DEM) |
| `orchestrate.py` | one TOML config | chained pipeline: landscape + ignition + WindNinja cfg + `.wxs` + FARSITE inputs/command files + `runfarsite` |
| `compare_perimeter.py` | run-dir or reference+simulated shapefiles | IoU + overlay PNG (+future metrics) |

Run all from the **repo root** (`python tools/<script.py> ...`). Any
relative path defaults or flags resolve against the run CWD; the scripts'
`--out` defaults are therefore already prefixed with `FireBehaviorModels/`
so default-omission writes into the app's sample-data tree
(`FireBehaviorModels/SampleData/Palisades/`). All six run natively on
Windows too (Python ≥ 3.11); see `WINDOWS.md` at the repo root.

`runroot_to_atm.py` imports helpers (`die`, `_STAMP_RE`, `parse_utc`,
`resolve_timezone`), `calfire_ignition.py` imports `die`, and
`ingest_landscape.py` (plus `compare_perimeter.py`) imports `die` from
`hrrr_to_wxs.py`; all six files must
stay in the same directory. Each
script's own docstring is the authority on data contracts;
`python tools/<script.py> --help` prints the full option list.

---

## calfire_ignition.py — CAL FIRE perimeter + IRWIN ignition → FARSITE shapefiles

Pipeline head for FARSITE/FlamMap runs. Resolves a historical California
fire from the CAL FIRE FRAP "California Fire Perimeters (all)" service,
then resolves the fire's ignition point of origin by its IRWINID against
NIFC's public WFIGS mirror of IRWIN, and writes the two FARSITE-accepted
shapefiles a run needs — the ignition seed and the real reference footprint
— plus `fire.json`. It does NOT run `hrrr_to_wxs.py` or FlamMap.

For each resolved fire the tool:
1. harvests FRAP metadata: `ALARM_DATE`/`CONT_DATE`/`GIS_ACRES`/`CAUSE`/
   `AGENCY`/`UNIT_ID`/`INC_NUM`/`IRWINID` (dates converted to whole-hour
   UTC instants);
2. queries WFIGS by `IrwinID` and takes the record's Point geometry as the
   point of origin (verified ~665 m from CAL FIRE's published ignition on
   the repo's Palisades case);
3. writes `ignition.*` (one POINT shape, the `FARSITE_IGNITION_FILE` seed)
   and `reference_perimeter.*` (the real final footprint as a multipart
   POLYGON to compare against the simulated perimeter), both with `.prj`
   WKT, plus `fire.json`;
4. prints a ready-to-run `hrrr_to_wxs.py` command with `--lat/--lon` pinned
   to the ignition point and a whole-hour-UTC `--start/--end` taken from the
   FRAP alarm/containment dates.

```
python tools/calfire_ignition.py --fire-name <name> [--year YYYY] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--fire-name` | *(required)* | CAL FIRE fire name; matched case-insensitively. |
| `--year` | *whole FRAP history* | Restrict to one fire `YEAR_`; omit to search all years. |
| `--index` | *(none)* | 0-based pick when the name matches >1 record (e.g. several "Ranch" fires in a year); required in that case. |
| `--lat`, `--lon` | *(IRWIN lookup)* | Manual ignition WGS84 `lat`/`lon`; give both and skip the IRWIN/WFIGS lookup (used for pre-IRWIN fires). |
| `--crs` | `EPSG:4326` | Output CRS for both shapefiles; any pyproj-accepted input (e.g. `EPSG:32611`). |
| `--out-dir` | `FireBehaviorModels/SampleData/<year>_<slug>` | Output directory, created if missing. `<slug>` = lowercased fire name with spaces → `-`. |

Examples:

```bash
# Resolve Palisades 2025 end-to-end (ignition over IRWIN), default output dir
python tools/calfire_ignition.py --fire-name Palisades --year 2025

# Manual ignition for a pre-IRWIN fire (no FRAP IRWINID), no WFIGS lookup
python tools/calfire_ignition.py --fire-name Cedar --year 2003 \
  --lat 32.8686 --lon -116.6775 --out-dir runs/cedar_2003

# Disambiguate a repeated name, UTM output
python tools/calfire_ignition.py --fire-name Ranch --year 2025 --index 1 \
  --crs EPSG:32611
```

Notes:
- IRWIN data is consumed via NIFC's public WFIGS "Ignitions - Wildland Fire
  Incident Locations" ArcGIS service, not the credential-gated IRWIN API. The
  service Point geometry (not the frequently-null `InitialLatitude/
  InitialLongitude` fields) is the point of origin.
- Coverage floor for auto-ignition is the IRWIN era (~2019+). Pre-IRWIN fires
  with a null FRAP `IRWINID` need `--lat/--lon`; the **polygon leg works for
  the full FRAP history (1878+)** regardless — comparison vs simulation is
  never blocked, only auto-ignition.
- FRAP properties are UPPERCASE; WFIGS properties are camelCase (`IrwinID`,
  `IncidentName`) — never assume case equality.
- The printed hrrr command's `--start/--end` are whole-hour UTC instants from
  the alarm/containment dates; `--dem` is a placeholder to fill in (hrrr_to_wxs
  requires it).
- Outputs land under the gitignored `FireBehaviorModels/` tree by default, so
  they stay untracked.

---

## hrrr_to_wxs.py — HRRR → FARSITE `.wxs`

Downloads the HRRR surface analysis for a UTC fire window, samples one
representative cell per hour, converts units, and writes a valid FARSITE
Weather Stream File whose rows are labelled on the fire-local **standard**
clock.

Two phases:
- **Phase A** — downloads + persists one six-band subset grib per hour into
  `--cache-dir` (skips already-cached hours; re-runs are offline).
- **Phase B** — converts the cached gribs to `.wxs` rows with no network.

```
python tools/hrrr_to_wxs.py --dem <lcp-or-dem-raster> [--start ... --end ...] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--dem` | *(required)* | Fire-area LCP/DEM raster; band 1 = elevation (m). Single source of truth for `RAWS_ELEVATION` and elevation-matched cell selection. |
| `--start`, `--end` | *(inferred from run-root)* | UTC burn-window bounds, `%Y-%m-%dT%H:%M` (optional trailing `Z`), whole hours, no `+HH:MM` offset allowed. Omit both to infer the window from `--run-root` frames. |
| `--row-timezone` | *derived from anchor* | IANA name of the fire-local clock used for row labels. Without `--start/--end` it also locates the run-root frames. |
| `--lat`, `--lon` | *inferred from DEM* | Anchor cell location; give both or neither. Omitted → DEM band-1 valid-data centroid. |
| `--elevation-tol-ft` | `500` | Elevation-match tolerance (ft) when choosing the sample cell. |
| `--lead-days` | `0` | Conditioning hours prepended before `--start`. |
| `--out` | `FireBehaviorModels/SampleData/Palisades/palisades-hrrr.wxs` | Output `.wxs` path; parent dir is created if missing. |
| `--dump-dir` | *(none)* | Optional dir for `samples.csv`, a pre-rounding audit dump of the actual sampled values. |
| `--cache-dir` | `./.cache/hrrr` | Dir persisting the per-hour subset gribs (CWD-relative). |
| `--threads` | `8` | Phase-A download thread count. |
| `--run-root` | *(none)* | Dir of per-hour WindNinja `_vel.asc`/`_ang.asc` pairs (same layout as `runroot_to_atm.py`); enables the burn-window wind-coverage gate and window inference. |
| `--plot PNG` | *(none)* | Write a 6-panel meteogram (temperature, RH, wind speed/direction, cloud, precip) of the generated `.wxs`; requires matplotlib. |

Examples:

```bash
# Explicit UTC window, anchored on the DEM centroid
python tools/hrrr_to_wxs.py \
  --dem FireBehaviorModels/SampleData/BlueMountain/BlueMountain.tif \
  --start 2025-01-12T00:00Z --end 2025-01-12T06:00Z

# Window inferred from run-root wind frames, with coverage gate + audit dump
python tools/hrrr_to_wxs.py \
  --dem .../palisades.tif \
  --run-root FireBehaviorModels/run2 \
  --dump-dir ./audit --plot ./hrrr-meteogram.png
```

Notes:
- `--start/--end` are **UTC instants**, never re-read as wall time; the
  fire-local clock only affects row labels.
- The HRRR grid is ~3 km; an anchor shift inside a cell can move the sampled
  cell (and its values + `RAWS_ELEVATION`). Pass explicit `--lat/--lon` to
  pin weather to a specific location.
- Without `--run-root`, the wind-coverage gate is skipped (standalone mode).

---

## runroot_to_atm.py — WindNinja run2 grids → FARSITE `.atm`

Builds a WindNinja-native FARSITE Atmosphere Grid (`.atm`) from a run-root
of terrain-resolved wind pairs — the same folder layout that
`hrrr_to_wxs.py --run-root` reads. **No network access in any code path.**

For each detected `_vel.asc`/`_ang.asc` pair, the tool:
1. converts wind speed m/s → mph (`--units mph`) or km/h (`--units kmh`);
2. resamples both grids onto the `--dem` landscape grid (FlamMap 6 requires
   wind grids to match the landscape cell size/extent);
3. writes each converted grid beside the `.atm`, with a matching `.prj` in
   the landscape CRS (direction normalized 0–359);
4. emits the 2-column `.atm` manifest (CRLF line endings, `WINDS` header +
   `ENGLISH`/`METRIC`).

```
python tools/runroot_to_atm.py --run-root <grids-dir> --dem <landscape-raster> [options]
```

`--run-root` and `--dem` are required unless `--verify` is used.

| Option | Default | Meaning |
|---|---|---|
| `--run-root` | *(required)* | Dir of per-hour `_vel.asc`/`_ang.asc` pairs. `PASTCAST-GCP-*` tiles are inputs, not outputs, and are ignored. |
| `--dem` | *(required)* | Fire-area landscape/LCP raster; wind grids are resampled onto its grid. |
| `--row-timezone` | *(none)* | IANA fire-local clock; **required** when `--start/--end` are given (matches UTC to run-root local frames). |
| `--start`, `--end` | *all frames* | UTC burn-window bounds; give both or neither. Omitted → use every detected frame. |
| `--lead-days` | `0` | Also emit rows for conditioning-lead hours before the burn window. |
| `--units` | `mph` | `mph` (writes `ENGLISH`) or `kmh` (writes `METRIC`). |
| `--out` | `FireBehaviorModels/SampleData/Palisades/palisades-hrrr.atm` | Output `.atm` path; converted grids are written beside it with their source basenames. |
| `--verify ATM` | *(none)* | Standalone integrity check: every grid referenced by the `.atm` must exist, be non-empty, and decode to exactly its header ncols × nrows. Nothing else runs. |

Examples:

```bash
# All detected frames, mph, into the app sample-data tree
python tools/runroot_to_atm.py \
  --run-root FireBehaviorModels/run2 \
  --dem .../palisades.tif

# Explicit UTC window (row-timezone mandatory), km/h
python tools/runroot_to_atm.py \
  --run-root FireBehaviorModels/run2 \
  --dem .../palisades.tif \
  --start 2025-01-12T00:00Z --end 2025-01-12T06:00Z \
  --row-timezone America/New_York --units kmh

# Verify a deployed .atm's grids in place
python tools/runroot_to_atm.py --verify FireBehaviorModels/SampleData/Palisades/palisades-hrrr.atm
```

Notes:
- A burn hour without a terrain-resolved pair is a hard error (wind-coverage
  gate), mirroring `hrrr_to_wxs.py`.
- `--out` is refused when it points into `--run-root` itself (would overwrite
  the source grids).
- `.atm` row times are the run2 local stamps; they must sit on the same clock
  as the `.wxs` rows / FARSITE burn periods (standard-time runs).
- FARSITE keeps a wind set in force until a later row supersedes it, so one
  row per detected frame covers the burn window.

---

## ingest_landscape.py — LANDFIRE (LFPS) landscape → FARSITE/WindNinja landscape

Downloads a fire-area landscape (elevation + fuel/canopy layers) as a single
multi-band raster from LANDFIRE's Product Service (LFPS), an open public REST
API (no auth token; only an email, an AOI, and a layer list are required), and
writes it as a FARSITE/WindNinja-ready GeoTIFF. It produces only the landscape
— it does NOT assemble the FARSITE inputs file, command file, weather, wind
grids, or run `runfarsite` (`orchestrate.py`, below, chains the whole pipeline
from one config file).

The default output is an **8-band** stack mirroring the working
`FireBehaviorModels/SampleData/BlueMountain/BlueMountain.tif` layout:
band 1 = elevation, 2 = slope, 3 = aspect, **band 4 = fuel model**, 5 = canopy
cover, 6 = canopy height, 7 = canopy base height, 8 = canopy bulk density.
FARSITE reads exactly one fuel band positionally (band 4); WindNinja reads
band 1 (elevation) via `elevation_file`. Layer order is the contract — `--layers`
is used verbatim in exact band order (never sorted/deduped), because LFPS emits
output bands in `Layer_List` order.

```
python tools/ingest_landscape.py --bbox "-118.7 33.9 -118.4 34.2" \
  --email you@example.com --out /tmp/lf_landscape [--dem-out /tmp/lf_elev.tif]
```

| Option | Default | Meaning |
|---|---|---|
| `--bbox` | *(none)* | WGS84 `W S E N` bbox (mutually exclusive with `--mapzone`); exactly one required. |
| `--mapzone` | *(none)* | LANDFIRE map-zone number (valid 1–10, 12–80, 98–99); returns the FULL zone extent. |
| `--email` | *(required)* | LFPS-required requester email (open API, not an auth token). |
| `--version` | `2024` | Release year for the annually-updated product layers (e.g. `2023`, `2024`, `2025`). |
| `--fuel-model` | `fbfm40` | Band-4 fuel classification: `fbfm40` (40 Scott & Burgan, default) or `fbfm13` (13 Anderson); case-insensitive. |
| `--layers` | *derived* | Semicolon-delimited LFPS layer codes, **used verbatim in exact band order**. Omit for the version-derived 8-band stack. |
| `--output-projection` | `5070` | EPSG WKID; MUST force `5070` (NAD83/Conus Albers) — without it LFPS emits a per-AOI Albers that changes every run. |
| `--resolution` | `30` | Output resolution in m; 30 = native (omit `Resample_Resolution`). LFPS only coarsens, so `<30` is rejected. |
| `--out` | `FireBehaviorModels/SampleData/landfire/landscape` | Output base path (no extension); written as `<out>.tif`. |
| `--dem-out` | *(none)* | Optional single-band int16 elevation GeoTIFF (band-1 extraction) for WindNinja. |
| `--max-wait` | `900` | Max seconds to poll job status (LFPS runs ~12 s–7 min typical, up to 2 h). |
| `--poll-interval` | `5` | Seconds between status polls. |
| `--keep-zip` | *off* | Keep the raw `<JobId>.zip` bundle after extraction (archival only); default deletes it. |

**AOI: bbox vs map zone.** Prefer `--bbox` for fire-scale pulls — it clips to a
tight area (small, fast LFPS job). `--mapzone` returns the FULL administrative
zone extent in that zone's native projection (useful for regional/whole-zone
analyses but region-sized and can exceed LFPS AOI/2-h limits). Both paths force
`--output-projection` (default 5070), so the CRS is stable either way.

**Annual-version resolution.** Terrain (elevation/slope/aspect) is LANDFIRE's
static base product (`LF2020_*`); fuels/canopy are updated annually
(`LF{--version}_*`). Band 4 is always the single fuel band selected by
`--fuel-model`. An explicit `--layers` bypasses version/fuel derivation entirely
(used to add a 9th band or pin a specific per-layer year). There is **no silent
version fallback** — if a requested year's layer 400s with
`Invalid products: <codes>`, the script prints that verbatim and points the user
to the products catalog.

### LFPS endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `https://lfps.usgs.gov/api/job/submit` | POST | Submit a job (Layer_List, Area_of_Interest, Email, Output_Projection). |
| `https://lfps.usgs.gov/api/job/status?JobId=<id>` | GET | Poll job status; `outputFile` on success. |
| `https://lfps.usgs.gov/api/job/cancel?JobId=<id>` | GET | Cancel an in-progress job. |
| `https://lfps.usgs.gov/api/upload/shapefile` | POST | Upload a shapefile AOI. |
| `https://lfps.usgs.gov/api/healthCheck` | GET | Service health. |
| `https://lfps.usgs.gov/products` | GET | Client-rendered product/version catalog. |
| `https://lfps.usgs.gov/docs/api` | — | Swagger API docs. |

### Limitations

- **CONUS/AK/HI only** — LFPS serves no insular areas; bbox ranges lon -188..-66,
  lat 18..72. A non-covered AOI is surfaced as the LFPS error verbatim.
- **6-h download retention / up-to-2-h jobs** — a product URL expires ~6 h after
  completion; a 404 on download is reported with that hint (retry by
  re-submitting).
- **Annual-version model** — terrain resolves to static `LF2020_*` and the five
  fuel/canopy products to `LF{--version}_*`; if LANDFIRE later releases an
  updated terrain base, only the `_TERRAIN` constant changes. The products
  catalog is a client-rendered Next.js SPA (not machine-readable), so the script
  does not scrape it; per-year availability is enforced by LFPS submit's
  `Invalid products: <codes>` response.
- **Category semantics not translated** — LFPS returns raw LANDFIRE codes
  (e.g. FBFM40 fuel-model integers); the script doesn't translate attribute
  tables into names. FARSITE runs on the codes, which is all it consumes.
- **`--resolution` 30 = native** — LFPS only coarsens, so finer-than-native
  (`<30`) is rejected.
- **FARSITE 9th-band requirement is unverified** — the working BlueMountain LCP
  is 9 bands (band 9 = FCCS), but LFPS's documented fire-behavior stack is 8.
  Default is the 8-band stack; if a real run later needs a 9th band, pass it via
  `--layers` (no code change).

---

## orchestrate.py — FARSITE/WindNinja pipeline from one TOML config

Chains the four scripts above, generates + runs a WindNinja CLI config, and
assembles + runs the FARSITE inputs/command files from a single human-editable
TOML file (see `config.example.toml` — a faithful annotated PALISADES example).
One config is the single source of truth for a run; TOML is parsed with the
stdlib `tomllib` (Python 3.11+), so there is zero new dependency.

```
python tools/orchestrate.py --config config.example.toml            # run
python tools/orchestrate.py --config config.example.toml --dry-run  # plan only
```

`--dry-run` prints every planned subprocess argv plus the full text of every
file that would be written (WindNinja cfg, FARSITE inputs + command files),
and touches nothing on disk. `--config` is required; config errors exit 2.

Sequential stages (each resumable via `enable=false` + its input override after
a partial/network failure):

1. **landscape** — `ingest_landscape.py --bbox/--mapzone --email --version
   --fuel-model --resolution --out <run>/landscape [--dem-out]`; the LCP is
   `<run>/landscape.tif`. `enable=false` reuses `[landscape] lcp`. The
   single-band WindNinja DEM = `[landscape] dem_out` or an extracted band-1 of
   the LCP (`<run>/<slug>-dem.tif`, mirroring `ingest_landscape._write_dem`).
2. **fire** — `calfire_ignition.py --fire-name --year [--index] [--lat/--lon]
   --crs <LCP CRS> --out-dir <run>`; `fire.json` fields (name/year/alarm/cont/
   lat/lon) then drive the window, timezone, and HRRR anchor. `enable=false`
   reuses `[fire] fire_json`.
3. **window** — UTC burn window from `[simulation] start/end` else the fire.json
   alarm/containment; the fire-local STANDARD IANA clock comes from
   `[simulation] row_timezone` or is derived from the ignition point. `lead_days`
   prepends conditioning days of weather (hrrr emits them as leading RAWS rows;
   FARSITE conditions fuels from the pre-start stream).
4. **windninja** — writes `<run>/windroot/<slug>.cfg`: user `[windninja.options]`
   passthroughs then the injected winning keys (`elevation_file`, `output_path`,
   `time_zone`, `num_threads`, `write_ascii_output`, `write_farsite_atm`,
   `mesh_resolution` = the LCP cell size, `units_mesh_resolution = m`). Runs
   `[windninja] command` if set; empty command prints the exact manual command
   and the run pauses at stage 5 until WindNinja's `.atm` exists (re-running the
   same config resumes automatically — existing pairs are reused). `enable=false`
   reuses an existing run root.
5. **atm** — locates the single native WindNinja `.atm` under the run root and
   integrity-checks every referenced grid with `runroot_to_atm.verify_atm`
   (presence + decodability; nothing is resampled/converted — WindNinja's
   `write_farsite_atm` + the injected `mesh_resolution` put the grids on the LCP
   grid already).
6. **weather** — `hrrr_to_wxs.py --dem <LCP> --lat/--lon <ignition> --start/--end
   --row-timezone <IANA> --run-root <run_root> --cache-dir ... --lead-days ...
   --out <run>/<slug>-hrrr.wxs`; the wind-coverage gate governs the burn window
   (conditioning lead hours need no WindNinja outputs). `enable=false` reuses
   `[weather] wxs`.
7. **farsite-assemble** — writes `<run>/<slug>-FarsiteInputs.txt`
   (fuel-moisture block derived from the LCP band-4 distinct models + 0,
   sample tuple `6 7 8 60 90 16`, overridable per-model via
   `[farsite.fuel_moistures]`; burn periods; local start/end; the `.wxs` rows as
   the `RAWS:` block; `FARSITE_ATM_FILE:` = the native `.atm`) and a single-line
   `<run>/<slug>-FarsiteCmd.txt` of absolute paths.
8. **farsite-run** — `runfarsite` when `[farsite] run=true` (requires
   `[farsite] command`, e.g. `wine C:/.../runfarsite.exe`); `run=false` assembles
   only, `enable=false` skips even assembly.

Notes:
- The `.wxs` rows, `.atm` rows, and burn periods are all on the fire-local
  STANDARD clock (the repo's documented timing contract); FARSITE burn-period
  entries are per-day `M D HHMM HHMM` — the config's `M D HHMM` end is written
  as an HHMM on the start's day, so span a night with one entry per day.
- All paths written into the WindNinja cfg / inputs / command files are absolute
  (`Path.resolve()`), so they are native `C:\...` paths on Windows and
  `Z:\`-style under Wine on Linux.
- The hrrr wind-coverage gate enforces the burn window only; a `.atm` covering
  more hours than the burn window is harmless (FARSITE keeps a wind set in force
  until a later row supersedes it).
- The WindNinja CLI and `runfarsite` binaries are not in this repo (WindNinja is
  present only as a DLL; `runfarsite.exe` lives under the gitignored
  `FireBehaviorModels/bin/`; on Linux/WSL they need Wine + the `bin/` DLL stack,
  see `../docs/WindNinja-CLI.md` / `../docs/FARSITE-CLI.md`). `orchestrate`
  drives them entirely through the `command`/`cwd` config fields, on Linux/Wine
  and native Windows alike (see `../WINDOWS.md`).

---

## compare_perimeter.py — CalFire vs FARSITE perimeter IoU + overlay

Consumes pipeline *output* (no stage of `orchestrate.py` is rewired): overlaps
the real CalFire final footprint `<run>/reference_perimeter.shp` (written by
`calfire_ignition.py`) against FARSITE's simulated perimeters and reports the
IoU (intersection-over-union) plus an overlay PNG. New metrics/visualizations
can slot in later via the `METRICS` registry and `plot_overlay()`; this version
ships IoU + the overlay only.

```
python tools/compare_perimeter.py --run-dir <run> [--metric iou]
                                  [--plot overlay.png] [--json result.json]
                                  [--basemap auto|imagery|osm|none]
python tools/compare_perimeter.py --reference <ref.shp> --simulated <sim.shp>
                                  [same options]
```

Exactly one source group is required: `--run-dir` (reference + fire.json +
`farsite-out/<slug>_Perimeters.shp` all derive from it; the slug is the
lower-cased fire name with spaces → `-`), or explicit `--reference` +
`--simulated`. `--metric` is a comma-list (valid: `iou`); `--json` dumps the
machine-readable `compare()` dict; `--plot` writes the overlay PNG.

**Final-perimeter selection.** FARSITE's `_Perimeters.shp` holds one record per
growth timestep; the final footprint is the record(s) with the max elapsed
field (`Elapsed_Mi`/`Elapsed_Minutes`/`*elapsed*`, else any `time`/`simtime`
field). With no such field all records are kept — growth is monotonic, so
union-all equals the final state (the printed `elapsed_field` reports which
path ran).

**Metric CRS.** Areas are computed in the simulated shapefile's own CRS (the
reference is reprojected into it) — FARSITE's landscape CRS, e.g. EPSG:32611
for Palisades. A geographic sim CRS falls back to a UTM zone derived from the
simulated footprint's centroid (`32611` = UTM 11N, etc.). IoU is planar; in
the ≤50 km extents this repo targets, the error vs geodesic is negligible.

**Basemap.** Esri World Imagery (satellite) is the default — the real burned
area stays visible under the simulated footprint, no API key needed. The chain
runs `imagery → osm → none`: any provider failure (HTTP, decode, offline)
falls through, and a fully offline host still gets the plain projected frame
with labeled axes. `--basemap none` skips tiles entirely; `--basemap imagery` /
`--basemap osm` pin one provider. Tiles are fetched directly with `requests`
and mosaicked with numpy/Pillow (no new dependency), mirroring how
`calfire_ignition.py` talks to public services.
