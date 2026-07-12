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
import multiprocessing as mp
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
    GLOBAL_CLEAN_REGISTRY,
    GLOBAL_TRAJECTORY_REGISTRY,
)
from src.common.utils import setup_file_logger, load_route_summary
from src.common.registry_utils import load_trajectory_registry
from src.analysis.campaigns.phase_quality.phase_quality_filters import apply_metadata_prefilters
from src.analysis.campaigns.phase_quality.phase_quality_plots import compile_route_audit_pdf

logger = logging.getLogger(__name__)


def _worker_init() -> None:
    """Initializes logging handlers and numeric thread limits inside spawned child workers."""
    setup_file_logger(log_filename="calibration.log")
    from src.common.concurrency import limit_numeric_threads
    limit_numeric_threads(1)


def _worker_run_campaign_route(
    route_id: str,
    df_pool: pd.DataFrame,
    df_map: pd.DataFrame,
    df_eval: pd.DataFrame,
    out_dir: Path,
    plot_format: str = "SVG",
    show_rejected: bool = False,
    use_clean: bool = False,
) -> tuple[str, dict]:
    """Worker target to load trajectories, run post-filters, and compile PDF report for a route."""
    setup_file_logger("calibration.log")
    try:
        df_route = df_pool[df_pool["route_id"] == route_id].copy()
        trajectories = {}
        
        load_clean = use_clean
        trajectories_clean = {} if load_clean else None
        
        # Pre-load raw registry index
        raw_reg_map = {}
        if GLOBAL_TRAJECTORY_REGISTRY.exists():
            try:
                df_raw_reg = load_trajectory_registry(GLOBAL_TRAJECTORY_REGISTRY)
                if not df_raw_reg.empty and "flight_id" in df_raw_reg.columns and "file_path" in df_raw_reg.columns:
                    raw_reg_map = dict(zip(df_raw_reg["flight_id"], df_raw_reg["file_path"]))
                    logger.info(f"[{route_id}] Loaded raw registry index with {len(raw_reg_map):,} entries.")
            except Exception as e:
                logger.warning(f"[{route_id}] Could not load raw registry index: {e}")

        # Pre-load clean registry index if loading clean trajectories
        clean_reg_map = {}
        if load_clean and GLOBAL_CLEAN_REGISTRY.exists():
            try:
                df_clean_reg = load_trajectory_registry(GLOBAL_CLEAN_REGISTRY)
                if not df_clean_reg.empty and "flight_id" in df_clean_reg.columns and "file_path" in df_clean_reg.columns:
                    clean_reg_map = dict(zip(df_clean_reg["flight_id"], df_clean_reg["file_path"]))
                    logger.info(f"[{route_id}] Loaded clean registry index with {len(clean_reg_map):,} entries.")
            except Exception as e:
                logger.warning(f"[{route_id}] Could not load clean registry index: {e}")
        
        logger.info(f"[{route_id}] Loading {len(df_route)} trajectories from disk...")
        for idx, row in df_route.iterrows():
            fid = row["flight_id"]
            rel_path = row.get("file_path")
            
            # Resolve raw trajectory
            raw_path = None
            if fid in raw_reg_map:
                raw_path = BASE_DIR / raw_reg_map[fid]
            elif rel_path:
                raw_path = BASE_DIR / rel_path

            if raw_path and raw_path.exists():
                try:
                    df_traj = pd.read_parquet(raw_path)
                    trajectories[fid] = df_traj
                except Exception as e:
                    logger.error(f"[{route_id}] Failed to read raw parquet for {fid} ({raw_path}): {e}")
            else:
                logger.warning(f"[{route_id}] Raw file missing on disk for {fid}: {raw_path}")
                
            # Resolve clean trajectory
            if load_clean:
                clean_path = None
                if fid in clean_reg_map:
                    clean_path = BASE_DIR / clean_reg_map[fid]
                elif rel_path:
                    # Sibling path fallback just in case
                    clean_path_cand = BASE_DIR / rel_path.replace("_raw.parquet", "_clean_si.parquet")
                    if clean_path_cand.exists():
                        clean_path = clean_path_cand
                
                if clean_path and clean_path.exists():
                    try:
                        trajectories_clean[fid] = pd.read_parquet(clean_path)
                    except Exception as e:
                        logger.error(f"[{route_id}] Failed to read clean parquet for {fid} ({clean_path}): {e}")
                else:
                    logger.debug(f"[{route_id}] Clean trajectory not found for {fid} (checked registry)")

        n_raw_loaded = len(trajectories)
        pct_raw = (n_raw_loaded / max(len(df_route), 1)) * 100
        logger.info(f"[{route_id}] Raw trajectories loaded: {n_raw_loaded}/{len(df_route)} ({pct_raw:.1f}%)")
        if pct_raw < 90.0:
            logger.warning(f"[{route_id}] Low raw coverage ({pct_raw:.1f}%) — check GLOBAL_TRAJECTORY_REGISTRY for missing entries.")

        if load_clean:
            n_loaded = len(trajectories_clean)
            pct = (n_loaded / max(len(df_route), 1)) * 100
            logger.info(f"[{route_id}] Clean trajectories loaded: {n_loaded}/{len(df_route)} ({pct:.1f}%)")
            if pct < 90.0:
                logger.warning(f"[{route_id}] Low clean coverage ({pct:.1f}%) — check GLOBAL_CLEAN_REGISTRY for missing entries.")

        if not trajectories:
            return f"[{route_id}] FAILED: No trajectories loaded.", {}
            
        # Run post-filters for flights that passed pre-filtering
        df_eval_updated = df_eval.copy()
        flight_updates = {}
        df_route_eval = df_eval_updated[df_eval_updated["route_id"] == route_id]
        
        from src.analysis.campaigns.phase_quality.phase_quality_filters import (
            apply_trajectory_postfilters,
            get_airport_coords,
            recompute_airport_distances,
        )
        from src.common.config import DEFAULT_POSTFILTER_THRESHOLDS, RECOMPUTE_AIRPORT_DISTANCES
        
        for idx, row in df_route_eval.iterrows():
            fid = row["flight_id"]
            status = row["status"]
            
            if status != "PASSED":
                continue
                
            df_raw = trajectories.get(fid)
            df_clean = trajectories_clean.get(fid) if trajectories_clean else None
            
            # If load_clean is True but clean trajectory is missing, reject it
            if load_clean and df_clean is None:
                df_eval_updated.loc[df_eval_updated["flight_id"] == fid, ["status", "fail_stage", "reject_reason"]] = [
                    "REJECTED", "POSTFILTER", "MISSING_CLEAN_TRAJECTORY"
                ]
                flight_updates[fid] = {
                    "status": "REJECTED",
                    "fail_stage": "POSTFILTER",
                    "reject_reason": "MISSING_CLEAN_TRAJECTORY"
                }
                continue
                
            if df_clean is None or df_raw is None or df_clean.empty or df_raw.empty:
                continue
                
            try:
                # Optionally recompute airport distances
                if RECOMPUTE_AIRPORT_DISTANCES:
                    dep_col = "estdepartureairport"
                    arr_col = "estarrivalairport"
                    if dep_col in df_clean.columns and arr_col in df_clean.columns:
                        dep_icao = df_clean[dep_col].iloc[0]
                        arr_icao = df_clean[arr_col].iloc[0]
                        if not pd.isna(dep_icao) and not pd.isna(arr_icao):
                            coords = get_airport_coords(dep_icao, arr_icao)
                            df_clean = recompute_airport_distances(df_clean, coords)
                            trajectories_clean[fid] = df_clean
                            
                # Run the post-filters
                rejected, reason, metrics = apply_trajectory_postfilters(
                    df_clean, df_raw, DEFAULT_POSTFILTER_THRESHOLDS
                )
                
                if rejected:
                    df_eval_updated.loc[df_eval_updated["flight_id"] == fid, ["status", "fail_stage", "reject_reason"]] = [
                        "REJECTED", "POSTFILTER", reason
                    ]
                    flight_updates[fid] = {
                        "status": "REJECTED",
                        "fail_stage": "POSTFILTER",
                        "reject_reason": reason
                    }
                    
            except Exception as e:
                logger.error(f"[{route_id}] Exception running post-filter for flight {fid}: {e}", exc_info=True)
                
        n_postfilter_rejected = len(flight_updates)
        n_postfilter_passed = sum(1 for _, row in df_route_eval.iterrows() if row["status"] == "PASSED") - n_postfilter_rejected
        logger.info(
            f"[{route_id}] Post-filter results: {n_postfilter_passed} PASSED, {n_postfilter_rejected} REJECTED "
            f"(reasons: {', '.join(set(v['reject_reason'] for v in flight_updates.values())) or 'none'})"
        )
            
        out_pdf = out_dir / f"{route_id}_audit_report.pdf"
        compile_route_audit_pdf(
            route_id=route_id,
            cohort_map_df=df_map,
            trajectories=trajectories,
            out_pdf_path=out_pdf,
            eval_df=df_eval_updated,
            show_rejected=show_rejected,
            plot_format=plot_format,
            trajectories_clean=trajectories_clean if (trajectories_clean and len(trajectories_clean) > 0) else None,
        )
        return f"[{route_id}] Successfully generated {out_pdf.name}", flight_updates
    except Exception as e:
        logger.error(f"[{route_id}] Error in worker: {e}", exc_info=True)
        return f"[{route_id}] ERROR: {e}", {}
def parse_args():
    def str2bool(v):
        if isinstance(v, bool):
            return v
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Boolean value expected.')

    parser = argparse.ArgumentParser(description="Run Phase Quality Filtering Campaign (Script 2 Part C)")
    parser.add_argument("--route", type=str, default="EDDF-LIRF", help="Specific route to evaluate (default: EDDF-LIRF)")
    parser.add_argument("--all", action="store_true", help="Evaluate all 6 target European routes")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel worker processes for PDF compilation")
    parser.add_argument("--format", type=str, choices=["SVG", "PNG"], default="SVG", help="Plot rendering format (default: SVG)")
    parser.add_argument("--out-dir", type=str, default=None, help="Custom output directory for evaluation results and PDFs")
    parser.add_argument("--show-rejected", type=str2bool, nargs='?', const=True, default=True, help="Plot rejected trajectories on audit pages (default: hidden)")
    parser.add_argument("--use-clean", type=str2bool, nargs='?', const=True, default=True, help="Automatically resolve and load clean trajectories from GLOBAL_CLEAN_REGISTRY for 4-plot comparison")
    
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
    
    logger.info(f"Compiling PDF reports for {len(target_routes)} routes using {args.workers} workers (use_clean={args.use_clean})...")
    
    all_flight_updates = {}
    
    if args.workers > 1 and len(target_routes) > 1:
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx, initializer=_worker_init) as executor:
            futures = {
                executor.submit(
                    _worker_run_campaign_route,
                    route,
                    df_pool,
                    df_map,
                    df_eval,
                    out_dir,
                    args.format,
                    args.show_rejected,
                    args.use_clean,
                ): route
                for route in target_routes
            }
            for future in as_completed(futures):
                msg, flight_updates = future.result()
                logger.info(msg)
                all_flight_updates.update(flight_updates)
    else:
        for route in target_routes:
            msg, flight_updates = _worker_run_campaign_route(
                route,
                df_pool,
                df_map,
                df_eval,
                out_dir,
                args.format,
                args.show_rejected,
                args.use_clean,
            )
            logger.info(msg)
            all_flight_updates.update(flight_updates)
            
    # Merge updates and rewrite final filter evaluation
    if all_flight_updates:
        logger.info(f"Applying post-filtering rejections to {len(all_flight_updates)} flights...")
        for fid, updates in all_flight_updates.items():
            df_eval.loc[df_eval["flight_id"] == fid, ["status", "fail_stage", "reject_reason"]] = [
                updates["status"], updates["fail_stage"], updates["reject_reason"]
            ]
        df_eval.to_csv(eval_csv_path, index=False)
        logger.info(f"Updated final evaluation table ({len(df_eval)} flights) saved to {eval_csv_path}")
            
    logger.info("=== Phase Quality Campaign Completed Successfully ===")


if __name__ == "__main__":
    main()
