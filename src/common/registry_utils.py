"""
Shared Utility Functions for Pipeline Registries (Trajectory, Stability, and Model Registries).
"""

import logging
from pathlib import Path
import pandas as pd
import numpy as np

from src.common.config import (
    BASE_DIR,
    GLOBAL_TRAJECTORY_REGISTRY,
    GLOBAL_STABILITY_REGISTRY,
    GLOBAL_MODEL_REGISTRY
)

logger = logging.getLogger(__name__)


def load_trajectory_registry(registry_path: Path = None) -> pd.DataFrame:
    """Reads trajectory registry, returning columns ['flight_id', 'file_path']."""
    cols = ["flight_id", "file_path"]
    path = registry_path if registry_path is not None else GLOBAL_TRAJECTORY_REGISTRY
    if not path.exists():
        logger.info(f"Trajectory registry does not exist at {path}. Returning empty registry.")
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_parquet(path)
        return df.reindex(columns=cols)
    except Exception as e:
        logger.error(f"Failed to load trajectory registry: {e}")
        raise


def get_flights_for_route(dep: str, arr: str) -> pd.DataFrame:
    """Filters trajectory registry for flight IDs containing '_{dep}-{arr}_'."""
    df = load_trajectory_registry()
    if df.empty:
        return df
    pattern = f"_{dep}-{arr}_"
    return df[df["flight_id"].str.contains(pattern, na=False)]


def get_flight_metadata(flight_id: str) -> dict:
    """Parses 'icao24', 'callsign', 'route_id', and 'timestamp' from flight_id formatted as 'icao24_callsign_dep-arr_timestamp'."""
    parts = flight_id.split("_", 3)
    return {
        "icao24": parts[0] if len(parts) > 0 else "",
        "callsign": parts[1] if len(parts) > 1 else "",
        "route_id": parts[2] if len(parts) > 2 else "",
        "timestamp": parts[3] if len(parts) > 3 else ""
    }


def load_registered_trajectories(df_registry: pd.DataFrame, columns: list = None):
    """
    Generator that yields (Path, pd.DataFrame) of loaded trajectories grouped by file_path,
    filtered to keep only the registered flight IDs.
    """
    if df_registry.empty:
        return

    for file_path_str, group in df_registry.groupby("file_path"):
        path = Path(file_path_str)
        if not path.is_absolute():
            path = BASE_DIR / path

        if not path.exists():
            logger.warning(f"Trajectory file does not exist: {path}")
            continue

        registered_flight_ids = set(group["flight_id"])

        # Determine columns to load, ensuring flight_id is included for filtering
        load_cols = None
        if columns is not None:
            load_cols = list(columns)
            if "flight_id" not in load_cols:
                load_cols.append("flight_id")

        try:
            df_loaded = pd.read_parquet(path, columns=load_cols)
        except Exception as e:
            logger.error(f"Failed to read parquet file {path}: {e}")
            continue

        if "flight_id" not in df_loaded.columns:
            logger.warning(f"flight_id column not found in trajectory file {path}")
            continue

        df_filtered = df_loaded[df_loaded["flight_id"].isin(registered_flight_ids)]

        # Drop flight_id if it wasn't requested
        if columns is not None and "flight_id" not in columns:
            df_filtered = df_filtered.drop(columns=["flight_id"])

        yield path, df_filtered


def load_stability_registry() -> pd.DataFrame:
    """Reads GLOBAL_STABILITY_REGISTRY, returning columns ['route_id', 'N_current', 'pca_mean_vector', 'pca_variance', 'delta_cv', 'needs_resample', 'is_uptodate']."""
    cols = ["route_id", "N_current", "pca_mean_vector", "pca_variance", "delta_cv", "needs_resample", "is_uptodate"]
    if not GLOBAL_STABILITY_REGISTRY.exists():
        logger.info(f"Stability registry does not exist at {GLOBAL_STABILITY_REGISTRY}. Returning empty registry.")
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_parquet(GLOBAL_STABILITY_REGISTRY)
        return df.reindex(columns=cols)
    except Exception as e:
        logger.error(f"Failed to load stability registry: {e}")
        raise


def save_stability_registry(df: pd.DataFrame):
    """Writes to GLOBAL_STABILITY_REGISTRY."""
    try:
        GLOBAL_STABILITY_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(GLOBAL_STABILITY_REGISTRY, index=False)
    except Exception as e:
        logger.error(f"Failed to save stability registry: {e}")
        raise


def update_stability_record(route_id: str, updates: dict):
    """Updates/inserts stability record, converting numpy arrays for pca_mean_vector and pca_variance into lists for parquet compatibility."""
    df = load_stability_registry()

    # Remove existing record if present
    df = df[df["route_id"] != route_id]

    new_record = {"route_id": route_id}
    for k, v in updates.items():
        if isinstance(v, np.ndarray):
            new_record[k] = v.tolist()
        else:
            new_record[k] = v

    # Explicitly convert pca_mean_vector and pca_variance if they are numpy arrays
    for key in ["pca_mean_vector", "pca_variance"]:
        if key in new_record and isinstance(new_record[key], np.ndarray):
            new_record[key] = new_record[key].tolist()

    df_new = pd.DataFrame([new_record])
    if df.empty:
        df_updated = df_new
    else:
        df_updated = pd.concat([df, df_new], ignore_index=True)

    save_stability_registry(df_updated)
    logger.info(f"Updated stability record for route {route_id}")


def batch_update_stability_registry(records: list) -> None:
    """
    Upserts a list of stability records in a single read-modify-write cycle.

    Far more efficient than calling ``update_stability_record`` per route when
    processing thousands of routes in parallel -- performs exactly one parquet
    read and one parquet write regardless of batch size.

    Parameters
    ----------
    records : list[dict]
        Each dict must contain ``route_id`` plus any subset of the stability
        schema columns.  Numpy arrays in ``pca_mean_vector`` / ``pca_variance``
        are automatically converted to lists for parquet compatibility.

    Notes
    -----
    Not thread-safe for concurrent writes.  The orchestrator must call this
    from a single result-collector thread / the main process only.
    """
    if not records:
        return

    df = load_stability_registry()

    # Collect incoming route_ids for bulk removal
    incoming_ids = {r["route_id"] for r in records if r.get("route_id")}
    df = df[~df["route_id"].isin(incoming_ids)]

    # Normalise each record: convert numpy arrays → lists
    clean_records = []
    for rec in records:
        clean = dict(rec)
        for key in ("pca_mean_vector", "pca_variance"):
            if key in clean and isinstance(clean[key], np.ndarray):
                clean[key] = clean[key].tolist()
        clean_records.append(clean)

    df_new = pd.DataFrame(clean_records)
    df_updated = pd.concat([df, df_new], ignore_index=True) if not df.empty else df_new

    save_stability_registry(df_updated)
    logger.info(f"Batch-updated stability registry: {len(clean_records)} records written.")


def get_stability_record(route_id: str) -> dict:
    """Retrieves a single route's stability record or None."""
    df = load_stability_registry()
    if df.empty:
        return None
    df_route = df[df["route_id"] == route_id]
    if df_route.empty:
        return None
    return df_route.iloc[0].to_dict()


def load_model_registry() -> pd.DataFrame:
    """
    Reads GLOBAL_MODEL_REGISTRY.

    Schema (one row per corridor = unique route_id + cluster_id):
        route_id                  : str   e.g. 'EGLL-EDDF'
        cluster_id                : int   0 to optimal_k-1
        optimal_k                 : int   best k found
        silhouette_score          : float best silhouette (NaN if k=1)
        cluster_size              : int   number of flights in this cluster
        medoid_historical_flight_id: str  original flight_id chosen as medoid
        corridor_flight_id        : str   e.g. 'EGLL-EDDF_corridor_c0'
        file_path                 : str   posix path to corridor .parquet
        route_class               : int   1=Single 2=Binary 3=Multi 4=Chaos
    """
    cols = [
        "route_id", "cluster_id", "optimal_k", "silhouette_score",
        "cluster_size", "medoid_historical_flight_id", "corridor_flight_id",
        "file_path", "route_class",
    ]
    if not GLOBAL_MODEL_REGISTRY.exists():
        logger.info(f"Model registry does not exist at {GLOBAL_MODEL_REGISTRY}. Returning empty registry.")
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_parquet(GLOBAL_MODEL_REGISTRY)
        # Back-compat: old records may have 'route' but not 'route_id'
        if "route" in df.columns and "route_id" not in df.columns:
            df = df.rename(columns={"route": "route_id"})
        return df.reindex(columns=cols)
    except Exception as e:
        logger.error(f"Failed to load model registry: {e}")
        raise


def save_model_registry(df: pd.DataFrame):
    """Writes to GLOBAL_MODEL_REGISTRY."""
    try:
        GLOBAL_MODEL_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(GLOBAL_MODEL_REGISTRY, index=False)
    except Exception as e:
        logger.error(f"Failed to save model registry: {e}")
        raise


def register_corridors(
    route_id: str,
    optimal_k: int,
    silhouette_score: float,
    route_class: int,
    corridors: list,
) -> None:
    """
    Upserts corridor records for a single route (single read-modify-write).

    Each dict in ``corridors`` must contain:
        cluster_id, cluster_size, medoid_historical_flight_id,
        corridor_flight_id, file_path.
    """
    df = load_model_registry()
    df = df[df["route_id"] != route_id]

    new_entries = []
    for corr in corridors:
        file_path_val = corr.get("file_path")
        if file_path_val is not None:
            file_path_val = Path(file_path_val).as_posix()
        entry = {
            "route_id": route_id,
            "cluster_id": corr.get("cluster_id"),
            "optimal_k": optimal_k,
            "silhouette_score": silhouette_score,
            "cluster_size": corr.get("cluster_size", 0),
            "medoid_historical_flight_id": corr.get("medoid_historical_flight_id"),
            "corridor_flight_id": corr.get("corridor_flight_id"),
            "file_path": file_path_val,
            "route_class": route_class,
        }
        new_entries.append(entry)

    df_new = pd.DataFrame(new_entries)
    df_updated = pd.concat([df, df_new], ignore_index=True) if not df.empty else df_new
    save_model_registry(df_updated)
    logger.info(
        f"Registered {len(corridors)} corridors for route {route_id} "
        f"(k_opt={optimal_k}, sil={silhouette_score:.4f}, class={route_class})."
    )


def batch_register_corridors(route_results: list) -> None:
    """
    Upserts corridor records for many routes in a single read-modify-write.

    Parameters
    ----------
    route_results : list[dict]
        Each dict is the return value of ``cluster_route()``.  Must contain:
        ``route_id``, ``optimal_k``, ``silhouette_score``, ``route_class``,
        and ``corridors`` (list of per-cluster dicts as in ``register_corridors``).

    Notes
    -----
    Not thread-safe for concurrent writers. Call only from the main process
    after collecting results from a worker pool.
    """
    if not route_results:
        return

    df = load_model_registry()
    incoming_ids = {r["route_id"] for r in route_results if r.get("route_id")}
    df = df[~df["route_id"].isin(incoming_ids)]

    new_entries = []
    for res in route_results:
        route_id = res["route_id"]
        optimal_k = res["optimal_k"]
        silhouette_score = res["silhouette_score"]
        route_class = res["route_class"]
        for corr in res.get("corridors", []):
            file_path_val = corr.get("file_path")
            if file_path_val is not None:
                file_path_val = Path(file_path_val).as_posix()
            new_entries.append({
                "route_id": route_id,
                "cluster_id": corr.get("cluster_id"),
                "optimal_k": optimal_k,
                "silhouette_score": silhouette_score,
                "cluster_size": corr.get("cluster_size", 0),
                "medoid_historical_flight_id": corr.get("medoid_historical_flight_id"),
                "corridor_flight_id": corr.get("corridor_flight_id"),
                "file_path": file_path_val,
                "route_class": route_class,
            })

    if not new_entries:
        return

    df_new = pd.DataFrame(new_entries)
    df_updated = pd.concat([df, df_new], ignore_index=True) if not df.empty else df_new
    save_model_registry(df_updated)
    logger.info(f"Batch-registered corridors: {len(new_entries)} records across {len(route_results)} routes.")


def load_synthesized_paths_map() -> dict[tuple[str, int], Path]:
    """Maps (route_id, cluster_id) -> absolute Path of corridor file."""
    df = load_model_registry()
    paths_map = {}
    if df.empty:
        return paths_map

    for _, row in df.iterrows():
        route_id = row.get("route_id") if pd.notna(row.get("route_id")) else row.get("route")
        if pd.isna(route_id) or not route_id:
            continue

        try:
            cluster_id = int(row["cluster_id"])
        except (ValueError, TypeError):
            continue

        file_path_str = row["file_path"]
        if pd.isna(file_path_str) or not file_path_str:
            continue

        path = Path(file_path_str)
        if not path.is_absolute():
            path = BASE_DIR / path

        paths_map[(route_id, cluster_id)] = path

    return paths_map
