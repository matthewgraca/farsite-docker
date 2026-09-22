# tools — FARSITE weather/wind data-prep scripts

Two command-line utilities that generate FARSITE weather-stream (`.wxs`) and
Atmosphere Grid (`.atm`) inputs from public and WindNinja-computed data:

| Script | Input | Output |
|---|---|---|
| `hrrr_to_wxs.py` | HRRR sfc analysis archive (network) | FARSITE `.wxs` weather stream |
| `runroot_to_atm.py` | WindNinja run2 wind grids (`_vel.asc`/`_ang.asc`, offline) | FARSITE `.atm` + resampled wind grids |

Run both from the **repo root** (`python tools/<script>.py ...`). Any
relative path defaults or flags resolve against the run CWD; the scripts'
`--out` defaults are therefore already prefixed with `FireBehaviorModels/`
so default-omission writes into the app's sample-data tree
(`FireBehaviorModels/SampleData/Palisades/`).

`runroot_to_atm.py` imports helpers (`die`, `_STAMP_RE`, `parse_utc`,
`resolve_timezone`) from `hrrr_to_wxs.py`; the two files must stay in the
same directory. Each script's own docstring is the authority on data
contracts; `python tools/<script.py> --help` prints the full option list.

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
