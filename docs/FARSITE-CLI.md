# FARSITE CLI (`runfarsite`) — Usage Guide

This guide documents the **command-line** FARSITE app in this repo,
`FireBehaviorModels/bin/runfarsite.exe`, and the text input files it consumes.
FARSITE grows a surface/spot fire over a landscape under weather and (optionally)
terrain-resolved gridded winds, and writes per-cell fire-behavior grids,
perimeters, and spot logs.

Primary sources:
- `FireBehaviorModels/doc/FarsiteInputFile.pdf` — the FARSITE DLL input-file switch reference.
- `FireBehaviorModels/SampleData/Farsite/` — `FarsiteCmd.txt` (command file), `FarsiteRunLog.txt` (a complete inputs file), `TestFarsite.bat`.
- `FireBehaviorModels/SampleData/BlueMountain/` — the sample landscape + ignition.
- `runfarsite.exe` / `farsite.dll` binary strings (usage text, switch names, output filenames).
- The `tools/` data-prep scripts that generate FARSITE weather (`hrrr_to_wxs.py`) and gridded winds (`runroot_to_atm.py`).

Companion guide: `WindNinja-CLI.md` (producing the gridded-wind inputs FARSITE consumes).

---

## 1. Version & runtime requirements

`runfarsite` 2.3 (2026/08/19 build) drives `farsite.dll` (FlamMap platform 6.2.45.0
per the sample run log). It is a **Windows x86-64 PE executable** — on Linux/WSL
run it under Wine or inside the repo's Docker image; the DLL stack in `bin/` must
be loadable:

- **PATH** includes `FireBehaviorModels/bin` (all 75 DLLs: `farsite.dll`,
  `WindNinjadll.dll`, GDAL/proj stack, MSVC runtime, boost, curl, …).
- **`GDAL_DATA`** → `FireBehaviorModels/bin/share/gdal-data`.
- **`PROJ_LIB`** → `FireBehaviorModels/bin/share/proj`.
- **`WINDNINJA_DATA`** → `FireBehaviorModels/bin/share/windninja-data`
  (required only when gridded-wind generation is enabled, but set it always).

`FireBehaviorModels/SetEnv.bat` sets all four on Windows. On unix-like hosts set
the same variables (Wine maps `C:\…` to the repo tree). No conda/Python packages
are needed to *run* the app itself; the `tools/` pre-processors need the
`environment.yml` conda env (`flammap`) with `rasterio`/`numpy`.

Input files required per run:

| File | Purpose | Sample in repo |
|---|---|---|
| **Landscape (LCP)** raster | 9-band landscape: elevation/aspect/slope/fuel model + canopy fields; source of all fuels | `SampleData/BlueMountain/BlueMountain.tif` (30 m, NAD83 Albers) |
| **FARSITE inputs file** | text switches: weather, timing, wind, spotting, gridded winds | `SampleData/Farsite/FarsiteRunLog.txt` |
| **Ignition shapefile** | point(s) where fire starts (`.shp` + `.dbf/.prj/.shx`) | `SampleData/BlueMountain/centerIgnit.shp` |
| *(optional)* **Barrier shapefile** | polygons burned as non-burnable | — |

---

## 2. CLI invocation

```
runfarsite <commandfile>
```

`runfarsite` takes **exactly one argument**: the path to a *command file*. Each
non-empty line of the command file is **one FARSITE run**:

```
[LCPName] [InputsFileName] [IgnitionFileName] [BarrierFileName] [outputDirPath] [outputsType]
```

| Field | Meaning |
|---|---|
| `LCPName` | path to the Landscape File |
| `InputsFileName` | path to the FARSITE Inputs File (ASCII) |
| `IgnitionFileName` | path to the Ignition shape File |
| `BarrierFileName` | path to the Barrier shape File, or `0` for none |
| `outputDirPath` | output **base name** (no extension); outputs derive from it |
| `outputsType` | `0` = both ASCII + GeoTIFF, `1` = ASCII grid, `2` = GeoTIFF |

Sample `SampleData/Farsite/FarsiteCmd.txt` (paths relative to the
`SampleData/Farsite/` working directory):

```
..\BlueMountain\BlueMountain.tif .\FarsiteRunLog.txt ..\BlueMountain\centerIgnit.shp 0 .\out\test 2
```

Run with `SampleData/Farsite/TestFarsite.bat`, which simply executes
`runfarsite FarsiteCmd.txt`. Progress is per-run on stdout
(`Loading lcp file for Farsite #0: …`, `Launching Farsite #0`, …), including
`Farsite #0 edge of landscape hits: N` when the fire exits the landscape.

> Note: the sample uses `FarsiteRunLog.txt` as the inputs file. That file is a
> run-log echo that also happens to be a valid inputs file: its log lines start
> with `#` and are therefore treated as comments. A purpose-built inputs file
> (see §3) is the normal case.

---

## 3. FARSITE inputs file

Plain ASCII; **`#` in column 1 = comment** (also inline markers like
`#SELECTED FARSITE OUTPUTS` are comments). Switches are `NAME: value`
(whitespace-insensitive), one per line, order-independent. Units are
**English by default** (feet, mph) unless a `*_UNITS` switch says otherwise.

### 3.1 Mandatory switches

| Switch | Format | Notes |
|---|---|---|
| `FUEL_MOISTURES_DATA:` | `N`, then N lines `Model F1 F10 F100 FMLiveHerb FMLiveWoody` | **Fuel Model 0 is required** (defaults for fuels not listed) |
| weather data | **one** of `RAWS` / `WEATHER_DATA`+`WIND_DATA` (see §4) | |
| `FARSITE_START_TIME:` | `MM DD HHmm` | start of simulation |
| `FARSITE_END_TIME:` | `MM DD HHmm` | end of simulation |
| `FARSITE_TIMESTEP:` | minutes (integer) | no secondary step in the DLL |
| `FARSITE_DISTANCE_RES:` | meters | re-evaluate fire characteristics every this distance |
| `FARSITE_PERIMETER_RES:` | meters | vertex spacing on perimeters at each step |
| `FARSITE_IGNITION_FILE:` | path | alternative to the command-file ignition arg |

### 3.2 Optional switches (from `FarsiteInputFile.pdf`)

| Switch | Meaning / sample |
|---|---|
| `FOLIAR_MOISTURE_CONTENT:` `90` | percent; default `100` |
| `CROWN_FIRE_METHOD:` `Finney` \| `ScottReinhardt` | note the DLL string is `ScottReinhardt` (the PDF prints `ScottRhienhardt`); default Finney |
| `FARSITE_BURN_PERIODS:` `N` + N lines `MM DD HHmm HHmm` | restrict burning to per-day windows; missing ⇒ burn whole sim; periods must not overlap |
| `FARSITE_SPOT_PROBABILITY:` | 0–1 ember-survival; keep ≤ 0.1 or perimeter resolution explodes |
| `FARSITE_SPOT_IGNITION_DELAY:` | minutes delay to ignite after landing |
| `FARSITE_MINIMUM_SPOT_DISTANCE:` `30` | meters an ember must travel to start a spot fire |
| `FARSITE_SPOT_GRID_RESOLUTION:` `15` | background spotting grid; should subdivide the landscape cell (e.g. 30 → 15,10,6,5,3,2,1) |
| `FARSITE_ACCELERATION_ON:` `1` | 0/1 |
| `FARSITE_BARRIER_FILE:` path | fuels set non-burnable where a barrier crosses (DLL behavior differs from FARSITE4) |
| `FARSITE_FILL_BARRIERS:` `1` | also fill inside barrier polygons to non-burnable |
| `CUSTOM_FUELS_FILE:` path | custom fuel-model data (`.fmd`) |
| `FARSITE_ATM_FILE:` path | **gridded winds manifest** (external WindNinja), see §5 |
| `GRIDDED_WINDS_GENERATE:` `Yes`/`No` | run **embedded** WindNinja |
| `GRIDDED_WINDS_RESOLUTION:` res | grid resolution for embedded WindNinja (landscape units) |
| `GRIDDED_WINDS_DIRECTION_BIN:` deg | reuse embedded WindNinja output if direction (default tol **20°**) |
| `GRIDDED_WINDS_SPEED_BIN:` mph | reuse if speed (default tol **5 mph**) |

### 3.3 Extra switches present in `farsite.dll` (not in the PDF)

- `SPOTTING_SEED:` integer — deterministic spot-fire RNG seed (sample file uses `-1` = time-seeded/random).
- `FARSITE_MIN_IGNITION_VERTEX_DISTANCE:` meters — minimum ignition-vertex spacing.
- `GRIDDED_WINDS_DIURNAL:` `Yes`/`No` — diurnal winds for the embedded WindNinja run.

Example mandatory block (abridged from `SampleData/Farsite/FarsiteRunLog.txt`):

```
FARSITE_START_TIME: 6 20 1700
FARSITE_END_TIME: 6 21 2059
FARSITE_TIMESTEP: 60
FARSITE_DISTANCE_RES: 30
FARSITE_PERIMETER_RES: 60
FARSITE_SPOT_GRID_RESOLUTION: 15
FARSITE_SPOT_PROBABILITY: 0.035
FARSITE_SPOT_IGNITION_DELAY: 0
FARSITE_MINIMUM_SPOT_DISTANCE: 30
FARSITE_ACCELERATION_ON: 1
FARSITE_FILL_BARRIERS: 0
FARSITE_BURN_PERIODS: 2
6 20 1700 2059
6 21 1200 2059
FOLIAR_MOISTURE_CONTENT: 100.000000
CROWN_FIRE_METHOD: ScottReinhardt
```

---

## 4. Weather input

### 4.1 `RAWS` — hourly stream (recommended; used by this repo)

```
RAWS: N
RAWS_ELEVATION: <elev>
RAWS_UNITS: English    # or Metric
# Year Mth Day HHMM  Temp  RH   Pcp    WS   WDir   CC
2025 6 17 1300 76 20 0.00 3 24 0
...
```

- `Temp` °F, `RH` %, `Pcp` = hourly precip inches, `WS` mph, `WDir` azimuth (deg),
  `CC` cloud cover 0–100.
- Records must be **sequential**; FARSITE conditions fuels through the stream.
- `RAWS_ELEVATION` and `RAWS_UNITS` are **required** with `RAWS`.

**Generating this section** — `tools/hrrr_to_wxs.py` downloads HRRR and writes a
`.wxs` whose rows ARE this RAWS block (header `Year Mth Day Time Temp RH HrlyPcp
WindSpd WindDir CloudCov`). Paste the `RAWS_UNITS:`/`RAWS_ELEVATION:`/rows text
straight into the inputs file under `RAWS: <row count>`.

### 4.2 `WEATHER_DATA` / `WIND_DATA` — coarse daily + wind

```
WEATHER_DATA: Mth Day Pcp mTH xTH mT xT xH mH Elv PST PET   (sequential days)
WIND_DATA:     Mth Day Hour Speed Direction CloudCover      (ascending, hourly preferred)
```
`WEATHER_DATA_UNITS:` / `WIND_DATA_UNITS:` are `METRIC`/`ENGLISH`.

> `RAWS` **cannot** be combined with `WEATHER_DATA`/`WIND_DATA`.

---

## 5. Gridded winds (terrain-resolved, from WindNinja)

Two mutually exclusive routes (plus `RAWS` for the scalar hourly wind):

1. **External grids → `FARSITE_ATM_FILE:`** — point at a `.atm` manifest; every
   referenced speed/direction grid must sit in the **same directory**, be
   ASCII or GeoTIFF, and match the LCP **cell size, extent, datum, projection**.
   Example `.atm` (from `FireBehaviorModels/atm/pal.atm`):
   ```
   WINDS
   ENGLISH
   1 1 1600 palisades_01-01-2025_1600_56m_vel.asc palisades_01-01-2025_1600_56m_ang.asc
   ...
   ```
   Build these with `tools/runroot_to_atm.py --run-root <windninja pairs> --dem <LCP>`.
2. **Embedded WindNinja → `GRIDDED_WINDS_GENERATE: Yes`** with
   `GRIDDED_WINDS_RESOLUTION:` (and optional `GRIDDED_WINDS_DIURNAL: Yes`).
   FARSITE calls `WindNinjadll.dll` directly and writes
   `<base>_WindGrids.tif`. Prior runs are reused within the direction/speed
   bins (§3.2).

Timing contract (repo pipelines): `.atm` row times and `.wxs`/`RAWS` stamps are on
the **fire-local standard clock** and must land inside the `FARSITE_BURN_PERIODS`
windows. FARSITE keeps a wind set in force until a later `.atm` row supersedes it.

---

## 6. Outputs

All outputs derive from the command-file base name. From `runfarsite.exe`
strings, with base `out/test`:

| Output | Contents |
|---|---|
| `out/test_ArrivalTime.asc` | minutes-since-start arrival grid |
| `out/test_FlameLength.asc` | max flame length |
| `out/test_SpreadRate.asc` | rate of spread |
| `out/test_SpreadDirection.asc` | spread azimuth |
| `out/test_Intensity.asc` | fireline intensity |
| `out/test_HeatPerUnitArea.asc` | heat per unit area |
| `out/test_ReactionIntensity.asc` | reaction intensity |
| `out/test_CrownFire.asc` | crown-fire activity |
| `out/test_Ignitions.asc` | ignition grid |
| `out/test_SpotGrid.asc` | background spotting grid |
| `out/test_Timings.txt` | run bookkeeping/timings |
| `out/test_Perimeters.shp` | final + intermediate perimeters |
| `out/test_Spots.csv` / `out/test_Spots.shp` | `launchTime, launchX, launchY, landTime, landX, landY, FlightTime, Distance(m), Distance(ft)` |
| `out/test_WindGrids.tif` | embedded-WindNinja wind-field stack |
| `out/test_FarsiteOutputs.tif` | all grid layers stacked |
| `out/test_<Layer>.tif` | per-layer GeoTIFF copies |

`outputsType` selects `0` both ASCII+GeoTIFF, `1` ASCII only, `2` GeoTIFF only.
Vertices that cross the landscape edge are extinguished and counted (reported to
stdout and in `FarsiteRunLog.txt` as `Final Number Fires`, `Number Spot Fires`,
`Final Number Vertices`).

---

## 7. Full worked example

Using the repo sample exactly (Windows/Wine CWD = `FireBehaviorModels/SampleData/Farsite`):

**Command file** `FarsiteCmd.txt`:
```
..\BlueMountain\BlueMountain.tif Inputs.txt ..\BlueMountain\centerIgnit.shp 0 .\out\bluemtn 2
```

**Inputs file** `Inputs.txt` — a minimal valid run (fuel moistures exemplary;
weather short):

```
FUEL_MOISTURES_DATA: 22
0 6 7 8 60 90 16
101 6 7 8 60 90 16
...                # one line per fuel model present in the LCP
188 6 7 8 60 90 16
RAWS: 3
RAWS_ELEVATION: 3412
RAWS_UNITS: English
2026 6 20 1700 76 20 0.00 3 24 0
2026 6 20 1800 78 24 0.00 2 349 0
2026 6 20 1900 71 35 0.00 1 304 0
FARSITE_START_TIME: 6 20 1700
FARSITE_END_TIME: 6 20 2000
FARSITE_TIMESTEP: 60
FARSITE_DISTANCE_RES: 30
FARSITE_PERIMETER_RES: 60
FARSITE_ACCELERATION_ON: 1
CROWN_FIRE_METHOD: ScottReinhardt
FOLIAR_MOISTURE_CONTENT: 100
```

Then:

```
runfarsite FarsiteCmd.txt
```

Verified artifacts match exactly this shape: the real sample **is** this layout
(`BlueMountain.tif` = 9-band LCP at 30 m; `centerIgnit.shp` ignition; the
`FarsiteRunLog.txt` inputs file with 22 fuel-moisture entries, `RAWS: 218` rows,
burn periods, `GRIDDED_WINDS_GENERATE: Yes` + `GRIDDED_WINDS_RESOLUTION: 30` +
`GRIDDED_WINDS_DIURNAL: Yes`).

---

## 8. LLM-agent quick reference

- **Exactly one** `runfarsite <commandfile>` argument; one run per command-file line; 6 whitespace-separated fields per line, all required (barrier = literal `0` when none).
- The **LCP** must be the multi-band landscape (not a plain DEM); ignition is a point/polygon shapefile with sidecar `.prj`.
- Inputs-file pitfalls: `FUEL_MOISTURES_DATA` **requires model 0**; `RAWS` rows must be sequential and need `RAWS_UNITS`+`RAWS_ELEVATION`; burn-period times must lie within `START`/`END` and not overlap; `FARSITE_SPOT_PROBABILITY ≤ 0.1`.
- External gridded winds: `.atm` + grids must live **in the same folder**, grids must match the LCP exactly (cell size/extent/datum/CRS) — build with `tools/runroot_to_atm.py --dem <LCP>` which resamples WindNinja's ~56 m mesh to the LCP grid and writes matching `.prj` files.
- Browser of inputs: any string that is not a recognized `SWITCH:` is treated as an error line by the DLL parser — keep every non-comment line a valid switch.
- Emulate the run-log stream `#SELECTED FARSITE OUTPUTS` block if you want to document which layers a run writes; it is comment-only and does not toggle outputs.
- The Python pre-processors (`tools/hrrr_to_wxs.py`, `tools/runroot_to_atm.py`, `tools/calfire_ignition.py`) need the `flammap` conda env and run from the repo root; they only prepare **inputs** — `runfarsite` itself needs no Python.
- Citation for derived data: "Fire behavior calculations produced with the command line fire behavior applications developed by the Missoula Fire Sciences Laboratory, Missoula, MT" (`FireBehaviorModels/README.txt`).
