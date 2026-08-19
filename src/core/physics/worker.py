"""
worker.py — Slot 3+4 integration: Physics Simulation Worker

Executes a batch of SimTasks end-to-end:
  1. Loads each cluster trajectory via get_loader() (Slot 3)
  2. Evaluates PSFlight + CoCiP via get_model() (Slot 4) — vectorized first,
     sequential fallback if vectorized fails
  3. Writes results to Delta Lake via append_sim_lake()
  4. Returns BatchOutput for Slot 5 to evaluate

This function runs inside a ThreadPoolExecutor thread spawned by engine.py.
Threads share the parent process's MetDataset memory, avoiding the OOM issue
that would occur if each worker had to hold its own ERA5 dataset copy.
Thread workers inherit parent log handlers — no setup_file_logger() needed here.
setup_file_logger() is called once in the orchestrator entrypoint only.
"""

import gc
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pycontrails import Flight, Fleet, MetDataset
from pycontrails.models.cocip import Cocip
from pycontrails.models.ps_model import PSFlight

from src.common.config import UNSUPPORTED_TYPECODE_FLAG
from src.common.utils import log_skipped_aircraft
from src.core.physics.loaders import get_loader
from src.core.physics.models.ps_cocip import get_model
from src.data_manager.io_utils import append_sim_lake
from src.data_manager.schemas import BatchOutput, CorridorCluster, SimTask

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
    step_down_method: Optional[str] = None,
    overwrite: bool = False,
) -> BatchOutput:
    """Execute a batch of SimTasks through the physics pipeline.

    Executes in 3 phases:
      1. Phase 1: Load Flight objects via get_loader().
      2. Phase 2: Vectorized PSFlight eval with copy_source=True (sequential fallback on failure).
      3. Phase 3: Vectorized CoCiP eval with copy_source=True (sequential fallback on failure).
      Persists successful trajectories to Delta Lake.

    Parameters
    ----------
    batch : List[SimTask]
        Tasks to simulate — all share the same (route, cluster_id) group key.
    corridors_map : Dict[Tuple[str, int], Any]
        ``(route_key, cluster_id)`` → CorridorCluster(path, fl).
    model_config_id : str
        Fuel/model config flag, e.g. ``'kerosene'`` or ``'kerosene_lowmem'``.
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
    step_down_method : str, optional
        Step-down altitude method for variational mode (e.g. 'cap').
    overwrite : bool, optional
        Overwrite existing partition in Delta Lake (default False).

    Returns
    -------
    BatchOutput
        Contains successful (task, flight) pairs and failed (task, reason_str) pairs.
    """
    ps_model, cocip_model = get_model(model_config_id, met, rad, max_age_hours, copy_source=True)
    loader = get_loader(sim_mode=sim_mode, fuel=fuel, step_down_method=step_down_method)

    # Phase 1: Load
    loaded, load_failed = _load_flights(batch, corridors_map, loader)
    if not loaded:
        return BatchOutput(successful=[], failed=load_failed)

    # Phase 2: PSFlight (vectorized → sequential fallback)
    ps_ok, ps_failed = _eval_psflight(loaded, ps_model, model_config_id)
    if not ps_ok:
        return BatchOutput(successful=[], failed=load_failed + ps_failed)

    # Phase 3: CoCiP (vectorized → sequential fallback)
    cocip_ok, cocip_failed = _eval_cocip(ps_ok, cocip_model, model_config_id)

    # Write successful trajectories to Delta Lake
    _write_to_lake(cocip_ok, model_config_id, fuel, lake_path, overwrite)

    return BatchOutput(
        successful=cocip_ok,
        failed=load_failed + ps_failed + cocip_failed,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_flights(
    batch: List[SimTask],
    corridors_map: Dict[Tuple[str, int], Any],
    loader: Any,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 1: Load Flight objects for each SimTask.

    Returns
    -------
    loaded_pairs : List[Tuple[SimTask, Flight]]
        Tasks that loaded successfully.
    failed_pairs : List[Tuple[SimTask, str]]
        (task, reason_str) for tasks that failed to load.
    """
    loaded: List[Tuple[SimTask, Flight]] = []
    failed: List[Tuple[SimTask, str]] = []
    for task in batch:
        sim_fid = task.to_sim_fid()
        try:
            flight = loader(task, corridors_map)
        except Exception as exc:
            logger.error("Loader raised for %s: %s", sim_fid, exc)
            flight = None
        if flight is None:
            failed.append((task, "load_failed"))
            continue
        loaded.append((task, flight))
    return loaded, failed


def _eval_psflight(
    pairs: List[Tuple[SimTask, Flight]],
    ps_model: Any,
    model_config_id: str,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 2: Run PSFlight on all flights. Vectorized first, per-flight sequential fallback.

    Returns
    -------
    ok_pairs : List[Tuple[SimTask, Flight]]
        Flights that passed PSFlight (with performance columns populated).
    failed_pairs : List[Tuple[SimTask, str]]
        (task, reason_str) for flights that failed PSFlight.
    """
    tasks_list, flights_list = zip(*pairs)

    # Attempt vectorized
    try:
        logger.info("PSFlight vectorized eval: %d flights, model=%s.", len(flights_list), model_config_id)
        fl_ps_out = ps_model.eval(list(flights_list))
        if isinstance(fl_ps_out, Fleet):
            fl_ps_list = fl_ps_out.to_flight_list()
        elif isinstance(fl_ps_out, Flight):
            fl_ps_list = [fl_ps_out]
        else:
            fl_ps_list = list(fl_ps_out)
        # Rebuild pairs using flight_id to match back to tasks
        fid_to_task: Dict[str, SimTask] = {t.to_sim_fid(): t for t in tasks_list}
        ok_pairs: List[Tuple[SimTask, Flight]] = []
        failed_pairs: List[Tuple[SimTask, str]] = []
        returned_fids = set()
        for fl in fl_ps_list:
            fid = fl.attrs.get("flight_id", "UNK")
            returned_fids.add(fid)
            task = fid_to_task.get(fid)
            if task:
                ok_pairs.append((task, fl))
        for task in tasks_list:
            if task.to_sim_fid() not in returned_fids:
                logger.warning("Flight %s missing from vectorized PSFlight output.", task.to_sim_fid())
                failed_pairs.append((task, "psflight_vectorized_missing_from_output"))
        return ok_pairs, failed_pairs
    except Exception as vec_err:
        logger.warning("Vectorized PSFlight failed (%s). Falling back to sequential.", vec_err)

    # Sequential fallback — lazy instantiation of seq_ps
    seq_ps = PSFlight(
        met=ps_model.met,
        params={**{k: v for k, v in ps_model.params.items() if k != "copy_source"}, "copy_source": False},
    )
    return _eval_psflight_sequential(pairs, seq_ps, model_config_id)


def _eval_psflight_sequential(
    pairs: List[Tuple[SimTask, Flight]],
    ps_model: Any,
    model_config_id: str,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Stage fallback: Run PSFlight one flight at a time."""
    ok: List[Tuple[SimTask, Flight]] = []
    failed: List[Tuple[SimTask, str]] = []
    for task, flight in pairs:
        sim_fid = task.to_sim_fid()
        try:
            fl_ps = ps_model.eval(flight)
            ok.append((task, fl_ps))
        except Exception as exc:
            logger.error("Sequential PSFlight eval failed for %s: %s", sim_fid, exc)
            tc = flight.attrs.get("aircraft_type")
            log_skipped_aircraft(
                sim_fid, tc or UNSUPPORTED_TYPECODE_FLAG,
                f"ERROR_FLAG: Sequential PSFlight failed: {exc}",
            )
            failed.append((task, f"psflight_sequential_error: {exc}"))
    return ok, failed


def _eval_cocip(
    pairs: List[Tuple[SimTask, Flight]],
    cocip_model: Any,
    model_config_id: str,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 3: Run CoCiP on PSFlight-evaluated flights. Vectorized first, sequential fallback.

    GC note: del fl_ps + gc.collect() after Fleet eval to release PSFlight source data
    before iterating results.
    """
    if not pairs:
        return [], []

    tasks_list, flights_list = zip(*pairs)
    fid_to_task: Dict[str, SimTask] = {t.to_sim_fid(): t for t in tasks_list}

    # Attempt vectorized Fleet CoCiP
    try:
        logger.info("CoCiP vectorized eval: %d flights, model=%s.", len(flights_list), model_config_id)
        fl_ps = list(flights_list)  # local ref for cleanup
        fl_out = cocip_model.eval(source=fl_ps)
        del fl_ps   # release PSFlight source data immediately
        gc.collect()

        if isinstance(fl_out, Fleet):
            fl_out_list = fl_out.to_flight_list()
        elif isinstance(fl_out, Flight):
            fl_out_list = [fl_out]
        else:
            fl_out_list = list(fl_out)

        ok: List[Tuple[SimTask, Flight]] = []
        failed: List[Tuple[SimTask, str]] = []
        returned_fids = set()
        for fl in fl_out_list:
            fid = fl.attrs.get("flight_id", "UNK")
            returned_fids.add(fid)
            task = fid_to_task.get(fid)
            if task:
                ok.append((task, fl))
        for task in tasks_list:
            if task.to_sim_fid() not in returned_fids:
                logger.warning("Flight %s missing from vectorized CoCiP output.", task.to_sim_fid())
                failed.append((task, "cocip_vectorized_missing_from_output"))
        return ok, failed
    except Exception as cocip_err:
        logger.warning("Vectorized CoCiP failed (%s). Falling back to sequential.", cocip_err)

    # Sequential fallback — lazy instantiation of seq_cocip
    # Pass met/rad from the vectorized model (same references, copy_source=False)
    seq_cocip = Cocip(
        met=cocip_model.met,
        rad=cocip_model.rad,
        params={**{k: v for k, v in cocip_model.params.items() if k != "copy_source"}, "copy_source": False},
    )
    return _eval_cocip_sequential(pairs, seq_cocip, model_config_id)


def _eval_cocip_sequential(
    pairs: List[Tuple[SimTask, Flight]],
    cocip_model: Any,
    model_config_id: str,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Sequential CoCiP fallback — one flight at a time."""
    ok: List[Tuple[SimTask, Flight]] = []
    failed: List[Tuple[SimTask, str]] = []
    for task, flight in pairs:
        sim_fid = task.to_sim_fid()
        try:
            fl_sim = cocip_model.eval(source=flight)
            ok.append((task, fl_sim))
        except Exception as exc:
            logger.error("Sequential CoCiP eval failed for %s: %s", sim_fid, exc)
            tc = flight.attrs.get("aircraft_type")
            log_skipped_aircraft(
                sim_fid, tc or UNSUPPORTED_TYPECODE_FLAG,
                f"ERROR_FLAG: Sequential CoCiP failed: {exc}",
            )
            failed.append((task, f"cocip_sequential_error: {exc}"))
    return ok, failed


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

