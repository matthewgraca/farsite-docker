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
conda create -n flammap -c conda-forge python=3.12 rasterio numpy xarray cfgrib eccodes herbie-data pyshp pyproj requests timezonefinder tzdata pandas scipy matplotlib pytest
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
terminal, or set the four variables yourself:

| Variable | Value |
|---|---|
| `PATH` | += `FireBehaviorModels\bin` |
| `GDAL_DATA` | `FireBehaviorModels\bin\share\gdal-data` |
| `PROJ_LIB` | `FireBehaviorModels\bin\share\proj` |
| `WINDNINJA_DATA` | `FireBehaviorModels\bin\share\windninja-data` |

PowerShell equivalent (paths under `C:\path\to`):

```powershell
$env:PATH = "C:\path\to\FireBehaviorModels\bin;" + $env:PATH
$env:GDAL_DATA = "C:\path\to\FireBehaviorModels\bin\share\gdal-data"
$env:PROJ_LIB  = "C:\path\to\FireBehaviorModels\bin\share\proj"
$env:WINDNINJA_DATA = "C:\path\to\FireBehaviorModels\bin\share\windninja-data"
```

Required for the native `runfarsite.exe` / `WindNinja_cli.exe`.

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
python -m pytest -m "not integration"
python tools/orchestrate.py --config config.example.toml --dry-run
```

## Further reading

- [`docs/FARSITE-CLI.md`](docs/FARSITE-CLI.md)
- [`docs/WindNinja-CLI.md`](docs/WindNinja-CLI.md)
