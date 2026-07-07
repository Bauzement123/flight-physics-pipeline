"""
phase_quality_plots.py — Component 2 (Part A) of Phase Quality Filter Campaign.

Provides modular plotting and PDF compilation functions for visual audit reports.
Renders Cartopy ground tracks and time-normalized [0, 1] vertical profiles
color-coded by flight_phase (GND, CL, CR, DE, LVL) using fast LineCollection rendering.

Includes explicit NA phase label statistics tracking, legend entries, and coordinate cleaning.
"""

import logging
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection
import matplotlib.lines as mlines
import numpy as np
import pandas as pd
import cartopy.crs as ccrs

from src.common.config import M_TO_FT, BASE_DIR
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)

PHASE_COLORS = {
    "GND": "#7f7f7f",  # Gray
    1: "#7f7f7f",
    "CL": "#1f77b4",   # Blue
    2: "#1f77b4",
    "CR": "#2ca02c",   # Green
    4: "#2ca02c",
    "DE": "#ff7f0e",   # Orange
    3: "#ff7f0e",
    "LVL": "#9467bd",  # Purple
    5: "#9467bd",
}
DEFAULT_COLOR = "#bcbd22"  # Olive / Yellow-Green for NA or unknown phase

# Hardcoded DPI for PDF rasterization and rendering
DEFAULT_DPI = 150


def _get_phase_color(val: Any) -> str:
    """Maps a flight_phase value (string or int) to a hex color."""
    if pd.isna(val) or val is None:
        return DEFAULT_COLOR
    val_str = str(val).strip().upper()
    if val_str in PHASE_COLORS:
        return PHASE_COLORS[val_str]
    try:
        val_int = int(float(val))
        if val_int in PHASE_COLORS:
            return PHASE_COLORS[val_int]
    except (ValueError, TypeError):
        pass
    return DEFAULT_COLOR


def plot_cohort_audit_page(
    route_id: str,
    cohort_idx: int,
    candidate_flight_ids: list[str],
    trajectories: dict[str, pd.DataFrame],
    eval_records: dict[str, dict] | None = None,
    show_rejected: bool = False,
    crop_padding: float = 1.5,
    plot_format: str = "SVG",
) -> tuple[plt.Figure, dict[str, int]]:
    """
    Renders a single cohort audit page (Cartopy ground track + time-normalized [0, 1]
    vertical profile) for the fixed candidate flights assigned to this cohort.
    
    Surviving trajectories are color-coded by flight_phase.
    Returns: (fig, stats_dict)
    """
    fig = plt.figure(figsize=(12, 5.2))
    ax1 = fig.add_subplot(1, 2, 1, projection=ccrs.PlateCarree())
    ax2 = fig.add_subplot(1, 2, 2)

    if plot_format.upper() == "PNG":
        ax1.set_rasterization_zorder(10)
        ax2.set_rasterization_zorder(10)

    # 1. Add cached European basemap features
    map_cache = EuropeanMapCache().initialize()
    map_cache.add_features_to_axes(ax1)

    # 2. Add background airport markers
    if not map_cache.airports_df.empty:
        ax1.scatter(
            map_cache.airports_df["lon"],
            map_cache.airports_df["lat"],
            color="#6c757d",
            s=12,
            alpha=0.3,
            edgecolors="none",
            transform=ccrs.PlateCarree(),
            zorder=2,
        )

    # Track bounding boxes and statistics
    min_lon, max_lon = 180.0, -180.0
    min_lat, max_lat = 90.0, -90.0
    min_alt, max_alt = 1e9, -1e9
    plotted_count = 0
    rejected_count = 0
    prefilter_rejects = 0
    postfilter_rejects = 0

    total_points = 0
    na_points = 0
    flights_with_na = 0

    # 3. Iterate over the fixed candidate flights for this cohort
    for fid in candidate_flight_ids:
        status = "PASSED"
        fail_stage = "NONE"
        if eval_records and fid in eval_records:
            status = eval_records[fid].get("status", "PASSED")
            fail_stage = eval_records[fid].get("fail_stage", "NONE")

        if status == "REJECTED":
            rejected_count += 1
            if fail_stage == "PREFILTER":
                prefilter_rejects += 1
            elif fail_stage == "POSTFILTER":
                postfilter_rejects += 1

            if not show_rejected or fid not in trajectories:
                continue

        df_fl = trajectories.get(fid)
        if df_fl is None or df_fl.empty:
            continue

        # Extract coordinates
        lon_col = "lon" if "lon" in df_fl.columns else "longitude"
        lat_col = "lat" if "lat" in df_fl.columns else "latitude"
        alt_col = "baroaltitude" if "baroaltitude" in df_fl.columns else (
            "geoaltitude" if "geoaltitude" in df_fl.columns else "altitude"
        )
        time_col = "time" if "time" in df_fl.columns else "timestamp"

        if not all(col in df_fl.columns for col in [lon_col, lat_col, alt_col]):
            continue

        df_fl = df_fl.sort_values(by=time_col) if time_col in df_fl.columns else df_fl
        lons = df_fl[lon_col].values.astype(float)
        lats = df_fl[lat_col].values.astype(float)
        alts = df_fl[alt_col].values.astype(float)

        # Coordinate cleaning: filter out NaN or Inf coordinates to prevent Shapely warnings
        valid_mask = ~(np.isnan(lons) | np.isnan(lats) | np.isinf(lons) | np.isinf(lats) | np.isnan(alts))
        if not np.any(valid_mask):
            continue

        lons = lons[valid_mask]
        lats = lats[valid_mask]
        alts = alts[valid_mask]

        # Note: Altitude is NOT normalized! It remains in real physical altitude (feet).

        # Adopt corridor model time normalization (pca_compressor.py::vectorize_flight)
        if time_col in df_fl.columns:
            ts_series = pd.to_datetime(df_fl[time_col])
            ts = (ts_series - ts_series.iloc[0]).dt.total_seconds().values.astype(float)[valid_mask]
            if len(ts) > 1 and ts[-1] > 0:
                t_norm = ts / ts[-1]
            else:
                t_norm = np.linspace(0.0, 1.0, len(ts))
        else:
            t_norm = np.linspace(0.0, 1.0, len(lons))

        # Track NA phase statistics
        phases = df_fl["flight_phase"].values[valid_mask] if "flight_phase" in df_fl.columns else [None] * len(lons)
        colors = [_get_phase_color(p) for p in phases]

        fl_pts = len(phases)
        fl_na = sum(1 for p in phases if pd.isna(p) or p is None or _get_phase_color(p) == DEFAULT_COLOR)
        total_points += fl_pts
        na_points += fl_na
        if fl_na > 0:
            flights_with_na += 1

        # Update bounding box
        min_lon = min(min_lon, np.nanmin(lons))
        max_lon = max(max_lon, np.nanmax(lons))
        min_lat = min(min_lat, np.nanmin(lats))
        max_lat = max(max_lat, np.nanmax(lats))
        min_alt = min(min_alt, np.nanmin(alts))
        max_alt = max(max_alt, np.nanmax(alts))

        if status == "REJECTED" and show_rejected:
            # Plot rejected flights as faint dashed gray line
            ax1.plot(lons, lats, color="#a0a0a0", linestyle="--", linewidth=0.8, alpha=0.4, zorder=3, transform=ccrs.PlateCarree())
            ax2.plot(t_norm, alts, color="#a0a0a0", linestyle="--", linewidth=0.8, alpha=0.4, zorder=3)
        else:
            plotted_count += 1
            # Build fast LineCollections for ground track
            pts_xy = np.array([lons, lats]).T.reshape(-1, 1, 2)
            segs_xy = np.concatenate([pts_xy[:-1], pts_xy[1:]], axis=1)
            lc_xy = LineCollection(segs_xy, colors=colors[:-1], linewidths=1.2, alpha=0.75, zorder=4, transform=ccrs.PlateCarree())
            ax1.add_collection(lc_xy)

            # Build LineCollections for vertical profile
            pts_zt = np.array([t_norm, alts]).T.reshape(-1, 1, 2)
            segs_zt = np.concatenate([pts_zt[:-1], pts_zt[1:]], axis=1)
            lc_zt = LineCollection(segs_zt, colors=colors[:-1], linewidths=1.2, alpha=0.75, zorder=4)
            ax2.add_collection(lc_zt)

    # 4. Auto-crop extent to airports + padding
    parts = route_id.split("-")
    if len(parts) >= 2 and not map_cache.airports_df.empty:
        dep_icao, arr_icao = parts[0].strip().upper(), parts[-1].strip().upper()
        dep_arr_df = map_cache.airports_df[
            map_cache.airports_df["icao"].str.upper().isin([dep_icao, arr_icao])
        ]
        if not dep_arr_df.empty and len(dep_arr_df) >= 1:
            min_lon = min(min_lon, dep_arr_df["lon"].min())
            max_lon = max(max_lon, dep_arr_df["lon"].max())
            min_lat = min(min_lat, dep_arr_df["lat"].min())
            max_lat = max(max_lat, dep_arr_df["lat"].max())

    if min_lon < max_lon and min_lat < max_lat:
        ax1.set_extent(
            [min_lon - crop_padding, max_lon + crop_padding, min_lat - crop_padding, max_lat + crop_padding],
            crs=ccrs.PlateCarree(),
        )

    # 5. Gridlines and formatting
    try:
        from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
        gl = ax1.gridlines(draw_labels=True, linestyle="--", color="dimgray", linewidth=0.6, alpha=0.5)
        gl.top_labels = False
        gl.right_labels = False
        gl.xformatter = LONGITUDE_FORMATTER
        gl.yformatter = LATITUDE_FORMATTER
        gl.xlabel_style = {"size": 8}
        gl.ylabel_style = {"size": 8}
    except Exception as e:
        logger.debug(f"Gridline error: {e}")
        ax1.grid(True, linestyle="--", alpha=0.5)

    # Title summary header with NA Statistics
    na_pct = (na_points / total_points * 100.0) if total_points > 0 else 0.0
    summary_str = f"Route: {route_id} | Cohort {cohort_idx} ({len(candidate_flight_ids)} Candidates)\n"
    if eval_records:
        summary_str += f"Plotted: {plotted_count} | Dropped: {rejected_count} (Pre: {prefilter_rejects}, Post: {postfilter_rejects}) | "
    else:
        summary_str += f"Baseline Audit: Displaying all {plotted_count} trajectories | "
    summary_str += f"Phase NA: {na_points:,}/{total_points:,} pts ({na_pct:.1f}%) in {flights_with_na} flights"

    fig.suptitle(summary_str, fontsize=10.5, fontweight="bold", y=0.98)

    ax1.set_title("Ground Track", fontsize=10)
    ax2.set_title("Vertical Profile", fontsize=10)
    ax2.set_xlabel("Normalized Time Point [0, 1]", fontsize=9)
    ax2.set_ylabel("Altitude (ft)", fontsize=9)
    ax2.grid(True, linestyle="--", alpha=0.5)

    # Explicitly set limits since add_collection does not autoscale
    ax2.set_xlim(-0.02, 1.02)
    if min_alt < max_alt and max_alt > -1e8:
        ax2.set_ylim(max(0.0, min_alt - 1000.0), max_alt + 2500.0)
    else:
        ax2.set_ylim(0.0, 45000.0)

    # Add phase color legend including NA entry
    legend_handles = [
        mlines.Line2D([], [], color=PHASE_COLORS["GND"], label="GND (Ground)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["CL"], label="CL (Climb)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["CR"], label="CR (Cruise)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["DE"], label="DE (Descent)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["LVL"], label="LVL (Level)", linewidth=2),
        mlines.Line2D([], [], color=DEFAULT_COLOR, label="NA / Unlabeled", linewidth=2),
    ]
    ax2.legend(handles=legend_handles, loc="upper right", fontsize=7.5, framealpha=0.85)

    fig.tight_layout(rect=[0, 0, 1, 0.93])

    stats = {
        "plotted": plotted_count,
        "rejected": rejected_count,
        "prefilter": prefilter_rejects,
        "postfilter": postfilter_rejects,
        "total_points": total_points,
        "na_points": na_points,
        "flights_with_na": flights_with_na,
    }
    return fig, stats


def compile_route_audit_pdf(
    route_id: str,
    cohort_map_df: pd.DataFrame,
    trajectories: dict[str, pd.DataFrame],
    out_pdf_path: Path,
    eval_df: pd.DataFrame | None = None,
    show_rejected: bool = False,
    plot_format: str = "SVG",
) -> Path:
    """
    Compiles a multi-page PDF report for a given route by iterating through
    all cohorts (1 through 10) in the cohort map. Logs detailed NA statistics.
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    df_route_map = cohort_map_df[cohort_map_df["route_id"] == route_id].copy()
    if df_route_map.empty:
        logger.warning(f"No cohort mapping found for route {route_id}")
        return out_pdf_path

    eval_records = None
    if eval_df is not None and not eval_df.empty:
        df_eval_route = eval_df[eval_df["route_id"] == route_id]
        eval_records = df_eval_route.set_index("flight_id").to_dict(orient="index")

    cohorts = sorted(df_route_map["cohort_idx"].unique())
    logger.info(f"Compiling {len(cohorts)}-page PDF audit report ({plot_format.upper()} mode) for {route_id} -> {out_pdf_path.name}...")

    route_total_pts = 0
    route_na_pts = 0
    route_na_flights = 0

    with PdfPages(out_pdf_path) as pdf:
        for c_idx in cohorts:
            c_fids = df_route_map[df_route_map["cohort_idx"] == c_idx]["flight_id"].tolist()
            fig, stats = plot_cohort_audit_page(
                route_id=route_id,
                cohort_idx=c_idx,
                candidate_flight_ids=c_fids,
                trajectories=trajectories,
                eval_records=eval_records,
                show_rejected=show_rejected,
                plot_format=plot_format,
            )
            pdf.savefig(fig, dpi=DEFAULT_DPI, bbox_inches="tight")
            plt.close(fig)

            route_total_pts += stats["total_points"]
            route_na_pts += stats["na_points"]
            route_na_flights += stats["flights_with_na"]

            na_pct = (stats["na_points"] / stats["total_points"] * 100.0) if stats["total_points"] > 0 else 0.0
            logger.info(
                f"   -> Cohort {c_idx:2d}: Rendered {stats['plotted']:2d} flights | "
                f"Phase NA: {stats['na_points']:6d}/{stats['total_points']:6d} pts ({na_pct:4.1f}%) across {stats['flights_with_na']:2d} flights."
            )

    route_na_pct = (route_na_pts / route_total_pts * 100.0) if route_total_pts > 0 else 0.0
    logger.info(
        f"[{route_id} Summary] Total Trajectory Points: {route_total_pts:,} | "
        f"Total Phase NA: {route_na_pts:,} ({route_na_pct:.1f}%) across {route_na_flights} flights."
    )
    logger.info(f"Successfully generated {out_pdf_path}")
    return out_pdf_path
