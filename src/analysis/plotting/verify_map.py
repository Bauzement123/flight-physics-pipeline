"""
Verification scratch script for Map + Airport Builder.
Plots the pre-loaded European basemap with all airport coordinates overlaid.
"""

import sys
import os
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs

from src.common.map_cache import EuropeanMapCache

def main():
    print("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")

    print(f"Loaded {len(cache.airports_df)} airports.")
    print("Creating verification figure...")
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    print("Adding physical and cultural features to axes...")
    cache.add_features_to_axes(ax)

    print("Plotting airport coordinates...")
    if not cache.airports_df.empty:
        # Plot all airports as semi-transparent red dots
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
                    bbox=dict(facecolor='white', alpha=0.7, boxstyle='round,pad=0.2', edgecolor='none'),
                    zorder=4
                )

    # Add ticks and formatted labels to the axes frame
    try:
        import numpy as np
        import matplotlib.ticker as mticker
        from cartopy.mpl.ticker import LongitudeFormatter, LatitudeFormatter
        
        # Major ticks every 15 degrees
        major_lons = np.arange(-180, 181, 15)
        major_lats = np.arange(-90, 91, 15)
        
        # Minor ticks every 5 degrees
        minor_lons = np.arange(-180, 181, 5)
        minor_lats = np.arange(-90, 91, 5)
        
        ax.set_xticks(major_lons, crs=ccrs.PlateCarree())
        ax.set_yticks(major_lats, crs=ccrs.PlateCarree())
        
        # Format ticks with degree symbols (E/W/N/S)
        ax.xaxis.set_major_formatter(LongitudeFormatter())
        ax.yaxis.set_major_formatter(LatitudeFormatter())
        
        # Adjust tick label size
        ax.tick_params(axis='both', which='major', labelsize=9)

        # 1. Draw major gridlines (aligned with 15-degree ticks)
        gl_major = ax.gridlines(
            draw_labels=False,
            linestyle="--",
            color="dimgray",
            linewidth=0.8,
            alpha=0.6
        )
        gl_major.xlocator = mticker.FixedLocator(major_lons)
        gl_major.ylocator = mticker.FixedLocator(major_lats)

        # 2. Draw minor gridlines (every 5 degrees, no labels)
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
        print(f"Gridlines/tick labeling failed: {e}. Drawing simple grid instead.")
        ax.grid(True, linestyle="--", alpha=0.6, color="dimgray")

    ax.set_title("European Map Cache & Airport Registry Verification", fontsize=14, fontweight="bold", pad=15)
    
    # Save the output image
    out_dir = PROJECT_ROOT / "scratch"
    out_dir.mkdir(exist_ok=True)
    out_path_svg = out_dir / "verify_map.svg"
    out_path_png = out_dir / "verify_map.png"
    
    print(f"Saving output figures to {out_dir}...")
    fig.savefig(out_path_svg, format="svg", bbox_inches="tight")
    fig.savefig(out_path_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("Verification map generated successfully!")

if __name__ == "__main__":
    main()
