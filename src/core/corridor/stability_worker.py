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
from src.common.adapters import parquet_to_pycontrails, pycontrails_to_traffic, df_to_traffic
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


def validate_and_clean_phase_sequence(
    df_flight: pd.DataFrame,
    level_as_cruise: bool = True,
    min_phase_run_points: int = 1,
) -> tuple[bool, pd.DataFrame, str]:
    """
    Validates that a flight's phase sequence is GND-bookended with a clean
    airborne progression between departure and landing ground phases:

        GND+ → CLIMB → [CRUISE/LVL ↔ CLIMB]* → CRUISE → DESCENT → GND+

    This check intentionally runs on the FULL trajectory data (before any
    .airborne() call) so that flights lacking a confirmed landing phase (GND)
    are rejected. Only flights with a real departure AND a real landing are
    accepted, ensuring data quality for downstream geometric analysis.

    Handles real-world complexity:
    - NA / unknown labels are stripped before checking.
    - Step-climb patterns (CLIMB → CRUISE → CLIMB → CRUISE) are allowed.
    - LVL segments map to CRUISE in Practical Mode (--level-as-cruise True).
    - Short noise runs shorter than `min_phase_run_points` are denoised before
      checking to suppress single-row barometric blip rejections.

    Parameters
    ----------
    df_flight : pd.DataFrame
        Full trajectory data INCLUDING ground rows (i.e. trf.data before
        calling .airborne()). Must contain a 'flight_phase' column.
    level_as_cruise : bool
        Practical Mode: maps 'LVL' phases to 'CRUISE' before validation.
        Strict Mode (False): any 'LVL' segment causes rejection.
    min_phase_run_points : int
        Minimum number of rows in a contiguous phase run. Runs shorter than
        this are merged into the surrounding phase to absorb noise blips.

    Returns
    -------
    tuple[bool, pd.DataFrame, str]
        (is_valid, df_airborne, reject_reason)
        df_airborne has all ONGROUND and NA rows stripped, ready for PCA/clustering.
        On rejection, df_airborne is the original df_flight unchanged.
    """
    if df_flight is None or df_flight.empty or "flight_phase" not in df_flight.columns:
        return False, df_flight, "NO_PHASE_COLUMN"

    phases_raw = df_flight["flight_phase"].astype(str).str.upper().str.strip()
    if phases_raw.isna().all() or (phases_raw == "NONE").all() or (phases_raw == "NAN").all():
        return False, df_flight, "ALL_NULL_PHASES"

    phase_map = {
        # openap native 2-char labels (state_lable_map from openap/phase.py)
        "GND": "ONGROUND", "CL": "CLIMB", "CR": "CRUISE", "DE": "DESCENT", "LVL": "LVL",
        # common spelled-out variants stored by the pipeline
        "GROUND": "ONGROUND", "ONGROUND": "ONGROUND", "ON_GROUND": "ONGROUND",
        "CLIMB": "CLIMB", "CLB": "CLIMB",
        "CRUISE": "CRUISE", "CRS": "CRUISE",
        "DESCENT": "DESCENT", "DES": "DESCENT", "DESC": "DESCENT",
        "LEVEL": "LVL",
        # NA / unknown → strip before schema check
        "NA": "NA", "NONE": "NA", "NAN": "NA",
    }
    canonical = phases_raw.map(lambda x: phase_map.get(x, x))

    # Strip NA rows only — keep ONGROUND rows for GND-bookend check
    non_na_mask = canonical != "NA"
    phases_for_check = canonical[non_na_mask].tolist()

    if not phases_for_check:
        return False, df_flight, "ALL_NULL_PHASES"

    # Handle LVL mapping before denoising
    if not level_as_cruise and "LVL" in phases_for_check:
        return False, df_flight, "REJECTED_LVL_STRICT_MODE"
    if level_as_cruise:
        phases_for_check = ["CRUISE" if p == "LVL" else p for p in phases_for_check]

    # Denoise short runs (e.g. single-row barometric blips)
    if min_phase_run_points > 1 and len(phases_for_check) > 0:
        for _ in range(5):
            runs = []
            curr = phases_for_check[0]
            count = 0
            for p in phases_for_check:
                if p == curr:
                    count += 1
                else:
                    runs.append([curr, count])
                    curr = p
                    count = 1
            runs.append([curr, count])

            if all(r[1] >= min_phase_run_points for r in runs):
                break

            new_phases = []
            for i, (p, c) in enumerate(runs):
                if c < min_phase_run_points and len(runs) > 1:
                    rep = runs[i - 1][0] if i > 0 else runs[i + 1][0]
                    new_phases.extend([rep] * c)
                else:
                    new_phases.extend([p] * c)
            phases_for_check = new_phases

    # Find all contiguous airborne (non-ONGROUND) blocks
    blocks = []
    in_block = False
    start_idx = 0
    for i, p in enumerate(phases_for_check):
        if p != "ONGROUND":
            if not in_block:
                in_block = True
                start_idx = i
        else:
            if in_block:
                in_block = False
                blocks.append((start_idx, i - 1))
    if in_block:
        blocks.append((start_idx, len(phases_for_check) - 1))

    if not blocks:
        return False, df_flight, "NO_AIRBORNE_PHASES"

    best_reason = "INVALID_SCHEMA_UNKNOWN"
    for start_idx, end_idx in blocks:
        # Check GND bookending for this candidate block
        if start_idx == 0 or phases_for_check[start_idx - 1] != "ONGROUND":
            best_reason = "MISSING_DEPARTURE_GND"
            continue
        if end_idx == len(phases_for_check) - 1 or phases_for_check[end_idx + 1] != "ONGROUND":
            best_reason = "MISSING_LANDING_GND"
            continue

        # Extract airborne block phases
        block_phases = phases_for_check[start_idx : end_idx + 1]

        # Compress consecutive identical phases
        compressed = []
        for p in block_phases:
            if not compressed or compressed[-1] != p:
                compressed.append(p)

        if compressed[0] != "CLIMB":
            reason_suffix = "_".join(compressed[:4])
            best_reason = f"INVALID_SCHEMA_{reason_suffix}"
            continue

        if compressed[-1] != "DESCENT":
            reason_suffix = "_".join(compressed[:4])
            best_reason = f"INVALID_SCHEMA_{reason_suffix}"
            continue

        if "CRUISE" not in compressed:
            best_reason = "INVALID_SCHEMA_NO_CRUISE"
            continue

        allowed = {"CLIMB", "CRUISE", "DESCENT"}
        seen_descent = False
        invalid_progression = False
        for p in compressed:
            if p not in allowed:
                best_reason = f"INVALID_SCHEMA_UNEXPECTED_{p}"
                invalid_progression = True
                break
            if seen_descent and p in ("CLIMB", "CRUISE"):
                best_reason = "INVALID_SCHEMA_POST_DESCENT_CLIMB"
                invalid_progression = True
                break
            if p == "DESCENT":
                seen_descent = True

        if invalid_progression:
            continue

        # Found a valid GND-bookended clean airborne segment!
        df_airborne = df_flight.loc[valid_indices[start_idx : end_idx + 1]].copy()
        if len(df_airborne) < _MIN_FLIGHT_ROWS:
            best_reason = "TOO_FEW_AIRBORNE_ROWS"
            continue

        return True, df_airborne, "VALID"

    return False, df_flight, best_reason


def _load_single_file_full_phase(
    abs_path: Path,
    flight_ids: set,
    level_as_cruise: bool = True,
    min_phase_run_points: int = 1,
) -> list[dict]:
    """
    Loads one parquet file, extracts matching flights, validates full phase schema,
    and returns a list of result records for each flight examined.
    """
    if not abs_path.exists():
        logger.warning(f"Trajectory file missing on disk: {abs_path}")
        return [{"flight_id": fid, "file_path": str(abs_path), "is_valid": False, "flight_obj": None, "reject_reason": "MISSING_FILE"} for fid in flight_ids]

    try:
        flights_dict = parquet_to_pycontrails(str(abs_path))
    except Exception as exc:
        logger.warning(f"Skipping corrupted / unreadable parquet {abs_path}: {exc}")
        return [{"flight_id": fid, "file_path": str(abs_path), "is_valid": False, "flight_obj": None, "reject_reason": "CORRUPTED_PARQUET"} for fid in flight_ids]

    results = []
    for fid in flight_ids:
        if fid not in flights_dict:
            continue
        try:
            trf = pycontrails_to_traffic(flights_dict[fid])
            df_fl = trf.data
            is_valid, df_airborne, reason = validate_and_clean_phase_sequence(
                df_fl, level_as_cruise=level_as_cruise, min_phase_run_points=min_phase_run_points
            )
            if is_valid:
                trf_airborne = df_to_traffic(df_airborne, is_si=False)
                for attr in ['flight_id', 'icao24', 'callsign', 'typecode']:
                    if hasattr(trf, attr) and getattr(trf, attr) is not None:
                        setattr(trf_airborne, attr, getattr(trf, attr))
                results.append({"flight_id": fid, "file_path": str(abs_path), "is_valid": True, "flight_obj": trf_airborne, "reject_reason": "VALID"})
            else:
                results.append({"flight_id": fid, "file_path": str(abs_path), "is_valid": False, "flight_obj": None, "reject_reason": reason})
        except Exception as exc:
            logger.debug(f"Skipping flight {fid} in {abs_path}: {exc}")
            results.append({"flight_id": fid, "file_path": str(abs_path), "is_valid": False, "flight_obj": None, "reject_reason": "EXCEPTION_IN_VALIDATION"})

    return results


def _load_route_flights_full_phase(
    route_id: str,
    registry_df: pd.DataFrame,
    level_as_cruise: bool = True,
    min_phase_run_points: int = 1,
) -> tuple[list[dict], dict]:
    """
    Loads all matching candidate flights for a route from the registry, evaluates
    their phase schema validity concurrently, and returns the full list of candidate
    records along with aggregate query/acceptance statistics.
    """
    pattern = f"_{route_id}_"
    matched = registry_df[registry_df["flight_id"].str.contains(pattern, na=False)]

    if matched.empty:
        logger.warning(f"No registry entries found for route {route_id}.")
        return [], {"total_queried": 0, "total_valid": 0, "reject_reasons": {}}

    id_to_file = dict(zip(matched["flight_id"], matched["file_path"]))
    file_to_ids: dict[str, list] = {}
    for fid, fp in id_to_file.items():
        file_to_ids.setdefault(fp, []).append(fid)

    all_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=_IO_THREADS) as pool:
        future_map = {
            pool.submit(
                _load_single_file_full_phase,
                BASE_DIR / rel_path if not Path(rel_path).is_absolute() else Path(rel_path),
                set(fids),
                level_as_cruise,
                min_phase_run_points,
            ): rel_path
            for rel_path, fids in file_to_ids.items()
        }
        for future in as_completed(future_map):
            try:
                all_records.extend(future.result())
            except Exception as exc:
                logger.warning(f"Unexpected error from full phase file loader: {exc}")

    total_queried = len(all_records)
    total_valid = sum(1 for r in all_records if r["is_valid"])
    reject_reasons = {}
    for r in all_records:
        if not r["is_valid"]:
            reason = r["reject_reason"]
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    stats_dict = {
        "total_queried": total_queried,
        "total_valid": total_valid,
        "reject_reasons": reject_reasons,
    }
    if total_queried > 0:
        logger.info(f"[{route_id}] Phase schema scan complete: {total_valid}/{total_queried} valid ({total_valid/total_queried*100:.1f}% acceptance). Rejections: {reject_reasons}")
    else:
        logger.info(f"[{route_id}] No flights loaded.")
    return all_records, stats_dict


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
