"""Offline tests for calfire_ignition .prj output (runfarsite compatibility).

runfarsite's embedded GDAL parses legacy WKT1 (.prj) and rejects pyproj's
WKT2 default (GEOGCRS/ENSEMBLE...) with "ERROR 1: missing , or ]", which
misplaces the ignition and grows nothing. calfire must write its .prj as
WKT1_GDAL - never the WKT2 default.
"""

from pathlib import Path

import pytest
from pyproj import CRS

from calfire_ignition import _write_shapefile, write_polygon_shp

ATTRS = {"FireName": "x", "Year": 2025, "Cause": "14", "Acres": 1.0,
         "Start": "s", "Contain": "e", "Lat": 34.0, "Lon": -118.0}


def test_point_prj_is_wkt1_gdal(tmp_path):
    _write_shapefile(tmp_path, "ignition", 1, (-118.0, 34.0), ATTRS,
                     CRS.from_epsg(5070))
    prj = (tmp_path / "ignition.prj").read_text()
    assert "PROJCS[" in prj                    # WKT1 projected form
    assert "GEOGCRS" not in prj and "ENSEMBLE" not in prj  # not WKT2
    assert CRS.from_wkt(prj).to_epsg() == 5070


def test_polygon_prj_is_geographic_wkt1(tmp_path):
    poly = [[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]]
    _write_shapefile(tmp_path, "reference_perimeter", 5, poly, ATTRS,
                     CRS.from_epsg(4326))
    prj = (tmp_path / "reference_perimeter.prj").read_text()
    assert "GEOGCS[" in prj and "WGS 84" in prj    # WKT1 geographic form
    assert CRS.from_wkt(prj).to_epsg() == 4326


def test_nonfinite_vertex_dies_before_writing(monkeypatch, tmp_path):
    """A non-finite perimeter vertex must abort with attribution (ring/pt +
    source coord) instead of silently writing a poisoned .shp that breaks the
    auto-bbox reader downstream."""
    from calfire_ignition import die as _die
    calls = []
    monkeypatch.setattr("calfire_ignition.die",
                        lambda m: calls.append(m) or (_ for _ in ()).throw(SystemExit(2)))
    geom = {"type": "Polygon",
            "coordinates": [[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)],
                            [(0.2, 0.2), (float("inf"), float("inf")),
                             (0.8, 0.8)]]}
    with pytest.raises(SystemExit):
        write_polygon_shp(Path(tmp_path), geom, ATTRS, CRS.from_epsg(5070))
    assert "ring 1 pt 1" in calls[0] and "inf" in calls[0]


def test_reproject_retries_once_and_succeeds(monkeypatch, tmp_path):
    from pyproj import Transformer

    real = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    calls = []
    state = {"infs_left": 1}                 # one-shot global glitch
    def flaky_crs(crs):
        calls.append(crs)
        def go(self, x, y):
            if state["infs_left"] > 0:
                state["infs_left"] -= 1
                return float("inf"), float("inf")
            return real.transform(x, y)
        return type("T", (), {"transform": go})()
    monkeypatch.setattr("calfire_ignition._reproject", flaky_crs)
    geom = {"type": "Polygon",
            "coordinates": [[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]]}
    write_polygon_shp(Path(tmp_path), geom, ATTRS, CRS.from_epsg(5070))
    assert len(calls) == 2                   # initial + retry with a fresh init
    assert (tmp_path / "reference_perimeter.shp").is_file()


def test_reproject_persistently_infinite_dies(monkeypatch, tmp_path):
    """A transformer that NEVER yields a finite result must still abort with the
    reprojection attribution (never silently write an inf-laced .shp)."""
    class _AlwaysInf:
        def transform(self, x, y):
            return float("inf"), float("inf")
    monkeypatch.setattr("calfire_ignition._reproject", lambda crs: _AlwaysInf())
    monkeypatch.setattr("calfire_ignition.die",
                        lambda m: (_ for _ in ()).throw(SystemExit(2)))
    geom = {"type": "Polygon",
            "coordinates": [[(-122, 37), (-122, 38), (-121, 38),
                             (-121, 37), (-122, 37)]]}
    with pytest.raises(SystemExit):
        write_polygon_shp(Path(tmp_path), geom, ATTRS, CRS.from_epsg(5070))
