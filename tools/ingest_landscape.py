#!/usr/bin/env python3
"""ingest_landscape - pull a LANDFIRE fire-area landscape from LFPS and
write a FARSITE/WindNinja-ready multi-band GeoTIFF.

The FARSITE/WindNinja pipeline needs a fire-area landscape (elevation +
fuel/canopy layers) as a multi-band raster. LANDFIRE's Product Service
(LFPS) is a fully open public REST API (no auth token; only an email, an
AOI, and a layer list are required) that returns a single multi-band
GeoTIFF. This script submits an LFPS job, polls it, downloads the GeoTIFF,
validates it, and writes a stable FARSITE/WindNinja-ready landscape (plus
an optional standalone band-1 elevation DEM for WindNinja). It does NOT
assemble the FARSITE inputs file, command file, weather, wind grids, or run
`runfarsite` - that orchestration is a separate future orchestrator.

This repo's `runfarsite` loads its landscape as a multi-band raster; the
working sample `FireBehaviorModels/SampleData/BlueMountain/BlueMountain.tif`
is 9 bands (Elev, SlpD, Asp, FBFM40, CC, CH, CBH, CBD, FCCS). WindNinja
reads band 1 (elevation) of an `.lcp/.tif/.asc` via `elevation_file`. So one
LFPS multi-band GeoTIFF serves both.

LFPS endpoints (swagger: https://lfps.usgs.gov/docs/api):
  Submit   POST  https://lfps.usgs.gov/api/job/submit
  Status   GET   https://lfps.usgs.gov/api/job/status?JobId=<id>
  Cancel   GET   https://lfps.usgs.gov/api/job/cancel?JobId=<id>
  Upload   POST  https://lfps.usgs.gov/api/upload/shapefile
  Health   GET   https://lfps.usgs.gov/api/healthCheck
  Products GET   https://lfps.usgs.gov/products   (client-rendered catalog)

Sibling pipeline data sources (documented, NOT implemented here):
  - NOAA HRRR sfc via Herbie `FastHerbie(model="hrrr", product="sfc", fxx=0)` (AWS)
  - WindNinja HRRR pastcast `https://storage.googleapis.com/high-resolution-rapid-refresh/hrrr.<date>/conus/...grib2`
    (via `wx_model_type=PASTCAST-GCP-HRRR-CONUS-3-KM`)
  - CAL FIRE FRAP + NIFC WFIGS arcgis services (in `calfire_ignition.py`)
  - SRTM via OpenTopography (API key)

Limitations:
  - LFPS covers CONUS/AK/HI only (no insular areas); bbox ranges lon -188..-66,
    lat 18..72. A non-covered AOI is surfaced as the LFPS error verbatim.
  - Product download URLs expire ~6 h after a job completes; a 404 on download
    means the bundle expired and the job must be re-submitted.
  - Jobs run ~12 s-7 min typically but can take up to ~2 h; repeated small
    `--bbox` AOIs keep jobs small and fast.
  - Annual-version model: terrain (elevation/slope/aspect) is a static base
    product under LF2020_*; the fuels/canopy products are updated annually
    under LF{--version}_*. There is NO silent version fallback - if a requested
    year's layer 400s with `Invalid products: <codes>`, the script prints that
    verbatim and points the user to the products catalog.
  - `--resolution` must be > 30 (LFPS only coarsens; native is 30 m).
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
import requests

from hrrr_to_wxs import die

# LFPS REST endpoints (swagger: https://lfps.usgs.gov/docs/api)
SUBMIT_URL = "https://lfps.usgs.gov/api/job/submit"          # POST
STATUS_URL = "https://lfps.usgs.gov/api/job/status"          # GET ?JobId=<id>
CANCEL_URL = "https://lfps.usgs.gov/api/job/cancel"          # GET ?JobId=<id>
UPLOAD_URL = "https://lfps.usgs.gov/api/upload/shapefile"    # POST
HEALTH_URL = "https://lfps.usgs.gov/api/healthCheck"         # GET
PRODUCTS_URL = "https://lfps.usgs.gov/products"              # client-rendered catalog

# Static base terrain products: exempt from --version (LANDFIRE releases
# terrain as a base product and updates fuels/canopy annually).
_TERRAIN = ["LF2020_Elev", "LF2020_SlpD", "LF2020_Asp"]

# LFPS job-terminal / in-progress statuses.
_DONE = {"Succeeded", "Failed", "Canceled"}
_IN_PROGRESS = {"Pending", "Submitted", "Waiting", "Executing"}

# AOI-coverage sanity: LFPS serves CONUS/AK/HI (no insular areas).
_LON_RANGE = (-188.0, -66.0)
_LAT_RANGE = (18.0, 72.0)

_DEFAULT_OUT = "FireBehaviorModels/SampleData/landfire/landscape"


def short_name(code):
    """FARSITE-style short band name from an LFPS layer code.

    Strips the `LFyyyy_` prefix and lowercases: `LF2020_Elev` -> `elev`,
    `LF{yr}_FBFM40` -> `fbfm40`, `LF{yr}_CC` -> `cc`. Mirrors the
    BlueMountain sample's `BandName` convention.
    """
    code = code.strip()
    short = re.sub(r"^LF\d{4}_", "", code).lower()
    return _SHORT_NAMES.get(short, short)


# Explicit short-name overrides (generic strip-lowercase is right for most).
_SHORT_NAMES = {"slpd": "slp_d"}


def default_layers(version, fuel_model):
    """Resolve the default 8-band stack for a release `version` + `fuel_model`.

    Terrain (bands 1-3) is the static LF2020_* base; fuels/canopy (bands 4-8)
    use the annually-updated LF{version}_*. `fuel_model` selects the single
    fuel layer that occupies band 4 (index 3): FARSITE reads EXACTLY ONE fuel
    band positionally, so requesting both would break the band-4 contract.
    """
    fuel_upper = fuel_model.upper()
    if fuel_upper not in ("FBFM40", "FBFM13"):
        die(f"--fuel-model must be fbfm40 or fbfm13, got {fuel_model!r}")
    return [
        *_TERRAIN,
        f"LF{version}_{fuel_upper}",
        f"LF{version}_CC",
        f"LF{version}_CH",
        f"LF{version}_CBH",
        f"LF{version}_CBD",
    ]


def validate_bbox(s):
    """Parse/validate a `W S E N` decimal-degree bbox; return [w, s, e, n]."""
    try:
        w, s_, e, n = (float(x) for x in s.split())
    except ValueError:
        die(f"--bbox must be four numbers 'W S E N' (decimal degrees), got {s!r}")
    if not (_LON_RANGE[0] <= w <= _LON_RANGE[1] and _LON_RANGE[0] <= e <= _LON_RANGE[1]):
        die(f"bbox longitude must be in {_LON_RANGE} (LFPS covers CONUS/AK/HI), got W={w} E={e}")
    if not (_LAT_RANGE[0] <= s_ <= _LAT_RANGE[1] and _LAT_RANGE[0] <= n <= _LAT_RANGE[1]):
        die(f"bbox latitude must be in {_LAT_RANGE} (LFPS covers CONUS/AK/HI), got S={s_} N={n}")
    if not w < e:
        die(f"bbox west {w} must be < east {e}")
    if not s_ < n:
        die(f"bbox south {s_} must be < north {n}")
    return [w, s_, e, n]


def validate_mapzone(s):
    """Validate a LANDFIRE map-zone number; return the canonical string."""
    try:
        z = int(s)
    except (TypeError, ValueError):
        die(f"--mapzone must be an integer map-zone number, got {s!r}")
    valid = [*range(1, 11), *range(12, 81), 98, 99]
    if z not in valid:
        die(f"--mapzone {z} is not a valid LANDFIRE map zone (valid: 1-10, 12-80, 98-99)")
    return str(z)


def build_parser():
    p = argparse.ArgumentParser(
        prog="ingest_landscape.py",
        description="Ingest a LANDFIRE (LFPS) fire-area landscape as a "
                    "FARSITE/WindNinja-ready multi-band GeoTIFF.",
        epilog="AU: exactly one of --bbox/--mapzone required; --email is the "
               "LFPS-required requester email (not an auth token).",
    )
    p.add_argument("--bbox", default=None, metavar="W S E N",
                   help="WGS84 bbox 'W S E N' in decimal degrees; mutually "
                        "exclusive with --mapzone. Prefix-friendlier than "
                        "--mapzone for a single fire (small, fast LFPS job).")
    p.add_argument("--mapzone", default=None,
                   help="LANDFIRE map-zone number (valid 1-10, 12-80, 98-99); "
                        "returns the FULL zone extent. Mutually exclusive with --bbox.")
    p.add_argument("--email", required=True,
                   help="LFPS-required requester email (open API - not an auth token)")
    p.add_argument("--version", default="2024",
                   help="LANDFIRE release year for the annually-updated product "
                        "layers (e.g. 2023, 2024, 2025); default 2024")
    p.add_argument("--fuel-model", default="fbfm40", type=str.lower,
                   choices=["fbfm40", "fbfm13"],
                   help="band-4 fuel classification: fbfm40 (40 Scott & Burgan, "
                        "default) or fbfm13 (13 Anderson); case-insensitive")
    p.add_argument("--layers", default=None,
                   help="semicolon-delimited LFPS layer codes, USED VERBATIM in "
                        "exact band order. LFPS emits output bands in this order "
                        "and FARSITE/WindNinja read bands positionally (band 1 = "
                        "elevation, band 4 = fuel model), so ORDER is the contract "
                        "- the list is never sorted/deduped. Omit for the "
                        "version-derived default 8-band stack.")
    p.add_argument("--output-projection", default="5070",
                   help="EPSG WKID; default 5070 (NAD83/Conus Albers). MUST be "
                        "forced - without it LFPS emits a per-AOI Albers that "
                        "changes every run.")
    p.add_argument("--resolution", type=int, default=30,
                   help="output resolution in m; 30 = native LANDFIRE (omit "
                        "Resample_Resolution). LFPS only coarsens, so <30 "
                        "(finer than native) is rejected and >9999 is invalid.")
    p.add_argument("--out", default=_DEFAULT_OUT,
                   help="output base path (no extension); written as <out>.tif; "
                        "parent dirs created")
    p.add_argument("--dem-out", default=None,
                   help="optional path for a single-band elevation GeoTIFF "
                        "(band-1 extraction) for WindNinja/elevation use")
    p.add_argument("--max-wait", type=int, default=900,
                   help="max seconds to poll job status before erroring (default 900)")
    p.add_argument("--poll-interval", type=int, default=5,
                   help="seconds between status polls (default 5)")
    p.add_argument("--keep-zip", action="store_true",
                   help="keep the raw downloaded <JobId>.zip bundle after "
                        "extraction (provenance/archival only); default deletes it")
    return p


# ---------------------------------------------------------------------------
# LFPS job lifecycle
# ---------------------------------------------------------------------------
def _request(method, url, retries=3, **kw):
    """`method(url, timeout=30, **kw)` with retry on 5xx/connection errors.

    Backoff 1 s then 2 s. 4xx responses are returned untouched (caller
    decides how to surface them, e.g. `Invalid products: <codes>`).
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = method(url, timeout=30, **kw)
            if r.status_code >= 500:
                raise ConnectionError(f"HTTP {r.status_code}")
            return r
        except (requests.RequestException, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(attempt)  # 1 s, 2 s
    raise RuntimeError(f"request to {url} failed after {retries} attempts: {last}")


def _api_message(obj):
    """Extract the human-readable message from an LFPS error (Response or dict)."""
    if not isinstance(obj, dict) and hasattr(obj, "json"):
        try:
            obj = obj.json()
        except Exception:
            text = getattr(obj, "text", None)
            if text:
                return str(text).strip()
            return str(obj)
    if isinstance(obj, dict):
        for key in ("messages", "message", "Message", "error", "Error"):
            if obj.get(key):
                return str(obj[key])
    return str(obj)


def submit_job(layer_list, aoi, email, projection, resolution):
    """POST a job request; return the `jobId`. URL/keys are the verified v2 API."""
    body = {
        "Layer_List": layer_list,        # semicolon-joined, verbatim band order
        "Area_of_Interest": aoi,         # "W S E N" or a map-zone number
        "Email": email,
        "Output_Projection": projection,  # EPSG WKID string; MUST force 5070
    }
    if resolution > 30:
        body["Resample_Resolution"] = str(resolution)  # LFPS emits it for >30 coarsening
    r = _request(requests.post, SUBMIT_URL, json=body)
    if r.status_code >= 400:
        die(f"{_api_message(r)} - see {PRODUCTS_URL} to pick a valid --version/--layers")
    try:
        data = r.json()
    except ValueError as e:
        die(f"submit returned non-JSON: {e}")
    job = data.get("jobId")
    if not job:
        die(f"submit response missing jobId: {data!r}")
    return job


def poll_job(job_id, max_wait, poll_interval):
    """Poll status until terminal or `max_wait` elapsed; return the JSON on success."""
    start = time.time()
    while True:
        r = _request(requests.get, f"{STATUS_URL}?JobId={job_id}")
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "Succeeded":
            if not _output_url(data):
                die(f"job {job_id} Succeeded but response lacks outputFile: {data!r}")
            return data
        if status in {"Failed", "Canceled"}:
            die(f"job {job_id} {status}: {_api_message(data)}")
        if time.time() - start >= max_wait:
            die(f"job {job_id} still {status!r} after {max_wait}s (--max-wait); "
                f"latest messages: {_api_message(data)}")
        time.sleep(poll_interval)


def _output_url(data):
    """`outputFile` may be a bare URL or the LFPS object with a `File` key."""
    out = data.get("outputFile")
    if isinstance(out, dict):
        return out.get("File")
    return out


def download(output_url, out_dir, job_id, keep_zip):
    """Stream-download `output_url` zip, extract, return (raster, extract_dir).

    Extraction goes into a dedicated temp subdir so the "exactly one raster"
    contract never collides with pre-existing `.tif` files in `out_dir`. The
    bundle raster carries its own GUID basename (not the `jobId`), so it is
    located by globbing a single `*.tif`, not by name.
    """
    zip_path = out_dir / f"{job_id}.zip"
    r = _request(requests.get, output_url, stream=True)
    if r.status_code == 404:
        r.close()
        die("output URL returned 404: the LFPS bundle likely expired "
            "(products are retained ~6 h after completion); re-submit the job")
    r.raise_for_status()
    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            if chunk:
                f.write(chunk)
    r.close()

    import zipfile
    extract_dir = out_dir / f".{job_id}.extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
    except zipfile.BadZipFile as e:
        die(f"downloaded file is not a valid zip: {e}")

    tifs = sorted(extract_dir.glob("*.tif"))
    if len(tifs) != 1:
        die(f"bundle contains {len(tifs)} tif files; expected exactly one (contract)")
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return tifs[0], extract_dir


def finalize(out_path, layer_list, projection_wkid, resolution, dem_out):
    """Validate the output GeoTIFF, set band descriptions, extract `dem_out`."""
    names = [short_name(code) for code in layer_list]
    with rasterio.open(out_path) as src:
        if src.count != len(layer_list):
            die(f"band count {src.count} != requested layers {len(layer_list)} "
                f"({layer_list})")
        rx, ry = src.res
        if abs(rx - resolution) > 0.01 or abs(ry - resolution) > 0.01:
            die(f"resolution ({rx:.3f}, {ry:.3f}) != requested {resolution}")
        epsg = src.crs.to_epsg()
        if epsg != int(projection_wkid):
            die(f"output CRS EPSG:{epsg} != requested EPSG:{projection_wkid}")
        dt = np.dtype(src.dtypes[0])
        if not np.issubdtype(dt, np.integer):
            die(f"output dtype {src.dtypes[0]} is not integer (expected "
                f"int8/int16/uint16)")
        if dem_out:
            _write_dem(src, dem_out)
    with rasterio.open(out_path, "r+") as dst:
        for i, nm in enumerate(names, 1):
            dst.set_band_description(i, nm)
    return names


def _write_dem(src, dem_out_path):
    """Extract band 1 as a single-band int16 GeoTIFF for WindNinja."""
    profile = src.profile.copy()
    profile.update(count=1, dtype="int16", driver="GTiff")
    with rasterio.open(dem_out_path, "w", **profile) as dst:
        dem = src.read(1).astype("int16")
        dst.write(dem, 1)
        dst.set_band_description(1, "elev")


def main(argv=None):
    args = build_parser().parse_args(argv)

    # ---- AOI: exactly one of --bbox/--mapzone -----------------------------
    if (args.bbox is None) == (args.mapzone is None):
        die("provide exactly one of --bbox or --mapzone")
    if args.bbox is not None:
        wsen = validate_bbox(args.bbox)
        aoi = " ".join(f"{x:.10f}" for x in wsen)
    else:
        aoi = validate_mapzone(args.mapzone)

    # ---- layer list --------------------------------------------------------
    if args.layers:
        layer_list = [tok.strip() for tok in args.layers.split(";") if tok.strip()]
        if not layer_list:
            die("--layers produced no layer codes")
    else:
        layer_list = default_layers(args.version, args.fuel_model)

    # ---- resolution --------------------------------------------------------
    # 30 is the native LANDFIRE resolution (LFPS receives no Resample_Resolution
    # and emits 30 m). LFPS only coarsens, so <30 (finer-than-native) is invalid
    # and >9999 exceeds the API limit.
    if args.resolution < 30:
        die(f"--resolution must be >=30 (LFPS only coarsens; native is 30), got {args.resolution}")
    if args.resolution > 9999:
        die("--resolution must be <=9999")

    layer_str = ";".join(layer_list)

    # ---- submit ------------------------------------------------------------
    job_id = submit_job(layer_str, aoi, args.email,
                        args.output_projection, args.resolution)
    print(f"job: {job_id}")
    print(f"submit: Layer_List={layer_str} AOI={aoi}")
    print(f"  Output_Projection=EPSG:{args.output_projection} Resample_Resolution={args.resolution}")

    # ---- poll --------------------------------------------------------------
    status = poll_job(job_id, args.max_wait, args.poll_interval)
    output_url = _output_url(status)

    # ---- download + extract + rename ---------------------------------------
    out = Path(args.out)
    out_dir = out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    final_tif = out.with_suffix(".tif")
    extracted, extract_dir = download(output_url, out_dir, job_id, args.keep_zip)
    # The bundle raster carries a GUID basename; rename to the stable <out>.tif
    # and drop the temp extract dir, leaving the single <out>.tif as the
    # deliverable (the GUID .tfw/.aux.xml sidecars are removed with it).
    try:
        extracted.replace(final_tif)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    # ---- validate + finalize ----------------------------------------------
    names = finalize(final_tif, layer_list, args.output_projection,
                     args.resolution, args.dem_out)

    # ---- summary -----------------------------------------------------------
    print(f"status: Succeeded")
    print(f"output: {final_tif}")
    print(f"bands: {len(names)} -> {', '.join(names)}")
    print(f"crs: EPSG:{args.output_projection}")
    print(f"resolution: {args.resolution} m")
    if args.dem_out:
        print(f"dem_out: {args.dem_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
