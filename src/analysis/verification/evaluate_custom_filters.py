"""
evaluate_custom_filters.py — Custom Parameter & Duration Filter Evaluation Engine.

Evaluates user-specified spatial, candidate, and duration filters against the Top K
routes in master_flights.parquet and computes cumulative route and flight retention.

Adheres strictly to Code Review & Quality Audit Standards (Rule 7):
- Functions <= 50 lines, main() <= 80 lines.
- Centralized logging via setup_file_logger("calibration.log").
- Pure vectorized pandas/numpy operations.
"""

import argparse
import logging
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.common.config import MASTER_FLIGHTS_FILE
from src.common.utils import setup_file_logger
from src.common.config import DEFAULT_PREFILTER_THRESHOLDS

logger = logging.getLogger(__name__)


def load_and_filter_top_k(top_k: int) -> tuple[pd.DataFrame, int]:
    """Loads required columns from master flights and filters to Top K routes."""
    cols = [
        "estdepartureairport", "estarrivalairport",
        "firstseen", "lastseen",
        "estdepartureairporthorizdistance", "estdepartureairportvertdistance",
        "estarrivalairporthorizdistance", "estarrivalairportvertdistance",
        "departureairportcandidatescount", "arrivalairportcandidatescount"
    ]
    logger.info(f"Loading master flights from {MASTER_FLIGHTS_FILE}...")
    df = pd.read_parquet(MASTER_FLIGHTS_FILE, columns=cols)
    
    df["route_str"] = df["estdepartureairport"].astype(str) + "-" + df["estarrivalairport"].astype(str)
    route_counts = df["route_str"].value_counts()
    actual_k = min(top_k, len(route_counts))
    top_routes = route_counts.head(actual_k).index
    
    df_top = df[df["route_str"].isin(top_routes)].copy()
    
    # Compute duration and route median duration in seconds
    first_seen = pd.to_datetime(df_top["firstseen"])
    last_seen = pd.to_datetime(df_top["lastseen"])
    df_top["duration_s"] = (last_seen - first_seen).dt.total_seconds()
    
    route_medians = df_top.groupby("route_str")["duration_s"].median()
    df_top["route_median_s"] = df_top["route_str"].map(route_medians)
    
    logger.info(f"Filtered to top {actual_k:,} routes ({len(df_top):,} total flights).")
    return df_top, actual_k


def build_filter_mask(df: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.Series, dict]:
    """Evaluates active filter conditions vectorially and returns mask and pass stats."""
    mask = pd.Series(True, index=df.index)
    stats = {}
    total = len(df) if len(df) > 0 else 1
    
    def apply_cond(name: str, cond: pd.Series):
        nonlocal mask
        passed = cond.sum()
        stats[name] = (passed, (passed / total) * 100.0)
        mask = mask & cond

    if args.max_dep_horiz_dist is not None:
        apply_cond("Dep Horiz Distance <= " + f"{args.max_dep_horiz_dist:,}m",
                   df["estdepartureairporthorizdistance"] <= args.max_dep_horiz_dist)
        
    if args.max_dep_vert_dist is not None:
        apply_cond("Dep Vert Distance <= " + f"{args.max_dep_vert_dist:,}m",
                   df["estdepartureairportvertdistance"].abs() <= args.max_dep_vert_dist)
        
    if args.max_arr_horiz_dist is not None:
        apply_cond("Arr Horiz Distance <= " + f"{args.max_arr_horiz_dist:,}m",
                   df["estarrivalairporthorizdistance"] <= args.max_arr_horiz_dist)
        
    if args.max_arr_vert_dist is not None:
        apply_cond("Arr Vert Distance <= " + f"{args.max_arr_vert_dist:,}m",
                   df["estarrivalairportvertdistance"].abs() <= args.max_arr_vert_dist)
        
    if args.max_dep_candidates is not None:
        apply_cond("Dep Candidate Count <= " + f"{args.max_dep_candidates}",
                   df["departureairportcandidatescount"] <= args.max_dep_candidates)
        
    if args.max_arr_candidates is not None:
        apply_cond("Arr Candidate Count <= " + f"{args.max_arr_candidates}",
                   df["arrivalairportcandidatescount"] <= args.max_arr_candidates)
        
    if args.max_duration_pct_above_median is not None:
        max_s = df["route_median_s"] * (1.0 + args.max_duration_pct_above_median / 100.0)
        apply_cond("Duration <= +" + f"{args.max_duration_pct_above_median}% of Median",
                   df["duration_s"] <= max_s)
        
    if args.min_duration_pct_below_median is not None:
        min_s = df["route_median_s"] * (1.0 - args.min_duration_pct_below_median / 100.0)
        apply_cond("Duration >= -" + f"{args.min_duration_pct_below_median}% of Median",
                   df["duration_s"] >= min_s)
        
    return mask, stats


def compute_retention_metrics(df: pd.DataFrame, mask: pd.Series, total_routes: int, min_route_flights: int) -> dict:
    """Computes cumulative flight, route, and usable flight retention metrics."""
    total_flights = len(df) if len(df) > 0 else 1
    df_surv = df[mask]
    surv_flights = len(df_surv)
    
    route_counts = df_surv["route_str"].value_counts()
    routes_any = (route_counts >= 1).sum()
    valid_corridors = route_counts[route_counts >= min_route_flights].index
    routes_corridor = len(valid_corridors)
    
    df_usable = df_surv[df_surv["route_str"].isin(valid_corridors)]
    usable_flights = len(df_usable)
    
    return {
        "surv_flights": surv_flights,
        "surv_flt_pct": (surv_flights / total_flights) * 100.0,
        "routes_any": routes_any,
        "routes_any_pct": (routes_any / (total_routes if total_routes > 0 else 1)) * 100.0,
        "routes_corridor": routes_corridor,
        "routes_corridor_pct": (routes_corridor / (total_routes if total_routes > 0 else 1)) * 100.0,
        "usable_flights": usable_flights,
        "usable_flt_pct": (usable_flights / total_flights) * 100.0,
    }


def print_evaluation_report(total_flts: int, total_rtes: int, min_flts: int, stats: dict, metrics: dict) -> None:
    """Prints a formatted evaluation report to stdout and logger."""
    lines = [
        "",
        "=======================================================================",
        "               CUSTOM PARAMETER & DURATION FILTER REPORT               ",
        "=======================================================================",
        f" Initial Population : {total_rtes:,} Top Routes | {total_flts:,} Total Flights",
        "-----------------------------------------------------------------------",
        " INDIVIDUAL FILTER PASS RATES (Isolated Check against Initial Pool):",
    ]
    if not stats:
        lines.append("   (No filters specified — all flights pass)")
    else:
        for name, (cnt, pct) in stats.items():
            lines.append(f"   • {name:<36} : {cnt:>10,} flts ({pct:>5.1f}%)")
            
    lines.extend([
        "-----------------------------------------------------------------------",
        " CUMULATIVE RETENTION (All Active Filters Applied Simultaneously):",
        f"   • Surviving Flights (Total)          : {metrics['surv_flights']:>10,} flts ({metrics['surv_flt_pct']:>5.1f}%)",
        f"   • Surviving Routes (>= 1 flt)        : {metrics['routes_any']:>10,} rtes ({metrics['routes_any_pct']:>5.1f}%)",
        f"   • Surviving Corridors (>= {min_flts:02d} flts)   : {metrics['routes_corridor']:>10,} rtes ({metrics['routes_corridor_pct']:>5.1f}%)",
        f"   • Usable Flights (in Corridors)      : {metrics['usable_flights']:>10,} flts ({metrics['usable_flt_pct']:>5.1f}%)",
        "=======================================================================",
        ""
    ])
    report = "\n".join(lines)
    print(report)
    for line in lines:
        if line.strip():
            logger.info(line.strip())


def parse_args() -> argparse.Namespace:
    """Parses command-line filter thresholds and options."""
    p = argparse.ArgumentParser(description="Evaluate custom parameter and duration retention.")
    p.add_argument("--top-k-routes", type=int, default=5000, help="Top K routes by volume (default: 5000).")
    p.add_argument("--min-route-flights", type=int, default=50, help="Min flights for corridor survival (default: 50).")
    p.add_argument("--max-dep-horiz-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_dep_horiz_dist"), help="Max departure horiz dist in m.")
    p.add_argument("--max-dep-vert-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_dep_vert_dist"), help="Max departure vert dist in m.")
    p.add_argument("--max-arr-horiz-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_arr_horiz_dist"), help="Max arrival horiz dist in m.")
    p.add_argument("--max-arr-vert-dist", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_arr_vert_dist"), help="Max arrival vert dist in m.")
    p.add_argument("--max-dep-candidates", type=int, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_dep_candidates"), help="Max departure candidate count.")
    p.add_argument("--max-arr-candidates", type=int, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_arr_candidates"), help="Max arrival candidate count.")
    p.add_argument("--max-duration-pct-above-median", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("max_duration_pct_above_median"), help="Max %% duration above route median.")
    p.add_argument("--min-duration-pct-below-median", type=float, default=DEFAULT_PREFILTER_THRESHOLDS.get("min_duration_pct_below_median"), help="Min %% duration below route median.")
    return p.parse_args()


def main() -> None:
    """Main execution entrypoint."""
    setup_file_logger("calibration.log")
    args = parse_args()
    
    logger.info("=== Starting Custom Parameter & Duration Filter Evaluation ===")
    df_top, total_routes = load_and_filter_top_k(args.top_k_routes)
    
    mask, filter_stats = build_filter_mask(df_top, args)
    metrics = compute_retention_metrics(df_top, mask, total_routes, args.min_route_flights)
    
    print_evaluation_report(len(df_top), total_routes, args.min_route_flights, filter_stats, metrics)
    logger.info("=== Evaluation Complete ===")


if __name__ == "__main__":
    main()
