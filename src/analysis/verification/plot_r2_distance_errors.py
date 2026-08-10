"""
Test R2-1 Endpoint Distance Error Analysis & Visualization Script

Calculates statistical error metrics (MAE, RMSE, percentiles, tolerance bands)
for Raw and Clean endpoint distance metrics relative to OpenSky fd4 ground truth.
Generates CSV report summaries and a 3x2 multi-panel diagnostic plot.
"""

import sys
import logging
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from src.common.config import REPORTS_DIR, DATA_DIR
from src.common.utils import setup_file_logger

PLOTS_DIR = DATA_DIR / "analysis" / "plots"
INPUT_PARQUET = REPORTS_DIR / "r2_distance_table.parquet"
OUTPUT_SUMMARY_CSV = REPORTS_DIR / "r2_error_summary.csv"
OUTPUT_DEP_OUTLIERS_CSV = REPORTS_DIR / "r2_dep_route_outliers.csv"
OUTPUT_ARR_OUTLIERS_CSV = REPORTS_DIR / "r2_arr_route_outliers.csv"
OUTPUT_PLOT_SVG = PLOTS_DIR / "r2_distance_errors.svg"

logger = logging.getLogger(__name__)


def parse_route_info(df: pd.DataFrame) -> pd.DataFrame:
    """Extracts dep_airport, arr_airport, and route from flight_id."""
    def get_route(fid: str):
        if not fid or not isinstance(fid, str):
            return None, None, None
        parts = fid.split("_")
        if len(parts) < 5:
            return None, None, None
        route_str = parts[-3]
        if "-" not in route_str:
            return None, None, route_str
        dep, arr = route_str.split("-", 1)
        return dep, arr, f"{dep}-{arr}"

    parsed = [get_route(fid) for fid in df["flight_id"]]
    parsed_df = pd.DataFrame(parsed, columns=["dep_airport", "arr_airport", "route"])
    return pd.concat([df, parsed_df], axis=1)


def compute_metrics(series_err: pd.Series, series_abs: pd.Series, total_count: int, name: str) -> dict:
    """Computes standard error summary metrics for a given error series."""
    valid_s = series_err.dropna()
    valid_abs = series_abs.dropna()
    n_valid = len(valid_s)
    
    if n_valid == 0:
        return {"metric": name, "n_valid": 0, "n_total": total_count, "pct_valid": 0.0}

    mae = valid_abs.mean()
    rmse = np.sqrt((valid_s ** 2).mean())
    median_err = valid_s.median()
    mean_err = valid_s.mean()
    p90 = valid_abs.quantile(0.90)
    p95 = valid_abs.quantile(0.95)
    p99 = valid_abs.quantile(0.99)
    
    pct_500m = (valid_abs < 500).mean() * 100
    pct_1km = (valid_abs < 1000).mean() * 100
    pct_2km = (valid_abs < 2000).mean() * 100
    pct_5km = (valid_abs < 5000).mean() * 100

    return {
        "metric": name,
        "n_valid": n_valid,
        "n_total": total_count,
        "pct_valid": round((n_valid / total_count) * 100, 2),
        "mean_err_m": round(mean_err, 2),
        "median_err_m": round(median_err, 2),
        "mae_m": round(mae, 2),
        "rmse_m": round(rmse, 2),
        "p90_abs_m": round(p90, 2),
        "p95_abs_m": round(p95, 2),
        "p99_abs_m": round(p99, 2),
        "pct_within_500m": round(pct_500m, 2),
        "pct_within_1km": round(pct_1km, 2),
        "pct_within_2km": round(pct_2km, 2),
        "pct_within_5km": round(pct_5km, 2),
    }


def generate_plots(df: pd.DataFrame, out_path: Path):
    """Generates a 4x2 multi-panel diagnostic plot saved to SVG."""
    fig, axes = plt.subplots(4, 2, figsize=(14, 20))
    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
    
    clip_val = 50000
    
    # -------------------------------------------------------------
    # Row 1: Absolute Distance Histograms (Clipped at 50km)
    # -------------------------------------------------------------
    # Departure Distances
    ax = axes[0, 0]
    fd4_dep = np.clip(df["fd4_dep_horiz_m"].dropna(), 0, clip_val)
    raw_dep = np.clip(df["raw_dep_horiz_m"].dropna(), 0, clip_val)
    clean_dep = np.clip(df["clean_dep_horiz_m"].dropna(), 0, clip_val)
    
    ax.hist(fd4_dep, bins=100, alpha=0.5, label="FD4 Ground Truth", color="green", density=True)
    ax.hist(raw_dep, bins=100, alpha=0.5, label="Raw Distance", color="orange", density=True)
    ax.hist(clean_dep, bins=100, alpha=0.5, label="Clean Distance", color="blue", density=True)
    ax.set_title("Departure Absolute Horizontal Distance Distribution", fontsize=12, fontweight="bold")
    ax.set_xlabel("Distance to Airport (meters)")
    ax.set_ylabel("Density")
    ax.legend(loc="upper right")

    # Arrival Distances
    ax = axes[0, 1]
    fd4_arr = np.clip(df["fd4_arr_horiz_m"].dropna(), 0, clip_val)
    raw_arr = np.clip(df["raw_arr_horiz_m"].dropna(), 0, clip_val)
    clean_arr = np.clip(df["clean_arr_horiz_m"].dropna(), 0, clip_val)
    
    ax.hist(fd4_arr, bins=100, alpha=0.5, label="FD4 Ground Truth", color="green", density=True)
    ax.hist(raw_arr, bins=100, alpha=0.5, label="Raw Distance", color="orange", density=True)
    ax.hist(clean_arr, bins=100, alpha=0.5, label="Clean Distance", color="blue", density=True)
    ax.set_title("Arrival Absolute Horizontal Distance Distribution", fontsize=12, fontweight="bold")
    ax.set_xlabel("Distance to Airport (meters)")
    ax.set_ylabel("Density")
    ax.legend(loc="upper right")

    # -------------------------------------------------------------
    # Row 2: Error Distributions (Log-clipped [-50km, +50km])
    # -------------------------------------------------------------
    # Departure Error Distribution
    ax = axes[1, 0]
    raw_dep_valid = df["err_raw_dep"].dropna()
    clean_dep_valid = df["err_clean_dep"].dropna()
    
    raw_dep_clipped = np.clip(raw_dep_valid, -clip_val, clip_val)
    clean_dep_clipped = np.clip(clean_dep_valid, -clip_val, clip_val)
    
    ax.hist(raw_dep_clipped, bins=100, alpha=0.5, label="Raw Error", color="orange", density=True)
    ax.hist(clean_dep_clipped, bins=100, alpha=0.5, label="Clean Error", color="blue", density=True)
    ax.set_title("Departure Signed Distance Error Distribution", fontsize=12, fontweight="bold")
    ax.set_xlabel("Signed Error relative to FD4 (meters)")
    ax.set_ylabel("Density")
    ax.legend(loc="upper right")
    
    overflow_dep_raw = (raw_dep_valid.abs() > clip_val).sum()
    overflow_dep_clean = (clean_dep_valid.abs() > clip_val).sum()
    ax.annotate(
        f"Outliers >50km:\nRaw: {overflow_dep_raw:,}\nClean: {overflow_dep_clean:,}",
        xy=(0.03, 0.75), xycoords="axes fraction",
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8),
        fontsize=9
    )

    # Arrival Error Distribution
    ax = axes[1, 1]
    raw_arr_valid = df["err_raw_arr"].dropna()
    clean_arr_valid = df["err_clean_arr"].dropna()
    
    raw_arr_clipped = np.clip(raw_arr_valid, -clip_val, clip_val)
    clean_arr_clipped = np.clip(clean_arr_valid, -clip_val, clip_val)
    
    ax.hist(raw_arr_clipped, bins=100, alpha=0.5, label="Raw Error", color="orange", density=True)
    ax.hist(clean_arr_clipped, bins=100, alpha=0.5, label="Clean Error", color="blue", density=True)
    ax.set_title("Arrival Signed Distance Error Distribution", fontsize=12, fontweight="bold")
    ax.set_xlabel("Signed Error relative to FD4 (meters)")
    ax.set_ylabel("Density")
    ax.legend(loc="upper right")
    
    overflow_arr_raw = (raw_arr_valid.abs() > clip_val).sum()
    overflow_arr_clean = (clean_arr_valid.abs() > clip_val).sum()
    ax.annotate(
        f"Outliers >50km:\nRaw: {overflow_arr_raw:,}\nClean: {overflow_arr_clean:,}",
        xy=(0.03, 0.75), xycoords="axes fraction",
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8),
        fontsize=9
    )

    # -------------------------------------------------------------
    # Row 3: Log-scale Boxplots of Absolute Errors
    # -------------------------------------------------------------
    # Departure Boxplot
    ax = axes[2, 0]
    bdata_dep_raw = df["abs_raw_dep"].dropna()
    bdata_dep_raw = bdata_dep_raw[bdata_dep_raw > 0]
    bdata_dep_clean = df["abs_clean_dep"].dropna()
    bdata_dep_clean = bdata_dep_clean[bdata_dep_clean > 0]
    
    bp = ax.boxplot([bdata_dep_raw, bdata_dep_clean], tick_labels=["Raw", "Clean"], patch_artist=True, showfliers=False)
    colors = ["#ffcc80", "#90caf9"]
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
    ax.set_yscale("log")
    ax.set_title("Departure Absolute Error Comparison (Log Scale)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Absolute Error |dist - fd4| (meters)")
    ax.grid(True, which="both", ls="--", alpha=0.5)

    # Arrival Boxplot
    ax = axes[2, 1]
    bdata_arr_raw = df["abs_raw_arr"].dropna()
    bdata_arr_raw = bdata_arr_raw[bdata_arr_raw > 0]
    bdata_arr_clean = df["abs_clean_arr"].dropna()
    bdata_arr_clean = bdata_arr_clean[bdata_arr_clean > 0]
    
    bp = ax.boxplot([bdata_arr_raw, bdata_arr_clean], tick_labels=["Raw", "Clean"], patch_artist=True, showfliers=False)
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
    ax.set_yscale("log")
    ax.set_title("Arrival Absolute Error Comparison (Log Scale)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Absolute Error |dist - fd4| (meters)")
    ax.grid(True, which="both", ls="--", alpha=0.5)

    # -------------------------------------------------------------
    # Row 4: Error vs Error Correlation Scatter (±20km)
    # -------------------------------------------------------------
    limit_val = 20000
    
    # Departure Scatter
    ax = axes[3, 0]
    valid_dep = df.dropna(subset=["err_raw_dep", "err_clean_dep"])
    ax.scatter(valid_dep["err_raw_dep"], valid_dep["err_clean_dep"], alpha=0.1, s=2, color="purple")
    ax.axhline(0, color="black", linestyle="--", linewidth=1.5)
    ax.axvline(0, color="black", linestyle="--", linewidth=1.5)
    ax.set_xlim(-limit_val, limit_val)
    ax.set_ylim(-limit_val, limit_val)
    ax.set_title("Departure: Raw Error vs. Clean Error (Zoom ±20km)", fontsize=12, fontweight="bold")
    ax.set_xlabel("Raw Signed Error (meters)")
    ax.set_ylabel("Clean Signed Error (meters)")

    # Arrival Scatter
    ax = axes[3, 1]
    valid_arr = df.dropna(subset=["err_raw_arr", "err_clean_arr"])
    ax.scatter(valid_arr["err_raw_arr"], valid_arr["err_clean_arr"], alpha=0.1, s=2, color="purple")
    ax.axhline(0, color="black", linestyle="--", linewidth=1.5)
    ax.axvline(0, color="black", linestyle="--", linewidth=1.5)
    ax.set_xlim(-limit_val, limit_val)
    ax.set_ylim(-limit_val, limit_val)
    ax.set_title("Arrival: Raw Error vs. Clean Error (Zoom ±20km)", fontsize=12, fontweight="bold")
    ax.set_xlabel("Raw Signed Error (meters)")
    ax.set_ylabel("Clean Signed Error (meters)")

    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


def main():
    setup_file_logger(log_filename="analysis.log")
    logger.info("Starting R2-1 distance error analysis & visualization pipeline.")
    
    print("=" * 75)
    print("R2-1 DISTANCE ERROR STATISTICAL ANALYSIS & VISUALIZATION")
    print("=" * 75)

    if not INPUT_PARQUET.exists():
        raise FileNotFoundError(f"Input distance table missing: {INPUT_PARQUET}")

    print(f"\nLoading dataset from:\n  {INPUT_PARQUET}")
    df = pd.read_parquet(INPUT_PARQUET)
    total_flights = len(df)
    print(f"Loaded {total_flights:,} total flight records.")

    # Parse route metadata
    df = parse_route_info(df)

    # Compute signed & absolute errors
    df["err_raw_dep"] = df["raw_dep_horiz_m"] - df["fd4_dep_horiz_m"]
    df["abs_raw_dep"] = df["err_raw_dep"].abs()
    
    df["err_clean_dep"] = df["clean_dep_horiz_m"] - df["fd4_dep_horiz_m"]
    df["abs_clean_dep"] = df["err_clean_dep"].abs()

    df["err_raw_arr"] = df["raw_arr_horiz_m"] - df["fd4_arr_horiz_m"]
    df["abs_raw_arr"] = df["err_raw_arr"].abs()

    df["err_clean_arr"] = df["clean_arr_horiz_m"] - df["fd4_arr_horiz_m"]
    df["abs_clean_arr"] = df["err_clean_arr"].abs()

    # Restricted comparison mask: evaluate raw vs clean on the subset where raw is available
    valid_raw_dep_mask = df["raw_dep_horiz_m"].notna()
    valid_raw_arr_mask = df["raw_arr_horiz_m"].notna()

    # Calculate summary metrics
    summaries = [
        compute_metrics(df.loc[valid_raw_dep_mask, "err_raw_dep"], df.loc[valid_raw_dep_mask, "abs_raw_dep"], total_flights, "Raw Dep (Valid Only)"),
        compute_metrics(df["err_clean_dep"], df["abs_clean_dep"], total_flights, "Clean Dep (All Flights)"),
        compute_metrics(df.loc[valid_raw_dep_mask, "err_clean_dep"], df.loc[valid_raw_dep_mask, "abs_clean_dep"], total_flights, "Clean Dep (Raw Valid Subset)"),
        compute_metrics(df.loc[valid_raw_arr_mask, "err_raw_arr"], df.loc[valid_raw_arr_mask, "abs_raw_arr"], total_flights, "Raw Arr (Valid Only)"),
        compute_metrics(df["err_clean_arr"], df["abs_clean_arr"], total_flights, "Clean Arr (All Flights)"),
        compute_metrics(df.loc[valid_raw_arr_mask, "err_clean_arr"], df.loc[valid_raw_arr_mask, "abs_clean_arr"], total_flights, "Clean Arr (Raw Valid Subset)"),
    ]

    summary_df = pd.DataFrame(summaries)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(OUTPUT_SUMMARY_CSV, index=False)
    print(f"\nSaved statistical summary to:\n  {OUTPUT_SUMMARY_CSV}")

    print("\n" + "=" * 75)
    print("STATISTICAL ERROR SUMMARY")
    print("=" * 75)
    print(summary_df.to_string(index=False))

    # Calculate Top 15 Route Outliers (min 20 flights)
    route_stats = df.groupby("route").agg(
        n_flights=("flight_id", "count"),
        med_abs_clean_dep_err=("abs_clean_dep", "median"),
        med_abs_clean_arr_err=("abs_clean_arr", "median"),
        p99_abs_clean_dep_err=("abs_clean_dep", lambda x: x.quantile(0.99)),
        p99_abs_clean_arr_err=("abs_clean_arr", lambda x: x.quantile(0.99)),
    ).query("n_flights >= 20")

    top_dep_outliers = route_stats.sort_values("med_abs_clean_dep_err", ascending=False).head(15)
    top_arr_outliers = route_stats.sort_values("med_abs_clean_arr_err", ascending=False).head(15)

    top_dep_outliers.to_csv(OUTPUT_DEP_OUTLIERS_CSV)
    top_arr_outliers.to_csv(OUTPUT_ARR_OUTLIERS_CSV)
    print(f"\nSaved Departure Route Outliers to:\n  {OUTPUT_DEP_OUTLIERS_CSV}")
    print(f"Saved Arrival Route Outliers to:\n  {OUTPUT_ARR_OUTLIERS_CSV}")

    print("\n" + "=" * 75)
    print("TOP 15 DEPARTURE ROUTE OUTLIERS (by Median Absolute Clean Dep Error)")
    print("=" * 75)
    print(top_dep_outliers[["n_flights", "med_abs_clean_dep_err", "p99_abs_clean_dep_err"]].to_string())

    print("\n" + "=" * 75)
    print("TOP 15 ARRIVAL ROUTE OUTLIERS (by Median Absolute Clean Arr Error)")
    print("=" * 75)
    print(top_arr_outliers[["n_flights", "med_abs_clean_arr_err", "p99_abs_clean_arr_err"]].to_string())

    # Generate 3x2 diagnostic plot
    print(f"\nGenerating 3x2 diagnostic plot figure...")
    generate_plots(df, OUTPUT_PLOT_SVG)
    print(f"Saved diagnostic plots to:\n  {OUTPUT_PLOT_SVG}")
    print("=" * 75)


if __name__ == "__main__":
    main()
