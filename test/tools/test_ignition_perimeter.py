"""Offline tests for the WFIGS-origin containment check.

A WFIGS point of origin can sit outside the CAL FIRE reference perimeter
(e.g. POST 2024 LAC 00205253, ~2.3 km off - a source-data mismatch, not a
pipeline bug). rather than auto-heal it, orchestrate dies pointing at a
seed-vs-perimeter PNG so the user sees the offset at a glance; the fuel
nudge only runs after this check. `--dry-run` reports + never writes.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
import shapefile
from pyproj import CRS, Transformer

from orchestrate import (ensure_ignition_within_perimeter, load_config,
                         main, _rings_union)

CENTER = (34.0845, -118.5405)
DATA = Path(__file__).resolve().parent.parent / "data"


def write_fire_json(tmp_path, lat=CENTER[0], lon=CENTER[1]):
    p = tmp_path / "fire.json"
    p.write_text(json.dumps({"name": "TEST", "year": 2024,
                             "lat": lat, "lon": lon}))
    return p


def write_ref(tmp_path, cx, cy, size=1000.0):
    """A square EPSG:5070 reference polygon centered on (cx, cy), size meters;
    one POLYGON record with a single ring."""
    p = tmp_path / "reference_perimeter.shp"
    w = shapefile.Writer(str(p), shapeType=shapefile.POLYGON)
    w.field("NAME", "C", 40)
    h = size / 2.0
    w.poly([[(cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h),
             (cx - h, cy + h), (cx - h, cy - h)]])
    w.record("ref")
    w.close()
    (tmp_path / "reference_perimeter.prj").write_text(
        CRS.from_epsg(5070).to_wkt())
    return p.with_suffix(".shp")


def center5070():
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    return t.transform(CENTER[1], CENTER[0])


def test_origin_inside_is_accepted_without_plot(tmp_path):
    cx, cy = center5070()
    write_ref(tmp_path, cx, cy)
    fj = write_fire_json(tmp_path)          # inside the 1 km square
    png = tmp_path / "ignition_outside_perimeter.png"
    rec = ensure_ignition_within_perimeter(fj, tmp_path / "reference_perimeter.shp",
                                           tolerance_m=100.0, plot_path=png)
    assert rec["inside"] is True
    assert not png.exists()


def test_origin_outside_writes_png_and_reports_nearest(tmp_path):
    cx, cy = center5070()
    write_ref(tmp_path, cx, cy)
    lon0, lat0 = Transformer.from_crs("EPSG:5070", "EPSG:4326",
                                      always_xy=True).transform(cx, cy)
    # 5 km west of the 1 km square -> outside by ~4500 m
    fj = write_fire_json(tmp_path, lat=lat0, lon=lon0 - 0.06)
    png = tmp_path / "ignition_outside_perimeter.png"
    rec = ensure_ignition_within_perimeter(fj, tmp_path / "reference_perimeter.shp",
                                           tolerance_m=100.0, plot_path=png)
    assert rec["inside"] is False
    assert 4500 < rec["distance_m"] < 5600          # ~5 km west of a 1 km box
    assert abs(rec["nearest_lon"] - CENTER[1]) < 0.01
    assert png.is_file() and png.stat().st_size > 0


def test_dry_does_not_write_png(tmp_path):
    cx, cy = center5070()
    write_ref(tmp_path, cx, cy)
    lon0, lat0 = Transformer.from_crs("EPSG:5070", "EPSG:4326",
                                      always_xy=True).transform(cx, cy)
    fj = write_fire_json(tmp_path, lat=lat0, lon=lon0 - 0.06)
    png = tmp_path / "ignition_outside_perimeter.png"
    rec = ensure_ignition_within_perimeter(fj, tmp_path / "reference_perimeter.shp",
                                           tolerance_m=100.0, plot_path=png,
                                           dry=True)
    assert rec["inside"] is False
    assert not png.exists()


def test_missing_reference_returns_none(tmp_path):
    fj = write_fire_json(tmp_path)
    assert ensure_ignition_within_perimeter(
        fj, tmp_path / "reference_perimeter.shp", tolerance_m=100.0) is None


def test_tolerance_allows_near_edge_origin(tmp_path):
    cx, cy = center5070()
    write_ref(tmp_path, cx, cy)
    lon0, lat0 = Transformer.from_crs("EPSG:5070", "EPSG:4326",
                                      always_xy=True).transform(cx, cy)
    fj = write_fire_json(tmp_path, lat=lat0, lon=lon0 - 0.0115)  # just W of edge
    rec = ensure_ignition_within_perimeter(
        fj, tmp_path / "reference_perimeter.shp", tolerance_m=1000.0)
    assert rec["inside"] is False
    assert 300 < rec["distance_m"] < 1200          # tolerance widens acceptance


def test_rings_union_keeps_islands_separate(tmp_path):
    """Two non-nested rings must stay separate parts, not become
    ring[0]-exterior-with-ring[1]-hole (which would garble containment)."""
    from shapely.geometry import Polygon

    p = tmp_path / "ref.shp"
    w = shapefile.Writer(str(p), shapeType=shapefile.POLYGON)
    w.field("N", "C", 20)
    a = [(0, 0), (0, 1000), (1000, 1000), (1000, 0), (0, 0)]
    b = [(5000, 0), (5000, 1000), (6000, 1000), (6000, 0), (5000, 0)]
    w.poly([a, b])      # two parts in one record
    w.record("x")
    w.close()
    (tmp_path / "ref.prj").write_text(CRS.from_epsg(5070).to_wkt())

    u = _rings_union(p)
    assert u.geom_type == "MultiPolygon" and len(u.geoms) == 2
    for g in u.geoms:
        assert g.geom_type == "Polygon" and len(g.interiors) == 0


def test_rings_union_nested_becomes_hole(tmp_path):
    from shapely.geometry import Polygon

    p = tmp_path / "ref.shp"
    w = shapefile.Writer(str(p), shapeType=shapefile.POLYGON)
    w.field("N", "C", 20)
    outer = [(0, 0), (0, 1000), (1000, 1000), (1000, 0), (0, 0)]
    inner = [(400, 400), (400, 600), (600, 600), (600, 400), (400, 400)]
    w.poly([outer, inner])
    w.record("x")
    w.close()
    (tmp_path / "ref.prj").write_text(CRS.from_epsg(5070).to_wkt())

    u = _rings_union(p)
    assert u.geom_type == "Polygon" and len(u.interiors) == 1


def test_config_validates_origin_tolerance_m(tmp_path):
    load_config(_write(tmp_path, "ok.toml", {"fire": {"origin_tolerance_m": 50}}))
    load_config(_write(tmp_path, "zero.toml", {"fire": {"origin_tolerance_m": 0}}))
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, "neg.toml",
                           {"fire": {"origin_tolerance_m": -5}}))


def test_stage_ignition_dies_with_visualization_point(tmp_path):
    """End-to-end through Runner.run(): an origin well outside the perimeter
    must abort (exit 2) naming the PNG, before any fuel nudge."""
    cx, cy = center5070()
    write_ref(tmp_path, cx, cy)
    lon0, lat0 = Transformer.from_crs("EPSG:5070", "EPSG:4326",
                                      always_xy=True).transform(cx, cy)
    write_fire_json(tmp_path, lat=lat0, lon=lon0 - 0.06)  # ~4.5 km west

    from test_ignition_burnable import write_lcp, write_ignition
    write_lcp(tmp_path, 122)
    write_ignition(tmp_path)

    png = tmp_path / "ignition_outside_perimeter.png"
    cfg = _write(tmp_path, "run.toml", {
        "fire": {"enable": False, "fire_json": str(tmp_path / "fire.json")},
        "landscape": {"enable": False, "lcp": str(tmp_path / "lcp.tif")},
        "simulation": {"start": "2024-06-15T07:00Z",
                       "end": "2024-06-26T07:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": False, "run_root": str(tmp_path / "windroot")},
        "weather": {"enable": False,
                    "wxs": str(DATA / "palisades-hrrr.wxs")},
        "farsite": {"enable": False, "run": False},
    })
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg)])
    assert exc.value.code == 2
    assert png.is_file() and png.stat().st_size > 0


def _write(path, name, sections):
    def toml_val(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)
        raise TypeError(f"unsupported TOML value: {v!r}")

    path = path / name
    lines = []
    for sec, vals in sections.items():
        lines.append(f"[{sec}]")
        for k, v in vals.items():
            lines.append(f"{k} = {toml_val(v)}")
    path.write_text("\n".join(lines) + "\n")
    return path
