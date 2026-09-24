# WindNinja CLI — Usage Guide

WindNinja computes terrain-resolved surface wind fields (speed + direction at a
specified height above ground) from coarse weather-model or point/domain inputs.
This guide covers the **command-line interface** (CLI) and associated
environment/library requirements. It was built from:

- `FireBehaviorModels/bin/WindNinjadll.dll` — the WindNinja 3.11.0 engine
  shipped in this repository (version/release string/SCM from DLL strings).
- The official WindNinja CLI instructions
  (`doc/CLI_instructions.pdf` in the windninja project).
- In-repo workflows that consume WindNinja output:
  `FireBehaviorModels/run2/` (a raw run), `FireBehaviorModels/atm/` and
  `FireBehaviorModels/SampleData/Palisades/` (generated grids/`.atm`/`.wxs`),
  and `tools/hrrr_to_wxs.py` / `tools/runroot_to_atm.py`.

Second companion guide: `FARSITE-CLI.md` (how FARSITE consumes WindNinja output).

---

## 1. What ships where

In **this** repository WindNinja is present **only as a shared library**:

| Artifact | Location | Purpose |
|---|---|---|
| `WindNinjadll.dll` | `FireBehaviorModels/bin/` | WindNinja 3.11.0 engine (2026/06/25 build, SCM `fd9c3e50…`) |
| `date_time_zonespec.csv`, `tz_world.zip` | `FireBehaviorModels/bin/share/windninja-data/` | time-zone database WindNinja reads at startup |
| generated run | `FireBehaviorModels/run2/NINJAFOAM_palisades_15640_268/` | an OpenFOAM("NINJAFOAM") momentum-solver case WindNinja wrote (12-proc decompose, `simpleFoam`, `sample` → `postProcessing/surfaces`) |
| HRRR input tile | `FireBehaviorModels/run2/PASTCAST-GCP-HRRR-CONUS-3-KM-palisades/` | a packaged HRRR "pastcast" (from the GCP archive): zip of per-hour 3-km GeoTIFFs, 4 bands each (surface temp, U, V, cloud cover) — the field set WindNinja's HRRR downloader uses; a local wx-model forecast input to WindNinja |

The **standalone CLI executable** — `WindNinja_cli` (`WindNinja_cli.exe` on
Windows) — is **not** in this repo; it ships in the official WindNinja
distribution (same engine/option vocabulary as `WindNinja.dll` here, so every
option documented below applies to both). Ways to get a CLI:

- Official WindNinja installer/binaries: https://firelab.github.io/windninja/ (Windows: `bin/WindNinja_cli.exe`; Linux: build or binary release).
- In this repo the engine is also driven **embedded inside `runfarsite`** (FARSITE's `GRIDDED_WINDS_GENERATE`), see [§6](#6-embedded-use-from-farsite) and `FARSITE-CLI.md`.
- Python wrapper used by some pipelines: `gagreene/WindNinja`.

> Version check on any build: `WindNinja_cli --version` prints the version,
> SCM, and release date. The DLL here reports `3.11.0`.

---

## 2. Environment & library requirements

The CLI loads the full DLL family in its `bin` directory. For this repo's DLL:

- **All 75 sibling DLLs** in `FireBehaviorModels/bin/` must be resolvable at
  runtime — at minimum the GDAL stack (`gdal.dll`, `geos.dll`, `proj_9.dll`,
  `geotiff.dll`, `tiff.dll`), `libcurl.dll` (model/elevation downloads),
  `boost_*` (option parsing), plus the runtime
  (`concrt140.dll`/`vcruntime`, `icu`, `sqlite3`, `hdf5`, `libpng`, `zlib`, …).
- **PATH** must include `FireBehaviorModels/bin`.
- **`WINDNINJA_DATA`** must point at `FireBehaviorModels/bin/share/windninja-data`.
  If this is unset WindNinja fails: `Could not initialize WindNinja, try setting
  WINDNINJA_DATA`.
- **`GDAL_DATA`** → `FireBehaviorModels/bin/share/gdal-data`.
- **`PROJ_LIB`** → `FireBehaviorModels/bin/share/proj`.

On Windows, `FireBehaviorModels/SetEnv.bat` sets all four. Outside Windows the
CLI must run under a runtime that loads the DLLs (e.g. Wine on Linux) or use a
native Linux WindNinja build that bundles the same data files. Set
`WINDNINJA_DATA`/`GDAL_DATA`/`PROJ_LIB` explicitly when scripting; do **not**
rely on a shell profile.

Input data requirements:

- **DEM/elevation**: `*.asc`, `*.lcp`, `*.tif`, `*.img`, or `*.vrt`; must carry
  a **projection/spatial reference** (WindNinja converts geographic to UTM
  automatically). Alternatively `fetch_elevation` downloads one over HTTPS.
- **Weather model forecasts** (for `wxModelInitialization`): a projected wind/temp/cloud forecast — raw GRIB2/NetCDF via `forecast_filename`, or a WindNinja-readable regridded package such as the 3-km PASTCAST tile above.
- **Network access** only when fetching forecasts, elevation, or station data;
  a fully-local run needs no internet.

---

## 3. Invocation syntax

The CLI accepts options three ways, freely mixed:

```bat
:: 1) a configuration file (text; '#' starts a comment)
WindNinja_cli C:\path\to\cli_wxModelInitialization_diurnal.cfg

:: 2) inline options, space- or '='-separated
WindNinja_cli --num_threads 4 --vegetation=trees --output_wind_height 20

:: 3) both: config file + inline overrides (inline wins on conflict)
WindNinja_cli run.cfg --elevation_file C:/data/fire.asc --vegetation grass

:: response file (same effect as a config file)
WindNinja_cli @options.txt
```

Generic meta-options (`--help`, `--version`, `--citation`,
`--runtime_options`, `--config_file`) and a heuristic: typing
`WindNinja_cli` with no file prints available options). `--runtime_options`
dumps `config_options.csv` from the WindNinja data dir — a table of
*runtime/environment* switches (`WINDNINJA_DATA`, `NINJA_FILL_DEM_NO_DATA`,
`CPL_DEBUG`, GDAL options, solver/env knobs), not the simulation option list;
the simulation options themselves come from the `--help`/bare invocation dump.

Config-file format (official CLI docs):

```
# comment to end of line
num_threads = 12
elevation_file = C:/XXXX/missoula valley.tif
initialization_method = wxModelInitialization
time_zone = America/Denver
wx_model_type = UCAR-NAM-CONUS-12-KM
forecast_duration = 100
output_wind_height = 20.0
units_output_wind_height = ft
vegetation = trees
diurnal_winds = true
mesh_resolution = 250.0
units_mesh_resolution = m
write_goog_output = true
write_shapefile_output = true
write_ascii_output = true
write_farsite_atm = true
write_wx_model_goog_output = true
write_wx_model_shapefile_output = true
write_wx_model_ascii_output = true
```

---

## 4. Initialization methods (required-top-level options)

Every run picks **exactly one** `initialization_method` (values verbatim from
the DLL): `domainAverageInitialization`, `pointInitialization`,
`wxModelInitialization`, `griddedInitialization`.

| Method | Allowed inputs | Typical purpose |
|---|---|---|
| `domainAverageInitialization` | `input_speed`, `input_direction`, `input_wind_height`, `units_input_wind_height` (+ diurnal: `month`/`day`/`hour`/`minute`, `uni_air_temp`, `uni_cloud_cover`) | one uniform wind everywhere, terrain-modulated |
| `pointInitialization` | weather stations: `fetch_station=true`, `fetch_type=bbox`/`stid`, `wx_station_filename`, or point-ini table (station list w/ radius of influence); `number_time_steps`, start/stop `*_year/_month/_day/_hour/_minute` | mesonet observations interpolated across the domain |
| `wxModelInitialization` | `wx_model_type`, `time_zone`, start/stop times, `forecast_duration` (or `forecast_filename` / `forecast_time`) | drive with NWS model output |
| `griddedInitialization` | `input_speed_grid` + `input_dir_grid` (ASCII grids), start/stop times | pre-computed wind rasters, e.g. a coarse run resampled into a fine mesh |

Common required-ish options across methods:

- `elevation_file` **or** `fetch_elevation` (one is mandatory).
- `time_zone` (IANA; e.g. `America/Denver`, or `auto-detect`; values are in
  `windninja-data/date_time_zonespec.csv`). Needed for any time-labelled run.
- `vegetation`: `grass`, `brush`, or `trees`.
- `input_wind_height`/`units_input_wind_height` and
  `output_wind_height`/`units_output_wind_height` (`ft`/`m`).
- `mesh_resolution`/`units_mesh_resolution` **or** `mesh_choice`
  (`coarse`/`medium`/`fine`); leave both unset to use the WindNinja default mesh.
- Simulation window: `start_*`/`stop_*` (year…minute) or
  `number_time_steps` (point runs).

Wind value options: `input_speed`, `input_speed_units`
(`mps`/`mph`/`kph`/`kts`), `output_speed_units` (same value set), and
`input_direction` (degrees, 0–359/360=north). For weather-model runs the speed
and direction are derived from the forecast, not set manually.

`wx_model_type` choices exposed by this DLL (UCAR download names):
`UCAR-GFS-GLOBAL-0.5-DEG`, `UCAR-NDFD-CONUS-2.5-KM`,
`UCAR-RAP-CONUS-13-KM`, `UCAR-NAM-CONUS-12-KM`, `UCAR-NAM-ALASKA-11-KM`
(and companions); the internal forecast-model ids include `gfs_global`,
`hrrr_alaska`, `hrrr_conus`, `hrrr_conus_sub`,
`hires_arw_alaska/conus/guam/hawaii/puerto_rico`,
`nam_alaska/conus/north_america/nest_*`, `rap_conus`, `rap_north_america`,
and HRRR sub-hourly variants. Corresponding fields downloaded are
`TCDC,TMP,UGRD,VGRD` at `2_m_above_ground`, `10_m_above_ground`.

**Mutual exclusivity / requireds**: WindNinja validates mutually exclusive and
missing options and prints a diagnostic instead of running; no run proceeds
until the option set is consistent. When scripting, treat the error stream as
contract, not text to parse for output.

---

## 5. Full option reference (from the DLL's option table)

> **Version skew.** The option names below reflect the **3.11.0** DLL shipped in
> this repo (`FireBehaviorModels/bin/WindNinjadll.dll`). The current
> `windninja/` source tree (v3.13 / 4.0-dev) renamed the two ASCII sub-options:
> `ascii_out_utm` → `ascii_out_proj` and `ascii_out_4326` → `ascii_out_geog`
> (rename landed 2025-07-31, after v3.12). It also adds options absent from the
> 3.11 DLL: `write_geotiff_output`/`write_wx_model_geotiff_output` (+
> `geotiff_out_resolution`/`units_geotiff_out_resolution`),
> `goog_out_speed_interval_scaling`, `goog_out_use_consistent_color_scale`, and
> (build-dependent) `compute_friction_velocity`/`friction_velocity_calculation_method`,
> `compute_emissions`/`fire_perimeter_file`, and the NINJAFOAM momentum set
> `existing_case_directory`/`momentum_flag`/`number_of_iterations`/`mesh_count`/
> `turbulence_output_flag`. Check a build's exact set with its own `--help`.

Descriptions are WindNinja's own help text; option names are the config-file /
CLI keys verbatim.

### Generic (meta) options
| Option | Meaning |
|---|---|
| `version` | print version (`3.11.0`, SCM, release date) |
| `config_file` | config file path (a bare filename argument also works) |
| `response_file` | response file; also usable as `@name` |
| `citation` | print the required citation |
| `runtime_options` | print all available configuration options |

### Simulation options — input
| Option | Meaning |
|---|---|
| `num_threads` | threads for the mass/momentum solver |
| `elevation_file` | input elevation: `*.asc, *.lcp, *.tif, *.img`; must be projected |
| `fetch_elevation` | download an elevation file (then used as domain) |
| `north`, `south`, `east`, `west` | bbox of the elevation download |
| `x_center`, `y_center` | center of the download domain |
| `x_buffer`, `y_buffer`, `buffer_units` | distances out from center (`kilometers`, `miles`) |
| `elevation_source` | source for elevation download (e.g. `srtm`) |
| `initialization_method` | one of the four methods in §4 |
| `time_zone` | IANA name or `auto-detect` |
| `wx_model_type` | NWS model (see §4 list) |
| `forecast_duration` | hours of forecast to download |
| `forecast_filename` | already-downloaded forecast file (GRIB2/NC) |
| `forecast_time` | specific UTC run time, format `20200131T180000`; repeatable |
| `match_points` | match simulation to points (true/false) |
| `input_speed`, `input_speed_units` | uniform speed + its units (`mps/mph/kph/kts`) |
| `output_speed_units` | units of written wind fields |
| `input_direction` | uniform wind direction (deg) |
| `input_speed_grid`, `input_dir_grid` | ASCII rasters for `griddedInitialization` |
| `uni_air_temp`, `air_temp_units` | surface air temp and units (`K,C,R,F`) |
| `uni_cloud_cover`, `cloud_cover_units` | cloud cover and units (`fraction/percent/canopy_category`) |
| `start_year…start_minute`, `stop_year…stop_minute` | simulation window |
| `number_time_steps` | point-init timestep count |
| `fetch_station` | download station file from Mesonet API (true/false) |
| `fetch_metadata`, `metadata_filename` | station-metadata fetch + output file |
| `fetch_type` | `bbox` (by bounding box) or `stid` (by station ID) |
| `fetch_current_station_data` | latest data (true) vs timeseries (false) |
| `station_buffer`, `station_buffer_units` | fetch radius around DEM |
| `fetch_station_name` | list of station IDs |
| `wx_station_filename` | input weather-station file |
| `input_wind_height`, `units_input_wind_height` | input speed height above vegetation (`ft/m`) |
| `output_wind_height`, `units_output_wind_height` | output height above vegetation (`ft/m`) |
| `vegetation` | `grass`, `brush`, `trees` |
| `diurnal_winds` | include diurnal winds (true/false) |
| `month`, `day`, `hour`, `minute` | simulation time for domain-average diurnal runs |
| `mesh_choice` | `coarse`, `medium`, `fine` |
| `mesh_resolution`, `units_mesh_resolution` | explicit mesh size (`ft/m`) |
| `output_buffer_clipping` | percent clipped off output-file buffers |
| `non_neutral_stability` | use non-neutral stability (true/false) |
| `alpha_stability` | stability exponent; valid range `0 < alpha <= 5` |
| `input_points_file` | file of `lat,long,z` (z m above ground) for point sampling |
| `output_points_file` | file receiving sampled points |

### Output options
| Option | Meaning |
|---|---|
| `write_ascii_output` | write ASCII fire behavior files (true/false) |
| `ascii_out_aaigrid` | AAIGRID format (default true) |
| `ascii_out_json` | JSON ASCII grids (default false) |
| `ascii_out_4326` | write in EPSG:4326 lat/lon (default false) |
| `ascii_out_utm` | write in UTM northing/easting (default true) |
| `ascii_out_uv` | write u,v vector components (default false) |
| `ascii_out_resolution`, `units_ascii_out_resolution` | grid size for ASCII outputs (`-1` = mesh res) |
| `write_shapefile_output` | plus `shape_out_resolution`/`units_shape_out_resolution` |
| `write_goog_output` | Google-Earth KMZ (plus `goog_out_resolution`, `goog_out_color_scheme`, `goog_out_vector_scaling`) |
| `write_wx_model_ascii_output` / `_shapefile_output` / `_goog_output` | same outputs for raw wx-model forecast |
| `write_vtk_output` | VTK of the (analytic/mass) mesh |
| `write_pdf_output` | geospatial PDF (`pdf_size`, `pdf_width/height`, `pdf_linewidth`, `pdf_basemap`, `pdf_out_resolution`) |
| `write_farsite_atm` | write a **FARSITE `.atm`** manifest + wind grids |
| `write_wx_station_kml`, `write_wx_station_csv` | point-init diagnostics |
| `output_path` | directory for outputs |

### Output naming (observed in this repo)
Terrain-resolved per-hour grids are written with a run-labeled basename the
repo tooling relies on:

```
<DEM-basename>_MM-DD-YYYY_HHMM_<res>m_vel.asc      # wind speed, m/s
<DEM-basename>_MM-DD-YYYY_HHMM_<res>m_ang.asc      # wind direction, deg (360=north)
<...>_vel.prj / _ang.prj                            # WKT projection (shapefile-family .prj)
```

Example (from `FireBehaviorModels/atm/`): `palisades_01-01-2025_1600_56m_vel.asc`.
When `write_farsite_atm=true`, the same directory gets an `.atm` whose rows
reference these grids. The companion scripts `tools/hrrr_to_wxs.py --run-root`
and `tools/runroot_to_atm.py` discover these pairs by regex
`MM-DD-YYYY_HHMM`, require both halves per hour, and ignore `PASTCAST-GCP-*`
files (which are WindNinja **inputs**, not outputs).

---

## 6. Embedded use from FARSITE

`runfarsite` embeds this same DLL (checked: `farsite.dll` imports
`WindNinjadll.dll`). When the FARSITE inputs file enables
`GRIDDED_WINDS_GENERATE: Yes`, FARSITE drives a WindNinja forecast/domain run
at `GRIDDED_WINDS_RESOLUTION`, optionally with `GRIDDED_WINDS_DIURNAL: Yes`,
and writes `%OUTPUTBASE%_WindGrids.tif`. Runs are cached: a new gridded-wind
request is reused when wind direction is within `GRIDDED_WINDS_DIRECTION_BIN`
(degs, default tolerance **20°**) and speed within `GRIDDED_WINDS_SPEED_BIN`
(mph, default **5 mph**) of a previous request.

Two in-repo pipelines produce WindNinja grids for **external** FARSITE use (the
`.atm` route, `FARSITE_ATM_FILE`):

1. Configure a WindNinja CLI run with `initialize_method=wxModelInitialization`,
   `wx_model_type=…HRRR…`, `forecast_time`, `elevation_file=<fire DEM or LCP>`,
   and `write_ascii_output=true` → per-hour `_vel.asc`/`_ang.asc` pairs.
2. `python tools/runroot_to_atm.py --run-root <dir> --dem <LCP> …`
   converts m/s → mph, resamples the ~56 m WindNinja mesh onto the 30 m LCP
   grid (FlamMap requires wind grids to match the landscape cell size/extent
   and datum), writes `.prj`, and emits the `.atm` manifest.
3. `python tools/hrrr_to_wxs.py --run-root <same dir> --dem <LCP> …`
   builds the matching hourly weather stream (`.wxs` text pasted into the
   FARSITE `RAWS` section); its wind-coverage gate fails a burn hour that has
   no WindNinja pair.

See `FARSITE-CLI.md` for the `RAWS`/`FARSITE_ATM_FILE` input-switch details.

---

## 7. Common failure modes & gotchas

- **`Could not initialize WindNinja, try setting WINDNINJA_DATA`** — missing/invalid `WINDNINJA_DATA` (or the data files inside it).
- **"DEM does not contain spatial reference information"** — elevation must be projected; WindNinja cannot operate on a raw elevation raster.
- **"Cannot be identified as a valid weather model initialization file"** — `forecast_filename` is not a readable projected GRIB/NC file.
- **"Cannot initialize with a forecast file in lat/long spacing"** — forecasts must be in a projected CRS.
- **`pointInitialization` + momentum solver** — when the build has the
  NINJAFOAM momentum solver enabled (`momentum_flag=true`), the CLI rejects
  `pointInitialization` (`'pointInitialization' is not a valid
  'initialization_method' if the momentum solver is enabled`); use
  domain-average or wx-model init for momentum runs.
- **`FARSITE atmosphere file (*.atm) cannot be written because the speed units and/or output wind height above ground are incorrect`** — to write a FARSITE `.atm`, `output_speed_units` must be `mph` (or km/h via `output_speed_units=kph` + FARSITE METRIC atm) and `output_wind_height` must be the FARSITE reference height (20 ft).
- **Time mismatches** — `forecast_time` is UTC; `time_zone` maps it to local for diurnal logic. The repo `.wxs`/`.atm` rows are stamped on the fire-local **standard** clock; keep the same clock between WindNinja frames, `.wxs`, and FARSITE burn periods.
- **Speed units ambiguity** — WindNinja native ASCII speed grids are **m/s**; the repo tooling converts them before FARSITE (which expects mph grids for an `ENGLISH .atm`) with `runroot_to_atm.py --units mph`.

---

## 8. LLM-agent quick reference

- To learn a given build's exact options: `WindNinja_cli --runtime_options` (dump) or `WindNinja_cli --help`/`--version`.
- Options are validated; treat a non-zero exit with the printed diagnostic as the contract error, and fix the *option set*, not the output.
- Generate config files programmatically as `name = value` lines, `#`-commentable; never emit `--` inside a config file (only on the command line).
- Wind-Ninja naming for the repo tooling must stay `<base>_MM-DD-YYYY_HHMM_.._vel.asc` / `_ang.asc`, hourly, both present, in one directory.
- All paths used by this repo's helpers are relative to the run CWD; helpers run from the repo root (`python tools/<script>.py`).
- Conda runtime for the Python helpers: `environment.yml` (env name `flammap`; requires numpy/rasterio et al., Python 3.14).
- Cite per `--citation`:
  Forthofer, J.M., Butler, B.W., Wagenbrenner, N.S., 2014. A comparison of three approaches for simulating fine-scale surface winds in support of wildland fire management. Part I. *Int. J. Wildland Fire*, 23:969–931. doi:10.1071/WF12089.
