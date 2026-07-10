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


def calculate_acceleration(df_clean: pd.DataFrame) -> pd.Series:
    """
    Calculate 3D acceleration (m/s^2) step-to-step.
    
    Acceleration is computed from horizontal ground speed (gs converted to m/s)
    and vertical rate (rocd in m/s).
    """
    import numpy as np
    
    gs_col = "gs" if "gs" in df_clean.columns else ("velocity" if "velocity" in df_clean.columns else "gs")
    rocd_col = "rocd" if "rocd" in df_clean.columns else ("vertrate" if "vertrate" in df_clean.columns else "rocd")
    time_col = "time" if "time" in df_clean.columns else "timestamp"
    
    # Convert ground speed from knots to m/s
    gs_mps = df_clean[gs_col] * 0.514444
    
    # Get vertical speed (already in m/s)
    rocd_mps = df_clean[rocd_col]
    
    # Calculate dt in seconds
    t_series = pd.to_datetime(df_clean[time_col])
    dt = t_series.diff().dt.total_seconds().fillna(1.0)
    # Avoid division by zero
    dt = dt.replace(0.0, 1.0)
    
    # Calculate components
    dv_horiz = gs_mps.diff().fillna(0.0)
    dv_vert = rocd_mps.diff().fillna(0.0)
    
    acc_horiz = dv_horiz / dt
    acc_vert = dv_vert / dt
    
    # 3D acceleration: sqrt(acc_horiz^2 + acc_vert^2)
    acc_3d = np.sqrt(acc_horiz**2 + acc_vert**2)
    return pd.Series(acc_3d, index=df_clean.index)


def get_airport_coords(origin_icao: str, dest_icao: str) -> dict:
    """
    Look up airport coordinates and elevation (ft) from airportsdata database.
    
    Returns:
        dict: {'origin': (lat, lon, elev_ft), 'dest': (lat, lon, elev_ft)}
    """
    import airportsdata
    
    data = airportsdata.load()
    coords = {}
    
    for key, icao in [("origin", origin_icao), ("dest", dest_icao)]:
        if icao in data:
            info = data[icao]
            coords[key] = (info["lat"], info["lon"], info["elevation"])
        else:
            logger.warning(f"Airport {icao} not found in airportsdata database! Using fallback (0,0,0).")
            coords[key] = (0.0, 0.0, 0.0)
            
    return coords


def recompute_airport_distances(df_clean: pd.DataFrame, airport_coords: dict) -> pd.DataFrame:
    """
    Populate columns based on origin/destination airport coordinates.
    
    Parameters
    ----------
    df_clean : pd.DataFrame
        EKF-smoothed trajectory.
    airport_coords : dict
        Mapping {'origin': (lat, lon, elev_ft), 'dest': (lat, lon, elev_ft)}.
        
    Returns
    -------
    pd.DataFrame
        Copy of df_clean with:
        * 'dist_hor_nm' - airport-to-airport horizontal distance (nautical miles)
        * 'dist_vert_ft' - airport-to-airport vertical distance (dest - origin elevation) (ft)
        * 'dist_total_nm' - combined 3D distance
    """
    from math import radians, sin, cos, sqrt, atan2
    
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371.0  # Earth radius in km
        φ1, φ2 = map(radians, [lat1, lat2])
        Δφ = radians(lat2 - lat1)
        Δλ = radians(lon2 - lon1)
        a = sin(Δφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(Δλ / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c
        
    o_lat, o_lon, o_elev_ft = airport_coords["origin"]
    d_lat, d_lon, d_elev_ft = airport_coords["dest"]
    
    horiz_km = haversine(o_lat, o_lon, d_lat, d_lon)
    horiz_nm = horiz_km * 0.539957
    vert_ft = d_elev_ft - o_elev_ft
    
    df_out = df_clean.copy()
    df_out["dist_hor_nm"] = horiz_nm
    df_out["dist_vert_ft"] = vert_ft
    df_out["dist_total_nm"] = sqrt(horiz_nm ** 2 + (vert_ft / 6076.12) ** 2)
    return df_out


def filter_max_velocity(df_clean: pd.DataFrame, thresholds: dict) -> tuple[bool, str, dict]:
    """
    Check if maximum 3D velocity exceeds max_velocity_kt limit.
    
    3D velocity = sqrt(gs_kt^2 + vertrate_kt^2)
    """
    import numpy as np
    
    gs_col = "gs" if "gs" in df_clean.columns else ("velocity" if "velocity" in df_clean.columns else "gs")
    rocd_col = "rocd" if "rocd" in df_clean.columns else ("vertrate" if "vertrate" in df_clean.columns else "rocd")
    
    gs_kt = df_clean[gs_col]
    # convert rocd from m/s to knots: 1 m/s = 1.9438444924 knots
    rocd_kt = df_clean[rocd_col] * 1.9438444924
    
    vel_3d_kt = np.sqrt(gs_kt**2 + rocd_kt**2)
    max_vel_3d = vel_3d_kt.max()
    
    metrics = {"max_speed_3d_kt": max_vel_3d}
    limit = thresholds.get("max_velocity_kt", 650.0)
    
    if max_vel_3d > limit:
        return True, f"max 3D speed {max_vel_3d:.1f} kt > {limit} kt", metrics
    return False, "PASSED", metrics


def filter_max_acceleration(df_clean: pd.DataFrame, thresholds: dict) -> tuple[bool, str, dict]:
    """
    Check if maximum step-to-step 3D acceleration exceeds limit (m/s^2).
    """
    acc_3d = calculate_acceleration(df_clean)
    max_acc = acc_3d.max()
    
    metrics = {"max_acceleration_mps2": max_acc}
    limit = thresholds.get("max_acceleration_mps2", 340.29)
    
    if max_acc > limit:
        return True, f"max 3D acceleration {max_acc:.2f} m/s^2 > {limit} m/s^2", metrics
    return False, "PASSED", metrics


def passes_distance_prefilters(df_clean: pd.DataFrame, thresholds: dict) -> tuple[bool, str, dict]:
    """
    Check if the first and last waypoints of df_clean are within distance pre-filter limits
    of the origin and destination airports.
    """
    from math import radians, sin, cos, sqrt, atan2
    
    dep_col = "estdepartureairport"
    arr_col = "estarrivalairport"
    
    if dep_col not in df_clean.columns or arr_col not in df_clean.columns:
        return False, "PASSED", {}
        
    dep_icao = df_clean[dep_col].iloc[0]
    arr_icao = df_clean[arr_col].iloc[0]
    
    if pd.isna(dep_icao) or pd.isna(arr_icao):
        return False, "PASSED", {}
        
    coords = get_airport_coords(dep_icao, arr_icao)
    o_lat, o_lon, o_elev_ft = coords["origin"]
    d_lat, d_lon, d_elev_ft = coords["dest"]
    
    o_elev_m = o_elev_ft / 3.280839895
    d_elev_m = d_elev_ft / 3.280839895
    
    # Get first and last valid waypoints
    lat_col = "latitude" if "latitude" in df_clean.columns else "lat"
    lon_col = "longitude" if "longitude" in df_clean.columns else "lon"
    alt_col = "altitude" if "altitude" in df_clean.columns else ("baroaltitude" if "baroaltitude" in df_clean.columns else "geoaltitude")
    
    # Sort by time to make sure first/last are correct
    time_col = "time" if "time" in df_clean.columns else "timestamp"
    df_sorted = df_clean.sort_values(by=time_col)
    
    first_lat = df_sorted[lat_col].iloc[0]
    first_lon = df_sorted[lon_col].iloc[0]
    first_alt = df_sorted[alt_col].iloc[0]
    
    last_lat = df_sorted[lat_col].iloc[-1]
    last_lon = df_sorted[lon_col].iloc[-1]
    last_alt = df_sorted[alt_col].iloc[-1]
    
    def haversine_m(lat1, lon1, lat2, lon2):
        R = 6371000.0  # Earth radius in meters
        φ1, φ2 = map(radians, [lat1, lat2])
        Δφ = radians(lat2 - lat1)
        Δλ = radians(lon2 - lon1)
        a = sin(Δφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(Δλ / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c
        
    dep_horiz = haversine_m(first_lat, first_lon, o_lat, o_lon)
    dep_vert = abs(first_alt - o_elev_m)
    
    arr_horiz = haversine_m(last_lat, last_lon, d_lat, d_lon)
    arr_vert = abs(last_alt - d_elev_m)
    
    metrics = {
        "recomputed_dep_horiz_m": dep_horiz,
        "recomputed_dep_vert_m": dep_vert,
        "recomputed_arr_horiz_m": arr_horiz,
        "recomputed_arr_vert_m": arr_vert,
    }
    
    thresh = thresholds.get("max_dep_horiz_dist")
    if thresh is not None and dep_horiz > thresh:
        return True, f"DEP_HORIZ_DIST ({dep_horiz:.1f} > {thresh:.1f}m)", metrics
        
    thresh = thresholds.get("max_dep_vert_dist")
    if thresh is not None and dep_vert > thresh:
        return True, f"DEP_VERT_DIST ({dep_vert:.1f} > {thresh:.1f}m)", metrics
        
    thresh = thresholds.get("max_arr_horiz_dist")
    if thresh is not None and arr_horiz > thresh:
        return True, f"ARR_HORIZ_DIST ({arr_horiz:.1f} > {thresh:.1f}m)", metrics
        
    thresh = thresholds.get("max_arr_vert_dist")
    if thresh is not None and arr_vert > thresh:
        return True, f"ARR_VERT_DIST ({arr_vert:.1f} > {thresh:.1f}m)", metrics
        
    return False, "PASSED", metrics


def apply_trajectory_postfilters(
    df_clean: pd.DataFrame,
    df_raw: pd.DataFrame,
    thresholds: Dict[str, Any]
) -> Tuple[bool, str, dict]:
    """
    Evaluates a loaded trajectory against max 3D speed, max 3D acceleration,
    and recomputed distance pre-filters.
    
    Returns (rejected: bool, reason: str, metrics: dict).
    """
    if df_clean.empty:
        return True, "EMPTY_CLEAN_TRAJECTORY", {}
        
    all_metrics = {}
    
    # 1. Max Velocity
    rejected, reason, metrics = filter_max_velocity(df_clean, thresholds)
    all_metrics.update(metrics)
    if rejected:
        return True, reason, all_metrics
        
    # 2. Max Acceleration
    rejected, reason, metrics = filter_max_acceleration(df_clean, thresholds)
    all_metrics.update(metrics)
    if rejected:
        return True, reason, all_metrics
        
    # 3. Distance Pre-filters
    from src.common import config
    merged_thresholds = config.DEFAULT_PREFILTER_THRESHOLDS.copy()
    merged_thresholds.update(thresholds)
    
    rejected, reason, metrics = passes_distance_prefilters(df_clean, merged_thresholds)
    all_metrics.update(metrics)
    if rejected:
        return True, reason, all_metrics
        
    return False, "PASSED", all_metrics
