"""Unit tests for worker architecture refactoring (B.1 factory fallbacks and B.2 FID contracts)."""

from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest
from pycontrails import Flight

from src.core.physics.worker import (
    _eval_cocip,
    _eval_psflight,
    _load_flights,
)
from src.data_manager.schemas import SimTask


def _make_task(
    icao24: str = "48455b",
    callsign: str = "KLM123",
    dep: str = "EHAM",
    arr: str = "EDDF",
    firstseen: int = 1735689600,
    lastseen: int = 1735693200,
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


def make_flight(fid: str = "EHAM-EDDF_B738_R1_D2025-01-01", attrs: dict = None) -> Flight:
    """Create a minimal pycontrails Flight object with given flight_id or attrs."""
    n = 3
    if attrs is None:
        attrs = {"flight_id": fid}
    return Flight(
        data={
            "longitude": np.array([-5.0, 0.0, 5.0]),
            "latitude": np.array([50.0, 51.0, 52.0]),
            "altitude": np.array([10000.0, 10500.0, 11000.0]),
            "time": pd.date_range("2025-01-01", periods=n, freq="10min"),
        },
        attrs=attrs,
    )


@patch("src.core.physics.worker.get_model")
def test_psflight_sequential_fallback_uses_factory(mock_get_model):
    """Test 1: Vectorized PSFlight failure triggers get_model with copy_source=False."""
    task = _make_task()
    flight = make_flight(fid=task.to_sim_fid())

    ps_model = MagicMock()
    ps_model.eval.side_effect = RuntimeError("vectorized fail")

    seq_ps = MagicMock()
    seq_ps.eval.return_value = flight
    seq_cocip = MagicMock()
    mock_get_model.return_value = (seq_ps, seq_cocip)

    met = MagicMock()
    rad = MagicMock()
    ok_pairs, failed_pairs = _eval_psflight(
        pairs=[(task, flight)],
        ps_model=ps_model,
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
    )

    mock_get_model.assert_called_once_with(
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
        copy_source=False,
    )
    assert len(ok_pairs) == 1
    assert ok_pairs[0] == (task, flight)
    assert len(failed_pairs) == 0


@patch("src.core.physics.worker.get_model")
def test_cocip_sequential_fallback_uses_factory(mock_get_model):
    """Test 2: Vectorized CoCiP failure triggers get_model with copy_source=False."""
    task = _make_task()
    flight = make_flight(fid=task.to_sim_fid())

    cocip_model = MagicMock()
    cocip_model.eval.side_effect = RuntimeError("vectorized fail")

    seq_ps = MagicMock()
    seq_cocip = MagicMock()
    seq_cocip.eval.return_value = flight
    mock_get_model.return_value = (seq_ps, seq_cocip)

    met = MagicMock()
    rad = MagicMock()
    ok_pairs, failed_pairs = _eval_cocip(
        pairs=[(task, flight)],
        cocip_model=cocip_model,
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
    )

    mock_get_model.assert_called_once_with(
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
        copy_source=False,
    )
    assert len(ok_pairs) == 1
    assert ok_pairs[0] == (task, flight)
    assert len(failed_pairs) == 0


@patch("src.core.physics.worker.get_model")
def test_psflight_missing_fid_routes_to_failed(mock_get_model):
    """Test 3: Missing flight_id in PSFlight eval output triggers ValueError and sequential fallback."""
    task = _make_task()
    flight = make_flight(fid=task.to_sim_fid())

    ps_model = MagicMock()
    flight_without_fid = make_flight(attrs={})
    ps_model.eval.side_effect = RuntimeError("Vectorized PSFlight OOM")

    seq_ps = MagicMock()
    seq_ps.eval.side_effect = RuntimeError("sequential failure")
    seq_cocip = MagicMock()
    mock_get_model.return_value = (seq_ps, seq_cocip)

    met = MagicMock()
    rad = MagicMock()
    ok_pairs, failed_pairs = _eval_psflight(
        pairs=[(task, flight)],
        ps_model=ps_model,
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
    )

    mock_get_model.assert_called_once_with(
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
        copy_source=False,
    )
    assert len(ok_pairs) == 0
    assert len(failed_pairs) == 1
    assert failed_pairs[0][0] == task


@patch("src.core.physics.worker.get_model")
def test_cocip_missing_fid_routes_to_failed(mock_get_model):
    """Test 4: Vectorized CoCiP error triggers sequential fallback."""
    task = _make_task()
    flight = make_flight(fid=task.sim_fid)

    cocip_model = MagicMock()
    cocip_model.eval.side_effect = RuntimeError("Vectorized CoCiP OOM")

    seq_ps = MagicMock()
    seq_cocip = MagicMock()
    seq_cocip.eval.side_effect = RuntimeError("sequential failure")
    mock_get_model.return_value = (seq_ps, seq_cocip)

    met = MagicMock()
    rad = MagicMock()
    ok_pairs, failed_pairs = _eval_cocip(
        pairs=[(task, flight)],
        cocip_model=cocip_model,
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
    )

    mock_get_model.assert_called_once_with(
        model_config_id="kerosene",
        met=met,
        rad=rad,
        max_age_hours=10,
        copy_source=False,
    )
    assert len(ok_pairs) == 0
    assert len(failed_pairs) == 1
    assert failed_pairs[0][0] == task


def test_loader_fid_mismatch_fails():
    """Test 5: Loader returning flight with mismatched flight_id marks task as failed."""
    task = _make_task()
    wrong_flight = make_flight(fid="WRONG_FID")
    loader_fn = MagicMock(return_value=wrong_flight)

    loaded, failed = _load_flights(batch=[task], corridors_map={}, loader=loader_fn)

    assert len(loaded) == 0
    assert len(failed) == 1
    assert failed[0] == (task, "loader_fid_mismatch")
