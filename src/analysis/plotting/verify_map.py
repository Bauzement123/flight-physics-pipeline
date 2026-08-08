"""
Verification Module: Map Cache & Airport Overlay Verification

Plots the pre-loaded European basemap (EuropeanMapCache) overlaid with airport
coordinates to verify shapefile rendering, airport registry alignment, and geographic bounding box fit.
"""

import argparse
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import cartopy.crs as ccrs

from src.common.config import (
    BASE_DIR,
    EUR_LAT_MIN,
    EUR_LAT_MAX,
    EUR_LON_MIN,
    EUR_LON_MAX,
    WEATHER_PADDING
)
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def generate_verification_map(
    output_dir: Path | None = None,
    show_bbox: bool = False,
    target_airframes_only: bool = False,
    icao_schema_only: bool = False,
    survived_bbox_only: bool = False,
    lat_min: float = EUR_LAT_MIN,
    lat_max: float = EUR_LAT_MAX,
    lon_min: float = EUR_LON_MIN,
    lon_max: float = EUR_LON_MAX,
) -> None:
    """
    Renders and saves the European basemap verification figure overlaid with styled airport coordinates.
    """
    if output_dir is None:
        output_dir = BASE_DIR / "data" / "analysis" / "plots"

    logger.info("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")

    df_airports = cache.airports_df.copy()
    logger.info(f"Loaded total {len(df_airports)} airports from cache.")

    # Apply filters based on CLI flags
    if target_airframes_only and "has_target_airframe" in df_airports.columns:
        df_airports = df_airports[df_airports["has_target_airframe"] == True]
        logger.info(f"Filtered by --target-airframes-only: {len(df_airports)} airports remaining.")

    if icao_schema_only and "is_icao_schema" in df_airports.columns:
        df_airports = df_airports[df_airports["is_icao_schema"] == True]
        logger.info(f"Filtered by --icao-schema-only: {len(df_airports)} airports remaining.")

    if survived_bbox_only and "survived_bbox" in df_airports.columns:
        df_airports = df_airports[df_airports["survived_bbox"] == True]
        logger.info(f"Filtered by --survived-bbox-only: {len(df_airports)} airports remaining.")

    logger.info("Creating verification figure...")
    fig = plt.figure(figsize=(14, 11))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    logger.info("Adding physical and cultural features to axes...")
    cache.add_features_to_axes(ax)

    # Draw bounding box rectangle if requested
    if show_bbox:
        logger.info(f"Drawing bounding box rectangle: Lat [{lat_min}, {lat_max}], Lon [{lon_min}, {lon_max}]")
        bbox_rect = mpatches.Rectangle(
            (lon_min, lat_min),
            lon_max - lon_min,
            lat_max - lat_min,
            fill=False,
            edgecolor="darkred",
            linewidth=2.5,
            linestyle="--",
            transform=ccrs.PlateCarree(),
            zorder=5,
            label=f"Base BBox [{lat_min:.1f}, {lat_max:.1f}] x [{lon_min:.1f}, {lon_max:.1f}]"
        )
        ax.add_patch(bbox_rect)

        pad = WEATHER_PADDING
        weather_bbox_rect = mpatches.Rectangle(
            (lon_min - pad, lat_min - pad),
            (lon_max - lon_min) + (pad * 2),
            (lat_max - lat_min) + (pad * 2),
            fill=False,
            edgecolor="forestgreen",
            linewidth=2.5,
            linestyle="-.",
            transform=ccrs.PlateCarree(),
            zorder=4,
            label=f"Weather BBox (+{pad}° Pad)"
        )
        ax.add_patch(weather_bbox_rect)

    logger.info("Plotting airport coordinates with shape/color encoding...")
    if not df_airports.empty:
        # Group 1: ICAO Schema & Target Airframe -> Red X
        mask1 = (df_airports["is_icao_schema"] == True) & (df_airports["has_target_airframe"] == True)
        if mask1.any():
            ax.scatter(
                df_airports.loc[mask1, "lon"],
                df_airports.loc[mask1, "lat"],
                color="red",
                marker="x",
                s=35,
                linewidths=1.5,
                transform=ccrs.PlateCarree(),
                zorder=4,
                label="ICAO Schema + Target Airframe (Red X)"
            )

        # Group 2: ICAO Schema & Non-Target Airframe -> Gray X
        mask2 = (df_airports["is_icao_schema"] == True) & (df_airports["has_target_airframe"] == False)
        if mask2.any():
            ax.scatter(
                df_airports.loc[mask2, "lon"],
                df_airports.loc[mask2, "lat"],
                color="gray",
                marker="x",
                s=25,
                alpha=0.6,
                transform=ccrs.PlateCarree(),
                zorder=3,
                label="ICAO Schema + Non-Target Airframe (Gray X)"
            )

        # Group 3: Non-ICAO Schema & Target Airframe -> Red O
        mask3 = (df_airports["is_icao_schema"] == False) & (df_airports["has_target_airframe"] == True)
        if mask3.any():
            ax.scatter(
                df_airports.loc[mask3, "lon"],
                df_airports.loc[mask3, "lat"],
                color="red",
                marker="o",
                s=30,
                facecolors="none",
                linewidths=1.5,
                transform=ccrs.PlateCarree(),
                zorder=4,
                label="Non-ICAO Schema + Target Airframe (Red O)"
            )

        # Group 4: Non-ICAO Schema & Non-Target Airframe -> Gray O
        mask4 = (df_airports["is_icao_schema"] == False) & (df_airports["has_target_airframe"] == False)
        if mask4.any():
            ax.scatter(
                df_airports.loc[mask4, "lon"],
                df_airports.loc[mask4, "lat"],
                color="gray",
                marker="o",
                s=20,
                facecolors="none",
                alpha=0.5,
                transform=ccrs.PlateCarree(),
                zorder=3,
                label="Non-ICAO Schema + Non-Target Airframe (Gray O)"
            )

        # Label key edge airports or sample airports
        sample_airports = ["EGLL", "EDDF", "LIRF", "EHAM", "LEMD", "ESSA", "BIKF", "LLRM", "LTCF", "ENAS", "LPAZ", "LPPD"]
        for _, row in df_airports.iterrows():
            icao = row["icao"]
            if icao in sample_airports:
                ax.text(
                    row["lon"] + 0.3,
                    row["lat"] + 0.3,
                    icao,
                    transform=ccrs.PlateCarree(),
                    fontsize=8,
                    fontweight="bold",
                    color="darkblue",
                    bbox=dict(facecolor="white", alpha=0.8, boxstyle="round,pad=0.2", edgecolor="gray"),
                    zorder=6
                )

    # Gridlines and Ticks
    try:
        import numpy as np
        import matplotlib.ticker as mticker
        from cartopy.mpl.ticker import LongitudeFormatter, LatitudeFormatter

        major_lons = np.arange(-180, 181, 15)
        major_lats = np.arange(-90, 91, 15)

        ax.set_xticks(major_lons, crs=ccrs.PlateCarree())
        ax.set_yticks(major_lats, crs=ccrs.PlateCarree())

        ax.xaxis.set_major_formatter(LongitudeFormatter())
        ax.yaxis.set_major_formatter(LatitudeFormatter())
        ax.tick_params(axis="both", which="major", labelsize=9)

        gl_major = ax.gridlines(
            draw_labels=False,
            linestyle="--",
            color="dimgray",
            linewidth=0.8,
            alpha=0.6
        )
        gl_major.xlocator = mticker.FixedLocator(major_lons)
        gl_major.ylocator = mticker.FixedLocator(major_lats)
    except Exception as e:
        logger.warning(f"Gridlines notice: {e}. Drawing simple grid.")
        ax.grid(True, linestyle="--", alpha=0.6, color="dimgray")

    ax.legend(loc="lower left", fontsize=8, framealpha=0.9)
    ax.set_title("European Map Cache & Airport Visual Triage Verification", fontsize=14, fontweight="bold", pad=15)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path_svg = output_dir / "verify_map.svg"
    out_path_png = output_dir / "verify_map.png"

    logger.info(f"Saving output figures to {output_dir}...")
    fig.savefig(out_path_svg, format="svg", bbox_inches="tight")
    fig.savefig(out_path_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("✓ Verification map generated successfully!")


def main():
    setup_file_logger(log_filename="analysis.log")

    parser = argparse.ArgumentParser(description="Render Verification Map with Styled Airports and Bounding Box")
    parser.add_argument("--output-dir", help="Directory to save generated verification plots")
    parser.add_argument("--show-bbox", action="store_true", help="Draw the physical rectangular bounding box overlay on the map")
    parser.add_argument("--target-airframes-only", action="store_true", help="Only render airports that are served by target airframes")
    parser.add_argument("--icao-schema-only", action="store_true", help="Only render airports following standard 4-letter ICAO schema")
    parser.add_argument("--survived-bbox-only", action="store_true", help="Only render airports that survived the bounding box filter")
    parser.add_argument("--lat-min", type=float, default=EUR_LAT_MIN, help="Latitude min for bbox overlay")
    parser.add_argument("--lat-max", type=float, default=EUR_LAT_MAX, help="Latitude max for bbox overlay")
    parser.add_argument("--lon-min", type=float, default=EUR_LON_MIN, help="Longitude min for bbox overlay")
    parser.add_argument("--lon-max", type=float, default=EUR_LON_MAX, help="Longitude max for bbox overlay")

    args = parser.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else None

    generate_verification_map(
        output_dir=out_dir,
        show_bbox=args.show_bbox,
        target_airframes_only=args.target_airframes_only,
        icao_schema_only=args.icao_schema_only,
        survived_bbox_only=args.survived_bbox_only,
        lat_min=args.lat_min,
        lat_max=args.lat_max,
        lon_min=args.lon_min,
        lon_max=args.lon_max,
    )


if __name__ == "__main__":
    main()
