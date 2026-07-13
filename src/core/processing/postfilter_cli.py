from __future__ import annotations
import argparse
import logging
from pathlib import Path

import pandas as pd

from src.common.config import (
    GLOBAL_CLEAN_REGISTRY,
    POSTFILTER_BATCH_SIZE_DEFAULT,
    PROCESSING_DEFAULT_MAX_WORKERS,
    DEFAULT_PREFILTER_THRESHOLDS,
    DEFAULT_POSTFILTER_THRESHOLDS,
    BASE_DIR,
)
from src.common.utils import setup_file_logger
from .postfilter_orchestrator import run_postfilters

logger = logging.getLogger(__name__)

def main() -> None:
    """CLI entrypoint for the post-filtering pipeline."""
    # Setup logger first (idempotent, standard log file name is processing.log)
    setup_file_logger(log_filename="processing.log")
    
    parser = argparse.ArgumentParser(description="Clean trajectory post-filtering pipeline.")
    parser.add_argument("--ranks", type=int, nargs="+", help="Route volume ranks to process.")
    parser.add_argument("--rank-range", type=int, nargs=2, help="Inclusive rank range (e.g. 1 10).")
    parser.add_argument("--routes", type=str, nargs="+", help="Corridor strings (e.g. EDDF-LIRF).")
    parser.add_argument("--source-dir", type=str, help="Directory path to filter clean parquets by location.")
    parser.add_argument("--overwrite", action="store_true", help="Re-process and overwrite existing filter outcomes.")
    parser.add_argument("--workers", "--num-workers", "--max-workers", dest="max_workers", type=int, default=None, help="Maximum worker processes.")
    parser.add_argument("--batch-size", type=int, default=POSTFILTER_BATCH_SIZE_DEFAULT, help=f"Batch size (default: {POSTFILTER_BATCH_SIZE_DEFAULT}).")
    parser.add_argument(
        "--filters",
        type=str,
        nargs="+",
        choices=["velocity", "coordinate_velocity", "acceleration", "distance"],
        default=["velocity", "coordinate_velocity", "acceleration", "distance"],
        help="Sub-filters to run (default: all four)."
    )
    args = parser.parse_args()
    
    # 1. Route, rank, and directory scope resolution
    target_flight_ids: set[str] | None = None
    
    if args.source_dir or args.routes or args.ranks or args.rank_range:
        target_flight_ids = set()
        
        # Filter flights by source-dir location if provided
        if args.source_dir:
            s_dir = Path(args.source_dir).resolve()
            if GLOBAL_CLEAN_REGISTRY.exists():
                df_reg = pd.read_parquet(GLOBAL_CLEAN_REGISTRY)
                for _, row in df_reg.iterrows():
                    fpath = Path(row["file_path"])
                    if not fpath.is_absolute():
                        fpath = BASE_DIR / fpath
                    try:
                        fpath = fpath.resolve()
                        if s_dir in fpath.parents or fpath.parent == s_dir:
                            target_flight_ids.add(row["flight_id"])
                    except Exception:
                        pass
                        
        # Resolve target corridors from ranks or explicit routes
        target_corridors = []
        if args.routes:
            for r in args.routes:
                if "-" in r:
                    dep, arr = r.split("-", 1)
                    target_corridors.append((dep.strip().upper(), arr.strip().upper()))
                else:
                    logger.warning(f"Skipping malformed route format: {r} (expected DEP-ARR)")
                    
        if args.ranks or args.rank_range:
            from src.common.utils import extract_target_routes
            routes_df = extract_target_routes(
                specific_ranks=args.ranks,
                lower=args.rank_range[0] if args.rank_range else None,
                upper=args.rank_range[1] if args.rank_range else None,
            )
            for _, row in routes_df.iterrows():
                target_corridors.append((row["dep"], row["arr"]))
                
        if target_corridors:
            from src.common.registry_utils import get_flights_for_route
            matching_dfs = []
            for dep, arr in target_corridors:
                matching_dfs.append(get_flights_for_route(dep, arr))
                
            if matching_dfs:
                df_target = pd.concat(matching_dfs).drop_duplicates(subset=["flight_id"])
                if not df_target.empty:
                    target_flight_ids.update(df_target["flight_id"].tolist())
                    
    # 2. Invoke orchestrator
    _DISTANCE_KEYS = ("max_dep_horiz_dist", "max_dep_vert_dist", "max_arr_horiz_dist", "max_arr_vert_dist")
    thresholds = DEFAULT_POSTFILTER_THRESHOLDS.copy()
    for k in _DISTANCE_KEYS:
        if k in DEFAULT_PREFILTER_THRESHOLDS:
            thresholds[k] = DEFAULT_PREFILTER_THRESHOLDS[k]

    run_postfilters(
        registry_path=GLOBAL_CLEAN_REGISTRY,
        filters_to_run=args.filters,
        thresholds=thresholds,
        batch_size=args.batch_size,
        overwrite=args.overwrite,
        max_workers=args.max_workers,
        target_flight_ids=target_flight_ids,
    )

if __name__ == "__main__":
    from src.common.config import init_runtime
    init_runtime()
    main()
