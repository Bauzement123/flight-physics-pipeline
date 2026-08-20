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
import dask

# Prevent Dask ThreadPoolExecutor from colliding on underlying C-libraries (NetCDF4/HDF5)
# during concurrent weather chunk reads. Since our parallelism is managed by ProcessPoolExecutor
# distributing flights, evaluating weather chunks synchronously avoids intra-flight race conditions.
dask.config.set(scheduler="synchronous")

from pycontrails import Flight, Fleet, MetDataset
from pycontrails.models.cocip import Cocip  # noqa: F401 — kept for test_worker_helpers patch targets
from pycontrails.models.ps_model import PSFlight  # noqa: F401 — kept for test_worker_helpers patch targets

from src.common.adapters import promote_attrs_to_data
from src.common.config import UNSUPPORTED_TYPECODE_FLAG
from src.common.utils import log_skipped_aircraft
from src.core.physics.loaders import get_loader
from src.core.physics.models.ps_cocip import get_model
from src.data_manager.io_utils import append_sim_lake
from src.data_manager.schemas import BatchOutput, CorridorCluster, SimTask
from src.core.physics.slots.slot5_evaluator import check_load_ok, check_cocip_ok

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
    lake_verbosity: str = "full",
    low_mem: bool = False,
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
    lake_verbosity : str, optional
        Delta Lake storage verbosity: 'full' or 'summary' (default 'full').
    low_mem : bool, optional
        Low memory execution mode (default False). If True, sets copy_source=False
        and executes models sequentially to minimize peak RAM.

    Returns
    -------
    BatchOutput
        Contains successful (task, flight) pairs and failed (task, reason_str) pairs.
    """
    copy_source = not low_mem
    ps_model, cocip_model = get_model(
        model_config_id, met, rad, max_age_hours, copy_source=copy_source
    )
    loader = get_loader(sim_mode=sim_mode, fuel=fuel, step_down_method=step_down_method)

    # Phase 1: Load + Gate
    loaded_ok, load_failed = _load_flights(batch, corridors_map, loader, sim_mode=sim_mode)
    if not loaded_ok:
        return BatchOutput(successful=[], failed=load_failed)

    # Phase 2: PSFlight (vectorized → sequential fallback / lowmem direct sequential)
    ps_ok, ps_failed = _eval_psflight(
        loaded_ok, ps_model, model_config_id, met=met, rad=rad, max_age_hours=max_age_hours, low_mem=low_mem
    )
    if not ps_ok:
        return BatchOutput(successful=[], failed=load_failed + ps_failed)

    # Phase 3: CoCiP (vectorized → sequential fallback / lowmem direct sequential)
    cocip_ok, cocip_failed = _eval_cocip(
        ps_ok, cocip_model, model_config_id, met=met, rad=rad, max_age_hours=max_age_hours, low_mem=low_mem
    )

    # Phase 4: Slot 5 Universal Pre-Lake Gate
    final_ok, lake_gate_failed = check_cocip_ok(cocip_ok, sim_mode=sim_mode)

    # Write successful trajectories to Delta Lake
    _write_to_lake(
        final_ok,
        model_config_id=model_config_id,
        fuel=fuel,
        lake_path=lake_path,
        overwrite=overwrite,
        lake_verbosity=lake_verbosity,
    )

    # Cleanup large memory structures before next batch
    for _, flight in final_ok:
        del flight
    gc.collect()

    return BatchOutput(
        successful=final_ok,
        failed=load_failed + ps_failed + cocip_failed + lake_gate_failed,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_flights(
    batch: List[SimTask],
    corridors_map: Dict[Tuple[str, int], Any],
    loader: Any,
    sim_mode: str = "standard",
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 1: Load and gate Flight objects for each SimTask.

    Returns
    -------
    ok_pairs : List[Tuple[SimTask, Flight]]
        Tasks that loaded and passed Slot 5 load invariants.
    failed_pairs : List[Tuple[SimTask, str]]
        (task, reason_str) for tasks that failed loading or gating.
    """
    raw_pairs: List[Tuple[SimTask, Optional[Flight]]] = []
    failed: List[Tuple[SimTask, str]] = []
    for task in batch:
        sim_fid = task.sim_fid
        try:
            flight = loader(task, corridors_map)
            raw_pairs.append((task, flight))
        except Exception as exc:
            logger.error("Loader raised for %s: %s", sim_fid, exc)
            failed.append((task, "load_failed"))

    ok_pairs, gate_failed = check_load_ok(raw_pairs, sim_mode=sim_mode)
    return ok_pairs, failed + gate_failed


def _eval_psflight(
    pairs: List[Tuple[SimTask, Flight]],
    ps_model: Any,
    model_config_id: str,
    met: Any,
    rad: Any,
    max_age_hours: int,
    low_mem: bool = False,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 2: Run PSFlight on all flights. Vectorized first, per-flight sequential fallback.

    Returns
    -------
    ok_pairs : List[Tuple[SimTask, Flight]]
        Flights that passed PSFlight (with performance columns populated).
    failed_pairs : List[Tuple[SimTask, str]]
        (task, reason_str) for flights that failed PSFlight.
    """
    if low_mem:
        return _eval_psflight_sequential(pairs, ps_model, model_config_id)

    tasks_list, flights_list = zip(*pairs)

    # Attempt vectorized
    try:
        logger.info("PSFlight vectorized eval: %d flights, model=%s.", len(flights_list), model_config_id)
        fl_ps_out = ps_model.eval(list(flights_list))
        fl_ps_list = fl_ps_out.to_flight_list() if isinstance(fl_ps_out, Fleet) else list(fl_ps_out)
        
        # Rebuild pairs using flight_id to match back to tasks
        fid_to_task: Dict[str, SimTask] = {t.sim_fid: t for t in tasks_list}
        ok_pairs: List[Tuple[SimTask, Flight]] = []
        for fl in fl_ps_list:
            fid = fl.attrs.get("flight_id")
            if task := fid_to_task.get(fid):
                ok_pairs.append((task, fl))
                
        return ok_pairs, []
    except Exception as vec_err:
        logger.warning("Vectorized PSFlight failed (%s). Falling back to sequential.", vec_err)

    # Sequential fallback — instantiated via get_model factory (copy_source=False)
    seq_ps, _ = get_model(
        model_config_id=model_config_id,
        met=met,
        rad=rad,
        max_age_hours=max_age_hours,
        copy_source=False,
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
        sim_fid = task.sim_fid
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
    met: Any,
    rad: Any,
    max_age_hours: int,
    low_mem: bool = False,
) -> Tuple[List[Tuple[SimTask, Flight]], List[Tuple[SimTask, str]]]:
    """Phase 3: Run CoCiP on PSFlight-evaluated flights. Vectorized first, sequential fallback.

    GC note: del fl_ps + gc.collect() after Fleet eval to release PSFlight source data
    before iterating results.
    """
    if not pairs:
        return [], []

    if low_mem:
        return _eval_cocip_sequential(pairs, cocip_model, model_config_id)

    tasks_list, flights_list = zip(*pairs)
    fid_to_task: Dict[str, SimTask] = {t.sim_fid: t for t in tasks_list}

    # Attempt vectorized Fleet CoCiP
    try:
        logger.info("CoCiP vectorized eval: %d flights, model=%s.", len(flights_list), model_config_id)
        fl_ps = list(flights_list)  # local ref for cleanup
        fl_out = cocip_model.eval(source=fl_ps)
        del fl_ps   # release PSFlight source data immediately
        gc.collect()

        fl_out_list = fl_out.to_flight_list() if isinstance(fl_out, Fleet) else list(fl_out)
        
        ok: List[Tuple[SimTask, Flight]] = []
        for fl in fl_out_list:
            fid = fl.attrs.get("flight_id")
            if task := fid_to_task.get(fid):
                ok.append((task, fl))
                
        return ok, []
    except Exception as cocip_err:
        logger.warning("Vectorized CoCiP failed (%s). Falling back to sequential.", cocip_err)

    # Sequential fallback — instantiated via get_model factory (copy_source=False)
    _, seq_cocip = get_model(
        model_config_id=model_config_id,
        met=met,
        rad=rad,
        max_age_hours=max_age_hours,
        copy_source=False,
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
        sim_fid = task.sim_fid
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
    lake_verbosity: str = "full",
) -> None:
    """Build trajectory DataFrames via unified Flight.to_dataframe() and append to Delta Lake.

    In both modes, the 14 fixed metadata attributes are injected directly into ``flight.attrs``,
    preserving the full CoCiP and model-specific attribute payload.

    In ``summary`` mode, the flight is reduced to its first waypoint before conversion,
    broadcasting all attributes across the single row with zero loss of flight-level metadata.
    In ``full`` mode, all waypoints are converted.
    """
    if not successful:
        return

    all_dfs: List[pd.DataFrame] = []
    for task, flight in successful:
        time_series = flight.data.get("time", [])
        if len(time_series) > 0:
            firstseen = pd.Timestamp(np.min(time_series))
            lastseen = pd.Timestamp(np.max(time_series))
            if firstseen.tzinfo is not None:
                firstseen = firstseen.tz_localize(None)
            if lastseen.tzinfo is not None:
                lastseen = lastseen.tz_localize(None)
        else:
            firstseen = pd.Timestamp(task.firstseen, unit="s", tz="UTC").tz_localize(None)
            lastseen = pd.Timestamp(task.lastseen, unit="s", tz="UTC").tz_localize(None)

        dep_date = int(firstseen.strftime("%Y%m%d"))
        ef_total = float(np.nansum(flight["ef"])) if "ef" in flight.data else 0.0
        fuel_burn = float(flight.attrs.get("total_fuel_burn", 0.0))

        # Inject 14 fixed metadata attributes into flight.attrs (retaining all existing attrs)
        flight.attrs["SIM_FID"] = task.sim_fid
        flight.attrs["model_config_id"] = model_config_id
        flight.attrs["fuel"] = fuel
        flight.attrs["route"] = f"{task.dep}-{task.arr}"
        flight.attrs["icao24"] = task.icao24
        flight.attrs["callsign"] = task.callsign
        flight.attrs["typecode"] = task.typecode
        flight.attrs["cluster_id"] = np.int32(task.cluster_id)
        flight.attrs["FL"] = float(task.fl)
        flight.attrs["dep_date"] = np.int32(dep_date)
        flight.attrs["firstseen"] = firstseen
        flight.attrs["lastseen"] = lastseen
        flight.attrs["EF_total"] = ef_total
        flight.attrs["total_fuel_burn"] = fuel_burn

        target_fl = (
            Flight(data={k: np.asarray(v)[:1] for k, v in flight.data.items()}, attrs=flight.attrs)
            if lake_verbosity == "summary"
            else flight
        )

        # Promote all scalar attrs into flight.data so to_dataframe() emits them as columns.
        # This covers both the 14 fixed metadata fields and any variable CoCiP / model
        # attrs injected during simulation (e.g. different model_config_ids, SAF flags).
        # Non-scalar attrs (arrays, dicts, None) are silently skipped by promote_attrs_to_data.
        promote_attrs_to_data(target_fl)

        df = target_fl.to_dataframe()
        df.attrs = {}  # prevent pyarrow JSON serialization crash on df.attrs passthrough
        if "waypoint" not in df.columns:
            df["waypoint"] = np.arange(len(df), dtype=np.int32)

        all_dfs.append(df)

    combined = pd.concat(all_dfs, ignore_index=True)
    try:
        with _LAKE_WRITE_LOCK:
            append_sim_lake(lake_path, combined, overwrite=overwrite)
    except Exception as exc:
        logger.error("Failed to append trajectories to Delta Lake: %s", exc)

