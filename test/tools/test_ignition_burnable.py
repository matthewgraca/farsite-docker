"""Offline tests for orchestrate.ensure_ignition_burnable.

A WFIGS/IRWIN ignition seed can land on a non-burnable LCP pixel (NB1-9 / model
0 / nodata), in which case FARSITE would show no growth. The preflight nudges
the seed to the nearest burnable cell, rewrites ignition.shp (the file FARSITE
reads), records an audit block in fire.json, and never touches fire.json's
lat/lon (the weather/elevation anchor).
"""

import json

import numpy as np
import pytest
import rasterio
import shapefile
from pyproj import CRS, Transformer

from orchestrate import ensure_ignition_burnable

CENTER = (34.0845, -118.5405)     # seed location (matches raster center pixel)
SEED_PX = 15                      # raster is 31x31, seed at pixel (15,15)
FUEL_OK = 122                     # burnable fuel code (e.g. TU1)
FUEL_NB = 91                      # NB1 non-burnable


def write_lcp(tmp_path, seed_fuel):
    """31x31 int16 EPSG:5070 raster centered on CENTER; band 4 = seed_fuel at
    the center pixel, FUEL_OK everywhere else (band 1 = flat elevation)."""
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    cx, cy = t.transform(CENTER[1], CENTER[0])
    prof = dict(driver="GTiff", height=31, width=31, count=4, dtype="int16",
                crs="EPSG:5070",
                transform=rasterio.Affine(30, 0, cx - 15 * 30, 0, -30, cy + 15 * 30),
                nodata=-9999)
    fuel = np.full((31, 31), FUEL_OK, dtype=np.int16)
    fuel[SEED_PX, SEED_PX] = seed_fuel
    elev = np.zeros((31, 31), dtype=np.int16)
    p = tmp_path / "lcp.tif"
    with rasterio.open(p, "w", **prof) as r:
        r.write(elev, 1)
        r.write(fuel, 4)
    return p


def write_ignition(tmp_path, at=CENTER):
    """A single-point POINT shapefile at `at` (WGS84) with an EPSG:5070 .prj."""
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    x, y = t.transform(at[1], at[0])
    p = tmp_path / "ignition.shp"
    w = shapefile.Writer(str(p))
    w.shapeType = 1
    w.field("NAME", "C", 40)
    w.record("wfigs-seed")
    w.point(x, y)
    w.close()
    (tmp_path / "ignition.prj").write_text(CRS.from_epsg(5070).to_wkt())
    return p


def write_fire_json(tmp_path):
    p = tmp_path / "fire.json"
    p.write_text(json.dumps({"name": "PALISADES", "year": 2025,
                             "lat": CENTER[0], "lon": CENTER[1]}))
    return p


def read_seed(p):
    r = shapefile.Reader(str(p))
    return tuple(float(v) for v in r.shape(0).points[0])


def test_burnable_seed_is_left_untouched(tmp_path):
    lcp = write_lcp(tmp_path, seed_fuel=FUEL_OK)
    ign = write_ignition(tmp_path)
    fj = write_fire_json(tmp_path)
    before = read_seed(ign)
    rec = ensure_ignition_burnable(lcp, ign, fj)
    assert rec["burnable"] is True and rec["adjusted"] is False
    assert read_seed(ign) == before
    assert not json.loads(fj.read_text()).get("ignition_adjusted")


def test_non_burnable_seed_is_nudged_and_recorded(tmp_path):
    lcp = write_lcp(tmp_path, seed_fuel=FUEL_NB)
    ign = write_ignition(tmp_path)
    fj = write_fire_json(tmp_path)
    orig = read_seed(ign)
    rec = ensure_ignition_burnable(lcp, ign, fj)
    assert rec["adjusted"] is True
    assert rec["seed_fuel"] == FUEL_NB
    assert rec["adjusted_fuel"] == FUEL_OK
    assert 0 < rec["offset_m"] <= 60          # nearest neighbor, ~30-60 m
    moved = read_seed(ign)
    assert moved != orig                      # geometry rewritten
    with rasterio.open(lcp) as ds:
        moved_px = ds.index(*moved)
    assert moved_px != (SEED_PX, SEED_PX)     # moved off the NB pixel
    # audit block written; weather anchor (lat/lon) untouched
    doc = json.loads(fj.read_text())
    adj = doc["ignition_adjusted"]
    assert adj["original"]["lat"] == pytest.approx(CENTER[0])
    assert adj["offset_m"] > 0
    assert doc["lat"] == CENTER[0] and doc["lon"] == CENTER[1]   # anchor untouched


def test_dry_run_reports_but_writes_nothing(tmp_path):
    lcp = write_lcp(tmp_path, seed_fuel=FUEL_NB)
    ign = write_ignition(tmp_path)
    fj = write_fire_json(tmp_path)
    before = read_seed(ign)
    rec = ensure_ignition_burnable(lcp, ign, fj, dry=True)
    assert rec["adjusted"] is True
    assert read_seed(ign) == before
    assert not json.loads(fj.read_text()).get("ignition_adjusted")


def test_no_burnable_within_cap_dies(tmp_path, capsys):
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    cx, cy = t.transform(CENTER[1], CENTER[0])
    prof = dict(driver="GTiff", height=31, width=31, count=4, dtype="int16",
                crs="EPSG:5070",
                transform=rasterio.Affine(30, 0, cx - 15 * 30, 0, -30, cy + 15 * 30),
                nodata=-9999)
    fuel = np.full((31, 31), FUEL_NB, dtype=np.int16)   # entire raster non-burnable
    p = tmp_path / "allnb.tif"
    with rasterio.open(p, "w", **prof) as r:
        r.write(np.zeros((31, 31), dtype=np.int16), 1)
        r.write(fuel, 4)
    ign = write_ignition(tmp_path)
    fj = write_fire_json(tmp_path)
    with pytest.raises(SystemExit) as exc:
        ensure_ignition_burnable(p, ign, fj, max_nudge_m=200.0)
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "refusing to nudge" in (cap.err + cap.out)
