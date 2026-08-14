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

from src.common.config import (
    BASE_DIR,
    GLOBAL_CORRIDOR_MODEL_REGISTRY,
)
from src.common.utils import load_route_summary
from src.data_manager.schemas import CorridorCluster, SimTask

logger = logging.getLogger(__name__)


def build_corridors_map(
    ranks: Optional[List[int]] = None,
    registry_path: Optional[Path] = None,
    corridors_dir: Optional[Path] = None,
) -> Dict[Tuple[str, int], CorridorCluster]:
    """Build the (route_id, cluster_id) -> CorridorCluster map from registry.

    Reads ``file_path``, ``cluster_id``, and ``fl`` from the model registry.
    If ``ranks`` is supplied, filters routes to those ranks in ``route_summary``.

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
    reg = registry_path or GLOBAL_CORRIDOR_MODEL_REGISTRY
    corridors_map: Dict[Tuple[str, int], CorridorCluster] = {}

    if not Path(reg).exists():
        logger.warning("GLOBAL_CORRIDOR_MODEL_REGISTRY not found: %s", reg)
        return corridors_map

    df = pd.read_parquet(reg)

    # Filter by rank if requested
    if ranks is not None:
        df_summary = load_route_summary()
        allowed = df_summary[df_summary["rank"].isin(ranks)]["route"]
        allowed_ids = set(r.replace(" -> ", "-") for r in allowed)
        route_col = "route_id" if "route_id" in df.columns else "route"
        df = df[df[route_col].isin(allowed_ids)]
        logger.info(
            "build_corridors_map: filtered by ranks %s → %d route row(s) in registry.",
            ranks, len(df),
        )

    for _, row in df.iterrows():
        route_id   = str(row.get("route_id") or row.get("route", ""))
        cluster_id = int(row["cluster_id"])
        rel_path   = row["file_path"]
        fl_val     = row.get("fl")
        fl         = float(fl_val) if fl_val is not None and not pd.isna(fl_val) else float("nan")

        if corridors_dir is not None:
            # If a custom corridors directory was provided, resolve filename inside it
            abs_path = corridors_dir / Path(rel_path).name
        else:
            abs_path = BASE_DIR / rel_path

        if abs_path.exists():
            corridors_map[(route_id, cluster_id)] = CorridorCluster(path=abs_path, fl=fl)
        else:
            logger.warning("Corridor file missing for %s c%d: %s", route_id, cluster_id, abs_path)

    logger.info("corridors_map: %d entry/entries loaded from registry.", len(corridors_map))
    return corridors_map


def generate_flightlist(
    cohort_df: pd.DataFrame,
    available_clusters: Dict[Tuple[str, int], Any],
    clusters_per_flight: int = 1,
) -> List[SimTask]:
    """Generate candidate SimTask objects from a daily flight cohort DataFrame.

    One baseline SimTask is produced per (flight row × sampled cluster_id) pair.
    The flight level (FL) is obtained strictly from the CorridorCluster metadata
    calibrated in the corridor model registry. If a cluster has missing or
    invalid FL data, the flight is skipped and logged with an error rather than
    filled with an arbitrary default.

    Parameters
    ----------
    cohort_df : pd.DataFrame
        One day's slice of master_flights. Required columns:
        ``icao24, callsign, firstseen, lastseen,
        estdepartureairport, estarrivalairport, typecode``.
    available_clusters : Dict[Tuple[str, int], CorridorCluster]
        Mapping of ``(route_key, cluster_id)`` to ``CorridorCluster(path, fl)``.
        Produced by build_corridors_map().
    clusters_per_flight : int, optional
        Number of clusters to sample per flight (default 1).

    Returns
    -------
    List[SimTask]
        Flat list of SimTask objects, one per (flight, cluster) pair.
        Empty if no clusters are available for any flight in the cohort.
    """
    tasks: List[SimTask] = []
    allowed_routes = {r for (r, _) in available_clusters}

    for _, row in cohort_df.iterrows():
        dep = row["estdepartureairport"]
        arr = row["estarrivalairport"]
        route_key = f"{dep}-{arr}"

        icao24 = row["icao24"]
        callsign = row.get("callsign", "UNK") or "UNK"

        # firstseen / lastseen are expected as epoch integers (seconds UTC).
        firstseen = int(row["firstseen"].timestamp()
                        if hasattr(row["firstseen"], "timestamp")
                        else row["firstseen"])
        lastseen = int(row["lastseen"].timestamp()
                       if hasattr(row["lastseen"], "timestamp")
                       else row["lastseen"])

        available = [cid for (r, cid) in available_clusters if r == route_key]
        if not available:
            # Only warn if this flight was on an explicitly requested/allowed route
            if route_key in allowed_routes:
                logger.warning(
                    "No synthesized base paths for route %s — skipping flight %s/%s.",
                    route_key, icao24, callsign,
                )
            continue

        sample_size = min(clusters_per_flight, len(available))
        sampled = np.random.choice(available, size=sample_size, replace=False)

        for cluster_id in sampled:
            cluster_entry = available_clusters.get((route_key, cluster_id))
            if cluster_entry is None:
                logger.warning(
                    "Cluster entry (%s, %d) not found in available_clusters — skipping flight %s/%s.",
                    route_key, cluster_id, icao24, callsign,
                )
                continue

            fl = getattr(cluster_entry, "fl", None)
            if fl is None or np.isnan(fl) or fl <= 0:
                logger.error(
                    "Cluster (%s, %d) has missing or invalid FL (%s) — skipping flight %s/%s. "
                    "Verify GLOBAL_CORRIDOR_MODEL_REGISTRY before running.",
                    route_key, cluster_id, fl, icao24, callsign,
                )
                continue

            tasks.append(SimTask(
                icao24=icao24,
                callsign=callsign,
                dep=dep,
                arr=arr,
                firstseen=firstseen,
                lastseen=lastseen,
                typecode=str(row.get("typecode", "") or ""),
                cluster_id=int(cluster_id),
                fl=float(fl),
            ))

    logger.info(
        "Slot 1 (Flight List Generation) generated %d tasks from %d cohort rows.",
        len(tasks), len(cohort_df),
    )
    return tasks


# Backward compatibility aliases
enumerate_cohort = generate_flightlist
generate_tasks = generate_flightlist
