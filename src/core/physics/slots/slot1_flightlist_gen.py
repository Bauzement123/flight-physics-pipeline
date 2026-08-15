"""
slots/slot1_flightlist_gen.py — Slot 1: Flight List Generation & Corridor Mapping

Responsible for corridor metadata mapping and generating candidate SimTask objects
from a daily flight cohort DataFrame.

Functions:
- build_corridors_map(): Central public function to build (route, cluster_id) -> CorridorCluster
  mapping from the corridor model registry, with optional rank filtering. Serves as the central
  seam for future cluster assignment logic.
- generate_flightlist(): Pure transform converting master_flights rows into flat SimTask list.
  Scopes missing-cluster warnings strictly to requested/allowed routes.

Extracted from: clone_simulation.py L595–L668
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.data_manager.schemas import CorridorCluster, SimTask, FlightCandidate
from src.data_manager.io_utils import read_corridors_map

logger = logging.getLogger(__name__)


def build_corridors_map(
    ranks: Optional[List[int]] = None,
    registry_path: Optional[Path] = None,
    corridors_dir: Optional[Path] = None,
) -> Dict[Tuple[str, int], CorridorCluster]:
    """Build the (route_id, cluster_id) -> CorridorCluster map from registry.

    Delegates data retrieval to ``src.data_manager.io_utils.read_corridors_map()``.

    Parameters
    ----------
    ranks : List[int], optional
        List of route ranks to include (1-indexed). If None, all routes in the
        registry are included.
    registry_path : Path, optional
        Path to the corridor model registry parquet. Defaults to GLOBAL_CORRIDOR_MODEL_REGISTRY.
    corridors_dir : Path, optional
        Base directory containing corridor parquet files if overriding default paths.

    Returns
    -------
    Dict[Tuple[str, int], CorridorCluster]
        Mapping of (route_id, cluster_id) -> CorridorCluster(path, fl).
    """
    return read_corridors_map(
        ranks=ranks,
        registry_path=registry_path,
        corridors_dir=corridors_dir,
    )


def generate_base_flightlist(
    cohort_df: pd.DataFrame,
    available_clusters: Dict[Tuple[str, int], CorridorCluster],
) -> List[FlightCandidate]:
    """Pure flightlist generation mapping cohort rows to available cluster IDs.

    Filters flights based on available clusters for their route. Does not sample;
    returns all valid available cluster IDs for each flight row.

    Parameters
    ----------
    cohort_df : pd.DataFrame
        One day's slice of master_flights.
    available_clusters : Dict[Tuple[str, int], CorridorCluster]
        Mapping of ``(route_key, cluster_id)`` to ``CorridorCluster(path, fl)``.

    Returns
    -------
    List[FlightCandidate]
        List of candidate objects for flights with at least one valid cluster.
    """
    candidate_pool = []
    allowed_routes = {r for (r, _) in available_clusters}

    for _, row in cohort_df.iterrows():
        dep = row["estdepartureairport"]
        arr = row["estarrivalairport"]
        route_key = f"{dep}-{arr}"

        icao24 = row["icao24"]
        callsign = row.get("callsign", "UNK") or "UNK"

        available_cids = [cid for (r, cid) in available_clusters if r == route_key]
        if not available_cids:
            if route_key in allowed_routes:
                logger.warning(
                    "No synthesized base paths for route %s — skipping flight %s/%s.",
                    route_key, icao24, callsign,
                )
            continue

        valid_cids = []
        for cluster_id in available_cids:
            cluster_entry = available_clusters.get((route_key, cluster_id))
            fl = getattr(cluster_entry, "fl", None)
            if fl is None or np.isnan(fl) or fl <= 0:
                logger.error(
                    "Cluster (%s, %d) has missing or invalid FL (%s) — skipping for flight %s/%s. "
                    "Verify GLOBAL_CORRIDOR_MODEL_REGISTRY before running.",
                    route_key, cluster_id, fl, icao24, callsign,
                )
                continue
            valid_cids.append(cluster_id)

        if valid_cids:
            firstseen = int(row["firstseen"].timestamp()
                            if hasattr(row["firstseen"], "timestamp")
                            else row["firstseen"])
            lastseen = int(row["lastseen"].timestamp()
                           if hasattr(row["lastseen"], "timestamp")
                           else row["lastseen"])
                           
            candidate_pool.append(FlightCandidate(
                icao24=icao24,
                callsign=callsign,
                dep=dep,
                arr=arr,
                firstseen=firstseen,
                lastseen=lastseen,
                typecode=str(row.get("typecode", "") or ""),
                valid_cluster_ids=valid_cids
            ))

    return candidate_pool


def select_clusters(
    candidate_pool: List[FlightCandidate],
    available_clusters: Dict[Tuple[str, int], CorridorCluster],
    strategy: str = "random",
    **kwargs: Any
) -> List[SimTask]:
    """Select clusters for each candidate flight using a specific strategy.

    Parameters
    ----------
    candidate_pool : List[FlightCandidate]
        List of valid flight candidates.
    available_clusters : Dict[Tuple[str, int], CorridorCluster]
        Mapping of ``(route_key, cluster_id)`` to ``CorridorCluster(path, fl)``.
    strategy : str
        The selection strategy to use (e.g. 'random').
    **kwargs
        Strategy-specific arguments (e.g. ``clusters_per_flight``).

    Returns
    -------
    List[SimTask]
        Flat list of SimTask objects.
    """
    tasks: List[SimTask] = []

    if strategy not in ("random",):
        logger.warning("Strategy '%s' not fully implemented or recognized. Defaulting to 'random'.", strategy)
        strategy = "random"

    for candidate in candidate_pool:

        sampled_cids = []
        if strategy == "random":
            clusters_per_flight = kwargs.get("clusters_per_flight", 1)
            sample_size = min(clusters_per_flight, len(candidate.valid_cluster_ids))
            sampled_cids = np.random.choice(candidate.valid_cluster_ids, size=sample_size, replace=False).tolist()

        for cluster_id in sampled_cids:
            cluster_entry = available_clusters[(candidate.route_key, cluster_id)]

            tasks.append(SimTask(
                icao24=candidate.icao24,
                callsign=candidate.callsign,
                dep=candidate.dep,
                arr=candidate.arr,
                firstseen=candidate.firstseen,
                lastseen=candidate.lastseen,
                typecode=candidate.typecode,
                cluster_id=int(cluster_id),
                fl=float(cluster_entry.fl),
            ))

    logger.info(
        "Slot 1 (Flight List Generation) generated %d tasks from %d candidate pool rows.",
        len(tasks), len(candidate_pool),
    )
    return tasks


def generate_flightlist(
    cohort_df: pd.DataFrame,
    available_clusters: Dict[Tuple[str, int], Any],
    clusters_per_flight: int = 1,
) -> List[SimTask]:
    """Backward-compatible wrapper for generating SimTask objects."""
    pool = generate_base_flightlist(cohort_df, available_clusters)
    return select_clusters(pool, available_clusters, strategy="random", clusters_per_flight=clusters_per_flight)


# Backward compatibility aliases
enumerate_cohort = generate_flightlist
generate_tasks = generate_flightlist
