"""
Phase A Calibration: PCA Dimension Determination (D_PCA).
=========================================================
Computes optimal D_PCA across oversampled calibration routes to capture >=95% variance.
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.common.config import CALIBRATION_ROUTES
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.core.corridor.pca_compressor import (
    normalize_vectors,
    vectorize_cohort,
)
from src.core.corridor.stability_worker import _load_route_flights

from sklearn.decomposition import PCA

logger = logging.getLogger(__name__)

def find_d_pca(
    X_scaled: np.ndarray,
    variance_target: float = 0.95,
    max_components: int = 20,
) -> int:
    """Finds the minimum number of PCA components needed to capture variance_target."""
    n_max = min(max_components, X_scaled.shape[0] - 1, X_scaled.shape[1])
    probe = PCA(n_components=n_max, random_state=42)
    probe.fit(X_scaled)
    cumvar = np.cumsum(probe.explained_variance_ratio_)
    d_pca = int(np.searchsorted(cumvar, variance_target)) + 1
    d_pca = min(d_pca, n_max)
    return d_pca


def run_phase_a(routes: list[str] = CALIBRATION_ROUTES) -> int:
    """Evaluates find_d_pca on clean flights of calibration routes."""
    registry_df = load_trajectory_registry()
    d_vals = []

    for route_id in routes:
        logger.info(f"Evaluating D_PCA for route {route_id}...")
        flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
        if not flights:
            continue

        # Use EKF clean flights directly
        norm_flights = flights
        if len(norm_flights) < 10:
            continue

        X_raw = vectorize_cohort(norm_flights)
        X_scaled, _, _ = normalize_vectors(X_raw)
        d = find_d_pca(X_scaled, variance_target=0.95)
        d_vals.append(d)
        logger.info(f"  [{route_id}] N={len(norm_flights)}, D_PCA(95%)={d}")

    if not d_vals:
        raise RuntimeError("No valid calibration routes evaluated.")

    recommended_d = int(np.median(d_vals))
    logger.info(f"Recommended D_PCA across {len(d_vals)} routes: {recommended_d}")
    return recommended_d


def main():
    parser = argparse.ArgumentParser(description="Phase A: D_PCA Calibration")
    parser.parse_args()
    setup_file_logger("phase_a_calibration.log")
    d = run_phase_a()
    print(f"\nRecommended D_PCA = {d} (N_STANDARD = {5 * d})")


if __name__ == "__main__":
    setup_file_logger(log_filename="calibration.log")
    main()
