"""
Analysis Module: Route Popularity vs. Distance Binned Profile
Aggregates route flight counts and unique route frequencies into distance bins.
Supports standard and cumulative binning modes.
"""

import os
import sys
import argparse
import logging
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Import configurations
from src.common.config import BASE_DIR, ROUTE_SUMMARY_PARQUET
from src.common.utils import load_route_summary

logger = logging.getLogger(__name__)

def load_and_filter_data(summary_path: Path, min_frequency: int) -> pd.DataFrame:
    """Loads route summary and filters out low-frequency routes."""
    logger.info(f"Loading Route Summary: {summary_path}")
    if not summary_path.exists():
        logger.error(f"Route summary file not found at: {summary_path}")
        return pd.DataFrame()

    df = load_route_summary(summary_path)
    if df.empty:
        logger.error("Route Summary is empty.")
        return pd.DataFrame()

    initial_len = len(df)
    # Filter by minimum frequency
    df = df[df['total_route_count'] >= min_frequency]
    logger.info(f"Filtered out {initial_len - len(df)} routes with popularity < {min_frequency}. {len(df)} routes remaining.")
    return df

def aggregate_bins(df: pd.DataFrame, bin_size: float, cumulative: bool) -> pd.DataFrame:
    """Aggregates flights and routes into distance bins (standard or cumulative)."""
    if df.empty or 'distance_m' not in df.columns:
        return pd.DataFrame()

    # Convert distance to km
    df = df.copy()
    df['distance_km'] = df['distance_m'] / 1000.0
    df = df.dropna(subset=['distance_km'])

    if df.empty:
        return pd.DataFrame()

    max_dist_km = df['distance_km'].max()
    num_bins = int(np.ceil(max_dist_km / bin_size))
    
    bin_records = []

    if cumulative:
        # Cumulative bins: [0, bin_end)
        for i in range(1, num_bins + 1):
            bin_end = i * bin_size
            sub_df = df[df['distance_km'] < bin_end]
            
            flights_sum = sub_df['total_route_count'].sum()
            routes_count = sub_df['route'].nunique()
            
            bin_records.append({
                'bin_index': i - 1,
                'bin_label': f"<{int(bin_end)}",
                'flights': flights_sum,
                'routes': routes_count,
                'bin_start': 0.0,
                'bin_end': bin_end
            })
    else:
        # Standard bins: [bin_start, bin_end)
        for i in range(num_bins):
            bin_start = i * bin_size
            bin_end = (i + 1) * bin_size
            sub_df = df[(df['distance_km'] >= bin_start) & (df['distance_km'] < bin_end)]
            
            flights_sum = sub_df['total_route_count'].sum()
            routes_count = sub_df['route'].nunique()
            
            bin_records.append({
                'bin_index': i,
                'bin_label': f"{int(bin_start)}-{int(bin_end)}",
                'flights': flights_sum,
                'routes': routes_count,
                'bin_start': bin_start,
                'bin_end': bin_end
            })

    return pd.DataFrame(bin_records)

def plot_dual_axis(
    df_binned: pd.DataFrame, 
    bin_size: float, 
    min_freq: int, 
    cumulative: bool, 
    output_path: Path
):
    """Generates and saves the dual Y-axis plot."""
    if df_binned.empty:
        logger.error("No aggregated data available to plot.")
        return

    logger.info("Generating dual Y-axis plot...")
    fig, ax1 = plt.subplots(figsize=(12, 6.5))
    ax2 = ax1.twinx()

    indices = np.arange(len(df_binned))
    bar_width = 0.55

    # Create a nice gradient-colored bar chart for Flight Volume on primary Y-axis (ax1)
    # Normalized heights for color map mapping
    flights_data = df_binned['flights'].values
    max_flights = max(flights_data) if len(flights_data) > 0 and max(flights_data) > 0 else 1
    
    # Generate colors based on flight volume height
    cmap = plt.colormaps.get_cmap('GnBu') # Green-Blue gradient
    bar_colors = [cmap(0.4 + 0.5 * (val / max_flights)) for val in flights_data]

    bars = ax1.bar(
        indices, 
        df_binned['flights'], 
        width=bar_width, 
        color=bar_colors, 
        edgecolor='#1d637d', 
        alpha=0.85, 
        label='Flight Volume (left)'
    )

    # Plot unique routes on secondary Y-axis (ax2)
    line = ax2.plot(
        indices, 
        df_binned['routes'], 
        color='#ff5a36', 
        marker='o', 
        linewidth=2.2, 
        markersize=6, 
        label='Unique Routes (right)'
    )

    # Title & Subtitles
    mode_str = "Cumulative" if cumulative else "Standard"
    plt.title(f"Flight Volume & Route Diversity vs. Geodesic Distance ({mode_str} Bins)", fontsize=13, fontweight='bold', pad=18)
    
    # Axis labels
    ax1.set_xlabel("Route Geodesic Distance Range (km)", fontsize=11, labelpad=10)
    ax1.set_ylabel("Total Flight Volume (cumulative flights)", fontsize=11, color='#125169', labelpad=8)
    ax2.set_ylabel("Unique Routes Count (airport pairs)", fontsize=11, color='#e03e1b', labelpad=8)

    # Tick configurations
    ax1.set_xticks(indices)
    ax1.set_xticklabels(df_binned['bin_label'], rotation=45, ha='right', fontsize=9)
    
    # Color tick parameters to match metrics
    ax1.tick_params(axis='y', labelcolor='#125169')
    ax2.tick_params(axis='y', labelcolor='#e03e1b')
    
    # Format tick labels with thousand separators
    ax1.get_yaxis().set_major_formatter(plt.FuncFormatter(lambda x, p: f"{int(x):,}"))
    ax2.get_yaxis().set_major_formatter(plt.FuncFormatter(lambda y, p: f"{int(y):,}"))

    # Grids: Draw light dashed lines aligned with flight volume ticks
    ax1.grid(True, which='major', axis='y', linestyle='--', alpha=0.5, color='#cccccc')
    ax1.set_axisbelow(True)

    # Legends merging
    lines_labels = [bars] + line
    labels = [l.get_label() for l in lines_labels]
    ax1.legend(lines_labels, labels, loc='upper right', framealpha=0.9)

    # Stats / Configuration box in plot
    stats_text = (
        f"Bin Size: {bin_size} km\n"
        f"Min Frequency Filter: {min_freq} flights\n"
        f"Total Bins: {len(df_binned)}"
    )
    ax1.text(
        0.02, 0.95, stats_text, 
        transform=ax1.transAxes, 
        fontsize=9, 
        verticalalignment='top',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8, edgecolor='#cccccc')
    )

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, format='svg')
    plt.close()
    
    logger.info(f"✓ Binned popularity plot successfully saved to: {output_path}")

def print_aggregation_table(df_binned: pd.DataFrame, cumulative: bool):
    """Prints a clean summary table of binned data to the console."""
    if df_binned.empty:
        return
        
    print("\n" + "="*70)
    print(f"      ROUTE POPULARITY BINS REPORT ({'CUMULATIVE' if cumulative else 'STANDARD'})")
    print("="*70)
    print(f"{'Bin Range (km)':<18} | {'Total Flights':<18} | {'Unique Routes':<18}")
    print("-"*70)
    for _, row in df_binned.iterrows():
        print(f"{row['bin_label']:<18} | {int(row['flights']):<18,} | {int(row['routes']):<18,}")
    print("="*70 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Analyze route popularity against binned distance ranges.")
    parser.add_argument(
        "--summary", 
        default=str(ROUTE_SUMMARY_PARQUET), 
        help="Path to master route summary parquet or pickle file"
    )
    parser.add_argument(
        "--output-dir", 
        default=str(BASE_DIR / "data" / "analysis" / "plots"), 
        help="Destination directory for the generated plot"
    )
    parser.add_argument(
        "--bin-size", 
        type=float, 
        default=100.0, 
        help="Distance bin size (width) in kilometers. Default: 100.0"
    )
    parser.add_argument(
        "--cumulative", 
        action="store_true", 
        help="Enable cumulative binning (0 to end) instead of window binning"
    )
    parser.add_argument(
        "--min-frequency", 
        type=int, 
        default=1, 
        help="Minimum flight count required for a route to be included. Default: 1"
    )
    
    args = parser.parse_args()

    summary_path = Path(args.summary)
    output_dir = Path(args.output_dir)
    
    # Generate dynamic filename
    cum_str = "true" if args.cumulative else "false"
    filename = f"popularity_dist_bin_{args.bin_size:.0f}_minfreq_{args.min_frequency}_cum_{cum_str}.svg"
    output_path = output_dir / filename

    logger.info("Initializing Route Popularity vs. Distance Analysis workflow...")
    logger.info(f"Bin size: {args.bin_size} km, Cumulative: {args.cumulative}, Min Frequency: {args.min_frequency}")

    # Load and filter
    df = load_and_filter_data(summary_path, args.min_frequency)
    if df.empty:
        logger.error("No data matching minimum frequency criteria. Program aborted.")
        sys.exit(1)

    # Bin aggregation
    df_binned = aggregate_bins(df, args.bin_size, args.cumulative)
    if df_binned.empty:
        logger.error("Bin aggregation failed or returned empty dataframe.")
        sys.exit(1)

    # Console statistics output
    print_aggregation_table(df_binned, args.cumulative)

    # Plot generation
    plot_dual_axis(df_binned, args.bin_size, args.min_frequency, args.cumulative, output_path)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [POPULARITY] - %(levelname)s - %(message)s")
    main()
