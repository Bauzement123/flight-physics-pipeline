"""Unit tests for private worker helper functions in worker.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from pycontrails import Flight
from src.core.physics.worker import (
    _load_flights,
    _eval_psflight,
    _eval_psflight_sequential,
    _eval_cocip,
    _eval_cocip_sequential,
    run_batch,
)
from src.data_manager.schemas import BatchOutput, SimTask


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


def _make_mock_flight(sim_fid: str, typecode: str = "B738") -> MagicMock:
    """Create a minimal mock Flight object for testing."""
    flight = MagicMock(spec=Flight)
    flight.attrs = {"flight_id": sim_fid, "aircraft_type": typecode, "total_fuel_burn": 5000.0}
    flight.data = {"ef": np.array([100.0, 200.0]), "altitude": np.array([10668.0, 10668.0])}
    flight.__len__.return_value = 10
    flight.__getitem__.side_effect = lambda k: flight.data[k]
    return flight


def test_load_flights_all_valid():
    """Test 1: All flights load successfully."""
    task1 = _make_task(callsign="KLM101")
    task2 = _make_task(callsign="KLM102")
    batch = [task1, task2]
    corridors_map = {}

    loader = MagicMock()
    loader.side_effect = lambda task, cmap: _make_mock_flight(task.sim_fid, task.typecode)

    loaded_pairs, failed_results = _load_flights(batch, corridors_map, loader)

    assert len(loaded_pairs) == 2
    assert len(failed_results) == 0
    assert loaded_pairs[0][0] == task1
    assert loaded_pairs[1][0] == task2


def test_load_flights_loader_raises():
    """Test 2: Loader raises exception for one task without propagating."""
    task1 = _make_task(callsign="FAIL1")
    task2 = _make_task(callsign="OK2")
    batch = [task1, task2]
    corridors_map = {}

    def loader_side_effect(task, cmap):
        if task.callsign == "FAIL1":
            raise RuntimeError("Trajectory file corrupted")
        return _make_mock_flight(task.sim_fid, task.typecode)

    loader = MagicMock(side_effect=loader_side_effect)

    loaded_pairs, failed_results = _load_flights(batch, corridors_map, loader)

    assert len(loaded_pairs) == 1
    assert len(failed_results) == 1
    assert failed_results[0][0] == task1
    assert "load_failed" in failed_results[0][1]


def test_load_flights_loader_returns_none():
    """Test 3: Loader returns None for one task preserving failure reason."""
    task1 = _make_task(callsign="NONE1")
    task2 = _make_task(callsign="OK2")
    batch = [task1, task2]
    corridors_map = {}

    def loader_side_effect(task, cmap):
        if task.callsign == "NONE1":
            return None
        return _make_mock_flight(task.sim_fid, task.typecode)

    loader = MagicMock(side_effect=loader_side_effect)

    loaded_pairs, failed_results = _load_flights(batch, corridors_map, loader)

    assert len(loaded_pairs) == 1
    assert len(failed_results) == 1
    assert failed_results[0][0] == task1
    assert "load_failed" in failed_results[0][1]


def test_eval_psflight_fleet_succeeds():
    """Vectorized PSFlight succeeds — sequential NOT called, returns raw ok_pairs."""
    task1 = _make_task(callsign="FL01")
    fl1 = _make_mock_flight(task1.sim_fid)
    pairs = [(task1, fl1)]

    ps_model = MagicMock()
    fl_ps_out = _make_mock_flight(task1.sim_fid)  # output flight
    ps_model.eval.return_value = [fl_ps_out]  # returns list of flights

    met_mock, rad_mock = MagicMock(), MagicMock()
    ok_pairs, failed_pairs = _eval_psflight(pairs, ps_model, "kerosene", met_mock, rad_mock, 48)

    ps_model.eval.assert_called_once()
    assert len(ok_pairs) == 1
    assert len(failed_pairs) == 0
    assert ok_pairs[0][0] == task1
    # No WorkerResult in output
    assert not any(hasattr(x, "status") for _, x in ok_pairs)


def test_eval_psflight_fleet_fails_falls_back_to_sequential():
    """Vectorized PSFlight raises — sequential called, failed tasks return (task, str)."""
    task1 = _make_task(callsign="FL01")
    task2 = _make_task(callsign="FL02")
    fl1 = _make_mock_flight(task1.sim_fid)
    fl2 = _make_mock_flight(task2.sim_fid)
    pairs = [(task1, fl1), (task2, fl2)]

    ps_model = MagicMock()
    ps_model.eval.side_effect = RuntimeError("Fleet PSFlight OOM")  # vectorized fails
    ps_model.met = MagicMock()
    ps_model.params = {"copy_source": True}

    seq_ps = MagicMock()
    seq_ps.eval.side_effect = lambda fl: fl  # returns flight unchanged

    with patch("src.core.physics.worker.get_model", return_value=(seq_ps, None)):
        met_mock, rad_mock = MagicMock(), MagicMock()
        ok_pairs, failed_pairs = _eval_psflight(pairs, ps_model, "kerosene", met_mock, rad_mock, 48)

    assert len(ok_pairs) == 2
    assert len(failed_pairs) == 0
    assert all(isinstance(task, type(task1)) for task, _ in ok_pairs)


@patch("src.core.physics.worker.log_skipped_aircraft")
def test_eval_psflight_sequential_filters_kinematic(mock_log_skipped):
    """Sequential PSFlight catches kinematic errors — failed tasks return (task, str)."""
    task1 = _make_task(callsign="FL01")
    task2 = _make_task(callsign="FL02")
    task3 = _make_task(callsign="FL03")
    fl1 = _make_mock_flight(task1.sim_fid)
    fl2 = _make_mock_flight(task2.sim_fid)
    fl3 = _make_mock_flight(task3.sim_fid)
    pairs = [(task1, fl1), (task2, fl2), (task3, fl3)]

    def ps_eval(fl):
        if fl == fl2:
            raise RuntimeError("Unrealistic fuel mass flow")
        return fl

    ps_model = MagicMock()
    ps_model.eval.side_effect = ps_eval

    ok_pairs, failed_pairs = _eval_psflight_sequential(pairs, ps_model, "kerosene")

    assert len(ok_pairs) == 2
    assert len(failed_pairs) == 1
    # failed entry is (task, reason_str) — NOT WorkerResult
    failed_task, failed_reason = failed_pairs[0]
    assert failed_task == task2
    assert isinstance(failed_reason, str)
    assert "psflight_sequential_error" in failed_reason
    mock_log_skipped.assert_called_once()


def test_eval_cocip_fleet_fails_falls_back_to_sequential():
    """CoCiP Fleet fails — falls back to sequential, returns (task, str) for failures."""
    task1 = _make_task(callsign="FL01")
    task2 = _make_task(callsign="FL02")
    fl1 = _make_mock_flight(task1.sim_fid)
    fl2 = _make_mock_flight(task2.sim_fid)
    pairs = [(task1, fl1), (task2, fl2)]

    cocip_model = MagicMock()
    cocip_model.eval.side_effect = RuntimeError("Fleet CoCiP failed")  # first call fails
    cocip_model.met = MagicMock()
    cocip_model.rad = MagicMock()
    cocip_model.params = {"copy_source": True}

    # Patch Cocip constructor for lazy seq_cocip instantiation
    seq_cocip = MagicMock()
    seq_cocip.eval.side_effect = lambda source: _make_mock_flight(source.attrs["flight_id"])

    with patch("src.core.physics.worker.get_model", return_value=(None, seq_cocip)):
        met_mock, rad_mock = MagicMock(), MagicMock()
        ok_pairs, failed_pairs = _eval_cocip(pairs, cocip_model, "kerosene", met_mock, rad_mock, 48)

    assert len(ok_pairs) == 2
    assert len(failed_pairs) == 0
    # No WorkerResult in output
    assert all(isinstance(task, type(task1)) for task, _ in ok_pairs)


def test_run_batch_returns_batch_output_type():
    """run_batch returns BatchOutput with no WorkerResult construction in worker."""
    task = _make_task()
    batch = [task]

    with patch("src.core.physics.worker.get_model") as mock_get_model, \
         patch("src.core.physics.worker.get_loader") as mock_get_loader, \
         patch("src.core.physics.worker._load_flights") as mock_load, \
         patch("src.core.physics.worker._eval_psflight") as mock_ps, \
         patch("src.core.physics.worker._eval_cocip") as mock_cocip, \
         patch("src.core.physics.worker._write_to_lake") as mock_write:

        mock_get_model.return_value = (MagicMock(), MagicMock())
        fl = _make_mock_flight(task.sim_fid)
        mock_load.return_value = ([(task, fl)], [])
        mock_ps.return_value = ([(task, fl)], [])
        mock_cocip.return_value = ([(task, fl)], [])

        result = run_batch(
            batch=batch,
            corridors_map={},
            model_config_id="kerosene",
            sim_mode="standard",
            lake_path=Path("/tmp/fake"),
            met=MagicMock(),
            rad=MagicMock(),
            max_age_hours=48,
        )

    assert isinstance(result, BatchOutput)
    assert result.successful == [(task, fl)]
    assert result.failed == []

