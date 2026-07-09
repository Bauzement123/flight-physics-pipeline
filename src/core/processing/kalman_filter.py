"""
Module 2.1: Trajectory Processing & Filtering
============================================================
Reads raw OpenSky trajectories and produces clean, equidistant SI-unit Parquet
trajectories suitable for corridor-level analysis and physical simulation.

Processing pipeline per flight
------------------------------
  raw parquet  →  pycontrails.Flight  →  traffic.Flight
  → .airborne()                          (strip ground data)
  → grid injection                       (uniform CORRIDOR_TIME_GRID_SECONDS grid)
  → geodetic gap filling                 (great-circle lat/lon for gaps >100 km)
  → time interpolation                   (scalar columns)
  → LAEA Cartesian projection            (pyproj: lat/lon → x, y in metres)
  → ProcessXYZZFilterBase.preprocess()   (aviation units → EKF state space)
  → EKF forward pass                     (verbatim traffic code + 2 diag lines)
  → traffic.rts_smoother()               (RTS backward pass, no custom math)
  → postprocess + back-project           (EKF state → aviation units → lat/lon)
  → slice at grid timestamps             (.isin(grid_times))
  → OpenAP phase labeling
  → traffic_to_pycontrails()             (aviation units → SI)

Two programmatic entry points allow in-process injection from other pipeline
stages without a disk round-trip:
  clean_traffic_flight(traffic_flight, flight_id, typecode, ...)
  clean_pycontrails_flight(pyc_flight, flight_id, typecode, ...)
"""

import argparse
import logging
import os
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pyproj import CRS, Geod, Transformer
from scipy import linalg
from traffic.core import Flight
from traffic.algorithms.filters.ekf import EKF, ProcessXYZZFilterBase, rts_smoother

from src.common.adapters import (
    parquet_to_pycontrails,
    pycontrails_to_traffic,
    traffic_to_pycontrails,
    write_flights_to_parquet,
)
from src.common.config import (
    BASE_DIR,
    CORRIDOR_TIME_GRID_SECONDS,
    GLOBAL_CLEAN_REGISTRY,
    is_supported_typecode,
)
from src.common.utils import (
    extract_target_routes,
    log_skipped_aircraft,
    setup_file_logger,
    update_global_registry,
)

warnings.simplefilter(action="ignore", category=FutureWarning)

ArrayFloat = npt.NDArray[np.floating]

GEODESIC_DISTANCE_THRESHOLD_M: float = 100_000.0


# ==============================================================================
# EKF helpers — verbatim copies of traffic.algorithms.filters.ekf internals
# with 2 extra diagnostic-recording lines in _ekf_correct.
# ==============================================================================

def _compute_R_Q(
    measurements: pd.DataFrame,
    window_size: int = 17,
) -> tuple[ArrayFloat, ArrayFloat]:
    """
    Compute empirical measurement noise covariance R and process noise covariance Q.

    Verbatim copy of traffic EKF.apply() lines 246-326.

    R is built from per-channel rolling-window residual variance:
        R_jj = ( std(z_j - rolling_mean(z_j, W))^2 + sigma_sensor_j^2 )^(1/2)  ... squared again
    i.e. R_jj = std(z_j - rolling_mean(z_j, W))^2 + sigma_sensor_j^2

    The sensor baseline standard deviations (std_dev_gps, etc.) are all zero here,
    matching traffic's defaults.  They are kept as named variables so they can be
    tuned per-sensor without changing the formula.

    Q is scaled from R by empirical diagonal multipliers (verbatim traffic):
        Q = diag([0.1, 0.1, 0.01, 0.3, 1.0, 0.5]) * R
    These weights reflect the relative process noise per state component:
      x, y       — small (0.1): position changes slowly relative to measurement noise
      alt_baro   — very small (0.01): barometric altitude is stable
      math_angle — moderate (0.3): heading can change at turns
      velocity   — high (1.0): groundspeed varies with wind
      vert_rate  — moderate (0.5): vertical rate fluctuates around cruise
    """
    # Baseline sensor noise standard deviations (set to 0 = fully data-driven, per traffic defaults)
    std_dev_gps        = 0   # horizontal position sensor noise floor (m)
    std_dev_baro       = 0   # barometric altitude sensor noise floor (m)
    std_dev_track      = 0   # track angle sensor noise floor (rad)
    std_dev_gps_speed  = 0   # groundspeed sensor noise floor (m/s)
    std_dev_baro_speed = 0   # vertical rate sensor noise floor (m/s)

    # _rv: rolling-residual variance for one channel + sensor noise floor
    def _rv(col: pd.Series, s: float) -> float:
        return (col - col.rolling(window_size).mean()).std() ** 2 + s ** 2

    # Build diagonal R: take sqrt of each variance entry, form diagonal matrix, then square it.
    # Net result: R_jj = variance of the rolling residual + sensor_noise^2  (matches traffic exactly)
    R = (
        np.diag([
            _rv(measurements.x,          std_dev_gps)        ** 0.5,
            _rv(measurements.y,          std_dev_gps)        ** 0.5,
            _rv(measurements.alt_baro,   std_dev_baro)       ** 0.5,
            _rv(measurements.math_angle, std_dev_track)      ** 0.5,
            _rv(measurements.velocity,   std_dev_gps_speed)  ** 0.5,
            _rv(measurements.vert_rate,  std_dev_baro_speed) ** 0.5,
        ])
    ) ** 2
    Q = np.diag([0.1, 0.1, 0.01, 0.3, 1.0, 0.5]) * R
    return R, Q


def _ekf_predict(
    x: pd.Series,
    P: ArrayFloat,
    Q: ArrayFloat,
    dt: float,
) -> tuple[pd.Series, ArrayFloat]:
    """
    EKF prediction step — verbatim copy of traffic extended_kalman_filter lines 40-45.

    Uses the traffic-library's kinematic state-transition functions:
      F   = Jacobian of f(x, dt) w.r.t. x  — linearises the nonlinear motion model
      x_p = f(x, dt)                        — propagates state forward by dt seconds
      P_p = F P F^T + Q                     — propagates covariance (Chapman-Kolmogorov)
    """
    F      = EKF.jacobian_state_transition(x, dt)   # (6×6) linearised state-transition matrix
    x_pred = EKF.state_transition_function(x, dt)   # nonlinear kinematic propagation of x
    P_pred = F @ P @ F.T + Q                        # a-priori covariance (process noise added)
    return x_pred, P_pred


def _ekf_correct(
    x_pred: pd.Series,
    P_pred: ArrayFloat,
    R: ArrayFloat,
    measurement: pd.Series,
    reject_sigma: float = 3.0,
) -> tuple[pd.Series, ArrayFloat, ArrayFloat, ArrayFloat]:
    """
    EKF correction (measurement update) step.
    Verbatim copy of traffic extended_kalman_filter lines 47-73, with 2 extra lines
    to record S and nu for diagnostic export.

    Observation model: H = I_6 (direct state measurement — each ADS-B field maps
    to exactly one state component).  Per-component sigma gating rejects outliers
    by temporarily setting H[j,j]=0 for any component where |nu_j| > 3*sigma_j,
    effectively treating that measurement as missing for this step.

    Kalman gain:  K = P_pred H^T (H P_pred H^T + R)^{-1}
    State update: x = x_pred + K nu        (nu = pre-gate innovation)
    Cov update:   P = (I - K H) P_pred
    """
    n  = len(x_pred)
    H  = np.eye(n)            # identity: each z_j directly observes x_j
    nu = measurement - x_pred # pre-gate innovation: z_k - x_{k|k-1}
    S  = H @ P_pred @ H.T + R # innovation covariance (first pass, before gating)
    std_devs = np.sqrt(np.diag(S))

    # Component-wise sigma gating (verbatim traffic)
    for j in range(n):
        if abs(nu.iloc[j]) > abs(reject_sigma * std_devs[j]):
            measurement.iloc[j] = x_pred.iloc[j]  # replace rejected measurement with prediction
            H[j, j] = 0                            # zero out this component in the update

    S   = H @ P_pred @ H.T + R                          # S re-computed after gating  ← diagnostic
    K   = linalg.solve(S, H @ P_pred, assume_a="pos").T  # optimal Kalman gain
    x_u = x_pred + K @ nu                               # state update (pre-gate nu, verbatim traffic)
    P_u = (np.eye(n) - K @ H) @ P_pred                  # Joseph-form covariance update
    return x_u, P_u, S, nu.to_numpy()                   # nu returned for diagnostics  ← diagnostic


# ==============================================================================
# Core EKF orchestrator
# ==============================================================================

def run_6d_kinematic_ekf(
    measurements: pd.DataFrame,
) -> tuple[pd.DataFrame, ArrayFloat, ArrayFloat, ArrayFloat]:
    """
    6D kinematic EKF forward pass followed by traffic library's RTS smoother.

    State vector: x = [x, y, alt_baro, math_angle, velocity, vert_rate]
      x, y         — LAEA Cartesian position (metres)
      alt_baro     — barometric altitude (metres)
      math_angle   — unwrapped heading in mathematical convention (radians)
      velocity     — groundspeed (m/s)
      vert_rate    — vertical rate (m/s)

    Forward pass: verbatim copy of traffic.extended_kalman_filter internals,
    with 2 extra lines per step to record S_hist and e_hist for diagnostics.
    Backward pass: calls traffic.rts_smoother directly — no custom RTS math.

    Parameters
    ----------
    measurements : pd.DataFrame
        Output of ProcessXYZZFilterBase.preprocess(): DatetimeIndex,
        columns [x, y, alt_baro, math_angle, velocity, vert_rate].

    Returns
    -------
    smoothed_states : pd.DataFrame   RTS-smoothed state, same shape as measurements
    S_hist          : (T, 6, 6)      innovation covariance S_k = H P_{k|k-1} H^T + R
    P_hist          : (T, 6, 6)      a-posteriori covariance P_{k|k}
    e_hist          : (T, 6)         pre-gate innovation nu_k = z_k - x_{k|k-1}
    """
    n_states   = len(measurements.columns)   # 6
    n_steps    = len(measurements)
    R, Q       = _compute_R_Q(measurements)  # empirical noise matrices from rolling residuals
    timestamps = measurements.index.to_series()

    # Pre-allocate: states and covariances for the forward pass; S and e for diagnostics
    states      = np.repeat(measurements.iloc[0].to_numpy().reshape(1, -1), n_steps, axis=0)
    covariances = np.zeros((n_steps, n_states, n_states))   # P_{k|k} history
    S_hist      = np.zeros((n_steps, n_states, n_states))   # innovation covariance history
    e_hist      = np.zeros((n_steps, n_states))             # innovation vector history

    x = measurements.iloc[0]   # initial state x_0
    P = np.eye(n_states)        # initial covariance P_0 = I  (uninformative)

    # Forward EKF loop (verbatim traffic extended_kalman_filter, + 2 recording lines)
    for i in range(1, n_steps):
        dt             = (timestamps.iloc[i] - timestamps.iloc[i - 1]).total_seconds()
        x, P           = _ekf_predict(x, P, Q, dt)                    # a-priori step
        x, P, S, nu    = _ekf_correct(x, P, R, measurements.iloc[i].copy())  # a-posteriori step
        states[i]      = x.to_numpy()
        covariances[i] = P
        S_hist[i]      = S   # diagnostic: innovation covariance at step i
        e_hist[i]      = nu  # diagnostic: pre-gate innovation at step i

    # Wrap forward-filtered states into a DataFrame (same index as measurements)
    states_df = pd.DataFrame(states, index=measurements.index, columns=measurements.columns)

    # Backward RTS smoother — call traffic library directly, no custom math here
    smoothed_states = rts_smoother(
        states_df, covariances, Q,
        measurements.index,            # DatetimeIndex used internally for dt computation
        EKF.jacobian_state_transition,
        EKF.state_transition_function,
    )
    return smoothed_states, S_hist, covariances, e_hist


# ==============================================================================
# Quality metrics
# ==============================================================================

def compute_ekf_quality_metrics(
    S_hist: ArrayFloat,
    P_hist: ArrayFloat,
    e_hist: ArrayFloat,
) -> tuple[float, float, float]:
    """
    Derives three scalar quality metrics from the EKF forward-pass diagnostics.

    ekf_mean_nis    — mean Normalized Innovation Squared across valid steps:
                      NIS_k = nu_k^T S_k^{-1} nu_k
                      For a well-tuned filter NIS_k ~ chi^2(6), mean ≈ 6.
                      Values >> 6 indicate filter inconsistency or outliers.

    ekf_max_trace_p — maximum trace of the a-posteriori covariance P_{k|k}.
                      High values flag segments where the filter is uncertain
                      (e.g., long ADS-B gaps or rejected measurements).

    ekf_quality_score — composite scalar in [0, 1]:
                        score = clip(nis_factor * trace_factor, 0, 1)
                        nis_factor   = 6 / max(6, mean_nis)   (penalises inconsistency)
                        trace_factor = exp(-(max_trace - 60) / 500)  (penalises high uncertainty)
    """
    nis_sum     = 0.0
    valid_steps = 0
    max_trace_p = 0.0

    for i in range(1, len(e_hist)):
        tr_p = float(np.trace(P_hist[i]))   # trace(P_{k|k}) = sum of state variances
        if tr_p > max_trace_p:
            max_trace_p = tr_p
        if np.any(e_hist[i] != 0):           # skip steps where no real measurement was used
            try:
                nis = float(e_hist[i] @ linalg.inv(S_hist[i]) @ e_hist[i])  # NIS_k
                if np.isfinite(nis) and nis < 1e5:
                    nis_sum    += nis
                    valid_steps += 1
            except Exception:
                pass

    mean_nis      = nis_sum / max(1, valid_steps)
    nis_factor    = 6.0 / max(6.0, mean_nis)                              # bounded at 1 for mean_nis ≤ 6
    trace_factor  = float(np.exp(-max(0.0, max_trace_p - 60.0) / 500.0)) # exponential penalty
    quality_score = float(np.clip(nis_factor * trace_factor, 0.0, 1.0))
    return quality_score, float(mean_nis), float(max_trace_p)


# ==============================================================================
# Flight phase assignment
# ==============================================================================

def assign_flight_phases(df: pd.DataFrame, typecode: str) -> pd.DataFrame:
    """
    Labels each 60-second grid waypoint with an OpenAP aerodynamic flight phase.

    Input df is expected to be in aviation units after ProcessXYZZFilterBase.postprocess():
      altitude      — feet
      groundspeed   — knots
      vertical_rate — feet/min
    (i.e., the same units traffic uses internally, before traffic_to_pycontrails converts to SI)

    OpenAP FlightPhase labels: 'GN' ground, 'CL' climb, 'CR' cruise,
    'DE' descent, 'LV' level, 'NA' unclassified.
    Falls back to a simple ROCD threshold scheme if openap is unavailable.
    """
    df_out = df.copy()
    try:
        from openap.phase import FlightPhase
        # Elapsed seconds since flight start (OpenAP requires monotonic time axis)
        ts      = (pd.to_datetime(df_out["timestamp"]) - pd.to_datetime(df_out["timestamp"]).iloc[0]).dt.total_seconds().values
        alt_ft  = df_out["altitude"].values      # ft  — direct from postprocess output
        spd_kts = df_out["groundspeed"].values   # kts — direct from postprocess output
        roc_fpm = df_out["vertical_rate"].values  # ft/min — direct from postprocess output
        fp = FlightPhase()
        fp.set_trajectory(ts, alt_ft, spd_kts, roc_fpm)
        phases = fp.phaselabel()
    except Exception as exc:
        logging.debug(f"OpenAP phase labeling failed for {typecode}: {exc}. Using ROCD fallback.")
        # Simple ROCD fallback: climb > +500 fpm, descent < -500 fpm, else cruise
        roc    = df_out.get("vertical_rate", pd.Series(np.zeros(len(df_out)))).values
        phases = ["CL" if r > 500 else "DE" if r < -500 else "CR" for r in roc]
    df_out["flight_phase"] = phases
    df_out["phase"]        = phases
    return df_out


# ==============================================================================
# Grid injection & LAEA projection — verbatim from working commit c3a21f1
# ==============================================================================

def _fill_geodetic_gaps(df_merged: pd.DataFrame) -> pd.DataFrame:
    """
    Fills lat/lon for injected grid rows that fall within large ADS-B gaps.

    For each pair of consecutive raw ADS-B rows (rows where latitude was not NaN
    before the grid merge), computes the WGS84 geodetic distance.  If this gap
    exceeds GEODESIC_DISTANCE_THRESHOLD_M (100 km), the intervening grid-placeholder
    rows receive linearly-spaced lat/lon points along the great-circle arc via
    pyproj.Geod.npts().  Shorter gaps are left as NaN and handled by the subsequent
    time-interpolation step.
    """
    raw_idxs = df_merged.index[~df_merged["latitude"].isna()].tolist()  # indices of real ADS-B rows
    geod     = Geod(ellps="WGS84")
    for k in range(len(raw_idxs) - 1):
        i_s, i_e = raw_idxs[k], raw_idxs[k + 1]
        if i_e - i_s <= 1:
            continue  # no grid rows between these two real rows
        lon1, lat1 = df_merged.loc[i_s, "longitude"], df_merged.loc[i_s, "latitude"]
        lon2, lat2 = df_merged.loc[i_e, "longitude"], df_merged.loc[i_e, "latitude"]
        _, _, dist = geod.inv(lon1, lat1, lon2, lat2)   # WGS84 forward azimuth + distance
        mid_idxs   = list(range(i_s + 1, i_e))
        if dist > GEODESIC_DISTANCE_THRESHOLD_M:         # only geodesic-interpolate large gaps
            path = geod.npts(lon1, lat1, lon2, lat2, len(mid_idxs))  # equally-spaced arc points
            for step, gi in enumerate(mid_idxs):
                df_merged.loc[gi, "longitude"] = path[step][0]
                df_merged.loc[gi, "latitude"]  = path[step][1]
    return df_merged


def _prepare_grid_and_project(
    f_data: pd.DataFrame,
    time_grid_seconds: int,
) -> tuple[pd.DataFrame, pd.DatetimeIndex, Transformer]:
    """
    Injects a uniform time grid, fills coordinate gaps, and projects to LAEA Cartesian.
    Verbatim copy of grid-injection block from original working commit c3a21f1.

    Steps
    -----
    1. Generate a uniform DatetimeIndex from floor(t_start) to ceil(t_end) at
       `time_grid_seconds` spacing.  These are the canonical output timestamps.
    2. Concatenate raw ADS-B rows with an empty grid DataFrame, deduplicate on
       timestamp, and sort.  Grid rows have NaN for all measurement columns.
    3. Call _fill_geodetic_gaps() to pre-fill lat/lon for large gaps via great-circle arcs.
    4. Time-interpolate all scalar measurement columns so the EKF receives
       fully dense input (no NaN).  ffill/bfill handles boundary points.
    5. Project (lon, lat) → (x, y) using a per-flight Lambert Azimuthal Equal Area
       projection centred on the flight's mean position.  x and y are in metres.
       Two Transformer objects are built: to_xy (→ Cartesian) and to_lonlat (← Cartesian),
       only to_lonlat is returned for the back-projection step.
    """
    grid_times = pd.date_range(
        start=f_data["timestamp"].min().floor(f"{time_grid_seconds}s"),
        end=f_data["timestamp"].max().ceil(f"{time_grid_seconds}s"),
        freq=f"{time_grid_seconds}s",
    )
    # Merge raw rows with empty grid rows; grid rows inherit NaN measurement columns
    df_merged = (
        pd.concat([f_data, pd.DataFrame({"timestamp": grid_times})])
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    df_merged = _fill_geodetic_gaps(df_merged)  # geodetic arc fill for large gaps

    # Time-based linear interpolation for all scalar measurement columns
    interp_cols = ["latitude", "longitude", "altitude", "groundspeed", "track", "vertical_rate"]
    df_merged = df_merged.set_index("timestamp", drop=False)
    df_merged[interp_cols] = df_merged[interp_cols].interpolate(method="time")
    df_merged[interp_cols] = df_merged[interp_cols].ffill().bfill()  # handle boundary NaNs
    df_merged = df_merged.reset_index(drop=True)

    # Per-flight LAEA Cartesian projection (pyproj)
    mean_lat  = df_merged["latitude"].mean()
    mean_lon  = df_merged["longitude"].mean()
    proj4     = f"+proj=laea +lat_0={mean_lat} +lon_0={mean_lon} +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
    to_xy     = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_proj4(proj4), always_xy=True)
    to_lonlat = Transformer.from_crs(CRS.from_proj4(proj4), CRS.from_epsg(4326), always_xy=True)
    df_merged["x"], df_merged["y"] = to_xy.transform(
        df_merged["longitude"].values, df_merged["latitude"].values
    )
    return df_merged, grid_times, to_lonlat


# ==============================================================================
# Programmatic entry points
# ==============================================================================

def _save_diagnostics(
    diag_out_path: Path,
    measurements: pd.DataFrame,
    e_hist: ArrayFloat,
    S_hist: ArrayFloat,
    P_hist: ArrayFloat,
    metrics: tuple[float, float, float],
) -> str:
    """Persists EKF diagnostic tensors to a compressed .npz archive, returns relative path."""
    diag_out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        diag_out_path,
        timestamps=measurements.index.astype(np.int64) // 10 ** 9,
        e_k=e_hist, S_k=S_hist, P_k=P_hist,
        metrics=np.array(metrics, dtype=np.float64),
    )
    return diag_out_path.resolve().relative_to(BASE_DIR).as_posix()


def _finalize_resampled_flight(
    df_resampled: pd.DataFrame,
    f_air_data: pd.DataFrame,
    flight_id: str,
    typecode: str,
) -> Any | None:
    """Re-injects metadata, sets onground=False, assigns phases, converts to pycontrails."""
    meta_cols = ["icao24", "callsign", "typecode", "estdepartureairport", "estarrivalairport", "firstseen", "lastseen"]
    for col in meta_cols:
        if col in f_air_data.columns:
            df_resampled[col] = f_air_data[col].iloc[0]
    df_resampled["flight_id"] = flight_id
    df_resampled["onground"]  = False
    df_phased = assign_flight_phases(df_resampled, typecode=typecode)
    return traffic_to_pycontrails(df_phased, typecode=typecode)


def clean_traffic_flight(
    traffic_flight: Flight,
    flight_id: str,
    typecode: str,
    time_grid_seconds: int = CORRIDOR_TIME_GRID_SECONDS,
    save_diagnostics: bool = False,
    diag_out_path: Path | None = None,
) -> tuple[Any | None, float, float, float, str | None]:
    """
    Primary cleaning entry point — accepts a traffic.core.Flight in aviation units.
    Suitable for in-process injection from any module already holding a traffic Flight
    (e.g., a post-fetch processing stage that avoids a disk round-trip).

    Rule 11 typecode validation must be performed by the caller (clean_pycontrails_flight
    does this; direct callers must call is_supported_typecode() themselves).

    Inter-Process / External Dataflow Synchronization Contract:
    -----------------------------------------------------------
    This function is completely decoupled from global registry mutations. It performs pure
    in-memory trajectory cleaning and metric computation without writing to disk registries.
    If external dataflows or custom orchestrators invoke this function across multiple
    ProcessPoolExecutor worker processes, the caller is strictly responsible for collecting
    the returned metrics and invoking `update_global_registry(new_entries)` in the main process
    after the worker pool finishes (or invoking a rebuild of `GLOBAL_CLEAN_REGISTRY`).

    Returns
    -------
    pc_out          : pycontrails.Flight | None  — cleaned flight in SI units, or None on failure
    q_score         : float                       — ekf_quality_score in [0, 1]
    mean_nis        : float                       — mean Normalized Innovation Squared
    max_trace_p     : float                       — max trace(P_{k|k})
    diag_saved_path : str | None                  — relative path to .npz, or None
    """
    required = ["timestamp", "latitude", "longitude", "track", "groundspeed", "vertical_rate", "altitude", "onground"]
    # Drop rows missing any column the EKF depends on (NaN-safe pre-filter)
    f_data   = traffic_flight.data.dropna(subset=[c for c in required if c in traffic_flight.data.columns])
    if len(f_data) < 10:
        return None, 0.0, 0.0, 0.0, None
    f_air = Flight(f_data).airborne()   # mask: keep only airborne segments
    if f_air is None or len(f_air) < 10:
        return None, 0.0, 0.0, 0.0, None

    # Grid injection + geodetic gap fill + LAEA projection
    df_merged, grid_times, to_lonlat = _prepare_grid_and_project(f_air.data.copy(), time_grid_seconds)
    # Convert aviation units → EKF state space [x, y, alt_baro, math_angle, velocity, vert_rate]
    preprocessor  = ProcessXYZZFilterBase()
    measurements  = preprocessor.preprocess(df_merged.set_index("timestamp", drop=False))

    # EKF forward pass + RTS smoother; collect diagnostic tensors
    smoothed_states, S_hist, P_hist, e_hist = run_6d_kinematic_ekf(measurements)
    q_score, mean_nis, max_trace_p = compute_ekf_quality_metrics(S_hist, P_hist, e_hist)

    diag_saved_path = None
    if save_diagnostics and diag_out_path is not None:
        diag_saved_path = _save_diagnostics(
            Path(diag_out_path), measurements, e_hist, S_hist, P_hist,
            (q_score, mean_nis, max_trace_p),
        )

    # Postprocess: EKF state space → aviation units; use .values to avoid pandas index mismatch
    post_dict = preprocessor.postprocess(smoothed_states)
    df_out    = df_merged.copy()
    for col, val in post_dict.items():
        df_out[col] = val.values if hasattr(val, "values") else np.asarray(val)
    # Back-project smoothed (x, y) → (lat, lon) via inverse LAEA transformer
    lon_v, lat_v        = to_lonlat.transform(smoothed_states["x"].values, smoothed_states["y"].values)
    df_out["latitude"]  = lat_v
    df_out["longitude"] = lon_v

    # Slice to exact canonical grid timestamps (equidistant output)
    df_resampled = df_out[df_out["timestamp"].isin(grid_times)].copy().reset_index(drop=True)
    if df_resampled.empty:
        return None, 0.0, 0.0, 0.0, None

    pc_out = _finalize_resampled_flight(df_resampled, f_air.data, flight_id, typecode)
    return pc_out, q_score, mean_nis, max_trace_p, diag_saved_path


def clean_pycontrails_flight(
    pyc_flight: Any,
    flight_id: str,
    typecode: str,
    time_grid_seconds: int = CORRIDOR_TIME_GRID_SECONDS,
    save_diagnostics: bool = False,
    diag_out_path: Path | None = None,
) -> tuple[Any | None, float, float, float, str | None]:
    """
    Programmatic entry point for a pycontrails.Flight object.
    Suitable for in-process injection from the fetcher or any other pipeline stage
    that holds a pycontrails flight without writing to disk first.
    Enforces Rule 11: typecode is validated before any processing begins.

    Inter-Process / External Dataflow Synchronization Contract:
    -----------------------------------------------------------
    This function never mutates `GLOBAL_CLEAN_REGISTRY` or holds file locks. If invoked by an
    external dataflow inside a multiprocessing worker pool, the external orchestrator must collect
    returned flight entries and call `update_global_registry(new_entries)` in the main process.
    """
    if not is_supported_typecode(typecode):
        log_skipped_aircraft(flight_id, str(typecode), "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode")
        return None, 0.0, 0.0, 0.0, None
    traffic_flight = pycontrails_to_traffic(pyc_flight)
    if traffic_flight is None:
        return None, 0.0, 0.0, 0.0, None
    return clean_traffic_flight(
        traffic_flight, flight_id, typecode, time_grid_seconds, save_diagnostics, diag_out_path
    )


# ==============================================================================
# Batch file worker
# ==============================================================================

def _process_single_raw_file(
    raw_file: Path,
    out_dir_path: Path,
    save_diagnostics: bool,
    overwrite: bool,
    target_flight_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Loads one raw Parquet, cleans each flight via clean_pycontrails_flight, writes output.

    Inter-Process / External Dataflow Synchronization Contract:
    -----------------------------------------------------------
    This worker function is explicitly safe for concurrent execution across child processes in a
    `ProcessPoolExecutor`. It writes clean SI trajectory Parquet files to `out_dir_path` but **never**
    reads, modifies, or locks the shared `GLOBAL_CLEAN_REGISTRY`. Instead, it returns a list of
    dictionary entries representing clean flights processed in this file.
    When external dataflows or custom pipelines invoke `_process_single_raw_file` across worker
    pools, the parent orchestrator must aggregate these returned dictionaries and call
    `update_global_registry(GLOBAL_CLEAN_REGISTRY, all_entries)` exactly once after all workers finish.
    """
    out_path = out_dir_path / raw_file.name.replace("_raw.parquet", "_clean_si.parquet")
    if out_path.exists() and not overwrite:
        logging.info(f"Already exists, skipping: {out_path.name}")
        return []
    try:
        flights_dict = parquet_to_pycontrails(str(raw_file))
    except Exception as exc:
        logging.error(f"Failed to read {raw_file.name}: {exc}")
        return []

    pc_flights:       list[Any]            = []
    registry_entries: list[dict[str, Any]] = []

    for fid, pyc_flight in flights_dict.items():
        if target_flight_ids is not None and fid not in target_flight_ids:
            continue
        t_code = pyc_flight.attrs.get("aircraft_type", None)
        if not is_supported_typecode(t_code):
            log_skipped_aircraft(fid, t_code, "ERROR_FLAG: Missing, NaN, or non-target family aircraft typecode in raw parquet")
            continue
        diag_path = (out_dir_path / "diagnostics" / f"{fid}_ekf_diag.npz") if save_diagnostics else None
        pc_out, q, nis, tr, diag_rel = clean_pycontrails_flight(
            pyc_flight, fid, t_code,
            time_grid_seconds=CORRIDOR_TIME_GRID_SECONDS,
            save_diagnostics=save_diagnostics,
            diag_out_path=diag_path,
        )
        if pc_out is None:
            continue
        pc_flights.append(pc_out)
        registry_entries.append({
            "flight_id":       fid,
            "file_path":       out_path.resolve().relative_to(BASE_DIR).as_posix(),
            "ekf_quality_score": q,
            "ekf_mean_nis":    nis,
            "ekf_max_trace_p": tr,
            "diag_file_path":  diag_rel,
        })

    if pc_flights:
        write_flights_to_parquet(pc_flights, out_path)
        logging.info(f"Wrote {len(pc_flights)} clean flights → {out_path.name}")
    return registry_entries


# ==============================================================================
# Batch orchestrator
# ==============================================================================

def process_trajectories_by_route_ranks(
    ranks: list[int] | None = None,
    rank_range: tuple[int, int] | None = None,
    routes: list[str] | None = None,
    source_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
    save_diagnostics: bool = False,
    overwrite: bool = False,
    max_workers: int | None = None,
) -> None:
    """Resolves target corridors by rank/route, processes all raw Parquet files (in parallel), updates registry."""
    all_entries: list[dict[str, Any]] = []
    tasks_to_run: list[tuple[Path, Path, bool, bool, set[str] | None]] = []

    if source_dir is not None:
        s_dir = Path(source_dir)
        if not s_dir.exists():
            logging.warning(f"Specified source_dir does not exist: {s_dir}")
            return
        o_dir = Path(out_dir) if out_dir else (s_dir.parent / "clean" if s_dir.name == "raw" else s_dir)
        o_dir.mkdir(parents=True, exist_ok=True)
        for rf in s_dir.glob("*_raw.parquet"):
            tasks_to_run.append((rf, o_dir, save_diagnostics, overwrite, None))
    else:
        from src.common.registry_utils import get_flights_for_route

        # Gather target (dep, arr) pairs to process
        target_corridors: list[tuple[str, str]] = []
        if routes:
            for r in routes:
                if "-" in r:
                    dep, arr = r.split("-", 1)
                    target_corridors.append((dep.strip().upper(), arr.strip().upper()))
                else:
                    logging.warning(f"Skipping malformed route format: {r} (expected DEP-ARR)")

        if ranks or rank_range:
            routes_df = extract_target_routes(
                specific_ranks=ranks,
                lower=rank_range[0] if rank_range else None,
                upper=rank_range[1] if rank_range else None,
            )
            for _, row in routes_df.iterrows():
                target_corridors.append((row["dep"], row["arr"]))

        if not target_corridors:
            logging.warning("No target corridors resolved by ranks/routes.")
            return

        # Query registry for flight ids/paths matching resolved corridors
        matching_dfs = []
        for dep, arr in target_corridors:
            matching_dfs.append(get_flights_for_route(dep, arr))

        if not matching_dfs:
            logging.warning("No matching trajectory records resolved.")
            return

        df_target = pd.concat(matching_dfs).drop_duplicates(subset=["flight_id"])
        if df_target.empty:
            logging.warning("No trajectory records found in registry for target corridors.")
            return

        # Group target flight IDs by their raw relative file path
        grouped = df_target.groupby("file_path")["flight_id"].apply(set).to_dict()

        for rel_file_path, fids in grouped.items():
            raw_file = BASE_DIR / rel_file_path
            if not raw_file.exists():
                logging.warning(f"Raw file registered but not found on disk: {raw_file}")
                continue

            # Determine clean output directory
            if out_dir:
                o_dir = Path(out_dir)
            else:
                o_dir = raw_file.parent.parent / "clean" if raw_file.parent.name == "raw" else raw_file.parent
            o_dir.mkdir(parents=True, exist_ok=True)
            tasks_to_run.append((raw_file, o_dir, save_diagnostics, overwrite, fids))

    if not tasks_to_run:
        logging.warning("No raw trajectory files to process.")
        return

    effective_workers = max_workers if max_workers is not None else (os.cpu_count() or 1)
    if effective_workers <= 1:
        logging.info(f"Processing {len(tasks_to_run)} files sequentially (max_workers={effective_workers})...")
        for rf, odir, sdiag, ow, tfids in tasks_to_run:
            all_entries.extend(_process_single_raw_file(rf, odir, sdiag, ow, tfids))
    else:
        logging.info(f"Processing {len(tasks_to_run)} files across {effective_workers} parallel workers...")
        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            futures = [
                executor.submit(_process_single_raw_file, rf, odir, sdiag, ow, tfids)
                for rf, odir, sdiag, ow, tfids in tasks_to_run
            ]
            for fut in as_completed(futures):
                try:
                    entries = fut.result()
                    all_entries.extend(entries)
                except Exception as exc:
                    logging.error(f"Worker process failed: {exc}")

    if all_entries:
        update_global_registry(GLOBAL_CLEAN_REGISTRY, all_entries)
        logging.info(f"Registry updated with {len(all_entries)} new clean flight entries.")


# ==============================================================================
# CLI entry point
# ==============================================================================

def main() -> None:
    setup_file_logger(log_filename="processing.log")
    parser = argparse.ArgumentParser(description="6D Kinematic EKF trajectory cleaning pipeline.")
    parser.add_argument("--ranks",            type=int, nargs="+", help="Route volume ranks to process.")
    parser.add_argument("--rank-range",       type=int, nargs=2,   help="Inclusive rank range (e.g. 1 10).")
    parser.add_argument("--routes",           type=str, nargs="+", help="Corridor strings (e.g. EDDF-LIRF).")
    parser.add_argument("--source-dir",       type=str,            help="Direct path to raw Parquet directory.")
    parser.add_argument("--out-dir",          type=str,            help="Output directory for clean Parquet files.")
    parser.add_argument("--save-diagnostics", action="store_true", help="Save S_k, P_k, e_k tensors to .npz per flight.")
    parser.add_argument("--overwrite",        action="store_true", help="Re-process and overwrite existing clean files.")
    parser.add_argument("--workers", "--num-workers", "--max-workers", dest="max_workers", type=int, default=None, help="Maximum number of parallel worker processes to spawn.")
    args = parser.parse_args()

    rank_range_tuple = tuple(args.rank_range) if args.rank_range else None
    process_trajectories_by_route_ranks(
        ranks=args.ranks,
        rank_range=rank_range_tuple,
        routes=args.routes,
        source_dir=args.source_dir,
        out_dir=args.out_dir,
        save_diagnostics=args.save_diagnostics,
        overwrite=args.overwrite,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()