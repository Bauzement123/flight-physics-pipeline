from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Any

from src.common.config import MPS_TO_KT, MPS_TO_FPM
from src.common.utils import haversine_distance_m

def _calc_horiz_dist_m(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Calculate horizontal distance in meters using Haversine formula."""
    return haversine_distance_m(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)

def _calc_vert_dist_m(alt_m: float, elev_m: float) -> float:
    """Calculate absolute vertical distance in meters."""
    return abs(alt_m - elev_m)

def _calc_coord_velocity_mps(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Calculate step-to-step horizontal and vertical coordinate velocities in m/s.

    Returns:
        vel_horiz_mps: Haversine-derived horizontal speed series (m/s).
        vel_vert_mps:  Altitude-diff-derived vertical speed series (m/s, signed).
    """
    df_sorted = df.sort_values(by="time").drop_duplicates(subset=["time"])

    lat = pd.to_numeric(df_sorted["latitude"], errors="coerce").astype(float)
    lon = pd.to_numeric(df_sorted["longitude"], errors="coerce").astype(float)
    alt = pd.to_numeric(df_sorted["altitude"], errors="coerce").astype(float)

    horiz_dist_m = haversine_distance_m(
        lat1=lat.shift(1).fillna(lat),
        lon1=lon.shift(1).fillna(lon),
        lat2=lat,
        lon2=lon,
    )

    vert_dist_m = alt.diff().fillna(0.0)

    t_series = pd.to_datetime(df_sorted["time"])
    dt = t_series.diff().dt.total_seconds().fillna(1.0)
    dt = dt.replace(0.0, 1.0)

    vel_horiz_mps = pd.Series(horiz_dist_m / dt, index=df_sorted.index)
    vel_vert_mps  = pd.Series(vert_dist_m  / dt, index=df_sorted.index)

    return vel_horiz_mps, vel_vert_mps

def _calc_acceleration_mps2(df: pd.DataFrame) -> pd.Series:
    """Calculate step-to-step 3D acceleration in m/s^2."""
    df_sorted = df.sort_values(by="time").drop_duplicates(subset=["time"])

    gs_mps = pd.to_numeric(df_sorted["gs"], errors="coerce").astype(float)
    rocd_mps = pd.to_numeric(df_sorted["rocd"], errors="coerce").astype(float)

    if gs_mps.notna().sum() < 2 and rocd_mps.notna().sum() < 2:
        return pd.Series(dtype=float)

    t_series = pd.to_datetime(df_sorted["time"])
    dt = t_series.diff().dt.total_seconds()
    dt = dt.replace(0.0, np.nan)

    dv_horiz = gs_mps.diff()
    dv_vert = rocd_mps.diff()

    acc_horiz = dv_horiz / dt
    acc_vert = dv_vert / dt

    acc_3d = np.sqrt(acc_horiz**2 + acc_vert**2)
    return pd.Series(acc_3d, index=df_sorted.index)

def _safe_float(val: Any) -> float:
    """Safely convert value to float, mapping pd.NA / NaN / invalid types to float('nan')."""
    if pd.isna(val) or val is pd.NA:
        return float("nan")
    try:
        return float(val)
    except (ValueError, TypeError):
        return float("nan")

# ---------------------------------------------------------------------------
# Metric Extractors - Feature computation
# ---------------------------------------------------------------------------

def extract_horiz_velocity_metric(df: pd.DataFrame) -> float:
    """Extract max horizontal speed from gs (m/s)."""
    if df.empty or "gs" not in df.columns:
        return float("nan")
    return _safe_float(df["gs"].max())

def extract_vert_velocity_metric(df: pd.DataFrame) -> float:
    """Extract max vertical speed from rocd (m/s)."""
    if df.empty or "rocd" not in df.columns:
        return float("nan")
    return _safe_float(df["rocd"].abs().max())

def extract_coord_horiz_velocity_metric(df: pd.DataFrame) -> float:
    """Extract max horizontal coordinate-derived speed (m/s)."""
    if df.empty or "latitude" not in df.columns or "longitude" not in df.columns:
        return float("nan")
    vel_horiz_mps, _ = _calc_coord_velocity_mps(df)
    return _safe_float(vel_horiz_mps.max())

def extract_coord_vert_velocity_metric(df: pd.DataFrame) -> float:
    """Extract max vertical coordinate-derived speed (m/s)."""
    if df.empty or "altitude" not in df.columns:
        return float("nan")
    _, vel_vert_mps = _calc_coord_velocity_mps(df)
    return _safe_float(vel_vert_mps.abs().max())

def extract_acceleration_metric(df: pd.DataFrame) -> float:
    """Extract maximum 3D acceleration (m/s^2)."""
    if df.empty or "gs" not in df.columns or "rocd" not in df.columns:
        return float("nan")
    acc_3d = _calc_acceleration_mps2(df)
    return _safe_float(acc_3d.max())

def extract_distance_metrics(df: pd.DataFrame, airports: dict) -> dict[str, float]:
    """Extract origin/destination proximity distances (meters)."""
    result = {
        "dep_horiz_dist_m": float("nan"),
        "dep_vert_dist_m": float("nan"),
        "arr_horiz_dist_m": float("nan"),
        "arr_vert_dist_m": float("nan"),
    }
    
    if df.empty or "estdepartureairport" not in df.columns or "estarrivalairport" not in df.columns:
        return result

    dep_icao = df["estdepartureairport"].iloc[0]
    arr_icao = df["estarrivalairport"].iloc[0]

    if pd.isna(dep_icao) or pd.isna(arr_icao):
        return result

    dep_icao, arr_icao = str(dep_icao).strip().upper(), str(arr_icao).strip().upper()
    if dep_icao not in airports or arr_icao not in airports:
        return result

    dep_lat, dep_lon = airports[dep_icao]["lat"], airports[dep_icao]["lon"]
    arr_lat, arr_lon = airports[arr_icao]["lat"], airports[arr_icao]["lon"]
    dep_elev_ft = airports[dep_icao].get("elevation", airports[dep_icao].get("elev", 0.0)) or 0.0
    arr_elev_ft = airports[arr_icao].get("elevation", airports[arr_icao].get("elev", 0.0)) or 0.0

    dep_elev_m, arr_elev_m = dep_elev_ft / 3.280839895, arr_elev_ft / 3.280839895
    df_sorted = df.sort_values(by="time")

    if "latitude" in df_sorted.columns and "longitude" in df_sorted.columns:
        df_coords = df_sorted.dropna(subset=["latitude", "longitude"])
        if not df_coords.empty:
            first_c = df_coords.iloc[0]
            last_c = df_coords.iloc[-1]
            result["dep_horiz_dist_m"] = _safe_float(_calc_horiz_dist_m(first_c["latitude"], first_c["longitude"], dep_lat, dep_lon))
            result["arr_horiz_dist_m"] = _safe_float(_calc_horiz_dist_m(last_c["latitude"], last_c["longitude"], arr_lat, arr_lon))

    if "altitude" in df_sorted.columns:
        df_alt = df_sorted.dropna(subset=["altitude"])
        if not df_alt.empty:
            first_alt = df_alt["altitude"].iloc[0]
            last_alt = df_alt["altitude"].iloc[-1]
            result["dep_vert_dist_m"]  = _safe_float(_calc_vert_dist_m(first_alt, dep_elev_m))
            result["arr_vert_dist_m"]  = _safe_float(_calc_vert_dist_m(last_alt, arr_elev_m))

    return result
