#!/usr/bin/env python3
"""compare_perimeter - overlap the CalFire reference perimeter against the
FARSITE-simulated perimeter: IoU (intersection-over-union) metric plus an
overlay PNG on an XYZ basemap.

Data contract (outputs derive from the command-file base name, per tools/
README): <run>/reference_perimeter.shp is the real final footprint written by
calfire_ignition.py; <run>/farsite-out/<slug>_Perimeters.shp is FARSITE's
output, one record per growth timestep. The FINAL perimeter is the record
with the max elapsed field (Elapsed_Mi / Elapsed_Minutes / *elapsed*); with no
such field every record is kept (fire growth is monotonic, so union-all == the
final footprint).

Areas/metrics are computed in the simulated shapefile's own projected CRS -
the reference is reprojected into it - because local projected CRS give
accurate planar areas (a mostly-geographic sim CRS falls back to a UTM zone
derived from the simulated footprint's centroid). The overlay PNG is plotted
in EPSG:3857 (Web Mercator) because XYZ tiles are natively 3857; basemap
providers are tried in order (imagery -> osm) with tile failures falling
through to a plain projected frame. FARSITE perimeters arrive as closed-ring
POLYLINE (SHPT 3/13) or POLYGON (5/15) records; both are accepted.

New metrics slot into METRICS and new visualizations into plot_overlay();
this change ships IoU + the overlay only.

Usage:
    python tools/compare_perimeter.py --run-dir <run>
    python tools/compare_perimeter.py --reference <ref.shp> --simulated <sim.shp>
                                      [--metric iou] [--plot overlay.png]
                                      [--json result.json] [--basemap auto]
"""

import argparse
import json
import math
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import pyproj
import requests
import shapefile
from PIL import Image
from shapely.geometry import Point, Polygon
from shapely.ops import transform, unary_union

from hrrr_to_wxs import die

# XYZ slippy-map tile endpoints. imagery uses an {z}/{y}/{x} path (ArcGIS),
# osm an {z}/{x}/{y} path; both are filled from the same named kwargs below.
TILE_URLS = {
    "imagery": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
HEADERS = {"User-Agent": "compare-perimeter/0.1 (+FARSITE data pipeline)"}
TILE_SIZE = 256
MERCATOR_EXTENT = 40075016.685578488   # EPSG:3857 world width, m; = 2*pi*6378137

_ELAPSED_NAMES = ("elapsed_mi", "elapsed_minutes", "elapsed",
                  "time", "simtime", "sim_time")
_RING_SHPTYPES = (3, 5, 13, 15)   # POLYLINE/POLYGON (+ Z variants), closed rings


# ---------------------------------------------------------------------------
# shapefile loading
# ---------------------------------------------------------------------------
def read_shapefile_crs(shp_path):
    """The sibling .prj WKT as a pyproj.CRS (die on absence/garbage)."""
    prj = Path(shp_path).with_suffix(".prj")
    if not prj.is_file():
        die(f"no .prj companion for {shp_path} (perimeter CRS required)")
    try:
        return pyproj.CRS.from_wkt(prj.read_text())
    except pyproj.exceptions.CRSError:
        die(f"cannot parse .prj CRS for {shp_path}")


def _valid(g):
    """Fix self-intersections with the standard buffer(0) repair."""
    return g if g.is_valid else g.buffer(0)


def _rings_to_polygons(shp, shp_type):
    """One shape's flat points+parts into Polygon(s), holes self-classified.

    Shapefiles store every ring of a record as byte-part offsets over one flat
    point list with no explicit outer/hole markers, so rings are classified by
    containment: ring 0 is the first shell; each later ring whose first vertex
    falls inside an existing shell is that shell's hole, otherwise it starts a
    new (multi-part) polygon. FARSITE closed-ring polylines and calfire
    reference polygons both round-trip through this.
    """
    if shp_type not in _RING_SHPTYPES:
        die(f"expected polygon/polyline-ring shapefile, got SHPT {shp_type}")
    offsets = list(shp.parts) + [len(shp.points)]
    rings = [shp.points[a:b] for a, b in zip(offsets[:-1], offsets[1:])]
    bodies = []   # [{"ring": [...], "holes": [[...], ...], "poly": Polygon}]
    for ring in rings:
        if len(ring) < 4:              # fewer points than a triangle: junk ring
            continue
        poly = _valid(Polygon(ring))
        first = Point(ring[0])
        owner = next((b for b in bodies if b["poly"].contains(first)), None)
        if owner is None:
            bodies.append({"ring": list(ring), "holes": [], "poly": poly})
        else:
            owner["holes"].append(list(ring))
            owner["poly"] = _valid(Polygon(owner["ring"], owner["holes"]))
    return [b["poly"] for b in bodies]


def load_polygons(shp_path, record_indices=None):
    """Read one or more records (default: all) as valid shapely Polygons.

    Returns (list[Polygon], pyproj.CRS). A single record can yield several
    polygons (multi-part / multiple shells); the list is flattened.
    """
    reader = shapefile.Reader(str(shp_path))
    if record_indices is None:
        record_indices = range(reader.numRecords)
    crs = read_shapefile_crs(shp_path)
    polys = []
    for i in record_indices:
        polys.extend(_rings_to_polygons(reader.shape(i), reader.shapeType))
    return polys, crs


def select_final_perimeters(shp_path):
    """(list[Polygon], meta) - the FINAL simulated footprint's polygons.

    FARSITE writes one record per growth timestep; the record(s) carrying the
    max elapsed field ARE the final perimeter. The elapsed field is matched
    case-insensitively against Elapsed_Mi/Elapsed_Minutes/elapsed/time/simtime
    (first exact hit), else any name starting with 'elapsed'. With no usable
    elapsed field every record is kept - growth is monotonic, so union-all ==
    the final state. meta = {"field", "records", "elapsed"} (elapsed null when
    no field or no parseable values).
    """
    reader = shapefile.Reader(str(shp_path))
    names = {f[0].strip().lower() for f in reader.fields
             if f[0] != "DeletionFlag"}
    lowered = [f[0].strip().lower() for f in reader.fields]  # + DeletionFlag
    field_low = next((c for c in _ELAPSED_NAMES if c in names), None)
    if field_low is None:
        field_low = next((n for n in names if n.startswith("elapsed")), None)
    # report the field's original casing (e.g. "Elapsed_Mi", not "elapsed_mi")
    field = next((f[0].strip() for f in reader.fields
                  if f[0] != "DeletionFlag"
                  and f[0].strip().lower() == field_low), field_low)
    recs = reader.records()
    meta = {"field": field if field_low else None, "records": len(recs),
            "elapsed": None}
    keep = None
    if field_low is not None:
        idx = lowered.index(field_low) - 1  # records() skips DeletionFlag
        vals = []
        for r in recs:
            try:
                vals.append(float(r[idx]))
            except (TypeError, ValueError):
                vals.append(math.nan)
        good = [v for v in vals if not math.isnan(v)]
        if good:
            meta["elapsed"] = float(max(good))
            keep = [i for i, v in enumerate(vals) if v == meta["elapsed"]]
    geoms, _ = load_polygons(shp_path, record_indices=keep)
    return geoms, meta


# ---------------------------------------------------------------------------
# CRS handling and metrics
# ---------------------------------------------------------------------------
def to_crs(geoms, crs_from, crs_to):
    """Reproject a flat geometry list (always_xy = lon/lat order throughout)."""
    trans = pyproj.Transformer.from_crs(crs_from, crs_to, always_xy=True)
    return [transform(trans.transform, g) for g in geoms]


def metric_crs_for(sim_crs, sim_geoms):
    """CRS for planar area math: the sim CRS when projected, else a UTM zone
    derived from the simulated footprint's centroid (32611 = UTM 11N etc.)."""
    if sim_crs.is_projected:
        return sim_crs
    if not sim_crs.is_geographic:
        die("cannot derive a metric CRS from the simulated perimeter's CRS")
    lon, lat = unary_union(sim_geoms).centroid.coords[0]
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return pyproj.CRS.from_epsg(epsg)


def footprints(shp_path):
    """{'crs', 'geoms', 'meta'} for one perimeter shapefile."""
    crs = read_shapefile_crs(shp_path)
    geoms, meta = select_final_perimeters(shp_path)
    return {"crs": crs, "geoms": geoms, "meta": meta}


METRICS = {"iou": lambda a, b: a.intersection(b).area / a.union(b).area}


def compute(name, a, b):
    """Apply metric `name` to pre-union polygons a, b in the metric CRS."""
    if name not in METRICS:
        die(f"unknown metric '{name}' (valid: {'/'.join(METRICS)})")
    return METRICS[name](a, b)


def compare(sim_path, ref_path):
    """IoU (plus areas) between the two footprints, in the metric CRS.

    Returns the machine-readable result dict: reference/simulated areas in ha,
    intersection+union in m2, iou, the sim record metadata, and the metric CRS
    as its EPSG int (or WKT when the CRS has no EPSG).
    """
    sim = footprints(sim_path)
    ref = footprints(ref_path)
    metric_crs = metric_crs_for(sim["crs"], sim["geoms"])
    a_geoms = to_crs(sim["geoms"], sim["crs"], metric_crs)
    b_geoms = to_crs(ref["geoms"], ref["crs"], metric_crs)
    A = unary_union(a_geoms)
    B = unary_union(b_geoms)
    if A.area <= 0 or B.area <= 0:
        die(f"degenerate area: simulated {A.area:.1f} m2, "
            f"reference {B.area:.1f} m2")
    inter = A.intersection(B).area
    union = A.union(B).area
    epsg = metric_crs.to_epsg()
    return {
        "reference_ha": B.area / 10000.0,
        "simulated_ha": A.area / 10000.0,
        "area_m2": {"intersection": inter, "union": union},
        "iou": inter / union,
        "sim": {"records": sim["meta"]["records"],
                "elapsed_field": sim["meta"]["field"],
                "elapsed_minutes": sim["meta"]["elapsed"]},
        "metric_crs": epsg if epsg is not None else metric_crs.to_wkt(),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _web_mercator_to_lonlat(x, y):
    """Inverse EPSG:3857 -> (lon, lat) degrees for slippy tile math."""
    lon = x / MERCATOR_EXTENT * 360.0
    lat = math.degrees(2 * math.atan(
        math.exp(y * math.pi / (MERCATOR_EXTENT / 2.0))) - math.pi / 2.0)
    return lon, lat


def _tiles_for(bounds, zoom):
    """(x0, x1, y0, y1) slippy-map tile indices covering a 3857 bbox."""
    n = 1 << zoom
    minx, miny, maxx, maxy = bounds
    xs, ys = [], []
    for x, y in ((minx, maxy), (maxx, maxy), (maxx, miny), (minx, miny)):
        lon, lat = _web_mercator_to_lonlat(x, y)
        tx = int(math.floor((lon + 180.0) / 360.0 * n))
        ty = int(math.floor(
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n))
        xs.append(min(max(tx, 0), n - 1))
        ys.append(min(max(ty, 0), n - 1))
    return min(xs), max(xs), min(ys), max(ys)


def _choose_zoom(w_m, max_zoom):
    """Zoom where the footprint spans ~2 XYZ tiles (clamped to max_zoom)."""
    if w_m <= 0:
        return 1
    zoom = int(math.floor(math.log2(MERCATOR_EXTENT * 2.0 / w_m)))
    zoom = max(0, min(zoom, max_zoom))
    return max(zoom, 1)


def fetch_basemap(providers, bounds3857, zoom, max_tiles):
    """First provider (in order) that fully mosaics, else None.

    Returns (mosaic_rgb, extent3857, provider) where extent is the imshow
    extent (x_min, x_max, y_min, y_max) pinned to exactly the fetched tile
    grid. Any per-tile failure (HTTP, decode, timeout) discards that provider's
    partial mosaic so a broken tile never shows as a grey hole over the fire;
    a provider needing more than max_tiles is skipped outright.
    """
    n = 1 << zoom
    for provider in providers:
        if provider not in TILE_URLS:
            continue
        x0, x1, y0, y1 = _tiles_for(bounds3857, zoom)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > max_tiles:
            continue
        url_t = TILE_URLS[provider]
        try:
            cols = []
            for yt in range(y0, y1 + 1):
                row = [
                    np.array(Image.open(
                        BytesIO(requests.get(
                            url_t.format(z=zoom, x=xt, y=yt),
                            headers=HEADERS, timeout=10).content))
                        .convert("RGB"))
                    for xt in range(x0, x1 + 1)
                ]
                cols.append(np.concatenate(row, axis=1))
            mosaic = np.concatenate(cols, axis=0)
        except Exception:
            continue                    # fall through to the next provider
        scale = MERCATOR_EXTENT / (n * TILE_SIZE)
        x_min = -MERCATOR_EXTENT / 2.0 + x0 * TILE_SIZE * scale
        x_max = -MERCATOR_EXTENT / 2.0 + (x1 + 1) * TILE_SIZE * scale
        y_max = MERCATOR_EXTENT / 2.0 - y0 * TILE_SIZE * scale
        y_min = MERCATOR_EXTENT / 2.0 - (y1 + 1) * TILE_SIZE * scale
        return mosaic, (x_min, x_max, y_min, y_max), provider
    return None


def _signed_area(ring):
    """Shoelace signed area (positive = counter-clockwise)."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def plot_overlay(result, pair, basemap, out_path, title):
    """Overlay the two footprints on the basemap (or a plain frame) as a PNG.

    pair = (reference_union_geom, simulated_union_geom) in EPSG:3857, so the
    axis is 3857-native and mosaicked tiles need no reprojection. basemap is a
    (mosaic, extent) as returned by fetch_basemap(), or None for a plain frame.
    matplotlib is imported lazily here (tools convention).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.path import Path as MplPath
    from matplotlib.patches import PathPatch, Patch
    from matplotlib.ticker import FuncFormatter

    ref_geom, sim_geom = pair

    def path_for(geom, ox, oy):
        """Path with MOVETO/LINETO subpaths; outer rings CCW, holes CW, so the
        nonzero winding rule renders holes correctly. Vertices are shifted by
        (ox, oy): Agg's vector-polygon fill silently degrades for coordinates
        ~1e7 m from the origin, so the whole scene is drawn near (0, 0)."""
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        verts, codes = [], []
        for poly in polys:
            ext = list(poly.exterior.coords)[:-1]
            rings = [(ext, True)] + [
                (list(h.coords)[:-1], False) for h in poly.interiors]
            for ring, outer in rings:
                if len(ring) < 3:
                    continue
                if (_signed_area(ring) > 0) != outer:   # outer -> CCW, hole -> CW
                    ring = ring[::-1]
                verts.extend((x - ox, y - oy) for x, y in ring)
                codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 1)
        return MplPath(np.asarray(verts, dtype=float), codes)

    def draw(ax, geom, ox, oy, **kw):
        ax.add_patch(PathPatch(path_for(geom, ox, oy), fill=True, **kw))

    minx, miny, maxx, maxy = ref_geom.union(sim_geom).bounds
    pad = max((maxx - minx), (maxy - miny), 1.0) * 0.05
    ox, oy = minx, miny
    fig, ax = plt.subplots(figsize=(12.8, 10.0))
    if basemap is not None:
        mosaic, extent, _ = basemap
        ex = extent
        ax.imshow(mosaic, extent=(ex[0] - ox, ex[1] - ox, ex[2] - oy, ex[3] - oy),
                  interpolation="nearest", zorder=0)
    else:
        ax.set_xlabel("EPSG:3857 easting (m)")
        ax.set_ylabel("EPSG:3857 northing (m)")
    # the whole scene (polygons, tiles, axis limits) stays near (0, 0) so
    # Agg's vector-polygon fill is exact; ticks relabel to absolute meters.
    ax.set_xlim(minx - ox - pad, maxx - ox + pad)
    ax.set_ylim(miny - oy - pad, maxy - oy + pad)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{v + ox:,.0f}"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{v + oy:,.0f}"))
    ax.set_aspect("equal")
    draw(ax, ref_geom, ox, oy, facecolor="#d9d9d9", edgecolor="red",
         linewidth=1.2, zorder=1)
    draw(ax, sim_geom, ox, oy, facecolor="cyan", edgecolor="black",
         linewidth=1.2, zorder=2, alpha=0.5)
    ax.legend(handles=[
        Patch(facecolor="#d9d9d9", edgecolor="red", label="CalFire perimeter"),
        Patch(facecolor="cyan", edgecolor="black", alpha=0.5, label="FARSITE"),
    ], loc="best", framealpha=0.9)
    ax.set_title(title)
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="compare_perimeter.py",
        description="Compare the CalFire reference perimeter against the "
                    "FARSITE-simulated perimeter (IoU + overlay PNG).",
        epilog="Exactly one source: --run-dir, or --reference + --simulated "
               "together.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-dir", metavar="PATH", default=None,
                     help="pipeline run dir; reference_perimeter.shp + fire.json "
                          "derive the simulated FARSITE output from it")
    src.add_argument("--reference", metavar="SHP", default=None,
                     help="reference (real CalFire) perimeter shapefile "
                          "(requires --simulated)")
    p.add_argument("--simulated", metavar="SHP", default=None,
                   help="simulated (FARSITE) perimeter shapefile "
                        "(requires --reference)")
    p.add_argument("--metric", default="iou",
                   help="comma-list of metrics to print (valid: "
                        f"{'/'.join(METRICS)})")
    p.add_argument("--plot", metavar="PNG", default=None,
                   help="write the overlay PNG to this path")
    p.add_argument("--json", metavar="PATH", default=None,
                   help="write the machine-readable compare() dict as JSON")
    p.add_argument("--basemap", choices=("auto", "imagery", "osm", "none"),
                   default="auto",
                   help="auto tries imagery then osm; none skips tiles "
                        "(default auto)")
    p.add_argument("--max-tiles", type=int, default=36,
                   help="skip a basemap provider needing more tiles than this "
                        "(default 36)")
    p.add_argument("--max-zoom", type=int, default=17,
                   help="uppermost basemap zoom allowed (default 17)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.run_dir and args.simulated:
        die("--run-dir is mutually exclusive with --reference/--simulated")
    if (args.reference is None) != (args.simulated is None):
        die("give both --reference and --simulated, or --run-dir only")

    fire_name = "perimeter comparison"
    if args.run_dir:
        run = Path(args.run_dir)
        ref = run / "reference_perimeter.shp"
        if not ref.is_file():
            die(f"no reference perimeters at {ref} "
                f"(--run-dir needs reference_perimeter.shp)")
        try:
            name = json.loads((run / "fire.json").read_text()).get("name")
        except (OSError, ValueError):
            name = None
        if not name:
            die(f"no fire.json (or no 'name' inside) in {run} - cannot derive "
                f"the FARSITE output path")
        slug = str(name).lower().replace(" ", "-")
        sim = run / "farsite-out" / f"{slug}_Perimeters.shp"
        if not sim.is_file():
            die(f"no simulated perimeters at {sim} — did the FARSITE stage run?")
        fire_name = str(name)
    else:
        sim, ref = Path(args.simulated), Path(args.reference)

    metrics = [m.strip().lower() for m in args.metric.split(",") if m.strip()]
    if not metrics:
        die("--metric cannot be empty")
    for m in metrics:
        if m not in METRICS:
            die(f"unknown metric '{m}' (valid: {'/'.join(METRICS)})")

    result = compare(sim, ref)

    sim_fp = footprints(sim)
    ref_fp = footprints(ref)
    metric_crs = metric_crs_for(sim_fp["crs"], sim_fp["geoms"])
    a_geoms = to_crs(sim_fp["geoms"], sim_fp["crs"], metric_crs)
    b_geoms = to_crs(ref_fp["geoms"], ref_fp["crs"], metric_crs)
    A = unary_union(a_geoms)
    B = unary_union(b_geoms)

    print(f"reference: {ref}")
    print(f"  records: {ref_fp['meta']['records']}")
    print(f"simulated: {sim}")
    print(f"  records: {sim_fp['meta']['records']}   "
          f"elapsed_field: {sim_fp['meta']['field']}")
    if sim_fp["meta"]["elapsed"] is not None:
        print(f"  elapsed_minutes: {sim_fp['meta']['elapsed']:g}")
    epsg = metric_crs.to_epsg()
    print(f"metric_crs: {epsg if epsg is not None else metric_crs.to_wkt()}")
    print(f"reference_ha: {result['reference_ha']:.1f}")
    print(f"simulated_ha: {result['simulated_ha']:.1f}")
    print(f"intersection_m2: {result['area_m2']['intersection']:.0f}")
    print(f"union_m2: {result['area_m2']['union']:.0f}")
    for m in metrics:
        print(f"{m} = {compute(m, A, B):.3f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2, sort_keys=True)
        print(f"json: {args.json}")

    if args.plot:
        merc = pyproj.CRS.from_epsg(3857)
        pair = (unary_union(to_crs(ref_fp["geoms"], ref_fp["crs"], merc)),
                unary_union(to_crs(sim_fp["geoms"], sim_fp["crs"], merc)))
        minx, miny, maxx, maxy = pair[0].union(pair[1]).bounds
        pad = max((maxx - minx), (maxy - miny), 1.0) * 0.1
        bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)
        zoom = _choose_zoom(maxx - minx, args.max_zoom)
        if args.basemap == "none":
            basemap = None
        else:
            providers = (["imagery", "osm"] if args.basemap == "auto"
                         else [args.basemap])
            basemap = fetch_basemap(providers, bounds, zoom, args.max_tiles)
        title = f"{fire_name} — IoU = {result['iou']:.3f}"
        plot_overlay(result, pair, basemap, args.plot, title)
        if basemap is None:
            if args.basemap == "none":
                print("basemap: none (--basemap none)")
            else:
                print("basemap: none — tile fetch failed")
        else:
            print(f"basemap: {basemap[2]}")
        print(f"plot: {args.plot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
