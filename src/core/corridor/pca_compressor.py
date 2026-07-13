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
from src.common.config import (
    DELTA_CV_EPSILON,
)

logger = logging.getLogger(__name__)


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
# Group 4 - PCA: per-route fitting
# ===========================================================================


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
