# Windows support

Every Python tool and the orchestrator (`tools/`) run natively on Windows
(Python ≥ 3.11), invoking the Windows-native binaries through the `[windninja]
command` / `[farsite] command` config fields. The pipeline tail
(`runfarsite.exe`, `WindNinja_cli.exe`) is Windows-native, and nothing here
forces Wine on a Windows host. This file is the setup recipe for a win-64 box.

## Windows conda env

`environment.yml` is a fully-pinned **Linux-only** conda export. Use the recipe below.

win-64, conda-forge (default channel):

```bat
conda create -n flammap -c conda-forge python=3.12 rasterio numpy xarray cfgrib eccodes herbie-data pyshp pyproj requests timezonefinder tzdata pandas scipy matplotlib pytest tqdm
```

Activate with `conda activate flammap`.

## Binary runtime environment

`orchestrate.py` now **self-supplies the native runtime variables**: its
Python stages neutralize `PROJ_LIB`/`PROJ_DATA` in-process (a stray value
pointing at the repo's older proj.db makes the conda pyproj reproject valid
WGS84 points to `(inf, inf)`), while the `runfarsite`/`WindNinja` subprocesses
get exactly `GDAL_DATA`, `WINDNINJA_DATA`, `PROJ_LIB`, and `PROJ_DATA`
re-injected from `FireBehaviorModels\bin\share\...` (computed from the repo
layout, independent of the shell). **So the pipeline terminal does not need any
of these variables** - the only requirement is `conda activate flammap`.

If you run the native binaries *manually* (outside the pipeline), give them
the repo data dirs yourself — either `FireBehaviorModels\SetEnv.bat`
(**note:** it prepends `bin` to `PATH`, which can shadow the conda GDAL DLLs;
fine for a native-only shell, not the pipeline shell) or these:

| Variable | Value |
|---|---|
| `PATH` | *(not required)* — the exes load their DLL family from their own directory |
| `GDAL_DATA` | `FireBehaviorModels\bin\share\gdal-data` |
| `PROJ_LIB` | `FireBehaviorModels\bin\share\proj` |
| `PROJ_DATA` | `FireBehaviorModels\bin\share\proj` |
| `WINDNINJA_DATA` | `FireBehaviorModels\bin\share\windninja-data` |

cmd equivalent (paths under `C:\path\to`) for a manual native-run terminal:

```bat
set "GDAL_DATA=C:\path\to\FireBehaviorModels\bin\share\gdal-data"
set "PROJ_LIB=C:\path\to\FireBehaviorModels\bin\share\proj"
set "PROJ_DATA=C:\path\to\FireBehaviorModels\bin\share\proj"
set "WINDNINJA_DATA=C:\path\to\FireBehaviorModels\bin\share\windninja-data"
```

These point at the repo's **native** data and must match the DLLs the exes load
(the repo's `bin` stack).
…
**`PROJ_LIB`/`PROJ_DATA` caveat:** neither may point at
`FireBehaviorModels\bin\share\proj` in the terminal that runs the *Python*
stages: the repo's bundled `proj.db` predates the conda stack, and with either
variable set there the conda `pyproj 3.8/PROJ 9.8` quietly emits `(inf, inf)`
for valid WGS84 coordinates — `reprojecting to EPSG:5070 produced a
non-finite vertex ... -> (inf, inf)` / `no finite transform probe`. The tools
now neutralize both in-process before importing pyproj, and `orchestrate.py`
re-injects them **only into the native subprocesses** (`runfarsite`/`WindNinja`,
which do need them — the repo's GDAL reads proj.db from these). So the
pipeline works with a bare `conda activate flammap`; only manual native runs
need the four vars above.

`FireBehaviorModels\bin` does **not** need to be on `PATH` — the native exes
resolve their sibling DLLs from their own directory. Prepending `bin` to `PATH`
in the same terminal as the Python pipeline can shadow the conda GDAL DLLs and
break `rasterio` imports ("DLL load failed ... procedure not found" — the same
class as an OSGeo4W-on-PATH clash). `FireBehaviorModels\SetEnv.bat` does prepend
`bin`; that is fine in a `runfarsite`-only shell, but the pipeline terminal
needs no data variables at all.

## OSGeo4W dll clobbering
If you have OSGeo4W, it will compete with your conda environment's dlls, causing 
libraries like `rasterio` to fail.

Kick OSGeo4W out of your path for this session:
```bat
set "PATH=%PATH:C:\path\to\OSGeo4W\binaries;=%"
```

For my machine, it looks like:
```bat
set "PATH=%PATH:C:\Users\mgraca\AppData\Local\Programs\OSGeo4W\bin;=%"
```

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
version's tag). Use Python's `zipfile` to extract — Windows `tar` drops deep
entries (`Can't create '\\?\C:\...'`, an incomplete tree that still looks
present), and `zipfile` keeps the archive's top-level folder:

```bat
curl.exe -L -o "%USERPROFILE%\eccodes-2.49.0.zip" https://github.com/ecmwf/eccodes/archive/refs/tags/2.49.0.zip
rmdir /s /q "%USERPROFILE%\.eccodes" 2>nul
python -c "import zipfile; z=zipfile.ZipFile(r'%USERPROFILE%\eccodes-2.49.0.zip'); z.extractall(r'%USERPROFILE%\.eccodes'); print('entries:', len(z.namelist()))"
python -c "import os; d=r'%USERPROFILE%\.eccodes\eccodes-2.49.0\definitions'; print('def files:', sum(len(f) for _,_,f in os.walk(d))); print('section.1.def bytes:', os.path.getsize(os.path.join(d,'grib2','section.1.def')))"
rem expect: def files = 24115 and section.1.def bytes = 5486 (for 2.49.0)

set "ECCODES_DEFINITION_PATH=%USERPROFILE%\.eccodes\eccodes-2.49.0\definitions"
set "ECCODES_SAMPLES_PATH=%USERPROFILE%\.eccodes\eccodes-2.49.0\samples"
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
