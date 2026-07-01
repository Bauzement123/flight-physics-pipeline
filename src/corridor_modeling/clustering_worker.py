"""
Module: Clustering Worker
=========================
Pure, picklable per-route clustering logic for the Stage 3 Model pipeline.
Designed to run inside a ``ProcessPoolExecutor`` worker process.

Responsibilities per route:
    1. Load the converged flight cohort from the trajectory registry.
    2. Classify & normalise (ROCD + holding-pattern), tracking is_clean_flags.
    3. Vectorize → Z-score → fit a fresh per-route PCA.
    4. Evaluate K-Means for k ∈ [1, CLUSTERING_MAX_K] using Silhouette score.
    5. For each cluster, identify the closest *originally-clean* flight to the
       centroid in PCA space as the representative medoid.
       Fallback: closest flight regardless of clean status if no clean flights
       exist in a cluster.
    6. Save each medoid as a time-normalised corridor parquet template.
    7. Return a structured result dict for the orchestrator to batch-register.

Top-level public API
--------------------
    cluster_route(route_id, registry_df, d_pca,
                  time_grid_seconds, overwrite) -> dict

Modular by design: ``cluster_route`` can be called directly by a unified
streaming pipeline (Option B, Step 6) without any changes.
"""

import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pycontrails import Flight
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from src.common.config import (
    BASE_DIR,
    CORRIDOR_PATHS_DIR,
    CLUSTERING_MAX_K,
    SILHOUETTE_THRESHOLD,
    CHAOS_VARIANCE_THRESHOLD,
    MIN_FLIGHTS_FOR_CLUSTERING,
)
from src.common.adapters import parquet_to_pycontrails, pycontrails_to_traffic, pycontrails_to_parquet, traffic_to_pycontrails
from src.corridor_modeling.pca_compressor import (
    classify_and_normalize_cohort,
    vectorize_cohort,
    normalize_vectors,
    fit_pca,
    apply_pca,
)

from typing import Optional

logger = logging.getLogger(__name__)

_MIN_FLIGHT_ROWS = 10


# ===========================================================================
# Internal helpers
# ===========================================================================

def _load_cohort(
    route_id: str,
    registry_df: pd.DataFrame,
) -> list:
    """
    Loads all valid airborne TrafficFlight objects for a route from the
    trajectory registry.  Corrupt/missing files are skipped gracefully.

    Returns list of TrafficFlight (may be empty).
    """
    pattern = f"_{route_id}_"
    matched = registry_df[registry_df["flight_id"].str.contains(pattern, na=False)]
    if matched.empty:
        logger.warning(f"No registry entries found for route {route_id}.")
        return []

    id_to_file = dict(zip(matched["flight_id"], matched["file_path"]))
    file_to_ids: dict = {}
    for fid, fp in id_to_file.items():
        file_to_ids.setdefault(fp, []).append(fid)

    flights = []
    for rel_path, fids in file_to_ids.items():
        abs_path = BASE_DIR / rel_path if not Path(rel_path).is_absolute() else Path(rel_path)
        if not abs_path.exists():
            logger.warning(f"Trajectory file missing: {abs_path}")
            continue
        try:
            flights_dict = parquet_to_pycontrails(str(abs_path))
        except Exception as exc:
            logger.warning(f"Skipping corrupted parquet {abs_path}: {exc}")
            continue
        for fid in fids:
            if fid not in flights_dict:
                continue
            try:
                trf = pycontrails_to_traffic(flights_dict[fid])
                airborne = trf.airborne()
                if airborne is not None and len(airborne.data) >= _MIN_FLIGHT_ROWS:
                    flights.append(airborne)
            except Exception as exc:
                logger.debug(f"Skipping flight {fid}: {exc}")

    return flights


def _evaluate_optimal_k(
    X_pca: np.ndarray,
) -> tuple:
    """
    Evaluates K-Means for k ∈ [2, min(CLUSTERING_MAX_K, N-1)] using
    Silhouette score.  Returns the best (k, labels, silhouette, route_class).

    Parameters
    ----------
    X_pca : np.ndarray, shape (N, d_pca)

    Returns
    -------
    best_k         : int
    best_labels    : np.ndarray, shape (N,)
    best_silhouette: float   (NaN if k forced to 1)
    route_class    : int     1=Single, 2=Binary, 3=Multi, 4=Chaos
    """
    n = len(X_pca)
    best_k = 1
    best_labels = np.zeros(n, dtype=int)
    best_silhouette = float("nan")

    max_k = min(CLUSTERING_MAX_K, n - 1)
    if max_k >= 2:
        best_score = -1.0
        for k in range(2, max_k + 1):
            try:
                km = KMeans(n_clusters=k, random_state=42, n_init="auto")
                labels = km.fit_predict(X_pca)
                score = silhouette_score(X_pca, labels)
                logger.debug(f"  k={k}  silhouette={score:.4f}")
                if score > best_score and score >= SILHOUETTE_THRESHOLD:
                    best_k = k
                    best_labels = labels
                    best_score = score
                    best_silhouette = score
            except Exception as exc:
                logger.warning(f"KMeans k={k} failed: {exc}")

    # Determine route class
    if best_k == 1:
        # Measure total spread of PCA coordinates (raw variance sum, not
        # physical units — same scale for all routes)
        total_variance = float(np.var(X_pca, axis=0).sum())
        if total_variance > CHAOS_VARIANCE_THRESHOLD:
            route_class = 4  # Chaos: high variance, no clear clusters
            logger.info(f"  route_class=4 (Chaos) total_var={total_variance:.2f}")
        else:
            route_class = 1  # Single baseline
            logger.info(f"  route_class=1 (Single) total_var={total_variance:.2f}")
    elif best_k == 2:
        route_class = 2  # Binary split
        logger.info(f"  route_class=2 (Binary) k=2 sil={best_silhouette:.4f}")
    else:
        route_class = 3  # Multi-track
        logger.info(f"  route_class=3 (Multi) k={best_k} sil={best_silhouette:.4f}")

    return best_k, best_labels, best_silhouette, route_class


def _select_medoid(
    X_pca: np.ndarray,
    cluster_mask: np.ndarray,
    is_clean_flags: list,
) -> int:
    """
    Selects the representative medoid index within a cluster.

    Strategy:
    1. Compute cluster centroid in PCA space.
    2. Among flights where ``is_clean_flags[i] == True``, find the one with
       the minimum Euclidean distance to the centroid.
    3. Fallback: if no clean flights exist in the cluster, use all flights.

    Parameters
    ----------
    X_pca         : np.ndarray, shape (N, d_pca)  full PCA matrix
    cluster_mask  : np.ndarray bool, shape (N,)    True for members of this cluster
    is_clean_flags: list[bool], length N

    Returns
    -------
    int  index into the full arrays (X_pca, flights, is_clean_flags)
    """
    cluster_indices = np.where(cluster_mask)[0]
    centroid = X_pca[cluster_mask].mean(axis=0)
    distances = np.linalg.norm(X_pca[cluster_mask] - centroid, axis=1)

    # Try clean flights first
    clean_local = np.array([is_clean_flags[i] for i in cluster_indices])
    if clean_local.any():
        # Mask distances to clean flights only
        distances_clean = np.where(clean_local, distances, np.inf)
        local_best = int(np.argmin(distances_clean))
    else:
        logger.warning("No clean flights in cluster — falling back to closest dirty flight.")
        local_best = int(np.argmin(distances))

    return int(cluster_indices[local_best])


def _save_corridor(
    flight,
    dep: str,
    arr: str,
    cluster_id: int,
    route_class: int,
    optimal_k: int,
    time_grid_seconds: int,
) -> tuple:
    """
    Time-normalises the medoid flight, resamples to a uniform grid, and saves
    as a corridor `.parquet` template.

    Returns
    -------
    (corridor_flight_id, abs_file_path)
    """
    corridor_flight_id = f"{dep}-{arr}_corridor_c{cluster_id}"
    out_path = CORRIDOR_PATHS_DIR / f"{dep}-{arr}_corridor_c{cluster_id}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build a minimal pycontrails Flight from the TrafficFlight data using the unified adapter
    attrs = {
        "flight_id": corridor_flight_id,
        "icao24": "MEDOID",
        "callsign": "MEDOID",
        "route_class": route_class,
        "cluster_id": cluster_id,
        "optimal_k": optimal_k,
    }

    try:
        pyc_flight = traffic_to_pycontrails(flight, typecode="B738", drop_kinematics=True, **attrs)
        resampled = pyc_flight.resample_and_fill(freq=f"{time_grid_seconds}s")

        # Timeline normalisation: shift start to 2025-01-01 00:00:00 UTC
        df_final = resampled.to_dataframe()
        delta = df_final["time"] - df_final["time"].min()
        df_final["time"] = pd.Timestamp("2025-01-01 00:00:00") + delta
        df_final["route_class"] = route_class
        df_final["cluster_id"] = cluster_id

        final_flight = Flight(data=df_final, crs="EPSG:4326", **attrs)
        pycontrails_to_parquet(final_flight, out_path)
        logger.info(f"  Saved corridor: {out_path.name}")
    except Exception as exc:
        logger.error(f"  Failed to save corridor {corridor_flight_id}: {exc}")
        raise

    return corridor_flight_id, out_path


# ===========================================================================
# Public helpers for the streaming pipeline (Step 6)
# ===========================================================================

def load_and_classify_cohort(
    route_id: str,
    registry_df: pd.DataFrame,
) -> Optional[tuple]:
    """
    Steps 1-2 of the clustering pipeline: load flights from the trajectory
    registry, classify & normalise, then vectorize and Z-score.

    Separating this from PCA+clustering allows the streaming pipeline to
    perform a ΔRSD stability check on the Z-scored vectors before committing
    to the full PCA + K-Means pass.

    Parameters
    ----------
    route_id    : str  e.g. ``"EGLL-EDDF"``
    registry_df : pd.DataFrame  global trajectory registry (read-only)

    Returns
    -------
    (normalized_flights, is_clean_flags, X_scaled) on success, or None if
    there are insufficient flights after any processing step.
    """
    try:
        flights = _load_cohort(route_id, registry_df)
    except Exception as exc:
        logger.error(f"Route {route_id}: cohort load failed: {exc}")
        return None

    if len(flights) < MIN_FLIGHTS_FOR_CLUSTERING:
        logger.warning(
            f"Route {route_id}: only {len(flights)} flights loaded "
            f"(need >= {MIN_FLIGHTS_FOR_CLUSTERING})"
        )
        return None

    try:
        normalized_flights, is_clean_flags = classify_and_normalize_cohort(flights)
    except Exception as exc:
        logger.error(f"Route {route_id}: normalisation failed: {exc}")
        return None

    if len(normalized_flights) < MIN_FLIGHTS_FOR_CLUSTERING:
        logger.warning(
            f"Route {route_id}: only {len(normalized_flights)} flights after normalisation."
        )
        return None

    try:
        X = vectorize_cohort(normalized_flights)
        X_scaled, _mean, _std = normalize_vectors(X)
    except Exception as exc:
        logger.error(f"Route {route_id}: vectorization failed: {exc}")
        return None

    logger.info(
        f"Route {route_id}: loaded {len(normalized_flights)} clean flights, "
        f"{sum(is_clean_flags)} clean-flagged, X_scaled shape={X_scaled.shape}"
    )
    return normalized_flights, is_clean_flags, X_scaled


def cluster_from_prepared(
    route_id: str,
    dep: str,
    arr: str,
    normalized_flights: list,
    is_clean_flags: list,
    X_scaled: np.ndarray,
    d_pca: int,
    time_grid_seconds: int = 60,
) -> dict:
    """
    Steps 3-5 of the clustering pipeline: fit PCA on the Z-scored feature
    matrix, run K-Means hyperparameter search, select medoids per cluster,
    and save corridor parquet files.

    Intended for use by the streaming pipeline where classify has already
    been done (and a ΔRSD stability check has passed).

    Parameters
    ----------
    route_id          : str
    dep, arr          : str   ICAO codes
    normalized_flights: list  TrafficFlight objects from load_and_classify_cohort
    is_clean_flags    : list[bool]
    X_scaled          : np.ndarray, shape (N, n_features)  Z-scored vectors
    d_pca             : int  PCA dimensionality
    time_grid_seconds : int

    Returns
    -------
    dict matching the cluster_route return schema (route_id, status, optimal_k,
    silhouette_score, route_class, corridors, error_msg, timing_seconds).
    """
    t_start = time.perf_counter()

    _NULL = dict(
        route_id=route_id, status="error", optimal_k=0,
        silhouette_score=float("nan"), route_class=0,
        corridors=[], error_msg=None, timing_seconds=0.0,
    )

    # Step 3: Fit PCA + project
    try:
        t0 = time.perf_counter()
        pca_model = fit_pca(X_scaled, n_components=d_pca)
        X_pca = apply_pca(pca_model, X_scaled)
        logger.info(
            f"Route {route_id}: PCA projected {len(X_pca)} flights to {d_pca}D "
            f"in {time.perf_counter()-t0:.2f}s"
        )
    except Exception as exc:
        r = dict(_NULL)
        r["error_msg"] = f"PCA failed: {exc}"
        logger.error(f"Route {route_id}: {r['error_msg']}")
        return r

    # Step 4: K-Means hyperparameter tuning
    try:
        t0 = time.perf_counter()
        optimal_k, labels, best_silhouette, route_class = _evaluate_optimal_k(X_pca)
        logger.info(
            f"Route {route_id}: optimal_k={optimal_k} sil={best_silhouette} "
            f"class={route_class} in {time.perf_counter()-t0:.2f}s"
        )
    except Exception as exc:
        r = dict(_NULL)
        r["error_msg"] = f"K-Means tuning failed: {exc}"
        logger.error(f"Route {route_id}: {r['error_msg']}")
        return r

    # Step 5: Per-cluster medoid selection + corridor save
    cluster_sizes = np.bincount(labels, minlength=optimal_k)
    corridors = []

    for cluster_id in range(optimal_k):
        cluster_mask = labels == cluster_id
        cluster_size = int(cluster_sizes[cluster_id])

        if cluster_size == 0:
            logger.warning(f"Route {route_id}: cluster {cluster_id} is empty, skipping.")
            continue

        try:
            medoid_idx = _select_medoid(X_pca, cluster_mask, is_clean_flags)
            medoid_flight = normalized_flights[medoid_idx]
            medoid_flight_id = getattr(medoid_flight, "flight_id", None) or str(medoid_idx)
        except Exception as exc:
            logger.error(f"Route {route_id} cluster {cluster_id}: medoid selection failed: {exc}")
            continue

        try:
            corridor_flight_id, out_path = _save_corridor(
                flight=medoid_flight,
                dep=dep,
                arr=arr,
                cluster_id=cluster_id,
                route_class=route_class,
                optimal_k=optimal_k,
                time_grid_seconds=time_grid_seconds,
            )
        except Exception as exc:
            logger.error(f"Route {route_id} cluster {cluster_id}: corridor save failed: {exc}")
            continue

        try:
            rel_path = out_path.resolve().relative_to(BASE_DIR).as_posix()
        except ValueError:
            rel_path = out_path.resolve().as_posix()

        corridors.append(dict(
            cluster_id=cluster_id,
            cluster_size=cluster_size,
            medoid_historical_flight_id=medoid_flight_id,
            corridor_flight_id=corridor_flight_id,
            file_path=rel_path,
        ))
        logger.info(
            f"Route {route_id}: cluster {cluster_id} → medoid={medoid_flight_id} "
            f"size={cluster_size}"
        )

    if not corridors:
        r = dict(_NULL)
        r["error_msg"] = "No corridors produced — all clusters failed."
        logger.error(f"Route {route_id}: {r['error_msg']}")
        return r

    timing = time.perf_counter() - t_start
    logger.info(f"Route {route_id}: clustering complete in {timing:.2f}s ({len(corridors)} corridors)")
    return dict(
        route_id=route_id,
        status="ok",
        optimal_k=optimal_k,
        silhouette_score=float(best_silhouette) if not np.isnan(best_silhouette) else None,
        route_class=route_class,
        corridors=corridors,
        error_msg=None,
        timing_seconds=round(timing, 3),
    )


# ===========================================================================
# Public entry point — top-level, picklable for ProcessPoolExecutor
# ===========================================================================

def cluster_route(
    route_id: str,
    registry_df: pd.DataFrame,
    d_pca: int,
    time_grid_seconds: int = 60,
    overwrite: bool = False,
) -> dict:
    """
    Full per-route clustering pipeline.

    Delegates to :func:`load_and_classify_cohort` and
    :func:`cluster_from_prepared` so that the streaming pipeline (Step 6)
    can reuse each step individually with an interleaved ΔRSD stability check.

    Parameters
    ----------
    route_id         : str   e.g. ``"EGLL-EDDF"``
    registry_df      : pd.DataFrame  global trajectory registry (read-only)
    d_pca            : int   PCA dimensionality
    time_grid_seconds: int   temporal resolution of saved corridor files
    overwrite        : bool  (currently unused — kept for API compatibility)

    Returns
    -------
    dict with keys:
        route_id, status, optimal_k, silhouette_score, route_class,
        corridors (list of per-cluster dicts), error_msg, timing_seconds
    """
    t_start = time.perf_counter()
    dep, arr = route_id.split("-", 1)

    logger.info(f"Route {route_id}: starting clustering...")

    prepared = load_and_classify_cohort(route_id, registry_df)
    if prepared is None:
        # Determine whether it was truly insufficient data or a processing error
        pattern = f"_{route_id}_"
        n_reg = len(registry_df[registry_df["flight_id"].str.contains(pattern, na=False)])
        if n_reg < MIN_FLIGHTS_FOR_CLUSTERING:
            return dict(
                route_id=route_id, status="insufficient_data", optimal_k=0,
                silhouette_score=float("nan"), route_class=0, corridors=[],
                error_msg=f"Only {n_reg} registry entries (need >= {MIN_FLIGHTS_FOR_CLUSTERING})",
                timing_seconds=round(time.perf_counter() - t_start, 3),
            )
        return dict(
            route_id=route_id, status="error", optimal_k=0,
            silhouette_score=float("nan"), route_class=0, corridors=[],
            error_msg="Cohort load / classify / vectorize failed (see logs above)",
            timing_seconds=round(time.perf_counter() - t_start, 3),
        )

    normalized_flights, is_clean_flags, X_scaled = prepared
    result = cluster_from_prepared(
        route_id=route_id,
        dep=dep,
        arr=arr,
        normalized_flights=normalized_flights,
        is_clean_flags=is_clean_flags,
        X_scaled=X_scaled,
        d_pca=d_pca,
        time_grid_seconds=time_grid_seconds,
    )
    # Override timing to include load+classify phase
    result["timing_seconds"] = round(time.perf_counter() - t_start, 3)
    return result

