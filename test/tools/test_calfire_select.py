"""Offline tests for calfire_ignition.select_fire (name/year disambiguation).

Name+year can match several FRAP records; --inc (FRAP incident number,
"UNIT INC" or INC) and --index resolve which one the pipeline ingests.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from calfire_ignition import select_fire


def cand(unit, inc, year=2025, name="Ranch", alarm=None, acres=100):
    return {"fire_name": name, "year": year, "alarm_date": alarm,
            "cont_date": None, "gis_acres": acres, "cause": "14",
            "agency": "CDF", "unit_id": unit, "inc_num": inc,
            "irwin_id": None, "geometry": None}


def test_unique_inc_selects_that_fire():
    cands = [cand("LDF", "00000701", alarm=datetime(2025, 1, 1)),
             cand("LDF", "00000702", alarm=datetime(2025, 2, 1)),
             cand("RIV", "00000703", alarm=datetime(2025, 3, 1))]
    got = select_fire(cands, "Ranch", 2025, None, "LDF 00000702")
    assert got["inc_num"] == "00000702"


def test_inc_alone_matches_when_unique():
    cands = [cand("LDF", "00000701"), cand("LDF", "00000702")]
    got = select_fire(cands, "Ranch", 2025, None, "00000701")
    assert got["inc_num"] == "00000701"


def test_inc_tolerates_punctuation_and_case():
    cands = [cand("LDF", "00000701")]
    got = select_fire(cands, "Ranch", 2025, None, "  ldf-00000701  ")
    assert got["inc_num"] == "00000701"


def test_inc_no_match_dies(capsys):
    cands = [cand("LDF", "00000701"), cand("LDF", "00000702")]
    with pytest.raises(SystemExit) as exc:
        select_fire(cands, "Ranch", 2025, None, "LDF 99999999")
    assert exc.value.code == 2
    assert "--inc" in capsys.readouterr().err


def test_ambiguous_still_requires_index(capsys):
    cands = [cand("LDF", "00000701"), cand("LDF", "00000702")]
    with pytest.raises(SystemExit) as exc:
        select_fire(cands, "Ranch", 2025, None, None)
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert "pass --index" in out.err


def test_index_bounds_checked():
    cands = [cand("LDF", "00000701")]
    with pytest.raises(SystemExit):
        select_fire(cands, "Ranch", 2025, 5, None)
