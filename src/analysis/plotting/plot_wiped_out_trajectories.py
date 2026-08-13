"""
Plotting Top 20 Wiped-Out Macro Routes (Clean Trajectories)
Visualizes the actual flight paths of macro routes that were deleted during clustering.
"""

import argparse
import logging
import random
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import os

from src.common.config import BASE_DIR
from src.common.map_cache import EuropeanMapCache
from src.common.utils import setup_file_logger, split_route_string

logger = logging.getLogger(__name__)

MAX_TRAJECTORIES_PER_MACRO = 1000

def _normalize_wipeout_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if 'canonical_route' in df.columns and 'Canonical_Route' not in df.columns:
        df['Canonical_Route'] = df['canonical_route']
    elif 'route' in df.columns and 'Canonical_Route' not in df.columns:
        df['Canonical_Route'] = df['route']
        
    if 'total_flights' in df.columns and 'Initial_Count' not in df.columns:
        df['Initial_Count'] = df['total_flights']
    if 'passed_flights' in df.columns and 'Surviving_Count' not in df.columns:
        df['Surviving_Count'] = df['passed_flights']
        
    if 'Is_Wiped_Out_Now' not in df.columns:
        if 'exclusion_pct' in df.columns:
            df['Is_Wiped_Out_Now'] = df['exclusion_pct'] > 0
        elif 'Surviving_Count' in df.columns:
            df['Is_Wiped_Out_Now'] = df['Surviving_Count'] == 0
        else:
            df['Is_Wiped_Out_Now'] = True
            
    if 'Was_Viable' not in df.columns:
        if 'Initial_Count' in df.columns:
            df['Was_Viable'] = df['Initial_Count'] > 0
        else:
            df['Was_Viable'] = True

    return df

def plot_wiped_out_trajectories(csv_path: Path, output_dir: Path, trajectories_dir: Path):
    if not csv_path.exists():
        logger.error(f"CSV not found at {csv_path}")
        return

    df = pd.read_csv(csv_path)
    df = _normalize_wipeout_df(df)
    wiped_out = df[df['Is_Wiped_Out_Now'] & df['Was_Viable']]
    top_20 = wiped_out.sort_values('Initial_Count', ascending=False).head(20)
    
    if top_20.empty:
        logger.info("No wiped out routes found in the CSV.")
        return

    logger.info("Initializing EuropeanMapCache...")
    cache = EuropeanMapCache()
    cache.initialize(resolution="10m")
    df_airports = cache.airports_df.copy()
    df_airports = df_airports[df_airports['survived_bbox'] == True]
    
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-gather all rank folders
    all_route_folders = [d for d in trajectories_dir.iterdir() if d.is_dir()]

    for idx, row in top_20.iterrows():
        macro_route = row['Canonical_Route']
        initial = row['Initial_Count']
        surviving = row['Surviving_Count']
        
        logger.info(f"Processing macro {macro_route}...")
        dep_prefix, arr_prefix = split_route_string(macro_route)
        if dep_prefix == "UNK" or arr_prefix == "UNK":
            logger.warning(f"Invalid route format: {macro_route}")
            continue

        # Find matching rank folders
        matching_folders = []
        for folder in all_route_folders:
            # name format: rank_001_EBBR-LEMD or EBBR-LEMD
            route = folder.name.split('_')[-1]
            r_dep, r_arr = split_route_string(route)
            if r_dep != "UNK" and r_arr != "UNK":
                if r_dep.startswith(dep_prefix) and r_arr.startswith(arr_prefix):
                    matching_folders.append(folder)

        if not matching_folders:
            logger.warning(f"No downloaded trajectory folders found for macro {macro_route}")
            continue

        # Gather all clean parquet files
        clean_parquets = []
        for fld in matching_folders:
            clean_dir = fld / "clean"
            if clean_dir.exists():
                clean_parquets.extend(list(clean_dir.glob("*.parquet")))

        if not clean_parquets:
            logger.warning(f"No clean trajectories found for macro {macro_route}")
            continue

        # Subsample if too many
        if len(clean_parquets) > MAX_TRAJECTORIES_PER_MACRO:
            logger.info(f"Found {len(clean_parquets)} flights, sampling down to {MAX_TRAJECTORIES_PER_MACRO}...")
            clean_parquets = random.sample(clean_parquets, MAX_TRAJECTORIES_PER_MACRO)
        else:
            logger.info(f"Found {len(clean_parquets)} clean flights for macro {macro_route}.")

        # Setup Plot
        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        cache.add_features_to_axes(ax)

        # Plot departure/arrival airports (Removed to reduce map clutter)
        
        # Plot the trajectories
        plotted_count = 0
        for pq_path in clean_parquets:
            try:
                # The data should have longitude, latitude.
                df_flight = pd.read_parquet(pq_path, columns=['longitude', 'latitude'])
                if not df_flight.empty:
                    ax.plot(
                        df_flight['longitude'], df_flight['latitude'],
                        color='blue', linewidth=0.5, alpha=0.15,
                        transform=ccrs.PlateCarree(), zorder=3
                    )
                    plotted_count += 1
            except Exception as e:
                logger.debug(f"Failed to load {pq_path}: {e}")

        # Adding a single proxy artist for the legend to represent the trajectories
        if plotted_count > 0:
            import matplotlib.lines as mlines
            line_proxy = mlines.Line2D([], [], color='blue', linewidth=1, alpha=0.5, label=f'Clean Flights (n={plotted_count})')
            handles, labels = ax.get_legend_handles_labels()
            handles.append(line_proxy)
            labels.append(line_proxy.get_label())
            ax.legend(handles=handles, labels=labels, loc='lower left', fontsize=9)
        else:
            ax.legend(loc='lower left', fontsize=9)
            
        ax.set_title(f"Macro Route: {macro_route} Actual Flight Paths\nInitial: {initial} | Survived: {surviving} (WIPED OUT)", fontsize=12, fontweight='bold')
        
        out_path = output_dir / f"{macro_route}_trajectories.png"
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved {out_path}")

    logger.info("Finished plotting all macros.")

def main():
    setup_file_logger(log_filename="analysis.log")
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv-path', type=str, required=True, help="Path to clustering_wipeouts.csv")
    parser.add_argument('--output-dir', type=str, default=str(BASE_DIR / "data" / "analysis" / "plots" / "wipeout_trajectories"), help="Output directory")
    parser.add_argument('--trajectories-dir', type=str, default=str(BASE_DIR / "data" / "trajectories"), help="Path to trajectories directory")
    args = parser.parse_args()
    
    plot_wiped_out_trajectories(Path(args.csv_path), Path(args.output_dir), Path(args.trajectories_dir))

if __name__ == "__main__":
    main()
