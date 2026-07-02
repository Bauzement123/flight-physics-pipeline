"""
Module: Stability Worker
========================
Pure, picklable per-route processing logic for the Stage 2 Stability Sampling
pipeline.  Designed to run inside a ``ProcessPoolExecutor`` worker process.

Responsibilities per route:
    1. Load up to N_STANDARD raw trajectory flights (threaded I/O, corrupted-
       parquet-safe).
    2. Classify & normalise holding-pattern outliers.
    3. Vectorize → Z-score → fresh per-route PCA fit → project.
    4. Compute ΔCV stability metric across sequential batches.
    5. If ΔCV ≥ DELTA_CV_THRESHOLD, expand the flight sample and restart from
       step 1 (complete PCA re-fit — no state carried over).
    6. Return a result dict serialisable by the orchestrator.

Top-level public API
--------------------
    process_route(route_id, registry_df, n_standard, d_pca,
                  delta_cv_threshold, resample_multiplier,
                  max_resample_rounds, overwrite) -> dict
"""

import logging
import math
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from src.common.config import BASE_DIR
from src.common.adapters import parquet_to_pycontrails, pycontrails_to_traffic
from src.core.corridor.pca_compressor import (
    classify_and_normalize_cohort,
    vectorize_cohort,
    normalize_vectors,
    fit_pca,
    apply_pca,
    update_running_stats,
    calculate_delta_cv,
)

logger = logging.getLogger(__name__)

# Minimum flights needed before we attempt PCA / ΔCV computation
_MIN_FLIGHTS = 3

# Number of parquet files to load concurrently inside one worker process.
# I/O-bound: threads release the GIL during file reads → genuine parallelism.
_IO_THREADS = 4

# Minimum rows in a loaded flight before it counts as valid
_MIN_FLIGHT_ROWS = 10

# Sequential batch size used when splitting X_pca for the ΔCV rolling loop
_DELTA_CV_BATCH_SIZE = 10


# ===========================================================================
# Internal helpers
# ===========================================================================

def _load_single_file(
    abs_path: Path,
    flight_ids: set,
) -> list:
    """
    Loads one parquet file and extracts all matching TrafficFlight objects.

    Returns an empty list (never raises) on any file-level or flight-level
    failure so the caller can continue with other files.
    """
    if not abs_path.exists():
        logger.warning(f"Trajectory file missing on disk: {abs_path}")
        return []

    try:
        flights_dict = parquet_to_pycontrails(str(abs_path))
    except Exception as exc:
        logger.warning(f"Skipping corrupted / unreadable parquet {abs_path}: {exc}")
        return []

    valid = []
    for fid in flight_ids:
        if fid not in flights_dict:
            continue
        try:
            trf = pycontrails_to_traffic(flights_dict[fid])
            airborne = trf.airborne()
            if airborne is not None and len(airborne.data) >= _MIN_FLIGHT_ROWS:
                valid.append(airborne)
        except Exception as exc:
            logger.debug(f"Skipping flight {fid} in {abs_path}: {exc}")

    return valid


def _load_route_flights(
    route_id: str,
    n_target: int,
    registry_df: pd.DataFrame,
) -> list:
    """
    Loads up to ``n_target`` valid airborne TrafficFlight objects for a given
    route.

    Uses an inner ThreadPoolExecutor to load multiple parquet files
    concurrently (I/O-bound, GIL released during reads).  All file-level and
    flight-level errors are caught and logged; the function never raises.

    Parameters
    ----------
    route_id  : str   e.g. ``"EGLL-EDDF"``
    n_target  : int   maximum number of flights to return
    registry_df : pd.DataFrame  the global trajectory registry

    Returns
    -------
    list[TrafficFlight]  -- may be shorter than n_target
    """
    # Pattern match: registry flight_ids contain "_DEP-ARR_"
    pattern = f"_{route_id}_"
    matched = registry_df[registry_df["flight_id"].str.contains(pattern, na=False)]

    if matched.empty:
        logger.warning(f"No registry entries found for route {route_id}.")
        return []

    # Reproducible shuffle so different runs are consistent within a route
    rng = random.Random(route_id)
    shuffled_ids = matched["flight_id"].tolist()
    rng.shuffle(shuffled_ids)

    # Group by file — load only files we need
    id_to_file = dict(zip(matched["flight_id"], matched["file_path"]))
    file_to_ids: dict[str, list] = {}
    for fid in shuffled_ids[:n_target]:
        fp = id_to_file[fid]
        file_to_ids.setdefault(fp, []).append(fid)

    # Submit one task per file to the thread pool
    all_flights: list = []
    with ThreadPoolExecutor(max_workers=_IO_THREADS) as pool:
        future_map = {
            pool.submit(
                _load_single_file,
                BASE_DIR / rel_path if not Path(rel_path).is_absolute() else Path(rel_path),
                set(fids),
            ): rel_path
            for rel_path, fids in file_to_ids.items()
        }
        for future in as_completed(future_map):
            try:
                all_flights.extend(future.result())
            except Exception as exc:
                logger.warning(f"Unexpected error from file loader: {exc}")

    logger.debug(f"Route {route_id}: loaded {len(all_flights)} / {n_target} flights.")
    return all_flights[:n_target]


def _run_pca_pipeline(
    flights: list,
    n_components: int,
) -> tuple:
    """
    Full fresh PCA pipeline for one round of processing.

    Called on the initial batch AND again on every resample — no state is
    carried over between calls.

    Parameters
    ----------
    flights      : list[TrafficFlight]
    n_components : int   (= D_PCA from config)

    Returns
    -------
    X_pca      : np.ndarray, shape (N, n_components)
    mean_vec   : np.ndarray, shape (300,)   Z-score mean used for this round
    std_vec    : np.ndarray, shape (300,)   Z-score std  used for this round

    Raises
    ------
    ValueError  if fewer than _MIN_FLIGHTS remain after normalisation/vectorization
    """
    # Step 1 — ROCD classification & holding-pattern renormalisation
    normalized_flights, _ = classify_and_normalize_cohort(flights)
    if len(normalized_flights) < _MIN_FLIGHTS:
        raise ValueError(
            f"Only {len(normalized_flights)} flights survived normalisation "
            f"(need >= {_MIN_FLIGHTS})."
        )

    # Step 2 — Vectorize to (N, 300)
    X = vectorize_cohort(normalized_flights)

    # Step 3 — Z-score standardise
    X_scaled, mean_vec, std_vec = normalize_vectors(X)

    # Step 4 — Fit PCA fresh on this route/round's data
    pca_model = fit_pca(X_scaled, n_components=n_components)

    # Step 5 — Project
    X_pca = apply_pca(pca_model, X_scaled)

    return X_pca, mean_vec, std_vec


def _compute_stability(
    X_pca: np.ndarray,
    batch_size: int = _DELTA_CV_BATCH_SIZE,
) -> tuple:
    """
    Splits ``X_pca`` into sequential batches and runs the Chan et al.
    batch-combining running-stats update, computing ΔCV between consecutive
    variance estimates.

    Parameters
    ----------
    X_pca      : np.ndarray, shape (N, d_pca)
    batch_size : int  (default: _DELTA_CV_BATCH_SIZE = 10)

    Returns
    -------
    delta_cv   : float   last ΔCV value (between penultimate and final batch)
    mean_final : np.ndarray, shape (d_pca,)
    var_final  : np.ndarray, shape (d_pca,)
    n_final    : int

    Notes
    -----
    If N < 2 * batch_size, only one ΔCV measurement is possible (comparing
    the first batch variance to the full-data variance).  ΔCV is then the
    relative change from batch 1 → batch 2, which is a conservative estimate.
    """
    n = len(X_pca)
    n_batches = max(2, math.ceil(n / batch_size))
    splits = np.array_split(X_pca, n_batches)

    mean_r, var_r, n_r = None, None, 0
    prev_var = None
    delta_cv = float("inf")

    for batch in splits:
        if len(batch) == 0:
            continue
        mean_r, var_r, n_r = update_running_stats(batch, mean_r, var_r, n_r)
        if prev_var is not None:
            delta_cv = calculate_delta_cv(prev_var, var_r)
        prev_var = var_r.copy()

    return delta_cv, mean_r, var_r, n_r


# ===========================================================================
# Public entry point — must be top-level and picklable for ProcessPoolExecutor
# ===========================================================================

def process_route(
    route_id: str,
    registry_df: pd.DataFrame,
    n_standard: int,
    d_pca: int,
    delta_cv_threshold: float,
    resample_multiplier: int,
    max_resample_rounds: int,
    overwrite: bool = False,
) -> dict:
    """
    Full per-route stability processing.

    Implements the resample loop: if ΔCV ≥ ``delta_cv_threshold`` after the
    initial ``n_standard`` flights, the sample is expanded by
    ``resample_multiplier^round`` and the entire PCA pipeline is restarted
    from scratch (no state carryover).

    Parameters
    ----------
    route_id             : str   e.g. ``"EGLL-EDDF"``
    registry_df          : pd.DataFrame  global trajectory registry (read-only)
    n_standard           : int   initial query budget (= N_STANDARD from config)
    d_pca                : int   PCA dimensionality (= D_PCA from config)
    delta_cv_threshold   : float convergence threshold
    resample_multiplier  : int   budget multiplier per resample round
    max_resample_rounds  : int   hard cap on resample attempts
    overwrite            : bool  (unused here; checked by orchestrator upstream)

    Returns
    -------
    dict with keys:
        route_id, status, N_current, pca_mean_vector, pca_variance,
        delta_cv, needs_resample, is_uptodate, resample_rounds, error_msg
    """
    _NULL_RESULT = dict(
        route_id=route_id,
        status="error",
        N_current=0,
        pca_mean_vector=None,
        pca_variance=None,
        delta_cv=None,
        needs_resample=True,
        is_uptodate=False,
        resample_rounds=0,
        error_msg=None,
    )

    resample_round = 0
    n_query = n_standard

    while True:
        logger.info(
            f"Route {route_id} | round {resample_round} | querying {n_query} flights..."
        )

        # ------------------------------------------------------------------
        # 1. Load flights (corrupted-parquet-safe)
        # ------------------------------------------------------------------
        try:
            flights = _load_route_flights(route_id, n_query, registry_df)
        except Exception as exc:
            result = dict(_NULL_RESULT)
            result["error_msg"] = f"Flight loading failed: {exc}"
            logger.error(f"Route {route_id}: {result['error_msg']}")
            return result

        if len(flights) < _MIN_FLIGHTS:
            result = dict(_NULL_RESULT)
            result["status"] = "insufficient_data"
            result["error_msg"] = (
                f"Only {len(flights)} valid flights available "
                f"(need >= {_MIN_FLIGHTS})."
            )
            logger.warning(f"Route {route_id}: {result['error_msg']}")
            return result

        # ------------------------------------------------------------------
        # 2. Fresh PCA pipeline (full restart every round)
        # ------------------------------------------------------------------
        try:
            X_pca, mean_vec, std_vec = _run_pca_pipeline(flights, d_pca)
        except Exception as exc:
            result = dict(_NULL_RESULT)
            result["error_msg"] = f"PCA pipeline failed: {exc}"
            logger.error(f"Route {route_id}: {result['error_msg']}")
            return result

        # ------------------------------------------------------------------
        # 3. ΔCV stability computation
        # ------------------------------------------------------------------
        try:
            delta_cv, mean_final, var_final, n_final = _compute_stability(X_pca)
        except Exception as exc:
            result = dict(_NULL_RESULT)
            result["error_msg"] = f"Stability computation failed: {exc}"
            logger.error(f"Route {route_id}: {result['error_msg']}")
            return result

        logger.info(
            f"Route {route_id} | round {resample_round} | "
            f"N={n_final} | ΔCV={delta_cv:.5f} | "
            f"threshold={delta_cv_threshold:.5f}"
        )

        # ------------------------------------------------------------------
        # 4. Convergence check
        # ------------------------------------------------------------------
        converged = delta_cv < delta_cv_threshold
        at_cap = resample_round >= max_resample_rounds

        if converged or at_cap:
            if at_cap and not converged:
                logger.warning(
                    f"Route {route_id}: ΔCV={delta_cv:.5f} still above threshold "
                    f"after {resample_round} resample rounds. Forcing convergence."
                )
            return dict(
                route_id=route_id,
                status="ok",
                N_current=int(n_final),
                pca_mean_vector=mean_final,
                pca_variance=var_final,
                delta_cv=float(delta_cv),
                needs_resample=False,
                is_uptodate=True,
                resample_rounds=resample_round,
                error_msg=None,
            )

        # Not converged — expand and retry
        resample_round += 1
        n_query = n_standard * (resample_multiplier ** resample_round)
        logger.info(
            f"Route {route_id}: ΔCV not converged. "
            f"Expanding to {n_query} flights (round {resample_round})."
        )
