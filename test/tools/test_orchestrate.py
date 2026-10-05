"""Offline tests for tools/orchestrate.py.

No network, no Windows binaries: the pipeline is exercised end-to-end with
reuse (`enable=false` + input overrides) against fixtures committed under
test/data/ (palisades.tif LCP, fire.json, palisades-hrrr.wxs, and the two
01-06-2025 wind-grid pairs), and config errors are asserted to die().

The integration marker covers a real HRRR ingest; deselect with:
    pytest -m "not integration"
"""

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from orchestrate import main, split_command

DATA = Path(__file__).resolve().parent.parent / "data"

# Fixture facts (pinned; a change in test/data/ breaks these loudly):
WXS_ROWS = 265            # non-header, non-RAWS_ lines of palisades-hrrr.wxs
FUEL_MODEL_COUNT = 28     # distinct band-4 models incl. 0 in palisades.tif


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def toml_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)          # double-quoted TOML basic string
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"unsupported TOML value: {v!r}")


def write_config(tmp_path, name, sections):
    """Serialize a config dict to a TOML file under tmp_path."""
    cfg = tmp_path / name
    lines = []
    for sec, vals in sections.items():
        lines.append(f"[{sec}]")
        for k, v in vals.items():
            if v is None:      # omitted key = schema default (None)
                continue
            lines.append(f"{k} = {toml_val(v)}")
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def build_scratch_run(tmp_path, *, n_atm=1, grids=True):
    """A simulated WindNinja run root: 2 terrain-resolved pairs + `n_atm` .atm
    manifests naming exactly those grids (the native write_farsite_atm layout).
    grids=False leaves an empty dir (exercises the write-cfg decision path)."""
    run_root = tmp_path / "windroot"
    run_root.mkdir()
    if grids:
        for h in ("0000", "0100"):
            for k in ("vel", "ang"):
                for ext in ("asc", "prj"):
                    shutil.copy(
                        DATA / "atm" / f"palisades_01-06-2025_{h}_56m_{k}.{ext}",
                        run_root)
    if n_atm > 0:
        content = ["WINDS", "ENGLISH",
                   "1 6 0000 palisades_01-06-2025_0000_56m_vel.asc "
                   "palisades_01-06-2025_0000_56m_ang.asc",
                   "1 6 0100 palisades_01-06-2025_0100_56m_vel.asc "
                   "palisades_01-06-2025_0100_56m_ang.asc"]
        body = ("\r\n".join(content) + "\r\n").encode()
        for i in range(n_atm):
            name = "wn.atm" if n_atm == 1 else f"wn{i}.atm"
            (run_root / name).write_bytes(body)
    return run_root


def offline_config(tmp_path, *, run_root, weather_wxs=DATA / "palisades-hrrr.wxs",
                   farsite_run=False, farsite_enable=True, burn_periods=None,
                   fuel_overrides=None):
    """The reusable offline base config; individual tests tweak the dict."""
    weather = {"enable": False, "threads": 8, "elevation_tol_ft": 500,
               "cache_dir": None}
    if weather_wxs is not None:
        weather["wxs"] = str(weather_wxs)
    return {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(DATA / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z", "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0,
                       "burn_periods": burn_periods or []},
        "windninja": {"enable": False, "run_root": str(run_root)},
        "weather": weather,
        "farsite": {"enable": farsite_enable, "run": farsite_run,
                    "fuel_moistures": fuel_overrides or {}},
        "output": {"run_dir": str(tmp_path / "out")},
    }


def run_main(argv):
    """Run orchestrate.main() capturing stdout; returns (rc, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(argv)
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# offline end-to-end assembly
# ---------------------------------------------------------------------------
def test_offline_assembly_end_to_end(tmp_path):
    run_root = build_scratch_run(tmp_path)
    cfg = write_config(tmp_path, "offline.toml",
                       offline_config(tmp_path, run_root=run_root,
                                      burn_periods=[["2025-01-06T18:00Z",
                                                     "2025-01-06T22:00Z"]],
                                      fuel_overrides={122: "1 2 3 4 5 6"}))
    rc, out = run_main(["--config", str(cfg)])
    assert rc == 0

    out_dir = tmp_path / "out"
    inputs = out_dir / "palisades-FarsiteInputs.txt"
    cmdfile = out_dir / "palisades-FarsiteCmd.txt"

    # step 7 integrity check ran on the native .atm
    assert "atm verified: 2 rows, 4 grids OK" in out
    # step 3 extracted the band-1 DEM from the reused LCP
    assert (out_dir / "palisades-dem.tif").is_file()

    text = inputs.read_text()
    # window: 2025-01-06 08:00/10:00Z - 8h standard -> local 1 6 0000/0200
    assert "FARSITE_START_TIME: 1 6 0000" in text
    assert "FARSITE_END_TIME: 1 6 0200" in text
    # native atm manifest path
    assert f"FARSITE_ATM_FILE: {(run_root / 'wn.atm').resolve()}" in text
    # RAWS block from the reused wxs
    assert f"RAWS: {WXS_ROWS}" in text
    assert "2025 1 1 1600 63 62 0.00 3 184 0" in text   # first wxs row, verbatim
    # fuel-moisture block: distinct band-4 models (+0), override wins
    assert f"FUEL_MOISTURES_DATA: {FUEL_MODEL_COUNT}" in text
    assert "0 6 7 8 60 90 16" in text
    assert "122 1 2 3 4 5 6" in text                     # [farsite.fuel_moistures]
    # UTC burn period (18:00-22:00Z, std -8h) -> local per-day "M D HHMM HHMM"
    assert "FARSITE_BURN_PERIODS: 1" in text
    assert "1 6 1000 1400" in text

    # command file: single line, 6 fields, 2nd field = abs inputs path
    line = cmdfile.read_text().strip().split()
    assert len(line) == 6
    assert line[1] == str(inputs.resolve())


def test_burn_period_utc_to_local_splits_across_local_midnight(tmp_path):
    """A UTC burn window spanning a local midnight becomes one FARSITE per-day
    'M D HHMM HHMM' entry per local day."""
    run_root = build_scratch_run(tmp_path)
    cfg = write_config(tmp_path, "midnight.toml",
                       offline_config(tmp_path, run_root=run_root,
                                      burn_periods=[["2025-01-07T06:00Z",
                                                     "2025-01-07T10:00Z"]]))
    rc, _ = run_main(["--config", str(cfg)])
    assert rc == 0
    text = (tmp_path / "out" / "palisades-FarsiteInputs.txt").read_text()
    # 06:00Z..10:00Z with std -8h == local Jan 6 22:00 .. Jan 7 02:00
    assert "FARSITE_BURN_PERIODS: 2" in text
    assert "1 6 2200 2400" in text
    assert "1 7 0000 0200" in text


def test_farsite_disabled_skips_assembly_and_run(tmp_path):
    """farsite.enable=false: assembly and runfarsite are skipped; upstream
    reuse legs still run, and no -FarsiteInputs/-FarsiteCmd files are written
    (the resumed hand-drive path keeps prior files untouched)."""
    run_root = build_scratch_run(tmp_path)
    cfg = write_config(tmp_path, "nofar.toml",
                       offline_config(tmp_path, run_root=run_root,
                                      farsite_enable=False, farsite_run=True))
    rc, out = run_main(["--config", str(cfg)])
    assert rc == 0
    out_dir = tmp_path / "out"
    # upstream leg still ran (reused overrides are executed)
    assert (out_dir / "palisades-dem.tif").is_file()
    assert "atm verified: 2 rows, 4 grids OK" in out
    # farsite leg skipped: no assembly, and run=true + empty command never dies
    assert not (out_dir / "palisades-FarsiteInputs.txt").exists()
    assert not (out_dir / "palisades-FarsiteCmd.txt").exists()
    assert "farsite disabled" in out


def test_windninja_mesh_m_override_injects_mesh_resolution(tmp_path):
    """[windninja] mesh_m lets WN solve on a different grid than the LCP; the
    native .atm flows to FARSITE, which regrids it internally - no resample
    stage, and the LCP cell size stays the default when mesh_m is unset."""
    run_root = build_scratch_run(tmp_path, n_atm=0, grids=False)  # no pairs -> write cfg
    base = {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(DATA / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z", "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": True, "run_root": str(run_root),
                      "options": {"initialization_method": "wxModelInitialization",
                                  "wx_model_type": "PASTCAST-GCP-HRRR-CONUS-3-KM"}},
        "weather": {"enable": False, "wxs": str(DATA / "palisades-hrrr.wxs")},
        "farsite": {"enable": False, "run": False},
        "output": {"run_dir": str(tmp_path / "out")},
    }
    for label, mesh_m in (("coarse", 120.0), ("finer-than-lcp", 10.0)):
        cfg = dict(base)
        cfg["windninja"] = dict(base["windninja"], mesh_m=mesh_m)
        rc, out = run_main(["--config", str(write_config(
            tmp_path, f"mesh{label}.toml", cfg)), "--dry-run"])
        assert rc == 0
        assert f"mesh_resolution = {mesh_m:g}" in out
        # trust contract: no resample stage is planned for a coarse wind
        assert "winds-resampled" not in out and "runroot_to_atm" not in out
    # default (mesh_m unset) still anchors on the LCP cell size
    rc, out = run_main(["--config", str(write_config(
        tmp_path, "meshdefault.toml", base)), "--dry-run"])
    assert rc == 0
    assert "mesh_resolution = 30" in out


# ---------------------------------------------------------------------------
# config.example.toml dry-run: plan printed, NOTHING touched
# ---------------------------------------------------------------------------
def test_example_config_dry_run_prints_plan_and_touches_nothing():
    run_dir = Path(__file__).resolve().parent.parent.parent / \
        "FireBehaviorModels/SampleData/2025_palisades"
    before = {p.relative_to(run_dir) for p in run_dir.rglob("*")} \
        if run_dir.is_dir() else set()

    rc, out = run_main(["--config",
                        str(Path(__file__).resolve().parent.parent.parent /
                            "config.example.toml"), "--dry-run"])
    assert rc == 0

    # example LCP is 30 m -> injected mesh_resolution == 30
    for needle in ("mesh_resolution = 30", "units_mesh_resolution = m",
                   "write_ascii_output = true", "write_farsite_atm = true",
                   "elevation_file = ", "output_path = ",
                   "time_zone = America/Los_Angeles",
                   "initialization_method = wxModelInitialization",
                   "--lead-days 3",
                   "palisades-FarsiteInputs.txt", "palisades-FarsiteCmd.txt"):
        assert needle in out, needle

    # nothing written to disk
    after = {p.relative_to(run_dir) for p in run_dir.rglob("*")} \
        if run_dir.is_dir() else set()
    assert before == after
    assert not (run_dir / "windroot").exists()


# ---------------------------------------------------------------------------
# negative checks (each via main() + pytest.raises(SystemExit))
# ---------------------------------------------------------------------------
def test_unknown_config_key_dies(tmp_path):
    cfg = write_config(tmp_path, "bad.toml",
                       {"fire": {"nam": "Palisades"}})
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])


def test_missing_initialization_method_dies(tmp_path, capsys):
    run_root = build_scratch_run(tmp_path, n_atm=0, grids=False)  # no pairs ->
    # windninja stage reaches the write-cfg decision, then dies on the missing
    # initialization_method rather than reusing/verifying anything
    cfg = write_config(tmp_path, "noini.toml", {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(DATA / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z", "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": True, "run_root": str(run_root),
                      "options": {"diurnal_winds": "true"}},
        "output": {"run_dir": str(tmp_path / "out")},
    })
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])
    assert "initialization_method" in capsys.readouterr().err


def test_missing_simulation_end_with_no_fire_cont_dies(tmp_path, capsys):
    # a fire.json whose cont_date is null
    fj = json.loads((DATA / "fire.json").read_text())
    fj["cont_date"] = None
    no_cont = tmp_path / "no-cont.json"
    no_cont.write_text(json.dumps(fj))
    cfg = write_config(tmp_path, "noend.toml", {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(no_cont)},
        "simulation": {"start": "2025-01-06T08:00Z", "end": None,
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "output": {"run_dir": str(tmp_path / "out")},
    })
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])
    assert "missing simulation end" in capsys.readouterr().err


def test_farsite_run_requires_command_dies(tmp_path):
    run_root = build_scratch_run(tmp_path)
    cfg = write_config(tmp_path, "norun.toml",
                       offline_config(tmp_path, run_root=run_root,
                                      farsite_run=True))
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])


def test_weather_disabled_without_wxs_dies(tmp_path, capsys):
    run_root = build_scratch_run(tmp_path)      # valid pairs + .atm -> reaches
    cfg = write_config(tmp_path, "nowxs.toml", {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(DATA / "fire.json")},
        "simulation": {"start": "2025-01-06T08:00Z", "end": "2025-01-06T10:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0},
        "windninja": {"enable": False, "run_root": str(run_root)},
        "weather": {"enable": False, "wxs": None},
        "farsite": {"enable": True, "run": False},
        "output": {"run_dir": str(tmp_path / "out")},
    })
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])
    assert "weather.enable=false requires [weather] wxs" in capsys.readouterr().err


def test_no_atm_dies_at_stage7(tmp_path, capsys):
    run_root = build_scratch_run(tmp_path, n_atm=0)   # pairs but no .atm
    cfg = write_config(tmp_path, "noatm.toml",
                       offline_config(tmp_path, run_root=run_root))
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])
    assert "no .atm produced by WindNinja under" in capsys.readouterr().err


def test_ambiguous_multiple_atm_dies_at_stage7(tmp_path, capsys):
    run_root = build_scratch_run(tmp_path, n_atm=2)   # two .atm manifests
    cfg = write_config(tmp_path, "twoatm.toml",
                       offline_config(tmp_path, run_root=run_root))
    with pytest.raises(SystemExit):
        main(["--config", str(cfg)])
    assert "ambiguous: multiple .atm under" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# [windninja]/[farsite] command splitting (Windows quoting)
# ---------------------------------------------------------------------------
def test_split_command_handles_windows_paths():
    assert split_command("wine C:/x/runfarsite.exe") == ["wine", "C:/x/runfarsite.exe"]
    assert split_command(r'"C:\Program Files\WindNinja\WindNinja_cli.exe"') \
        == [r"C:\Program Files\WindNinja\WindNinja_cli.exe"]
    assert split_command(r"C:\repo\runfarsite.exe") == [r"C:\repo\runfarsite.exe"]


# ---------------------------------------------------------------------------
# optional live integration (real HRRR ingest; not run by default)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_integration_live_hrrr_assembly(tmp_path):
    """Full assembly with a real HRRR ingest for the cached single hour.

    Requires the hrrr cache file FireBehaviorModels/.cache/hrrr/
    hrrr_2025010700.grib2 (per test/test_hrrr_to_wxs.py). The run_root is
    seeded with one wind pair on the LOCAL stamp of that cached hour
    (2025-01-07T00:00Z == Jan 6 16:00 America/Los_Angeles), so the hrrr
    wind-coverage gate passes. Skipped unless selected with -m integration.
    """
    cached = (Path(__file__).resolve().parent.parent.parent /
              "FireBehaviorModels/.cache/hrrr/hrrr_2025010700.grib2")
    if not cached.is_file():
        pytest.skip("no cached hrrr_2025010700.grib2 under "
                    "FireBehaviorModels/.cache/hrrr")
    run_root = build_scratch_run(tmp_path, n_atm=0, grids=False)
    # local stamp of UTC 2025-01-07T00:00Z in America/Los_Angeles (std -8h)
    for k in ("vel", "ang"):
        for ext in ("asc", "prj"):
            shutil.copy(DATA / "atm" / f"palisades_01-06-2025_0000_56m_{k}.{ext}",
                        run_root / f"palisades_01-06-2025_1600_56m_{k}.{ext}")
    (run_root / "wn.atm").write_bytes((
        "WINDS\r\nENGLISH\r\n1 6 1600 palisades_01-06-2025_1600_56m_vel.asc "
        "palisades_01-06-2025_1600_56m_ang.asc\r\n").encode())

    out_dir = tmp_path / "out"
    cfg = write_config(tmp_path, "integration.toml", {
        "landscape": {"enable": False, "lcp": str(DATA / "palisades.tif")},
        "fire": {"enable": False, "fire_json": str(DATA / "fire.json")},
        "simulation": {"start": "2025-01-07T00:00Z", "end": "2025-01-07T00:00Z",
                       "row_timezone": "America/Los_Angeles", "lead_days": 0,
                       "burn_periods": []},
        "windninja": {"enable": False, "run_root": str(run_root)},
        "weather": {"enable": True, "cache_dir": str(cached.parent),
                    "threads": 1, "elevation_tol_ft": 500, "wxs": None},
        "farsite": {"enable": True, "run": False},
        "output": {"run_dir": str(out_dir)},
    })
    rc, out = run_main(["--config", str(cfg)])
    assert rc == 0
    inputs = out_dir / "palisades-FarsiteInputs.txt"
    text = inputs.read_text()
    assert "FARSITE_START_TIME: 1 6 1600" in text       # 00:00Z - 8h standard
    assert "RAWS: 1" in text                             # single-hour window
    shutil.rmtree(out_dir, ignore_errors=True)


def test_native_spawn_env_adds_repo_data_dirs(monkeypatch):
    """Native children (runfarsite/WindNinja) must get the repo's bundled
    GDAL/PROJ data dirs in their env - which the Python stages deliberately
    neutralize in-process because a stray PROJ_LIB/PROJ_DATA breaks conda
    pyproj transforms into (inf, inf)."""
    import orchestrate as o

    captured = {}
    def fake_run(argv, cwd=None, env=None):
        captured["env"], captured["argv"] = env, argv
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(o.subprocess, "run", fake_run)

    runner = o.Runner({}, dry=False)
    runner.run_cmd("farsite-run", ["runfarsite.exe", "inputs.txt"], native=True)
    assert captured["env"] is not None
    proj_dir = o.REPO_ROOT / "FireBehaviorModels" / "bin" / "share" / "proj"
    assert Path(captured["env"]["PROJ_LIB"]) == proj_dir
    assert Path(captured["env"]["PROJ_DATA"]) == proj_dir
    assert Path(captured["env"]["GDAL_DATA"]) == \
        o.REPO_ROOT / "FireBehaviorModels" / "bin" / "share" / "gdal-data"

    runner.run_cmd("fire", [str(o.REPO_ROOT / "tools" / "calfire_ignition.py")])
    assert captured["env"] is None      # python children inherit the clean env

