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
from concurrent.futures import ThreadPoolExecutor
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
    WEATHER_PADDING,
    ALL_TARGET_FAMILIES,
)
from src.core.physics.engine import crop_met_dataset, run_parallel
from src.core.physics.slots.slot1_flightlist_gen import build_corridors_map, generate_base_flightlist, select_clusters
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
    """Open, optionally crop, and optionally eager-load one ERA5 dataset. Thread-safe.

    In standard mode: crop to bbox (reduces in-memory footprint) then eager-load.
    In low-mem mode: skip crop (dask only loads touched chunks anyway, crop adds
    graph overhead without meaningfully reducing peak RAM) and skip eager load.
    """
    met_ds = era5_obj.open_metdataset()
    if not low_mem:
        met_ds = crop_met_dataset(met_ds, bbox, pad=WEATHER_PADDING)
        met_ds.data.load()
    return met_ds


def _build_era5_objects(
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
    weather_cache_dir: Path,
) -> Tuple[ERA5, ERA5]:
    """Construct ERA5 PL and SL objects for a contiguous time range.

    Prefers offline mode (``paths=`` list) when all hourly .nc files exist.
    Falls back to online ``DiskCacheStore`` fetch when any file is missing.
    Adapted from clone_simulation.py L312-394.
    """
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S")
    end_str   = end_time.strftime("%Y-%m-%dT%H:%M:%S")
    hours     = pd.date_range(start=start_time, end=end_time, freq="h")

    pl_paths, sl_paths = [], []
    all_pl_exist = all_sl_exist = True

    for h in hours:
        pl_name = f"{h.strftime('%Y%m%d-%H')}-era5pl0.5reanalysis.nc"
        sl_name = f"{h.strftime('%Y%m%d-%H')}-era5sl0.5reanalysis.nc"
        pl_file = weather_cache_dir / pl_name
        sl_file = weather_cache_dir / sl_name

        if pl_file.exists():
            pl_paths.append(str(pl_file))
        else:
            all_pl_exist = False
        if sl_file.exists():
            sl_paths.append(str(sl_file))
        else:
            all_sl_exist = False

    if all_pl_exist and all_sl_exist and pl_paths and sl_paths:
        logger.info("ERA5 offline mode: all files cached for %s → %s.", start_str, end_str)
        try:
            era5_pl = ERA5(
                time=(start_str, end_str),
                paths=pl_paths,
                variables=ERA5_PRESSURE_LEVEL_VARIABLES,
                pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
                grid=ERA5_GRID,
            )
            era5_sl = ERA5(
                time=(start_str, end_str),
                paths=sl_paths,
                variables=ERA5_SURFACE_VARIABLES,
                pressure_levels=-1,
                grid=ERA5_GRID,
            )
            return era5_pl, era5_sl
        except Exception as exc:
            logger.warning("Offline ERA5 init failed (%s) — falling back to online.", exc)

    logger.info("ERA5 online mode: fetching via DiskCacheStore for %s → %s.", start_str, end_str)
    disk_cache = DiskCacheStore(cache_dir=str(weather_cache_dir))
    era5_pl = ERA5(
        time=(start_str, end_str),
        variables=ERA5_PRESSURE_LEVEL_VARIABLES,
        pressure_levels=ERA5_REQUIRED_PRESSURE_LEVELS,
        grid=ERA5_GRID,
        cachestore=disk_cache,
    )
    era5_sl = ERA5(
        time=(start_str, end_str),
        variables=ERA5_SURFACE_VARIABLES,
        pressure_levels=-1,
        grid=ERA5_GRID,
        cachestore=disk_cache,
    )
    return era5_pl, era5_sl


def _load_hour_range(
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
    weather_cache_dir: Path,
    bbox: list,
    low_mem: bool,
) -> Tuple[MetDataset, MetDataset]:
    """Load and crop PL+SL ERA5 for a contiguous block. PL and SL opened concurrently."""
    era5_pl, era5_sl = _build_era5_objects(start_time, end_time, weather_cache_dir)
    with ThreadPoolExecutor(max_workers=WEATHER_IO_WORKERS) as executor:
        future_pl = executor.submit(_open_crop_and_load, era5_pl, bbox, low_mem)
        future_sl = executor.submit(_open_crop_and_load, era5_sl, bbox, low_mem)
        met = future_pl.result()
        rad = future_sl.result()
    return met, rad


def _populate_hour_cache(
    needed: pd.DatetimeIndex,
    hour_cache: Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]],
    weather_cache_dir: Path,
    bbox: list,
    low_mem: bool,
) -> None:
    """Load only the missing hours from ``needed`` into ``hour_cache``.

    Consecutive missing hours are grouped into contiguous load ranges to
    minimise ERA5 object construction overhead.  Each loaded block is then
    sliced into per-hour ``MetDataset`` entries and stored in the cache.
    """
    missing = sorted(h for h in needed if h not in hour_cache)
    if not missing:
        logger.debug("All %d ERA5 hours already cached.", len(needed))
        return

    # Group consecutive hours into contiguous ranges
    ranges: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    t0 = prev = missing[0]
    for h in missing[1:]:
        if h - prev > pd.Timedelta(hours=1):
            ranges.append((t0, prev))
            t0 = h
        prev = h
    ranges.append((t0, prev))

    logger.info(
        "Loading %d missing ERA5 hours in %d contiguous range(s).",
        len(missing), len(ranges),
    )

    for seg_start, seg_end in ranges:
        met_block, rad_block = _load_hour_range(
            seg_start, seg_end, weather_cache_dir, bbox, low_mem
        )
        seg_hours = pd.date_range(seg_start, seg_end, freq="h")
        for h in seg_hours:
            try:
                h_np = h.to_datetime64()
                hour_cache[h] = (
                    MetDataset(met_block.data.sel(time=[h_np])),
                    MetDataset(rad_block.data.sel(time=[h_np])),
                )
            except Exception as exc:
                logger.warning("Could not slice hour %s from ERA5 block: %s", h, exc)


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
        Lazy-load ERA5 datasets; skip eager ``.load()`` call (default False).
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
    """
    # ── Slot 1: Build corridor cluster mapping from model registry ───── #
    corridors_map = build_corridors_map(ranks=ranks, corridors_dir=corridors_dir)
    if not corridors_map:
        logger.error("No corridor files found for ranks %s — nothing to do.", ranks)
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

        if min_distance_km > 0 and "distance_km" in cohort_df.columns:
            cohort_df = cohort_df[cohort_df["distance_km"] >= min_distance_km]
            if cohort_df.empty:
                logger.info(
                    "All flights < min_distance_km=%.1f km — skipping day %s.",
                    min_distance_km, day,
                )
                continue

        candidate_pool = generate_base_flightlist(
            cohort_df=cohort_df,
            available_clusters=corridors_map,
        )
        tasks: List[SimTask] = select_clusters(
            candidate_pool=candidate_pool,
            available_clusters=corridors_map,
            strategy=cluster_selection,
            clusters_per_flight=clusters_per_flight,
        )
        if not tasks:
            logger.info("No tasks generated for day %s — skipping.", day)
            continue

        total_cohort_tasks += len(tasks)

        # ── Compute exact ERA5 window from task firstseen / lastseen ────── #
        era5_start = _floor_h(
            min(pd.Timestamp(t.firstseen, unit="s", tz="UTC") for t in tasks)
        ) - pd.Timedelta(hours=1)

        era5_end = _ceil_h(
            max(pd.Timestamp(t.lastseen, unit="s", tz="UTC") for t in tasks)
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

        # ── Load only missing hours ──────────────────────────────────────── #
        needed = pd.date_range(era5_start, era5_end, freq="h")
        _populate_hour_cache(needed, hour_cache, weather_cache_dir, bbox, low_mem)

        available = [h for h in needed if h in hour_cache]
        if not available:
            logger.error("No ERA5 data available for day %s — skipping.", day)
            continue

        # ── Concatenate cached hours → full met / rad for this day ────────  #
        met = MetDataset(xr.concat(
            [hour_cache[h][0].data for h in available], dim="time"
        ))
        rad = MetDataset(xr.concat(
            [hour_cache[h][1].data for h in available], dim="time"
        ))

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
                "Day %s completed in %s — 0 tasks committed (all %d cohort tasks skipped via skip-gate), 0 failed.",
                day, _format_duration(day_elapsed), len(tasks),
            )
            del met, rad
            gc.collect()
            continue

        logger.info(
            "Day %s: %d task(s) → %d batch(es) after skip-gate.",
            day, len(tasks), len(batches),
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
            low_mem=low_mem,
            overwrite=overwrite,
        )

        day_succeeded = day_failed = 0
        day_baseline_succeeded = 0
        day_stepdown_succeeded = 0
        pending: list = list(batches)  # start with Slot-2 batches
        round_idx = 0

        while pending:
            round_still_todo: List[SimTask] = []

            for worker_results in run_parallel(pending, worker_fn, max_workers):
                # Build task lookup for Slot 5 variational step-down
                task_by_fid = {
                    t.to_sim_fid(): t
                    for batch in pending
                    for t in batch
                }
                eval_result = evaluate(
                    worker_results, task_by_fid, sim_mode, step_size, min_safe_fl
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
        vacuum_sim_lake(lake_path)
        optimize_sim_lake(lake_path, z_order_cols=["dep_date", "route", "EF_total"])
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

