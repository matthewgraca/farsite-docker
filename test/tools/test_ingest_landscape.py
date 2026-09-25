"""Tests for tools/ingest_landscape.py.

Offline unit/end-to-end tests mock `requests` to drive the full
submit->poll->download->finalize pipeline against a fixture GeoTIFF, plus the
AOI/default_layers validation paths. The live LFPS integration test is marked
`integration` (deselect with `pytest -m "not integration"`).
"""

import io
import zipfile
from contextlib import redirect_stderr, redirect_stdout

import numpy as np
import pytest

import ingest_landscape as ing
from hrrr_to_wxs import die

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
VALID_BBOX = "-118.7 33.9 -118.4 34.2"


class FakeResp:
    """Minimal `requests.Response` used by the mocks."""

    def __init__(self, status_code=200, json_data=None, text="", payload=b""):
        self.status_code = status_code
        self._json_data = json_data
        self._text = text
        self._payload = payload

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]

    def close(self):
        pass


def make_fixture_tif(path, nbands=3, crs=5070, res=30.0):
    """Write a small int16 multi-band test GeoTIFF in EPSG `crs` at `res` m."""
    from rasterio.crs import CRS
    from rasterio.transform import from_origin
    profile = {
        "driver": "GTiff", "height": 10, "width": 10, "count": nbands,
        "dtype": "int16", "crs": CRS.from_epsg(crs),
        "transform": from_origin(-118.7, 34.2, res, res),
    }
    data = np.arange(100 * nbands, dtype="int16").reshape(nbands, 10, 10)
    with __import__("rasterio").open(path, "w", **profile) as dst:
        dst.write(data)


def make_bundle_zip(zip_path, job_id, tif_path):
    """Zip a LANDFIRE-style bundle: <jobid>.tif + tfw + aux.xml + region.xml."""
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(tif_path, f"{job_id}.tif")
        z.writestr(f"{job_id}.tfw", "0.0\n0.0\n0.0\n0.0\n0.0\n0.0\n")
        z.writestr(f"{job_id}.tif.aux.xml", "<PAMDataset/>")
        z.writestr("region.xml", "<Region/>")
    return zip_path.read_bytes()


def install_fake_net(monkeypatch, submit_resp, status_resps, zip_bytes):
    """Patch requests.post/get; returns captured submit body list."""
    captured = {}

    def fake_post(url, *a, **kw):
        captured["body"] = kw.get("json")
        captured["calls"] = captured.get("calls", 0) + 1
        return submit_resp

    def fake_get(url, *a, **kw):
        if url.startswith(ing.STATUS_URL):
            return status_resps.pop(0)
        return FakeResp(status_code=200, payload=zip_bytes)

    monkeypatch.setattr(ing.requests, "post", fake_post)
    monkeypatch.setattr(ing.requests, "get", fake_get)
    return captured


# ---------------------------------------------------------------------------
# default_layers / short_name
# ---------------------------------------------------------------------------
def test_default_layers_substitutes_year_tenure_and_fuel_band():
    l = ing.default_layers("2023", "fbfm40")
    # three static base terrain codes stay LF2020_*
    assert l[:3] == ["LF2020_Elev", "LF2020_SlpD", "LF2020_Asp"]
    # band index 3 (0-based) is the fuel band in both fuel models
    assert l[3] == "LF2023_FBFM40"
    assert l[4:] == ["LF2023_CC", "LF2023_CH", "LF2023_CBH", "LF2023_CBD"]
    assert len(l) == 8


def test_default_layers_fbfm13_changes_only_fuel_band():
    l = ing.default_layers("2025", "fbfm13")
    assert l[:3] == ["LF2020_Elev", "LF2020_SlpD", "LF2020_Asp"]
    assert l[3] == "LF2025_FBFM13"          # band 4 = fbfm13
    assert l[4:] == ["LF2025_CC", "LF2025_CH", "LF2025_CBH", "LF2025_CBD"]


def test_default_layers_rejects_unknown_fuel_model():
    with pytest.raises(SystemExit):
        ing.default_layers("2024", "bin")


def test_short_name_strips_year_prefix():
    assert ing.short_name("LF2020_Elev") == "elev"
    assert ing.short_name("LF2024_FBFM40") == "fbfm40"
    assert ing.short_name("LF2025_CBD") == "cbd"


# ---------------------------------------------------------------------------
# AOI + resolution validation
# ---------------------------------------------------------------------------
def test_aoi_requires_exactly_one_of_bbox_or_mapzone(capsys):
    with pytest.raises(SystemExit):
        ing.main(["--email", "a@b.c"])            # neither
    with pytest.raises(SystemExit):
        ing.main(["--bbox", VALID_BBOX, "--mapzone", "5", "--email", "a@b.c"])


def test_bbox_validates_order_and_ranges(capsys):
    with pytest.raises(SystemExit):
        ing.validate_bbox("-118.4 33.9 -118.7 34.2")   # W > E
    with pytest.raises(SystemExit):
        ing.validate_bbox("-118.7 34.2 -118.4 33.9")   # S > N
    with pytest.raises(SystemExit):
        ing.validate_bbox("-300 33.9 -200 34.2")       # lon out of LFPS range
    with pytest.raises(SystemExit):
        ing.validate_bbox("-118.7 0 -118.4 1")         # lat below LFPS coverage
    assert ing.validate_bbox(VALID_BBOX) == [-118.7, 33.9, -118.4, 34.2]


def test_mapzone_validates_list(capsys):
    assert ing.validate_mapzone("5") == "5"
    assert ing.validate_mapzone("99") == "99"
    with pytest.raises(SystemExit):
        ing.validate_mapzone("11")
    with pytest.raises(SystemExit):
        ing.validate_mapzone("not-a-zone")


def test_resolution_below_native_rejected_before_network(monkeypatch, capsys):
    monkeypatch.setattr(ing.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not submit")))
    with pytest.raises(SystemExit):
        ing.main(["--bbox", VALID_BBOX, "--email", "a@b.c", "--resolution", "29"])


# ---------------------------------------------------------------------------
# end-to-end mock run through submit -> poll -> download -> finalize
# ---------------------------------------------------------------------------
def test_main_submits_downloads_finalizes_and_extracts_dem(tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "fixture.tif"
    make_fixture_tif(fixture, nbands=3)
    layers = "LF2020_Elev;LF2020_SlpD;LF2020_Asp"
    job_id = "test-job"
    zip_bytes = make_bundle_zip(tmp_path / "bundle.zip", job_id, fixture)

    captured = install_fake_net(
        monkeypatch,
        submit_resp=FakeResp(status_code=200, json_data={"jobId": job_id}),
        status_resps=[
            FakeResp(status_code=200, json_data={"status": "Executing"}),
            FakeResp(status_code=200, json_data={
                "status": "Succeeded",
                "outputFile": {"File": "http://lfps/out/test-job.zip"},
            }),
        ],
        zip_bytes=zip_bytes,
    )

    out_base = tmp_path / "landscape"
    dem_out = tmp_path / "elevation.tif"
    rc = ing.main([
        "--bbox", VALID_BBOX, "--email", "a@b.c",
        "--layers", layers,
        "--out", str(out_base), "--dem-out", str(dem_out),
        "--max-wait", "20", "--poll-interval", "0",
    ])
    assert rc == 0

    # explicit --layers bypasses version/fuel derivation: body is the 3 codes verbatim
    assert captured["body"]["Layer_List"] == layers
    assert captured["calls"] == 1

    import rasterio
    final = out_base.with_suffix(".tif")
    assert final.is_file()
    with rasterio.open(final) as ds:
        assert ds.count == 3
        assert ds.crs.to_epsg() == 5070
        rx, ry = ds.res
        assert abs(rx - 30.0) < 0.01 and abs(ry - 30.0) < 0.01
        assert list(ds.descriptions) == ["elev", "slp_d", "asp"]

    # --dem-out: single-band int16 elevation extraction
    with rasterio.open(dem_out) as ds:
        assert ds.count == 1
        assert ds.dtypes[0] == "int16"
        assert ds.crs.to_epsg() == 5070


def test_main_default_resolution_30_is_accepted(tmp_path, monkeypatch, capsys):
    # Same end-to-end but omitting --resolution: the 30 m native default must
    # not be rejected (it is the manual-smoke contract).
    fixture = tmp_path / "fixture.tif"
    make_fixture_tif(fixture, nbands=3)
    job_id = "test-job"
    zip_bytes = make_bundle_zip(tmp_path / "bundle.zip", job_id, fixture)
    install_fake_net(
        monkeypatch,
        submit_resp=FakeResp(status_code=200, json_data={"jobId": job_id}),
        status_resps=[FakeResp(status_code=200, json_data={
            "status": "Succeeded",
            "outputFile": "http://lfps/out/test-job.zip"})],
        zip_bytes=zip_bytes,
    )
    out_base = tmp_path / "landscape30"
    rc = ing.main([
        "--bbox", VALID_BBOX, "--email", "a@b.c",
        "--layers", "LF2020_Elev;LF2020_SlpD;LF2020_Asp",
        "--out", str(out_base), "--max-wait", "20",
    ])
    assert rc == 0
    assert out_base.with_suffix(".tif").is_file()


def test_main_overwrites_existing_output(tmp_path, monkeypatch, capsys):
    # Re-running ingest over a prior <out>.tif is an advertised resume path.
    # Path.rename raises FileExistsError on Windows when the target exists, so
    # the extract must be moved with os.replace semantics (Path.replace).
    fixture = tmp_path / "fixture.tif"
    make_fixture_tif(fixture, nbands=3)
    job_id = "test-job"
    zip_bytes = make_bundle_zip(tmp_path / "bundle.zip", job_id, fixture)
    install_fake_net(
        monkeypatch,
        submit_resp=FakeResp(status_code=200, json_data={"jobId": job_id}),
        status_resps=[FakeResp(status_code=200, json_data={
            "status": "Succeeded",
            "outputFile": "http://lfps/out/test-job.zip"})],
        zip_bytes=zip_bytes,
    )
    out_base = tmp_path / "landscape"
    layers = "LF2020_Elev;LF2020_SlpD;LF2020_Asp"
    make_fixture_tif(out_base.with_suffix(".tif"), nbands=1)   # pre-existing stub
    rc = ing.main([
        "--bbox", VALID_BBOX, "--email", "a@b.c",
        "--layers", layers,
        "--out", str(out_base), "--max-wait", "20",
    ])
    assert rc == 0
    # the pipeline output replaced the stub (3-band finalize, not 1-band stub)
    import rasterio
    with rasterio.open(out_base.with_suffix(".tif")) as ds:
        assert ds.count == 3


# ---------------------------------------------------------------------------
# invalid-products robustness: no silent version fallback
# ---------------------------------------------------------------------------
def test_submit_invalid_products_surfaces_message_no_fallback(monkeypatch, capsys):
    captured = {}

    def fake_post(url, *a, **kw):
        captured["calls"] = captured.get("calls", 0) + 1
        captured["body"] = kw.get("json")
        return FakeResp(status_code=400, json_data={"message": "Invalid products: LF9999_FBFM40"})

    monkeypatch.setattr(ing.requests, "post", fake_post)

    with pytest.raises(SystemExit):
        ing.main([
            "--bbox", VALID_BBOX, "--email", "a@b.c",
            "--version", "9999", "--out", str(__import__("tempfile").mkdtemp()),
        ])
    err = capsys.readouterr().err
    assert "Invalid products: LF9999_FBFM40" in err    # API message printed verbatim
    assert captured["calls"] == 1                       # no retry / fallback year
    assert "LF9999_FBFM40" in captured["body"]["Layer_List"]  # requested verbatim


# ---------------------------------------------------------------------------
# live integration (network)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_live_lfps_3band_bbox(tmp_path):
    """A genuinely small LFPS job: 3 terrain bands clipped to a bbox."""
    out_base = tmp_path / "landscape_live"
    rc = ing.main([
        "--bbox", VALID_BBOX, "--email", "you@example.com",
        "--layers", "LF2020_Elev;LF2020_SlpD;LF2020_Asp",
        "--out", str(out_base),
        "--max-wait", "600", "--poll-interval", "5",
    ])
    assert rc == 0
    import rasterio
    final = out_base.with_suffix(".tif")
    assert final.is_file()
    with rasterio.open(final) as ds:
        assert ds.count == 3
        assert ds.crs.to_epsg() == 5070
        rx, _ = ds.res
        assert abs(rx - 30.0) < 0.01
