"""
Module 1.2b: OpenSky Fetcher Orchestrator
Batch-processing orchestration engine. Coordinates fetching trajectories for ranked corridors
from Trino/local cache into dynamically generated dataset namespace directories.
"""
import argparse
import logging
import math
import sys
from pathlib import Path
import pandas as pd

from src.common.config import FLIGHT_LISTS_DIR, get_dataset_dir, FLIGHT_REGISTRY_DIR
from src.common.utils import load_route_summary, split_route_string, generate_dataset_name, setup_file_logger
from src.fetching import opensky_fetcher

# Setup Logging
# logging.basicConfig is configured inside the __main__ block to prevent importing script pollution.

def extract_target_routes(
    summary_path: str, 
    lower: int = None, 
    upper: int = None, 
    specific_ranks: list = None, 
    fetch_format: str = 'oneway',
    min_distance: float = None
) -> pd.DataFrame:
    """Loads the RouteSummary, applies the selected filters, and returns route codes."""
    logging.info(f"Loading route metadata from summary: {summary_path}")
    df_summary = load_route_summary(summary_path)
    
    if df_summary.empty:
        logging.error("RouteSummary is empty.")
        return pd.DataFrame()

    # Filter ranks
    if specific_ranks:
        mask = df_summary['rank'].isin(specific_ranks)
        logging.info(f"Filtering by specific ranks: {specific_ranks}")
    elif lower is not None and upper is not None:
        mask = (df_summary['rank'] >= lower) & (df_summary['rank'] <= upper)
        logging.info(f"Filtering by rank corridor: {lower} to {upper}")
    else:
        logging.error("No valid filtering criteria provided.")
        return pd.DataFrame()

    filtered_df = df_summary[mask].copy()
    if filtered_df.empty:
        logging.warning("No routes found matching the criteria.")
        return pd.DataFrame()

    # Filter by distance if requested
    if min_distance is not None:
        if 'distance_m' in filtered_df.columns:
            before_count = len(filtered_df)
            filtered_df = filtered_df[filtered_df['distance_m'] >= min_distance * 1000.0].copy()
            excluded_count = before_count - len(filtered_df)
            logging.info(f"Filtered by minimum route distance >= {min_distance} km. Excluded {excluded_count} routes. Remaining: {len(filtered_df)}")
        else:
            logging.warning("Column 'distance_m' not found in RouteSummary. Skipping distance filtering.")

    # Bidirectional route resolution
    if fetch_format == 'roundtrip':
        logging.info("Roundtrip requested. Resolving inverse return flight paths...")
        inverse_routes = []
        for route_str in filtered_df['route']:
            dep, arr = split_route_string(route_str)
            if dep != 'UNK':
                inverse_routes.append(f"{arr} -> {dep}")
        
        # Look up inverse routes in the master database
        return_df = df_summary[df_summary['route'].isin(inverse_routes)].copy()
        
        before_count = len(filtered_df)
        filtered_df = pd.concat([filtered_df, return_df]).drop_duplicates(subset=['route'])
        added_count = len(filtered_df) - before_count
        logging.info(f"Resolved {added_count} return routes. Total target routes: {len(filtered_df)}")

    # Split route DEP -> ARR
    deps = []
    arrs = []
    for _, row in filtered_df.iterrows():
        dep, arr = split_route_string(row['route'])
        deps.append(dep)
        arrs.append(arr)
        
    filtered_df['dep'] = deps
    filtered_df['arr'] = arrs

    # Standardize flights count column
    if 'no_of_flights' not in filtered_df.columns:
        possible_count_cols = [c for c in filtered_df.columns if 'count' in c.lower() or 'flight' in c.lower()]
        if possible_count_cols:
            filtered_df.rename(columns={possible_count_cols[0]: 'no_of_flights'}, inplace=True)
        else:
            logging.error("Could not locate the flight count column in the RouteSummary!")
            return pd.DataFrame()

    return filtered_df[['rank', 'dep', 'arr', 'no_of_flights']]


def compute_fetch_targets(
    routes_df: pd.DataFrame, 
    input_dir: str, 
    strategy: str, 
    value: float,
    start_date: str = None,
    end_date: str = None,
    typecode: str = None
) -> list:
    """Verifies file existence and calculates sample size quotas after applying filters."""
    execution_plan = []
    base_dir = Path(input_dir)
    
    logging.info("Scanning flight lists and calculating sample quotas...")

    for _, row in routes_df.iterrows():
        rank = row['rank']
        dep, arr = row['dep'], row['arr']
        
        expected_filename = f"{dep}-{arr}.parquet"
        file_path = base_dir / expected_filename
        
        # Check if list is missing (no fallback generation)
        if not file_path.exists():
            logging.error(f"Flight list missing: {expected_filename}. Slicing must be run first. Skipping corridor {dep} -> {arr}.")
            continue

        # Load and filter in-memory to get actual capacity
        try:
            df_flights = pd.read_parquet(file_path)
            from src.fetching.opensky_fetcher import filter_flight_list
            df_filtered = filter_flight_list(df_flights, start_date=start_date, end_date=end_date, typecode=typecode)
            capacity = len(df_filtered)
            logging.info(f"Corridor {dep} -> {arr}: filtered from {len(df_flights)} to {capacity} flights.")
        except Exception as e:
            logging.error(f"Error reading/filtering flight list {expected_filename}: {e}")
            continue

        if capacity == 0:
            logging.warning(f"Rank {rank} ({dep}->{arr}) has 0 flights matching the filters. Skipping.")
            continue

        # Quota Strategy
        if strategy == 'all':
            target = capacity
        elif strategy == 'fixed':
            target = min(int(value), capacity)
        elif strategy == 'percent':
            target = min(math.ceil(capacity * (value / 100.0)), capacity)
        else:
            target = capacity

        execution_plan.append({
            'rank': rank,
            'dep': dep,
            'arr': arr,
            'file_path': str(file_path),
            'target': target,
            'capacity': capacity,
            'filename': expected_filename
        })
        
    return execution_plan


def execute_batch_fetch(
    execution_plan: list, 
    out_dir: str, 
    seed: int,
    start_date: str = None,
    end_date: str = None,
    typecode: str = None
):
    """Executes the batch fetching loop sequentially."""
    if not execution_plan:
        logging.error("Execution plan is empty. Aborting batch fetch.")
        return

    print("\n" + "="*70)
    print("BATCH FETCH PLAN")
    print("="*70)
    for i, item in enumerate(execution_plan, 1):
        print(f"{i:02d}.  {item['dep']} -> {item['arr']} | Sample Size: {item['target']} | Source: {item['filename']}")
    print("="*70 + "\n")

    success_count = 0
    total = len(execution_plan)

    for i, item in enumerate(execution_plan, 1):
        logging.info(f"Processing [{i}/{total}] - Rank {item['rank']} | {item['dep']} -> {item['arr']}")
        
        try:
            success = opensky_fetcher.fetch_trajectories(
                input_list_path=item['file_path'],
                out_dir=out_dir,
                sample_size=item['target'],
                seed=seed,
                start_date=start_date,
                end_date=end_date,
                typecode=typecode
            )
            if success:
                success_count += 1
        except Exception as e:
            logging.error(f"CRITICAL ERROR fetching trajectories for {item['dep']}->{item['arr']}: {e}")
            logging.warning("Continuing pipeline run for next corridor...")
            continue
            
    logging.info(f"BATCH FETCHING COMPLETE: {success_count}/{total} routes processed successfully.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
    
    def check_seed_range(value):
        ivalue = int(value)
        if ivalue < 0 or ivalue > 4294967295:
            raise argparse.ArgumentTypeError(f"Seed {value} must be between 0 and 4294967295")
        return ivalue

    parser = argparse.ArgumentParser(description="OpenSky Fetcher Orchestrator - Batch Trajectory Downloader")
    parser.add_argument("--route-summary", default=str(FLIGHT_REGISTRY_DIR / "master_flights_RouteSummary.pkl"), help="Path to RouteSummary pickle file")
    parser.add_argument("--input-dir", default=str(FLIGHT_LISTS_DIR), help="Directory containing sliced flight list parquets")
    parser.add_argument("--format", choices=['oneway', 'roundtrip'], default='oneway', help="Fetch format directionality")
    
    # Entry Point Options (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated ranks list (e.g. '1,5,12')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of corridor ranks")
    
    parser.add_argument("--upper-rank", type=int, help="Upper bound of corridor ranks")
    
    # Quota Strategy
    parser.add_argument("--strategy", choices=['fixed', 'percent', 'all'], default='fixed', help="Sampling strategy")
    parser.add_argument("--value", type=float, default=50, help="Value for fixed/percent strategies")
    parser.add_argument("--seed", type=check_seed_range, default=42, help="Seed value for randomized sampling state")
    
    # Optional filtering parameters
    parser.add_argument("--start-date", default=None, help="Start bounds of flight departure window (ISO format)")
    parser.add_argument("--end-date", default=None, help="End bounds of flight departure window (ISO format)")
    parser.add_argument("--typecode", default=None, help="Aircraft model code (e.g. B738, A320)")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")

    args = parser.parse_args()

    # Validate range
    if args.ranks is not None and args.upper_rank is not None:
        parser.error("--upper-rank cannot be used when --ranks is specified.")

    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")

    specific_ranks_list = None
    if args.ranks:
        try:
            specific_ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")

    # Generate dynamic dataset directory name early and set up file logger
    dataset_name = generate_dataset_name(
        ranks=specific_ranks_list,
        lower_rank=args.lower_rank,
        upper_rank=args.upper_rank,
        strategy=args.strategy,
        value=args.value,
        seed=args.seed,
        fetch_format=args.format,
        start_date=args.start_date,
        end_date=args.end_date,
        typecode=args.typecode,
        min_distance=args.min_distance
    )
    out_dir_path = get_dataset_dir(dataset_name)
    setup_file_logger(out_dir_path)
    logging.info(f"Generated dynamic dataset directory: data/trajectories/{dataset_name}/")

    # 1. Resolve corridors to fetch
    routes = extract_target_routes(
        summary_path=args.route_summary, 
        lower=args.lower_rank, 
        upper=args.upper_rank,
        specific_ranks=specific_ranks_list,
        fetch_format=args.format,
        min_distance=args.min_distance
    )
    
    if not routes.empty:
        # 2. Map routes to files and calculate quotas
        plan = compute_fetch_targets(
            routes_df=routes, 
            input_dir=args.input_dir, 
            strategy=args.strategy, 
            value=args.value,
            start_date=args.start_date,
            end_date=args.end_date,
            typecode=args.typecode
        )
        
        if plan:
            # 4. Run batch downloader
            import time
            start_time = time.time()
            execute_batch_fetch(
                execution_plan=plan, 
                out_dir=str(out_dir_path),
                seed=args.seed,
                start_date=args.start_date,
                end_date=args.end_date,
                typecode=args.typecode
            )
            duration = time.time() - start_time
            logging.info(f"Batch fetch run completed in {duration:.2f} seconds ({duration/60:.2f} minutes).")
        else:
            logging.error("No valid corridors available in the execution plan.")
    else:
        logging.error("No target corridors extracted matching the CLI parameters.")
