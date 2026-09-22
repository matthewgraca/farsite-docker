"""Integration tests for tools/calfire_ignition.py.

Runs the real pipeline once against the live CAL FIRE FRAP and NIFC WFIGS
services for the 2025 Palisades fire, writing outputs into a pytest-managed
temp directory that is deleted on teardown (no output files are left behind).
Verifies fire.json metadata, both shapefiles, and the printed sample HRRR
command. Deselect with:
    pytest -m "not integration"
"""

import contextlib
import io
import json
import shutil
from types import SimpleNamespace

import pytest
import shapefile  # pyshp

from calfire_ignition import main

pytestmark = pytest.mark.integration

FIRE = "PALISADES"
YEAR = 2025

# Pinned to live service data (verified 2026-09-22). These exact values ARE
# the validation: if FRAP/WFIGS drift, the assertions fail loudly.
LAT = 34.0677826402894
LON = -118.551123339829
ACRES = 23448.88
CAUSE = "14"
AGENCY = "CDF"
UNIT = "LDF"
INC_NUM = "00000738"
IRWIN_ID = "{A7EA5D21-F882-44B8-BF64-44AB11059DC1}"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """Run the real pipeline once for Palisades 2025; delete all outputs."""
    out_dir = tmp_path_factory.mktemp("calfire_run")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main(["--fire-name", FIRE, "--year", str(YEAR),
                   "--out-dir", str(out_dir)])
    assert rc == 0
    yield SimpleNamespace(
        out_dir=out_dir,
        fire_json=json.loads((out_dir / "fire.json").read_text()),
        stdout=stdout.getvalue(),
    )
    # no output files left behind: everything lives in the pytest tmp dir
    shutil.rmtree(out_dir, ignore_errors=True)


def test_fire_json_matches_palisades_record(run):
    fj = run.fire_json
    assert fj["name"] == FIRE and fj["year"] == YEAR
    assert fj["alarm_date"] == "2025-01-07T00:00:00+00:00"
    assert fj["cont_date"] == "2025-01-31T00:00:00+00:00"
    assert fj["gis_acres"] == pytest.approx(ACRES, abs=0.01)
    assert fj["cause"] == CAUSE
    assert fj["agency"] == AGENCY
    assert fj["unit_id"] == UNIT
    assert fj["inc_num"] == INC_NUM
    assert fj["irwin_id"] == IRWIN_ID
    assert fj["lat"] == pytest.approx(LAT, abs=1e-5)
    assert fj["lon"] == pytest.approx(LON, abs=1e-5)
    assert fj["crs"] == 4326


def test_ignition_shapefile_is_point_seed(run):
    with shapefile.Reader(str(run.out_dir / "ignition")) as shp:
        assert shp.shapeType == shapefile.POINT   # 1
        assert shp.numRecords == 1
        rec = shp.record(0)
        assert rec["FireName"] == FIRE and rec["Year"] == YEAR
        assert rec["Lat"] == pytest.approx(LAT, abs=1e-3)   # dBASE N12(6)
        assert rec["Lon"] == pytest.approx(LON, abs=1e-3)
        x, y = shp.shape(0).points[0]                       # x/lon, y/lat
        assert (x, y) == pytest.approx((LON, LAT), abs=1e-5)


def test_reference_perimeter_shapefile_is_polygon(run):
    with shapefile.Reader(str(run.out_dir / "reference_perimeter")) as shp:
        assert shp.shapeType == shapefile.POLYGON   # 5
        assert shp.numRecords == 1
        shape0 = shp.shape(0)
        assert len(shape0.parts) >= 1
        assert len(shape0.points) > 1000            # real footprint, not stub
        xmin, ymin, xmax, ymax = shape0.bbox
        assert xmin < LON < xmax and ymin < LAT < ymax  # ignition inside footprint
    for base in ("ignition", "reference_perimeter"):
        prj = (run.out_dir / f"{base}.prj").read_text()
        assert "GEOGCRS" in prj and "WGS 84" in prj   # WKT2 of EPSG:4326


def test_prints_sample_hrrr_command(run):
    out = run.stdout
    assert "python tools/hrrr_to_wxs.py" in out
    assert f"--lat {LAT:.6f} --lon {LON:.6f}" in out   # 34.067783 -118.551123
    assert "--start 2025-01-07T00:00Z" in out
    assert "--end 2025-01-31T00:00Z" in out
    assert "palisades-hrrr.wxs" in out
