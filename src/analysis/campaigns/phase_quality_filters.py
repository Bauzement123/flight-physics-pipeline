"""
Phase Quality Campaign Filtering Engine (Script 2 Part B)

Implements:
1. Metadata Pre-Filtering (Step 1.3): Evaluates candidate flights against 8 configurable
   metadata thresholds before loading trajectory parquet files.
2. Trajectory Post-Filtering (Step 1.4): Evaluates loaded trajectories against physical
   waypoint counts, phase progression, and ROCD anomaly checks.

All default parameters are None (ignored/pass-through unless overridden via CLI).
"""

import logging
import pandas as pd
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


def apply_metadata_prefilters(
    df_candidates: pd.DataFrame,
    df_route_summary: pd.DataFrame,
    thresholds: Dict[str, Any]
) -> pd.DataFrame:
    """
    Applies the 8 master pre-filter options to df_candidates using median durations
    from df_route_summary.
    
    If a threshold in `thresholds` is None, that check is skipped entirely.
    
    Returns a DataFrame annotated with:
        - 'status': "PASSED" or "REJECTED"
        - 'fail_stage': "PREFILTER" (if rejected) or "PASSED"
        - 'reject_reason': Exact descriptive string of the failure reason.
    """
    logger.info(f"Applying metadata pre-filters across {len(df_candidates)} candidate flights...")
    
    # 1. Map route IDs (e.g. 'EDDF-LIRF') to median duration in seconds from route summary
    route_medians_min = {}
    if not df_route_summary.empty and "route" in df_route_summary.columns and "route_duration_median" in df_route_summary.columns:
        for _, row in df_route_summary.iterrows():
            clean_route = str(row["route"]).replace(" -> ", "-").replace(" ", "")
            route_medians_min[clean_route] = float(row["route_duration_median"])
        logger.info(f"Loaded median durations for {len(route_medians_min)} routes from summary.")
    else:
        logger.warning("Route summary DataFrame is empty or missing required columns! Duration checks will be skipped if median is unavailable.")

    statuses = []
    fail_stages = []
    reject_reasons = []

    for idx, row in df_candidates.iterrows():
        route_id = str(row.get("route_id", ""))
        median_min = route_medians_min.get(route_id, None)
        median_s = (median_min * 60.0) if median_min is not None else None
        
        rejected = False
        reason = "PASSED"
        
        # 1. Departure horizontal distance (meters)
        val = row.get("estdepartureairporthorizdistance", None)
        thresh = thresholds.get("max_dep_horiz_dist", None)
        if thresh is not None and val is not None and not pd.isna(val) and float(val) > float(thresh):
            rejected = True
            reason = f"DEP_HORIZ_DIST ({float(val):.1f} > {float(thresh):.1f}m)"
        
        # 2. Departure vertical distance (meters, absolute altitude diff from airport)
        if not rejected:
            val = row.get("estdepartureairportvertdistance", None)
            thresh = thresholds.get("max_dep_vert_dist", None)
            if thresh is not None and val is not None and not pd.isna(val) and abs(float(val)) > float(thresh):
                rejected = True
                reason = f"DEP_VERT_DIST ({abs(float(val)):.1f} > {float(thresh):.1f}m)"
                
        # 3. Arrival horizontal distance (meters)
        if not rejected:
            val = row.get("estarrivalairporthorizdistance", None)
            thresh = thresholds.get("max_arr_horiz_dist", None)
            if thresh is not None and val is not None and not pd.isna(val) and float(val) > float(thresh):
                rejected = True
                reason = f"ARR_HORIZ_DIST ({float(val):.1f} > {float(thresh):.1f}m)"
                
        # 4. Arrival vertical distance (meters, absolute altitude diff from airport)
        if not rejected:
            val = row.get("estarrivalairportvertdistance", None)
            thresh = thresholds.get("max_arr_vert_dist", None)
            if thresh is not None and val is not None and not pd.isna(val) and abs(float(val)) > float(thresh):
                rejected = True
                reason = f"ARR_VERT_DIST ({abs(float(val)):.1f} > {float(thresh):.1f}m)"
                
        # 5. Departure candidates count
        if not rejected:
            val = row.get("departureairportcandidatescount", None)
            thresh = thresholds.get("max_dep_candidates", None)
            if thresh is not None and val is not None and not pd.isna(val) and int(val) > int(thresh):
                rejected = True
                reason = f"DEP_CANDIDATES ({int(val)} > {int(thresh)})"
                
        # 6. Arrival candidates count
        if not rejected:
            val = row.get("arrivalairportcandidatescount", None)
            thresh = thresholds.get("max_arr_candidates", None)
            if thresh is not None and val is not None and not pd.isna(val) and int(val) > int(thresh):
                rejected = True
                reason = f"ARR_CANDIDATES ({int(val)} > {int(thresh)})"
                
        # 7. Duration percentage above median
        if not rejected:
            val = row.get("duration_s", None)
            thresh = thresholds.get("max_duration_pct_above_median", None)
            if thresh is not None and val is not None and not pd.isna(val) and median_s is not None and median_s > 0:
                max_allowed_s = median_s * (1.0 + float(thresh) / 100.0)
                if float(val) > max_allowed_s:
                    rejected = True
                    reason = f"DURATION_ABOVE_MEDIAN ({float(val):.1f}s > +{float(thresh):.1f}% of {median_s:.1f}s)"
                    
        # 8. Duration percentage below median (Option A)
        if not rejected:
            val = row.get("duration_s", None)
            thresh = thresholds.get("min_duration_pct_below_median", None)
            if thresh is not None and val is not None and not pd.isna(val) and median_s is not None and median_s > 0:
                min_allowed_s = median_s * (1.0 - float(thresh) / 100.0)
                if float(val) < min_allowed_s:
                    rejected = True
                    reason = f"DURATION_BELOW_MEDIAN ({float(val):.1f}s < -{float(thresh):.1f}% of {median_s:.1f}s)"
                    
        if rejected:
            statuses.append("REJECTED")
            fail_stages.append("PREFILTER")
            reject_reasons.append(reason)
        else:
            statuses.append("PASSED")
            fail_stages.append("PASSED")
            reject_reasons.append("PASSED")

    df_out = df_candidates.copy()
    df_out["status"] = statuses
    df_out["fail_stage"] = fail_stages
    df_out["reject_reason"] = reject_reasons
    
    n_passed = sum(1 for s in statuses if s == "PASSED")
    n_rejected = sum(1 for s in statuses if s == "REJECTED")
    logger.info(f"Metadata pre-filter results: {n_passed} PASSED, {n_rejected} REJECTED.")
    
    return df_out


def apply_trajectory_postfilters(
    flight_id: str,
    df_traj: pd.DataFrame,
    thresholds: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Step 1.4 Placeholder: Evaluates a loaded trajectory against physical waypoint count,
    phase progression (CLIMB -> CRUISE -> DESCENT), and ROCD anomaly rules.
    
    Returns (passed: bool, reject_reason: str).
    """
    # Will be implemented in Step 1.4
    return True, "PASSED"
