"""Offline unit tests for hrrr_to_wxs.choose_cell (DEM read / coverage logic).

choose_cell must read DEM elevation via rasterio ``index()``/``read()``, never
``DatasetReader.sample()``: in some Windows environments rasterio's sample()
degrades to all-masked/nan for valid in-bounds coordinates (a DLL-shadowed GDAL
breaks that warp path), which would hard-fail the weather stage. A nodata cell
at the nearest HRRR cell must not be fatal - the function rescues to the
nearest valid window cell and dies only when the whole window has no data.
"""

import contextlib
import io

import numpy as np
import pytest
import rasterio
import xarray as xr

from hrrr_to_wxs import choose_cell

ANCHOR = (34.0845, -118.5405)          # matches the synthetic raster center
ELEV_M = 111                            # valid-pixel elevation (m)
ELEV_FT = ELEV_M * 3.28084


def hr_grid(center_lat, center_lon, step=0.05, n=29):
    """Synthetic HRRR lat/lon grid (CF longitudes 0..360) stepping from the
    anchor, so grid cell (0,0) sits on the anchor coordinate."""
    lat = center_lat + step * np.arange(n)
    lon = (center_lon + step * np.arange(n)) % 360
    return xr.Dataset(
        {"latitude": (("y", "x"), np.broadcast_to(lat[:, None], (n, n)).astype(float)),
         "longitude": (("y", "x"), np.broadcast_to(lon[None, :], (n, n)).astype(float))})


def write_dem(tmp_path, *, nodata_patch=True, all_nodata=False, center=ANCHOR):
    """A 31x31 int16 EPSG:5070 raster centered on `center`; every pixel
    ELEV_M except an optional nodata block that firmly covers the probe."""
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    cx, cy = t.transform(center[1], center[0])
    a = np.full((31, 31), ELEV_M, dtype=np.int16)
    if nodata_patch:
        a[13:18, 13:18] = -9999                 # thick block over the target cell
    if all_nodata:
        a[:] = -9999
    prof = dict(driver="GTiff", height=31, width=31, count=1, dtype="int16",
                crs="EPSG:5070",
                transform=rasterio.Affine(30, 0, cx - 15 * 30, 0, -30, cy + 15 * 30),
                nodata=-9999)
    p = tmp_path / "dem.tif"
    with rasterio.open(p, "w", **prof) as r:
        r.write(a, 1)
    return rasterio.open(p)


def run(dem, window_radius=2, step=0.05):
    with contextlib.redirect_stdout(io.StringIO()):
        return choose_cell(hr_grid(*ANCHOR, step=step), *ANCHOR, dem, tol_ft=500,
                           dem_window_radius=window_radius)


def test_choose_cell_reads_via_index_and_read_pins_valid_cell(tmp_path):
    """The nearest HRRR cell (0,0 = anchor) samples valid elevation, so the
    normal path returns a finite RAWS_ELEVATION anchored at the seed."""
    dem = write_dem(tmp_path, nodata_patch=False)
    iy, ix, clat, clon, dem_ft, target_ft = run(dem)
    assert (iy, ix) == (0, 0)                     # anchor cell chosen
    assert np.isclose(dem_ft, ELEV_FT, atol=1.0)  # 111 m -> ft
    assert np.isclose(target_ft, ELEV_FT, atol=1.0)
    assert np.isclose(clon, ANCHOR[1], atol=0.01)


def test_choose_cell_rescues_to_valid_window_cell_when_nearest_is_nodata(tmp_path):
    """A nodata block over the nearest HRRR cell must not die; the ±window
    search picks the nearest valid cell and still yields a finite elevation.
    Step is tiny so neighboring HRRR cells also land inside the small raster."""
    dem = write_dem(tmp_path, nodata_patch=True)
    iy, ix, clat, clon, dem_ft, target_ft = run(dem, step=0.001, window_radius=2)
    assert target_ft is None                      # rescue path taken
    assert np.isfinite(dem_ft)
    assert np.isclose(dem_ft, ELEV_FT, atol=1.0)  # rescued to a 111 m cell
    assert 0 <= iy < 3 and 0 <= ix < 3            # inside the window


def test_choose_cell_dies_when_window_has_no_data(tmp_path, capsys):
    """All-nodata DEM: genuine non-coverage -> the friendly die, not a crash."""
    dem = write_dem(tmp_path, all_nodata=True)
    with pytest.raises(SystemExit) as exc:
        run(dem)
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "does the raster cover the anchor" in (cap.err + cap.out)


def test_choose_cell_never_calls_dataset_sample(tmp_path):
    """Regression guard: rasterio.sample() is broken in some Windows envs and
    must not be (re)introduced into the read path."""
    dem = write_dem(tmp_path, nodata_patch=False)

    def boom(*args, **kwargs):
        raise AssertionError("DatasetReader.sample() must not be called")

    dem.sample = boom
    iy, ix, clat, clon, dem_ft, target_ft = run(dem)
    assert np.isfinite(dem_ft) and np.isclose(target_ft, ELEV_FT, atol=1.0)
