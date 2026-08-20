"""
orchestrator.py — Physics Simulation Orchestrator

Main day-by-day coordination loop for the flight physics pipeline.

Weather loading strategy
------------------------
Rather than a fixed 3-day calendar window, the ERA5 time range is computed
from the actual task list for each day:

    era5_start = floor_h(min(t.firstseen)) - 1h
    era5_end   = ceil_h(max(t.lastseen))   + max_age_hours + 1h

This means short-haul days load fewer ERA5 hours while long-haul days still
get the full forward coverage they need.  A per-hour MetDataset cache with
lazy eviction re-uses hours that overlap between consecutive days so no data
is ever loaded twice.

Slot ordering per day
---------------------
1  enumerate_cohort()    — build SimTask list from cohort
2  filter_and_batch()    — skip-gate (Delta Lake) + group into batches
   engine.run_parallel() — ThreadPoolExecutor dispatch → yields WorkerResult
5  evaluate()            — partition results; standard still_todo always []
   vacuum_sim_lake()     — clean up stale Delta Lake files
"""

import gc
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import xarray as xr
from pycontrails import DiskCacheStore, MetDataset
from pycontrails.datalib.ecmwf import ERA5

from src.common.config import (
    EUR_BBOX,
    ERA5_GRID,
    ERA5_PRESSURE_LEVEL_VARIABLES,
    ERA5_REQUIRED_PRESSURE_LEVELS,
    ERA5_SURFACE_VARIABLES,
    MIN_SAFE_FL,
    WEATHER_IO_WORKERS,
    WEATHER_OPEN_PACKET_HOURS,
    WEATHER_PADDING,
    ALL_TARGET_FAMILIES,
)
from src.core.physics.engine import run_parallel
from src.core.physics.slots.slot1_flightlist_gen import build_corridors_map, generate_flightlist
from src.core.physics.slots.slot2_batcher import filter_and_batch, partition_tasks
from src.core.physics.slots.slot5_evaluator import evaluate
from src.core.physics.worker import run_batch
from src.data_manager.io_utils import optimize_sim_lake, read_master_flights, vacuum_sim_lake
from src.data_manager.schemas import CorridorCluster, MasterFlightQuery, SimTask

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private weather helpers (adapted verbatim from clone_simulation.py)
# ---------------------------------------------------------------------------

def _floor_h(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.floor("h")


def _ceil_h(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.ceil("h")


def _format_duration(seconds: float) -> str:
    """Format duration in seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s ({seconds:.1f}s)"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s ({seconds:.1f}s)"


def _open_crop_and_load(era5_obj: ERA5, bbox: list, low_mem: bool) -> MetDataset:
    """Open, spatially crop, and optionally eager-load one ERA5 dataset. Thread-safe.

    Always applies spatial bounding box cropping via native ``downselect()`` to bound array
    extents in memory/graphs. In low-mem mode, skips the eager ``.load()`` so arrays remain lazy.
    """
    met_ds = era5_obj.open_metdataset(dataset_kwargs={"engine": "h5netcdf"})
    west, south, east, north = bbox
    padded = (
        max(-180.0, west - WEATHER_PADDING),
        max(-90.0, south - WEATHER_PADDING),
        min(180.0, east + WEATHER_PADDING),
        min(90.0, north + WEATHER_PADDING),
    )
    met_ds = met_ds.downselect(padded)
    if not low_mem:
        met_ds.data.load()
    return met_ds


def _load_hour_packet(
    hours: List[pd.Timestamp],
    weather_cache_dir: Path,
    bbox: list,
    low_mem: bool,
) -> Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]]:
    """Load a bounded packet of ERA5 hours sequentially. Never mutates shared state.

    Each hour is opened as an independent single-file ERA5 object. PL and SL
    are opened sequentially within the packet — the safer choice for the VM/SMB
    environment where low_mem is used and C-library state is fragile.
    The executor queues packets and runs at most WEATHER_IO_WORKERS concurrently;
    on VM (workers=1) this collapses to a fully sequential single-threaded loop.
    Returns an isolated dict safe to merge into hour_cache on the main thread.
    """
    result: Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]] = {}
    for h in hours:
        h_str = h.strftime("%Y-%m-%dT%H:%M:%S")
        pl_path = weather_cache_dir / f"{h.strftime('%Y%m%d-%H')}-era5pl0.5reanalysis.nc"
        sl_path = weather_cache_dir / f"{h.strftime('%Y%m%d-%H')}-era5sl0.5reanalysis.nc"
        try:
            if pl_path.exists() and sl_path.exists():
                era5_pl = ERA5(
                    time=(h_str, h_str),
                    paths=[str(pl_path)],
                    variables=ERA5_PRESSURE_LEVEL_VARIABLES,
                    pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
                    grid=ERA5_GRID,
                )
                era5_sl = ERA5(
                    time=(h_str, h_str),
                    paths=[str(sl_path)],
                    variables=ERA5_SURFACE_VARIABLES,
                    pressure_levels=-1,
                    grid=ERA5_GRID,
                )
            else:
                disk_cache = DiskCacheStore(cache_dir=str(weather_cache_dir))
                era5_pl = ERA5(
                    time=(h_str, h_str),
                    variables=ERA5_PRESSURE_LEVEL_VARIABLES,
                    pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
                    grid=ERA5_GRID,
                    cachestore=disk_cache,
                )
                era5_sl = ERA5(
                    time=(h_str, h_str),
                    variables=ERA5_SURFACE_VARIABLES,
                    pressure_levels=-1,
                    grid=ERA5_GRID,
                    cachestore=disk_cache,
                )
            met = _open_crop_and_load(era5_pl, bbox, low_mem)
            rad = _open_crop_and_load(era5_sl, bbox, low_mem)
            result[h] = (met, rad)
        except Exception as exc:
            logger.warning("ERA5 load failed for hour %s: %s", h, exc)
    return result


def _populate_hour_cache(
    needed: pd.DatetimeIndex,
    hour_cache: Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]],
    weather_cache_dir: Path,
    bbox: list,
    low_mem: bool,
) -> None:
    """Load missing hours into hour_cache using bounded packets dispatched to a thread pool.

    Missing hours are partitioned into packets of WEATHER_OPEN_PACKET_HOURS.
    Packets are submitted to ThreadPoolExecutor(max_workers=WEATHER_IO_WORKERS).
    Each worker builds an isolated dict; the main thread merges results.
    On VM (WEATHER_IO_WORKERS=1, WEATHER_OPEN_PACKET_HOURS=1): fully sequential,
    one file open at a time.
    """
    missing = sorted(h for h in needed if h not in hour_cache)
    if not missing:
        logger.debug("All %d ERA5 hours already cached.", len(needed))
        return

    P = WEATHER_OPEN_PACKET_HOURS
    packets = [missing[i : i + P] for i in range(0, len(missing), P)]

    logger.info(
        "Loading %d missing ERA5 hour(s) in %d packet(s) of <=%d h "
        "(workers=%d, low_mem=%s).",
        len(missing), len(packets), P, WEATHER_IO_WORKERS, low_mem,
    )
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=WEATHER_IO_WORKERS) as pool:
        futures = {
            pool.submit(_load_hour_packet, pkt, weather_cache_dir, bbox, low_mem): pkt
            for pkt in packets
        }
        for future in as_completed(futures):
            try:
                hour_cache.update(future.result())
            except Exception as exc:
                logger.error("ERA5 packet failed: %s", exc)

    loaded = sum(1 for h in missing if h in hour_cache)
    logger.info(
        "ERA5 cache populated: %d/%d hour(s) loaded in %s.",
        loaded, len(missing), _format_duration(time.perf_counter() - t0),
    )


# ---------------------------------------------------------------------------
# Public orchestrator entry point
# ---------------------------------------------------------------------------

def run(
    date_range: List[date],
    sim_mode: str,
    model_config_id: str,
    lake_path: Path,
    weather_cache_dir: Path,
    ranks: Optional[List[int]] = None,
    corridors_dir: Optional[Path] = None,
    fuel: str = "kerosene",
    step_down_method: Optional[str] = None,
    max_age_hours: int = 48,
    max_workers: int = 4,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
    low_mem: bool = False,
    cluster_selection: str = "random",
    clusters_per_flight: int = 1,
    min_distance_km: float = 0.0,
    overwrite: bool = False,
    batch_size: int = 50,
    bbox: list = EUR_BBOX,
    lake_verbosity: str = "full",
) -> None:
    """Run the physics pipeline for a list of calendar days.

    Parameters
    ----------
    date_range : List[date]
        Ordered list of calendar days to process (inclusive, no duplicates).
    sim_mode : str
        ``'standard'`` (baseline) or ``'variational'`` (step-down second pass).
    model_config_id : str
        Fuel/model config flag passed to ``get_model()``, e.g. ``'kerosene'``.
    lake_path : Path
        Delta Lake root directory for simulation results.
    weather_cache_dir : Path
        Directory containing hourly ERA5 ``.nc`` cache files.
    ranks : List[int], optional
        List of corridor ranks to simulate (default None = all in registry).
    corridors_dir : Path, optional
        Custom directory for corridor trajectory parquets.
    fuel : str
        Fuel type attached to Flight in Slot 3 (default 'kerosene').
    step_down_method : str, optional
        Step-down altitude method in Slot 3 for variational mode (e.g. 'cap').
    max_age_hours : int
        Maximum contrail age in hours (default 48).
    max_workers : int
        Thread pool size for ``run_parallel`` (default 4).
    step_size : float
        FL step-down increment in feet for variational mode (default 1000.0).
    min_safe_fl : float
        Minimum FL below which step-down is halted (default 280.0).
    low_mem : bool
        Low-memory execution mode (default False). Skips eager ERA5 .load()
        (arrays stay file-backed until accessed) and runs physics models
        sequentially with copy_source=False, cutting peak RAM by ~60%.
    clusters_per_flight : int
        Number of cluster trajectories to generate per flight (default 1).
    min_distance_km : float
        Pre-filter: skip routes shorter than this distance in km (default 0).
    overwrite : bool
        If True, bypass the Delta Lake skip-gate in Slot 2 and re-simulate
        all tasks regardless of prior results (default False).
    batch_size : int
        Max flights per parallel batch.
    bbox : list
        ``[west, south, east, north]`` spatial crop applied to ERA5 data.
    lake_verbosity : str
        Storage verbosity for Delta Lake: ``'full'`` (all waypoints) or ``'summary'`` (1-row summary).
    """
    # ── Slot 1: Build corridor cluster mapping from model registry ───── #
    corridors_map = build_corridors_map(
        ranks=ranks,
        corridors_dir=corridors_dir,
        min_distance_km=min_distance_km,
    )
    if not corridors_map:
        logger.error(
            "No corridor files found for ranks=%s, min_distance_km=%.1f — nothing to do.",
            ranks, min_distance_km,
        )
        return

    allowed_routes = list({route_id for (route_id, _) in corridors_map.keys()})
    logger.info("Orchestrator: target route(s) across clusters: %s", allowed_routes)

    # Campaign-wide metrics tracking (wall-to-wall)
    run_t0 = time.perf_counter()
    total_cohort_tasks = 0
    total_committed_tasks = 0
    total_baseline_tasks = 0
    total_stepdowns_emitted = 0
    total_failed_tasks = 0
    total_skipped_tasks = 0

    # Per-hour ERA5 cache: pd.Timestamp (UTC, hourly) → (met_h, rad_h)
    hour_cache: Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]] = {}

    for day_idx, day in enumerate(date_range):
        day_t0 = time.perf_counter()
        logger.info("=== Day %s  (%d / %d) ===", day, day_idx + 1, len(date_range))

        # ── Slot 1: read cohort with exact route predicate pushdown ─────── #
        day_start = pd.Timestamp(day, tz="UTC")
        day_end   = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
        query     = MasterFlightQuery(
            dep_date_start=day_start,
            dep_date_end=day_end,
            routes=allowed_routes,
            typecodes=ALL_TARGET_FAMILIES,
        )

        try:
            cohort_df = read_master_flights(query)
        except FileNotFoundError:
            logger.warning("master_flights not found — skipping day %s.", day)
            continue

        if cohort_df.empty:
            logger.info("No flights for day %s — skipping.", day)
            continue

        tasks: List[SimTask] = generate_flightlist(
            cohort_df=cohort_df,
            available_clusters=corridors_map,
            strategy=cluster_selection,
            clusters_per_flight=clusters_per_flight,
        )
        if not tasks:
            logger.info("No tasks generated for day %s — skipping.", day)
            continue

        total_cohort_tasks += len(tasks)

        # ── Slot 2: skip-gate + group into batches ───────────────────────── #
        batches = filter_and_batch(
            tasks=tasks,
            sim_mode=sim_mode,
            lake_path=lake_path,
            step_size=step_size,
            min_safe_fl=min_safe_fl,
            max_batch_size=batch_size,
            overwrite=overwrite,
        )

        if not batches:
            day_elapsed = time.perf_counter() - day_t0
            total_skipped_tasks += len(tasks)
            logger.info(
                "Day %s completed in %s — 0 tasks committed (all %d cohort tasks already simulated), 0 weather I/O.",
                day, _format_duration(day_elapsed), len(tasks),
            )
            continue

        active_tasks = [t for b in batches for t in b]

        # ── Compute exact ERA5 window from active unsimulated tasks ──────── #
        era5_start = _floor_h(
            min(pd.Timestamp(t.firstseen, unit="s", tz="UTC") for t in active_tasks)
        ) - pd.Timedelta(hours=1)

        era5_end = _ceil_h(
            max(pd.Timestamp(t.lastseen, unit="s", tz="UTC") for t in active_tasks)
        ) + pd.Timedelta(hours=max_age_hours + 1)

        window_h = (era5_end - era5_start).total_seconds() / 3600
        logger.info(
            "ERA5 window: %s → %s  (%.0f h).  Cache size before eviction: %d h.",
            era5_start, era5_end, window_h, len(hour_cache),
        )

        # ── Lazy eviction: drop hours that fall before this day's window ── #
        stale = [h for h in list(hour_cache) if h < era5_start]
        if stale:
            logger.info("Evicting %d stale ERA5 hour(s) from cache.", len(stale))
            for h in stale:
                del hour_cache[h]
            gc.collect()

        # ── Load only missing hours ──────────────────────────────────────── #
        needed = pd.date_range(era5_start, era5_end, freq="h")
        _populate_hour_cache(needed, hour_cache, weather_cache_dir, bbox, low_mem)

        available = [h for h in needed if h in hour_cache]
        if not available:
            logger.error("No ERA5 data available for day %s — skipping.", day)
            continue

        # ── Concatenate cached hours → full met / rad for this day ────────  #
        met_xr = xr.concat([hour_cache[h][0].data for h in available], dim="time")
        rad_xr = xr.concat([hour_cache[h][1].data for h in available], dim="time")
        if low_mem:
            met_xr = met_xr.chunk({"time": -1})
            rad_xr = rad_xr.chunk({"time": -1})

        met = MetDataset(met_xr, copy=False)
        rad = MetDataset(rad_xr, copy=False)

        logger.info(
            "Day %s: %d cohort task(s) → %d active after skip-gate → %d batch(es).",
            day, len(tasks), len(active_tasks), len(batches),
        )

        # ── Engine: ThreadPoolExecutor dispatch ─────────────────────────── #
        worker_fn = partial(
            run_batch,
            corridors_map=corridors_map,
            model_config_id=model_config_id,
            sim_mode=sim_mode,
            lake_path=lake_path,
            met=met,
            rad=rad,
            max_age_hours=max_age_hours,
            fuel=fuel,
            step_down_method=step_down_method,
            overwrite=overwrite,
            lake_verbosity=lake_verbosity,
            low_mem=low_mem,
        )

        day_succeeded = day_failed = 0
        day_baseline_succeeded = 0
        day_stepdown_succeeded = 0
        pending: list = list(batches)  # start with Slot-2 batches
        round_idx = 0

        while pending:
            round_still_todo: List[SimTask] = []

            # Build task lookup for Slot 5 variational step-down once per round (pending is stable during inner loop)
            task_by_fid = {
                t.sim_fid: t
                for batch in pending
                for t in batch
            }

            for batch_output in run_parallel(pending, worker_fn, max_workers):
                eval_result = evaluate(
                    batch_output, task_by_fid, sim_mode, model_config_id, step_size, min_safe_fl
                )

                n_succ = len(eval_result.succeeded)
                day_succeeded += n_succ
                day_failed    += len(eval_result.failed)

                if round_idx == 0:
                    day_baseline_succeeded += n_succ
                else:
                    day_stepdown_succeeded += n_succ

                if eval_result.still_todo:
                    round_still_todo.extend(eval_result.still_todo)

            if round_still_todo:
                # Re-batch all round step-downs together via Slot 2 partition_tasks to pack full vectorized batches
                pending = partition_tasks(round_still_todo, max_batch_size=batch_size)
                round_idx += 1
            else:
                pending = []

        # ── Post-day cleanup ─────────────────────────────────────────────── #
        optimize_sim_lake(lake_path, z_order_cols=["dep_date", "route", "EF_total"])
        vacuum_sim_lake(lake_path, retention_hours=0)
        del met, rad
        gc.collect()

        day_elapsed = time.perf_counter() - day_t0
        total_committed_tasks += day_succeeded
        total_baseline_tasks += day_baseline_succeeded
        total_stepdowns_emitted += day_stepdown_succeeded
        total_failed_tasks += day_failed

        rate_str = f"{day_elapsed / day_succeeded:.2f}s/task" if day_succeeded > 0 else "N/A"
        if sim_mode == "variational":
            breakdown_str = f"{day_baseline_succeeded} baseline + {day_stepdown_succeeded} step-downs emitted"
        else:
            breakdown_str = f"{day_succeeded} cohort"

        logger.info(
            "Day %s completed in %s — %d tasks committed to lake (%s) [%s, %d failed].",
            day, _format_duration(day_elapsed), day_succeeded, rate_str, breakdown_str, day_failed,
        )

    total_elapsed = time.perf_counter() - run_t0
    rate_str = f"{total_elapsed / total_committed_tasks:.2f}s/task" if total_committed_tasks > 0 else "N/A"
    fps_str = f"{total_committed_tasks / total_elapsed:.2f} tasks/s" if total_elapsed > 0 else "N/A"

    if sim_mode == "variational":
        campaign_breakdown = (
            f"  • Baseline Tasks:      {total_baseline_tasks} tasks\n"
            f"  • Step-Downs Emitted:  {total_stepdowns_emitted} tasks\n"
        )
    else:
        campaign_breakdown = f"  • Cohort Tasks:        {total_cohort_tasks} candidate tasks\n"

    logger.info(
        "\n"
        "================================================================================\n"
        "ORCHESTRATOR CAMPAIGN SUMMARY\n"
        "================================================================================\n"
        "Total Duration:          %s across %d calendar day(s)\n"
        "Total Tasks Committed:   %d tasks written to Delta Lake (%s, %s)\n"
        "%s"
        "Failed Simulations:      %d\n"
        "================================================================================",
        _format_duration(total_elapsed), len(date_range),
        total_committed_tasks, rate_str, fps_str,
        campaign_breakdown,
        total_failed_tasks,
    )

