"""Unit and integration tests for lake write verbosity (full vs summary)."""

from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from pycontrails import Flight

from src.core.physics.cli import parse_args
from src.core.physics.worker import _write_to_lake
from src.data_manager.io_utils import (
    read_sim_lake_metadata,
    read_existing_sim_fids,
    read_ef_by_base_key,
)
from src.data_manager.schemas import SimTask, SIM_LAKE_FIXED_COLUMNS


def _make_task(
    icao24: str = "48455b",
    callsign: str = "KLM123",
    dep: str = "EHAM",
    arr: str = "LFPG",
    firstseen: int = 1735689600,
    lastseen: int = 1735693200,
    typecode: str = "B738",
    cluster_id: int = 1,
    fl: float = 350.0,
) -> SimTask:
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


def _make_flight(task: SimTask, n_waypoints: int = 5) -> Flight:
    times = pd.date_range("2025-01-01 00:00:00", periods=n_waypoints, freq="10s")
    df = pd.DataFrame({
        "time": times,
        "latitude": np.linspace(52.3, 49.0, n_waypoints),
        "longitude": np.linspace(4.76, 2.55, n_waypoints),
        "altitude": np.full(n_waypoints, 10668.0),
        "ef": np.full(n_waypoints, 1e12),
        "fuel_flow": np.full(n_waypoints, 0.7),
    })
    flight = Flight(df)
    flight.attrs["flight_id"] = task.to_sim_fid()
    flight.attrs["aircraft_type"] = task.typecode
    flight.attrs["total_fuel_burn"] = 1500.0
    return flight


def test_cli_lake_verbosity_argument():
    args_default = parse_args(["--start-date", "2025-01-01", "--end-date", "2025-01-01", "--out-dir", "out"])
    assert args_default.lake_verbosity == "full"

    args_summary = parse_args(["--start-date", "2025-01-01", "--end-date", "2025-01-01", "--out-dir", "out", "--lake-verbosity", "summary"])
    assert args_summary.lake_verbosity == "summary"


def test_write_to_lake_summary_mode(tmp_path: Path):
    lake_path = tmp_path / "delta_sim_summary"
    task = _make_task()
    flight = _make_flight(task, n_waypoints=10)

    _write_to_lake(
        successful=[(task, flight)],
        model_config_id="kerosene",
        fuel="kerosene",
        lake_path=lake_path,
        overwrite=False,
        lake_verbosity="summary",
    )

    # Verify flight.attrs populated
    for col in SIM_LAKE_FIXED_COLUMNS:
        assert col in flight.attrs

    # Read back metadata using Acero reader
    df = read_sim_lake_metadata(lake_path, [task], columns=["SIM_FID", "FL", "EF_total"])
    assert len(df) == 1
    assert df["SIM_FID"].iloc[0] == task.to_sim_fid()
    assert df["FL"].iloc[0] == 350.0
    assert df["EF_total"].iloc[0] == 10 * 1e12

    # Verify skip-gate frozenset
    existing_fids = read_existing_sim_fids(lake_path, [task])
    assert task.to_sim_fid() in existing_fids

    # Verify variational EF lookup
    ef_map = read_ef_by_base_key(lake_path, [task])
    cluster_fid = task.to_sim_fid().rsplit("_", 1)[0]
    assert cluster_fid in ef_map
    assert ef_map[cluster_fid] == [(350.0, 10 * 1e12)]


def test_write_to_lake_summary_mode_retains_arbitrary_model_attrs(tmp_path: Path):
    """Verify that arbitrary CoCiP/model attributes in flight.attrs are retained in summary Delta Lake."""
    from deltalake import DeltaTable
    lake_path = tmp_path / "delta_sim_summary_attrs"
    task = _make_task()
    flight = _make_flight(task, n_waypoints=10)
    flight.attrs["custom_cocip_metric"] = 123.456
    flight.attrs["cocip_model_version"] = "v2.1"

    _write_to_lake(
        successful=[(task, flight)],
        model_config_id="kerosene",
        fuel="kerosene",
        lake_path=lake_path,
        overwrite=False,
        lake_verbosity="summary",
    )

    dt = DeltaTable(str(lake_path))
    df = dt.to_pandas()
    assert len(df) == 1
    assert "custom_cocip_metric" in df.columns
    assert df["custom_cocip_metric"].iloc[0] == 123.456
    assert df["cocip_model_version"].iloc[0] == "v2.1"


def test_write_to_lake_full_mode(tmp_path: Path):
    lake_path = tmp_path / "delta_sim_full"
    task = _make_task()
    flight = _make_flight(task, n_waypoints=10)

    _write_to_lake(
        successful=[(task, flight)],
        model_config_id="kerosene",
        fuel="kerosene",
        lake_path=lake_path,
        overwrite=False,
        lake_verbosity="full",
    )

    df = read_sim_lake_metadata(lake_path, [task], columns=["SIM_FID", "waypoint"])
    assert len(df) == 1
    assert df["waypoint"].iloc[0] == 0

