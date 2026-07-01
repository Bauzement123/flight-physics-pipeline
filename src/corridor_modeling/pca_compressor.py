"""
Module: PCA Compressor
======================
Preprocessing, feature engineering, and stability-tracking pipeline for route
trajectory cohorts.  Operates on raw ``TrafficFlight`` cohorts and produces
PCA coordinates used by the Stage 2 stability registry and Stage 3
clustering engine.

Public API (in pipeline order):
    1. classify_and_normalize_cohort  -- ROCD classification & holding-pattern correction
    2. vectorize_flight / vectorize_cohort -- time-uniform 300-dim feature vectors
    3. normalize_vectors              -- Z-score standardization
    4. find_d_pca                     -- Phase A calibration: find D_PCA constant (int only, no model saved)
       fit_pca                        -- per-route: fit PCA with n_components = D_PCA
       apply_pca                      -- project Z-scored vectors into PCA coordinates
    5. update_running_stats           -- online batch-combining mean/variance
       calculate_delta_cv             -- relative-std DeltaCV stability metric
"""

import logging

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from traffic.core import Flight as TrafficFlight
from openap.phase import FlightPhase

from src.common.config import (
    DELTA_CV_EPSILON,
    ROCD_MIN_CLIMB_RATE,
    ROCD_MIN_DESCENT_RATE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helper: TOC/TOD finder (private copy to avoid circular import with
# path_generator, which will import classify_and_normalize_cohort from here)
# ---------------------------------------------------------------------------

def _find_toc_tod(flight: TrafficFlight, labels: list) -> tuple:
    """Identifies Top-of-Climb and Top-of-Descent indices."""
    df = flight.data
    altitudes = df["altitude"].values
    max_alt = np.max(altitudes)

    cruise_indices = [
        idx for idx, lbl in enumerate(labels)
        if lbl in ("CR", "LVL") and altitudes[idx] > 15000
    ]

    if cruise_indices:
        toc_idx = cruise_indices[0]
        tod_idx = cruise_indices[-1]
    else:
        cruise_thresh = max_alt * 0.90
        above_thresh = np.where(altitudes >= cruise_thresh)[0]
        if len(above_thresh) > 0:
            toc_idx = above_thresh[0]
            tod_idx = above_thresh[-1]
        else:
            toc_idx = int(len(df) * 0.2)
            tod_idx = int(len(df) * 0.8)

    toc_idx = max(2, min(toc_idx, int(len(df) * 0.4)))
    tod_idx = max(int(len(df) * 0.6), min(tod_idx, len(df) - 3))
    return toc_idx, tod_idx


# ===========================================================================
# Group 1 - ROCD Classification & Holding-Pattern Renormalization
# ===========================================================================

def classify_and_normalize_cohort(
    raw_flights: list,
) -> tuple:
    """
    Classifies a cohort into clean baseline flights vs. holding-pattern
    outliers, then linearises the climb/descent segments of outliers so that
    all flights have realistic ROCD profiles before vectorization.

    Parameters
    ----------
    raw_flights : list[TrafficFlight]
        Airborne flight objects loaded from the Stage 1 trajectory registry.

    Returns
    -------
    normalized_flights : list[TrafficFlight]
        Same length as raw_flights. Clean flights returned unmodified;
        holding-pattern outliers have climb/descent segments linearised.
    is_clean_flags : list[bool]
        True at index i if raw_flights[i] was NOT modified. Used by Step 4
        medoid selection to ensure the representative is always a real flight.
    """
    if not raw_flights:
        return [], []

    # ------------------------------------------------------------------
    # Step 1: Compute per-flight ROCD metrics via FlightPhase
    # ------------------------------------------------------------------
    flight_metrics: list[dict] = []

    for idx, flight in enumerate(raw_flights):
        try:
            df = flight.data
            ts = (df["timestamp"] - df["timestamp"].iloc[0]).dt.total_seconds().values
            alt = df["altitude"].values
            spd = df["groundspeed"].values
            roc = df["vertical_rate"].values

            fp = FlightPhase()
            fp.set_trajectory(ts, alt, spd, roc)
            labels = fp.phaselabel()

            toc_idx, tod_idx = _find_toc_tod(flight, labels)

            climb_dur_min = (ts[toc_idx] - ts[0]) / 60.0
            climb_rocd = (
                (alt[toc_idx] - alt[0]) / climb_dur_min if climb_dur_min > 0 else 0.0
            )
            descent_dur_min = (ts[-1] - ts[tod_idx]) / 60.0
            descent_rocd = (
                (alt[tod_idx] - alt[-1]) / descent_dur_min if descent_dur_min > 0 else 0.0
            )

            flight_metrics.append({
                "flight": flight,
                "toc_idx": toc_idx,
                "tod_idx": tod_idx,
                "climb_rocd": abs(climb_rocd),
                "descent_rocd": abs(descent_rocd),
            })
        except Exception as exc:
            logger.warning(
                f"Phase identification failed for flight {idx}: {exc}. Using fallback metrics."
            )
            df = flight.data
            flight_metrics.append({
                "flight": flight,
                "toc_idx": int(len(df) * 0.2),
                "tod_idx": int(len(df) * 0.8),
                "climb_rocd": ROCD_MIN_CLIMB_RATE,
                "descent_rocd": ROCD_MIN_DESCENT_RATE,
            })

    # ------------------------------------------------------------------
    # Step 2: Classify into clean vs. holding-pattern outliers
    # ------------------------------------------------------------------
    clean_metrics = [
        m for m in flight_metrics
        if m["climb_rocd"] >= ROCD_MIN_CLIMB_RATE
        and m["descent_rocd"] >= ROCD_MIN_DESCENT_RATE
    ]

    # Fallback: if fewer than 30% pass the hard thresholds, take the top 60%
    # by descent ROCD so we always have a clean reference set.
    if len(clean_metrics) < max(1, len(flight_metrics) * 0.30):
        threshold_count = max(1, int(len(flight_metrics) * 0.60))
        sorted_by_descent = sorted(
            flight_metrics, key=lambda x: x["descent_rocd"], reverse=True
        )
        clean_metrics = sorted_by_descent[:threshold_count]
        logger.info(
            f"ROCD fallback: < 30% clean flights detected.\n"
            f"Selected top {len(clean_metrics)} flights by descent ROCD as clean baseline."
        )

    median_clean_climb = float(np.median([m["climb_rocd"] for m in clean_metrics]))
    median_clean_descent = float(np.median([m["descent_rocd"] for m in clean_metrics]))

    if np.isnan(median_clean_climb) or median_clean_climb < 500:
        median_clean_climb = ROCD_MIN_CLIMB_RATE
    if np.isnan(median_clean_descent) or median_clean_descent < 500:
        median_clean_descent = ROCD_MIN_DESCENT_RATE

    logger.info(
        f"Cohort baseline ROCD - Climb: {median_clean_climb:.1f} ft/min, "
        f"Descent: {median_clean_descent:.1f} ft/min  "
        f"({len(clean_metrics)}/{len(flight_metrics)} clean)"
    )

    clean_callsigns = {m["flight"].callsign for m in clean_metrics}

    # ------------------------------------------------------------------
    # Step 3: Apply linear renormalization to holding-pattern outliers
    # ------------------------------------------------------------------
    normalized_flights: list = []
    is_clean_flags: list[bool] = []

    for metric in flight_metrics:
        flight = metric["flight"]

        if flight.callsign in clean_callsigns:
            normalized_flights.append(flight)
            is_clean_flags.append(True)
            continue

        logger.info(f"Renormalizing holding-pattern flight {flight.callsign}...")

        df_new = flight.data.copy()
        times = (
            df_new["timestamp"] - df_new["timestamp"].iloc[0]
        ).dt.total_seconds().values.copy()
        altitudes = df_new["altitude"].values.copy()
        latitudes = df_new["latitude"].values.copy()
        longitudes = df_new["longitude"].values.copy()

        toc_idx = metric["toc_idx"]
        tod_idx = metric["tod_idx"]

        # Linearise climb segment if below ROCD threshold
        if metric["climb_rocd"] < ROCD_MIN_CLIMB_RATE:
            alt_diff_climb = altitudes[toc_idx] - altitudes[0]
            new_climb_dur = (alt_diff_climb / median_clean_climb) * 60.0
            old_climb_dur = times[toc_idx] - times[0]
            time_shift = new_climb_dur - old_climb_dur

            climb_len = toc_idx + 1
            latitudes[:climb_len] = np.linspace(latitudes[0], latitudes[toc_idx], climb_len)
            longitudes[:climb_len] = np.linspace(longitudes[0], longitudes[toc_idx], climb_len)
            altitudes[:climb_len] = np.linspace(altitudes[0], altitudes[toc_idx], climb_len)
            times[:climb_len] = np.linspace(times[0], times[0] + new_climb_dur, climb_len)
            times[climb_len:] = times[climb_len:] + time_shift

        # Linearise descent segment if below ROCD threshold
        if metric["descent_rocd"] < ROCD_MIN_DESCENT_RATE:
            alt_diff_descent = altitudes[tod_idx] - altitudes[-1]
            new_descent_dur = (alt_diff_descent / median_clean_descent) * 60.0

            descent_len = len(df_new) - tod_idx
            latitudes[tod_idx:] = np.linspace(latitudes[tod_idx], latitudes[-1], descent_len)
            longitudes[tod_idx:] = np.linspace(longitudes[tod_idx], longitudes[-1], descent_len)
            altitudes[tod_idx:] = np.linspace(altitudes[tod_idx], altitudes[-1], descent_len)
            times[tod_idx:] = np.linspace(
                times[tod_idx], times[tod_idx] + new_descent_dur, descent_len
            )

        df_new["latitude"] = latitudes
        df_new["longitude"] = longitudes
        df_new["altitude"] = altitudes
        df_new["timestamp"] = df_new["timestamp"].iloc[0] + pd.to_timedelta(times, unit="s")

        normalized_flights.append(TrafficFlight(df_new))
        is_clean_flags.append(False)

    return normalized_flights, is_clean_flags


# ===========================================================================
# Group 2 - Trajectory Vectorization
# ===========================================================================

def vectorize_flight(flight: TrafficFlight) -> np.ndarray:
    """
    Resamples a trajectory to exactly 100 time-uniform points and returns a
    flat 300-dimension feature vector [lat*100, lon*100, alt*100].

    Time-uniform interpolation is used for consistency with the existing
    classify_and_cluster_cohort vectorizer in path_generator.py. Altitude is
    included (unlike the legacy 200-dim Lat/Lon-only vector) to capture
    vertical corridor separation in the PCA space.

    Parameters
    ----------
    flight : TrafficFlight

    Returns
    -------
    np.ndarray, shape (300,), dtype float64
    """
    df = flight.data
    lats = df["latitude"].values.astype(float)
    lons = df["longitude"].values.astype(float)
    alts = df["altitude"].values.astype(float)
    ts = (df["timestamp"] - df["timestamp"].iloc[0]).dt.total_seconds().values.astype(float)

    if len(ts) > 1 and ts[-1] > 0:
        t_norm = ts / ts[-1]
    else:
        t_norm = np.linspace(0.0, 1.0, len(ts))

    target = np.linspace(0.0, 1.0, 100)
    lats_r = np.interp(target, t_norm, lats)
    lons_r = np.interp(target, t_norm, lons)
    alts_r = np.interp(target, t_norm, alts)

    return np.concatenate([lats_r, lons_r, alts_r])


def vectorize_cohort(flights: list) -> np.ndarray:
    """
    Vectorizes a list of flights into a feature matrix.

    Parameters
    ----------
    flights : list[TrafficFlight]

    Returns
    -------
    np.ndarray, shape (N, 300), dtype float64
    """
    vectors = []
    for i, flight in enumerate(flights):
        try:
            vectors.append(vectorize_flight(flight))
        except Exception as exc:
            logger.warning(f"Vectorization failed for flight {i}: {exc}. Skipping.")
    if not vectors:
        raise ValueError("No flights could be vectorized in cohort.")
    return np.array(vectors, dtype=float)


# ===========================================================================
# Group 3 - Z-Score Normalization
# ===========================================================================

def normalize_vectors(
    X: np.ndarray,
) -> tuple:
    """
    Z-score standardizes feature matrix X to zero mean and unit variance.

    Dimensions with zero standard deviation are set to zero in X_scaled
    (not NaN) to avoid polluting downstream computations.

    Parameters
    ----------
    X : np.ndarray, shape (N, 300)

    Returns
    -------
    X_scaled    : np.ndarray, shape (N, 300)
    mean_vector : np.ndarray, shape (300,)
    std_vector  : np.ndarray, shape (300,)  -- raw std before guarding
    """
    mean_vector = X.mean(axis=0)
    std_vector = X.std(axis=0)

    safe_std = np.where(std_vector == 0, 1.0, std_vector)
    X_scaled = (X - mean_vector) / safe_std

    # Zero out columns that had zero std (they carry no information)
    X_scaled[:, std_vector == 0] = 0.0

    return X_scaled, mean_vector, std_vector


# ===========================================================================
# Group 4 - PCA: Phase A calibration helper vs. per-route fitting
# ===========================================================================

def find_d_pca(
    X_scaled: np.ndarray,
    variance_target: float = 0.95,
    max_components: int = 20,
) -> int:
    """
    Finds the minimum number of PCA components needed to capture
    ``variance_target`` cumulative explained variance.

    Called ONCE by the Step 5 Phase A calibration script on the 3
    oversampled calibration routes to derive the global ``D_PCA`` constant.
    Returns only the integer -- no model is fitted permanently or saved.

    Parameters
    ----------
    X_scaled       : np.ndarray, shape (N, 300), Z-score normalised
    variance_target: float  (default 0.95)
    max_components : int    upper bound for the search (default 20)

    Returns
    -------
    d_pca : int
    """
    n_max = min(max_components, X_scaled.shape[0] - 1, X_scaled.shape[1])
    probe = PCA(n_components=n_max, random_state=42)
    probe.fit(X_scaled)

    cumvar = np.cumsum(probe.explained_variance_ratio_)
    d_pca = int(np.searchsorted(cumvar, variance_target)) + 1
    d_pca = min(d_pca, n_max)

    logger.info(
        f"find_d_pca: {d_pca} components capture "
        f"{cumvar[d_pca - 1] * 100:.2f}% variance "
        f"(target {variance_target * 100:.0f}%)"
    )
    return d_pca


def fit_pca(
    X_scaled: np.ndarray,
    n_components: int,
) -> PCA:
    """
    Fits a PCA model on the given data and returns it.

    Called PER ROUTE in the main pipeline using ``n_components = D_PCA``
    (the constant written to ``config.py`` by the Phase A calibration script).
    The returned model is used immediately via ``apply_pca()`` and is
    NOT persisted to disk.

    Parameters
    ----------
    X_scaled     : np.ndarray, shape (N, 300), Z-score normalised
    n_components : int  (= D_PCA from config)

    Returns
    -------
    sklearn PCA model, already fitted on X_scaled
    """
    pca_model = PCA(n_components=n_components, random_state=42)
    pca_model.fit(X_scaled)
    return pca_model


def apply_pca(pca_model: PCA, X_scaled: np.ndarray) -> np.ndarray:
    """
    Projects Z-score normalised vectors into PCA coordinates.

    Always called after ``fit_pca()`` on the same route's data.
    Never re-fits -- uses ``pca_model.transform()`` only.

    Parameters
    ----------
    pca_model : sklearn PCA returned by fit_pca()
    X_scaled  : np.ndarray, shape (N, 300)

    Returns
    -------
    np.ndarray, shape (N, n_components)

    Raises
    ------
    ValueError if input feature dimension mismatches model expectations.
    """
    expected = pca_model.n_features_in_
    if X_scaled.shape[1] != expected:
        raise ValueError(
            f"apply_pca: input has {X_scaled.shape[1]} features, "
            f"but PCA model expects {expected}."
        )
    return pca_model.transform(X_scaled)


# ===========================================================================
# Group 5 - Running Statistics & DeltaCV Stability Metric
# ===========================================================================

def update_running_stats(
    new_pca_vectors: np.ndarray,
    mean_old: np.ndarray = None,
    var_old: np.ndarray = None,
    n_old: int = 0,
) -> tuple:
    """
    Updates running mean and variance using Chan et al. parallel batch-combining
    algorithm (numerically stable for any batch size).

    Parameters
    ----------
    new_pca_vectors : np.ndarray, shape (n_b, d_pca)
        PCA-projected coordinates of the new batch.
    mean_old : np.ndarray or None, shape (d_pca,)
        Running mean from previous batches. None on first call.
    var_old  : np.ndarray or None, shape (d_pca,)
        Running variance from previous batches. None on first call.
    n_old    : int
        Number of samples already processed.

    Returns
    -------
    mean_new : np.ndarray, shape (d_pca,)
    var_new  : np.ndarray, shape (d_pca,)
    n_new    : int
    """
    n_b = len(new_pca_vectors)
    if n_b == 0:
        if mean_old is None:
            raise ValueError("update_running_stats: no data provided on first call.")
        return mean_old, var_old, n_old

    mean_b = new_pca_vectors.mean(axis=0)
    var_b = new_pca_vectors.var(axis=0)

    if mean_old is None or n_old == 0:
        return mean_b, var_b, n_b

    # Chan et al. parallel algorithm
    n_a = n_old
    n_new = n_a + n_b
    delta = mean_b - mean_old

    mean_new = (n_a * mean_old + n_b * mean_b) / n_new
    var_new = (
        n_a * var_old + n_b * var_b + (n_a * n_b / n_new) * delta ** 2
    ) / n_new

    return mean_new, var_new, n_new


def calculate_delta_cv(
    var_old: np.ndarray,
    var_new: np.ndarray,
    epsilon: float = DELTA_CV_EPSILON,
) -> float:
    """
    Computes scalar DeltaCV stability metric as the L2 norm of the relative
    standard deviation change per PCA dimension:

        DeltaCV = || (sigma_new - sigma_old) / (sigma_old + epsilon) ||_2

    Using relative std change (rather than raw CV = sigma/mu) avoids
    instability when PCA-projected means are near zero.

    Parameters
    ----------
    var_old : np.ndarray, shape (d_pca,)
        Variance vector from the previous running state.
    var_new : np.ndarray, shape (d_pca,)
        Updated variance vector after the new batch.
    epsilon : float
        Guard for near-zero std (default: DELTA_CV_EPSILON = 1e-8).

    Returns
    -------
    float
        Scalar DeltaCV value. Below DELTA_CV_THRESHOLD signals convergence.
    """
    std_old = np.sqrt(np.maximum(var_old, 0.0))
    std_new = np.sqrt(np.maximum(var_new, 0.0))
    relative_change = (std_new - std_old) / (std_old + epsilon)
    # Normalize by sqrt(d_pca) so the metric is dimension-independent:
    # a threshold of τ means "average per-dimension relative std change is τ",
    # regardless of how many PCA components are used.
    d = len(relative_change)
    return float(np.linalg.norm(relative_change) / np.sqrt(max(d, 1)))
