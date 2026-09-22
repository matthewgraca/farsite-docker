"""Integration tests for tools/hrrr_to_wxs.py.

Both tests download one real hour of HRRR sfc analysis from NOAA's public
archive and run the full pipeline via main(). Deselect with:
    pytest -m "not integration"
"""

import contextlib
import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hrrr_to_wxs import WANTED, open_cached, main

pytestmark = pytest.mark.integration

HOUR = "2025-01-07T00:00"     # single whole UTC hour; date requested as 1-07-2025
DEM = (Path(__file__).resolve().parent.parent / "data" / "palisades.tif")
CACHE_GRIB = "hrrr_2025010700.grib2"
RAWS_ELEV = 1332              # verified real value; matches SampleData sample wxs
WANTED_SHORTS = {s for s, _, _ in WANTED}
HEADER = "Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov"


@pytest.fixture(scope="module")
def wxs_run(tmp_path_factory):
    """Run the real pipeline once for the single hour; delete all artifacts."""
    assert DEM.is_file(), f"missing test DEM fixture: {DEM}"
    run_dir = tmp_path_factory.mktemp("hrrr_wxs_run")
    cache_dir = run_dir / "cache"
    wxs = run_dir / "weather.wxs"
    argv = [
        "--start", HOUR, "--end", HOUR,
        "--row-timezone", "UTC",          # pins row labels to UTC: no tz math
        "--dem", str(DEM),                # anchor inferred: band-1 valid centroid
        "--cache-dir", str(cache_dir),
        "--out", str(wxs),
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main(argv)                   # die() raises SystemExit -> test fails
    assert rc == 0
    cache_file = cache_dir / CACHE_GRIB
    yield SimpleNamespace(cache_file=cache_file, wxs=wxs, cache_dir=cache_dir)
    # after both module tests: delete the downloaded grib AND the generated .wxs
    # (the committed test/data/palisades.tif fixture is left untouched)
    shutil.rmtree(run_dir, ignore_errors=True)


def _data_rows(wxs):
    return [ln for ln in wxs.read_text().splitlines()
            if ln and not ln.startswith(("RAWS_", "Year"))]


def test_ingest_single_hour_has_expected_vars_and_date(wxs_run):
    """Downloaded subset decodes to the 2025-01-07 hour with all 6 wanted vars."""
    assert wxs_run.cache_file.is_file() and wxs_run.cache_file.stat().st_size > 0

    ds = open_cached(wxs_run.cache_file)          # same decoder main() uses
    assert WANTED_SHORTS <= set(ds.data_vars)     # t2m r2 tp tcc u10 v10
    times = np.atleast_1d(np.asarray(ds["time"].values))
    assert times.size == 1                            # single hour subset
    assert str(times[0]).startswith("2025-01-07T00:00")

    rows = _data_rows(wxs_run.wxs)
    assert len(rows) == 1                          # single hour -> single row
    cols = rows[0].split()
    assert cols[:4] == ["2025", "1", "7", "0000"]  # UTC row clock echoes input
    assert len(cols) == 10
    assert all(np.isfinite(float(v)) for v in cols[4:])  # the 6 variable columns


def test_wxs_file_has_correct_format(wxs_run):
    """Generated .wxs matches the FARSITE weather-stream layout."""
    lines = wxs_run.wxs.read_text().splitlines()
    assert lines[0] == "RAWS_UNITS: ENGLISH"
    assert lines[1] == f"RAWS_ELEVATION: {RAWS_ELEV}"   # real Palisades elevation
    assert lines[2] == HEADER
    rows = _data_rows(wxs_run.wxs)
    assert len(rows) == 1
    cols = rows[0].split()
    assert len(cols) == 10
    temp, rh = int(cols[4]), int(cols[5])
    pcp = float(cols[6])
    wspd, wdir, cloud = int(cols[7]), int(cols[8]), int(cols[9])
    assert 0 <= temp <= 140                           # deg F envelope
    assert 0 <= rh <= 99
    assert pcp >= 0.0
    assert wspd >= 0
    assert 0 <= wdir <= 359
    assert 0 <= cloud <= 100
