"""
Phase Quality Campaign CLI Orchestrator (Script 2 Part C)

Evaluates the candidate flight pool against configurable metadata pre-filters,
saves the evaluation summary table (filter_evaluation.csv), and compiles multi-page
visual audit PDF reports across European target routes.

Usage Examples:
    # Run baseline (no filters, 100% pass-through) on all 6 routes in PNG format:
    python -m src.analysis.campaigns.phase_quality.run_phase_quality_campaign --all --workers 4 --format PNG

    # Run with departure distance <= 5000m and min duration >= -30% of median:
    python -m src.analysis.campaigns.phase_quality.run_phase_quality_campaign --all --workers 4 --format PNG --max-dep-horiz-dist 5000 --min-duration-pct-below-median 30
"""

import argparse
import logging
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd

from src.common.config import (
    BASE_DIR,
    PHASE_QUALITY_DIR,
    PHASE_QUALITY_RUNS_DIR,
    AUDIT_CANDIDATE_POOL_REGISTRY,
    AUDIT_COHORT_MAP_REGISTRY,
    DEFAULT_PREFILTER_THRESHOLDS,
)
from src.common.utils import setup_file_logger, load_route_summary
from src.analysis.campaigns.phase_quality.phase_quality_filters import apply_metadata_prefilters
from src.analysis.campaigns.phase_quality.phase_quality_plots import compile_route_audit_pdf

logger = logging.getLogger(__name__)


def _worker_run_campaign_route(
    route_id: str,
    df_pool: pd.DataFrame,
    df_map: pd.DataFrame,
    df_eval: pd.DataFrame,
    out_dir: Path,
    plot_format: str = "SVG",
    show_rejected: bool = False,
) -> str:
    """Worker target to load trajectories and compile PDF report for a single route."""
    setup_file_logger("calibration.log")
    try:
        df_route = df_pool[df_pool["route_id"] == route_id].copy()
        trajectories = {}
        
        logger.info(f"[{route_id}] Loading {len(df_route)} raw trajectory files from disk...")
        for idx, row in df_route.iterrows():
            fid = row["flight_id"]
            rel_path = row["file_path"]
            abs_path = BASE_DIR / rel_path
            
            if not abs_path.exists():
                continue
                
            try:
                df_traj = pd.read_parquet(abs_path)
                trajectories[fid] = df_traj
            except Exception as e:
                logger.error(f"[{route_id}] Failed to read parquet for {fid} ({abs_path}): {e}")
                
        if not trajectories:
            return f"[{route_id}] FAILED: No trajectories loaded."
            
        out_pdf = out_dir / f"{route_id}_audit_report.pdf"
        compile_route_audit_pdf(
            route_id=route_id,
            cohort_map_df=df_map,
            trajectories=trajectories,
            out_pdf_path=out_pdf,
            eval_df=df_eval,
            show_rejected=show_rejected,
            plot_format=plot_format,
        )
        return f"[{route_id}] Successfully generated {out_pdf.name}"
    except Exception as e:
        logger.error(f"[{route_id}] Error in worker: {e}", exc_info=True)
        return f"[{route_id}] ERROR: {e}"


def parse_args():
    parser = argparse.ArgumentParser(description="Run Phase Quality Filtering Campaign (Script 2 Part C)")
    parser.add_argument("--route", type=str, default="EDDF-LIRF", help="Specific route to evaluate (default: EDDF-LIRF)")
    parser.add_argument("--all", action="store_true", help="Evaluate all 6 target European routes")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel worker processes for PDF compilation")
    parser.add_argument("--format", type=str, choices=["SVG", "PNG"], default="SVG", help="Plot rendering format (default: SVG)")
    parser.add_argument("--out-dir", type=str, default=None, help="Custom output directory for evaluation results and PDFs")
    parser.add_argument("--show-rejected", action="store_true", help="Plot rejected trajectories on audit pages (default: hidden)")
    
    # 8 Metadata Pre-filter flags (all default to None = ignored unless set)
    parser.add_argument("--max-dep-horiz-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["max_dep_horiz_dist"], help="Max departure horizontal distance in meters")
    parser.add_argument("--max-dep-vert-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["max_dep_vert_dist"], help="Max departure vertical distance in meters")
    parser.add_argument("--max-arr-horiz-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["max_arr_horiz_dist"], help="Max arrival horizontal distance in meters")
    parser.add_argument("--max-arr-vert-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["max_arr_vert_dist"], help="Max arrival vertical distance in meters")
    parser.add_argument("--max-dep-candidates", type=int, default=DEFAULT_PREFILTER_THRESHOLDS["max_dep_candidates"], help="Max departure airport candidate count")
    parser.add_argument("--max-arr-candidates", type=int, default=DEFAULT_PREFILTER_THRESHOLDS["max_arr_candidates"], help="Max arrival airport candidate count")
    parser.add_argument("--max-duration-pct-above-median", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["max_duration_pct_above_median"], help="Max duration %% above route median")
    parser.add_argument("--min-duration-pct-below-median", type=float, default=DEFAULT_PREFILTER_THRESHOLDS["min_duration_pct_below_median"], help="Min duration %% below route median (Option A)")
    
    return parser.parse_args()


def main():
    setup_file_logger("calibration.log")
    args = parse_args()
    
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        parts = []
        if args.max_dep_horiz_dist is not None: parts.append(f"dephoriz{int(args.max_dep_horiz_dist)}")
        if args.max_dep_vert_dist is not None: parts.append(f"depvert{int(args.max_dep_vert_dist)}")
        if args.max_arr_horiz_dist is not None: parts.append(f"arrhoriz{int(args.max_arr_horiz_dist)}")
        if args.max_arr_vert_dist is not None: parts.append(f"arrvert{int(args.max_arr_vert_dist)}")
        if args.max_dep_candidates is not None: parts.append(f"depcand{args.max_dep_candidates}")
        if args.max_arr_candidates is not None: parts.append(f"arrcand{args.max_arr_candidates}")
        if args.max_duration_pct_above_median is not None: parts.append(f"durabove{int(args.max_duration_pct_above_median)}")
        if args.min_duration_pct_below_median is not None: parts.append(f"durbelow{int(args.min_duration_pct_below_median)}")
        
        folder_name = "run_" + "_".join(parts) + f"_{args.format.lower()}" if parts else f"baseline_no_filter_{args.format.lower()}"
        out_dir = PHASE_QUALITY_RUNS_DIR / folder_name
        
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"=== Starting Phase Quality Campaign -> Output dir: {out_dir} ===")
    
    pool_file = AUDIT_CANDIDATE_POOL_REGISTRY
    map_file = AUDIT_COHORT_MAP_REGISTRY
    
    if not pool_file.exists() or not map_file.exists():
        logger.critical("Candidate pool or cohort map missing! Please run build_audit_candidate_pool.py first.")
        return
        
    df_pool = pd.read_parquet(pool_file)
    df_map = pd.read_parquet(map_file)
    df_route_summary = load_route_summary()
    
    thresholds = {
        "max_dep_horiz_dist": args.max_dep_horiz_dist,
        "max_dep_vert_dist": args.max_dep_vert_dist,
        "max_arr_horiz_dist": args.max_arr_horiz_dist,
        "max_arr_vert_dist": args.max_arr_vert_dist,
        "max_dep_candidates": args.max_dep_candidates,
        "max_arr_candidates": args.max_arr_candidates,
        "max_duration_pct_above_median": args.max_duration_pct_above_median,
        "min_duration_pct_below_median": args.min_duration_pct_below_median,
    }
    
    df_eval = apply_metadata_prefilters(df_pool, df_route_summary, thresholds)
    
    eval_csv_path = out_dir / "filter_evaluation.csv"
    df_eval.to_csv(eval_csv_path, index=False)
    logger.info(f"Saved evaluation summary table ({len(df_eval)} flights) to {eval_csv_path}")
    
    target_routes = [
        "EDDF-LIRF",
        "EGLL-BIKF",
        "ESSA-EHAM",
        "ESSA-LEMD",
        "LFRS-LFMN",
        "LGSA-LGAV",
    ] if args.all else [args.route]
    
    logger.info(f"Compiling PDF reports for {len(target_routes)} routes using {args.workers} workers...")
    
    if args.workers > 1 and len(target_routes) > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    _worker_run_campaign_route,
                    route,
                    df_pool,
                    df_map,
                    df_eval,
                    out_dir,
                    args.format,
                    args.show_rejected
                ): route
                for route in target_routes
            }
            for future in as_completed(futures):
                res = future.result()
                logger.info(res)
    else:
        for route in target_routes:
            res = _worker_run_campaign_route(
                route,
                df_pool,
                df_map,
                df_eval,
                out_dir,
                args.format,
                args.show_rejected
            )
            logger.info(res)
            
    logger.info("=== Phase Quality Campaign Completed Successfully ===")


if __name__ == "__main__":
    main()
