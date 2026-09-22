#!/usr/bin/env python3
"""hrrr_to_wxs - ingest HRRR analysis hours into a FARSITE .wxs weather stream.

Downloads the HRRR sfc analysis for a UTC fire window, samples one representative
cell per hour, converts units, and writes a valid FARSITE "Weather Stream File"
(.wxs) whose rows are labelled on the fire-local STANDARD clock.

Dependencies (documented, not vendored): herbie, xarray, cfgrib, numpy, rasterio,
pyproj, timezonefinder, tqdm (Phase-A download progress bar), matplotlib (optional;
only for --plot) - plus zoneinfo from stdlib and the system eccodes library.
Reference environment recipe:

    conda create -n hrrr-wxs -c conda-forge python=3.12 numpy xarray pandas rasterio pyproj eccodes
    conda activate hrrr-wxs && python -m pip install herbie cfgrib timezonefinder tqdm

Input contract: --start/--end are UTC instants (%Y-%m-%dT%H:%M with an optional
trailing Z). They are never re-read as wall time in --row-timezone; that flag only
converts the UTC instants to the fire-local clock used for the row labels. Site
height (RAWS_ELEVATION) always comes from --dem band 1 at the representative cell
(-m, converted to feet): for HRRR-derived weather the site height IS the terrain
under the sampled cell, so --dem is the single source of truth.

Anchor: --lat/--lon are optional - pass both or neither. When omitted, the anchor
is inferred from --dem as the valid-data centroid of band 1 (masking nodata; falls
back to the raster-bounds center if the band is all-nodata). Explicit values
override inference. Precision note: the HRRR grid is ~3 km, so a sub-cell anchor
shift (e.g. centroid vs an explicit ignition point) can move the sampled HRRR cell
by one and with it the row values and RAWS_ELEVATION; pass explicit --lat/--lon
when you want weather pinned to a specific location.

Pipeline: Phase A downloads+persists one six-band subset grib per hour under
--cache-dir (skip-if-cached, offline re-runs); Phase B converts those cached files
to rows with no network. Evolution notes vs the original design (herbie 2026.x
removed the list-of-search-strings API): the fetch uses Herbie's single-regex
subset path (download(search=...)) over one alternation regex resolved from the
inventory, and Phase B merges the six shortNames via cfgrib.open_datasets because
plain xr.open_dataset silently drops u10/v10 (heightAboveGround 2 m vs 10 m).
"""

import argparse
import concurrent.futures
import math
import re
import shutil
import sys
import time
import warnings
from tqdm import tqdm
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

# Variables needed per hour, with the expected inventory (search_this) fragments.
# shortName is the cfgrib/xarray name; grib_name/level identify the grib messages.
WANTED = [
    ("t2m", "TMP", "2 m above ground"),   # temperature at 2 m (K)
    ("r2", "RH", "2 m above ground"),     # relative humidity at 2 m (%)
    ("tp", "APCP", "surface"),            # hourly accumulated precip (mm)
    ("tcc", "TCDC", "entire atmosphere"), # total cloud cover (%, 0..100)
    ("u10", "UGRD", "10 m above ground"), # wind U component (m/s)
    ("v10", "VGRD", "10 m above ground"), # wind V component (m/s)
]
SAMPLE_COLS = "utc,valid_local,Temp_K,RH,APCP_mm,u10,v10,TCDC,cell_lat,cell_lon"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def parse_utc(s):
    """Parse a whole-hour UTC instant, rejecting any tz suffix."""
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1]
    if re.search(r"[+-]\d{2}:?\d{2}$", s):
        die(f"'{s}' carries an offset suffix; input must be UTC - omit the offset")
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except ValueError:
        die(f"cannot parse '{s}' as UTC %Y-%m-%dT%H:%M (optional trailing Z)")
    if dt.minute != 0:
        die(f"timestamps must be whole UTC hours: '{s}' has minute {dt.minute}")
    return dt


def build_parser():
    p = argparse.ArgumentParser(
        prog="hrrr_to_wxs.py",
        description="Ingest HRRR analysis hours into a FARSITE .wxs weather stream.",
        epilog="--start/--end are UTC instants; --row-timezone only relabels rows.",
    )
    p.add_argument("--start", help="UTC start %%Y-%%m-%%dT%%H:%%M (optional trailing Z)")
    p.add_argument("--end", help="UTC end %%Y-%%m-%%dT%%H:%%M (optional trailing Z)")
    p.add_argument("--row-timezone", default=None,
                   help="IANA name of the fire-local clock for row labels; default: "
                        "derived from the anchor via timezonefinder")
    p.add_argument("--lat", type=float, default=None,
                   help="anchor latitude (WGS84, -90..90); give both --lat and --lon, "
                        "or neither to infer the anchor from the --dem band-1 centroid")
    p.add_argument("--lon", type=float, default=None,
                   help="anchor longitude (WGS84, -180..180); see --lat")
    p.add_argument("--dem", required=True,
                   help="fire-area LCP/DEM raster; band 1 = elevation (m) - source "
                        "of RAWS_ELEVATION and elevation-matched cell selection")
    p.add_argument("--elevation-tol-ft", type=int, default=500,
                   help="elevation-match tolerance in feet (default 500)")
    p.add_argument("--lead-days", type=int, default=0,
                   help="conditioning days prepended before --start (default 0)")
    p.add_argument("--out", default="FireBehaviorModels/SampleData/Palisades/palisades-hrrr.wxs",
                   help="output .wxs path (default FireBehaviorModels/SampleData/Palisades/palisades-hrrr.wxs)")
    p.add_argument("--dump-dir", default=None,
                   help="optional dir for samples.csv (pre-rounding audit dump)")
    p.add_argument("--cache-dir", default="./.cache/hrrr",
                   help="dir persisting per-hour subset gribs (default ./.cache/hrrr)")
    p.add_argument("--threads", type=int, default=8,
                   help="Phase-A download threads (default 8)")
    p.add_argument("--run-root", default=None,
                   help="dir of per-hour WindNinja wind grids; detects MM-DD-YYYY_HHMM "
                        "_vel.asc/_ang.asc pairs - coverage gate + window inference")
    p.add_argument("--plot", default=None, metavar="PNG",
                   help="write a 6-panel timeseries figure (temperature, RH, wind "
                        "speed/direction, cloud, precip) of the generated .wxs to "
                        "this path; requires matplotlib")
    return p


def resolve_timezone(tz_name, lat, lon):
    """Return (ZoneInfo, standard-offset timedelta)."""
    if tz_name is None:
        try:
            from timezonefinder import TimezoneFinder
        except ImportError:
            die("timezonefinder not installed and --row-timezone not given")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tz_name = TimezoneFinder().timezone_at(lat=lat, lng=lon)
        if tz_name is None:
            die("cannot derive timezone from lat/lon; pass --row-timezone explicitly")
    try:
        zi = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        die(f"'{tz_name}' is not a valid IANA timezone (e.g. America/Los_Angeles)")
    # Fixed STANDARD offset: min of the Jan/Jul solstice UTC offsets - DST always
    # shifts the offset forward, so the smaller one is the standard (non-DST) one
    # in both hemispheres.
    std_off = min(zi.utcoffset(datetime(2025, 1, 1)), zi.utcoffset(datetime(2025, 7, 1)))
    return zi, std_off


# ---------------------------------------------------------------------------
# run-root wind-grid detection / coverage gate / window inference
# ---------------------------------------------------------------------------
_STAMP_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})_(\d{4})")


def scan_run_root(run_root):
    """Detect covered whole-hour UTC slots from _vel.asc/_ang.asc pairs."""
    vel = set()
    ang = set()
    for p in Path(run_root).rglob("*"):
        if not p.is_file():
            continue
        if not (p.name.endswith("_vel.asc") or p.name.endswith("_ang.asc")):
            continue
        m = _STAMP_RE.search(p.name)
        if not m:
            continue
        mm, dd, yyyy, hhmm = (int(g) for g in m.groups())
        hour, minute = divmod(hhmm, 100)
        if minute != 0:
            die(f"frame '{p.name}' has non-zero minute; wind grids must be hourly")
        key = (yyyy, mm, dd, hour)
        if p.name.endswith("_vel.asc"):
            vel.add(key)
        else:
            ang.add(key)
    return vel & ang  # a local hour counts only when BOTH a vel and an ang exist


def covered_utc_hours(run_root, std_off):
    """Map run-root local-frame hours to whole-hour UTC datetimes."""
    keys = scan_run_root(run_root)
    utcs = set()
    for yyyy, mm, dd, hour in keys:
        local = datetime(yyyy, mm, dd, hour)
        utcs.add(local - std_off)  # std_off is UTC-relative: local = utc + std_off
    return utcs


def wind_gate(burn_start, burn_end, utframes_by_hour, run_root):
    """Assert every burn-window hour is covered by a run-root wind pair."""
    missing = [h for h in _hours_inclusive(burn_start, burn_end)
               if h not in utframes_by_hour]
    if missing:
        shown = ", ".join(f"{h:%Y-%m-%dT%H:%M}Z" for h in missing[:10])
        more = f" (+{len(missing)-10} more)" if len(missing) > 10 else ""
        die(f"wind-coverage gate failed: no wind pair for {shown}{more} "
            f"in the burn window; run-root: {run_root}")
    return len(_hours_inclusive(burn_start, burn_end))


def _hours_inclusive(start_utc, end_utc):
    out = []
    t = start_utc
    while t <= end_utc:
        out.append(t)
        t += timedelta(hours=1)
    return out


# ---------------------------------------------------------------------------
# Phase A - download + persist six-band subset gribs
# ---------------------------------------------------------------------------
_MAX_FETCH_RETRIES = 3
_WANTED_SHORTS = {s for s, _, _ in WANTED}


def _subset_ok(cf):
    """True if the cached subset decodes all six wanted shortNames.

    Herbie's subset download reads byte ranges with urllib, which does not raise
    when the server closes the connection early - a truncated file can land in
    the cache looking non-empty. This check surfaces that immediately so fetch()
    can delete and retry, instead of Phase B failing on a missing variable.
    """
    import cfgrib
    import xarray as xr
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", (FutureWarning, UserWarning))
            dss = cfgrib.open_datasets(str(cf), backend_kwargs={"indexpath": ""})
            names = (set(dss[0].data_vars) if len(dss) == 1
                     else set(xr.merge(dss, compat="override", join="override").data_vars))
        return _WANTED_SHORTS <= names
    except Exception:  # noqa: BLE001 - truncated/unreadable file
        return False


def phase_a(utc_hours, cache_dir, threads):
    from herbie import FastHerbie

    def fname(h):
        return cache_dir / f"hrrr_{h:%Y%m%d%H}.grib2"

    uncached = [h for h in utc_hours
                if not (fname(h).exists() and fname(h).stat().st_size > 0)]
    if uncached:
        print(f"phase A: fetching {len(uncached)} of {len(utc_hours)} hours into {cache_dir}")
    else:
        print(f"phase A: all {len(utc_hours)} hours already cached - offline")
        return None

    fh = FastHerbie(uncached, fxx=[0], model="hrrr", product="sfc",
                    max_threads=min(threads, len(uncached)),
                    save_dir=str(cache_dir), verbose=False)
    missing = [H.date for H in fh.file_not_exists]
    if missing:
        mh = sorted(missing)
        shown = ", ".join(f"{h:%Y%m%d%H}" for h in mh[:10])
        more = f" (+{len(mh)-10} more)" if len(mh) > 10 else ""
        die(f"HRRR sfc fxx=0 not found for: {shown}{more}")

    first = min(fh.objects, key=lambda H: H.date)
    regex = resolve_search_regex(first)
    print(f"phase A: subset regex = {regex}")

    def fetch(H):
        h = H.date
        cf = fname(h)
        if cf.exists() and cf.stat().st_size > 0 and _subset_ok(cf):
            return h, None
        last_err = "subset failed to decode"
        for attempt in range(_MAX_FETCH_RETRIES):
            cf.unlink(missing_ok=True)
            try:
                H.download(search=regex)
                src = Path(H.get_localFilePath(regex))
                shutil.move(str(src), str(cf))
                if _subset_ok(cf):
                    return h, None
                last_err = f"subset truncated/missing variable(s) (attempt {attempt + 1})"
            except Exception as e:  # noqa: BLE001 - per-hour failures
                last_err = f"{e} (attempt {attempt + 1})"
            if attempt < _MAX_FETCH_RETRIES - 1:
                time.sleep(1 + attempt)
        return h, last_err

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(threads, len(uncached))) as ex:
        futures = {ex.submit(fetch, H) for H in fh.objects}
        completions = concurrent.futures.as_completed(futures)
        with tqdm(total=len(futures), desc="downloading", unit="h",
                  mininterval=0.25) as bar:
            for fut in completions:
                h, err = fut.result()
                if err is not None:
                    failures.append((h, err))
                bar.update(1)
    if failures:
        shown = ", ".join(f"{h:%Y%m%d%H} ({e})" for h, e in failures[:5])
        die(f"fetch failed for {len(failures)} hour(s): {shown}")
    # verify every requested hour is on disk and non-empty
    bad = [h for h in uncached
           if not (fname(h).exists() and fname(h).stat().st_size > 0)]
    if bad:
        die(f"missing/empty cache after fetch: {', '.join(sorted(f'{h:%Y%m%d%H}' for h in bad))}")
    return regex


def resolve_search_regex(H):
    """Validate the six wanted vars against a Herbie inventory; build one regex."""
    inv = H.inventory()
    parts = []
    for short, grib_name, level in WANTED:
        frag = f":{grib_name}:{level}:"
        hits = inv[inv["search_this"].str.contains(frag, regex=False)]["search_this"].tolist()
        if not hits:
            die(f"variable '{short}' ({grib_name} {level}) not found in inventory for {H.date:%Y-%m-%d %H:%M}Z")
        parts.append(hits[0])
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Phase B - offline conversion
# ---------------------------------------------------------------------------
def open_cached(cache_file):
    """Open a cached six-band subset as a merged xarray Dataset."""
    import cfgrib
    import xarray as xr
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        dss = cfgrib.open_datasets(str(cache_file), backend_kwargs={"indexpath": ""})
        if len(dss) == 1:
            return dss[0].compute()
        return xr.merge(dss, compat="override", join="override").compute()


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _dist2(lat, lon, lat_arr, lon_arr):
    """Squared degree-distance to the anchor; NaN cells -> inf."""
    dlat = lat_arr - lat
    dlon = ((lon_arr - lon + 180) % 360) - 180  # shortest way around -180/180
    d2 = dlat * dlat + dlon * dlon
    d2 = np.where(np.isnan(lat_arr) | np.isnan(lon_arr), np.inf, d2)
    return d2


def choose_cell(ds, lat, lon, dem, tol_ft, dem_window_radius=25):
    """Reprojection-safe representative cell: nearest, then elevation-matched."""
    lat2d = ds["latitude"].values
    lon2d = ds["longitude"].values
    ny, nx = lat2d.shape

    d2 = _dist2(lat, lon, lat2d, lon2d)
    iy0, ix0 = np.unravel_index(int(np.nanargmin(d2)), d2.shape)

    # demonstrate DEM coverage at the nearest cell (else hard error)
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", dem.crs, always_xy=True)
    nd = dem.nodata  # explicit fill (e.g. LANDFIRE LCP -9999 outside the landscape)
    xi, yi = t.transform(float(lon2d[iy0, ix0]), float(lat2d[iy0, ix0]))
    try:
        target_m = float(next(dem.sample([(xi, yi)], masked=True))[0])
    except Exception as e:  # noqa: BLE001
        die(f"cannot sample DEM '{dem.name}' at nearest cell "
            f"({lat2d[iy0, ix0]:.4f}, {lon2d[iy0, ix0]:.4f}): {e}")
    if target_m is None or np.isnan(target_m) or (nd is not None and target_m == nd):
        die(f"DEM '{dem.name}' has no data at nearest cell "
            f"({lat2d[iy0, ix0]:.4f}, {lon2d[iy0, ix0]:.4f}); "
            "does the raster cover the anchor?")
    target_ft = target_m * 3.28084

    # elevation-matched refinement over a window around the nearest cell
    y0, y1 = max(0, iy0 - dem_window_radius), min(ny, iy0 + dem_window_radius + 1)
    x0, x1 = max(0, ix0 - dem_window_radius), min(nx, ix0 + dem_window_radius + 1)
    wlat = lat2d[y0:y1, x0:x1]
    wlon = lon2d[y0:y1, x0:x1]

    lons = wlon.ravel()
    lats = wlat.ravel()
    xs, ys = t.transform(lons, lats)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # masked->nan is intended
        samples = np.array([
            np.nan if v is None or (nd is not None and float(v[0]) == nd)
            else float(v[0])
            for v in dem.sample(list(zip(xs, ys)), masked=True)])
    elems_m = samples.reshape(wlat.shape)
    match = (np.abs(elems_m * 3.28084 - target_ft) <= tol_ft)
    d2w = _dist2(lat, lon, wlat, wlon)
    d2w = np.where(match, d2w, np.inf)
    d2w = np.where(np.isnan(elems_m), np.inf, d2w)
    if not np.isfinite(d2w).any():
        print("  warning: no DEM cell within elevation tolerance; using plain nearest cell")
        iy, ix = iy0, ix0
        dem_ft = target_ft
    else:
        k = int(np.nanargmin(d2w))
        iy, ix = y0 + k // wlat.shape[1], x0 + k % wlat.shape[1]
        dem_ft = float(elems_m.ravel()[k]) * 3.28084

    # HRRR longitudes are 0..360 (CF convention); normalize to -180..180
    clat, clon = float(lat2d[iy, ix]), ((float(lon2d[iy, ix]) + 180) % 360) - 180
    dkm = haversine_km(lat, lon, clat, clon)
    if dkm > 5.0:
        print(f"  warning: representative cell {dkm:.1f} km from anchor "
              f"(HRRR spacing ~3 km)")
    return iy, ix, clat, clon, dem_ft, target_ft


def cell_value(ds, var, iy, ix, h, clat, clon, allow_fill=None):
    v = float(ds[var].values[iy, ix])
    if np.isnan(v):
        if allow_fill is not None:
            return allow_fill
        die(f"NaN for '{var}' at UTC hour {h:%Y-%m-%dT%H:%M}Z at cell "
            f"({clat:.4f}, {clon:.4f})")
    return v


def plot_wxs(wxs_path, out_png):
    """Render .wxs rows as a stacked 6-panel timeseries (meteogram).

    The six variables cannot share one axis (wind direction 0-360, precip
    ~0-0.5 in, temp ~40-90 F...): a common-y plot flattens most series, and six
    separate figures would break time alignment. The feasible single-artifact
    presentation is one figure with vertically stacked panels on one time axis.
    """
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    metric = False
    rows = []
    header_ok = False
    with open(wxs_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("raws_units"):
                metric = "metric" in low
                continue
            if low.startswith("raws_elevation"):
                continue
            if low.startswith("year") or line.startswith("Year "):
                header_ok = True
                continue
            if not header_ok:
                continue
            parts = line.split()
            if len(parts) != 10:
                continue
            try:
                y, mo, d, t = (int(parts[i]) for i in range(4))
                temp, rh = int(parts[4]), int(parts[5])
                pcp = float(parts[6])
                wspd, wdir, cc = int(parts[7]), int(parts[8]), int(parts[9])
            except ValueError:
                continue
            rows.append((datetime(y, mo, d, t // 100, t % 100),
                         temp, rh, pcp, wspd, wdir, cc))
    if not rows:
        die(f"no timeseries rows found in {wxs_path}")
    times = [r[0] for r in rows]
    t_, rh = [r[1] for r in rows], [r[2] for r in rows]
    pcp = [r[3] for r in rows]
    wspd, wdir, cc = [r[4] for r in rows], [r[5] for r in rows], [r[6] for r in rows]

    tunit = "C" if metric else "F"
    punit = "mm" if metric else "in"
    wunit = "km/h" if metric else "mph"

    fig, axes = plt.subplots(6, 1, sharex=True, figsize=(12, 15))
    fig.suptitle(f"{wxs_path} - weather stream ({len(rows)} obs)",
                 fontsize=13, y=0.98)

    def draw(ax, series, label, color, kind):
        if kind == "bar":
            ax.bar(times, series, width=0.03, color=color, align="center")
        elif kind == "scatter":
            ax.scatter(times, series, s=9, color=color)
        else:
            ax.plot(times, series, color=color, lw=1.4, ms=3)
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.3)

    draw(axes[0], t_, f"Temperature (deg {tunit})", "#d62728", "line")
    draw(axes[1], rh, "Relative humidity (%)", "#1f77b4", "line")
    draw(axes[2], wspd, f"Wind speed ({wunit})", "#2ca02c", "line")
    draw(axes[3], wdir, "Wind direction (deg FROM)", "#9467bd", "scatter")
    axes[3].set_ylim(0, 360)
    draw(axes[4], cc, "Cloud cover (%)", "#7f7f7f", "line")
    draw(axes[5], pcp, f"Hourly precip ({punit})", "#17becf", "bar")

    loc = mdates.AutoDateLocator(minticks=6, maxticks=12)
    axes[5].xaxis.set_major_locator(loc)
    axes[5].xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    axes[5].set_xlabel("Local time (fire-area clock)")
    for ax in axes[:-1]:
        plt.setp(ax.get_xticklabels(), visible=False)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"plot: {out}")


def dem_anchor(lat, lon, dem):
    """Resolve the anchor: explicit --lat/--lon, else infer it from the DEM.

    Inference is the valid-data centroid of band 1 (nodata masked out); an
    all-nodata band falls back to the raster-bounds center with a warning.
    Returns (lat, lon, used_inference).
    """
    if lat is not None and lon is not None:
        return lat, lon, False
    band = dem.read(1).astype("float64")
    nd = dem.nodata
    if nd is not None:
        band = np.where(band == nd, np.nan, band)
    ys, xs = np.nonzero(np.isfinite(band))
    if ys.size == 0:
        print(f"  warning: DEM '{dem.name}' has no valid band-1 pixels; "
              "using raster-bounds center as anchor")
        row, col = (band.shape[0] - 1) / 2.0, (band.shape[1] - 1) / 2.0
    else:
        row, col = float(ys.mean()), float(xs.mean())
    from pyproj import Transformer
    tr = Transformer.from_crs(dem.crs, "EPSG:4326", always_xy=True)
    x, y = dem.xy(row, col)
    lon, lat = tr.transform(x, y)
    return lat, lon, True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    args = build_parser().parse_args(argv)

    if bool(args.lat) != bool(args.lon):
        die("give both --lat and --lon, or neither to infer the anchor from --dem")
    if args.lat is not None:
        if not (-90 <= args.lat <= 90):
            die(f"--lat {args.lat} out of range [-90, 90]")
        if not (-180 <= args.lon <= 180):
            die(f"--lon {args.lon} out of range [-180, 180]")

    # UTC window: both given, or both omitted WITH --run-root (inferred later)
    if bool(args.start) != bool(args.end):
        die("give both --start and --end, or neither with --run-root")
    if args.start is None and args.run_root is None:
        die("--start/--end required unless --run-root detects the window")

    dem_path = Path(args.dem)
    if not dem_path.is_file():
        die(f"--dem file not found: {dem_path}")
    import rasterio
    dem = rasterio.open(dem_path)

    lat, lon, anchor_inferred = dem_anchor(args.lat, args.lon, dem)
    print(f"anchor: ({lat:.6f}, {lon:.6f}) "
          + ("[inferred from DEM band-1 centroid]" if anchor_inferred
             else "[explicit]"))

    tzinfo, std_off = resolve_timezone(args.row_timezone, lat, lon)

    # detect run-root frames first (basis for inference and the coverage gate)
    brun = covered_utc_hours(args.run_root, std_off) if args.run_root else None
    if args.start is None:
        if not brun:
            die(f"no _vel.asc/_ang.asc pairs found under run-root: {args.run_root}")
        utc_start = min(brun)
        utc_end = max(brun)
        inferred = (utc_start, utc_end)
        print(f"inferred window: {utc_start:%Y-%m-%dT%H:%M}Z .. {utc_end:%Y-%m-%dT%H:%M}Z "
              f"from {len(brun)} run-root frames")
    else:
        utc_start = parse_utc(args.start)
        utc_end = parse_utc(args.end)
        if utc_end < utc_start:
            die(f"--end {args.end} is before --start {args.start}")
        inferred = None

    burn_start, burn_end = utc_start, utc_end
    lead_start = utc_start - timedelta(days=args.lead_days)
    utc_hours = []
    t = lead_start
    while t <= utc_end:
        utc_hours.append(t)
        t += timedelta(hours=1)

    print(f"hours: {len(utc_hours)} ({args.lead_days} lead day(s)); burn window "
          f"{burn_start:%Y-%m-%dT%H:%M}Z .. {burn_end:%Y-%m-%dT%H:%M}Z")

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    phase_a(utc_hours, cache_dir, args.threads)

    # ---- Phase B: offline conversion -----------------------------------
    ds0_file = cache_dir / f"hrrr_{utc_hours[0]:%Y%m%d%H}.grib2"
    if not (ds0_file.exists() and ds0_file.stat().st_size > 0):
        die(f"cache file missing/invalid: {ds0_file}; delete it and re-run to refetch")
    try:
        ds0 = open_cached(ds0_file)
    except Exception as e:  # noqa: BLE001
        die(f"failed to open cached {ds0_file} for {utc_hours[0]:%Y%m%d%H}: {e}; "
            "delete it and re-run to refetch")
    m0 = [s for s, _, _ in WANTED if s not in ds0]
    if m0:
        die(f"cached {ds0_file} for {utc_hours[0]:%Y%m%d%H} is incomplete - missing "
            f"{', '.join(m0)}; delete it and re-run to refetch")
    iy, ix, clat, clon, dem_ft, _tgt = choose_cell(ds0, lat, lon, dem,
                                                   args.elevation_tol_ft)
    raws_elev = int(round(dem_ft))
    print(f"cell: ({clat:.4f}, {clon:.4f}); RAWS_ELEVATION: {raws_elev} ft")

    rows = []
    dump_rows = []
    missing = {s: 0 for s, _, _ in WANTED}
    for h in utc_hours:
        cf = cache_dir / f"hrrr_{h:%Y%m%d%H}.grib2"
        if not (cf.exists() and cf.stat().st_size > 0):
            die(f"cache file missing/invalid: {cf}; delete it and re-run to refetch")
        try:
            ds = open_cached(cf)
        except Exception as e:  # noqa: BLE001
            die(f"failed to open cached {cf} for {h:%Y%m%d%H}: {e}; "
                "delete it and re-run to refetch")
        missing_vars = [s for s, _, _ in WANTED if s not in ds]
        if missing_vars:
            die(f"cached {cf} for {h:%Y%m%d%H} is incomplete - missing "
                f"{', '.join(missing_vars)}; delete it and re-run to refetch")

        t2m = cell_value(ds, "t2m", iy, ix, h, clat, clon)
        rh = cell_value(ds, "r2", iy, ix, h, clat, clon)
        apcp = cell_value(ds, "tp", iy, ix, h, clat, clon, allow_fill=0.0)
        tcc = cell_value(ds, "tcc", iy, ix, h, clat, clon)
        u10 = cell_value(ds, "u10", iy, ix, h, clat, clon)
        v10 = cell_value(ds, "v10", iy, ix, h, clat, clon)

        temp_f = int(round(t2m * 9 / 5 - 459.67))
        rh_i = max(0, min(99, int(round(rh))))
        pcp_in = max(0.0, apcp / 25.4)
        wspd = int(round(math.hypot(u10, v10) * 2.23694))
        wdir = int(round((270 - math.degrees(math.atan2(v10, u10))) % 360))
        cloud = max(0, min(100, int(round(tcc if tcc > 1 else tcc * 100))))

        local = h + std_off
        rows.append(f"{local.year} {local.month} {local.day} {local.hour:02d}00 "
                    f"{temp_f} {rh_i} {pcp_in:.2f} {wspd} {wdir} {cloud}")
        dump_rows.append((h, local, t2m, rh, apcp, u10, v10, tcc / 100, clat, clon))
        for s, _, _ in WANTED:
            if np.isnan(float(ds[s].values[iy, ix])):
                missing[s] += 1

    # ---- coverage gate (only the non-lead burn window) -------------------
    mode = "standalone"
    n_covered = 0
    if args.run_root:
        n_covered = wind_gate(burn_start, burn_end, brun, args.run_root)
        mode = "run-root"
    if mode == "run-root":
        print(f"wind coverage: OK ({n_covered} of {len(utc_hours)} burn hours) [mode: run-root]")
    else:
        print("wind coverage: skipped (standalone mode; --run-root not given)")

    # ---- dump + write ----------------------------------------------------
    if args.dump_dir:
        import csv
        dump_dir = Path(args.dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
        with open(dump_dir / "samples.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(SAMPLE_COLS.split(","))
            for h, local, t2m, rh, apcp, u10, v10, tcc, clat, clon in dump_rows:
                w.writerow([h.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            local.strftime("%Y-%m-%d %H:%M"),
                            f"{t2m:.4f}", f"{rh:.4f}", f"{apcp:.4f}",
                            f"{u10:.4f}", f"{v10:.4f}", f"{tcc:.4f}",
                            f"{clat:.6f}", f"{clon:.6f}"])
        print(f"audit dump: {dump_dir / 'samples.csv'}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = "Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov"
    body = "\n".join(["RAWS_UNITS: ENGLISH",
                      f"RAWS_ELEVATION: {raws_elev}",
                      header, *rows])
    with open(out, "w") as f:
        f.write(body + "\n")
    if args.plot:
        plot_wxs(out, args.plot)

    # ---- summary -----------------------------------------------------------
    first, last = utc_hours[0], utc_hours[-1]
    tzname = tzinfo.key
    print(f"output: {out}")
    print(f"rows: {len(rows)}")
    if inferred:
        print(f"inferred window: {inferred[0]:%Y-%m-%dT%H:%M}Z .. "
              f"{inferred[1]:%Y-%m-%dT%H:%M}Z from {len(brun)} run-root frames")
    print(f"first: {first:%Y-%m-%dT%H:%M}Z == {(first + std_off):%Y-%m-%d %H:%M} {tzname}")
    print(f"last:  {last:%Y-%m-%dT%H:%M}Z == {(last + std_off):%Y-%m-%d %H:%M} {tzname}")
    if mode == "run-root":
        print(f"wind coverage: OK ({n_covered} of {len(utc_hours)} burn hours) [mode: run-root]")
    else:
        print("wind coverage: skipped (standalone mode; --run-root not given)")
    print(f"representative cell: ({clat:.4f}, {clon:.4f})")
    print(f"RAWS_ELEVATION: {raws_elev}")
    print(f"sample row: {rows[len(rows) // 2]}")
    print(f"missing per var: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
