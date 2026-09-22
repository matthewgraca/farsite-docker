#!/usr/bin/env python3
"""calfire_ignition - CAL FIRE perimeter + IRWIN ignition -> FARSITE ignition
and reference-perimeter shapefiles.

Pipeline head for FARSITE/FlamMap runs. Given a historical California fire name
(+year, optionally --index), this tool:

  1. harvests the real final fire-perimeter polygon + incident metadata from the
     CAL FIRE FRAP "California Fire Perimeters (all)" ArcGIS feature service;
  2. resolves the ignition point via the fire's IRWINID against NIFC's public
     WFIGS "Ignitions - Wildland Fire Incident Locations" mirror of IRWIN
     (the record's Point geometry is the point of origin);
  3. writes two FARSITE/FlamMap-accepted shapefiles - an ignition point seed
     (`ignition.*`, the FARSITE_IGNITION_FILE source) and the reference
     perimeter polygon (`reference_perimeter.*`, the real footprint to compare
     against the simulated perimeter) - plus `fire.json` metadata;
  4. prints a ready-to-run `hrrr_to_wxs.py` command with --lat/--lon pinned to
     the ignition point and a whole-hour-UTC --start/--end taken from the FRAP
     alarm/containment dates.

It does NOT run hrrr_to_wxs.py or FlamMap. Pre-IRWIN-era fires whose FRAP record
has no IRWINID (or that are absent from the public WFIGS mirror) take a manual
--lat/--lon override instead of a fabricated point.

Dependencies (no new installs; all present in the hrrr-wxs env - see the
hrrr_to_wxs.py docstring for the recipe): requests, pyshp (import shapefile),
pyproj - plus the stdlib. `die` is reused from hrrr_to_wxs.py; the two files
must stay in the same directory.

Field casing note: FRAP properties are UPPERCASE (EARTH OBSERVER legacy); WFIGS
properties are camelCase (e.g. IrwinID, IncidentName). Never assume case
equality between the two services.

Output geometry is written in the --crs (default EPSG:4326); the ignition
lat/lon and the polygon rings are reprojected from WGS84 when a different CRS
is requested. The .prj companion holds the CRS as WKT, which is how
GDAL/FlamMap/FARSITE identify the shapefile CRS.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# startup guard: the env recipe lives in tools/hrrr_to_wxs.py, not an env file
# ---------------------------------------------------------------------------
_ENV_RECIPE = (
    "conda create -n hrrr-wxs -c conda-forge python=3.12 numpy xarray pandas "
    "rasterio pyproj eccodes && python -m pip install herbie cfgrib "
    "timezonefinder tqdm requests pyshp"
)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _guard_env():
    missing = []
    for name in ("requests", "shapefile", "pyproj"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        die(f"missing {', '.join(missing)}; create env per tools/hrrr_to_wxs.py "
            f"header: {_ENV_RECIPE}")


_guard_env()

import requests  # noqa: E402  (bound after the presence guard; never reached on failure)
import shapefile  # noqa: E402
import pyproj  # noqa: E402

try:
    from hrrr_to_wxs import die  # noqa: F811  (same semantics; shared convention)
except ImportError:  # hrrr_to_wxs itself unresolvable (numpy/tqdm absent) - keep local
    pass

# ---------------------------------------------------------------------------
# services (public ArcGIS REST, both verified queries-accessible over plain HTTP)
# ---------------------------------------------------------------------------
FRAP_SERVICE = ("https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/"
                "California_Historic_Fire_Perimeters/FeatureServer/0/query")
WFIGS_SERVICE = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
                 "WFIGS_Incident_Locations/FeatureServer/0/query")
HEADERS = {"User-Agent": "calfire-ignition/0.1 (+FARSITE data pipeline)"}

FRAP_FIELDS = ("FIRE_NAME,YEAR_,ALARM_DATE,CONT_DATE,GIS_ACRES,CAUSE,AGENCY,"
               "UNIT_ID,INC_NUM,IRWINID")
# WFIGS geometry is authoritative; InitialLatitude/InitialLongitude are a
# fallback and are frequently null.
WFIGS_FIELDS = "IncidentName,FireDiscoveryDateTime,InitialLatitude,InitialLongitude"

# pyshp dBASE field spec - names all <= 10 bytes (pyshp's truncation limit):
# FireName C40, Year N8, Cause C40, Acres N12(1), Start C24, Contain C24,
# Lat N12(6), Lon N12(6)
SHP_FIELDS = [
    ("FireName", "C", 40, 0),
    ("Year", "N", 8, 0),
    ("Cause", "C", 40, 0),
    ("Acres", "N", 12, 1),
    ("Start", "C", 24, 0),
    ("Contain", "C", 24, 0),
    ("Lat", "N", 12, 6),
    ("Lon", "N", 12, 6),
]
_PAGE = 1000


def escape(s):
    """Single-quote SQL escaping for ArcGIS where clauses."""
    return str(s).replace("'", "''")


def _epoch_ms_to_utc(ms):
    if ms is None or ms == "":
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _strip_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------------------
# Step 1 - ArcGIS query helper
# ---------------------------------------------------------------------------
def query_arcgis(service_url, where, out_fields, *, geometry=True, out_sr=4326):
    """GET all matching features, paginated by resultOffset when the service
    sets exceededTransferLimit. Each feature: {geometry (GeoJSON), properties}.
    Returns [] on no features."""
    features = []
    offset = 0
    while True:
        params = {
            "f": "geojson",
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true" if geometry else "false",
            "outSR": out_sr,
            "resultRecordCount": _PAGE,
            "resultOffset": offset,
        }
        resp = requests.get(service_url, params=params, headers=HEADERS, timeout=60)
        if resp.status_code != 200:
            die(f"ArcGIS query failed ({resp.status_code}) for {service_url}; where={where}")
        try:
            data = resp.json()
        except ValueError:
            die(f"ArcGIS query returned non-JSON for {service_url}; where={where}")
        page = data.get("features") or []
        features.extend(page)
        if not data.get("exceededTransferLimit"):
            break
        offset += _PAGE
    return features


# ---------------------------------------------------------------------------
# Step 2 - resolve the CAL FIRE fire (polygon + metadata + IRWINID)
# ---------------------------------------------------------------------------
def list_fires(fire_name, year=None):
    where = f"UPPER(FIRE_NAME)='{escape(fire_name.upper())}'"
    if year is not None:
        where += f" AND YEAR_={int(year)}"
    fires = []
    for f in query_arcgis(FRAP_SERVICE, where, FRAP_FIELDS, geometry=True):
        props = f.get("properties") or {}
        name = _strip_none(props.get("FIRE_NAME"))
        if not name or name.upper() != fire_name.upper():
            continue  # belt-and-braces on the query's own UPPER() match
        fires.append({
            "geometry": f.get("geometry"),
            "fire_name": name,
            "year": props.get("YEAR_"),
            "alarm_date": _epoch_ms_to_utc(props.get("ALARM_DATE")),
            "cont_date": _epoch_ms_to_utc(props.get("CONT_DATE")),
            "gis_acres": props.get("GIS_ACRES"),
            "cause": _strip_none(props.get("CAUSE")),
            "agency": _strip_none(props.get("AGENCY")),
            "unit_id": _strip_none(props.get("UNIT_ID")),
            "inc_num": _strip_none(props.get("INC_NUM")),
            "irwin_id": _strip_none(props.get("IRWINID")),
        })
    return fires


def select_fire(cands, fire_name, year, index):
    """Index/ambiguity resolution: exactly one candidate wins; otherwise
    --index (0-based) is required and bounds-checked."""
    if not cands:
        where = f"'{fire_name}'" + (f" in {year}" if year is not None else "")
        die(f"no CAL FIRE fire named {where} in FRAP")
    n = len(cands)
    if index is not None:
        if not 0 <= index < n:
            die(f"--index {index} out of range (have {n} candidates)")
        return cands[index]
    if n == 1:
        return cands[0]

    def _dt(v):
        return v.strftime("%Y-%m-%dT%H:%MZ") if v else "-"

    print(f"'{fire_name}' matches {n} CAL FIRE records; pass --index to choose:")
    for i, c in enumerate(cands):
        print(f"  [{i}] {c['fire_name']} {c['year']}  alarm={_dt(c['alarm_date'])}  "
              f"cont={_dt(c['cont_date'])}  acres={c['gis_acres']}  "
              f"cause={c['cause']}  agency={c['agency']}  "
              f"unit={c['unit_id']}  inc={c['inc_num']}")
    die(f"--index required: '{fire_name}' is ambiguous (pass --index 0..{n - 1})")


# ---------------------------------------------------------------------------
# Step 3 - IRWIN ignition point by IRWINID
# ---------------------------------------------------------------------------
def fetch_ignition(irwin_id):
    """(lat, lon) of the incident point of origin, or None if unresolvable.
    The WFIGS Point geometry is authoritative; InitialLatitude/InitialLongitude
    are a fallback and are frequently null."""
    if not irwin_id:
        return None
    feats = query_arcgis(WFIGS_SERVICE, f"IrwinID='{escape(irwin_id)}'",
                         WFIGS_FIELDS, geometry=True)
    if not feats:
        return None
    f = feats[0]
    geom = f.get("geometry") or {}
    if geom.get("type") == "Point":
        coords = geom.get("coordinates") or []
        if len(coords) == 2:
            try:
                return float(coords[1]), float(coords[0])  # (lat, lon)
            except (TypeError, ValueError):
                pass
    props = f.get("properties") or {}
    lat, lon = props.get("InitialLatitude"), props.get("InitialLongitude")
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Step 4 - shapefiles, fire.json, suggested hrrr command
# ---------------------------------------------------------------------------
def _attrs_for(rec, lat, lon):
    """dBASE record values shared by both shapefiles (Lat/Lon = the ignition
    point, so ignition.* and reference_perimeter.* cross-link)."""
    return {
        "FireName": (rec["fire_name"] or "").upper(),
        "Year": int(rec["year"]) if rec["year"] is not None else 0,
        "Cause": rec["cause"] or "",
        "Acres": float(rec["gis_acres"]) if rec["gis_acres"] is not None else 0.0,
        "Start": rec["alarm_date"].strftime("%Y-%m-%d") if rec["alarm_date"] else "",
        "Contain": rec["cont_date"].strftime("%Y-%m-%d") if rec["cont_date"] else "",
        "Lat": float(lat),
        "Lon": float(lon),
    }


def _reproject(crs):
    """Transformer WGS84 -> crs, or None when crs IS WGS84 (no-op path)."""
    if crs.to_epsg() == 4326:
        return None
    return pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def _write_shapefile(out_dir, base, shape_type, rings_or_point, attrs, crs):
    w = shapefile.Writer(str(out_dir / base), shapeType=shape_type)
    for name, typ, size, dec in SHP_FIELDS:
        w.field(name, typ, size, dec)
    if shape_type == shapefile.POINT:
        x, y = rings_or_point
        w.point(x, y)
    else:
        w.poly(rings_or_point)
    w.record(*[attrs[name] for name, _, _, _ in SHP_FIELDS])
    w.close()
    (out_dir / f"{base}.prj").write_text(crs.to_wkt())


def write_point_shp(out_dir, lon, lat, attrs, crs):
    """ignition.* - the FARSITE_IGNITION_FILE seed. shp x/lon, y/lat."""
    t = _reproject(crs)
    xy = (lon, lat) if t is None else t.transform(lon, lat)
    _write_shapefile(out_dir, "ignition", shapefile.POINT, xy, attrs, crs)


def _rings_from_geojson(geom):
    """[(x, y), ...] per ring; outer ring first per polygon part. GeoJSON ring
    order (outer then holes) is preserved so pyshp can classify holes."""
    if not geom:
        return []
    gtype, coords = geom.get("type"), geom.get("coordinates")
    rings = []
    if gtype == "Polygon":
        parts = [coords]
    elif gtype == "MultiPolygon":
        parts = coords
    else:
        return []
    for part in parts:
        for ring in part:
            rings.append([(pt[0], pt[1]) for pt in ring])
    return rings


def write_polygon_shp(out_dir, geom, attrs, crs):
    """reference_perimeter.* - the real final fire footprint (single multipart
    polygon record). Vertices are (lon, lat) in 4326, reprojected otherwise."""
    rings = _rings_from_geojson(geom)
    if not rings or any(len(r) < 3 for r in rings):
        die("CAL FIRE returned a degenerate polygon")
    t = _reproject(crs)
    if t is not None:
        rings = [[t.transform(x, y) for x, y in ring] for ring in rings]
    _write_shapefile(out_dir, "reference_perimeter", shapefile.POLYGON,
                     rings, attrs, crs)


def write_fire_json(out_dir, rec, ignition, crs):
    def _iso(v):
        return v.isoformat() if v else None

    payload = {
        "name": rec["fire_name"],
        "year": rec["year"],
        "url": FRAP_SERVICE,
        "alarm_date": _iso(rec["alarm_date"]),
        "cont_date": _iso(rec["cont_date"]),
        "gis_acres": rec["gis_acres"],
        "cause": rec["cause"],
        "agency": rec["agency"],
        "unit_id": rec["unit_id"],
        "inc_num": rec["inc_num"],
        "irwin_id": rec["irwin_id"],
        "lat": float(ignition[0]),
        "lon": float(ignition[1]),
        "crs": crs.to_epsg() if crs.to_epsg() is not None else crs.to_wkt(),
        "source": "CAL FIRE FRAP + NIFC WFIGS (IRWIN)",
    }
    with open(out_dir / "fire.json", "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def wxs_command(rec, lat, lon, slug):
    """Suggested hrrr_to_wxs.py invocation: ignition anchor + whole-hour UTC
    window from the FRAP alarm/containment dates. --dem is a user placeholder
    (hrrr_to_wxs requires it); --end is omitted when containment is unknown."""
    head = f"python tools/hrrr_to_wxs.py --lat {lat:.6f} --lon {lon:.6f}"
    if rec["alarm_date"]:
        head += f" --start {rec['alarm_date']:%Y-%m-%dT%H:%M}Z"
    if rec["cont_date"]:
        head += f" --end {rec['cont_date']:%Y-%m-%dT%H:%M}Z"
    tail = (f"--dem <fire-area LCP raster> --out {slug}-hrrr.wxs")
    return f"{head} \\\n  {tail}"


def _slug(name):
    return name.lower().replace(" ", "-")


def build_parser():
    p = argparse.ArgumentParser(
        prog="calfire_ignition.py",
        description="CAL FIRE perimeter + IRWIN ignition point -> FARSITE "
                    "ignition & reference-perimeter shapefiles.",
        epilog="Outputs land in --out-dir (default FireBehaviorModels/SampleData/"
               "<year>_<fire-slug>); run from the repo root.",
    )
    p.add_argument("--fire-name", required=True,
                   help="CAL FIRE fire name (matched case-insensitively)")
    p.add_argument("--year", type=int, default=None,
                   help="fire YEAR_ filter; omit to search the whole FRAP history")
    p.add_argument("--index", type=int, default=None,
                   help="0-based pick when the name matches multiple CAL FIRE records")
    p.add_argument("--lat", type=float, default=None,
                   help="manual ignition latitude (WGS84, -90..90); give both "
                        "--lat and --lon to skip the IRWIN lookup")
    p.add_argument("--lon", type=float, default=None,
                   help="manual ignition longitude (WGS84, -180..180); see --lat")
    p.add_argument("--crs", default="EPSG:4326",
                   help="output CRS for both shapefiles (default EPSG:4326)")
    p.add_argument("--out-dir", default=None,
                   help="output directory (default FireBehaviorModels/SampleData/"
                        "<year>_<fire-slug>)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if (args.lat is None) != (args.lon is None):
        die("give both --lat and --lon, or neither")
    if args.lat is not None and not (-90.0 <= args.lat <= 90.0):
        die(f"--lat {args.lat} out of range [-90, 90]")
    if args.lon is not None and not (-180.0 <= args.lon <= 180.0):
        die(f"--lon {args.lon} out of range [-180, 180]")

    try:
        crs = pyproj.CRS.from_user_input(args.crs)
    except pyproj.exceptions.CRSError:
        die(f"cannot parse --crs {args.crs}")

    rec = select_fire(list_fires(args.fire_name, args.year),
                      args.fire_name, args.year, args.index)

    def _dt(v):
        return v.strftime("%Y-%m-%dT%H:%MZ") if v else "-"

    print(f"resolved: {rec['fire_name']} {rec['year']}")
    print(f"alarm: {_dt(rec['alarm_date'])}   cont: {_dt(rec['cont_date'])}")
    print(f"acres: {rec['gis_acres']}   cause: {rec['cause']}   "
          f"agency: {rec['agency']}")
    print(f"unit: {rec['unit_id']}   inc_num: {rec['inc_num']}")
    print(f"irwin_id: {rec['irwin_id']}")

    if args.lat is not None:
        lat, lon = args.lat, args.lon
        src = "manual --lat/--lon"
    else:
        ign = fetch_ignition(rec["irwin_id"])
        if ign is None:
            die("no ignition point for this fire (FRAP IRWINID absent or not in "
                "the public NIFC/IRWIN mirror); pass --lat/--lon to set it manually")
        lat, lon = ign
        src = "WFIGS (IRWIN) point of origin"

    slug = _slug(rec["fire_name"])
    out_dir = Path(args.out_dir) if args.out_dir else \
        Path(f"FireBehaviorModels/SampleData/{rec['year']}_{slug}")
    out_dir.mkdir(parents=True, exist_ok=True)

    attrs = _attrs_for(rec, lat, lon)
    write_point_shp(out_dir, lon, lat, attrs, crs)
    write_polygon_shp(out_dir, rec["geometry"], attrs, crs)
    write_fire_json(out_dir, rec, (lat, lon), crs)

    print(f"ignition: ({lat:.6f}, {lon:.6f})  [{src}]")
    print(f"crs: {crs.to_epsg() if crs.to_epsg() is not None else crs.to_wkt()}")
    print(f"output: {out_dir}")
    print(f"  {out_dir / 'ignition'}.shp/.shx/.dbf/.prj   (FARSITE_IGNITION_FILE seed)")
    print(f"  {out_dir / 'reference_perimeter'}.shp/.shx/.dbf/.prj   (reference footprint)")
    print(f"  {out_dir / 'fire.json'}")
    print()
    print("sample HRRR weather at the ignition point (use --dem on the fire area):")
    print(wxs_command(rec, lat, lon, slug))
    return 0


if __name__ == "__main__":
    sys.exit(main())
