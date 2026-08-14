"""
loaders/cluster_loader.py — Slot 3a: K-Cluster Loader (O1 Waterfall 1)

Loads a synthesized K-cluster trajectory from disk, time-shifts all waypoints
to match the target flight's firstseen timestamp, and returns a pycontrails
Flight object ready for physics evaluation.

No altitude modification is performed here. For step-down variants (O2),
use stepdown_loader instead.

Extracted from: clone_simulation.py prepare_cloned_flight() L37-94
                adapters.py read_flights_from_parquet() L100-121
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from pycontrails import Flight, HydrogenFuel, JetA

from src.common.config import is_supported_typecode
from src.common.utils import log_skipped_aircraft
from src.data_manager.schemas import SimTask

logger = logging.getLogger(__name__)

# Metadata columns that live in Flight.attrs, not in the waypoint dataframe.
# Dropping them from the df avoids duplicate-column warnings in pycontrails.
_METADATA_COLS = [
    "flight_id", "icao24", "callsign", "typecode", "fuel",
    "firstseen", "lastseen",
    "estdepartureairport", "estarrivalairport",
    "route_class", "cluster_id",
]


def load(
    task: SimTask,
    corridors_map: Dict[Tuple[str, int], Any],
    use_hydrogen: bool = False,
    cap_altitude: bool = False,
) -> Optional[Flight]:
    """Load and time-shift a K-cluster trajectory for the given SimTask.

    Reads the cluster parquet file identified by ``(route_key, task.cluster_id)``,
    time-shifts all waypoints so the trajectory starts at ``task.firstseen``,
    validates the aircraft typecode, and returns a pycontrails ``Flight``.

    Parameters
    ----------
    task : SimTask
        The simulation task. Uses ``dep``, ``arr``, ``cluster_id``,
        ``firstseen``, ``icao24``, ``callsign``, ``typecode``.
    corridors_map : Dict[Tuple[str, int], Any]
        Mapping of ``(route_key, cluster_id)`` to ``CorridorCluster(path, fl)``.
        ``route_key = f"{task.dep}-{task.arr}"``.
    use_hydrogen : bool, optional
        If True, attach HydrogenFuel() and nvpm_ei_n emission index (default False).
    cap_altitude : bool, optional
        If True, clamp trajectory altitude to task.fl ceiling (default False).

    Returns
    -------
    Optional[Flight]
        Time-shifted pycontrails Flight, or ``None`` if:
        - The cluster path is not in ``corridors_map``.
        - The cluster parquet file is empty.
        - The typecode fails ``is_supported_typecode()`` validation.
    """
    route_key = f"{task.dep}-{task.arr}"
    sim_fid = task.to_sim_fid()

    # --- Resolve cluster file path ---
    corridor_entry = corridors_map.get((route_key, task.cluster_id))
    if corridor_entry is None:
        logger.warning(
            "No cluster file for route=%s cluster_id=%d — skipping %s.",
            route_key, task.cluster_id, sim_fid,
        )
        return None

    corridor_path = corridor_entry.path if hasattr(corridor_entry, "path") else corridor_entry

    # --- Load base trajectory ---
    df_base = _read_cluster_parquet(corridor_path, sim_fid)
    if df_base is None:
        return None

    # --- Validate typecode ---
    if not is_supported_typecode(task.typecode):
        log_skipped_aircraft(
            sim_fid, task.typecode,
            "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode",
        )
        return None

    # --- Time-shift waypoints ---
    df_cloned = _time_shift(df_base, task.firstseen)

    # --- Apply altitude cap if requested ---
    if cap_altitude:
        df_cloned = _apply_altitude_cap(df_cloned, task.fl)

    # --- Build Flight attrs ---
    attrs = {
        "flight_id":            sim_fid,
        "aircraft_type":        task.typecode,
        "icao24":               task.icao24,
        "callsign":             task.callsign,
        "firstseen":            task.firstseen,
        "lastseen":             task.lastseen,
        "estdepartureairport":  task.dep,
        "estarrivalairport":    task.arr,
    }

    # Drop metadata cols from dataframe to avoid duplicate warnings
    df_cloned = df_cloned.drop(
        columns=[c for c in _METADATA_COLS if c in df_cloned.columns],
        errors="ignore",
    )

    fuel_obj = HydrogenFuel() if use_hydrogen else JetA()
    nvpm_kwargs = {"nvpm_ei_n": 2.76e13} if use_hydrogen else {}

    flight = Flight(data=df_cloned, fuel=fuel_obj, drop_duplicated_times=True, crs="EPSG:4326", **attrs, **nvpm_kwargs)
    flight.attrs["fuel"] = "hydrogen" if use_hydrogen else "kerosene"
    return flight


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _apply_altitude_cap(df: pd.DataFrame, fl_ft: float) -> pd.DataFrame:
    """Clamp trajectory altitude column to the given flight level ceiling.

    Current behaviour: hard clip (constant ceiling).
    # TODO: replace with smooth profile (e.g. Gaussian descent towards cap)
    """
    fl_m = fl_ft * 100 * 0.3048
    df = df.copy()
    if "altitude" in df.columns:
        df["altitude"] = df["altitude"].clip(upper=fl_m)
    return df

def _read_cluster_parquet(path: Path, sim_fid: str) -> Optional[pd.DataFrame]:
    """Read cluster parquet and return the first flight's DataFrame, or None."""
    try:
        df = pd.read_parquet(str(path))
    except Exception as exc:
        logger.warning("Failed to read cluster parquet %s: %s", path, exc)
        return None

    if df.empty:
        logger.warning("Empty cluster parquet for %s at %s.", sim_fid, path)
        return None

    # Normalise time column to UTC-aware
    for col in ("time", "timestamp"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
            if df[col].dt.tz is None:
                df[col] = df[col].dt.tz_localize("UTC")
            else:
                df[col] = df[col].dt.tz_convert("UTC")

    # If multiple flight_ids exist (grouped file), take the first one
    if "flight_id" in df.columns:
        first_fid = df["flight_id"].iloc[0]
        df = df[df["flight_id"] == first_fid].copy()

    return df


def _time_shift(df: pd.DataFrame, target_firstseen: int) -> pd.DataFrame:
    """Shift all waypoint timestamps so the trajectory starts at target_firstseen."""
    df = df.copy()
    synth_start = df["time"].iloc[0]
    target_start = pd.Timestamp(target_firstseen, unit="s", tz="UTC")
    offset = target_start - synth_start
    df["time"] = df["time"] + offset
    return df
