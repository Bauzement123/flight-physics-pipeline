"""
Plots a daily flight distribution histogram by orchestrating the extraction from master_flights.
Uses a skip-gate to dynamically generate the CSV cache if it doesn't already exist.
"""

import argparse
import sys
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import logging

import pyarrow.dataset as ds
from src.common.config import DATA_DIR, ALL_TARGET_FAMILIES, MASTER_FLIGHTS_FILE, is_supported_typecode
from src.common.utils import setup_file_logger
from src.data_manager.io_utils import read_route_summary
from src.data_manager.schemas import RouteSummaryQuery, MasterFlightQuery

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description="Extract daily flights and plot a distribution histogram.")
    parser.add_argument("--typecode", type=str, help="Filter by specific aircraft typecode.")
    parser.add_argument("--rank-range", type=int, nargs=2, help="Filter by route ranks (start end inclusive). e.g., --rank-range 1 1000")
    parser.add_argument("--ranks", type=int, nargs="+", help="Filter by specific route ranks (e.g. 1 2 3)")
    parser.add_argument("--departure-airport", type=str, help="Departure airport ICAO filter.")
    parser.add_argument("--arrival-airport", type=str, help="Arrival airport ICAO filter.")
    parser.add_argument("--year", type=int, default=2025, help="Year to analyze (default: 2025).")
    parser.add_argument("--only-csv", action="store_true", help="Only generate the CSV cache; do not plot.")
    parser.add_argument("--log-file", type=str, default="analysis.log", help="Log file name in data/logs/")
    return parser.parse_args()

def get_csv_path(args) -> Path:
    """Generate a deterministic CSV path based on filters."""
    parts = ["daily_flights"]
    if args.typecode:
        parts.append(args.typecode)
    else:
        parts.append("all_supported")
        
    if args.rank_range:
        parts.append(f"rank_{args.rank_range[0]}_{args.rank_range[1]}")
    elif args.ranks:
        parts.append("ranks_" + "_".join(str(r) for r in sorted(args.ranks)))
        
    if args.departure_airport:
        parts.append(f"dep_{args.departure_airport}")
    if args.arrival_airport:
        parts.append(f"arr_{args.arrival_airport}")
        
    out_dir = DATA_DIR / "results" / "flight_distributions"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / ("_".join(parts) + ".csv")

def generate_csv_cache(args, csv_path: Path):
    """Skip Gate 1: Generate the CSV cache using vectorized PyArrow pushdown."""
    logger.info("CSV cache not found. Initiating vectorized whole-year extraction...")
    
    # 1. Resolve Routes if ranks are provided
    target_routes = None
    ranks = None
    if args.rank_range:
        ranks = list(range(args.rank_range[0], args.rank_range[1] + 1))
    elif args.ranks:
        ranks = args.ranks
        
    if ranks:
        logger.info(f"Resolving {len(ranks)} ranks to specific routes...")
        summary_df = read_route_summary(RouteSummaryQuery(ranks=ranks), columns=["route"])
        target_routes = set(r.replace(" -> ", "-") for r in summary_df["route"].tolist())
        logger.info(f"Resolved to {len(target_routes)} valid target routes.")
        if not target_routes:
            logger.warning("No routes found for the given ranks. Output will be empty.")
            
    # Validate typecode
    typecodes = None
    if args.typecode:
        if not is_supported_typecode(args.typecode):
            logger.warning(f"Typecode {args.typecode} not in ALL_TARGET_FAMILIES. It may be skipped.")
        typecodes = [args.typecode]
    else:
        typecodes = ALL_TARGET_FAMILIES

    # Open PyArrow Dataset
    if not Path(MASTER_FLIGHTS_FILE).exists():
        raise FileNotFoundError(f"master_flights file not found: {MASTER_FLIGHTS_FILE}")
    master_dataset = ds.dataset(str(MASTER_FLIGHTS_FILE))

    # Fast PyArrow pushdown filter
    exprs = []
    start_ts = pd.Timestamp(f"{args.year}-01-01", tz="UTC")
    end_ts = pd.Timestamp(f"{args.year + 1}-01-01", tz="UTC") - pd.Timedelta(nanoseconds=1)
    exprs.append((ds.field("firstseen") >= start_ts) & (ds.field("firstseen") <= end_ts))
    
    if typecodes:
        exprs.append(ds.field("typecode").isin(typecodes))
        
    if args.departure_airport:
        exprs.append(ds.field("estdepartureairport") == args.departure_airport)
    if args.arrival_airport:
        exprs.append(ds.field("estarrivalairport") == args.arrival_airport)
        
    if target_routes:
        deps = list(set(r.split("-")[0] for r in target_routes))
        arrs = list(set(r.split("-")[1] for r in target_routes))
        exprs.append(ds.field("estdepartureairport").isin(deps) & ds.field("estarrivalairport").isin(arrs))

    combined = exprs[0]
    for e in exprs[1:]:
        combined = combined & e

    scan_columns = ["firstseen"]
    if target_routes or args.departure_airport or args.arrival_airport:
        scan_columns += ["estdepartureairport", "estarrivalairport"]
    scanner = master_dataset.scanner(filter=combined, columns=scan_columns)
    
    daily_counts = {}
    total_processed = 0

    for batch in scanner.to_batches():
        df_batch = batch.to_pandas()
        total_processed += len(df_batch)
        
        if target_routes and not df_batch.empty:
            df_batch["route"] = df_batch["estdepartureairport"] + "-" + df_batch["estarrivalairport"]
            df_batch = df_batch[df_batch["route"].isin(target_routes)]

        if not df_batch.empty:
            df_batch["dep_date"] = df_batch["firstseen"].dt.strftime("%Y%m%d").astype(int)
            counts = df_batch.groupby("dep_date").size()
            for date_int, count in counts.items():
                daily_counts[date_int] = daily_counts.get(date_int, 0) + count

    logger.info(f"Streamed {total_processed} candidate rows in batches.")

    # Build full 365-day dataframe
    date_range = pd.date_range(start=f"{args.year}-01-01", end=f"{args.year}-12-31", freq="D")
    df_dates = pd.DataFrame({"dep_date": [int(d.strftime("%Y%m%d")) for d in date_range]})
    
    df_counts = pd.DataFrame(list(daily_counts.items()), columns=["dep_date", "flight_count"]) if daily_counts else pd.DataFrame(columns=["dep_date", "flight_count"])
    final_df = pd.merge(df_dates, df_counts, on="dep_date", how="left").fillna(0)
    final_df["flight_count"] = final_df["flight_count"].astype(int)

    final_df.to_csv(csv_path, index=False)
    logger.info(f"CSV generation complete: {csv_path} ({len(final_df)} days saved)")

def main():
    args = parse_args()
    setup_file_logger(log_filename=args.log_file)
    
    csv_path = get_csv_path(args)
    
    # Skip Gate 1: Generate if missing
    if not csv_path.exists():
        generate_csv_cache(args, csv_path)
    else:
        logger.info(f"CSV cache found at {csv_path}. Skipping extraction.")
        
    # Skip Gate 2: Stop if --only-csv
    if args.only_csv:
        logger.info("Run finished (--only-csv flag provided).")
        sys.exit(0)
        
    # Build & Plot Histogram
    logger.info(f"Loading data from {csv_path} for plotting...")
    df = pd.read_csv(csv_path)
    
    if df.empty:
        logger.warning("CSV data is empty. Nothing to plot.")
        sys.exit(0)
        
    df['date'] = pd.to_datetime(df['dep_date'].astype(str), format='%Y%m%d')
    
    out_dir = DATA_DIR / "analysis" / "plots" / "daily_distributions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_filename = csv_path.stem + "_histogram.svg"
    out_path = out_dir / out_filename
    
    logger.info("Generating plot...")
    plt.figure(figsize=(12, 6))
    plt.bar(df['date'], df['flight_count'], color='skyblue', edgecolor='black', width=1.0)
    
    plt.title(f"Daily Flight Distribution ({csv_path.stem})", fontsize=16)
    plt.xlabel("Date", fontsize=14)
    plt.ylabel("Number of Flights", fontsize=14)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    
    logger.info(f"Saving plot to {out_path}...")
    plt.savefig(out_path, format="svg")
    plt.close()
    
    logger.info("Plotting complete.")

if __name__ == "__main__":
    main()
