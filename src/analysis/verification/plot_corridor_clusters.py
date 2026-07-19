"""
Verification Module: Trajectory Cluster & Medoid Visualization

Renders dual-panel visualizations comparing cluster member trajectories,
historical medoid trajectories, and resampled corridor templates for a route:
  - Left Panel (GeoMap): Cartopy PlateCarree GeoAxes overlaid with EuropeanMapCache.
  - Right Panel (Altitude Profile): Altitude trajectories across resampled progress steps.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import cartopy.crs as ccrs

from src.common.config import (
    BASE_DIR,
    GLOBAL_FLIGHT_CLUSTER_MAP,
    GLOBAL_CORRIDOR_SIM_REGISTRY,
    GLOBAL_CLEAN_REGISTRY,
)
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def plot_corridor_clusters(
    route_id: str,
    out_file: Path | None = None,
    show: bool = False,
    padding: float = 1.5,
) -> None:
    """
    Renders a dual-panel plot for a route corridor:
      - Left panel: Cartopy PlateCarree GeoMap with EuropeanMapCache basemap features.
      - Right panel: Altitude profiles across trajectory steps.
    """
    logger.info(f"Checking cluster map for route: {route_id}")

    if not GLOBAL_FLIGHT_CLUSTER_MAP.exists():
        logger.error(f"Flight cluster map not found at: {GLOBAL_FLIGHT_CLUSTER_MAP}")
        return

    df_map = pd.read_parquet(GLOBAL_FLIGHT_CLUSTER_MAP)

    # Support 'route' or 'route_id' column naming
    if "route" in df_map.columns and "route_id" not in df_map.columns:
        df_map = df_map.rename(columns={"route": "route_id"})

    df_route_map = df_map[df_map["route_id"] == route_id]

    if df_route_map.empty:
        logger.error(f"No cluster mappings found for route '{route_id}' in {GLOBAL_FLIGHT_CLUSTER_MAP}")
        return

    logger.info(
        f"Found {len(df_route_map)} mapped flights across "
        f"{df_route_map['cluster_id'].nunique()} cluster(s)."
    )

    # 1. Load clean registry to locate trajectory files
    if not GLOBAL_CLEAN_REGISTRY.exists():
        logger.error(f"Clean registry not found at: {GLOBAL_CLEAN_REGISTRY}")
        return

    df_clean = pd.read_parquet(GLOBAL_CLEAN_REGISTRY)
    df_clean_mapped = df_clean[df_clean["flight_id"].isin(df_route_map["flight_id"])]

    if df_clean_mapped.empty:
        logger.error("Could not match any mapped flight_ids to file paths in clean registry.")
        return

    # Group flight_ids by file_path for efficient loading
    file_to_flights = defaultdict(list)
    for _, row in df_clean_mapped.iterrows():
        fpath = Path(row["file_path"])
        if not fpath.is_absolute():
            fpath = BASE_DIR / fpath
        file_to_flights[fpath].append(row["flight_id"])

    # Load trajectory DataFrames
    logger.info(f"Loading trajectory data from {len(file_to_flights)} parquet file(s)...")
    loaded_trajectories: dict[str, pd.DataFrame] = {}
    for fpath, fids in file_to_flights.items():
        if not fpath.exists():
            logger.warning(f"File path does not exist: {fpath}")
            continue
        try:
            df_file = pd.read_parquet(fpath)
            for fid in fids:
                df_fid = df_file[df_file["flight_id"] == fid]
                if not df_fid.empty:
                    loaded_trajectories[fid] = df_fid
        except Exception as e:
            logger.error(f"Failed reading {fpath}: {e}")

    logger.info(f"Loaded {len(loaded_trajectories)} historical trajectory DataFrames.")

    # 2. Load Corridor Simulation Registry for templates
    templates: dict[int, pd.DataFrame] = {}
    if GLOBAL_CORRIDOR_SIM_REGISTRY.exists():
        try:
            df_sim = pd.read_parquet(GLOBAL_CORRIDOR_SIM_REGISTRY)
            matched_sim = df_sim[df_sim["flight_id"].astype(str).str.startswith(f"{route_id}_corridor_c")]
            for _, row in matched_sim.iterrows():
                fid_sim = str(row["flight_id"])
                if "_c" in fid_sim:
                    try:
                        cid = int(fid_sim.split("_c")[-1])
                        tpath = Path(row["file_path"])
                        if not tpath.is_absolute():
                            tpath = BASE_DIR / tpath
                        if tpath.exists():
                            templates[cid] = pd.read_parquet(tpath)
                    except ValueError:
                        pass
        except Exception as e:
            logger.warning(f"Could not load templates from {GLOBAL_CORRIDOR_SIM_REGISTRY}: {e}")

    # 3. Setup Dual-Panel Figure: GeoMap (Left) + Altitude Profile (Right)
    fig = plt.figure(figsize=(14, 6.5))
    ax1 = fig.add_subplot(1, 2, 1, projection=ccrs.PlateCarree())
    ax2 = fig.add_subplot(1, 2, 2)

    # Pre-load and draw cached European basemap features
    map_cache = EuropeanMapCache().initialize()
    map_cache.add_features_to_axes(ax1)

    colors = plt.cm.tab10.colors
    cluster_ids = sorted(df_route_map["cluster_id"].unique())

    print("\n" + "=" * 65)
    print(f"CLUSTER SUMMARY FOR ROUTE: {route_id}")
    print("=" * 65)
    print(f"{'Cluster ID':<12} {'Flight Count':<16} {'Medoid Flight ID'}")
    print("-" * 65)

    # Bounding box tracking for auto-cropping
    min_lon, max_lon = 180.0, -180.0
    min_lat, max_lat = 90.0, -90.0

    for cid in cluster_ids:
        color = colors[cid % len(colors)]
        cluster_flights = df_route_map[df_route_map["cluster_id"] == cid]
        medoid_rows = cluster_flights[cluster_flights["is_medoid"] == True]
        medoid_fid = medoid_rows["flight_id"].iloc[0] if not medoid_rows.empty else "UNKNOWN"

        print(f"{cid:<12} {len(cluster_flights):<16} {medoid_fid}")

        # Plot cluster member flights
        first_member = True
        for _, row in cluster_flights.iterrows():
            fid = row["flight_id"]
            if row["is_medoid"] or fid not in loaded_trajectories:
                continue
            df_traj = loaded_trajectories[fid].sort_values(by="time")
            lons = df_traj["longitude"].values
            lats = df_traj["latitude"].values

            min_lon = min(min_lon, float(np.min(lons)))
            max_lon = max(max_lon, float(np.max(lons)))
            min_lat = min(min_lat, float(np.min(lats)))
            max_lat = max(max_lat, float(np.max(lats)))

            label = f"Cluster {cid} Members (n={len(cluster_flights)})" if first_member else None
            ax1.plot(
                lons, lats,
                color=color, alpha=0.25, linewidth=0.8, zorder=3,
                transform=ccrs.PlateCarree(), label=label
            )

            # Altitude profile (ax2)
            alt_col = "altitude" if "altitude" in df_traj.columns else ("baroaltitude" if "baroaltitude" in df_traj.columns else None)
            if alt_col:
                alts = df_traj[alt_col].values
                ax2.plot(np.linspace(0, 100, len(alts)), alts, color=color, alpha=0.25, linewidth=0.8, zorder=3)

            first_member = False

        # Plot historical medoid
        if not medoid_rows.empty and medoid_fid in loaded_trajectories:
            df_med = loaded_trajectories[medoid_fid].sort_values(by="time")
            lons_m = df_med["longitude"].values
            lats_m = df_med["latitude"].values

            min_lon = min(min_lon, float(np.min(lons_m)))
            max_lon = max(max_lon, float(np.max(lons_m)))
            min_lat = min(min_lat, float(np.min(lats_m)))
            max_lat = max(max_lat, float(np.max(lats_m)))

            ax1.plot(
                lons_m, lats_m,
                color=color, alpha=1.0, linewidth=2.8, zorder=5,
                transform=ccrs.PlateCarree(),
                label=f"Cluster {cid} Medoid ({medoid_fid[:15]}...)"
            )
            # Start / End markers
            ax1.scatter(
                lons_m[0], lats_m[0],
                color=color, marker="o", s=60, edgecolor="black", zorder=6,
                transform=ccrs.PlateCarree()
            )
            ax1.scatter(
                lons_m[-1], lats_m[-1],
                color=color, marker="X", s=80, edgecolor="black", zorder=6,
                transform=ccrs.PlateCarree()
            )

            # Medoid altitude profile
            alt_col_m = "altitude" if "altitude" in df_med.columns else ("baroaltitude" if "baroaltitude" in df_med.columns else None)
            if alt_col_m:
                alts_m = df_med[alt_col_m].values
                ax2.plot(np.linspace(0, 100, len(alts_m)), alts_m, color=color, alpha=1.0, linewidth=2.5, zorder=5)

        # Plot resampled corridor template (from corridor_paths)
        if cid in templates:
            df_t = templates[cid].sort_values(by="time")
            lons_t = df_t["longitude"].values
            lats_t = df_t["latitude"].values

            ax1.plot(
                lons_t, lats_t,
                color="black", linestyle="--", linewidth=1.8, alpha=0.85, zorder=4,
                transform=ccrs.PlateCarree(),
                label=f"Cluster {cid} Template (Resampled)"
            )

            if "altitude" in df_t.columns:
                alts_t = df_t["altitude"].values
                ax2.plot(np.linspace(0, 100, len(alts_t)), alts_t, color="black", linestyle="--", linewidth=1.8, alpha=0.85, zorder=4)

    print("=" * 65 + "\n")

    # Set cropped map extent
    if min_lon < max_lon and min_lat < max_lat:
        ax1.set_extent(
            [min_lon - padding, max_lon + padding, min_lat - padding, max_lat + padding],
            crs=ccrs.PlateCarree()
        )

    # Dynamic Cartopy Gridlines
    try:
        gl = ax1.gridlines(draw_labels=True, linestyle="--", color="dimgray", linewidth=0.6, alpha=0.5)
        gl.top_labels = False
        gl.right_labels = False
        gl.xlabel_style = {"size": 8}
        gl.ylabel_style = {"size": 8}
    except Exception as e:
        logger.debug(f"Gridline labeling notice: {e}")

    ax1.set_title(f"Trajectory Clusters & Medoids: {route_id}", fontsize=12, fontweight="bold", pad=12)
    ax1.legend(loc="lower left", fontsize=7.5, framealpha=0.85)

    # Right Panel Formatting (Altitude Profiles)
    ax2.set_title("Altitude Profiles by Cluster", fontsize=12, fontweight="bold", pad=12)
    ax2.set_xlabel("Normalized Trajectory Progress (%)", fontsize=10)
    ax2.set_ylabel("Altitude (m)", fontsize=10)
    ax2.grid(True, linestyle="--", alpha=0.5)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, p: f"{int(y):,}m"))

    plt.tight_layout()

    # Determine default output file location if not provided
    if out_file is None:
        out_dir = BASE_DIR / "data" / "analysis" / "plots"
        out_file = out_dir / f"{route_id}_clusters.svg"

    out_file.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_file, dpi=150, bbox_inches="tight")
    logger.info(f"Cluster plot saved successfully to: {out_file.resolve()}")

    if show:
        plt.show()
    else:
        plt.close()


def main() -> None:
    setup_file_logger(log_filename="analysis.log")

    parser = argparse.ArgumentParser(description="Plot cluster trajectories, medoids, and altitude profiles for a route.")
    parser.add_argument("--route-id", type=str, default="LEPA-LEBL", help="Route ID string (e.g. LEPA-LEBL)")
    parser.add_argument(
        "--out-file",
        type=str,
        default=None,
        help="Path to save output plot file (default: data/analysis/plots/<route_id>_clusters.svg)",
    )
    parser.add_argument("--show", action="store_true", help="Display interactive matplotlib plot window")
    args = parser.parse_args()

    out_path = Path(args.out_file) if args.out_file else None
    plot_corridor_clusters(
        route_id=args.route_id,
        out_file=out_path,
        show=args.show,
    )


if __name__ == "__main__":
    main()
