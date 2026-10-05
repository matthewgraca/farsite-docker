"""Offline tests for tools/proj_axes.py (axis-order-robust transforms).

pyproj's `always_xy=True` is honored on a healthy install but is ignored on
some Windows builds (a valid CONUS (lon, lat) reprojects to (inf, inf) as the
input is consumed as (lat, lon), and geographic outputs come back
(lat, lon)-ordered). GeoProj must produce the same (lon, lat) semantics on
both kinds of build - the broken build is emulated here by monkeypatching
pyproj.Transformer.from_crs.
"""

import math
import sys
from pathlib import Path

import pyproj
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
import proj_axes
from proj_axes import GeoProj

CORRAL = (-121.472501388922, 37.6760138779413)   # (lon, lat) - the failing pt
CORRAL_5070 = (-2198813.51, 1922972.16)          # expected projected image


def test_forward_matches_pyproj_defaults():
    gp = GeoProj("EPSG:4326", "EPSG:5070")
    x, y = gp.transform(*CORRAL)
    assert (x, y) == pytest.approx(CORRAL_5070, abs=1.0)


def test_to_lonlat_roundtrips_projected_to_wgs():
    gp_f = GeoProj("EPSG:4326", "EPSG:5070")
    gp_r = GeoProj("EPSG:5070", "EPSG:4326")
    x, y = gp_f.transform(*CORRAL)
    lon, lat = gp_r.to_lonlat(x, y)
    assert (lon, lat) == pytest.approx(CORRAL, abs=1e-5)


def _broken_from_crs(real_fwd, real_rev):
    def factory(frm, too, always_xy=False, **kw):
        frm = pyproj.CRS.from_user_input(frm)
        too = pyproj.CRS.from_user_input(too)
        if frm.is_geographic and too.is_projected:
            class T:   # ignores always_xy on INPUT: consumes (lat, lon)
                def transform(self, a, b):
                    return real_fwd.transform(b, a)
            return T()
        if frm.is_projected and too.is_geographic:
            class T:   # OUTPUT swapped: returns (lat, lon)
                def transform(self, x, y):
                    lo, la = real_rev.transform(x, y)
                    return la, lo
            return T()
        return pyproj.Transformer.from_crs(frm, too, always_xy=always_xy, **kw)
    return factory


def test_broken_always_xy_build_still_transforms_correctly(monkeypatch):
    real_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070",
                                           always_xy=True)
    real_rev = pyproj.Transformer.from_crs("EPSG:5070", "EPSG:4326",
                                           always_xy=True)
    monkeypatch.setattr(proj_axes.pyproj.Transformer, "from_crs",
                        staticmethod(_broken_from_crs(real_fwd, real_rev)))
    gp_in = GeoProj("EPSG:4326", "EPSG:5070")
    assert gp_in._swap_in is True          # probe detected the broken build
    x, y = gp_in.transform(*CORRAL)
    assert (x, y) == pytest.approx(CORRAL_5070, abs=1.0)

    gp_out = GeoProj("EPSG:5070", "EPSG:4326")
    lon, lat = gp_out.to_lonlat(*CORRAL_5070)
    assert (lon, lat) == pytest.approx(CORRAL, abs=1e-5)


def test_probe_rejects_when_no_finite_forward():
    import proj_axes as pa
    class Nope:
        def transform(self, a, b):
            return float("nan"), float("nan")
    orig = pa.pyproj.Transformer.from_crs
    def bad_factory(frm, too, always_xy=False, **kw):
        return Nope()
    pa.pyproj.Transformer.from_crs = staticmethod(bad_factory)
    try:
        with pytest.raises(pyproj.exceptions.CRSError):
            GeoProj("EPSG:4326", "EPSG:5070")
    finally:
        pa.pyproj.Transformer.from_crs = orig


def test_geoproj_requires_a_geographic_endpoint():
    with pytest.raises(pyproj.exceptions.CRSError):
        GeoProj("EPSG:5070", "EPSG:32611")
