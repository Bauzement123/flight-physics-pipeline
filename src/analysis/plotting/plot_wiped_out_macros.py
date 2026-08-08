"""
Plotting Top 20 Wiped-Out Macro Routes
Visualizes the geographic bounds and corridors of macro routes that were deleted by strict config bounds.
"""

import argparse
import logging
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs

from src.common.config import BASE_DIR
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)

def plot_top_wiped_out_routes(csv_path: Path, output_dir: Path):
    if not csv_path.exists():
        logger.error(f"CSV not found at {csv_path}")
        return

    df = pd.read_csv(csv_path)
    # Filter for wiped out routes that were previously viable
    wiped_out = df[df['Is_Wiped_Out_Now'] & df['Was_Viable']]
    # Sort by initial count (highest impact) and take top 20
    top_20 = wiped_out.sort_values('Initial_Count', ascending=False).head(20)
    
    if top_20.empty:
        logger.info("No wiped out routes found in the CSV.")
        return

    logger.info("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")  # Use 10m because it's already downloaded in the user's cartopy cache
    df_airports = cache.airports_df.copy()

    # Extract airports and filter for European BBox only
    df_airports = df_airports[df_airports['survived_bbox'] == True]
    
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, row in top_20.iterrows():
        route = row['Canonical_Route']
        initial = row['Initial_Count']
        surviving = row['Surviving_Count']
        
        logger.info(f"Plotting route {route} ({initial} -> {surviving} flights)")
        
        try:
            dep_prefix, arr_prefix = route.split('-')
        except ValueError:
            logger.warning(f"Invalid route format: {route}")
            continue

        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        
        # Add map features
        cache.add_features_to_axes(ax)
        
        # Extract airports
        dep_airports = df_airports[df_airports['icao'].str.startswith(dep_prefix, na=False)]
        arr_airports = df_airports[df_airports['icao'].str.startswith(arr_prefix, na=False)]
        
        # Plot departure airports
        if not dep_airports.empty:
            ax.scatter(
                dep_airports['lon'], dep_airports['lat'], 
                color='forestgreen', marker='o', s=30, edgecolor='black', 
                transform=ccrs.PlateCarree(), zorder=4, label=f'{dep_prefix} (Departures)'
            )
            
        # Plot arrival airports
        if not arr_airports.empty:
            ax.scatter(
                arr_airports['lon'], arr_airports['lat'], 
                color='darkred', marker='s', s=30, edgecolor='black', 
                transform=ccrs.PlateCarree(), zorder=4, label=f'{arr_prefix} (Arrivals)'
            )
            
        # Draw faint connection lines
        if not dep_airports.empty and not arr_airports.empty:
            for _, dep_row in dep_airports.iterrows():
                for _, arr_row in arr_airports.iterrows():
                    ax.plot(
                        [dep_row['lon'], arr_row['lon']],
                        [dep_row['lat'], arr_row['lat']],
                        color='gray', linewidth=0.5, alpha=0.1,
                        transform=ccrs.PlateCarree(), zorder=3
                    )
        
        ax.legend(loc='lower left', fontsize=9)
        ax.set_title(f"Macro Route: {route}\nInitial: {initial} | Survived: {surviving} (WIPED OUT)", fontsize=12, fontweight='bold')
        
        out_path = output_dir / f"{route}.png"
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved {out_path}")

    logger.info("Finished generating all 20 plots.")

def main():
    setup_file_logger(log_filename="analysis.log")
    
    # Use absolute path for CSV assuming it's in the artifact dir, 
    # but we'll accept it via arg
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv-path', type=str, required=True, help="Path to clustering_wipeouts.csv")
    parser.add_argument('--output-dir', type=str, default=str(BASE_DIR / "data" / "analysis" / "plots" / "wipeouts"), help="Output directory")
    args = parser.parse_args()
    
    plot_top_wiped_out_routes(Path(args.csv_path), Path(args.output_dir))

if __name__ == "__main__":
    main()
