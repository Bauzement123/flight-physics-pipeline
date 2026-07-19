"""
Pipeline Module: Build Master Flights Route Summary

Single entrypoint for generating, filtering, and distance-enriching the master flights
route summary. Reads master_flights.parquet directly, applies spatial/quality filters,
computes vectorized Haversine distances, and exports canonical output files.
"""

from __future__ import annotations

import argparse
import logging
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

from src.common.config import (
    MASTER_FLIGHTS_FILE,
    MASTER_FLIGHTS_DB_DIR,
    MASTER_FLIGHTS_REPORTS_DIR,
    ROUTE_SUMMARY_PARQUET,
    ROUTE_SUMMARY_PKL,
    ROUTE_SUMMARY_CSV,
    EUR_LAT_MIN,
    EUR_LAT_MAX,
    EUR_LON_MIN,
    EUR_LON_MAX,
    init_runtime,
)
from src.common.utils import (
    setup_file_logger,
    haversine_distance_m,
    resolve_airport_coordinates,
)

logger = logging.getLogger(__name__)


def load_master_flights(input_path: Path) -> pd.DataFrame:
    """Loads input master flights dataset (Parquet or CSV) and validates schema."""
    logger.info(f"Loading master flights dataset from: {input_path}")
    if not input_path.exists():
        raise FileNotFoundError(f"Master flights file not found at: {input_path}")

    if input_path.suffix.lower() == ".parquet":
        df = pd.read_parquet(input_path)
    else:
        df = pd.read_csv(input_path)

    logger.info(f"Loaded {len(df):,} flight records.")
    
    dep_col = "estdepartureairport" if "estdepartureairport" in df.columns else "estdepatureairport"
    arr_col = "estarrivalairport"
    
    if dep_col not in df.columns or arr_col not in df.columns:
        raise KeyError(f"Required airport columns ({dep_col}, {arr_col}) not found in dataset.")

    df["estdepartureairport"] = df[dep_col].astype(str).str.strip()
    df["estarrivalairport"] = df[arr_col].astype(str).str.strip()
    df["route"] = df["estdepartureairport"] + " -> " + df["estarrivalairport"]
    return df


def compute_durations(df: pd.DataFrame) -> pd.Series:
    """Computes flight durations in minutes from timestamps."""
    if "duration" in df.columns and df["duration"].notna().any():
        return df["duration"].fillna(0)

    if "firstseen" not in df.columns or "lastseen" not in df.columns:
        logger.warning("Missing 'firstseen' or 'lastseen' columns. Defaulting duration to 0.")
        return pd.Series(0.0, index=df.index)

    try:
        firstseen_num = pd.to_numeric(df["firstseen"])
        lastseen_num = pd.to_numeric(df["lastseen"])
        duration = (lastseen_num - firstseen_num) / 60.0
    except (ValueError, TypeError):
        firstseen_dt = pd.to_datetime(df["firstseen"], errors="coerce", utc=True)
        lastseen_dt = pd.to_datetime(df["lastseen"], errors="coerce", utc=True)
        duration = (lastseen_dt - firstseen_dt).dt.total_seconds() / 60.0

    return duration.fillna(0.0)


def aggregate_routes(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Computes sub-aggregations (by route, typecode, icao24) and total route aggregations."""
    logger.info("Aggregating flight counts and duration statistics by route...")
    
    df_sub = df.groupby(["route", "typecode", "icao24"]).agg(
        callsigns=("callsign", "unique"),
        sub_count=("route", "size"),
        duration_min=("duration", "min"),
        duration_max=("duration", "max"),
        duration_median=("duration", "median"),
    ).reset_index()

    df_total = df.groupby("route").agg(
        total_route_count=("route", "size"),
        route_duration_min=("duration", "min"),
        route_duration_max=("duration", "max"),
        route_duration_median=("duration", "median"),
        route_duration_sum=("duration", "sum"),
    ).reset_index()

    df_joint = pd.merge(df_sub, df_total, on="route", how="left").sort_values(
        by=["total_route_count", "sub_count", "route"],
        ascending=[False, False, True],
    )

    summary_df = df_joint.groupby("route").agg(
        total_route_count=("total_route_count", "first"),
        route_duration_min=("route_duration_min", "first"),
        route_duration_max=("route_duration_max", "first"),
        route_duration_median=("route_duration_median", "first"),
        route_duration_sum=("route_duration_sum", "first"),
        unique_typecodes=("typecode", "unique"),
    ).sort_values(by="total_route_count", ascending=False).reset_index()

    return summary_df, df_joint


def filter_routes(summary_df: pd.DataFrame, airports_db: dict) -> pd.DataFrame:
    """Applies circular flight filter (DEP != ARR) and European bounding box filter."""
    initial_len = len(summary_df)
    initial_flights = summary_df["total_route_count"].sum()
    logger.info(f"Filtering routes... Initial: {initial_len:,} routes ({initial_flights:,} flights).")

    parsed_routes = summary_df["route"].str.split(r"\s*->\s*", expand=True)
    summary_df["estdepartureairport"] = parsed_routes[0].str.strip()
    summary_df["estarrivalairport"] = parsed_routes[1].str.strip()

    # Filter A: Remove circular routes
    df_non_circular = summary_df[summary_df["estdepartureairport"] != summary_df["estarrivalairport"]].copy()
    dropped_circ = initial_len - len(df_non_circular)
    logger.info(f"  Filter A (Non-circular): Dropped {dropped_circ:,} circular routes. Remaining: {len(df_non_circular):,} routes.")

    # Filter B: European Bounding Box filter
    dep_lats = df_non_circular["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    dep_lons = df_non_circular["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))
    arr_lats = df_non_circular["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    arr_lons = df_non_circular["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))

    in_box_mask = (
        (dep_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (dep_lons.between(EUR_LON_MIN, EUR_LON_MAX)) &
        (arr_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (arr_lons.between(EUR_LON_MIN, EUR_LON_MAX))
    )

    df_filtered = df_non_circular[in_box_mask].copy()
    dropped_oob = len(df_non_circular) - len(df_filtered)
    logger.info(f"  Filter B (European Bounding Box): Dropped {dropped_oob:,} out-of-bounds routes. Remaining: {len(df_filtered):,} routes.")

    return df_filtered


def enrich_distances(df_filtered: pd.DataFrame, airports_db: dict) -> pd.DataFrame:
    """Computes vectorized Haversine great-circle distances in meters for filtered routes."""
    logger.info("Computing vectorized Haversine route distances...")
    dep_lats = df_filtered["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    dep_lons = df_filtered["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))
    arr_lats = df_filtered["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    arr_lons = df_filtered["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))

    df_filtered["distance_m"] = haversine_distance_m(dep_lats, dep_lons, arr_lats, arr_lons)
    return df_filtered


def rank_routes(df_enriched: pd.DataFrame) -> pd.DataFrame:
    """Sorts routes by total flight volume and assigns numerical rank."""
    df_sorted = df_enriched.sort_values(by="total_route_count", ascending=False).reset_index(drop=True)
    df_sorted["rank"] = range(1, len(df_sorted) + 1)
    
    desired_cols = [
        "route", "total_route_count", "route_duration_min", "route_duration_max",
        "route_duration_median", "route_duration_sum", "unique_typecodes", "rank", "distance_m"
    ]
    return df_sorted[desired_cols]


def export_canonical(df_final: pd.DataFrame) -> None:
    """Exports canonical route summary files (.parquet, .pkl, .csv)."""
    logger.info(f"Exporting canonical Parquet to: {ROUTE_SUMMARY_PARQUET}")
    df_final.to_parquet(ROUTE_SUMMARY_PARQUET, index=False)

    logger.info(f"Exporting canonical Pickle to: {ROUTE_SUMMARY_PKL}")
    with open(ROUTE_SUMMARY_PKL, "wb") as f:
        pickle.dump(df_final, f)

    logger.info(f"Exporting canonical CSV to: {ROUTE_SUMMARY_CSV}")
    df_final.to_csv(ROUTE_SUMMARY_CSV, index=False)


def export_reports(df_final: pd.DataFrame, df_joint: pd.DataFrame, reports_dir: Path) -> None:
    """Exports detailed reports (rankings text, distribution CSV, detailed sub-aggregations)."""
    reports_dir.mkdir(parents=True, exist_ok=True)

    detailed_csv = reports_dir / "master_flights_detailed_counts.csv"
    df_joint.to_csv(detailed_csv, index=False)

    total_routes = len(df_final)
    total_flights = df_final["total_route_count"].sum()

    df_dist = df_final.copy()
    df_dist["cum_flights"] = df_dist["total_route_count"].cumsum()
    df_dist["cum_flights_percent"] = (df_dist["cum_flights"] / total_flights) * 100.0
    df_dist["cum_routes_percent"] = (df_dist["rank"] / total_routes) * 100.0

    dist_csv = reports_dir / "master_flights_route_distribution.csv"
    df_dist.to_csv(dist_csv, index=False)

    rankings_txt = reports_dir / "master_flights_route_rankings.txt"
    with open(rankings_txt, "w", encoding="utf-8") as f:
        f.write("Rank | Route | Total_Flights | Distance | Duration (Min/Med/Max) | (Unique_Typecodes)\n")
        f.write("-" * 110 + "\n")
        for _, row in df_final.iterrows():
            tc_list = [str(tc) for tc in row["unique_typecodes"] if pd.notna(tc) and str(tc).lower() != "nan"]
            tc_str = ", ".join(tc_list)
            dist_val = row.get("distance_m", np.nan)
            dist_str = f"{dist_val / 1000.0:.1f} km" if pd.notna(dist_val) else "N/A"
            f.write(
                f"{int(row['rank'])}. {row['route']} | {row['total_route_count']} | "
                f"{dist_str} | "
                f"{row['route_duration_min']:.1f}/{row['route_duration_median']:.1f}/{row['route_duration_max']:.1f} | "
                f"({tc_str})\n"
            )

    logger.info(f"Saved reports to directory: {reports_dir}")


def build_route_summary(input_path: Path, reports_dir: Path, reports_only: bool = False) -> pd.DataFrame:
    """Main orchestrator for route summary generation and enrichment."""
    df = load_master_flights(input_path)
    df["duration"] = compute_durations(df)

    summary_df, df_joint = aggregate_routes(df)

    parsed_routes = summary_df["route"].str.split(r"\s*->\s*", expand=True)
    unique_icaos = pd.concat([parsed_routes[0].str.strip(), parsed_routes[1].str.strip()]).dropna().unique()
    airports_db = resolve_airport_coordinates(list(unique_icaos))

    df_filtered = filter_routes(summary_df, airports_db)
    df_enriched = enrich_distances(df_filtered, airports_db)
    df_final = rank_routes(df_enriched)

    if not reports_only:
        export_canonical(df_final)

    export_reports(df_final, df_joint, reports_dir)
    logger.info(f"SUCCESS: Route summary complete. Output contains {len(df_final):,} clean routes.")
    return df_final


def main() -> None:
    init_runtime()
    setup_file_logger(log_filename="acquisition.log")

    parser = argparse.ArgumentParser(description="Build and enrich Master Flights Route Summary")
    parser.add_argument("--input", help="Path to master_flights.parquet dataset (defaults to config.MASTER_FLIGHTS_FILE)")
    parser.add_argument("--reports-dir", help="Directory for output reports (defaults to config.MASTER_FLIGHTS_REPORTS_DIR)")
    parser.add_argument("--reports-only", action="store_true", help="Generate reports without overwriting canonical route summary files")

    args = parser.parse_args()

    input_path = Path(args.input) if args.input else MASTER_FLIGHTS_FILE
    reports_dir = Path(args.reports_dir) if args.reports_dir else MASTER_FLIGHTS_REPORTS_DIR

    build_route_summary(input_path, reports_dir, reports_only=args.reports_only)


if __name__ == "__main__":
    main()
