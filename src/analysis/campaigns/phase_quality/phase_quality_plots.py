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
REJECTED_COLOR = "#ff0000" # Red for rejected trajectories

# Hardcoded DPI for PDF rasterization and rendering
DEFAULT_DPI = 300


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


def _extract_valid_coords_and_time(df_fl: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Extracts cleaned coordinates (lons, lats, alts, t_norm, phases) from a trajectory DataFrame."""
    lon_col = "lon" if "lon" in df_fl.columns else "longitude"
    lat_col = "lat" if "lat" in df_fl.columns else "latitude"
    alt_col = "baroaltitude" if "baroaltitude" in df_fl.columns else ("geoaltitude" if "geoaltitude" in df_fl.columns else "altitude")
    time_col = "time" if "time" in df_fl.columns else "timestamp"

    if not all(col in df_fl.columns for col in [lon_col, lat_col, alt_col]):
        return None

    df_fl = df_fl.sort_values(by=time_col) if time_col in df_fl.columns else df_fl
    lons, lats, alts = df_fl[lon_col].values.astype(float), df_fl[lat_col].values.astype(float), df_fl[alt_col].values.astype(float)
    valid_mask = ~(np.isnan(lons) | np.isnan(lats) | np.isinf(lons) | np.isinf(lats) | np.isnan(alts))
    if not np.any(valid_mask):
        return None

    lons, lats, alts = lons[valid_mask], lats[valid_mask], alts[valid_mask]
    if time_col in df_fl.columns:
        ts_series = pd.to_datetime(df_fl[time_col])
        ts = (ts_series - ts_series.iloc[0]).dt.total_seconds().values.astype(float)[valid_mask]
        t_norm = (ts / ts[-1]) if (len(ts) > 1 and ts[-1] > 0) else np.linspace(0.0, 1.0, len(ts))
    else:
        t_norm = np.linspace(0.0, 1.0, len(lons))

    phases = df_fl["flight_phase"].values[valid_mask] if "flight_phase" in df_fl.columns else [None] * len(lons)
    return lons, lats, alts, t_norm, phases


def _render_trajectory_pair_on_axes(
    ax_map: plt.Axes,
    ax_prof: plt.Axes,
    candidate_flight_ids: list[str],
    trajectories: dict[str, pd.DataFrame],
    eval_records: dict[str, dict] | None,
    show_rejected: bool,
    map_cache: EuropeanMapCache,
    route_id: str,
    crop_padding: float,
    label_prefix: str = "Raw",
) -> dict[str, Any]:
    """Helper to render ground track and vertical profile onto given axes."""
    min_lon, max_lon, min_lat, max_lat, min_alt, max_alt = 180.0, -180.0, 90.0, -90.0, 1e9, -1e9
    stats = {"plotted": 0, "rejected": 0, "prefilter": 0, "postfilter": 0, "total_points": 0, "na_points": 0, "flights_with_na": 0}

    if not map_cache.airports_df.empty:
        ax_map.scatter(map_cache.airports_df["lon"], map_cache.airports_df["lat"], color="#6c757d", s=12, alpha=0.3, edgecolors="none", transform=ccrs.PlateCarree(), zorder=2)

    for fid in candidate_flight_ids:
        status = eval_records[fid].get("status", "PASSED") if eval_records and fid in eval_records else "PASSED"
        fail_stage = eval_records[fid].get("fail_stage", "NONE") if eval_records and fid in eval_records else "NONE"
        if status == "REJECTED":
            stats["rejected"] += 1
            stats["prefilter" if fail_stage == "PREFILTER" else "postfilter"] += (1 if fail_stage in ["PREFILTER", "POSTFILTER"] else 0)
            if not show_rejected or fid not in trajectories:
                continue

        df_fl = trajectories.get(fid)
        if df_fl is None or df_fl.empty:
            continue
        coords = _extract_valid_coords_and_time(df_fl)
        if not coords:
            continue
        lons, lats, alts, t_norm, phases = coords
        colors = [_get_phase_color(p) for p in phases]

        fl_na = sum(1 for p in phases if pd.isna(p) or p is None or _get_phase_color(p) == DEFAULT_COLOR)
        stats["total_points"] += len(phases)
        stats["na_points"] += fl_na
        stats["flights_with_na"] += 1 if fl_na > 0 else 0

        min_lon, max_lon = min(min_lon, np.nanmin(lons)), max(max_lon, np.nanmax(lons))
        min_lat, max_lat = min(min_lat, np.nanmin(lats)), max(max_lat, np.nanmax(lats))
        min_alt, max_alt = min(min_alt, np.nanmin(alts)), max(max_alt, np.nanmax(alts))

        if status == "REJECTED" and show_rejected:
            ax_map.plot(lons, lats, color=REJECTED_COLOR, linestyle="--", linewidth=0.8, alpha=0.4, zorder=3, transform=ccrs.PlateCarree())
            ax_prof.plot(t_norm, alts, color=REJECTED_COLOR, linestyle="--", linewidth=0.8, alpha=0.4, zorder=3)
        else:
            stats["plotted"] += 1
            pts_xy = np.array([lons, lats]).T.reshape(-1, 1, 2)
            ax_map.add_collection(LineCollection(np.concatenate([pts_xy[:-1], pts_xy[1:]], axis=1), colors=colors[:-1], linewidths=1.2, alpha=0.75, zorder=4, transform=ccrs.PlateCarree()))
            pts_zt = np.array([t_norm, alts]).T.reshape(-1, 1, 2)
            ax_prof.add_collection(LineCollection(np.concatenate([pts_zt[:-1], pts_zt[1:]], axis=1), colors=colors[:-1], linewidths=1.2, alpha=0.75, zorder=4))

    _format_pair_axes(ax_map, ax_prof, map_cache, route_id, crop_padding, min_lon, max_lon, min_lat, max_lat, min_alt, max_alt, label_prefix)
    return stats


def _format_pair_axes(ax_map, ax_prof, map_cache, route_id, crop_padding, min_lon, max_lon, min_lat, max_lat, min_alt, max_alt, label_prefix):
    """Formats gridlines, extent, labels, and limits for a subplot pair."""
    parts = route_id.split("-")
    if len(parts) >= 2 and not map_cache.airports_df.empty:
        dep_arr_df = map_cache.airports_df[map_cache.airports_df["icao"].str.upper().isin([parts[0].strip().upper(), parts[-1].strip().upper()])]
        if not dep_arr_df.empty:
            min_lon, max_lon = min(min_lon, dep_arr_df["lon"].min()), max(max_lon, dep_arr_df["lon"].max())
            min_lat, max_lat = min(min_lat, dep_arr_df["lat"].min()), max(max_lat, dep_arr_df["lat"].max())

    if min_lon < max_lon and min_lat < max_lat:
        ax_map.set_extent([min_lon - crop_padding, max_lon + crop_padding, min_lat - crop_padding, max_lat + crop_padding], crs=ccrs.PlateCarree())

    try:
        from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
        gl = ax_map.gridlines(draw_labels=True, linestyle="--", color="dimgray", linewidth=0.6, alpha=0.5)
        gl.top_labels = gl.right_labels = False
        gl.xformatter, gl.yformatter = LONGITUDE_FORMATTER, LATITUDE_FORMATTER
        gl.xlabel_style = gl.ylabel_style = {"size": 8}
    except Exception:
        ax_map.grid(True, linestyle="--", alpha=0.5)

    ax_map.set_title(f"{label_prefix} Ground Track", fontsize=10)
    ax_prof.set_title(f"{label_prefix} Vertical Profile", fontsize=10)
    ax_prof.set_xlabel("Normalized Time Point [0, 1]", fontsize=9)
    ax_prof.set_ylabel("Altitude (ft)", fontsize=9)
    ax_prof.grid(True, linestyle="--", alpha=0.5)
    ax_prof.set_xlim(-0.02, 1.02)
    ax_prof.set_ylim(max(0.0, min_alt - 1000.0), max_alt + 2500.0 if (min_alt < max_alt and max_alt > -1e8) else 45000.0)


def plot_cohort_audit_page(
    route_id: str,
    cohort_idx: int,
    candidate_flight_ids: list[str],
    trajectories: dict[str, pd.DataFrame],
    eval_records: dict[str, dict] | None = None,
    show_rejected: bool = True,
    crop_padding: float = 1.5,
    plot_format: str = "SVG",
    trajectories_clean: dict[str, pd.DataFrame] | None = None,
) -> tuple[plt.Figure, dict[str, int]]:
    """
    Renders a cohort audit page. If trajectories_clean is provided, renders 6 subplots
    in a 3x2 grid:
      - Row 1: Raw + Prefilter (Map left, Profile right)
      - Row 2: Those But Clean (Map left, Profile right, ignoring post-filter rejections)
      - Row 3: Clean + Postfilter (Map left, Profile right, full rejections)
    Otherwise, renders 2 subplots in a 1x2 grid (Map left, Profile right).
    """
    is_three_row = trajectories_clean is not None
    fig = plt.figure(figsize=(13, 15.2 if is_three_row else 5.2))

    map_cache = EuropeanMapCache().initialize()

    # generate prefilter mask
    eval_records_pre = eval_records
    if is_three_row and eval_records:
        eval_records_pre = {}
        for fid, rec in eval_records.items():
            if rec.get("fail_stage") == "POSTFILTER":
                # Note: Using **rec safely preserves other keys while overwriting the status
                eval_records_pre[fid] = {
                    **rec,
                    "status": "PASSED",
                    "fail_stage": "NONE",
                    "reject_reason": "PASSED"
                }
            else:
                eval_records_pre[fid] = rec

    # Row 1: Raw + Prefilter (uses full eval_records)
    ax1 = fig.add_subplot(3 if is_three_row else 1, 2, 1, projection=ccrs.PlateCarree())
    ax2 = fig.add_subplot(3 if is_three_row else 1, 2, 2)

    if plot_format.upper() == "PNG":
        ax1.set_rasterization_zorder(10)
        ax2.set_rasterization_zorder(10)

    map_cache.add_features_to_axes(ax1)

    stats = _render_trajectory_pair_on_axes(
        ax1, ax2, candidate_flight_ids, trajectories, eval_records,
        show_rejected, map_cache, route_id, crop_padding,
        label_prefix="Raw + Prefilter" if is_three_row else ""
    )

    # If there are clean trajectories, render the additional rows
    if is_three_row:
        # -----------------------------------------------------------------
        # Build trajectory dictionaries that contain *all* flights when
        # show_rejected is True.  We keep the original clean dict untouched
        # for the normal (show_rejected=False) path.
        # -----------------------------------------------------------------
        if show_rejected:
            # Row 1 already uses the raw dict (trajectories) – no change needed.
            # For Rows 2 and 3 we need a merged dict that adds any missing raw
            # trajectories (rejected flights) to the clean dict so the renderer
            # can draw them as red dashed lines.
            trajectories_for_row2: dict[str, pd.DataFrame] = trajectories_clean.copy()
            trajectories_for_row3: dict[str, pd.DataFrame] = trajectories_clean.copy()
            for fid, df in trajectories.items():
                trajectories_for_row2.setdefault(fid, df)  # add missing rejected flights
                trajectories_for_row3.setdefault(fid, df)  # same for Row 3
        else:
            trajectories_for_row2 = trajectories_clean
            trajectories_for_row3 = trajectories_clean

        # Row 2: Those But Clean (uses eval_records ignoring POSTFILTER rejections)
        ax3 = fig.add_subplot(3, 2, 3, projection=ccrs.PlateCarree())
        ax4 = fig.add_subplot(3, 2, 4)

        if plot_format.upper() == "PNG":
            ax3.set_rasterization_zorder(10)
            ax4.set_rasterization_zorder(10)

        map_cache.add_features_to_axes(ax3)

        # Uses the merged dict so that rejected flights (absent from the clean
        # registry) are still drawable as red dashed lines when show_rejected=True.
        _render_trajectory_pair_on_axes(
            ax3, ax4, candidate_flight_ids, trajectories_for_row2, eval_records_pre,
            show_rejected, map_cache, route_id, crop_padding, label_prefix="Those But Clean"
        )

        # Row 3: Clean + Postfilter (uses full eval_records)
        ax5 = fig.add_subplot(3, 2, 5, projection=ccrs.PlateCarree())
        ax6 = fig.add_subplot(3, 2, 6)

        if plot_format.upper() == "PNG":
            ax5.set_rasterization_zorder(10)
            ax6.set_rasterization_zorder(10)

        map_cache.add_features_to_axes(ax5)

        # Uses the merged dict so that prefilter-rejected flights appear here too
        # as red dashed lines alongside the clean+postfilter evaluation colours.
        _render_trajectory_pair_on_axes(
            ax5, ax6, candidate_flight_ids, trajectories_for_row3, eval_records,
            show_rejected, map_cache, route_id, crop_padding, label_prefix="Clean + Postfilter"
        )

    na_pct = (stats["na_points"] / stats["total_points"] * 100.0) if stats["total_points"] > 0 else 0.0
    summary_str = f"Route: {route_id} | Cohort {cohort_idx} ({len(candidate_flight_ids)} Candidates)\n"
    summary_str += f"Plotted: {stats['plotted']} | Dropped: {stats['rejected']} (Pre: {stats['prefilter']}, Post: {stats['postfilter']}) | " if eval_records else f"Baseline Audit: Displaying {stats['plotted']} trajectories | "
    summary_str += f"Phase NA: {stats['na_points']:,}/{stats['total_points']:,} pts ({na_pct:.1f}%) in {stats['flights_with_na']} flights"

    fig.suptitle(summary_str, fontsize=10.5, fontweight="bold", y=0.98)
    legend_handles = [
        mlines.Line2D([], [], color=PHASE_COLORS["GND"], label="GND (Ground)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["CL"], label="CL (Climb)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["CR"], label="CR (Cruise)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["DE"], label="DE (Descent)", linewidth=2),
        mlines.Line2D([], [], color=PHASE_COLORS["LVL"], label="LVL (Level)", linewidth=2),
        mlines.Line2D([], [], color=DEFAULT_COLOR, label="NA / Unlabeled", linewidth=2),
    ]

    if show_rejected:
        rejected_handle = mlines.Line2D(
            [], [],
            color=REJECTED_COLOR,
            linestyle="--",
            linewidth=0.8,
            alpha=0.4,
            label="REJECTED (prefilter/postfilter)",
        )
        legend_handles.append(rejected_handle)

    ax2.legend(handles=legend_handles, loc="upper right", fontsize=7.5, framealpha=0.85)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return fig, stats


def compile_route_audit_pdf(
    route_id: str,
    cohort_map_df: pd.DataFrame,
    trajectories: dict[str, pd.DataFrame],
    out_pdf_path: Path,
    eval_df: pd.DataFrame | None = None,
    show_rejected: bool = False,
    plot_format: str = "SVG",
    trajectories_clean: dict[str, pd.DataFrame] | None = None,
) -> Path:
    """Compiles a multi-page PDF report for a route by iterating through all cohorts."""
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    df_route_map = cohort_map_df[cohort_map_df["route_id"] == route_id].copy()
    if df_route_map.empty:
        logger.warning(f"No cohort mapping found for route {route_id}")
        return out_pdf_path

    eval_records = eval_df[eval_df["route_id"] == route_id].set_index("flight_id").to_dict(orient="index") if eval_df is not None and not eval_df.empty else None
    cohorts = sorted(df_route_map["cohort_idx"].unique())
    logger.info(f"Compiling {len(cohorts)}-page PDF audit report ({plot_format.upper()} mode, 4-plot={trajectories_clean is not None}) for {route_id} -> {out_pdf_path.name}...")

    route_total_pts, route_na_pts, route_na_flights = 0, 0, 0
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
                crop_padding=1.5,
                plot_format=plot_format,
                trajectories_clean=trajectories_clean,
            )
            pdf.savefig(fig, dpi=DEFAULT_DPI, bbox_inches="tight")
            plt.close(fig)
            route_total_pts += stats["total_points"]
            route_na_pts += stats["na_points"]
            route_na_flights += stats["flights_with_na"]
            na_pct = (stats["na_points"] / stats["total_points"] * 100.0) if stats["total_points"] > 0 else 0.0
            logger.info(f"   -> Cohort {c_idx:2d}: Rendered {stats['plotted']:2d} flights | Phase NA: {stats['na_points']:6d}/{stats['total_points']:6d} pts ({na_pct:4.1f}%) across {stats['flights_with_na']:2d} flights.")

    route_na_pct = (route_na_pts / route_total_pts * 100.0) if route_total_pts > 0 else 0.0
    logger.info(f"[{route_id} Summary] Total Trajectory Points: {route_total_pts:,} | Total Phase NA: {route_na_pts:,} ({route_na_pct:.1f}%) across {route_na_flights} flights.")
    return out_pdf_path
