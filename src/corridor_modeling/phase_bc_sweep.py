"""
Phase BxC Joint Parameter Sweep
===============================
Runs a dense 2D sweep over N (sample size) x tau (delta-RSD threshold) on the 5
oversampled calibration routes (~400 flights each) to find the minimum viable
(N_STANDARD, DELTA_CV_THRESHOLD) pair for the streaming pipeline.

Uses the split-half delta-RSD calculation on Z-scored feature vectors (X_scaled),
exactly matching the runtime check in _streaming_compute_worker.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.common.config import BASE_DIR, D_PCA
from src.common.registry_utils import load_trajectory_registry
from src.common.utils import setup_file_logger
from src.corridor_modeling.pca_compressor import (
    classify_and_normalize_cohort,
    normalize_vectors,
    vectorize_cohort,
    calculate_delta_cv,
)
from src.corridor_modeling.stability_worker import _load_route_flights

logger = logging.getLogger(__name__)

CALIBRATION_ROUTES = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-LEMD",
    "ESSA-EHAM",
    "LFRS-LFMN",
]

DEFAULT_N_VALUES = [16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 400]
DEFAULT_TAU_VALUES = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
DEFAULT_K_REPLICATES = 30
CALIBRATION_OUT_DIR = BASE_DIR / "data" / "calibration"


def _prepare_route(route_id: str, registry_df: pd.DataFrame) -> dict:
    """Loads flights for route_id, normalizes, vectorizes, and Z-scores."""
    logger.info(f"  [{route_id}] loading cohort...")
    flights = _load_route_flights(route_id, n_target=9999, registry_df=registry_df)
    if not flights:
        raise RuntimeError(f"No flights found for route {route_id}")

    norm_flights, _ = classify_and_normalize_cohort(flights)
    if not norm_flights:
        raise RuntimeError(f"No flights survived normalization for {route_id}")

    X_raw = vectorize_cohort(norm_flights)
    X_scaled, _, _ = normalize_vectors(X_raw)

    logger.info(f"  [{route_id}] loaded {len(norm_flights)} clean flights (feature matrix shape {X_scaled.shape})")
    return {
        "route_id": route_id,
        "n_flights": len(norm_flights),
        "X_scaled": X_scaled,
    }


def run_sweep(
    route_cache: dict,
    n_values: list,
    tau_values: list,
    k_replicates: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Runs the K-replicate bootstrap evaluation across all routes and (N, tau) cells."""
    raw_records = []

    logger.info(f"Starting evaluations: {len(route_cache)} routes x {len(n_values)} N-values x {k_replicates} seeds...")

    for route_id, data in route_cache.items():
        X_scaled = data["X_scaled"]
        n_avail = len(X_scaled)

        for N in n_values:
            N_eff = min(N, n_avail)
            if N_eff < 4:
                continue

            for seed in range(k_replicates):
                rng = np.random.default_rng(seed=(abs(hash(route_id)) + N * 1000 + seed) % 2**32)
                idx = rng.choice(n_avail, size=N_eff, replace=False)
                X_sample = X_scaled[idx]

                half = N_eff // 2
                var_a = np.var(X_sample[:half], axis=0)
                var_b = np.var(X_sample[half:], axis=0)
                dcv = float(calculate_delta_cv(var_a, var_b))

                raw_records.append({
                    "route": route_id,
                    "N": N,
                    "N_eff": N_eff,
                    "seed": seed,
                    "delta_cv": dcv,
                })

    df_raw = pd.DataFrame(raw_records)

    # Build summary grid
    grid_rows = []
    for N in n_values:
        sub_n = df_raw[df_raw["N"] == N]
        if sub_n.empty:
            continue

        mean_dcv = float(sub_n["delta_cv"].mean())
        p10_dcv = float(sub_n["delta_cv"].quantile(0.10))
        p90_dcv = float(sub_n["delta_cv"].quantile(0.90))

        for tau in tau_values:
            row = {
                "N": N,
                "tau": tau,
                "mean_delta_cv": round(mean_dcv, 4),
                "p10_delta_cv": round(p10_dcv, 4),
                "p90_delta_cv": round(p90_dcv, 4),
            }

            # Overall convergence rate across all routes and seeds
            overall_conv = float((sub_n["delta_cv"] < tau).mean())
            row["convergence_rate"] = round(overall_conv, 4)

            # Per-route convergence rates
            for route_id in route_cache:
                sub_r = sub_n[sub_n["route"] == route_id]
                conv_r = float((sub_r["delta_cv"] < tau).mean()) if not sub_r.empty else 0.0
                row[f"conv_{route_id}"] = round(conv_r, 4)

            grid_rows.append(row)

    df_grid = pd.DataFrame(grid_rows)
    return df_raw, df_grid


def plot_results(df_raw: pd.DataFrame, df_grid: pd.DataFrame, out_dir: Path) -> None:
    """Generates 2D heatmaps and delta-RSD curves if matplotlib is available."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skipping plot generation.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Overall Heatmap
    plt.figure(figsize=(10, 6))
    pivot = df_grid.pivot(index="N", columns="tau", values="convergence_rate")
    plt.imshow(pivot.values, aspect="auto", origin="lower", cmap="YlGnBu", vmin=0.0, vmax=1.0)
    plt.colorbar(label="Convergence Rate")
    plt.xticks(range(len(pivot.columns)), [f"{t:.2f}" for t in pivot.columns])
    plt.yticks(range(len(pivot.index)), [str(n) for n in pivot.index])
    plt.xlabel(r"$\tau$ (Threshold)")
    plt.ylabel("N (Sample Size)")
    plt.title(r"Overall $\Delta$RSD Convergence Rate across 5 Calibration Routes")
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            plt.text(j, i, f"{val*100:.0f}%", ha="center", va="center",
                     color="white" if val > 0.6 else "black", fontsize=8)
    plt.tight_layout()
    hm_path = out_dir / "phase_bc_heatmap.png"
    plt.savefig(hm_path, dpi=150)
    plt.close()
    logger.info(f"Saved overall heatmap: {hm_path}")

    # 2. Per-Route Heatmaps
    routes = df_raw["route"].unique()
    n_routes = len(routes)
    fig, axes = plt.subplots(1, n_routes, figsize=(3.5 * n_routes, 5), sharey=True)
    if n_routes == 1:
        axes = [axes]

    for ax, route_id in zip(axes, routes):
        col_name = f"conv_{route_id}"
        if col_name not in df_grid.columns:
            continue
        p_route = df_grid.pivot(index="N", columns="tau", values=col_name)
        im = ax.imshow(p_route.values, aspect="auto", origin="lower", cmap="YlGnBu", vmin=0.0, vmax=1.0)
        ax.set_title(route_id, fontsize=10)
        ax.set_xticks(range(len(p_route.columns)))
        ax.set_xticklabels([f"{t:.2f}" for t in p_route.columns], rotation=45, fontsize=8)
        if ax == axes[0]:
            ax.set_yticks(range(len(p_route.index)))
            ax.set_yticklabels([str(n) for n in p_route.index], fontsize=8)
            ax.set_ylabel("N (Sample Size)")
        ax.set_xlabel(r"$\tau$")

    fig.tight_layout()
    pr_path = out_dir / "phase_bc_per_route.png"
    plt.savefig(pr_path, dpi=150)
    plt.close()
    logger.info(f"Saved per-route heatmap: {pr_path}")

    # 3. Delta-RSD Curves vs N
    plt.figure(figsize=(9, 6))
    for route_id in routes:
        sub_r = df_raw[df_raw["route"] == route_id]
        grouped = sub_r.groupby("N")["delta_cv"]
        medians = grouped.median()
        p25 = grouped.quantile(0.25)
        p75 = grouped.quantile(0.75)
        line, = plt.plot(medians.index, medians.values, marker="o", label=route_id)
        plt.fill_between(medians.index, p25.values, p75.values, color=line.get_color(), alpha=0.15)

    plt.yscale("log")
    plt.xlabel("N (Sample Size)")
    plt.ylabel(r"$\Delta$RSD (Split-Half)")
    plt.title(r"Split-Half $\Delta$RSD vs. Sample Size (Median $\pm$ IQR)")
    plt.grid(True, which="both", ls="--", alpha=0.5)
    plt.legend()
    plt.tight_layout()
    curve_path = out_dir / "phase_bc_delta_cv_curves.png"
    plt.savefig(curve_path, dpi=150)
    plt.close()
    logger.info(f"Saved curves plot: {curve_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase BxC Joint Parameter Sweep")
    parser.add_argument("--k-replicates", type=int, default=DEFAULT_K_REPLICATES, help="Number of random seeds per cell")
    parser.add_argument("--table-only", action="store_true", help="Skip plot generation")
    args = parser.parse_args()

    CALIBRATION_OUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Loading global trajectory registry...")
    registry_df = load_trajectory_registry()

    route_cache = {}
    for r in CALIBRATION_ROUTES:
        try:
            route_cache[r] = _prepare_route(r, registry_df)
        except Exception as exc:
            logger.error(f"Failed to prepare {r}: {exc}")

    if not route_cache:
        logger.error("No calibration routes prepared. Aborting.")
        sys.exit(1)

    t0 = time.perf_counter()
    df_raw, df_grid = run_sweep(route_cache, DEFAULT_N_VALUES, DEFAULT_TAU_VALUES, args.k_replicates)
    elapsed = time.perf_counter() - t0
    logger.info(f"Sweep completed in {elapsed:.2f} seconds.")

    raw_path = CALIBRATION_OUT_DIR / "phase_bc_raw.csv"
    grid_path = CALIBRATION_OUT_DIR / "phase_bc_grid.csv"
    df_raw.to_csv(raw_path, index=False)
    df_grid.to_csv(grid_path, index=False)
    logger.info(f"Saved raw evaluations: {raw_path}")
    logger.info(f"Saved summary grid: {grid_path}")

    if not args.table_only:
        plot_results(df_raw, df_grid, CALIBRATION_OUT_DIR)

    # Print a clean summary table to stdout for immediate review
    print("\n" + "="*80)
    print("CONVERGENCE RATE GRID (Overall % across all 5 calibration routes)")
    print("="*80)
    pivot = df_grid.pivot(index="N", columns="tau", values="convergence_rate")
    print(pivot.to_string(float_format=lambda x: f"{x*100:5.1f}%"))
    print("="*80)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [SWEEP] - %(levelname)s - %(message)s")
    setup_file_logger(log_filename="phase_bc_sweep.log")
    main()
