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

from src.common.config import BASE_DIR
from src.data_manager.schemas import CorridorCluster, FlightCandidate, RouteSummaryQuery, SimTask
from src.data_manager.io_utils import read_corridor_model_registry, read_route_summary

logger = logging.getLogger(__name__)


def build_corridors_map(
    ranks: Optional[List[int]] = None,
    corridors_dir: Optional[Path] = None,
    min_distance_km: float = 0.0,
    registry_path: Optional[Path] = None,
) -> Dict[Tuple[str, int], CorridorCluster]:
    """Build the (route_id, cluster_id) -> CorridorCluster map from registry.

    1. Queries ``read_route_summary`` if ``ranks`` or ``min_distance_km > 0`` is specified.
    2. Queries ``read_corridor_model_registry`` with allowed routes predicate pushdown.
    3. Resolves trajectory filepaths into ``CorridorCluster(path, fl)`` instances.

    Parameters
    ----------
    ranks : List[int], optional
        List of route ranks to include (1-indexed). If None, all routes in the
        registry are included.
    corridors_dir : Path, optional
        Base directory containing corridor parquet files if overriding default paths.
    min_distance_km : float
        Pre-filter: skip routes shorter than this distance in km (default 0.0).
    registry_path : Path, optional
        Path to the corridor model registry parquet. Defaults to GLOBAL_CORRIDOR_MODEL_REGISTRY.

    Returns
    -------
    Dict[Tuple[str, int], CorridorCluster]
        Mapping of (route_id, cluster_id) -> CorridorCluster(path, fl).
    """
    corridors_map: Dict[Tuple[str, int], CorridorCluster] = {}
    allowed_routes: Optional[List[str]] = None

    if ranks is not None or min_distance_km > 0:
        summary_query = RouteSummaryQuery(
            ranks=ranks,
            min_distance_km=min_distance_km if min_distance_km > 0 else None,
        )
        df_summary = read_route_summary(query=summary_query, columns=["rank", "route"])
        if df_summary.empty:
            logger.warning(
                "build_corridors_map: no routes matched criteria (ranks=%s, min_distance_km=%.1f).",
                ranks, min_distance_km,
            )
            return corridors_map

        allowed = df_summary["route"].dropna().tolist()
        allowed_routes = list({r.replace(" -> ", "-") for r in allowed})
        logger.info(
            "build_corridors_map: filtered by summary (ranks=%s, min_dist=%.1f km) → %d allowed route(s).",
            ranks, min_distance_km, len(allowed_routes),
        )

    df_registry = read_corridor_model_registry(
        routes=allowed_routes,
        registry_path=registry_path,
    )

    if df_registry.empty:
        logger.warning("build_corridors_map: registry returned 0 matching corridor models.")
        return corridors_map

    for _, row in df_registry.iterrows():
        route_id   = str(row.get("route_id") or row.get("route", "")).replace(" -> ", "-")
        cluster_id = int(row["cluster_id"])
        rel_path   = row["file_path"]
        fl_val     = row.get("fl")
        fl         = float(fl_val) if fl_val is not None and not pd.isna(fl_val) else float("nan")

        if corridors_dir is not None:
            abs_path = corridors_dir / Path(rel_path).name
        else:
            abs_path = BASE_DIR / rel_path

        if abs_path.exists():
            corridors_map[(route_id, cluster_id)] = CorridorCluster(path=abs_path, fl=fl)
        else:
            logger.warning("Corridor file missing for %s c%d: %s", route_id, cluster_id, abs_path)

    logger.info("corridors_map: %d entry/entries loaded from registry.", len(corridors_map))
    return corridors_map


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
        raw_callsign = row.get("callsign")
        callsign = str(raw_callsign).strip() if pd.notna(raw_callsign) and raw_callsign else "UNK"

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
                           
            raw_typecode = row.get("typecode")
            typecode = str(raw_typecode).strip() if pd.notna(raw_typecode) and raw_typecode else ""
            candidate_pool.append(FlightCandidate(
                icao24=icao24,
                callsign=callsign,
                dep=dep,
                arr=arr,
                firstseen=firstseen,
                lastseen=lastseen,
                typecode=typecode,
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
    available_clusters: Dict[Tuple[str, int], CorridorCluster],
    strategy: str = "random",
    clusters_per_flight: int = 1,
    **kwargs: Any,
) -> List[SimTask]:
    """Primary Slot 1 API: Convert cohort rows into concrete SimTask objects.

    Encapsulates ``generate_base_flightlist`` and ``select_clusters``.

    Parameters
    ----------
    cohort_df : pd.DataFrame
        One day's slice of master_flights.
    available_clusters : Dict[Tuple[str, int], CorridorCluster]
        Mapping of ``(route_key, cluster_id)`` to ``CorridorCluster(path, fl)``.
    strategy : str
        Cluster selection strategy (default 'random').
    clusters_per_flight : int
        Number of clusters to assign per flight (default 1).
    **kwargs : Any
        Additional strategy-specific keyword arguments.

    Returns
    -------
    List[SimTask]
        Flat list of simulation tasks.
    """
    candidate_pool = generate_base_flightlist(cohort_df, available_clusters)
    return select_clusters(
        candidate_pool=candidate_pool,
        available_clusters=available_clusters,
        strategy=strategy,
        clusters_per_flight=clusters_per_flight,
        **kwargs,
    )


# Backward compatibility aliases
enumerate_cohort = generate_flightlist
generate_tasks = generate_flightlist
