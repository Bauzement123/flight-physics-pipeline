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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.corridor_modeling.pca_compressor import (
    classify_and_normalize_cohort,
    find_d_pca,
    normalize_vectors,
    vectorize_cohort,
)
from src.corridor_modeling.stability_worker import _load_route_flights

logger = logging.getLogger(__name__)

CALIBRATION_ROUTES = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-LEMD",
    "ESSA-EHAM",
    "LFRS-LFMN",
    "LGSA-LGAV",
]


def run_phase_a(routes: list[str] = CALIBRATION_ROUTES) -> int:
    """Evaluates find_d_pca on clean flights of calibration routes."""
    registry_df = load_trajectory_registry()
    d_vals = []

    for route_id in routes:
        logger.info(f"Evaluating D_PCA for route {route_id}...")
        flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
        if not flights:
            continue

        norm_flights, _ = classify_and_normalize_cohort(flights)
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
    logging.basicConfig(level=logging.INFO)
    main()
