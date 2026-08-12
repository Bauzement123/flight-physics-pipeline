"""
slots/slot1_task_gen.py — Slot 1: Task Generation

Converts a cohort DataFrame (one day's slice of master_flights) into a flat
list of SimTask objects. Behaviour is identical for O1 and O2 runs — this
slot knows nothing about EF, step-downs, or cache state. Those concerns
belong to Slot 2 and Slot 5.

Extracted from: clone_simulation.py L595–L668
"""

import logging
from typing import Dict, List, Tuple

import numpy as np

from src.data_manager.schemas import SimTask

logger = logging.getLogger(__name__)


def generate_tasks(
    cohort_df,
    available_clusters: Dict[Tuple[str, int], object],
    clusters_per_flight: int = 1,
    default_fl: float = 350.0,
) -> List[SimTask]:
    """Generate a flat list of SimTask objects from a cohort DataFrame.

    One SimTask is produced per (flight row × sampled cluster_id) pair.
    No I/O is performed, no skip-gate checks are applied, no trajectories
    are loaded — those concerns belong to Slot 2 and the loaders.

    Parameters
    ----------
    cohort_df : pd.DataFrame
        One day's slice of master_flights. Required columns:
        ``icao24, callsign, firstseen, lastseen,
        estdepartureairport, estarrivalairport, typecode``.
    available_clusters : Dict[Tuple[str, int], object]
        Mapping of ``(route_key, cluster_id)`` to cluster file path.
        Produced by the orchestrator from the corridor paths registry.
    clusters_per_flight : int, optional
        Number of clusters to sample per flight (default 1).
        K=1 is the current stub — deterministic selection is deferred.
    default_fl : float, optional
        Target flight level in feet assigned to all O1 tasks (default 350.0).

        # TODO: second pass — replace with deterministic FL selection
        # based on flight metadata (typecode, route distance, historical
        # cruise altitude). For now every task uses this constant value.

    Returns
    -------
    List[SimTask]
        Flat list of SimTask objects, one per (flight, cluster) pair.
        Empty if no clusters are available for any flight in the cohort.
    """
    tasks: List[SimTask] = []

    for _, row in cohort_df.iterrows():
        dep = row["estdepartureairport"]
        arr = row["estarrivalairport"]
        route_key = f"{dep}-{arr}"

        icao24 = row["icao24"]
        callsign = row.get("callsign", "UNK") or "UNK"

        # firstseen / lastseen are expected as epoch integers (seconds UTC).
        # Tolerate both int and pd.Timestamp inputs.
        firstseen = int(row["firstseen"].timestamp()
                        if hasattr(row["firstseen"], "timestamp")
                        else row["firstseen"])
        lastseen = int(row["lastseen"].timestamp()
                       if hasattr(row["lastseen"], "timestamp")
                       else row["lastseen"])

        available = [cid for (r, cid) in available_clusters if r == route_key]
        if not available:
            logger.warning(
                "No synthesized base paths for route %s — skipping flight %s/%s.",
                route_key, icao24, callsign,
            )
            continue

        sample_size = min(clusters_per_flight, len(available))
        sampled = np.random.choice(available, size=sample_size, replace=False)

        for cluster_id in sampled:
            tasks.append(SimTask(
                icao24=icao24,
                callsign=callsign,
                dep=dep,
                arr=arr,
                firstseen=firstseen,
                lastseen=lastseen,
                typecode=str(row.get("typecode", "") or ""),
                cluster_id=int(cluster_id),
                fl=default_fl,
            ))

    logger.info("Slot 1 generated %d tasks from %d cohort rows.", len(tasks), len(cohort_df))
    return tasks
