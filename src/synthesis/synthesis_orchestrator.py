"""
Orchestrator for Flight Trajectory Synthesis
Iterates through a list or range of route ranks, generates synthesized 
reference flight paths, and manages skipping already generated paths.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
import pandas as pd

# Add project root to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.common.config import BASE_DIR, FLIGHT_REGISTRY_DIR, SYNTHESIZED_FLIGHT_PATHS_DIR
from src.common.utils import load_route_summary, split_route_string
from src.synthesis.path_generator import create_synthesized_trajectory

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [ORCHESTRATOR] - %(message)s')
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Batch Flight Trajectory Synthesis Orchestrator")
    
    # Ranks specifications (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ranks", type=str, help="Comma-separated list of ranks (e.g. '1,76,177')")
    group.add_argument("--lower-rank", type=int, help="Lower bound of route ranks range")
    
    parser.add_argument("--upper-rank", type=int, help="Upper bound of route ranks range")
    parser.add_argument("--out-dir", default=str(SYNTHESIZED_FLIGHT_PATHS_DIR), help="Output directory for synthesized trajectories")
    parser.add_argument("--grid-seconds", type=int, default=60, help="Time grid resolution in seconds (default: 60)")
    parser.add_argument("--overwrite", action="store_true", help="Force regeneration of synthesized paths even if they already exist")
    
    args = parser.parse_args()
    
    # Validate ranks bounds
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")
        
    # Resolve ranks list
    ranks_list = []
    if args.ranks:
        try:
            ranks_list = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
    elif args.lower_rank is not None and args.upper_rank is not None:
        if args.lower_rank > args.upper_rank:
            parser.error("--lower-rank cannot be greater than --upper-rank.")
        ranks_list = list(range(args.lower_rank, args.upper_rank + 1))
        
    logger.info(f"Targeting {len(ranks_list)} rank(s) for synthesized flight path generation.")
    
    # Load RouteSummary once
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing. Aborting.")
        sys.exit(1)
        
    out_dir_path = Path(args.out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    
    success_count = 0
    skipped_count = 0
    failed_count = 0
    
    for rank in ranks_list:
        logger.info(f"\n--- Processing synthesis for Rank {rank} ---")
        
        # 1. Resolve rank to route
        route_row = df_summary[df_summary['rank'] == rank]
        if route_row.empty:
            logger.warning(f"Rank {rank} not found in RouteSummary. Skipping.")
            failed_count += 1
            continue
            
        route_str = route_row['route'].iloc[0]
        dep, arr = split_route_string(route_str)
        if dep == 'UNK' or arr == 'UNK':
            logger.warning(f"Failed to parse airports for route '{route_str}'. Skipping.")
            failed_count += 1
            continue
            
        out_file = out_dir_path / f"{dep}-{arr}_synthesized.parquet"
        
        # 2. Check skip or overwrite conditions
        if out_file.exists():
            if not args.overwrite:
                logger.info(f"Synthesized trajectory for rank {rank} ({dep}-{arr}) already exists at {out_file.name}. Skipping.")
                skipped_count += 1
                continue
            else:
                logger.info(f"Overwrite flag is active. Deleting existing file: {out_file.name}")
                try:
                    out_file.unlink(missing_ok=True)
                except Exception as del_err:
                    logger.error(f"Failed to delete existing file {out_file.name}: {del_err}. Attempting generation anyway.")
                    
        # 3. Call synthesis engine
        try:
            res = create_synthesized_trajectory(
                rank=rank,
                output_parquet=str(out_file),
                time_grid_seconds=args.grid_seconds
            )
            if res:
                success_count += 1
            else:
                failed_count += 1
        except Exception as e:
            logger.error(f"Error generating synthesized trajectory for Rank {rank} ({dep}-{arr}): {e}", exc_info=True)
            failed_count += 1
            
    logger.info(f"\n=== Synthesis Orchestration Complete ===")
    logger.info(f"Total processed: {len(ranks_list)}")
    logger.info(f"Success: {success_count}")
    logger.info(f"Skipped: {skipped_count}")
    logger.info(f"Failed: {failed_count}")

if __name__ == "__main__":
    main()
