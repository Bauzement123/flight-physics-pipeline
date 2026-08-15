"""
slots/slot2_batcher.py — Slot 2: Task Filtering and Batching

Filters candidate SimTask objects against the Delta Lake simulation registry
(skip-gate) and partitions unsimulated tasks into execution batches.

Execution Modes:
- standard (Waterfall 1 Baseline):
  1. Checks each task against the Delta Lake via sim_fid_exists().
  2. Filters out tasks already simulated.
  3. Groups remaining tasks by (dep, arr, cluster_id).
  4. Chunks each group into sub-batches of max_batch_size (same as old --batch-size).
- variational (Variational Optimization Pass):
  1. Batch-reads all existing EF/FL results from the lake by base key.
  2. Per task:
     - No prior results → emit at task.fl (first simulation).
     - Any prior EF <= 0 → skip (contrail already suppressed).
     - All prior EF > 0 → compute_stepdown_task from lowest simulated FL.
  3. Groups and chunks remaining tasks identically to standard mode.

Extracted from:
- clone_simulation.py L183-L291 (filter_cohort_flights skip-gate + route grouping)
- engine.py L195-L276 (simulate_flights_parallel batch partitioning)
"""

import dataclasses
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.common.config import MIN_SAFE_FL
from src.data_manager.io_utils import read_ef_by_base_key, read_existing_sim_fids
from src.data_manager.schemas import SimTask

logger = logging.getLogger(__name__)


def partition_tasks(
    tasks: List[SimTask],
    max_batch_size: int = 50,
) -> List[List[SimTask]]:
    """Sort tasks by route, cluster, and timestamp, then continuously chunk into full batches.

    Guarantees deterministic ordering, maximizes vectorization batch saturation (100% full
    batches up to max_batch_size), and minimizes distinct routes per batch by keeping flights
    on the same corridor adjacent.

    Parameters
    ----------
    tasks : List[SimTask]
        Flat list of simulation tasks to batch.
    max_batch_size : int, optional
        Target batch size ceiling (default 50).

    Returns
    -------
    List[List[SimTask]]
        List of saturated task batches.
    """
    if not tasks:
        return []

    sorted_tasks = sorted(
        tasks,
        key=lambda t: (t.dep, t.arr, t.cluster_id, t.firstseen),
    )

    return [
        sorted_tasks[i : i + max_batch_size]
        for i in range(0, len(sorted_tasks), max_batch_size)
    ]


def filter_and_batch(
    tasks: List[SimTask],
    sim_mode: str,
    lake_path: Path,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
    max_batch_size: int = 50,
    overwrite: bool = False,
    model_config_id: Optional[str] = None,
) -> List[List[SimTask]]:
    """Filter candidate tasks against the Delta Lake and group into execution batches.

    Tasks are sorted by route, cluster, and timestamp, then continuously chunked
    into batches of ``max_batch_size``. This maximizes vectorized GPU/CPU utilization
    while keeping route switching minimal.

    Parameters
    ----------
    tasks : List[SimTask]
        Flat list of candidate tasks produced by Slot 1.
    sim_mode : str
        Execution mode: ``'standard'`` or ``'variational'``.
    lake_path : Path
        Path to the simulation Delta Lake directory.
    step_size : float, optional
        FL step-down decrement in feet (default 10.0). Variational only.
    min_safe_fl : float, optional
        Minimum safe FL in feet (default 280.0). Variational only.
    max_batch_size : int, optional
        Maximum number of tasks per batch (default 50).
    overwrite : bool, optional
        If True, re-simulate tasks already present in the lake (default False).
    model_config_id : str, optional
        Filter lake query to this model_config_id. Variational only.

    Returns
    -------
    List[List[SimTask]]
        List of task batches ordered by (dep, arr, cluster_id, firstseen).

    Raises
    ------
    ValueError
        If ``sim_mode`` is not ``'standard'`` or ``'variational'``.
    """
    if sim_mode not in ("standard", "variational"):
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'standard' or 'variational'.")

    if sim_mode == "variational":
        return _filter_and_expand_variational(
            tasks=tasks,
            lake_path=lake_path,
            step_size=step_size,
            min_safe_fl=min_safe_fl,
            max_batch_size=max_batch_size,
            model_config_id=model_config_id,
            overwrite=overwrite,
        )

    # --- standard: bulk existence check + route-sorted continuous batching ---
    routes = list({f"{t.dep}-{t.arr}" for t in tasks})
    dep_dates = {int(pd.Timestamp(t.firstseen, unit="s", tz="UTC").strftime("%Y%m%d")) for t in tasks}
    dep_date = dep_dates.pop() if len(dep_dates) == 1 else None

    existing_fids = frozenset() if overwrite else read_existing_sim_fids(lake_path, routes, dep_date)

    unsimulated_tasks = [t for t in tasks if t.to_sim_fid() not in existing_fids]
    skipped = len(tasks) - len(unsimulated_tasks)

    batches = partition_tasks(unsimulated_tasks, max_batch_size=max_batch_size)

    avg_density = (len(unsimulated_tasks) / len(batches)) if batches else 0.0
    logger.info(
        "Slot 2 (standard): %d tasks in → %d skipped → %d batch(es) "
        "[max_batch_size=%d, avg_density=%.1f flights/batch].",
        len(tasks), skipped, len(batches), max_batch_size, avg_density,
    )
    return batches


# ---------------------------------------------------------------------------
# Private: variational expansion logic
# ---------------------------------------------------------------------------

def _filter_and_expand_variational(
    tasks: List[SimTask],
    lake_path: Path,
    step_size: float,
    min_safe_fl: float,
    max_batch_size: int,
    model_config_id: Optional[str],
    overwrite: bool = False,
) -> List[List[SimTask]]:
    """Determine which tasks to simulate next for the variational step-down campaign.

    For each task:
    - No lake rows for its base key → emit task at task.fl (first simulation).
    - Any existing EF <= 0 → skip (contrail already suppressed at some FL).
    - All existing EF > 0 → call compute_stepdown_task from the lowest simulated FL.
      If that returns None (already at floor), skip.

    Parameters
    ----------
    tasks : List[SimTask]
        Candidate tasks from Slot 1. ``task.fl`` carries the *nominal* FL from
        the cluster registry — used only when no prior results exist.
    lake_path : Path
        Root of the simulation Delta Lake.
    step_size : float
        FL decrement per iteration (feet).
    min_safe_fl : float
        Operational floor below which no step-down is emitted.
    max_batch_size : int
        Max tasks per batch chunk.
    model_config_id : str or None
        Scope the lake query to a specific fuel/model configuration.
    overwrite : bool
        If True, ignore prior lake results and re-emit all tasks at nominal FL.

    Returns
    -------
    List[List[SimTask]]
        Batches ready for Slot 3 dispatch.
    """
    skipped_suppressed = 0
    skipped_floor = 0
    emit_initial = 0
    emit_stepdown = 0
    emitted_tasks: List[SimTask] = []

    if overwrite:
        for task in tasks:
            emit_initial += 1
            emitted_tasks.append(task)
    else:
        routes = list({f"{t.dep}-{t.arr}" for t in tasks})
        dep_dates = list({
            int(pd.Timestamp(t.firstseen, unit="s", tz="UTC").strftime("%Y%m%d"))
            for t in tasks
        })
        base_keys = {task.to_sim_fid().rsplit("_", 1)[0] for task in tasks}
        lake_results = read_ef_by_base_key(
            lake_path, base_keys, model_config_id, routes=routes, dep_dates=dep_dates
        )

        for task in tasks:
            base_key = task.to_sim_fid().rsplit("_", 1)[0]
            prior = lake_results.get(base_key)  # list[(fl, ef_total)] or None

            if prior is None:
                # Never simulated — emit at nominal FL from cluster registry
                emit_initial += 1
                emitted_tasks.append(task)
                continue

            # Any result with EF <= 0 means contrail is already suppressed
            if any(ef <= 0 for _fl, ef in prior):
                skipped_suppressed += 1
                logger.debug(
                    "Variational skip (suppressed) base_key=%s.", base_key
                )
                continue

            # All EF > 0 — step down from the lowest FL already simulated
            lowest_fl, lowest_ef = min(prior, key=lambda x: x[0])
            # Build a proxy task at the lowest FL to feed into compute_stepdown_task
            proxy_task = _task_at_fl(task, lowest_fl)
            next_task = compute_stepdown_task(proxy_task, lowest_ef, step_size, min_safe_fl)

            if next_task is None:
                skipped_floor += 1
                logger.debug(
                    "Variational skip (floor) base_key=%s lowest_fl=%.0f.", base_key, lowest_fl
                )
                continue

            emit_stepdown += 1
            emitted_tasks.append(next_task)

    batches = partition_tasks(emitted_tasks, max_batch_size=max_batch_size)

    avg_density = (len(emitted_tasks) / len(batches)) if batches else 0.0
    logger.info(
        "Slot 2 (variational): %d tasks in → "
        "%d initial, %d step-down, %d suppressed-skip, %d floor-skip "
        "→ %d batch(es) [max_batch_size=%d, avg_density=%.1f flights/batch, step_size=%.0f, min_safe_fl=%.0f].",
        len(tasks), emit_initial, emit_stepdown,
        skipped_suppressed, skipped_floor,
        len(batches), max_batch_size, avg_density, step_size, min_safe_fl,
    )
    return batches


def compute_stepdown_task(
    task: SimTask,
    ef: float,
    step_size: float = 10.0,
    min_safe_fl: float = MIN_SAFE_FL,
) -> Optional[SimTask]:
    """Return a new step-down SimTask if conditions are met, else None.

    Single source of truth for variational step-down task mutation.
    Owned by Slot 2 and called during both initial lake history evaluation
    and Slot 5 batch result evaluation.

    Parameters
    ----------
    task : SimTask
        The completed or candidate task whose FL we want to reduce.
    ef : float
        Energy Forcing result (J). Positive means contrail warming — step-down attempted.
    step_size : float
        FL reduction per step-down iteration (feet).
    min_safe_fl : float
        Minimum FL below which no further step-down is attempted.

    Returns
    -------
    Optional[SimTask]
        New SimTask at ``task.fl - step_size`` if ``ef > 0`` and
        ``task.fl - step_size >= min_safe_fl``, otherwise ``None``.
    """
    if ef <= 0:
        return None

    next_fl = task.fl - step_size
    if next_fl < min_safe_fl:
        return None

    return dataclasses.replace(task, fl=next_fl)


def _task_at_fl(task: SimTask, fl: float) -> SimTask:
    """Return a copy of task with fl replaced. Pure helper."""
    return dataclasses.replace(task, fl=fl)
