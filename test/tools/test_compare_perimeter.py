"""Offline tests for tools/compare_perimeter.py.

Covers shapefile loading (polygon vs closed-ring polyline, holes, multipart),
the elapsed-field final-perimeter selection (+ union-all fallback), the metric
CRS rule (sim CRS, UTM derivation for geographic sims), and the IoU metric.
No network and no fixtures: pyshp writes the synthetic shapefiles into
tmp_path, mirroring test_runroot_to_atm.py.
"""

import pyproj
import pytest
import shapefile
from shapely.geometry import Polygon

from compare_perimeter import (compare, compute, load_polygons,
                               select_final_perimeters)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def close(pts):
    """Close an open vertex list into a FARSITE-style ring."""
    return pts + [pts[0]] if pts[0] != pts[-1] else pts


def write(base_path, shp_type, crs, fields, rings_per_record, records):
    """Write `records` shapes (one list of rings per record) + the .prj
    companion, mirroring calfire_ignition's writer pattern."""
    base_path.parent.mkdir(parents=True, exist_ok=True)
    w = shapefile.Writer(str(base_path), shapeType=shp_type)
    for name, typ, size, dec in fields:
        w.field(name, typ, size, dec)
    for rings, rec in zip(rings_per_record, records):
        if shp_type in (shapefile.POLYLINE, shapefile.POLYLINEZ):
            w.line(rings)
        else:
            w.poly(rings)
        w.record(*rec)
    w.close()
    base_path.with_suffix(".prj").write_text(crs.to_wkt())
    return base_path


def sim_fields():
    """FARSITE-family dbf layout (Fire_Type ... Elapsed_Mi)."""
    return [("Fire_Type", "C", 40, 0), ("Month", "N", 4, 0),
            ("Day", "N", 4, 0), ("Hour", "N", 4, 0), ("Elapsed_Mi", "N", 12, 0)]


def test_iou_known_value():
    A = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
    B = Polygon([(1, 0), (3, 0), (3, 2), (1, 2)])   # 1 unit shift right
    assert compute("iou", A, B) == pytest.approx(1 / 3)


def test_sim_metric_crs_used(tmp_path):
    # projected sim CRS stays as-is; the geographic reference is reprojected
    sim = write(tmp_path / "sims" / "palisades_Perimeters.shp",
                shapefile.POLYLINE, pyproj.CRS.from_epsg(3857), sim_fields(),
                [[close([(-5000, -5000), (5000, -5000), (5000, 5000),
                         (-5000, 5000)])]],
                [("pal", 1, 1, 1, 4800)])
    ref = write(tmp_path / "refs" / "reference_perimeter.shp",
                shapefile.POLYGON, pyproj.CRS.from_epsg(4326),
                [("Name", "C", 12, 0)],
                [[close([(-0.0449, -0.0449), (0.0449, -0.0449),
                         (0.0449, 0.0449), (-0.0449, 0.0449)])]],
                [("pal",)])
    result = compare(sim, ref)
    assert result["metric_crs"] == 3857
    assert 0.0 < result["iou"] <= 1.0


def test_geographic_sim_crs_derives_utm(tmp_path):
    lon, lat = -118.55, 34.07   # Palisades area; UTM 11N, north
    ring = close([(lon - 0.005, lat - 0.005), (lon + 0.005, lat - 0.005),
                  (lon + 0.005, lat + 0.005), (lon - 0.005, lat + 0.005)])
    sim = write(tmp_path / "sims" / "sim_Perimeters.shp",
                shapefile.POLYLINE, pyproj.CRS.from_epsg(4326), sim_fields(),
                [[ring]], [("p", 1, 1, 1, 60)])
    ref = write(tmp_path / "refs" / "reference_perimeter.shp",
                shapefile.POLYGON, pyproj.CRS.from_epsg(4326),
                [("Name", "C", 12, 0)], [[ring]], [("r",)])
    result = compare(sim, ref)
    assert result["metric_crs"] == 32611


def test_select_final_perimeters_uses_max_elapsed(tmp_path):
    path = write(tmp_path / "sim" / "x_Perimeters.shp", shapefile.POLYLINE,
                 pyproj.CRS.from_epsg(32611), sim_fields(),
                 [[close([(0, 0), (20, 0), (20, 20), (0, 20)])],
                  [close([(0, 0), (10, 0), (10, 10), (0, 10)])],
                  [close([(0, 0), (30, 0), (30, 30), (0, 30)])]],
                 [("p", 1, 1, 1, 60), ("p", 1, 1, 1, 60), ("p", 1, 1, 1, 4800)])
    geoms, meta = select_final_perimeters(path)
    assert meta["field"] == "Elapsed_Mi"     # original casing is preserved
    assert meta["records"] == 3
    assert meta["elapsed"] == 4800
    assert len(geoms) == 1                       # only the max-elapsed record
    assert geoms[0].area == pytest.approx(900.0)


def test_select_fallback_all_when_no_elapsed_field(tmp_path):
    fields = [("Fire_Type", "C", 40, 0), ("Month", "N", 4, 0),
              ("Day", "N", 4, 0), ("Hour", "N", 4, 0)]     # no *elapsed*/time
    rings = [[close([(0, 0), (20, 0), (20, 20), (0, 20)])],
             [close([(0, 0), (10, 0), (10, 10), (0, 10)])]]
    path = write(tmp_path / "sim" / "x_Perimeters.shp", shapefile.POLYLINE,
                 pyproj.CRS.from_epsg(32611), fields, rings,
                 [("p", 1, 1, 1), ("p", 1, 1, 1)])
    geoms, meta = select_final_perimeters(path)
    assert meta["field"] is None
    assert meta["elapsed"] is None
    assert len(geoms) == 2                       # both records kept


def test_polygon_vs_polyline_rings_equal_area(tmp_path):
    ring = close([(0, 0), (40, 0), (40, 30), (20, 30), (20, 10), (0, 10)])
    fields = [("Elapsed_Mi", "N", 12, 0)]
    p1 = write(tmp_path / "a" / "a.shp", shapefile.POLYGON,
               pyproj.CRS.from_epsg(32611), fields, [[ring]], [(4800,)])
    p2 = write(tmp_path / "b" / "b.shp", shapefile.POLYLINE,
               pyproj.CRS.from_epsg(32611), fields, [[ring]], [(4800,)])
    (g1, _), (g2, _) = load_polygons(p1), load_polygons(p2)
    assert g1[0].area == pytest.approx(g2[0].area)
    assert g1[0].area == pytest.approx(800.0)   # 40x30 minus 20x20 notch


def test_interior_ring_becomes_hole(tmp_path):
    outer = close([(0, 0), (40, 0), (40, 40), (0, 40)])
    inner = close([(10, 10), (30, 10), (30, 30), (10, 30)])
    path = write(tmp_path / "h" / "h.shp", shapefile.POLYGON,
                 pyproj.CRS.from_epsg(32611), [("Elapsed_Mi", "N", 12, 0)],
                 [[outer, inner]], [(4800,)])
    geoms, _ = load_polygons(path)
    assert len(geoms) == 1
    g = geoms[0]
    assert len(g.interiors) == 1
    assert g.area == pytest.approx(1600.0 - 400.0)
    assert Polygon(g.interiors[0]).area == pytest.approx(400.0)
