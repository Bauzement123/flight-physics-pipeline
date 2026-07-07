"""
test_phase_quality_plots.py — Test runner for Script 2 (Part A) plotting & PDF engine.

Loads candidate flights from AUDIT_CANDIDATE_POOL_REGISTRY and AUDIT_COHORT_MAP_REGISTRY
for target routes (or all 6 routes in parallel), reads their raw trajectory parquet files,
and compiles 10-page baseline visual audit PDF reports.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import logging
import multiprocessing
import sys
from pathlib import Path
import pandas as pd

from src.common.config import (
    AUDIT_CANDIDATE_POOL_REGISTRY,
    AUDIT_COHORT_MAP_REGISTRY,
    PHASE_QUALITY_RUNS_DIR,
    BASE_DIR,
)
from src.common.utils import setup_file_logger
from src.analysis.campaigns.phase_quality_plots import compile_route_audit_pdf

logger = logging.getLogger(__name__)

ALL_ROUTES = [
    "EDDF-LIRF",
    "EGLL-BIKF",
    "ESSA-EHAM",
    "ESSA-LEMD",
    "LFRS-LFMN",
    "LGSA-LGAV",
]


def load_route_trajectories(df_pool: pd.DataFrame, route_id: str) -> dict[str, pd.DataFrame]:
    """Loads all raw trajectory DataFrames from disk for a specific route."""
    df_route = df_pool[df_pool["route_id"] == route_id].copy()
    trajectories = {}
    
    logger.info(f"[{route_id}] Loading {len(df_route)} raw trajectory files from disk...")
    for idx, row in df_route.iterrows():
        fid = row["flight_id"]
        rel_path = row["file_path"]
        abs_path = BASE_DIR / rel_path
        
        if not abs_path.exists():
            logger.warning(f"[{route_id}] Trajectory file missing on disk: {abs_path}")
            continue
            
        try:
            df_traj = pd.read_parquet(abs_path)
            trajectories[fid] = df_traj
        except Exception as e:
            logger.error(f"[{route_id}] Failed to read parquet for {fid} ({abs_path}): {e}")
            
    logger.info(f"[{route_id}] Successfully loaded {len(trajectories)}/{len(df_route)} trajectories.")
    return trajectories


def _worker_process_route(route_id: str, df_pool: pd.DataFrame, df_map: pd.DataFrame, out_dir: Path, plot_format: str = "SVG") -> str:
    """Worker target to load trajectories and compile PDF report for a single route."""
    setup_file_logger("calibration.log")
    try:
        trajectories = load_route_trajectories(df_pool, route_id)
        if not trajectories:
            return f"[{route_id}] FAILED: No trajectories loaded."
        
        out_pdf = out_dir / f"{route_id}_baseline_audit.pdf"
        compile_route_audit_pdf(
            route_id=route_id,
            cohort_map_df=df_map,
            trajectories=trajectories,
            out_pdf_path=out_pdf,
            eval_df=None,       # Unfiltered baseline test
            show_rejected=False,
            plot_format=plot_format,
        )
        return f"[{route_id}] SUCCESS -> {out_pdf.name}"
    except Exception as e:
        logger.error(f"[{route_id}] Worker exception: {e}", exc_info=True)
        return f"[{route_id}] ERROR: {e}"


def main() -> None:
    setup_file_logger("calibration.log")
    parser = argparse.ArgumentParser(description="Test runner for phase quality audit plotting engine.")
    parser.add_argument("--route", default="EDDF-LIRF", help="Single route ID to test plot (default: EDDF-LIRF)")
    parser.add_argument("--all", action="store_true", help="Run across all 6 target routes")
    parser.add_argument("--workers", type=int, default=min(4, multiprocessing.cpu_count()), help="Number of parallel worker processes")
    parser.add_argument("--format", choices=["PNG", "SVG", "png", "svg"], default="SVG", help="Plot rendering format inside PDF: PNG (rasterized plots, fast loading) or SVG (pure vector, current behavior)")
    parser.add_argument("--out-dir", type=str, default=None, help="Custom output directory for PDF reports (default: baseline_no_filter_<format>)")
    args = parser.parse_args()

    plot_fmt = args.format.upper()
    logger.info(f"Starting test runner for Phase Quality Plotting Engine (Format: {plot_fmt})...")

    if not AUDIT_CANDIDATE_POOL_REGISTRY.exists() or not AUDIT_COHORT_MAP_REGISTRY.exists():
        logger.critical("Candidate pool or cohort map registries not found! Run Script 1 first.")
        sys.exit(1)

    df_pool = pd.read_parquet(AUDIT_CANDIDATE_POOL_REGISTRY)
    df_map = pd.read_parquet(AUDIT_COHORT_MAP_REGISTRY)

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = PHASE_QUALITY_RUNS_DIR / f"baseline_no_filter_{plot_fmt.lower()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    routes_to_run = ALL_ROUTES if args.all else [args.route]
    logger.info(f"Target routes: {routes_to_run} | Parallel workers: {args.workers} | Output dir: {out_dir}")

    if len(routes_to_run) == 1 or args.workers <= 1:
        for r in routes_to_run:
            res = _worker_process_route(r, df_pool, df_map, out_dir, plot_fmt)
            logger.info(res)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_to_route = {
                executor.submit(_worker_process_route, r, df_pool, df_map, out_dir, plot_fmt): r
                for r in routes_to_run
            }
            for future in as_completed(future_to_route):
                r = future_to_route[future]
                try:
                    res = future.result()
                    logger.info(res)
                except Exception as exc:
                    logger.error(f"Route {r} generated an exception: {exc}")

    logger.info("Test runner execution complete!")


if __name__ == "__main__":
    main()
