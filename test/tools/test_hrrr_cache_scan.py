"""Offline tests for hrrr_to_wxs.scan_cache (once-over cache validation).

A window with several corrupt subsets must report ALL of them in one message
(delete all the listed files, re-run once) instead of Phase B dying one file at
a time.
"""

from datetime import datetime

import hrrr_to_wxs
from hrrr_to_wxs import WANTED, scan_cache

SHORTS = [s for s, _, _ in WANTED]


class FakeDS(dict):
    """Minimal xarray stand-in supporting `short in ds` membership."""
    pass


def write(cache, h, data=b"not-a-grib"):
    (cache / f"hrrr_{h:%Y%m%d%H}.grib2").write_bytes(data)


def hours(*names):
    return [datetime.strptime(n, "%Y%m%d%H") for n in names]


def test_scan_reports_all_corrupt_files_at_once(tmp_path):
    hs = hours("2025010400", "2025010401", "2025010402")
    for h in hs:
        write(tmp_path, h)
    problems, good, _ = scan_cache(hs, tmp_path)
    assert good is None
    assert len(problems) == 3                       # every corrupt hour listed
    for p in problems:
        assert p.startswith(hs[0].strftime("%Y%m%d%H")) or \
            p.startswith(hs[1].strftime("%Y%m%d%H")) or \
            p.startswith(hs[2].strftime("%Y%m%d%H"))
        assert "cannot decode" in p


def test_scan_flags_empty_and_garbage(tmp_path):
    hs = hours("2025010400", "2025010401")
    write(tmp_path, hs[0], b"")
    write(tmp_path, hs[1], b"garbage-bytes")
    problems, good, _ = scan_cache(hs, tmp_path)
    assert good is None
    assert problems[0].endswith("file missing/empty")
    assert "cannot decode" in problems[1]


def test_scan_keeps_first_good_and_lists_later_bads(monkeypatch, tmp_path):
    hs = hours("2025010400", "2025010401", "2025010402")
    for h in hs:
        write(tmp_path, h)

    def fake_open(path):
        if str(path).endswith("2025010400.grib2"):
            return FakeDS({s: None for s in SHORTS})
        raise RuntimeError("corrupt grid")

    monkeypatch.setattr(hrrr_to_wxs, "open_cached", fake_open)
    problems, good, dso = scan_cache(hs, tmp_path)
    assert good == hs[0]
    assert set(dso) == set(SHORTS)
    assert len(problems) == 2
    assert all("cannot decode" in p for p in problems)


def test_scan_flags_missing_variable(monkeypatch, tmp_path):
    hs = hours("2025010400")
    write(tmp_path, hs[0])

    def fake_open(path):
        return FakeDS({SHORTS[0]: None})            # only one of six wanted vars

    monkeypatch.setattr(hrrr_to_wxs, "open_cached", fake_open)
    problems, good, _ = scan_cache(hs, tmp_path)
    assert good is None
    assert len(problems) == 1
    assert ("missing " + ", ".join(SHORTS[1:])) in problems[0]
