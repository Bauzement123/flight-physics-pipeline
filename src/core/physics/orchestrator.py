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
1  generate_tasks()      — build SimTask list from cohort
2  filter_and_batch()    — skip-gate (Delta Lake) + group into batches
   engine.run_parallel() — ThreadPoolExecutor dispatch → yields WorkerResult
5  evaluate()            — partition results; O1 still_todo always []
   vacuum_sim_lake()     — clean up stale Delta Lake files
"""

import gc
import logging
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
    WEATHER_IO_WORKERS,
    WEATHER_PADDING,
)
from src.core.physics.engine import crop_met_dataset, run_parallel
from src.core.physics.slots.slot1_task_gen import generate_tasks
from src.core.physics.slots.slot2_batcher import filter_and_batch
from src.core.physics.slots.slot5_evaluator import evaluate
from src.core.physics.worker import run_batch
from src.data_manager.io_utils import read_master_flights, vacuum_sim_lake
from src.data_manager.schemas import MasterFlightQuery, SimTask

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private weather helpers (adapted verbatim from clone_simulation.py)
# ---------------------------------------------------------------------------

def _floor_h(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.floor("h")


def _ceil_h(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.ceil("h")


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
    corridors_map: Dict[Tuple[str, int], Path],
    lake_path: Path,
    weather_cache_dir: Path,
    max_age_hours: int = 48,
    max_workers: int = 4,
    step_size: float = 1000.0,
    min_safe_fl: float = 280.0,
    low_mem: bool = False,
    clusters_per_flight: int = 1,
    default_fl: float = 350.0,
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
        ``'O1'`` (standard) or ``'O2'`` (step-down variational — second pass).
    model_config_id : str
        Fuel/model config flag passed to ``get_model()``, e.g. ``'kerosene'``.
    corridors_map : Dict[Tuple[str, int], Path]
        ``(route_key, cluster_id)`` → cluster parquet path.
    lake_path : Path
        Delta Lake root directory for simulation results.
    weather_cache_dir : Path
        Directory containing hourly ERA5 ``.nc`` cache files.
    max_age_hours : int
        Maximum contrail age in hours (default 48).
    max_workers : int
        Thread pool size for ``run_parallel`` (default 4).
    step_size : float
        FL step-down increment in feet for O2 (default 1000.0).
    min_safe_fl : float
        Minimum FL below which step-down is halted (default 280.0).
    low_mem : bool
        Lazy-load ERA5 datasets; skip eager ``.load()`` call (default False).
    clusters_per_flight : int
        Number of cluster trajectories to generate per flight (default 1).
    default_fl : float
        Fallback flight level when registry lookup yields no value (default 350.0).
    min_distance_km : float
        Pre-filter: skip routes shorter than this distance in km (default 0).
    overwrite : bool
        If True, bypass the Delta Lake skip-gate in Slot 2 and re-simulate
        all tasks regardless of prior results (default False).
    bbox : list
        ``[west, south, east, north]`` spatial crop applied to ERA5 data.
    """
    # Per-hour ERA5 cache: pd.Timestamp (UTC, hourly) → (met_h, rad_h)
    hour_cache: Dict[pd.Timestamp, Tuple[MetDataset, MetDataset]] = {}

    for day_idx, day in enumerate(date_range):
        logger.info("=== Day %s  (%d / %d) ===", day, day_idx + 1, len(date_range))

        # ── Slot 1: read cohort + generate tasks ────────────────────────── #
        day_start = pd.Timestamp(day, tz="UTC")
        day_end   = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
        query     = MasterFlightQuery(dep_date_start=day_start, dep_date_end=day_end)

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

        tasks: List[SimTask] = generate_tasks(
            cohort_df=cohort_df,
            available_clusters=corridors_map,
            clusters_per_flight=clusters_per_flight,
            default_fl=default_fl,
        )
        if not tasks:
            logger.info("No tasks generated for day %s — skipping.", day)
            continue

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
            logger.info("All tasks already in lake for day %s — skipping engine.", day)
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
            low_mem=low_mem,
            overwrite=overwrite,
        )

        day_succeeded = day_failed = 0

        for worker_results in run_parallel(batches, worker_fn, max_workers):
            eval_result = evaluate(worker_results, sim_mode, step_size, min_safe_fl)
            day_succeeded += len(eval_result.succeeded)
            day_failed    += len(eval_result.failed)
            # O1: eval_result.still_todo is always [] — no re-queuing.
            # TODO (second pass / O2): re-queue eval_result.still_todo

        logger.info(
            "Day %s: %d succeeded, %d failed.", day, day_succeeded, day_failed
        )

        # ── Post-day cleanup ─────────────────────────────────────────────── #
        vacuum_sim_lake(lake_path)
        del met, rad
        gc.collect()

    logger.info(
        "Orchestrator run complete — %d day(s) processed.", len(date_range)
    )
