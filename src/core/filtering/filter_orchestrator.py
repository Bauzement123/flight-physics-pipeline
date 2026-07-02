"""
Module 1.1b: Master Filter Corridor Orchestrator
Batch-generates target route corridor files from the master flight registry
based on ranked traffic indices defined in the RouteSummary.
"""
import argparse
import logging
from pathlib import Path
import pandas as pd

from src.common.config import FLIGHT_LISTS_DIR, ROUTE_SUMMARY_PARQUET, MASTER_FLIGHTS_FILE
from src.common.utils import load_route_summary, split_route_string

# Configure logger
logger = logging.getLogger(__name__)

def extract_airports_from_ranks(route_summary_path: str, ranks: list, min_distance: float = None) -> pd.DataFrame:
    """
    Loads the RouteSummary pickle, filters it by ranks, and splits the routes.
    """
    logger.info(f"Extracting route corridors from summary for ranks: {ranks}")
    df_summary = load_route_summary(route_summary_path)
    
    if df_summary.empty:
        logger.warning("No records found in RouteSummary.")
        return pd.DataFrame()

    # Filter by selected ranks
    filtered_summary = df_summary[df_summary['rank'].isin(ranks)].copy()
    if filtered_summary.empty:
        logger.warning("No matching ranks found in the RouteSummary.")
        return pd.DataFrame()

    # Filter by minimum distance if specified
    if min_distance is not None:
        if 'distance_m' in filtered_summary.columns:
            before_count = len(filtered_summary)
            filtered_summary = filtered_summary[filtered_summary['distance_m'] >= min_distance * 1000.0].copy()
            excluded_count = before_count - len(filtered_summary)
            logger.info(f"Filtered by minimum route distance >= {min_distance} km. Excluded {excluded_count} routes. Remaining: {len(filtered_summary)}")
        else:
            logger.warning("Column 'distance_m' not found in RouteSummary. Skipping distance filtering.")

    # Split "DEP -> ARR" into separate columns
    deps = []
    arrs = []
    for _, row in filtered_summary.iterrows():
        dep, arr = split_route_string(row['route'])
        deps.append(dep)
        arrs.append(arr)
        
    filtered_summary['dep'] = deps
    filtered_summary['arr'] = arrs

    return filtered_summary[['rank', 'dep', 'arr']]


def filtered_lists_from_ranks(airports_df: pd.DataFrame, master_file_path: str, output_dir: str):
    """
    Processes selected route corridors, filters the master registry, and saves .parquet sliced lists.
    """
    master_path = Path(master_file_path)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if not master_path.exists():
        logger.error(f"Master flight registry not found at: {master_path}")
        return

    # Determine file format & load database
    is_parquet = master_path.suffix.lower() == '.parquet'
    logger.info(f"Reading master database: {master_path.name} (Format: {'Parquet' if is_parquet else 'CSV'})")
    
    try:
        if is_parquet:
            master_df = pd.read_parquet(master_path)
        else:
            master_df = pd.read_csv(master_path, low_memory=False)
    except Exception as e:
        logger.error(f"Failed to load master database: {e}")
        return

    # Map column headers
    dep_col = None
    arr_col = None
    for col in ['estdepartureairport', 'estdepatureairport', 'origin', 'departure']:
        if col in master_df.columns:
            dep_col = col
            break
    for col in ['estarrivalairport', 'destination', 'arrival']:
        if col in master_df.columns:
            arr_col = col
            break

    if not dep_col or not arr_col:
        logger.error(f"Could not identify airport columns in master database. Columns: {list(master_df.columns)}")
        return

    logger.info(f"Using database mapping: Departure='{dep_col}', Arrival='{arr_col}'")

    # Slice and generate the files
    for _, row in airports_df.iterrows():
        rank = row['rank']
        dep, arr = row['dep'], row['arr']
        
        target_filename = f"{dep}-{arr}.parquet"
        target_path = out_path / target_filename

        if target_path.exists():
            logger.info(f"Skipping Rank {rank} ({dep} -> {arr}): {target_filename} already exists.")
            continue

        logger.info(f"Processing Rank {rank}: Slicing corridor for {dep} -> {arr}...")

        # Filter target routes
        filtered_flights = master_df[(master_df[dep_col] == dep) & (master_df[arr_col] == arr)].copy()

        if filtered_flights.empty:
            logger.warning(f"  -> No flights found in registry for {dep} -> {arr}.")
            continue

        # Save to Parquet
        try:
            filtered_flights.to_parquet(target_path, index=False)
            logger.info(f"  -> Success: Saved {len(filtered_flights):,} flights to {target_path}")
        except Exception as e:
            logger.error(f"  -> Failed to write parquet list for {dep}->{arr}: {e}")


def orchestrate_filtered_list_creation(route_summary_path: str, master_file_path: str, output_dir: str, lower_rank: int, upper_rank: int, min_distance: float = None):
    """
    Orchestrates the creation of filtered lists for a continuous corridor of ranks.
    """
    ranks_corridor = list(range(lower_rank, upper_rank + 1))
    logger.info(f"Orchestrating corridor slicing for ranks: {lower_rank} to {upper_rank}")
    
    airports_df = extract_airports_from_ranks(route_summary_path, ranks_corridor, min_distance=min_distance)
    if not airports_df.empty:
        filtered_lists_from_ranks(airports_df, master_file_path, output_dir)


if __name__ == "__main__":
    # Configure logging at application entry point
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

    parser = argparse.ArgumentParser(description="Master Filter Orchestrator - Corridor slicing.")
    
    parser.add_argument("--route-summary", default=str(ROUTE_SUMMARY_PARQUET), help="Path to RouteSummary parquet or pickle file")
    parser.add_argument("--master-file", "--file", default=str(MASTER_FLIGHTS_FILE), help="Path to master flights database Parquet/CSV file")
    parser.add_argument("--out-dir", default=str(FLIGHT_LISTS_DIR), help="Output directory for sliced parquet lists")
    
    # Selection Strategy (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated list of specific ranks (e.g., '1,76,205')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of rank corridor")
    
    parser.add_argument("--upper-rank", type=int, help="Upper bound of rank corridor (Required if --lower-rank is used)")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")

    args = parser.parse_args()

    # Set up file logger to mirror stdout to filtering.log in centralized directory
    from src.common.utils import setup_file_logger
    setup_file_logger(log_filename="filtering.log")

    # Validate corridor bounds
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")

    # Execute corresponding orchestration path
    if args.ranks:
        try:
            target_ranks = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
            
        airports = extract_airports_from_ranks(args.route_summary, target_ranks, min_distance=args.min_distance)
        if not airports.empty:
            filtered_lists_from_ranks(airports, args.master_file, args.out_dir)
            
    elif args.lower_rank is not None and args.upper_rank is not None:
        orchestrate_filtered_list_creation(
            route_summary_path=args.route_summary,
            master_file_path=args.master_file,
            output_dir=args.out_dir,
            lower_rank=args.lower_rank,
            upper_rank=args.upper_rank,
            min_distance=args.min_distance
        )
