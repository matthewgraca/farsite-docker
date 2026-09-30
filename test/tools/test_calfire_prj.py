"""Offline tests for calfire_ignition .prj output (runfarsite compatibility).

runfarsite's embedded GDAL parses legacy WKT1 (.prj) and rejects pyproj's
WKT2 default (GEOGCRS/ENSEMBLE...) with "ERROR 1: missing , or ]", which
misplaces the ignition and grows nothing. calfire must write its .prj as
WKT1_GDAL - never the WKT2 default.
"""

from pyproj import CRS

from calfire_ignition import _write_shapefile

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
