"""
Verification Module: Map Cache & Airport Overlay Verification

Plots the pre-loaded European basemap (EuropeanMapCache) overlaid with airport
coordinates to verify shapefile rendering and airport registry alignment.
"""

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs

from src.common.config import BASE_DIR
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)


def generate_verification_map(output_dir: Path | None = None) -> None:
    """
    Renders and saves the European basemap verification figure overlaid with airport coordinates.
    """
    if output_dir is None:
        output_dir = BASE_DIR / "data" / "analysis" / "plots"

    logger.info("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")

    logger.info(f"Loaded {len(cache.airports_df)} airports.")
    logger.info("Creating verification figure...")
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    logger.info("Adding physical and cultural features to axes...")
    cache.add_features_to_axes(ax)

    logger.info("Plotting airport coordinates...")
    if not cache.airports_df.empty:
        ax.scatter(
            cache.airports_df["lon"],
            cache.airports_df["lat"],
            color="red",
            s=15,
            alpha=0.6,
            edgecolors="none",
            transform=ccrs.PlateCarree(),
            zorder=3,
            label="Airports"
        )

        # Label a subset of major airports to verify alignment
        sample_airports = ["EGLL", "EDDF", "LIRF", "EHAM", "LEMD", "ESSA", "BIKF"]
        for _, row in cache.airports_df.iterrows():
            icao = row["icao"]
            if icao in sample_airports:
                ax.text(
                    row["lon"] + 0.3,
                    row["lat"] + 0.3,
                    icao,
                    transform=ccrs.PlateCarree(),
                    fontsize=9,
                    fontweight="bold",
                    color="black",
                    bbox=dict(facecolor="white", alpha=0.7, boxstyle="round,pad=0.2", edgecolor="none"),
                    zorder=4
                )

    # Gridlines and Ticks
    try:
        import numpy as np
        import matplotlib.ticker as mticker
        from cartopy.mpl.ticker import LongitudeFormatter, LatitudeFormatter

        major_lons = np.arange(-180, 181, 15)
        major_lats = np.arange(-90, 91, 15)
        minor_lons = np.arange(-180, 181, 5)
        minor_lats = np.arange(-90, 91, 5)

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

        gl_minor = ax.gridlines(
            draw_labels=False,
            linestyle=":",
            color="darkgray",
            linewidth=0.5,
            alpha=0.4
        )
        gl_minor.xlocator = mticker.FixedLocator(minor_lons)
        gl_minor.ylocator = mticker.FixedLocator(minor_lats)
    except Exception as e:
        logger.warning(f"Gridlines/tick labeling notice: {e}. Drawing simple grid instead.")
        ax.grid(True, linestyle="--", alpha=0.6, color="dimgray")

    ax.set_title("European Map Cache & Airport Registry Verification", fontsize=14, fontweight="bold", pad=15)

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
    generate_verification_map()


if __name__ == "__main__":
    main()
