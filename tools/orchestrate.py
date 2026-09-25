#!/usr/bin/env python3
"""orchestrate - chain the tools/ data-prep scripts, WindNinja, and runfarsite
from a single TOML config file.

Runs the full FARSITE data pipeline behind one human-editable config
(see config.example.toml): ingest a LANDFIRE landscape (ingest_landscape),
resolve the ignition (calfire_ignition), generate + run a WindNinja CLI config
(mesh = the LCP grid, native write_farsite_atm output), ingest HRRR weather
(hrrr_to_wxs), then assemble the FARSITE inputs file + command file and invoke
runfarsite.

Execution order is sequential and every stage is individually resumable via its
enable=false + input-override fields after a partial or network failure.

CLI:
    python tools/orchestrate.py --config config.example.toml [--dry-run]

--dry-run prints every planned subprocess argv and the full text of every file
that would be written (WindNinja cfg, FARSITE inputs, command file) and touches
nothing on disk. Config errors exit 2 (via die()); success exits 0.
"""

import argparse
import json
import shlex
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hrrr_to_wxs import die, parse_utc, resolve_timezone
from runroot_to_atm import scan_frames, verify_atm

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent

# Default FARSITE 1-hr fuel-moisture tuple (verified sample block):
#  Model F1 F10 F100 FMLiveHerb FMLiveWoody
_FUEL_MOISTURE_DEFAULTS = "6 7 8 60 90 16"

_DEFAULTS = {
    "fire": {"enable": True, "name": None, "year": None, "index": None,
             "lat": None, "lon": None, "crs": None, "fire_json": None},
    "landscape": {"enable": True, "lcp": None, "bbox": None, "mapzone": None,
                  "email": None, "version": "2024", "fuel_model": "fbfm40",
                  "resolution": 30, "dem_out": None},
    "simulation": {"start": None, "end": None, "row_timezone": None,
                   "lead_days": 0, "burn_periods": []},
    "windninja": {"enable": True, "command": "", "run_root": None, "threads": 4,
                  "options": {}},
    "weather": {"enable": True, "wxs": None, "cache_dir": None, "threads": 8,
                "elevation_tol_ft": 500},
    "farsite": {"enable": True, "run": True, "command": "", "cwd": None,
                "barrier": "0", "out_base": None, "outputs_type": 2,
                "timestep": 60, "distance_res": 30, "perimeter_res": 60,
                "spot_grid_resolution": 15, "spot_probability": 0.035,
                "spot_ignition_delay": 0, "minimum_spot_distance": 30,
                "acceleration_on": 1, "fill_barriers": 0,
                "foliar_moisture_content": 100.0,
                "crown_fire_method": "ScottReinhardt", "fuel_moistures": {}},
    "output": {"run_dir": None},
}

TOP_LEVEL = ("fire", "landscape", "simulation", "windninja", "weather",
             "farsite", "output")
_SECTION_KEYS = {
    "fire": ("enable", "name", "year", "index", "lat", "lon", "crs", "fire_json"),
    "landscape": ("enable", "lcp", "bbox", "mapzone", "email", "version",
                  "fuel_model", "resolution", "dem_out"),
    "simulation": ("start", "end", "row_timezone", "lead_days", "burn_periods"),
    "windninja": ("enable", "command", "run_root", "threads", "options"),
    "weather": ("enable", "wxs", "cache_dir", "threads", "elevation_tol_ft"),
    "farsite": ("enable", "run", "command", "cwd", "barrier", "out_base",
                "outputs_type", "timestep", "distance_res", "perimeter_res",
                "spot_grid_resolution", "spot_probability", "spot_ignition_delay",
                "minimum_spot_distance", "acceleration_on", "fill_barriers",
                "foliar_moisture_content", "crown_fire_method", "fuel_moistures"),
    "output": ("run_dir",),
}


def slugify(name):
    return str(name).lower().replace(" ", "-")


def split_command(s):
    """Split a [windninja]/[farsite] command string into argv tokens.

    posix=False keeps backslashes literal (Windows paths) and groups a
    double-quoted argument into one token; the enclosing quotes are then
    dropped so the quoted path becomes a single clean argv element (a path
    with spaces passed to subprocess as one argument).
    """
    return [
        t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
        for t in shlex.split(s, posix=False)
    ]


def respath(v):
    """Resolve a config path against the repo root; absolute paths pass through."""
    p = Path(v)
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _render_wn_value(key, value):
    """Render a TOML value the way WindNinja's config parser expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    die(f"windninja.options.{key} must be scalar (bool/str/int/float), "
        f"got {type(value).__name__}")


def load_config(path):
    """Load + validate a TOML config. Unknown sections/keys die (typo guard)."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        die(f"--config not found: {path}")
    except tomllib.TOMLDecodeError as e:
        die(f"cannot parse {path}: {e}")
    for sec in data:
        if sec not in TOP_LEVEL:
            die(f"unknown top-level section [{sec}] in {path} "
                f"(valid: {'/'.join(TOP_LEVEL)})")
        for key in data[sec]:
            if key not in _SECTION_KEYS[sec]:
                die(f"unknown key '{key}' in [{sec}] in {path} "
                    f"(valid: {'/'.join(_SECTION_KEYS[sec])})")
    if "farsite" in data and "fuel_moistures" in data["farsite"]:
        fm = {}
        for model, value in data["farsite"]["fuel_moistures"].items():
            try:
                model = int(model)
            except (TypeError, ValueError):
                die(f"[farsite.fuel_moistures] keys must be integer fuel-model "
                    f"numbers, got {model!r}")
            if not isinstance(value, str):
                die(f"[farsite.fuel_moistures] {model} value must be a string "
                    f"tuple like '6 7 8 60 90 16', got {value!r}")
            fm[model] = value
        data["farsite"]["fuel_moistures"] = fm
    # fill schema defaults (validated above, so only known keys remain)
    for sec, defaults in _DEFAULTS.items():
        data.setdefault(sec, {})
        for key, value in defaults.items():
            data[sec].setdefault(key, value)
    return data


def build_parser():
    p = argparse.ArgumentParser(
        prog="orchestrate.py",
        description="Chain the tools/ data-prep scripts + WindNinja + runfarsite "
                    "from a single TOML config.",
        epilog="--dry-run prints every planned subprocess argv and the full text "
               "of every file that would be written; nothing is touched.",
    )
    p.add_argument("--config", metavar="PATH", required=True,
                   help="TOML config (see config.example.toml); relative paths "
                        "resolve against the repo root")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and generated file texts; touch nothing")
    return p


def _utc_naive(dt):
    """fire.json datetimes -> naive UTC (they are stored as UTC instants)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def read_fire_json(path):
    """Read fire.json (calfire_ignition output); returns a normalized dict."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        die(f"cannot read fire.json: {path}: {e}")
    for k in ("name", "year"):
        if data.get(k) is None:
            die(f"fire.json {path} missing non-null '{k}'")
    return {
        "name": data["name"],
        "year": data["year"],
        "alarm_date": _utc_naive(datetime.fromisoformat(data["alarm_date"])
                                 if data.get("alarm_date") else None),
        "cont_date": _utc_naive(datetime.fromisoformat(data["cont_date"])
                                if data.get("cont_date") else None),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
    }


def _open_lcp(lcp):
    import rasterio
    try:
        return rasterio.open(lcp)
    except Exception as e:  # noqa: BLE001 - surface the rasterio message verbatim
        die(f"cannot open LCP {lcp}: {e}")


def lcp_crs(cfg, lcp):
    """--crs argument for calfire_ignition: config override, else the LCP CRS."""
    if cfg["fire"].get("crs"):
        return str(cfg["fire"]["crs"])
    with _open_lcp(lcp) as ds:
        epsg = ds.crs.to_epsg()
    return f"EPSG:{epsg}" if epsg is not None else ds.crs.to_wkt()


def lcp_cell_m(cfg, lcp):
    """WindNinja mesh resolution = the LCP x-cell size (native atm must match)."""
    with _open_lcp(lcp) as ds:
        rx, ry = ds.res
    if rx != ry:
        die(f"non-square LCP cells not supported -- native atm must match the "
            f"LCP grid: {lcp} res=({rx}, {ry})")
    return float(rx)


def write_dem_band1(src_path, dem_path):
    """Extract LCP band 1 as a single-band int16 GeoTIFF for WindNinja.

    Mirrors ingest_landscape._write_dem exactly (same profile/dtype/band desc).
    """
    import rasterio
    dem_path = Path(dem_path)
    dem_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_lcp(src_path) as src:
        profile = src.profile.copy()
        profile.update(count=1, dtype="int16", driver="GTiff")
        with rasterio.open(dem_path, "w", **profile) as dst:
            dst.write(src.read(1).astype("int16"), 1)
            dst.set_band_description(1, "elev")


def fuel_models(lcp):
    """Distinct int fuel models from LCP band 4 among valid cells, 0 always."""
    import numpy as np
    with _open_lcp(lcp) as ds:
        if ds.count < 4:
            die(f"LCP {lcp} has {ds.count} bands; band 4 (fuel model) required")
        band = ds.read(4)
        masks = ds.read_masks(4)
        nodata = ds.nodata
    valid = masks != 0
    if nodata is not None:
        valid &= band != nodata
    models = {int(v) for v in band[valid]}
    models.add(0)
    return sorted(models)


def wxs_to_raws(wxs_path):
    """Parse a .wxs into (RAWS_ELEVATION, rows): drop RAWS_UNITS + the header
    line, keep the remaining non-empty lines verbatim."""
    elev = None
    rows = []
    with open(wxs_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            low = stripped.lower()
            if low.startswith("raws_units:"):
                continue
            if low.startswith("raws_elevation:"):
                elev = int(stripped.split(":", 1)[1].strip())
                continue
            if low.startswith("year"):   # "Year Mth Day Time ..." header
                continue
            rows.append(stripped)
    if elev is None:
        die(f"no RAWS_ELEVATION line in {wxs_path}")
    return elev, rows


def ftime(dt):
    """FARSITE local-clock instant 'M D HHMM' (sample uses unpadded M/D)."""
    return f"{dt.month} {dt.day} {dt.hour:02d}{dt.minute:02d}"


def find_wn_atm(run_root):
    """The single native WindNinja '*.atm' directly under run_root."""
    atms = sorted(respath(run_root).glob("*.atm"))
    if not atms:
        die(f"no .atm produced by WindNinja under {run_root} "
            f"(write_farsite_atm=true is injected; check the WindNinja run log)")
    if len(atms) > 1:
        die(f"ambiguous: multiple .atm under {run_root}: "
            + ", ".join(p.name for p in atms))
    return atms[0]


class Runner:
    """Sequential pipeline over one config; stages are individually resumable."""

    def __init__(self, cfg, *, dry=False):
        self.cfg = cfg
        self.dry = dry
        # resolved paths/state, filled by the stages in order
        self.run_dir = None
        self.slug = None
        self.lcp = None
        self.dem = None
        self.wind_root = None
        self.cache_dir = None
        self.wxs_path = None
        self.ign_shp = None
        self.atm_path = None
        self.utc_start = self.utc_end = None
        self.lead_start = None
        self.local_start = self.local_end = None
        self.std_off = timedelta(0)
        self.tz_name = None
        self._anchor = None
        self.wxs = None

    # ------------------------------------------------------------------ io --
    def run_cmd(self, stage, argv, *, cwd=None):
        argv = [str(a) for a in argv]
        cwd = cwd or REPO_ROOT
        if self.dry:
            print(f"\n[stage: {stage}] planned argv (dry-run, not executed):\n"
                  f"  {' '.join(argv)}   (cwd: {cwd})")
            return
        print(f"\n[stage: {stage}]")
        print(f"  $ {' '.join(argv)}   (cwd: {cwd})")
        r = subprocess.run(argv, cwd=cwd)
        if r.returncode != 0:
            die(f"stage '{stage}' failed (rc={r.returncode}): {' '.join(argv)}")

    def write_file(self, path, content, *, what):
        path = Path(path)
        if self.dry:
            print(f"\n[stage: farsite-assemble] would write {what}: "
                  f"{path.resolve()}\n{'-' * 72}\n{content.rstrip(chr(10))}\n"
                  f"{'-' * 72}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def stage(self, name):
        print(f"\n[{name}]")

    # ------------------------------------------------------------- paths ----
    def init_paths(self):
        """Resolve run_dir + slug before the first stage (landscape needs the
        slug for the default DEM name; the fire stage pins the run dir)."""
        fire = self.cfg["fire"]
        out = self.cfg["output"].get("run_dir")
        if out:
            run_dir = respath(out)
        elif fire["enable"]:
            name = fire["name"] or die("fire.enable=true requires [fire] name")
            year = fire["year"] or die("fire.enable=true requires [fire] year "
                                       "(pins the run dir and calfire's "
                                       "disambiguation)")
            run_dir = REPO_ROOT / f"FireBehaviorModels/SampleData/{year}_{slugify(name)}"
        else:
            fj = fire["fire_json"] or die("fire.enable=false requires [fire] fire_json")
            run_dir = respath(fj).parent
        if fire["enable"]:
            slug = slugify(fire["name"])
        else:
            fj = respath(fire["fire_json"])
            slug = slugify(read_fire_json(fj)["name"]) if fj.is_file() else "run"
        self.run_dir = run_dir
        self.slug = slug
        self.wind_root = respath(self.cfg["windninja"].get("run_root")) \
            if self.cfg["windninja"].get("run_root") else run_dir / "windroot"
        default_cache = REPO_ROOT / "FireBehaviorModels/.cache/hrrr"
        self.cache_dir = respath(self.cfg["weather"].get("cache_dir")) \
            if self.cfg["weather"].get("cache_dir") else default_cache
        self.wxs_path = run_dir / f"{slug}-hrrr.wxs"
        print(f"run dir: {run_dir}")
        print(f"wind root: {self.wind_root}")

    # ---------------------------------------------------------- stage 3 ----
    def stage_landscape(self):
        """LCP (FARSITE) + single-band DEM (WindNinja)."""
        land = self.cfg["landscape"]
        dem_from_cfg = respath(land["dem_out"]) if land.get("dem_out") else None
        dem = dem_from_cfg or self.run_dir / f"{self.slug}-dem.tif"
        name = "landscape"
        if not land["enable"]:
            lcp = land["lcp"] or die("landscape.enable=false requires [landscape] lcp")
            lcp = respath(lcp)
            if not lcp.is_file():
                die(f"[landscape] lcp not found: {lcp}")
            self.stage(name)
            print(f"landscape disabled (enable=false); reusing LCP {lcp.resolve()}")
        else:
            bbox = land.get("bbox")
            mapzone = land.get("mapzone")
            if (bbox is None) == (mapzone is None):
                die("landscape.enable=true requires exactly one of [landscape] "
                    "bbox or mapzone")
            email = land.get("email") or die("landscape.enable=true requires "
                                             "[landscape] email (LFPS requester)")
            argv = [sys.executable, str(TOOL_DIR / "ingest_landscape.py")]
            if bbox is not None:
                argv += ["--bbox", str(bbox)]
            else:
                argv += ["--mapzone", str(mapzone)]
            argv += ["--email", str(email), "--version", str(land["version"]),
                     "--fuel-model", str(land["fuel_model"]),
                     "--resolution", str(land["resolution"]),
                     "--out", str(self.run_dir / "landscape")]
            if dem_from_cfg is not None:
                argv += ["--dem-out", str(dem_from_cfg)]
            self.run_cmd(name, argv)
            lcp = self.run_dir / "landscape.tif"
        self.lcp = lcp
        self.dem = dem
        # WindNinja elevation: configured dem_out, else extract LCP band 1
        if dem_from_cfg is not None:
            print(f"DEM: {dem.resolve()}")
        elif self.dry:
            print(f"DEM: {dem} (band-1 elevation will be extracted from the "
                  f"LCP on a real run)")
        else:
            if not dem.is_file():
                self.stage("landscape: extract DEM")
                write_dem_band1(lcp, dem)
                print(f"DEM: extracted LCP band 1 -> {dem}")
            else:
                print(f"DEM: {dem} (already present)")
        if lcp.is_file():
            with _open_lcp(lcp) as ds:
                epsg = ds.crs.to_epsg()
                print(f"LCP: {lcp.resolve()} ({ds.count} bands, crs "
                      f"{'EPSG:' + str(epsg) if epsg else ds.crs}, res "
                      f"{ds.res[0]}/{ds.res[1]} m)")

    # ----------------------------------------------------------- stage 4 ----
    def stage_fire(self):
        """Ignition shapefile seed + fire.json; then read fire.json fields."""
        fire = self.cfg["fire"]
        name = "fire"
        if fire["enable"]:
            argv = [sys.executable, str(TOOL_DIR / "calfire_ignition.py"),
                    "--fire-name", str(fire["name"]),
                    "--year", str(fire["year"])]
            if fire.get("index") is not None:
                argv += ["--index", str(fire["index"])]
            if fire.get("lat") is not None and fire.get("lon") is not None:
                argv += ["--lat", f"{fire['lat']:.6f}", "--lon", f"{fire['lon']:.6f}"]
            argv += ["--crs", lcp_crs(self.cfg, self.lcp),
                     "--out-dir", str(self.run_dir)]
            self.run_cmd(name, argv)
            fire_json = self.run_dir / "fire.json"
        else:
            fire_json = respath(fire["fire_json"])
            if not fire_json.is_file():
                die(f"[fire] fire_json not found: {fire_json}")
        self.stage(name)
        print(f"fire.json: {fire_json.resolve()}")
        if not fire_json.is_file():
            if self.dry:
                print("[dry-run] fire.json not present yet (fire stage not "
                      "executed); using [fire]/[simulation] config values")
                return None
            die(f"fire.json not produced: {fire_json} (fire stage failed?)")
        return read_fire_json(fire_json)

    def ignition_shp(self):
        """FARSITE ignition seed: fire-stage output, else the fire.json dir."""
        if self.cfg["fire"]["enable"]:
            return self.run_dir / "ignition.shp"
        return respath(self.cfg["fire"]["fire_json"]).parent / "ignition.shp"

    # ----------------------------------------------------------- stage 5 ----
    def stage_window(self, fj):
        """Whole-hour UTC window + fire-local standard IANA clock."""
        sim = self.cfg["simulation"]
        when = fj["alarm_date"] if fj is not None else None
        utc_start = parse_utc(sim["start"]) if sim.get("start") else when
        if utc_start is None:
            die("no [simulation] start and fire.json has no alarm_date; "
                "set [simulation] start = <UTC whole hour YYYY-MM-DDTHH:MM>")
        cont = fj["cont_date"] if fj is not None else None
        utc_end = parse_utc(sim["end"]) if sim.get("end") else cont
        if utc_end is None:
            die("missing simulation end: fire.json has no cont_date and "
                "[simulation] end is not set; set [simulation] end = "
                "<UTC whole hour YYYY-MM-DDTHH:MM>")
        if utc_end < utc_start:
            die(f"[simulation] end {utc_end:%Y-%m-%dT%H:%M} is before start "
                f"{utc_start:%Y-%m-%dT%H:%M}")
        lat = None
        lon = None
        if fj is not None and fj["lat"] is not None and fj["lon"] is not None:
            lat, lon = fj["lat"], fj["lon"]
        if lat is None and self.cfg["fire"].get("lat") is not None \
                and self.cfg["fire"].get("lon") is not None:
            lat, lon = self.cfg["fire"]["lat"], self.cfg["fire"]["lon"]
        self._anchor = (lat, lon)
        zi, std_off = resolve_timezone(sim["row_timezone"], lat, lon)
        self.utc_start, self.utc_end = utc_start, utc_end
        self.local_start = utc_start + std_off
        self.local_end = utc_end + std_off
        self.std_off = std_off
        self.tz_name = sim["row_timezone"] or zi.key
        self.lead_start = utc_start - timedelta(days=sim.get("lead_days", 0))
        self.stage("window")
        print(f"window: {utc_start:%Y-%m-%dT%H:%M}Z .. {utc_end:%Y-%m-%dT%H:%M}Z"
              f"  (local {self.local_start:%Y-%m-%d %H:%M} .. "
              f"{self.local_end:%Y-%m-%d %H:%M} {self.tz_name}, "
              f"std_off {std_off})")
        if sim.get("lead_days"):
            print(f"fuel-conditioning lead: {sim['lead_days']} day(s) prepended "
                  f"({self.lead_start:%Y-%m-%dT%H:%M}Z .. {utc_start:%Y-%m-%dT%H:%M}Z)")

    # ---------------------------------------------------------- stage 6 ----
    def stage_windninja(self):
        """WindNinja CLI cfg; decisions, first match wins:
        1) pairs present -> reuse (resume path; no cfg write)
        2) enable=false -> skip entirely
        3) no pairs -> write cfg (optionally run / hand-off)."""
        wn = self.cfg["windninja"]
        opts = dict(wn.get("options") or {})
        pairs = scan_frames(self.wind_root) if self.wind_root.is_dir() else {}
        if pairs:
            self.stage("windninja")
            print(f"reusing {len(pairs)} terrain-resolved wind pairs under "
                  f"{self.wind_root.resolve()}")
            return
        if not wn["enable"]:
            self.stage("windninja")
            print("windninja disabled (enable=false); the .atm delivery is "
                  "checked by the atmosphere stage")
            return
        # write the cfg
        self.stage("windninja")
        if not isinstance(opts.get("initialization_method"), str) \
                or not opts["initialization_method"]:
            die("windninja.options.initialization_method is required "
                "(e.g. initialization_method = \"wxModelInitialization\")")
        cell_m = lcp_cell_m(self.cfg, self.lcp) if self.lcp.is_file() \
            else float(self.cfg["landscape"].get("resolution", 30))
        lines = [f"{k} = {_render_wn_value(k, v)}" for k, v in opts.items()]
        lines += [
            f"elevation_file = {self.dem.resolve()}",
            f"output_path = {self.wind_root.resolve()}",
            f"time_zone = {self.tz_name}",
            f"num_threads = {wn['threads']}",
            "write_ascii_output = true",
            "write_farsite_atm = true",
            f"mesh_resolution = {cell_m:g}",
            "units_mesh_resolution = m",
        ]
        cfg_path = self.wind_root / f"{self.slug}.cfg"
        self.write_file(cfg_path, "\n".join(lines) + "\n", what="WindNinja cfg")
        command = str(wn["command"]).strip()
        if command:
            if self.dry:
                print(f"[dry-run] windninja.command set; would run: "
                      f"{command} {cfg_path}"
                      f" (pair verification skipped in dry-run)")
                return
            self.run_cmd("windninja", [*split_command(command), str(cfg_path)])
            if not scan_frames(self.wind_root):
                die(f"no pairs produced under {self.wind_root} after running "
                    f"{command}; run WindNinja manually and re-run")
        else:
            print(f"WindNinja config written: {cfg_path.resolve()}")
            print(f"run manually, then re-run this orchestrator with the same "
                  f"--config (it resumes automatically):")
            print(f"  {command or 'WindNinja_cli'} {cfg_path.resolve()}")
            print("WindNinja not invoked (windninja.command empty); run the "
                  "command above, then re-run this orchestrator with the same "
                  "--config - it resumes automatically. The atmosphere stage "
                  "below is the pause point: it fails until the .atm exists.")

    # ----------------------------------------------------------- stage 7 ----
    def stage_atm(self):
        """Locate + integrity-check the native WindNinja .atm."""
        self.stage("atm")
        if self.dry:
            atms = sorted(respath(self.wind_root).glob("*.atm"))
            if not atms:
                print(f"find_wn_atm: none under {self.wind_root.resolve()} "
                      f"(write_farsite_atm=true is injected; check the WindNinja "
                      f"run log)")
                print("[dry-run] this fails a real run until WindNinja has "
                      "produced a .atm (the declared pause point)")
                self.atm_path = self.wind_root / "windninja.atm"   # placeholder
                return
            if len(atms) > 1:
                print(f"[dry-run] ambiguous .atm under {self.wind_root.resolve()}: "
                      + ", ".join(p.name for p in atms))
            atm = atms[0]
        else:
            atm = find_wn_atm(self.wind_root)
        self.atm_path = atm
        print(f"atm: {atm.resolve()}")
        n_rows, n_ok, missing, corrupt = verify_atm(atm)
        if missing or corrupt:
            die(f"atm integrity failed: {len(missing)} missing + {len(corrupt)} "
                f"corrupt grid(s) under {atm.parent} - re-copy the grids from a "
                f"clean generation and re-run")
        print(f"atm verified: {n_rows} rows, {n_ok} grids OK")

    # ----------------------------------------------------------- stage 8 ----
    def stage_weather(self):
        """HRRR -> .wxs (or reuse an existing wxs when weather.enable=false)."""
        wea = self.cfg["weather"]
        name = "weather"
        if wea["enable"]:
            argv = [sys.executable, str(TOOL_DIR / "hrrr_to_wxs.py"),
                    "--dem", str(self.lcp),
                    "--start", f"{self.utc_start:%Y-%m-%dT%H:%M}Z",
                    "--end", f"{self.utc_end:%Y-%m-%dT%H:%M}Z",
                    "--row-timezone", self.tz_name,
                    "--run-root", str(self.wind_root),
                    "--cache-dir", str(self.cache_dir),
                    "--lead-days", str(self.cfg["simulation"].get("lead_days", 0)),
                    "--elevation-tol-ft", str(wea.get("elevation_tol_ft", 500)),
                    "--threads", str(wea.get("threads", 8)),
                    "--out", str(self.wxs_path)]
            anchor = self._anchor
            if anchor is not None and anchor[0] is not None and anchor[1] is not None:
                argv += ["--lat", f"{anchor[0]:.6f}", "--lon", f"{anchor[1]:.6f}"]
            self.run_cmd(name, argv)
            wxs = self.wxs_path
        else:
            wxs = wea["wxs"] or die("weather.enable=false requires [weather] wxs")
            wxs = respath(wxs)
            if not wxs.is_file():
                die(f"[weather] wxs not found: {wxs}")
            print(f"[{name}] weather disabled (enable=false); reusing wxs "
                  f"{wxs.resolve()}")
        self.wxs = wxs
        return wxs

    # ------------------------------------------------------------ stage 9 ----
    def stage_assemble(self):
        """FARSITE inputs file + command file."""
        self.stage("farsite-assemble")
        sim = self.cfg["simulation"]
        far = self.cfg["farsite"]
        # wxs -> RAWS block
        if Path(self.wxs).is_file():
            raws_elev, rows = wxs_to_raws(self.wxs)
        elif self.dry:
            raws_elev, rows = 0, []
            print(f"[dry-run] wxs not present yet ({self.wxs}); RAWS rows "
                  f"are filled by the weather stage on a real run")
        else:
            die(f"weather .wxs missing: {self.wxs} (weather stage must run "
                f"or [weather] wxs must point at an existing file)")
        # fuel-moisture block: distinct band-4 models (+0), sample defaults
        overrides = dict(far.get("fuel_moistures") or {})
        if self.lcp.is_file():
            models = fuel_models(self.lcp)
        elif self.dry:
            print(f"[dry-run] LCP not produced yet ({self.lcp}); fuel-model "
                  f"block will carry its distinct band-4 values on a real run")
            models = sorted({0} | set(overrides))
        else:
            die(f"LCP not found for the fuel-moisture block: {self.lcp}")
        lines = [f"FUEL_MOISTURES_DATA: {len(models)}"]
        for m in models:
            if m in overrides:
                lines.append(f"{m} {overrides[m]}")
            else:
                lines.append(f"{m} {_FUEL_MOISTURE_DEFAULTS}")
        lines += [
            f"FARSITE_TIMESTEP: {far['timestep']}",
            f"FARSITE_DISTANCE_RES: {far['distance_res']}",
            f"FARSITE_PERIMETER_RES: {far['perimeter_res']}",
            f"FARSITE_SPOT_GRID_RESOLUTION: {far['spot_grid_resolution']}",
            f"FARSITE_SPOT_PROBABILITY: {far['spot_probability']}",
            f"FARSITE_SPOT_IGNITION_DELAY: {far['spot_ignition_delay']}",
            f"FARSITE_MINIMUM_SPOT_DISTANCE: {far['minimum_spot_distance']}",
            f"FARSITE_ACCELERATION_ON: {far['acceleration_on']}",
            f"FARSITE_FILL_BARRIERS: {far['fill_barriers']}",
        ]
        periods = sim.get("burn_periods") or []
        if periods:
            lines.append(f"FARSITE_BURN_PERIODS: {len(periods)}")
            for period in periods:
                if not (isinstance(period, list) and len(period) == 2):
                    die(f"simulation.burn_periods entries must be [start, end] "
                        f"pairs of 'M D HHMM', got {period!r}")
                start_tokens = str(period[0]).split()
                end_tokens = str(period[1]).split()
                if len(start_tokens) != 3 or len(end_tokens) != 3:
                    die(f"simulation.burn_periods entries must be 'M D HHMM', "
                        f"got {period!r}")
                # FARSITE format is "M D HHMM HHMM" (the end carries no day -
                # a period is per-day; span a night with multiple entries).
                lines.append(f"{period[0]} {end_tokens[2]}")
        lines += [
            f"FARSITE_START_TIME: {ftime(self.local_start)}",
            f"FARSITE_END_TIME: {ftime(self.local_end)}",
            f"FOLIAR_MOISTURE_CONTENT: {far['foliar_moisture_content']}",
            f"CROWN_FIRE_METHOD: {far['crown_fire_method']}",
            f"RAWS_ELEVATION: {raws_elev}",
            "RAWS_UNITS: English",
            f"RAWS: {len(rows)}",
            *rows,
            f"FARSITE_ATM_FILE: {self.atm_path.resolve() if not self.dry else self.atm_path}",
        ]
        inputs_path = self.run_dir / f"{self.slug}-FarsiteInputs.txt"
        self.write_file(inputs_path, "\n".join(lines) + "\n", what="FARSITE inputs file")
        # command file: single line, all fields absolute
        out_base = respath(far["out_base"]) if far.get("out_base") \
            else self.run_dir / "farsite-out" / self.slug
        cmd_line = " ".join([
            str(self.lcp.resolve()), str(inputs_path.resolve()),
            str(self.ignition_shp().resolve()), str(far.get("barrier", "0")),
            str(out_base.resolve()), str(far["outputs_type"])])
        if not self.dry:
            out_base.parent.mkdir(parents=True, exist_ok=True)
        cmd_path = self.run_dir / f"{self.slug}-FarsiteCmd.txt"
        self.write_file(cmd_path, cmd_line + "\n", what="FARSITE command file")
        print(f"out base: {out_base.resolve()}")
        self.out_base = out_base
        return inputs_path, cmd_path

    # ----------------------------------------------------------- stage 10 ---
    def stage_run(self, cmd_path):
        """Invoke runfarsite when [farsite] run=true."""
        far = self.cfg["farsite"]
        if not far["run"]:
            print("\n[stage: farsite-run] farsite.run=false; runfarsite not "
                  "invoked (assembly only)")
            return
        command = str(far.get("command") or "").strip()
        if not command:
            die("farsite.enable=true with farsite.run=true requires [farsite] "
                "command (e.g. wine C:/.../runfarsite.exe)")
        cwd = respath(far["cwd"]) if far.get("cwd") else REPO_ROOT
        self.run_cmd("farsite-run", [*split_command(command), str(cmd_path)], cwd=cwd)
        print(f"farsite done; outputs base: {self.out_base.resolve()}")

    # ---------------------------------------------------------------- run ---
    def run(self):
        self.init_paths()
        self.stage_landscape()
        fj = self.stage_fire()
        self.stage_window(fj)
        self.stage_windninja()
        self.stage_atm()
        self.stage_weather()
        inputs_path, cmd_path = self.stage_assemble()
        self.stage_run(cmd_path)
        print("\ndone")


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    runner = Runner(cfg, dry=args.dry_run)
    if args.dry_run:
        print(f"orchestrate --dry-run for {args.config}")
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
