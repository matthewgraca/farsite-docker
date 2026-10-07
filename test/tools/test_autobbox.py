"""Offline tests for the auto-bbox (bbox_from=reference) pipeline feature.

`[landscape] bbox_from = "reference"` derives the LFPS bbox from the CalFire
reference perimeter with a configurable margin (`bbox_margin_m`: "auto" = 10%
of the perimeter's width/height per axis, or a fixed meter value), so FARSITE
has headroom to overestimate without hitting the landscape edge. The fire
stage runs first in this mode (its seed CRS targets the LFPS EPSG:5070
landscape that is about to be ingested).
"""

import contextlib
import io
import sys
from pathlib import Path

import pytest
import shapefile
from pyproj import CRS, Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from orchestrate import (_LFPS_CRS, _expanded_projected_bounds, _geometry_extent,
                         _reference_bbox, load_config, main)

DATA = Path(__file__).resolve().parent.parent / "data"


def write_reference(tmp_path, bounds=(0.0, 0.0, 100000.0, 200000.0)):
    """A synthetic EPSG:5070 reference polygon covering `bounds` (meters)."""
    base = tmp_path / "reference_perimeter"
    w = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    w.field("NAME", "C", 40)
    x0, y0, x1, y1 = bounds
    w.poly([[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]])
    w.record("ref")
    w.close()
    base.with_suffix(".prj").write_text(CRS.from_epsg(5070).to_wkt())
    return base.with_suffix(".shp")


def test_auto_margin_is_10_percent_per_axis(tmp_path):
    ref = write_reference(tmp_path, (0, 0, 100000, 200000))
    x0, y0, x1, y1 = _expanded_projected_bounds(ref, _LFPS_CRS, "auto")
    # 10% of width (1e5 -> 1e4) and height (2e5 -> 2e4), per axis
    assert (x0, y0, x1, y1) == pytest.approx(
        (-10000, -20000, 110000, 220000), abs=1e-6)
    assert (x1 - x0) == pytest.approx(1.2e5)
    assert (y1 - y0) == pytest.approx(2.4e5)


def test_explicit_margin_takes_a_fixed_value(tmp_path):
    ref = write_reference(tmp_path, (0, 0, 100000, 200000))
    x0, y0, x1, y1 = _expanded_projected_bounds(ref, _LFPS_CRS, 5000.0)
    assert (x0, y0, x1, y1) == pytest.approx(
        (-5000, -5000, 105000, 205000), abs=1e-6)


def test_wgs84_bbox_contains_the_reference(tmp_path):
    ref = write_reference(tmp_path, (0, 0, 100000, 200000))
    w, s, e, n = (float(v) for v in _reference_bbox(ref, _LFPS_CRS, "auto").split())
    t = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    lons, lats = [], []
    for px, py in ((0, 0), (100000, 0), (0, 200000), (100000, 200000)):
        lon, lat = t.transform(px, py)
        lons.append(lon)
        lats.append(lat)
    assert w <= min(lons) and e >= max(lons)
    assert s <= min(lats) and n >= max(lats)


def test_missing_reference_dies(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _reference_bbox(tmp_path / "reference_perimeter.shp", _LFPS_CRS, "auto")
    assert exc.value.code == 2


def test_corrupt_shp_header_bbox_is_ignored(tmp_path):
    """Regression: the SHP *header* bbox is advisory and can carry garbage
    (NaN/±inf), which used to route non-finite bounds through the CRS
    transform - lat snapped to the 90-degree pole, lon became NaN. The extent
    must come from the geometry records, never the header."""
    import struct

    ref = write_reference(tmp_path, (0, 0, 100000, 200000))
    raw = bytearray(ref.read_bytes())
    # overwrite the header bbox (bytes 36..68: minX,minY,maxX,maxY) with NaN
    struct.pack_into("<4d", raw, 36, float("nan"), float("nan"),
                     float("nan"), float("nan"))
    ref.write_bytes(raw)

    r = shapefile.Reader(str(ref))
    assert all(v != v for v in r.bbox)  # header really is NaN now

    # geometry-derived extent + margin are unaffected by the corrupt header
    x0, y0, x1, y1 = _expanded_projected_bounds(ref, _LFPS_CRS, 5000.0)
    assert (x0, y0, x1, y1) == pytest.approx(
        (-5000, -5000, 105000, 205000), abs=1e-6)


def test_nonfinite_vertex_dies(tmp_path):
    class FakeShape:
        points = [(0.0, 0.0), (float("nan"), 1.0)]

    class FakeReader:
        def iterShapes(self):
            return iter([FakeShape()])

    with pytest.raises(SystemExit) as exc:
        _geometry_extent(FakeReader(), tmp_path / "corrupt.shp")
    assert exc.value.code == 2


def test_config_validates_bbox_from_and_margin(tmp_path):
    load_config(_write(tmp_path, "ok.toml",
                       {"landscape": {"enable": True, "bbox_from": "reference",
                                      "bbox_margin_m": "auto",
                                      "email": "a@b.c"}}))
    load_config(_write(tmp_path, "ok2.toml",
                       {"landscape": {"enable": True, "bbox_from": "reference",
                                      "bbox_margin_m": 8000.0,
                                      "email": "a@b.c"}}))
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, "bad-from.toml",
                           {"landscape": {"bbox_from": "other"}}))
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, "bad-margin.toml",
                           {"landscape": {"bbox_margin_m": "wide"}}))
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, "neg-margin.toml",
                           {"landscape": {"bbox_margin_m": -5}}))


def test_orchestrate_plans_expanded_bbox_for_bbox_from(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "fire.json").write_text(
        '{"name": "PALISADES", "year": 2025, "lat": 34.0, "lon": -118.0}')
    ref = write_reference(run, (0.0, 0.0, 100000.0, 200000.0))
    expected = _reference_bbox(ref, _LFPS_CRS, 5000.0)

    cfg = _write(tmp_path, "auto.toml", {
        "landscape": {"enable": True, "bbox_from": "reference",
                      "bbox_margin_m": 5000.0, "email": "a@b.c"},
        "fire": {"enable": False, "fire_json": str(run / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z",
                       "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": False,
                      "run_root": str(run / "windroot")},
        "weather": {"enable": False, "wxs": str(DATA / "palisades-hrrr.wxs")},
        "farsite": {"enable": False},
    })
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = main(["--config", str(cfg), "--dry-run"])
    assert rc == 0
    assert f"--bbox {expected}" in out.getvalue()


def test_bbox_and_bbox_from_are_mutually_exclusive(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "fire.json").write_text('{"name": "P", "year": 2025}')
    cfg = _write(tmp_path, "both.toml", {
        "landscape": {"enable": True, "bbox_from": "reference",
                      "bbox": "-120 33 -116 35", "email": "a@b.c"},
        "fire": {"enable": False, "fire_json": str(run / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z", "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": False,
                      "run_root": str(run / "windroot")},
        "weather": {"enable": False, "wxs": str(DATA / "palisades-hrrr.wxs")},
        "farsite": {"enable": False},
    })
    with pytest.raises(SystemExit):
        main(["--config", str(cfg), "--dry-run"])
    cap = capsys.readouterr()
    assert "exactly one of" in (cap.err + cap.out)


def _write(tmp_path, name, sections):
    import json as _json

    def toml_val(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return _json.dumps(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(toml_val(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{k} = {toml_val(x)}" for k, x in v.items()) + " }"
        raise TypeError(f"unsupported TOML value: {v!r}")

    path = tmp_path / name
    lines = []
    for sec, vals in sections.items():
        lines.append(f"[{sec}]")
        for k, v in vals.items():
            lines.append(f"{k} = {toml_val(v)}")
    path.write_text("\n".join(lines) + "\n")
    return path
