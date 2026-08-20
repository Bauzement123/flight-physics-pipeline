"""
test_section_c_optimizations.py — Comprehensive validation of Section C refactor

Covers:
1. SimTask.sim_fid cached string representation and consistency.
2. Vectorized check_load_ok gate (standard vs variational mode step-down validation).
3. Universal check_cocip_ok pre-lake gate (UNK flight_ids, missing ef, ef_all_nan, empty flights).
4. Low-memory mode (_lowmem) direct sequential bypass.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from pycontrails import Flight

from src.core.physics.slots.slot5_evaluator import check_load_ok, check_cocip_ok
from src.core.physics.worker import (
    run_batch,
    _eval_psflight,
    _eval_cocip,
    _eval_psflight_sequential,
    _eval_cocip_sequential,
)
from src.data_manager.schemas import BatchOutput, SimTask


def _make_task(callsign: str = "KLM123", fl: float = 350.0) -> SimTask:
    return SimTask(
        icao24="48455b",
        callsign=callsign,
        dep="EHAM",
        arr="LFPG",
        firstseen=1672531200,
        lastseen=1672534800,
        typecode="B738",
        cluster_id=1,
        fl=fl,
    )


def _make_mock_flight(sim_fid: str, alt_fl: float = 350.0, ef_val: float = 100.0) -> MagicMock:
    flight = MagicMock(spec=Flight)
    flight.attrs = {"flight_id": sim_fid, "aircraft_type": "B738"}
    alt_m = alt_fl * 100.0 * 0.3048
    flight.data = {
        "ef": np.array([ef_val, ef_val]),
        "altitude": np.array([alt_m, alt_m]),
    }
    flight.__len__.return_value = 10
    flight.__getitem__.side_effect = lambda k: flight.data[k]
    return flight


# ---------------------------------------------------------------------------
# 1. SimTask sim_fid Tests
# ---------------------------------------------------------------------------

def test_sim_task_sim_fid_initialization():
    """Verify sim_fid is pre-computed in __post_init__ without regex re-runs."""
    task = _make_task(callsign="dlh-456", fl=370.0)
    expected_fid = "48455b_DLH456_EHAM-LFPG_20230101_0000_1_370"
    assert task.sim_fid == expected_fid
    assert task.to_sim_fid() == expected_fid


# ---------------------------------------------------------------------------
# 2. Vectorized check_load_ok Tests
# ---------------------------------------------------------------------------

def test_check_load_ok_standard_mode():
    """Standard mode only performs universal structural validation."""
    task1 = _make_task(callsign="OK1", fl=350.0)
    task2 = _make_task(callsign="OK2", fl=320.0)
    fl1 = _make_mock_flight(task1.sim_fid, alt_fl=350.0)
    fl2 = _make_mock_flight(task2.sim_fid, alt_fl=300.0)  # FL differs, but allowed in standard

    pairs = [(task1, fl1), (task2, fl2)]
    ok, failed = check_load_ok(pairs, sim_mode="standard")

    assert len(ok) == 2
    assert len(failed) == 0


def test_check_load_ok_variational_mode_vectorized_stepdown():
    """Variational mode applies vectorized numpy validation on altitude step-downs."""
    task_ok = _make_task(callsign="PASS1", fl=300.0)
    task_fail = _make_task(callsign="FAIL1", fl=300.0)

    # PASS1 is at FL300 (diff <= 1.5)
    fl_pass = _make_mock_flight(task_ok.sim_fid, alt_fl=300.5)
    # FAIL1 is at FL350 (diff > 1.5)
    fl_fail = _make_mock_flight(task_fail.sim_fid, alt_fl=350.0)

    pairs = [(task_ok, fl_pass), (task_fail, fl_fail)]
    ok, failed = check_load_ok(pairs, sim_mode="variational")

    assert len(ok) == 1
    assert ok[0][0] == task_ok
    assert len(failed) == 1
    assert failed[0] == (task_fail, "step_down_failed")


# ---------------------------------------------------------------------------
# 3. Universal check_cocip_ok Tests
# ---------------------------------------------------------------------------

def test_check_cocip_ok_filters_invalid_flights():
    """check_cocip_ok quarantines UNK, missing EF, NaN EF, and empty flights."""
    task = _make_task()

    # Valid flight
    fl_valid = _make_mock_flight(task.sim_fid, ef_val=500.0)

    # Invalid flight: UNK fid
    fl_unk = _make_mock_flight("UNK", ef_val=500.0)

    # Invalid flight: missing ef
    fl_no_ef = MagicMock(spec=Flight)
    fl_no_ef.attrs = {"flight_id": task.sim_fid}
    fl_no_ef.data = {}
    fl_no_ef.__len__.return_value = 10

    # Invalid flight: all NaN ef
    fl_nan_ef = MagicMock(spec=Flight)
    fl_nan_ef.attrs = {"flight_id": task.sim_fid}
    fl_nan_ef.data = {"ef": np.array([np.nan, np.nan])}
    fl_nan_ef.__len__.return_value = 10
    fl_nan_ef.__getitem__.side_effect = lambda k: fl_nan_ef.data[k]

    pairs = [
        (task, fl_valid),
        (task, fl_unk),
        (task, fl_no_ef),
        (task, fl_nan_ef),
    ]

    ok, failed = check_cocip_ok(pairs)
    assert len(ok) == 1
    assert ok[0][1] == fl_valid
    assert len(failed) == 3
    reasons = [reason for _, reason in failed]
    assert "invalid_flight_id_unk" in reasons
    assert "missing_ef_column" in reasons
    assert "ef_all_nan" in reasons


# ---------------------------------------------------------------------------
# 4. Low-Memory Direct Sequential Bypass Tests
# ---------------------------------------------------------------------------

def test_eval_psflight_and_cocip_lowmem_bypasses_vectorization():
    """low_mem=True execution flag must call sequential eval directly regardless of model_config_id."""
    task = _make_task()
    fl = _make_mock_flight(task.sim_fid)

    ps_model = MagicMock()
    ps_model.eval.return_value = fl
    cocip_model = MagicMock()
    cocip_model.eval.return_value = fl

    with patch("src.core.physics.worker._eval_psflight_sequential", wraps=_eval_psflight_sequential) as mock_seq_ps, \
         patch("src.core.physics.worker._eval_cocip_sequential", wraps=_eval_cocip_sequential) as mock_seq_cocip:

        ok_ps, fail_ps = _eval_psflight([(task, fl)], ps_model, "kerosene", None, None, 48, low_mem=True)
        assert len(ok_ps) == 1
        assert len(fail_ps) == 0
        mock_seq_ps.assert_called_once()

        ok_cocip, fail_cocip = _eval_cocip([(task, fl)], cocip_model, "kerosene", None, None, 48, low_mem=True)
        assert len(ok_cocip) == 1
        assert len(fail_cocip) == 0
        mock_seq_cocip.assert_called_once()


def test_eval_kerosene_lowmem_model_can_run_vectorized():
    """model_config_id='kerosene_lowmem' with low_mem=False can still run vectorized."""
    task = _make_task()
    fl = _make_mock_flight(task.sim_fid)

    ps_model = MagicMock()
    ps_model.eval.return_value = [fl]
    cocip_model = MagicMock()
    cocip_model.eval.return_value = [fl]

    with patch("src.core.physics.worker._eval_psflight_sequential", wraps=_eval_psflight_sequential) as mock_seq_ps, \
         patch("src.core.physics.worker._eval_cocip_sequential", wraps=_eval_cocip_sequential) as mock_seq_cocip:

        ok_ps, fail_ps = _eval_psflight([(task, fl)], ps_model, "kerosene_lowmem", None, None, 48, low_mem=False)
        assert len(ok_ps) == 1
        assert len(fail_ps) == 0
        mock_seq_ps.assert_not_called()

        ok_cocip, fail_cocip = _eval_cocip([(task, fl)], cocip_model, "kerosene_lowmem", None, None, 48, low_mem=False)
        assert len(ok_cocip) == 1
        assert len(fail_cocip) == 0
        mock_seq_cocip.assert_not_called()
