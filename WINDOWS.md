# Windows support

Every Python tool and the orchestrator (`tools/`) run natively on Windows
(Python ≥ 3.11), invoking the Windows-native binaries through the `[windninja]
command` / `[farsite] command` config fields. The pipeline tail
(`runfarsite.exe`, `WindNinja_cli.exe`) is Windows-native, and nothing here
forces Wine on a Windows host. This file is the setup recipe for a win-64 box.

## Why not `environment.yml`

`environment.yml` is a fully-pinned **Linux-only** conda export. Packages such
as `_openmp_mutex`, `libgcc-ng`, `ld_impl_linux-64`, and `xorg-*` do not exist
on `win-64`, so `conda env create -f environment.yml` cannot solve on Windows.
Create the env with the recipe below instead.

## Windows conda env

win-64, conda-forge (default channel):

```bat
conda create -n flammap -c conda-forge python=3.12 rasterio numpy xarray cfgrib eccodes herbie-data pyshp pyproj requests timezonefinder tzdata pandas scipy matplotlib pytest tqdm
```

- `python=3.12` — the boring LTS pick.
- `tzdata` is **required** on Windows: `zoneinfo` (used by `hrrr_to_wxs.py`)
  reads the IANA timezone database from it. The committed env already ships it.
- Contingency — if any package is unavailable on win-64, drop it from the conda
  line and `pip install <name>` inside the env (documented manual fallback, not
  a code dependency).

Activate with `conda activate flammap`.

## Binary runtime environment

The orchestrator's subprocesses inherit the parent shell's environment, so the
native binaries need their runtime variables in **the same terminal** that
launches `orchestrate.py`. Either run `FireBehaviorModels\SetEnv.bat` in that
terminal, or set the three data variables yourself:

| Variable | Value |
|---|---|
| `PATH` | *(not required)* — the exes load their DLL family from their own directory; prepending `bin` can shadow the conda GDAL DLLs (see note below) |
| `GDAL_DATA` | `FireBehaviorModels\bin\share\gdal-data` |
| `PROJ_LIB` | `FireBehaviorModels\bin\share\proj` |
| `WINDNINJA_DATA` | `FireBehaviorModels\bin\share\windninja-data` |

cmd equivalent (paths under `C:\path\to`):

```bat
set "GDAL_DATA=C:\path\to\FireBehaviorModels\bin\share\gdal-data"
set "PROJ_LIB=C:\path\to\FireBehaviorModels\bin\share\proj"
set "WINDNINJA_DATA=C:\path\to\FireBehaviorModels\bin\share\windninja-data"
```

Required for the native `runfarsite.exe` / `WindNinja_cli.exe`. These three
data variables point at the repo's **native** GDAL/proj/WindNinja data and must
match the DLLs `runfarsite.exe`/`WindNinja_cli.exe` load (the repo's `bin`
stack). The conda `rasterio`/`pyproj` also read `GDAL_DATA`/`PROJ_LIB` and read
the repo's data fine (they are plain, version-tolerant tables); pointing the
*natives* at the conda-shared dirs instead would be the wrong-database case, so
set them here and not to the conda env's `Library\share\...`.

`FireBehaviorModels\bin` does **not** need to be on `PATH` — the native exes
resolve their sibling DLLs from their own directory. Prepending `bin` to `PATH`
in the same terminal as the Python pipeline can shadow the conda GDAL DLLs and
break `rasterio` imports ("DLL load failed ... procedure not found" — the same
class as an OSGeo4W-on-PATH clash). `FireBehaviorModels\SetEnv.bat` does prepend
`bin`; that is fine in a `runfarsite`-only shell, but prefer the three `set`
lines above in the terminal that launches `orchestrate.py`.

## ecCodes definitions (conda-forge win-64: MEMFS)

conda-forge's win-64 `eccodes` can be built with MEMFS: it serves the GRIB
definition/sample files from an **in-memory filesystem** (`/MEMFS/definitions`),
which intermittently delivers truncated reads to eccodes' flex-generated `.def`
parser. Symptom during the weather stage (Phase A `hrrr` decode):

```
fatal flex scanner internal error--end of buffer missed
ECCODES ERROR   :  Parser: syntax error at line 3 of /MEMFS/definitions/grib2/templates/template.3.resolution_flags.def
error: stage 'weather' failed (rc=2)   # a C-level exit(2); the per-hour Python retry cannot catch it
```

Detect it:

```bat
python -m eccodes selfcheck
rem if it prints "Definitions: /MEMFS/definitions" (instead of a real path), the build is MEMFS-affected
```

Fix: give eccodes a real definitions tree on disk, matching your installed
`eccodes` version (tag below is `2.49.0` — use your `conda list -n flammap`
version's tag):

```bat
curl.exe -L -o "%USERPROFILE%\eccodes-2.49.0.zip" https://github.com/ecmwf/eccodes/archive/refs/tags/2.49.0.zip
tar -xf "%USERPROFILE%\eccodes-2.49.0.zip" -C "%USERPROFILE%"

set "ECCODES_DEFINITION_PATH=%USERPROFILE%\eccodes-2.49.0\definitions"
set "ECCODES_SAMPLES_PATH=%USERPROFILE%\eccodes-2.49.0\samples"
python -m eccodes selfcheck
rem now real paths; then re-run the weather stage
```

The two `ECCODES_*` variables belong in the same terminal as `orchestrate.py`
alongside the data variables above (the env vars take precedence over the
compiled-in MEMFS default; eccodes then parses real files and the truncation is
gone).

## `config.example.toml` native entries

```toml
[farsite]
command = "C:\\path\\to\\FireBehaviorModels\\bin\\runfarsite.exe"

[windninja]
command = "C:\\path\\to\\WindNinja_cli.exe"
```

- If the path contains spaces, wrap it in double quotes — the orchestrator's
  command splitting honors quotes and keeps the path one argv element;
  backslashes are literal (double-backslash in TOML).
- The Wine form (`wine C:/...`) remains valid on Linux/WSL.

## Line endings

On Windows the repo generates `.wxs`, WindNinja `.cfg`, FARSITE inputs/command
files as CRLF (native text mode) and `.atm`/`.asc` always as exact CRLF — all
what the Windows DLLs expect. Files generated on Linux (LF) remain equally
valid.

## Smoke before a real run

```bat
python -m eccodes selfcheck
rem Definitions/Samples must be real paths, not /MEMFS/...
python -m pytest -m "not integration"
python tools/orchestrate.py --config config.example.toml --dry-run
```

## Further reading

- [`docs/FARSITE-CLI.md`](docs/FARSITE-CLI.md)
- [`docs/WindNinja-CLI.md`](docs/WindNinja-CLI.md)
