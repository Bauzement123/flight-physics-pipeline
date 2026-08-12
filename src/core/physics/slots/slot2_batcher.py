"""
slots/slot2_batcher.py — Slot 2: Task Filtering and Batching

Filters candidate SimTask objects against the Delta Lake simulation registry
(skip-gate) and partitions unsimulated tasks into execution batches.

Execution Modes:
- O1 (Waterfall 1 Baseline):
  1. Checks each task against the Delta Lake via sim_fid_exists().
  2. Filters out tasks already simulated.
  3. Groups remaining tasks by (dep, arr, cluster_id).
  4. Chunks each group into sub-batches of max_batch_size (same as old --batch-size).
- O2 (Variational Optimization Pass):
  Reserved for second pass. Raises NotImplementedError.

Extracted from:
- clone_simulation.py L183-L291 (filter_cohort_flights skip-gate + route grouping)
- engine.py L195-L276 (simulate_flights_parallel batch partitioning)
"""

import logging
from collections import defaultdict
from pathlib import Path
from typing import List

from src.data_manager.io_utils import sim_fid_exists
from src.data_manager.schemas import SimTask

logger = logging.getLogger(__name__)


def filter_and_batch(
    tasks: List[SimTask],
    sim_mode: str,
    lake_path: Path,
    step_size: float = 1000.0,
    min_safe_fl: float = 280.0,
    max_batch_size: int = 50,
    overwrite: bool = False,
) -> List[List[SimTask]]:
    """Filter candidate tasks against the Delta Lake and group into execution batches.

    Tasks are grouped by (dep, arr, cluster_id) so the worker can reuse the
    loaded K-cluster trajectory across all flights in one batch.  If a group
    exceeds ``max_batch_size`` it is chunked into sub-batches of that size —
    identical to the old clone_simulation.py ``--batch-size`` behaviour.

    Parameters
    ----------
    tasks : List[SimTask]
        Flat list of candidate tasks produced by Slot 1.
    sim_mode : str
        Execution mode: ``'O1'`` or ``'O2'``.
    lake_path : Path
        Path to the simulation Delta Lake directory.
    step_size : float, optional
        FL step-down decrement in feet (default 1000.0). O2 only.
    min_safe_fl : float, optional
        Minimum safe FL in feet (default 280.0). O2 only.
    max_batch_size : int, optional
        Maximum number of tasks per batch (default 50).  Groups larger than
        this are split into sub-batches of this size.

    Returns
    -------
    List[List[SimTask]]
        List of task batches ordered by (dep, arr, cluster_id).

    Raises
    ------
    ValueError
        If ``sim_mode`` is not ``'O1'`` or ``'O2'``.
    NotImplementedError
        If ``sim_mode`` is ``'O2'`` (reserved for second pass).
    """
    if sim_mode not in ("O1", "O2"):
        raise ValueError(f"Unknown sim_mode '{sim_mode}'. Must be 'O1' or 'O2'.")

    if sim_mode == "O2":
        raise NotImplementedError(
            "O2 variational batching is reserved for second pass. Run O1 first."
        )

    # --- O1: skip-gate + semantic grouping ---
    skipped = 0
    groups: dict = defaultdict(list)

    for task in tasks:
        sim_fid = task.to_sim_fid()
        if not overwrite and sim_fid_exists(lake_path, sim_fid):
            logger.info("Skipping %s — already in Delta Lake.", sim_fid)
            skipped += 1
            continue
        key = (task.dep, task.arr, task.cluster_id)
        groups[key].append(task)

    # Chunk each group into sub-batches of max_batch_size
    batches: List[List[SimTask]] = []
    for group in groups.values():
        for i in range(0, len(group), max_batch_size):
            batches.append(group[i : i + max_batch_size])

    logger.info(
        "Slot 2 (O1): %d tasks in → %d skipped → %d batch(es) "
        "across %d route-cluster group(s) [max_batch_size=%d].",
        len(tasks), skipped, len(batches), len(groups), max_batch_size,
    )
    return batches
