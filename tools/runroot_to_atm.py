#!/usr/bin/env python3
"""runroot_to_atm - build a FARSITE Atmosphere Grid (.atm) from a run root of
WindNinja wind grids (the same folder layout as hrrr_to_wxs --run-root).

FARSITE's Gridded Winds From ATM File input (switch FARSITE_ATM_FILE) is a
manifest that pairs each burn-window date/time with a wind SPEED grid and a wind
DIRECTION grid. WindNinja's native atmosphere output is the format produced here:

    WINDS
    ENGLISH            # speed grids interpreted as mph@20ft
    <Mth> <Day> <HHMM> <speed.asc> <dir.asc>    # one line per wind set, ascending

with every speed/direction grid in the SAME folder as the .atm. FARSITE keeps a
wind set in force until a later row supersedes it, so one row per detected frame
covers the burn window (same convention as the .wxs row clock / burn periods).

run2-style folders hold terrain-resolved WindNinja grids named
<prefix>_MM-DD-YYYY_HHMM_..._vel.asc/_ang.asc. Their speed values are in m/s, so
this tool converts them to mph (--units mph; or km/h with --units kmh), RESAMPLES
every grid onto the landscape/LCP grid from --dem (FlamMap 6 requires wind grids
to be the SAME cell size and extent as the landscape file - WindNinja's run2 mesh
is 55.53 m while palisades.tif is 30 m), writes the landscape CRS .prj beside
each grid, and emits the 2-column WindNinja-native .atm. Direction grids are
normalized to 0-359 (360 -> 0). The 3-km PASTCAST-GCP-* tiles are WindNinja
*inputs*, not output, and are excluded - a burn hour without a terrain-resolved
pair is a hard error, mirroring the hrrr_to_wxs wind-coverage gate.

No network access in any code path. Row times are the run2 local stamps; they
must sit on the same clock as the .wxs rows / FARSITE burn periods (they do for
standard-time runs such as the January Palisades window).
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from hrrr_to_wxs import die, _STAMP_RE, parse_utc, resolve_timezone

MPS_TO_MPH = 2.23694
MPS_TO_KMH = 3.6
_IGNORE_PREFIXES = ("PASTCAST-GCP-",)
_NUM_HEADER_LINES = 6


def build_parser():
    p = argparse.ArgumentParser(
        prog="runroot_to_atm.py",
        description="Build a WindNinja-native FARSITE Atmosphere Grid (.atm) from "
                    "a run-root of per-hour wind pairs.",
        epilog="--start/--end are UTC; --row-timezone matches them to the run-root "
               "local frames. Row times in the .atm are the run2 local stamps.",
    )
    p.add_argument("--run-root", default=None,
                   help="dir of per-hour WindNinja _vel.asc/_ang.asc pairs "
                        "(same layout as hrrr_to_wxs --run-root)")
    p.add_argument("--dem", default=None,
                   help="the fire-area landscape/LCP raster; wind grids are "
                        "resampled onto its grid (same cell size and extent, "
                        "required by FlamMap 6)")
    p.add_argument("--row-timezone", default=None,
                   help="IANA name of the fire-local clock; required when "
                        "--start/--end are given, optional otherwise")
    p.add_argument("--start", help="UTC start %%Y-%%m-%%dT%%H:%%M (optional trailing Z)")
    p.add_argument("--end", help="UTC end %%Y-%%m-%%dT%%H:%%M (optional trailing Z)")
    p.add_argument("--lead-days", type=int, default=0,
                   help="also emit rows for conditioning-lead hours before the "
                        "burn window (default 0)")
    p.add_argument("--units", default="mph", choices=("mph", "kmh"),
                   help="speed-grid units written under the units header; the "
                        "source m/s grids are converted accordingly (default mph)")
    p.add_argument("--out", default="FireBehaviorModels/SampleData/Palisades/palisades-hrrr.atm",
                   help="output .atm path; converted grids are written beside it "
                        "with their source basenames (default "
                        "FireBehaviorModels/SampleData/Palisades/palisades-hrrr.atm)")
    p.add_argument("--verify", default=None, metavar="ATM",
                   help="integrity-check the grids referenced by this .atm (they "
                        "must sit beside it): every grid must exist, be non-empty, "
                        "and decode to exactly its header ncols x nrows; reports "
                        "corrupt/missing files. Nothing else is required/run.")
    return p


def scan_frames(run_root):
    """Return {local stamp: (vel_path, ang_path)} for terrain-resolved pairs.

    A stamp counts only when BOTH a _vel.asc and a _ang.asc exist outside the
    input-family prefixes (PASTCAST-GCP-*). Non-hourly or one-sided stamps are
    ignored (and will show up as gate gaps if they fall in the window).
    """
    vel, ang = {}, {}
    for p in Path(run_root).rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if not (name.endswith("_vel.asc") or name.endswith("_ang.asc")):
            continue
        m = _STAMP_RE.search(name)
        if not m:
            continue
        mm, dd, yyyy, hhmm = (int(g) for g in m.groups())
        hour, minute = divmod(hhmm, 100)
        if minute != 0:
            die(f"frame '{p.name}' has non-zero minute; wind grids must be hourly")
        stamp = (yyyy, mm, dd, hour)
        if name.startswith(_IGNORE_PREFIXES):
            continue  # WindNinja input tile, not a terrain-resolved output
        bucket = vel if name.endswith("_vel.asc") else ang
        bucket.setdefault(stamp, []).append(p)
    frames = {}
    for stamp in sorted(set(vel) | set(ang)):
        vs, as_ = vel.get(stamp, []), ang.get(stamp, [])
        if vs and as_:
            vs.sort(key=lambda p: p.name)
            as_.sort(key=lambda p: p.name)
            frames[stamp] = (vs[0], as_[0])
    return frames


def read_asc(path):
    """Parse an ESRI ASCII grid; returns (meta dict, data ndarray)."""
    with open(path) as f:
        lines = [f.readline() for _ in range(_NUM_HEADER_LINES)]
        data = np.loadtxt(f, dtype="float64")
    meta = {}
    for line in lines:
        k, v = line.split(None, 1)
        k = k.lower()
        meta[k] = int(v) if k in ("ncols", "nrows") else float(v)
    meta["_lines"] = lines
    return meta, data


def _resample(src, dem, out_dir, transform):
    """Resample a wind grid onto the landscape (--dem) grid, nearest neighbor.

    FlamMap 6 requires wind grids to have the SAME cell size, extent, datum and
    projection as the landscape file; WindNinja's run2 mesh (55.53 m) is
    resampled here onto the LCP grid (30 m). Nearest keeps speed values unblended
    and avoids averaging direction across the 0/360 seam.
    """
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, Resampling

    meta, data = read_asc(src)
    nd = meta.get("nodata_value")
    src_aff = from_origin(meta["xllcorner"],
                          meta["yllcorner"] + meta["nrows"] * meta["cellsize"],
                          meta["cellsize"], meta["cellsize"])
    prj = src.with_suffix(".prj")
    if prj.exists():
        try:
            src_crs = rasterio.crs.CRS.from_wkt(prj.read_text())
        except Exception:  # noqa: BLE001
            src_crs = dem.crs
    else:
        src_crs = dem.crs
    if src_crs.to_epsg() and dem.crs.to_epsg() and src_crs.to_epsg() != dem.crs.to_epsg():
        print(f"  note: {src.name} CRS {src_crs.to_epsg()} differs from landscape "
              f"{dem.crs.to_epsg()}; reprojecting")
    vals = np.ascontiguousarray(transform(data), dtype="float32")
    dst = np.full((dem.height, dem.width), -9999.0, dtype="float32")
    reproject(vals, dst,
              src_transform=src_aff, src_crs=src_crs,
              dst_transform=dem.transform, dst_crs=dem.crs,
              src_nodata=nd, dst_nodata=-9999.0,
              resampling=Resampling.nearest)

    out = out_dir / src.name
    # Byte-format parity with WindNinja's native ASCII output: TAB-separated
    # keys and values, one value per cell with %.2f, a trailing tab per data row,
    # 6-decimal header values, CRLF line endings. FlamMap/Core grid readers
    # tokenize on tabs; a space-separated/LF file fails at grid creation.
    with open(out, "w", newline="") as f:
        f.write(f"ncols\t{dem.width}\r\n")
        f.write(f"nrows\t{dem.height}\r\n")
        f.write(f"xllcorner\t{dem.bounds.left:.6f}\r\n")
        f.write(f"yllcorner\t{dem.bounds.bottom:.6f}\r\n")
        f.write(f"cellsize\t{dem.res[0]:.6f}\r\n")
        f.write("NODATA_value\t-9999.000000\r\n")
        for row in dst:
            f.write("\t".join(f"{v:.2f}" for v in row) + "\t\r\n")
    # CRS sidecar = the landscape CRS, guaranteeing grid/landscape agreement
    (out_dir / src.name.replace(".asc", ".prj")).write_text(dem.crs.to_wkt())
    return out


def write_vel_asc(src, dem, out_dir, mult):
    """Speed grid: scale m/s -> mph or km/h, then resample to the landscape grid."""
    return _resample(src, dem, out_dir, lambda d: d * mult)


def write_ang_asc(src, dem, out_dir):
    """Direction grid: normalize 0-359 (360 -> 0), resample to the landscape grid.

    A 360 value is rejected by the wind-direction grid validator; WindNinja
    emits 360 for north.
    """
    return _resample(src, dem, out_dir, lambda d: d % 360)


def _hours_inclusive(start, end):
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def _grid_ok(path):
    """True if an ASCII grid parses to exactly (nrows, ncols) numeric cells."""
    try:
        with open(path) as f:
            meta = {}
            for _ in range(_NUM_HEADER_LINES):
                k, v = f.readline().split(None, 1)
                meta[k.lower()] = int(v) if k.lower() in ("ncols", "nrows") else float(v)
            data = np.loadtxt(f)
        return tuple(data.shape) == (meta["nrows"], meta["ncols"])
    except Exception:  # noqa: BLE001 - unreadable/truncated/non-numeric
        return False


def verify_atm(atm_path):
    """Check every grid referenced by an .atm for presence + decodability.

    Returns (rows_found, ok, missing, corrupt). A grid that is truncated,
    zero-byte, or partially written by an interrupted copy fails exactly the way
    FARSITE reports "Error creating {Wind Speed|Wind Direction} grid".
    """
    atm = Path(atm_path)
    d = atm.parent
    if not atm.is_file():
        die(f"ATM file not found: {atm}")
    rows = [l for l in atm.read_bytes().split(b"\r\n") if l.strip()][2:]
    refs = []
    for l in rows:
        p = l.split()
        if len(p) == 5:
            refs += [p[3].decode(), p[4].decode()]
    missing, corrupt = [], []
    for r in refs:
        f = d / r
        if not (f.exists() and f.stat().st_size > 0):
            missing.append(r)
        elif not _grid_ok(f):
            corrupt.append(r)
    return len(rows), len(refs) - len(missing) - len(corrupt), missing, corrupt


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.verify:
        n_rows, n_ok, missing, corrupt = verify_atm(args.verify)
        print(f"verify {args.verify}: {n_rows} rows, {n_ok} grids OK, "
              f"{len(missing)} missing, {len(corrupt)} corrupt")
        for g in sorted(corrupt)[:10]:
            print(f"  CORRUPT: {g}")
        for g in sorted(missing)[:10]:
            print(f"  MISSING: {g}")
        if missing or corrupt:
            print("FAIL: re-copy the grids from a clean generation (e.g. "
                  "/tmp/atm-burn) into this folder")
            return 2
        print("VERIFIED: all referenced grids present and decodable")
        return 0

    if not args.run_root:
        die("--run-root is required unless --verify is used")
    if not args.dem:
        die("--dem is required unless --verify is used")

    if bool(args.start) != bool(args.end):
        die("give both --start and --end, or neither")
    if args.start is not None and args.row_timezone is None:
        die("--start/--end are UTC; give --row-timezone to match them against "
            "the run-root local frames")

    std_off = None
    if args.row_timezone:
        _tzinfo, std_off = resolve_timezone(args.row_timezone, 0, 0)

    frames = scan_frames(args.run_root)
    if not frames:
        die(f"no terrain-resolved _vel.asc/_ang.asc pairs found under "
            f"run-root: {args.run_root}")
    frame_dt = {s: datetime(s[0], s[1], s[2], s[3]) for s in frames}
    print(f"frames: {len(frames)} terrain-resolved wind pairs under {args.run_root}")

    dem_path = Path(args.dem)
    if not dem_path.is_file():
        die(f"--dem file not found: {dem_path}")
    import rasterio
    dem = rasterio.open(dem_path)

    mult = MPS_TO_MPH if args.units == "mph" else MPS_TO_KMH
    units_word = "ENGLISH" if args.units == "mph" else "METRIC"

    # ---- window selection + coverage gate --------------------------------
    if args.start is not None:
        utc_start = parse_utc(args.start)
        utc_end = parse_utc(args.end)
        if utc_end < utc_start:
            die(f"--end {args.end} is before --start {args.start}")
        burn_l0 = utc_start + std_off
        burn_l1 = utc_end + std_off
        lead_l0 = utc_start - timedelta(days=args.lead_days) + std_off
        sel = sorted(s for s, dt in frame_dt.items() if lead_l0 <= dt <= burn_l1)
        covered = {dt for s, dt in frame_dt.items() if burn_l0 <= dt <= burn_l1}
        missing = [h for h in _hours_inclusive(burn_l0, burn_l1) if h not in covered]
        if missing:
            shown = ", ".join(h.strftime("%m-%d %H:%M") for h in missing[:10])
            more = f" (+{len(missing)-10} more)" if len(missing) > 10 else ""
            die(f"wind-coverage gate failed: no terrain-resolved wind pair for "
                f"{shown}{more} in the burn window; run-root: {args.run_root}")
    else:
        sel = sorted(frames)
        burn_l0 = frame_dt[sel[0]]
        burn_l1 = frame_dt[sel[-1]]
    print(f"burn window (local): {burn_l0:%m-%d %H:%M} .. {burn_l1:%m-%d %H:%M}")

    # ---- write converted grids + .atm ------------------------------------
    out = Path(args.out)
    grid_dir = out.parent
    grid_dir.mkdir(parents=True, exist_ok=True)
    if grid_dir.resolve() == Path(args.run_root).resolve():
        die("--out would overwrite the source grids; point --out at a folder "
            "separate from --run-root")

    rows = []
    for stamp in sel:
        vel_p, ang_p = frames[stamp]
        write_vel_asc(vel_p, dem, grid_dir, mult)
        write_ang_asc(ang_p, dem, grid_dir)
        m, d, hr = stamp[1], stamp[2], stamp[3]
        # WindNinja zero-pads month/day and writes CRLF line endings
        rows.append(f"{m:02d} {d:02d} {hr:02d}00 {vel_p.name} {ang_p.name}")

    with open(out, "w", newline="") as f:
        f.write("\r\n".join(["WINDS", units_word, *rows]) + "\r\n")

    # ---- summary -----------------------------------------------------------
    print(f"output: {out}")
    if std_off is not None:
        print(f"burn window (UTC): {(burn_l0 - std_off):%Y-%m-%dT%H:%M}Z .. "
              f"{(burn_l1 - std_off):%Y-%m-%dT%H:%M}Z")
    print(f"rows: {len(rows)}  units: {units_word} ({args.units})  "
          f"factor: {mult:g}")
    print(f"grid files: {len(rows) * 2} .asc + {len(rows) * 2} .prj in {grid_dir}")
    print(f"sample row: {rows[len(rows)//2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
