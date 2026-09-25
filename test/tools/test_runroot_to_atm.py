"""Windows-CRLF byte regression for tools/runroot_to_atm.py.

The .atm manifest and every resampled grid are written with open(newline="")
plus explicit \r\n so they are byte-identical on Linux and Windows. A future
regression to text mode would double the CR on Windows only (stray \r\r\n); the
grid header is TAB-separated and the WindNinja-native .atm must start with
"WINDS\r\n". This test pins the exact bytes on every platform.
"""

import shutil
from pathlib import Path

import pytest

from runroot_to_atm import main as atm_main

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

DATA = Path(__file__).resolve().parent.parent / "data"


def test_runroot_atm_and_grids_have_exact_crlf(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    for stamp in ("0000", "0100"):
        for k in ("vel", "ang"):
            for ext in ("asc", "prj"):
                shutil.copy(DATA / "atm" / f"palisades_01-06-2025_{stamp}_56m_{k}.{ext}",
                            run / f"palisades_01-06-2025_{stamp}_56m_{k}.{ext}")
    out_dir = tmp_path / "out"
    rc = atm_main(["--run-root", str(run), "--dem", str(DATA / "palisades.tif"),
                   "--units", "mph", "--out", str(out_dir / "test.atm")])
    assert rc == 0

    b = (out_dir / "test.atm").read_bytes()
    assert b.startswith(b"WINDS\r\n")
    assert b"\r\r\n" not in b

    grid = out_dir / "palisades_01-06-2025_0000_56m_vel.asc"
    assert grid.is_file()
    g = grid.read_bytes()
    assert g.startswith(b"ncols\t663\r\n")   # palisades.tif is 663 wide
    assert b"\r\r\n" not in g
