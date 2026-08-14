"""
worker.py — Slot 3+4 integration: Physics Simulation Worker

Executes a batch of SimTasks end-to-end:
  1. Loads each cluster trajectory via get_loader() (Slot 3)
  2. Evaluates PSFlight + CoCiP via get_model() (Slot 4) — vectorized first,
     sequential fallback if vectorized fails
  3. Writes results to Delta Lake via append_sim_lake()
  4. Returns List[WorkerResult] for Slot 5 to evaluate

This function runs inside a ThreadPoolExecutor thread spawned by engine.py.
Threads share the parent process's MetDataset memory, avoiding the OOM issue
that would occur if each worker had to hold its own ERA5 dataset copy.
Thread workers inherit parent log handlers — no setup_file_logger() needed here.
setup_file_logger() is called once in the orchestrator entrypoint only.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pycontrails import Flight, Fleet, MetDataset

from src.common.config import UNSUPPORTED_TYPECODE_FLAG, is_supported_typecode
from src.common.utils import log_skipped_aircraft
from src.core.physics.loaders import get_loader
from src.core.physics.models.ps_cocip import get_model
from src.data_manager.io_utils import append_sim_lake
from src.data_manager.schemas import CorridorCluster, SimTask, WorkerResult

logger = logging.getLogger(__name__)

# Delta Lake does not support concurrent writers. This lock serializes
# append_sim_lake() calls across ThreadPoolExecutor threads. It is only
# held for the duration of the write (~1-5 s), never during physics compute.
_LAKE_WRITE_LOCK = threading.Lock()


def run_batch(
    batch: List[SimTask],
    corridors_map: Dict[Tuple[str, int], Any],
    model_config_id: str,
    sim_mode: str,
    lake_path: Path,
    met: MetDataset,
    rad: MetDataset,
    max_age_hours: int,
    fuel: str = "kerosene",
    cap_altitude: bool = False,
    low_mem: bool = False,
    overwrite: bool = False,
) -> List[WorkerResult]:
    """Execute a batch of SimTasks through the physics pipeline.

    Parameters
    ----------
    batch : List[SimTask]
        Tasks to simulate — all share the same (route, cluster_id) group key.
    corridors_map : Dict[Tuple[str, int], CorridorCluster]
        ``(route_key, cluster_id)`` → CorridorCluster(path, fl).
    model_config_id : str
        Fuel/model config flag, e.g. ``'kerosene'``.
    sim_mode : str
        Execution mode flag: ``'standard'`` or ``'variational'``.
    lake_path : Path
        Delta Lake directory to append results to.
    met : MetDataset
        Pressure-level ERA5 dataset for this day's window.
    rad : MetDataset
        Surface-level ERA5 dataset for this day's window.
    max_age_hours : int
        Maximum contrail age in hours.
    fuel : str, optional
        Fuel type for trajectory preparation (default 'kerosene').
    cap_altitude : bool, optional
        Clamp trajectory altitude ceiling to task.fl (default False).
    low_mem : bool, optional
        Enable CoCiP low-memory preprocessing (default False).

    Returns
    -------
    List[WorkerResult]
        One entry per task. status='success' or status='fail'.
        EF=0.0 for failed tasks.
    """
    # Thread workers inherit parent log handlers — no setup_file_logger() needed.
    # (setup_file_logger is called once in orchestrator.__main__ block only.)
    loader     = get_loader(sim_mode=sim_mode, fuel=fuel, cap_altitude=cap_altitude)
    ps_model, cocip_model = get_model(model_config_id, met, rad, max_age_hours, low_mem)

    # ------------------------------------------------------------------ #
    # Phase 1: Load flights — collect valid (task, Flight) pairs           #
    # ------------------------------------------------------------------ #
    results: List[WorkerResult] = []
    loaded:  List[Tuple[SimTask, Flight]] = []

    for task in batch:
        sim_fid = task.to_sim_fid()
        try:
            flight = loader(task, corridors_map)
        except Exception as exc:
            logger.error("Loader raised for %s: %s", sim_fid, exc)
            flight = None

        if flight is None:
            results.append(WorkerResult(
                sim_fid=sim_fid, ef=0.0, fl=task.fl,
                model_config_id=model_config_id, status="fail",
            ))
            continue
        loaded.append((task, flight))

    if not loaded:
        return results

    tasks_list, flights_list = zip(*loaded)
    fid_to_task: Dict[str, SimTask] = {t.to_sim_fid(): t for t in tasks_list}

    # ------------------------------------------------------------------ #
    # Phase 2: Vectorized eval → sequential fallback                       #
    # ------------------------------------------------------------------ #
    eval_results: List[WorkerResult] = []
    successful_pairs: List[Tuple[SimTask, Flight]] = []

    try:
        logger.info(
            "Vectorized eval: %d flights, model=%s, sim_mode=%s.",
            len(flights_list), model_config_id, sim_mode,
        )
        fl_ps  = ps_model.eval(list(flights_list))
        fl_out = cocip_model.eval(source=fl_ps)

        if isinstance(fl_out, Fleet):
            fl_out = fl_out.to_flight_list()
        elif isinstance(fl_out, Flight):
            fl_out = [fl_out]

        for fl in fl_out:
            sim_fid = fl.attrs.get("flight_id", "UNK")
            task    = fid_to_task.get(sim_fid)
            ef      = _extract_ef(fl)
            eval_results.append(WorkerResult(
                sim_fid=sim_fid,
                ef=ef,
                fl=task.fl if task else 0.0,
                model_config_id=model_config_id,
                status="success",
            ))
            if task:
                successful_pairs.append((task, fl))

        # Any loaded flight that didn't appear in output → mark failed
        returned_fids = {r.sim_fid for r in eval_results}
        for task in tasks_list:
            if task.to_sim_fid() not in returned_fids:
                logger.warning("Flight %s missing from vectorized output.", task.to_sim_fid())
                eval_results.append(WorkerResult(
                    sim_fid=task.to_sim_fid(), ef=0.0, fl=task.fl,
                    model_config_id=model_config_id, status="fail",
                ))

    except Exception as vec_err:
        logger.warning(
            "Vectorized eval failed (%s). Falling back to sequential.", vec_err,
        )
        for task, flight in zip(tasks_list, flights_list):
            sim_fid = task.to_sim_fid()
            try:
                fl_ps  = ps_model.eval(flight)
                fl_sim = cocip_model.eval(source=fl_ps)
                ef     = _extract_ef(fl_sim)
                eval_results.append(WorkerResult(
                    sim_fid=sim_fid, ef=ef, fl=task.fl,
                    model_config_id=model_config_id, status="success",
                ))
                successful_pairs.append((task, fl_sim))
            except Exception as seq_err:
                logger.error("Sequential eval failed for %s: %s", sim_fid, seq_err)
                tc = flight.attrs.get("aircraft_type")
                log_skipped_aircraft(
                    sim_fid, tc or UNSUPPORTED_TYPECODE_FLAG,
                    f"ERROR_FLAG: Sequential simulation failed: {seq_err}",
                )
                eval_results.append(WorkerResult(
                    sim_fid=sim_fid, ef=0.0, fl=task.fl,
                    model_config_id=model_config_id, status="fail",
                ))

    results.extend(eval_results)

    # ------------------------------------------------------------------ #
    # Phase 3: Persist trajectories to Delta Lake                          #
    # ------------------------------------------------------------------ #
    _write_to_lake(successful_pairs, model_config_id, fuel, lake_path, overwrite)
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_ef(flight: Flight) -> float:
    """Extract total Energy Forcing (J) from a simulated Flight."""
    if "ef" not in flight.data:
        return 0.0
    return float(np.nansum(flight["ef"]))


def _write_to_lake(
    successful: List[Tuple[SimTask, Flight]],
    model_config_id: str,
    fuel: str,
    lake_path: Path,
    overwrite: bool = False,
) -> None:
    """Build the trajectory DataFrames with metadata and append to the Delta Lake."""
    if not successful:
        return

    all_dfs: List[pd.DataFrame] = []
    for task, flight in successful:
        df = flight.to_dataframe()
        firstseen = df["time"].min()
        lastseen = df["time"].max()
        dep_date = int(firstseen.strftime("%Y%m%d"))
        ef_total = float(np.nansum(flight["ef"])) if "ef" in flight.data else 0.0
        fuel_burn = float(flight.attrs.get("total_fuel_burn", 0.0))

        # Inject 14 fixed metadata columns
        df["SIM_FID"] = task.to_sim_fid()
        df["model_config_id"] = model_config_id
        df["fuel"] = fuel
        df["route"] = f"{task.dep}-{task.arr}"
        df["icao24"] = task.icao24
        df["callsign"] = task.callsign
        df["typecode"] = task.typecode
        df["cluster_id"] = task.cluster_id
        df["FL"] = task.fl
        df["dep_date"] = dep_date
        df["firstseen"] = firstseen
        df["lastseen"] = lastseen
        df["EF_total"] = ef_total
        df["total_fuel_burn"] = fuel_burn

        all_dfs.append(df)

    combined = pd.concat(all_dfs, ignore_index=True)
    try:
        with _LAKE_WRITE_LOCK:
            append_sim_lake(lake_path, combined, overwrite=overwrite)
    except Exception as exc:
        logger.error("Failed to append trajectories to Delta Lake: %s", exc)
