from __future__ import annotations
import logging
import time
from pathlib import Path
import numpy as np
import pandas as pd
from pycontrails import Flight

from src.common.config import (
    BASE_DIR,
    CORRIDOR_PATHS_DIR,
    CLUSTERING_MAX_K,
    SILHOUETTE_THRESHOLD,
    CHAOS_VARIANCE_THRESHOLD,
    UNSUPPORTED_TYPECODE_FLAG,
    is_supported_typecode,
)
from src.common.adapters import (
    dataframe_to_pycontrails,
    pycontrails_to_parquet,
)
from src.common.utils import log_skipped_aircraft
from src.core.corridor.corridor_clustering_engine import run_clustering

logger = logging.getLogger(__name__)


def _worker_init(threads_per_worker: int) -> None:
    """
    Initializes a process pool worker by configuring the shared logger and restricting
    BLAS/Lapack numeric thread counts to prevent thread oversubscription.
    """
    from src.common.utils import setup_file_logger
    from src.common.concurrency import limit_numeric_threads
    setup_file_logger(log_filename="corridor.log")
    limit_numeric_threads(threads_per_worker)


def _load_cohort_flights(cohort_rows: list[dict]) -> list[pd.DataFrame]:
    """
    Loads flight DataFrames from their respective parquet files.
    Groups by file path to minimize I/O overhead.

    Parameters
    ----------
    cohort_rows : list[dict]
        List of dictionaries containing 'file_path' and 'flight_id'.

    Returns
    -------
    list[pd.DataFrame]
        List of DataFrames matching the order of input cohort_rows.
    """
    from collections import defaultdict
    # Group flight_ids by their file paths to read each file only once
    file_to_flights = defaultdict(list)
    for row in cohort_rows:
        file_to_flights[row["file_path"]].append(row["flight_id"])

    loaded_flights = {}
    for file_path, flight_ids in file_to_flights.items():
        try:
            df = pd.read_parquet(file_path)
            # Filter and split by flight_id
            for fid in flight_ids:
                df_fid = df[df["flight_id"] == fid]
                if df_fid.empty:
                    logger.warning(f"Flight ID {fid} not found in file {file_path}")
                    continue
                loaded_flights[fid] = df_fid
        except Exception as e:
            logger.error(f"Failed to load flights from {file_path}: {e}")

    # Return in the original order specified by cohort_rows
    ordered_dfs = []
    for row in cohort_rows:
        fid = row["flight_id"]
        if fid in loaded_flights:
            ordered_dfs.append(loaded_flights[fid])

    return ordered_dfs


def cluster_route(
    route_id: str,
    rank: int,
    cohort_rows: list[dict],
    time_grid_seconds: int = 60,
) -> dict:
    """
    Worker task to perform clustering and corridor templates generation for a single route.

    Parameters
    ----------
    route_id : str
        The unique ID of the route cohort (e.g. '001_LEPA-LEBL' or 'LEPA-LEBL').
    rank : int
        The traffic rank of the route.
    cohort_rows : list[dict]
        List of dictionaries specifying the registered flights to load.
    time_grid_seconds : int
        The temporal spacing of the resampled reference parquets.

    Returns
    -------
    dict
        Structured outcome dictionary containing cluster metrics, paths, and mappings.
    """
    t_start = time.perf_counter()

    # Parse departure and arrival ICAO codes from the route_id
    if "_" in route_id:
        route_part = route_id.split("_", 1)[1]
    else:
        route_part = route_id
        
    if "-" in route_part:
        dep, arr = route_part.split("-", 1)
    else:
        dep, arr = "UNK", "UNK"

    logger.info(f"Route {route_id} (Rank {rank}): Starting clustering for {len(cohort_rows)} flights...")

    _NULL_RESULT = {
        "route_id": route_id,
        "status": "error",
        "optimal_k": 0,
        "silhouette_score": None,
        "route_class": 0,
        "corridors": [],
        "flight_mappings": [],
        "error_msg": None,
        "timing_seconds": 0.0,
    }

    # 1. Load DataFrames
    try:
        flight_dfs = _load_cohort_flights(cohort_rows)
    except Exception as exc:
        res = dict(_NULL_RESULT)
        res["error_msg"] = f"Failed to load cohort flights: {exc}"
        logger.error(f"Route {route_id}: {res['error_msg']}")
        res["timing_seconds"] = round(time.perf_counter() - t_start, 3)
        return res

    if not flight_dfs:
        res = dict(_NULL_RESULT)
        res["status"] = "insufficient_data"
        res["error_msg"] = "No flight DataFrames could be successfully loaded."
        logger.error(f"Route {route_id}: {res['error_msg']}")
        res["timing_seconds"] = round(time.perf_counter() - t_start, 3)
        return res

    # 2. Call the math engine to cluster the cohort
    try:
        clustering_res = run_clustering(
            flight_dfs=flight_dfs,
            n_pca_components=13,
            k_max=CLUSTERING_MAX_K,
            silhouette_threshold=SILHOUETTE_THRESHOLD,
            chaos_variance_threshold=CHAOS_VARIANCE_THRESHOLD,
        )
    except Exception as exc:
        res = dict(_NULL_RESULT)
        res["error_msg"] = f"Clustering math engine failed: {exc}"
        logger.error(f"Route {route_id}: {res['error_msg']}")
        res["timing_seconds"] = round(time.perf_counter() - t_start, 3)
        return res

    # 3. For each medoid index, shift baseline time, resample, and save reference parquet
    corridors = []
    medoid_flight_ids = set()

    for cluster_id in range(clustering_res.k):
        medoid_idx = clustering_res.medoid_indices[cluster_id]
        medoid_df = flight_dfs[medoid_idx]

        medoid_fid = medoid_df["flight_id"].iloc[0] if "flight_id" in medoid_df.columns else f"idx_{medoid_idx}"
        medoid_flight_ids.add(medoid_fid)

        tc = medoid_df["typecode"].iloc[0] if "typecode" in medoid_df.columns else UNSUPPORTED_TYPECODE_FLAG

        # Strict Aircraft Typecode Verification
        if not is_supported_typecode(tc):
            log_skipped_aircraft(
                medoid_fid,
                tc,
                "ERROR_FLAG: Medoid has missing, NaN, or non-target family typecode"
            )
            res = dict(_NULL_RESULT)
            res["error_msg"] = f"Medoid flight {medoid_fid} has unsupported aircraft typecode: {tc}"
            logger.error(f"Route {route_id}: {res['error_msg']}")
            res["timing_seconds"] = round(time.perf_counter() - t_start, 3)
            return res

        corridor_flight_id = f"{dep}-{arr}_corridor_c{cluster_id}"
        out_path = CORRIDOR_PATHS_DIR / f"{dep}-{arr}_corridor_c{cluster_id}.parquet"

        try:
            # Convert DF (already in SI units) to pycontrails.Flight
            pyc_flight = dataframe_to_pycontrails(medoid_df, typecode=tc)
            if pyc_flight is None:
                raise ValueError("dataframe_to_pycontrails returned None")

            # Resample waypoints to target time grid (60s default)
            resampled = pyc_flight.resample_and_fill(freq=f"{time_grid_seconds}s")

            # Shift start time to standard baseline: 2025-01-01 00:00:00 UTC
            df_final = resampled.to_dataframe()
            delta = df_final["time"] - df_final["time"].min()
            df_final["time"] = pd.Timestamp("2025-01-01 00:00:00") + delta
            df_final["route_class"] = clustering_res.route_class
            df_final["cluster_id"] = cluster_id

            # Re-inject static metadata attributes
            attrs = {
                "flight_id": corridor_flight_id,
                "aircraft_type": tc,
                "icao24": "MEDOID",
                "callsign": "MEDOID",
                "route_class": clustering_res.route_class,
                "cluster_id": cluster_id,
                "optimal_k": clustering_res.k,
            }

            final_flight = Flight(data=df_final, crs="EPSG:4326", **attrs)
            pycontrails_to_parquet(final_flight, out_path)

            try:
                rel_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
            except ValueError:
                rel_path = out_path.resolve().as_posix()

            corridors.append({
                "cluster_id": cluster_id,
                "cluster_size": int(np.sum(clustering_res.labels == cluster_id)),
                "medoid_historical_flight_id": medoid_fid,
                "corridor_flight_id": corridor_flight_id,
                "file_path": rel_path,
            })
            logger.info(f"Route {route_id} cluster {cluster_id}: Saved corridor template to {rel_path}")

        except Exception as exc:
            res = dict(_NULL_RESULT)
            res["error_msg"] = f"Failed to process and save corridor for medoid {medoid_fid}: {exc}"
            logger.error(f"Route {route_id}: {res['error_msg']}")
            res["timing_seconds"] = round(time.perf_counter() - t_start, 3)
            return res

    # 4. Compile flight cluster mappings
    flight_mappings = []
    for idx, df in enumerate(flight_dfs):
        fid = df["flight_id"].iloc[0] if "flight_id" in df.columns else f"idx_{idx}"
        cluster_id = int(clustering_res.labels[idx])
        is_medoid = fid in medoid_flight_ids
        flight_mappings.append({
            "flight_id": fid,
            "route_id": route_id,
            "cluster_id": cluster_id,
            "route_class": clustering_res.route_class,
            "is_medoid": is_medoid,
        })

    timing = time.perf_counter() - t_start
    logger.info(f"Route {route_id} (Rank {rank}): Clustering complete in {timing:.2f}s ({len(corridors)} corridors)")

    return {
        "route_id": route_id,
        "status": "ok",
        "optimal_k": clustering_res.k,
        "silhouette_score": float(clustering_res.silhouette_score) if pd.notna(clustering_res.silhouette_score) else None,
        "route_class": clustering_res.route_class,
        "corridors": corridors,
        "flight_mappings": flight_mappings,
        "error_msg": None,
        "timing_seconds": round(timing, 3),
    }
