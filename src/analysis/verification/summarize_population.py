"""
Analysis Module: Route Summary Inspection & Pareto Statistics

Read-only inspection tool that loads the canonical master flights route summary
Parquet file (or custom summary dataset) and prints human-readable route rankings
and Pareto distribution statistics.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import numpy as np
import pandas as pd

from src.common.config import REPORTS_DIR, ROUTE_SUMMARY_PARQUET
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def load_dataset(input_file: Path) -> pd.DataFrame:
    """Loads route summary dataset from Parquet, Pickle, or CSV."""
    logger.info(f"Loading route summary dataset from: {input_file}")
    if not input_file.exists():
        raise FileNotFoundError(f"Route summary file not found at: {input_file}")

    suffix = input_file.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(input_file)
    elif suffix in (".pkl", ".pickle"):
        df = pd.read_pickle(input_file)
    else:
        df = pd.read_csv(input_file)

    logger.info(f"Loaded {len(df):,} route summary records.")
    return df


def print_pareto_statistics(df: pd.DataFrame) -> None:
    """Calculates and logs Pareto cumulative flight volume distribution metrics."""
    if "total_route_count" not in df.columns:
        logger.warning("Column 'total_route_count' missing. Skipping Pareto analysis.")
        return

    df_sorted = df.sort_values(by="total_route_count", ascending=False).reset_index(drop=True)
    total_routes = len(df_sorted)
    total_flights = df_sorted["total_route_count"].sum()

    df_sorted["rank"] = df_sorted.index + 1
    df_sorted["cum_flights"] = df_sorted["total_route_count"].cumsum()
    df_sorted["cum_flights_percent"] = (df_sorted["cum_flights"] / total_flights) * 100.0
    df_sorted["cum_routes_percent"] = (df_sorted["rank"] / total_routes) * 100.0

    logger.info("=" * 60)
    logger.info("ROUTE SUMMARY PARETO DISTRIBUTION STATISTICS")
    logger.info("=" * 60)
    logger.info(f"Total Unique Routes: {total_routes:,}")
    logger.info(f"Total Represented Flights: {total_flights:,}")

    for p in [10, 20, 50]:
        under_p = df_sorted[df_sorted["cum_routes_percent"] <= p]
        if not under_p.empty:
            vol_percent = under_p["cum_flights_percent"].iloc[-1]
            logger.info(f"  Top {p}% of routes account for {vol_percent:.2f}% of total flights.")
    logger.info("=" * 60)


def export_rankings_txt(summary_df: pd.DataFrame, output_dir: Path, name: str) -> Path:
    """Exports a human-readable route rankings log text file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_txt = output_dir / f"{name}_route_rankings.txt"
    logger.info(f"Saving route rankings text report to: {ranking_txt}")

    with open(ranking_txt, "w", encoding="utf-8") as f:
        f.write("Rank | Route | Total_Flights | Distance | Duration (Min/Med/Max) | (Unique_Typecodes)\n")
        f.write("-" * 110 + "\n")
        for _, row in summary_df.iterrows():
            unique_tc = row.get("unique_typecodes", [])
            typecodes = [str(tc) for tc in unique_tc if pd.notna(tc) and str(tc).lower() != "nan"]
            typecodes_str = ", ".join(typecodes)

            dist_val = row.get("distance_m", np.nan)
            dist_str = f"{dist_val / 1000.0:.1f} km" if pd.notna(dist_val) else "N/A"

            dur_min = row.get("route_duration_min", 0.0)
            dur_med = row.get("route_duration_median", 0.0)
            dur_max = row.get("route_duration_max", 0.0)

            rank_val = int(row.get("rank", 0))

            f.write(
                f"{rank_val}. {row['route']} | {row['total_route_count']} | "
                f"{dist_str} | "
                f"{dur_min:.1f}/{dur_med:.1f}/{dur_max:.1f} | "
                f"({typecodes_str})\n"
            )

    return ranking_txt


def process_population(input_file: Path, output_dir: Path, name: str) -> None:
    """Inspects route summary dataset, logs Pareto metrics, and writes rank text file."""
    df = load_dataset(input_file)
    print_pareto_statistics(df)
    export_rankings_txt(df, output_dir, name)


def main() -> None:
    setup_file_logger(log_filename="acquisition.log")

    parser = argparse.ArgumentParser(description="Inspect and Summarize Master Flights Route Summary")
    parser.add_argument("--input", help="Path to route summary dataset (defaults to ROUTE_SUMMARY_PARQUET)")
    parser.add_argument("--out-dir", help="Directory to save output reports")
    parser.add_argument("--name", default="master_flights", help="Name prefix for output report files")

    args = parser.parse_args()

    input_path = Path(args.input) if args.input else ROUTE_SUMMARY_PARQUET
    out_dir = Path(args.out_dir) if args.out_dir else REPORTS_DIR

    process_population(input_path, out_dir, args.name)


if __name__ == "__main__":
    main()
