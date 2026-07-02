"""
Analysis Module: Flight Level Distribution Boxplot Analysis
Analyzes and visualizes the statistical distribution of stable cruise Flight Levels (FLs)
across individual route corridors (ordered by distance) or distance brackets using boxplots.
"""

import os
import sys
import argparse
import logging
import math
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# Central configurations and loaders
from src.common.config import BASE_DIR, GLOBAL_TRAJECTORY_REGISTRY, ROUTE_SUMMARY_PARQUET
from src.common.utils import load_route_summary
from src.common.registry_utils import load_trajectory_registry, load_registered_trajectories

logger = logging.getLogger(__name__)

def process_flight_levels(
    registry_path: Path,
    summary_path: Path,
    top_k_percent: float = 10.0,
    fl_step: float = 10.0
) -> pd.DataFrame:
    """
    Loads raw flight trajectories, extracts the cruise altitude threshold at (100 - top_k_percent)%,
    converts to Flight Levels (FL), matches with route distances, and rounds FLs.
    """
    logger.info(f"Loading raw trajectory registry: {registry_path}")
    if not registry_path.exists():
        logger.error(f"Registry file not found at: {registry_path}")
        return pd.DataFrame()

    df_registry = load_trajectory_registry(registry_path)

    # Filter to raw flight files
    df_raw_reg = df_registry[df_registry['file_path'].str.endswith('_raw.parquet', na=False)]
    if df_raw_reg.empty:
        logger.error("No raw trajectory entries found in registry.")
        return pd.DataFrame()

    # Load summary for distance matching
    logger.info(f"Loading Route Summary: {summary_path}")
    df_summary = load_route_summary(summary_path)
    if df_summary.empty:
        logger.error("Route Summary is empty or failed to load.")
        return pd.DataFrame()

    # Map route code "EDDF -> LEBL" to geodesic distance
    route_distance_map = {}
    for _, row in df_summary.iterrows():
        route_str = str(row['route']).strip()
        dist_val = row.get('distance_m', None)
        if pd.notna(dist_val):
            route_distance_map[route_str] = float(dist_val)

    # Process quantile threshold for stable cruise altitude
    q_val = 1.0 - (top_k_percent / 100.0)
    q_val = max(0.0, min(1.0, q_val)) # clamp to valid range

    flight_records = []
    processed_files = 0
    skipped_files = 0
    unique_files = df_raw_reg['file_path'].unique()
    logger.info(f"Scanning {len(unique_files)} unique parquet files referenced in registry...")

    cols_to_read = ['flight_id', 'baroaltitude', 'estdepartureairport', 'estarrivalairport']
    for abs_path, df_file_filtered in load_registered_trajectories(df_raw_reg, columns=cols_to_read):
        processed_files += 1

        required_cols = ['flight_id', 'baroaltitude', 'estdepartureairport', 'estarrivalairport']
        missing_cols = [c for c in required_cols if c not in df_file_filtered.columns]
        if missing_cols:
            continue

        if df_file_filtered.empty:
            continue

        # Extract stable height per flight (quantile to filter noise/climb/descent)
        grouped_heights = df_file_filtered.groupby('flight_id')['baroaltitude'].quantile(q_val)
        grouped_airports = df_file_filtered.groupby('flight_id').agg(
            dep=('estdepartureairport', 'first'),
            arr=('estarrivalairport', 'first')
        )
        grouped = pd.concat([grouped_heights, grouped_airports], axis=1).reset_index()
        grouped = grouped.rename(columns={'baroaltitude': 'cruise_height_m'})

        for _, row in grouped.iterrows():
            f_id = row['flight_id']
            height_m = row['cruise_height_m']
            dep_icao = str(row['dep']).strip() if pd.notna(row['dep']) else 'UNK'
            arr_icao = str(row['arr']).strip() if pd.notna(row['arr']) else 'UNK'

            route_code = f"{dep_icao} -> {arr_icao}"
            distance = route_distance_map.get(route_code, None)
            if distance is None:
                reverse_code = f"{arr_icao} -> {dep_icao}"
                distance = route_distance_map.get(reverse_code, None)

            if pd.notna(height_m) and distance is not None:
                # Convert height in meters to Flight Level (FL = ft / 100)
                fl_raw = height_m / 30.48
                # Round to nearest fl_step bucket to filter small fluctuations
                fl_bucketed = round(fl_raw / fl_step) * fl_step

                flight_records.append({
                    'flight_id': f_id,
                    'route': route_code,
                    'distance_m': distance,
                    'fl_raw': fl_raw,
                    'fl_bucketed': fl_bucketed
                })

    df_out = pd.DataFrame(flight_records)
    skipped_files = len(df_raw_reg['file_path'].unique()) - processed_files
    logger.info(f"Extraction complete. Files processed: {processed_files}, skipped: {skipped_files}")
    logger.info(f"Extracted {len(df_out)} flights with valid cruise Flight Levels.")
    return df_out

def generate_boxplot_chart(
    df_data: pd.DataFrame,
    dist_step: float,
    fl_step: float,
    top_k_percent: float,
    output_path: Path
):
    """
    Renders and saves vertical boxplot distribution chart.
    Supports either route-by-route sorted columns or distance bucket columns.
    """
    if df_data.empty:
        logger.error("No flight level data available to plot.")
        return

    logger.info("Grouping and structuring boxplot data...")

    # Set up variables for plotting
    data_to_plot = []
    labels = []
    metadata = []  # stores (mean_distance, count) for subtitle or console reports

    if dist_step is not None:
        # Binned Mode
        df_data['dist_km'] = df_data['distance_m'] / 1000.0
        max_dist_km = df_data['dist_km'].max()
        num_bins = math.ceil(max_dist_km / dist_step)

        # Group data into distance brackets
        for i in range(num_bins):
            min_b = i * dist_step
            max_b = (i + 1) * dist_step
            bin_label = f"{min_b:.0f}-{max_b:.0f} km"
            
            df_bin = df_data[(df_data['dist_km'] >= min_b) & (df_data['dist_km'] < max_b)]
            if not df_bin.empty:
                data_to_plot.append(df_bin['fl_bucketed'].values)
                labels.append(bin_label)
                metadata.append(( (min_b + max_b)/2.0, len(df_bin) ))

        fig_width = max(10, len(labels) * 1.5)
        title_type = f"Distance Brackets (Step: {dist_step:.0f} km)"
    else:
        # Route-by-route Mode (sorted by increasing geodesic distance)
        # Calculate mean distance for each route to sort them correctly
        route_stats = df_data.groupby('route').agg(
            mean_dist_m=('distance_m', 'mean'),
            flight_count=('flight_id', 'count')
        ).sort_values('mean_dist_m')

        for r_code, row in route_stats.iterrows():
            df_r = df_data[df_data['route'] == r_code]
            data_to_plot.append(df_r['fl_bucketed'].values)
            dist_km = row['mean_dist_m'] / 1000.0
            
            # Label format: "ROUTE (dist km)"
            labels.append(f"{r_code}\n({dist_km:.0f} km)")
            metadata.append((dist_km, int(row['flight_count'])))

        # Dynamic figure size adjusting to the number of route columns
        fig_width = max(11, len(labels) * 0.45)
        title_type = "Route Corridors (Sorted by Distance)"

    fig, ax = plt.subplots(figsize=(fig_width, 7.5))

    # Render custom candlestick-style boxplot
    # whis=[5, 95] sets whiskers to 5th and 95th percentiles of cruise levels
    try:
        bp = ax.boxplot(
            data_to_plot,
            tick_labels=labels,
            whis=[5, 95],
            patch_artist=True,
            showmeans=False,
            flierprops=dict(marker='o', markerfacecolor='#999999', markersize=4, markeredgecolor='none', alpha=0.5)
        )
    except TypeError:
        # Fallback for Matplotlib < 3.9
        bp = ax.boxplot(
            data_to_plot,
            labels=labels,
            whis=[5, 95],
            patch_artist=True,
            showmeans=False,
            flierprops=dict(marker='o', markerfacecolor='#999999', markersize=4, markeredgecolor='none', alpha=0.5)
        )

    # Style boxes (the candle bodies)
    for box in bp['boxes']:
        box.set(facecolor='#dbe9f6', edgecolor='#1d4e89', linewidth=1.2)

    # Style medians (line inside the box)
    for median in bp['medians']:
        median.set(color='#d62728', linewidth=1.8)

    # Style whiskers and caps (the wicks)
    for whisker in bp['whiskers']:
        whisker.set(color='#1d4e89', linestyle='--', linewidth=1.0)
    for cap in bp['caps']:
        cap.set(color='#1d4e89', linewidth=1.2)

    # Title & Labels
    plt.title(f"Cruise Flight Level (FL) Distribution vs. {title_type}", fontsize=13, fontweight='bold', pad=18)
    ax.set_ylabel("Cruise Flight Level (FL)", fontsize=11, labelpad=8)
    
    if dist_step is not None:
        ax.set_xlabel("Airport Geodesic Distance Bracket (km)", fontsize=11, labelpad=10)
        plt.xticks(fontsize=10)
    else:
        ax.set_xlabel("Unique Airport Route Corridor (Shortest to Longest)", fontsize=11, labelpad=12)
        # Rotate dense labels for readability
        plt.xticks(rotation=90, fontsize=7.5)

    # Format Y-axis with Flight Level ticks
    ax.yaxis.set_major_locator(ticker.MultipleLocator(20.0)) # ticks every FL20
    ax.yaxis.set_minor_locator(ticker.MultipleLocator(10.0)) # sub-ticks every FL10
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, p: f"FL{int(y):03d}"))

    # Enable grid lines
    ax.grid(True, which='major', axis='y', linestyle='--', alpha=0.5, color='#aaaaaa')
    ax.grid(True, which='minor', axis='y', linestyle=':', alpha=0.3, color='#cccccc')
    ax.set_axisbelow(True)

    # Upper/Lower limits
    all_flat = np.concatenate(data_to_plot)
    y_min = math.floor(all_flat.min() / 50.0) * 50.0
    y_max = math.ceil(all_flat.max() / 50.0) * 50.0
    ax.set_ylim(max(0.0, y_min - 20), y_max + 20)

    # Add text statistics box
    total_flights = len(df_data)
    total_groups = len(labels)
    stats_text = (
        f"Total Flights: {total_flights:,}\n"
        f"Total Groups: {total_groups}\n"
        f"FL Bucket Step: {fl_step:.0f} FL\n"
        f"Cruise Quantile: {100.0 - top_k_percent:.1f}%\n"
        f"Whiskers: 5th-95th Pct"
    )
    ax.text(
        0.02, 0.96, stats_text,
        transform=ax.transAxes,
        fontsize=9,
        verticalalignment='top',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.85, edgecolor='#cccccc')
    )

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, format='svg')
    plt.close()

    logger.info(f"✓ Flight Level distribution plot saved successfully to: {output_path}")

def print_text_report(df_data: pd.DataFrame, dist_step: float):
    """Prints a formatted text summary statistics report of Flight Level distributions."""
    if df_data.empty:
        return

    print("\n" + "="*85)
    print("                 CRUISE FLIGHT LEVEL DISTRIBUTION SUMMARY REPORT")
    print("="*85)
    print(f"Total Flights Processed: {len(df_data):,}")
    print("-"*85)
    
    if dist_step is not None:
        print(f"{'Distance Bracket':<18} | {'Flights':<8} | {'Min FL':<8} | {'5% (P05)':<8} | {'25% (P25)':<9} | {'Median FL':<9} | {'75% (P75)':<9} | {'95% (P95)':<8} | {'Max FL':<8}")
        print("-"*100)
        df_data['dist_km'] = df_data['distance_m'] / 1000.0
        max_dist_km = df_data['dist_km'].max()
        num_bins = math.ceil(max_dist_km / dist_step)

        for i in range(num_bins):
            min_b = i * dist_step
            max_b = (i + 1) * dist_step
            bin_label = f"{min_b:.0f}-{max_b:.0f} km"
            
            df_bin = df_data[(df_data['dist_km'] >= min_b) & (df_data['dist_km'] < max_b)]
            if not df_bin.empty:
                fls = df_bin['fl_bucketed']
                print(f"{bin_label:<18} | {len(fls):<8,} | FL{int(fls.min()):03d} | FL{int(fls.quantile(0.05)):03d} | FL{int(fls.quantile(0.25)):03d} | FL{int(fls.median()):03d} | FL{int(fls.quantile(0.75)):03d} | FL{int(fls.quantile(0.95)):03d} | FL{int(fls.max()):03d}")
    else:
        print(f"{'Route Corridor':<18} | {'Flights':<8} | {'Min FL':<8} | {'5% (P05)':<8} | {'25% (P25)':<9} | {'Median FL':<9} | {'75% (P75)':<9} | {'95% (P95)':<8} | {'Max FL':<8}")
        print("-"*100)
        route_stats = df_data.groupby('route').agg(
            mean_dist_m=('distance_m', 'mean'),
            flight_count=('flight_id', 'count')
        ).sort_values('mean_dist_m')

        for r_code, row in route_stats.iterrows():
            df_r = df_data[df_data['route'] == r_code]
            fls = df_r['fl_bucketed']
            print(f"{r_code:<18} | {len(fls):<8,} | FL{int(fls.min()):03d} | FL{int(fls.quantile(0.05)):03d} | FL{int(fls.quantile(0.25)):03d} | FL{int(fls.median()):03d} | FL{int(fls.quantile(0.75)):03d} | FL{int(fls.quantile(0.95)):03d} | FL{int(fls.max()):03d}")

    print("="*100 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Analyze stable cruise Flight Level (FL) distribution.")
    parser.add_argument(
        "--registry", 
        default=str(GLOBAL_TRAJECTORY_REGISTRY), 
        help="Path to global raw trajectory registry parquet file"
    )
    parser.add_argument(
        "--summary", 
        default=str(ROUTE_SUMMARY_PARQUET), 
        help="Path to master flight route summary parquet database"
    )
    parser.add_argument(
        "--top-k-percent", 
        type=float, 
        default=10.0, 
        help="Quantile threshold to extract stable cruise altitude per flight (e.g. 10.0 for 90th percentile)"
    )
    parser.add_argument(
        "--fl-step", 
        type=float, 
        default=10.0, 
        help="Flight Level bucket rounding step (default 10.0 is FL10, e.g. FL310, FL320...)"
    )
    parser.add_argument(
        "--dist-step", 
        type=float, 
        default=None, 
        help="Optional airport geodesic distance bin size in km (e.g. 200). If omitted, plots route-by-route sorted by distance."
    )
    parser.add_argument(
        "--output-dir", 
        default=str(BASE_DIR / "data" / "analysis" / "plots"), 
        help="Destination folder for exported SVG plots"
    )
    args = parser.parse_args()

    registry_path = Path(args.registry)
    summary_path = Path(args.summary)
    output_dir = Path(args.output_dir)

    # Determine dynamic filename
    if args.dist_step is not None:
        filename = f"fl_dist_step_{args.dist_step:.0f}_topk_{args.top_k_percent:.0f}.svg"
    else:
        filename = f"fl_dist_routes_topk_{args.top_k_percent:.0f}.svg"
        
    output_path = output_dir / filename

    logger.info("Initializing Flight Level Distribution Boxplot Analysis workflow...")
    
    # Process
    df_data = process_flight_levels(
        registry_path=registry_path,
        summary_path=summary_path,
        top_k_percent=args.top_k_percent,
        fl_step=args.fl_step
    )
    
    if df_data.empty:
        logger.error("No trajectory metrics extracted. Program aborted.")
        sys.exit(1)

    # Print summary report to console
    print_text_report(df_data, args.dist_step)

    # Render and save chart
    generate_boxplot_chart(
        df_data=df_data,
        dist_step=args.dist_step,
        fl_step=args.fl_step,
        top_k_percent=args.top_k_percent,
        output_path=output_path
    )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [FL_DIST] - %(levelname)s - %(message)s")
    main()
