from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Any

from src.common.config import MPS_TO_KT

def _calc_horiz_dist_m(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Calculate horizontal distance in meters using Haversine formula."""
    R = 6371000.0  # Earth radius in meters
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    delta_phi = np.radians(lat2 - lat1)
    delta_lambda = np.radians(lon2 - lon1)
    a = (
        np.sin(delta_phi / 2.0) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    return R * c

def _calc_vert_dist_m(alt_m: float, elev_m: float) -> float:
    """Calculate absolute vertical distance in meters."""
    return abs(alt_m - elev_m)

def _calc_coord_velocity_mps(df: pd.DataFrame) -> pd.Series:
    """Calculate step-to-step 3D coordinate velocity in m/s."""
    df_sorted = df.sort_values(by="time").drop_duplicates(subset=["time"])
    
    lat_rad = np.radians(df_sorted["latitude"])
    lon_rad = np.radians(df_sorted["longitude"])
    
    R = 6371000.0
    dlat = lat_rad.diff().fillna(0.0)
    dlon = lon_rad.diff().fillna(0.0)
    
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat_rad.shift(1).fillna(lat_rad)) * np.cos(lat_rad) * np.sin(dlon / 2.0) ** 2
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    horiz_dist_m = R * c
    
    vert_dist_m = df_sorted["altitude"].diff().fillna(0.0)
    
    t_series = pd.to_datetime(df_sorted["time"])
    dt = t_series.diff().dt.total_seconds().fillna(1.0)
    dt = dt.replace(0.0, 1.0)
    
    vel_horiz_mps = horiz_dist_m / dt
    vel_vert_mps = vert_dist_m / dt
    
    vel_3d_mps = np.sqrt(vel_horiz_mps**2 + vel_vert_mps**2)
    return pd.Series(vel_3d_mps, index=df_sorted.index)

def _calc_acceleration_mps2(df: pd.DataFrame) -> pd.Series:
    """Calculate step-to-step 3D acceleration in m/s^2."""
    df_sorted = df.sort_values(by="time").drop_duplicates(subset=["time"])
    
    gs_mps = df_sorted["gs"]
    rocd_mps = df_sorted["rocd"]
    
    t_series = pd.to_datetime(df_sorted["time"])
    dt = t_series.diff().dt.total_seconds().fillna(1.0)
    dt = dt.replace(0.0, 1.0)
    
    dv_horiz = gs_mps.diff().fillna(0.0)
    dv_vert = rocd_mps.diff().fillna(0.0)
    
    acc_horiz = dv_horiz / dt
    acc_vert = dv_vert / dt
    
    acc_3d = np.sqrt(acc_horiz**2 + acc_vert**2)
    return pd.Series(acc_3d, index=df_sorted.index)

def check_velocity(df: pd.DataFrame, thresholds: dict[str, float]) -> tuple[bool, str]:
    """Verify maximum 3D velocity from gs/rocd does not exceed limit (SI comparison)."""
    if df.empty:
        return False, "EMPTY_TRAJECTORY"
        
    vel_3d_mps = np.sqrt(df["gs"]**2 + df["rocd"]**2)
    max_vel = vel_3d_mps.max()
    
    limit_kt = thresholds.get("max_velocity_kt", 650.0)
    limit_mps = limit_kt / MPS_TO_KT
    
    if max_vel > limit_mps:
        max_vel_kt = max_vel * MPS_TO_KT
        return False, f"max 3D speed {max_vel_kt:.1f} kt > {limit_kt} kt"
    return True, "PASSED"

def check_coordinate_velocity(df: pd.DataFrame, thresholds: dict[str, float]) -> tuple[bool, str]:
    """Verify maximum 3D velocity from coordinates does not exceed limit (SI comparison)."""
    if df.empty:
        return False, "EMPTY_TRAJECTORY"
        
    vel_3d_mps = _calc_coord_velocity_mps(df)
    max_vel = vel_3d_mps.max()
    
    limit_kt = thresholds.get("max_velocity_kt", 650.0)
    limit_mps = limit_kt / MPS_TO_KT
    
    if max_vel > limit_mps:
        max_vel_kt = max_vel * MPS_TO_KT
        return False, f"max 3D coord speed {max_vel_kt:.1f} kt > {limit_kt} kt"
    return True, "PASSED"

def check_acceleration(df: pd.DataFrame, thresholds: dict[str, float]) -> tuple[bool, str]:
    """Verify maximum 3D acceleration does not exceed limit (m/s^2)."""
    if df.empty:
        return False, "EMPTY_TRAJECTORY"
        
    acc_3d = _calc_acceleration_mps2(df)
    max_acc = acc_3d.max()
    limit = thresholds.get("max_acceleration_mps2", 10.0)
    
    if max_acc > limit:
        return False, f"max 3D acceleration {max_acc:.2f} m/s^2 > {limit} m/s^2"
    return True, "PASSED"

def check_distance(df: pd.DataFrame, airports: dict, thresholds: dict[str, float]) -> tuple[bool, str]:
    """Verify origin/destination proximity limits (meters)."""
    if df.empty or "estdepartureairport" not in df.columns or "estarrivalairport" not in df.columns:
        return False, "EMPTY_OR_MISSING_AIRPORTS"
        
    dep_icao = df["estdepartureairport"].iloc[0]
    arr_icao = df["estarrivalairport"].iloc[0]
    
    if pd.isna(dep_icao) or pd.isna(arr_icao):
        return False, "MISSING_AIRPORT_ICAO"
        
    dep_icao, arr_icao = str(dep_icao).strip().upper(), str(arr_icao).strip().upper()
    if dep_icao not in airports or arr_icao not in airports:
        return False, f"AIRPORT_NOT_FOUND: {dep_icao}/{arr_icao}"
        
    dep_lat, dep_lon, dep_elev_ft = airports[dep_icao]["lat"], airports[dep_icao]["lon"], airports[dep_icao]["elevation"]
    arr_lat, arr_lon, arr_elev_ft = airports[arr_icao]["lat"], airports[arr_icao]["lon"], airports[arr_icao]["elevation"]
    
    dep_elev_m, arr_elev_m = dep_elev_ft / 3.280839895, arr_elev_ft / 3.280839895
    df_sorted = df.sort_values(by="time")
    
    dep_horiz = _calc_horiz_dist_m(df_sorted["latitude"].iloc[0], df_sorted["longitude"].iloc[0], dep_lat, dep_lon)
    arr_horiz = _calc_horiz_dist_m(df_sorted["latitude"].iloc[-1], df_sorted["longitude"].iloc[-1], arr_lat, arr_lon)
    dep_vert = _calc_vert_dist_m(df_sorted["altitude"].iloc[0], dep_elev_m)
    arr_vert = _calc_vert_dist_m(df_sorted["altitude"].iloc[-1], arr_elev_m)
    
    checks = [
        ("max_dep_horiz_dist", dep_horiz, "DEP_HORIZ"),
        ("max_dep_vert_dist", dep_vert, "DEP_VERT"),
        ("max_arr_horiz_dist", arr_horiz, "ARR_HORIZ"),
        ("max_arr_vert_dist", arr_vert, "ARR_VERT"),
    ]
    for key, val, name in checks:
        limit = thresholds.get(key)
        if limit is not None and val > limit:
            return False, f"{name}_DIST ({val:.1f} > {limit:.1f}m)"
            
    return True, "PASSED"
