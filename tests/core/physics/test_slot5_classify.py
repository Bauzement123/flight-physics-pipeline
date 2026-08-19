"""Unit tests for slot5_evaluator.py (Slot 5: Batch Result Evaluation)."""

from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from pycontrails import Flight

from src.core.physics.slots.slot5_evaluator import (
    _classify_results,
    _extract_actual_fl,
    _extract_ef,
    evaluate,
    get_evaluator,
)
from src.data_manager.schemas import BatchOutput, EvalResult, SimTask, WorkerResult


def _make_task(
    icao24: str = "48455b",
    callsign: str = "KLM123",
    dep: str = "EHAM",
    arr: str = "LFPG",
    firstseen: int = 1672531200,
    lastseen: int = 1672534800,
    typecode: str = "B738",
    cluster_id: int = 1,
    fl: float = 350.0,
) -> SimTask:
    """Create a minimal valid SimTask instance for testing."""
    return SimTask(
        icao24=icao24,
        callsign=callsign,
        dep=dep,
        arr=arr,
        firstseen=firstseen,
        lastseen=lastseen,
        typecode=typecode,
        cluster_id=cluster_id,
        fl=fl,
    )


def _make_mock_flight(
    fid: str,
    ef_value: float = 1.0e9,
    alt_m: float = 10668.0,
) -> MagicMock:
    """Create a minimal mock Flight object for testing."""
    flight = MagicMock(spec=Flight)
    flight.attrs = {"flight_id": fid}
    flight.data = {
        "ef": np.array([ef_value]),
        "altitude": np.array([alt_m]),
    }
    flight.__getitem__.side_effect = lambda key: flight.data[key]
    return flight


def test_classify_results_all_success():
    """Test 1: Classify results when all flights succeed."""
    task1 = _make_task(callsign="KLM101", fl=350.0)
    task2 = _make_task(callsign="KLM102", fl=370.0)
    fl1 = _make_mock_flight(task1.to_sim_fid(), ef_value=1.5e8, alt_m=350.0 * 30.48)
    fl2 = _make_mock_flight(task2.to_sim_fid(), ef_value=2.0e8, alt_m=370.0 * 30.48)
    batch_output = BatchOutput(successful=[(task1, fl1), (task2, fl2)], failed=[])

    succeeded, failed = _classify_results(batch_output, "kerosene")

    assert len(succeeded) == 2
    assert len(failed) == 0
    for r, task in zip(succeeded, [task1, task2]):
        assert isinstance(r, WorkerResult)
        assert r.status == "success"
        assert r.sim_fid == task.to_sim_fid()
        assert r.model_config_id == "kerosene"
        assert isinstance(r.ef, float)
        assert r.ef != 0.0


def test_classify_results_all_failed():
    """Test 2: Classify results when all flights fail."""
    task1 = _make_task(callsign="FAIL1", fl=350.0)
    task2 = _make_task(callsign="FAIL2", fl=370.0)
    batch_output = BatchOutput(
        successful=[],
        failed=[(task1, "psflight_error: kinematic failure"), (task2, "cocip_error: OOM")],
    )

    succeeded, failed = _classify_results(batch_output, "kerosene")

    assert len(succeeded) == 0
    assert len(failed) == 2
    for r, task in zip(failed, [task1, task2]):
        assert isinstance(r, WorkerResult)
        assert r.status == "fail"
        assert r.sim_fid == task.to_sim_fid()
        assert r.model_config_id == "kerosene"
        assert r.ef == 0.0
        assert r.actual_fl is None


def test_classify_results_mixed():
    """Test 3: Classify results with mixed success and failure."""
    task1 = _make_task(callsign="OK1", fl=350.0)
    task2 = _make_task(callsign="FAIL2", fl=370.0)
    fl1 = _make_mock_flight(task1.to_sim_fid(), ef_value=5.0e7, alt_m=350.0 * 30.48)
    batch_output = BatchOutput(
        successful=[(task1, fl1)],
        failed=[(task2, "load_failed: file not found")],
    )

    succeeded, failed = _classify_results(batch_output, "kerosene")

    assert len(succeeded) == 1
    assert len(failed) == 1
    assert succeeded[0].status == "success"
    assert succeeded[0].sim_fid == task1.to_sim_fid()
    assert failed[0].status == "fail"
    assert failed[0].sim_fid == task2.to_sim_fid()


def test_evaluate_standard_still_todo_always_empty():
    """Test 4: Standard mode evaluate always returns empty still_todo."""
    task1 = _make_task(callsign="KLM101", fl=350.0)
    task2 = _make_task(callsign="KLM102", fl=370.0)
    fl1 = _make_mock_flight(task1.to_sim_fid(), ef_value=1.0e8, alt_m=350.0 * 30.48)
    fl2 = _make_mock_flight(task2.to_sim_fid(), ef_value=2.0e8, alt_m=370.0 * 30.48)
    batch_output = BatchOutput(successful=[(task1, fl1), (task2, fl2)], failed=[])

    eval_result = evaluate(batch_output, {}, "standard", "kerosene")

    assert isinstance(eval_result, EvalResult)
    assert eval_result.still_todo == []
    assert len(eval_result.succeeded) == 2
    assert len(eval_result.failed) == 0


def test_evaluate_variational_emits_stepdown_for_positive_ef():
    """Test 5: Variational mode emits a step-down SimTask for positive EF."""
    task = _make_task(callsign="STEP1", fl=350.0)
    fl = _make_mock_flight(task.to_sim_fid(), ef_value=1.0e9, alt_m=350.0 * 30.48)
    batch_output = BatchOutput(successful=[(task, fl)], failed=[])
    task_by_fid = {task.to_sim_fid(): task}

    eval_result = evaluate(
        batch_output,
        task_by_fid,
        "variational",
        "kerosene",
        step_size=10.0,
        min_safe_fl=280.0,
    )

    assert isinstance(eval_result, EvalResult)
    assert len(eval_result.succeeded) == 1
    assert len(eval_result.still_todo) == 1
    assert len(eval_result.failed) == 0
    assert eval_result.still_todo[0].fl == 340.0


def test_evaluate_variational_fl_sanity_check_demotes_to_failed():
    """Test 6: Variational mode demotes result to failed if actual_fl deviates > 1.5 FL."""
    task = _make_task(callsign="DEV1", fl=350.0)
    fl = _make_mock_flight(task.to_sim_fid(), ef_value=1.0e9, alt_m=350.0 * 30.48)
    batch_output = BatchOutput(successful=[(task, fl)], failed=[])
    task_by_fid = {task.to_sim_fid(): task}

    with patch("src.core.physics.slots.slot5_evaluator._extract_actual_fl", return_value=340.0):
        eval_result = evaluate(batch_output, task_by_fid, "variational", "kerosene")

    assert isinstance(eval_result, EvalResult)
    assert len(eval_result.succeeded) == 0
    assert len(eval_result.failed) == 1
    assert eval_result.failed[0].status == "fail"
    assert eval_result.failed[0].sim_fid == task.to_sim_fid()
    assert len(eval_result.still_todo) == 0
