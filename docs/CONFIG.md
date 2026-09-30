# Config reference — `tools/orchestrate.py`

A single human-editable TOML file is the **single source of truth** for one
FARSITE/WindNinja run: landscape ingestion, ignition, burn window, WindNinja
cfg + run, HRRR weather, FARSITE inputs/command assembly, and `runfarsite`
execution. This page documents every section, key, default, and constraint.

```bash
python tools/orchestrate.py --config config.example.toml            # run
python tools/orchestrate.py --config config.example.toml --dry-run  # plan only
```

- `--dry-run` prints every planned subprocess argv **plus the full text of
  every file that would be written** (WindNinja cfg, FARSITE inputs, command
  file) and touches nothing on disk. Use it to eyeball any value before a real run.
- `config.example.toml` is a runnable Palisades 2025 sample and the best
  starting point — copy it and adapt. Windows specifics: `WINDOWS.md`.
- Companion guides: `WindNinja-CLI.md` (WindNinja option vocabulary, §options),
  `FARSITE-CLI.md` (what the inputs file/command file mean), `tools/README.md`
  (the six underlying scripts).

---

## 0. How the config is consumed

- **Schema is closed.** Unknown top-level sections or keys `die()` at load with
  exit code 2 (typo guard). The full key surface is in the tables below — there
  is no free-form passthrough except `[windninja.options]`.
- **Defaults fill in.** Omitted keys get the defaults in the tables (schema in
  `tools/orchestrate.py:_DEFAULTS`). Omitted *sections* behave as `enable=true`
  with all-default keys — so disabling a stage is always explicit.
- **Path resolution.** Relative paths resolve against the **repo root**
  (`TOOL_DIR.parent`), *not* the run CWD. Absolute paths pass through,
  including native Windows `C:\...` (backslashes literal).
- **Command strings** (`[windninja] command`, `[farsite] command`) are split
  with `shlex(posix=False)` — double-quoted paths with spaces stay one argv
  element; backslashes are literal.
- **Resumability.** Every stage has `enable`; set `false` *and* provide its
  input override (see §8) to reuse prior outputs after a partial/network failure.
- **Exit codes.** 0 success; 2 config error; non-zero from any subprocess
  subprocess aborts the pipeline at that stage.

---

## 1. Sections at a glance

| Section | What it configures |
|---|---|
| `[output]` | run directory for all artifacts (optional override) |
| `[fire]` | CAL FIRE fire resolution → ignition seed + `fire.json` |
| `[landscape]` | LANDFIRE landscape ingestion (or reuse path) |
| `[simulation]` | UTC burn window, time zone, fuel-conditioning lead, burn periods |
| `[windninja]` | WindNinja CLI cfg + run (or reuse existing wind grids) |
| `[weather]` | HRRR → `.wxs` weather stream (or reuse) |
| `[farsite]` | FARSITE inputs/command assembly + `runfarsite` invocation |

---

## 2. `[output]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `run_dir` | path | `FireBehaviorModels/SampleData/<year>_<slug>` when `fire.enable=true`, else the `fire_json` parent | root output directory for **all** generated artifacts (ignition, windroot, `.wxs`, inputs/command files, `farsite-out`) |

Slug = fire name lowercased with spaces → `-` (e.g. `Palisades` → `palisades`).

---

## 3. `[fire]` — ignition resolution

Resolves a historical California fire via `calfire_ignition.py` (CAL FIRE FRAP +
NIFC/WFIGS IRWIN lookup) and writes the ignition seed + real reference
footprint + `fire.json`.

| Key | Type | Default | Meaning / constraint |
|---|---|---|---|
| `enable` | bool | `true` | `false` ⇒ reuse `fire_json` (and its dir's `ignition.shp`), nothing runs |
| `name` | str | — | CAL FIRE fire name, case-insensitive. **Required** when `enable=true` |
| `year` | int | — | FRAP `YEAR_` filter; pins the default run dir. **Required** when enabled |
| `index` | int | — | 0-based pick when `name` matches >1 record in a year |
| `lat`, `lon` | float | — (IRWIN lookup) | manual WGS84 ignition; **give both** to skip the WFIGS lookup (pre-IRWIN fires) |
| `crs` | str | LCP CRS | output CRS for both shapefiles (any pyproj string, e.g. `EPSG:32611`); default = the LCP's CRS so the seed matches FARSITE's landscape |
| `fire_json` | path | `<run_dir>/fire.json` | output; **required input** when `enable=false` |

`fire.json` fields (name/year/alarm/cont/lat/lon) then drive the window, time
zone, and HRRR anchor. The script prints a ready-to-run `hrrr_to_wxs.py`
command for reference.

**Ignition burnability preflight** (runs between fire and window): the WFIGS
seed's LCP fuel cell is checked on band 4. If it is non-burnable — fuel model
`0`, `91–99` (Scott & Burgan NB1–9), or nodata — FARSITE would grow nothing, so
the seed is **nudged to the nearest burnable cell** (ring search, refusal beyond
5 km), the moved `ignition.shp` is rewritten (the file FARSITE's command file
points at), and the change is recorded under `fire.json["ignition_adjusted"]`
(`original`/`adjusted` lat-lon, `offset_m`, `original_fuel`, `adjusted_fuel`).
The top-level `fire.json` `lat`/`lon` (the weather/elevation anchor) is left at
the **original WFIGS coordinate** — only the seed moves. `--dry-run` prints what
would happen without writing.

---

## 4. `[landscape]` — LCP + DEM

FARSITE landscape (LCP) and the single-band DEM WindNinja reads elevation from.

| Key | Type | Default | Meaning / constraint |
|---|---|---|---|
| `enable` | bool | `true` | `false` ⇒ reuse `lcp`; nothing runs, no LANDFIRE network |
| `lcp` | path | — | reuse path; **required** when `enable=false` |
| `bbox` | str | — | `"W S E N"` WGS84; **exactly one** of `bbox`/`mapzone` required when enabled |
| `mapzone` | int | — | whole LANDFIRE map zone (1–10, 12–80, 98–99); region-sized, slow |
| `email` | str | — | LFPS-required requester email (open API, not a token). **Required** when enabled |
| `version` | int | `2024` | LANDFIRE release for the annual fuel/canopy layers |
| `fuel_model` | str | `fbfm40` | band-4 fuel classification: `fbfm40` or `fbfm13` |
| `resolution` | int | `30` | output m; 30 = native, `<30` rejected (LFPS only coarsens) |
| `dem_out` | path | `<run_dir>/<slug>-dem.tif` | optional single-band int16 elevation GeoTIFF (WindNinja `elevation_file`); omit for the band-1 auto-extract at the default path |

Ingested LCP = `<run_dir>/landscape.tif`, **forced to EPSG:5070** (the tool's
hard contract) — the ignition CRS follows via `[fire] crs` default, so FARSITE
runs in 5070. Default LFPS stack is **8 bands** (elev/slope/aspect/FBFM40/CC/
CH/CBH/CBD); the repo's `test/data/palisades.tif` (55.53 m) is the sample LCP.

---

## 5. `[simulation]` — window, clock, conditioning

| Key | Type | Default | Meaning / constraint |
|---|---|---|---|
| `start`, `end` | str | fire.json alarm/cont | UTC whole-hour instants `YYYY-MM-DDTHH:MM` (optional trailing `Z`, no offset); give both or neither. Missing ⇒ derived from `fire.json` |
| `row_timezone` | str | derived from ignition point | IANA clock for `.wxs`/`.atm` row labels and FARSITE local start/end (standard time; e.g. `America/Los_Angeles`) |
| `lead_days` | int | `0` | conditioning **days** of meteorology prepended before `start` (hrrr emits leading RAWS rows; FARSITE conditions fuels). ~3–7 conventional, 0 = smallest sample |
| `burn_periods` | list[list[str]] | `[]` | FARSITE per-day burn windows `[start, end]` of `"M D HHMM"`; each written `M D HHMM HHMM` with the end hour on the start's day — span a night with one entry per day. Empty ⇒ burn the whole window |

Example: `burn_periods = [["1 6 1600", "1 6 2059"]]`.

---

## 6. `[windninja]` — gridded winds

| Key | Type | Default | Meaning / constraint |
|---|---|---|---|
| `enable` | bool | `true` | `false` ⇒ skip the binary entirely; the `.atm` delivery is still checked by the atmosphere stage |
| `command` | str | `""` | native `WindNinja_cli.exe` path (quoted if spaces) or `wine C:/...` on Linux/WSL. Empty ⇒ write `<run>/windroot/<slug>.cfg` + print the manual command; the run pauses until the `.atm` exists (re-run the same config to resume) |
| `run_root` | dir | `<run_dir>/windroot` | dir of per-hour `_vel.asc`/`_ang.asc` pairs + the single `.atm`; point this at a dir with existing pairs (e.g. `FireBehaviorModels/atm`) to reuse committed grids and skip the CLI entirely |
| `threads` | int | `4` | `num_threads` for the CLI — **also the ceiling on how many hourly runs solve concurrently**. Each run is one thread and holds one full-domain mesh (mesh = LCP cell size), so peak RAM ≈ `min(hours, threads)` × per-mesh. More threads = more concurrent meshes = more memory, not just speed |
| `options` | table | `{}` | **passthrough** → every other WindNinja cfg key (see `WindNinja-CLI.md`) |

`[windninja.options]` is the only free-form table — keys are rendered as
`k = v` (`bool`→`true/false`, `int`, `float`, `str`; scalars only) and merged
before the injected keys below, which **always win**:

```toml
[windninja.options]
initialization_method = "wxModelInitialization"   # REQUIRED when a cfg is written
wx_model_type = "PASTCAST-GCP-HRRR-CONUS-3-KM"    # archived-HRRR pastcast
start_year = 2025   start_month = 1   start_day = 6     # PASTCAST window: whole
start_hour = 16     start_minute = 0                    # hours on the injected
stop_year  = 2025   stop_month  = 1   stop_day  = 6     # time_zone clock; NOT
stop_hour  = 22     stop_minute = 0                     # forecast_duration (4.0 rejects both)
vegetation = "grass"                                    # grass | brush | trees
input_wind_height = 10.0     units_input_wind_height = "ft"
output_wind_height = 20.0    units_output_wind_height = "ft"   # .atm: 20ft+mph OR 10m+kph only
output_speed_units = "mph"
diurnal_winds = "true"
```

**Injected and always-win:** `elevation_file`, `output_path`, `time_zone`,
`num_threads`, `write_ascii_output = true`, `write_farsite_atm = true`,
`mesh_resolution` (= LCP cell size), `units_mesh_resolution = m`. The final two
put WindNinja's native `.atm` grids on the LCP grid, which FARSITE requires.

**WindNinja 4.0 notes** (full detail in `WindNinja-CLI.md` §4.1):
- `PASTCAST-GCP-HRRR-CONUS-3-KM` downloads archived HRRR from
  `storage.googleapis.com/...`; the `ninjastorm.firelab.org` DNS ping at startup
  is a harmless non-fatal version check.
- `.atm` output settings must be **20 ft + mph** or **10 m + kph** — anything
  else aborts the run.
- `momentum_flag = true` (NINJAFOAM builds only) activates the full
  momentum-conserving solver: ~1–2 orders slower/more memory, coarsen + single
  hour + fit `num_threads` ≤ physical cores.

---

## 7. `[weather]` — HRRR → `.wxs`

| Key | Type | Default | Meaning / constraint |
|---|---|---|---|
| `enable` | bool | `true` | `false` ⇒ reuse `wxs`, nothing downloads |
| `wxs` | path | `<run_dir>/<slug>-hrrr.wxs` | reuse path; **required** when `enable=false` |
| `cache_dir` | dir | `FireBehaviorModels/.cache/hrrr` | per-hour grib cache; already-cached hours skip the network |
| `threads` | int | `8` | Phase-A download thread count |
| `elevation_tol_ft` | int | `500` | elevation-match tolerance when choosing the sampled HRRR cell |

The window, time zone, HRRR anchor (ignition point), and `lead_days` are all
injected from `[simulation]`/`[fire]`; the wind-coverage gate requires a
WindNinja `_vel.asc`/`_ang.asc` pair for every burn-window hour (lead hours do
not).

---

## 8. `[farsite]` — inputs assembly + run

### 8.1 The switch surface

| Key | Type | Default | Inputs-file switch / meaning |
|---|---|---|---|
| `enable` | bool | `true` | `false` ⇒ **neither assembly nor `runfarsite`** — prior `-FarsiteInputs`/`-FarsiteCmd` files are left untouched (the resumed hand-drive path); `run`-side is irrelevant while disabled |
| `run` | bool | **`true`** (note!) | `true` ⇒ invoke `runfarsite` (needs `command`); `false` ⇒ assemble only |
| `command` | str | `""` | native `runfarsite.exe` path (quoted if spaces). **Required** when `run=true` |
| `cwd` | dir | repo root | CWD for `runfarsite` |
| `barrier` | path\|`"0"` | `"0"` | barrier shapefile, or `"0"` for none |
| `out_base` | path | `<run_dir>/farsite-out/<slug>` | FARSITE output base name (no extension) |
| `outputs_type` | int | `2` | `0` = ASCII + GeoTIFF, `1` = ASCII only, `2` = GeoTIFF only |
| `timestep` | int | `60` | `FARSITE_TIMESTEP` (minutes) |
| `distance_res` | int | `30` | `FARSITE_DISTANCE_RES` (m) |
| `perimeter_res` | int | `60` | `FARSITE_PERIMETER_RES` (m) |
| `spot_grid_resolution` | int | `15` | `FARSITE_SPOT_GRID_RESOLUTION`; should subdivide the LCP cell (30 → 15/10/…) |
| `spot_probability` | float | `0.035` | `FARSITE_SPOT_PROBABILITY` (0–1, ember survival); **keep ≤ 0.1** or perimeter resolution explodes |
| `spot_ignition_delay` | int | `0` | `FARSITE_SPOT_IGNITION_DELAY` (minutes) |
| `minimum_spot_distance` | int | `30` | `FARSITE_MINIMUM_SPOT_DISTANCE` (m) |
| `acceleration_on` | int | `1` | `FARSITE_ACCELERATION_ON` (0/1) |
| `fill_barriers` | int | `0` | `FARSITE_FILL_BARRIERS` (0/1) |
| `foliar_moisture_content` | float | `100.0` | `FOLIAR_MOISTURE_CONTENT` (%) |
| `crown_fire_method` | str | `ScottReinhardt` | `Finney` or `ScottReinhardt` (DLL string — note two `t`s) |

### 8.2 `[farsite.fuel_moistures]` — per-model overrides

Key = fuel-model **int** (must parse as `int`), value = `"F1 F10 F100
FMLiveHerb FMLiveWoody"`. Models not listed get the default tuple.

```toml
[farsite.fuel_moistures]
0   = "6 7 8 60 90 16"     # fuel model 0 required; 6 7 8 60 90 16 = repo default
122 = "6 7 8 60 90 16"
```

Models are auto-derived as the LCP band-4 distinct values **plus `0`**; the
derived set + `[farsite.fuel_moistures]` overrides become the
`FUEL_MOISTURES_DATA` block.

### 8.3 Derived inputs (not config settable here)

Generated by the orchestrator from the stages above: `FARSITE_START_TIME` /
`FARSITE_END_TIME` (local standard clock from `[simulation]`), `FARSITE_BURN_PERIODS`
(from `[simulation] burn_periods`), the `RAWS:` block (from the `.wxs`),
`FARSITE_ATM_FILE` (the WindNinja `.atm`). **Not surfaced** as config keys:
`SPOTTING_SEED`, `CUSTOM_FUELS_FILE`, `GRIDDED_WINDS_GENERATE`/`GRIDDED_WINDS_*`
(`RAWS`-based runs don't use embedded WindNinja). To set one: run once with
`run=false` to assemble, hand-edit `-FarsiteInputs.txt`/`-FarsiteCmd.txt`, then
**re-run with `[farsite] enable=false` + `run=true`** — the orchestrator skips
assembly (files untouched) and fires `runfarsite` on your edited command file
(see §9).

---

## 9. Resuming after a partial / network failure

Every stage is individually resumable: fix the cause, set `enable=false` **and**
provide the listed override, re-run the same `--config`.

| Stage | `enable=false` also requires |
|---|---|
| `[landscape]` | `lcp` (path to the produced/reused LCP) |
| `[fire]` | `fire_json` (path to an existing `fire.json`) |
| `[windninja]` | (nothing) — existing pairs under `run_root` are reused by the atmosphere stage |
| `[weather]` | `wxs` (path to an existing `.wxs`) |
| `[farsite]` | (nothing) — skips assembly **and** run; combine with a prior `run=false` pass to hand-edit, then re-run with `enable=false` + `run=true` to fire `runfarsite` on your untouched files |

Note the `[farsite] run` default is **`true`** — a config that sets
`enable=true` but omits `run` and `command` fails at assembly time, which is why
the example sets `run = false` explicitly.

---

## 10. Options not surfaced by the orchestrator

Orchestrate passes only the keys above to the sub-scripts; some script options
are intentionally fixed or available only by running the script directly
(see `tools/README.md` per-script docs):

| Script | Script-only options (not config keys) |
|---|---|
| `ingest_landscape.py` | `--layers`, `--output-projection` (forced `5070` by the tool anyway), `--max-wait`, `--poll-interval`, `--keep-zip` |
| `hrrr_to_wxs.py` | `--dump-dir`, `--plot`, `--lat/--lon` (injected from ignition), `--row-timezone` (from `[simulation]`) |
| `calfire_ignition.py` | — (all surfaced via `[fire]`) |
| `runroot_to_atm.py` | `--units`, `--verify` (not part of the orchestrated path — WindNinja's native `.atm` is verified directly) |

---

## 11. Smoke & verify

```bash
python -m pytest -m "not integration"                       # offline unit tests
python tools/orchestrate.py --config config.example.toml --dry-run   # plan + file texts
python tools/orchestrate.py --config config.example.toml    # the run
python tools/compare_perimeter.py --run-dir FireBehaviorModels/SampleData/2025_palisades \
  --plot overlay.png --json result.json                     # IoU vs real footprint
```
