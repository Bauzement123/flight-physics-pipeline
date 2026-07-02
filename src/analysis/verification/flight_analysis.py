"""
Analysis Module: Flight Distance vs. Maximum Height Analysis
Processes raw flight trajectories to visualize the relationship between
airport great-circle distances and maximum flight baroaltitudes.
Enforces SI units (meters).
"""

import os
import sys
import argparse
import logging
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

# Import central configurations and utilities
from src.common.config import BASE_DIR, GLOBAL_TRAJECTORY_REGISTRY, ROUTE_SUMMARY_PARQUET, GLOBAL_MODEL_REGISTRY
from src.common.utils import load_route_summary, split_route_string
from src.common.registry_utils import (
    load_trajectory_registry,
    load_registered_trajectories,
    load_model_registry
)

logger = logging.getLogger(__name__)

def process_raw_trajectories(
    registry_path: Path,
    summary_path: Path,
    min_height: float = 0.0,
    min_distance: float = None,
    max_distance: float = None,
    top_k_percent: float = 0.0
) -> pd.DataFrame:
    """
    Loads raw trajectories from registry, extracts baroaltitude threshold at top K%, 
    and matches with route summary distances. Filters results based on 
    provided height and distance bounds (SI units).
    
    Args:
        registry_path (Path): Path to the global trajectory registry Parquet.
        summary_path (Path): Path to the master route summary Pickle.
        min_height (float): Minimum threshold for max altitude to filter outliers.
        min_distance (float): Minimum airport distance in meters.
        max_distance (float): Maximum airport distance in meters.
        top_k_percent (float): Percentage threshold to define height metric (e.g. 10.0 for 90th percentile).
        
    Returns:
        pd.DataFrame: Merged dataset containing distance_m and max_height_m.
    """
    logger.info(f"Loading raw trajectory registry: {registry_path}")
    if not registry_path.exists():
        logger.error(f"Registry file not found at: {registry_path}")
        return pd.DataFrame()
        
    try:
        df_registry = load_trajectory_registry()
    except Exception as e:
        logger.error(f"Failed to load trajectory registry: {e}")
        return pd.DataFrame()

    logger.info(f"Loading Route Summary: {summary_path}")
    df_summary = load_route_summary(summary_path)
    if df_summary.empty:
        logger.error("Route Summary DataFrame is empty or could not be loaded.")
        return pd.DataFrame()

    # Map route string e.g. "LEPA -> LEBL" to geodesic distance
    # Build a normalized mapping dictionary for fast lookups
    route_distance_map = {}
    for _, row in df_summary.iterrows():
        route_str = str(row['route']).strip()
        dist_val = row.get('distance_m', None)
        if pd.notna(dist_val):
            route_distance_map[route_str] = float(dist_val)

    # Filter the registry itself to raw flight files
    df_raw_reg = df_registry[df_registry['file_path'].str.endswith('_raw.parquet', na=False)]
    unique_files = df_raw_reg['file_path'].unique()
    logger.info(f"Scanning {len(unique_files)} unique parquet files referenced in raw registry.")

    flight_records = []
    processed_files_count = 0
    skipped_files_count = 0
    
    # Calculate target quantile: top K percent corresponds to (1 - K/100) quantile
    q_val = 1.0 - (top_k_percent / 100.0)
    q_val = max(0.0, min(1.0, q_val)) # clamp to valid range

    # Group matches by file path to load only registered flight IDs
    cols_to_read = ['flight_id', 'baroaltitude', 'estdepartureairport', 'estarrivalairport']
    for abs_path, df_file_filtered in load_registered_trajectories(df_raw_reg, columns=cols_to_read):
        processed_files_count += 1

        if df_file_filtered.empty:
            continue


        # Group by flight_id to aggregate baroaltitude quantile (vectorized for speed)
        grouped_heights = df_file_filtered.groupby('flight_id')['baroaltitude'].quantile(q_val)
        grouped_airports = df_file_filtered.groupby('flight_id').agg(
            dep=('estdepartureairport', 'first'),
            arr=('estarrivalairport', 'first')
        )
        grouped = pd.concat([grouped_heights, grouped_airports], axis=1).reset_index()
        grouped = grouped.rename(columns={'baroaltitude': 'max_height'})

        for _, row in grouped.iterrows():
            f_id = row['flight_id']
            max_h = row['max_height']
            dep_icao = str(row['dep']).strip() if pd.notna(row['dep']) else 'UNK'
            arr_icao = str(row['arr']).strip() if pd.notna(row['arr']) else 'UNK'
            
            # Form the route code matching Route Summary expectations
            route_code = f"{dep_icao} -> {arr_icao}"
            distance = route_distance_map.get(route_code, None)
            
            if distance is None:
                # Try reverse route code mapping as fallback
                reverse_code = f"{arr_icao} -> {dep_icao}"
                distance = route_distance_map.get(reverse_code, None)

            if pd.notna(max_h) and distance is not None:
                # Apply distance filters
                if min_distance is not None and distance < min_distance:
                    continue
                if max_distance is not None and distance > max_distance:
                    continue
                
                # Apply height filter
                if max_h >= min_height:
                    flight_records.append({
                        'flight_id': f_id,
                        'route': route_code,
                        'max_height_m': max_h,
                        'distance_m': distance
                    })

    skipped_files_count = len(df_raw_reg['file_path'].unique()) - processed_files_count
    df_out = pd.DataFrame(flight_records)
    logger.info(f"Extraction complete. Processed files: {processed_files_count}, Skipped files: {skipped_files_count}")
    logger.info(f"Extracted {len(df_out)} flights with valid baroaltitude ({top_k_percent}% threshold) and airport distance within bounds.")
    return df_out

def process_synthesized_trajectories(
    synthesized_registry_path: Path,
    summary_path: Path,
    min_height: float = 0.0,
    min_distance: float = None,
    max_distance: float = None,
    top_k_percent: float = 0.0
) -> pd.DataFrame:
    """
    Loads synthesized trajectories from the synthesized registry, extracts altitude threshold at top K%,
    and matches with route summary distances. Filters results based on provided bounds.
    """
    logger.info(f"Loading synthesized registry: {synthesized_registry_path}")
    if not synthesized_registry_path.exists():
        logger.warning(f"Synthesized registry not found at: {synthesized_registry_path}. Skipping synthetic trajectories.")
        return pd.DataFrame()

    try:
        df_reg = load_model_registry()
    except Exception as e:
        logger.error(f"Failed to load model registry: {e}")
        return pd.DataFrame()

    df_summary = load_route_summary(summary_path)
    if df_summary.empty:
        return pd.DataFrame()

    # Map route string from "DEP -> ARR" to geodesic distance
    route_distance_map = {}
    for _, row in df_summary.iterrows():
        route_str = str(row['route']).strip()
        dist_val = row.get('distance_m', None)
        if pd.notna(dist_val):
            route_distance_map[route_str] = float(dist_val)

    synth_records = []
    processed_count = 0
    skipped_count = 0
    
    # Calculate target quantile
    q_val = 1.0 - (top_k_percent / 100.0)
    q_val = max(0.0, min(1.0, q_val))

    for _, row in df_reg.iterrows():
        rel_path = row['file_path']
        route = row.get('route', 'UNK').strip()  # Format is "DEP-ARR"
        
        # Convert "DEP-ARR" to "DEP -> ARR"
        route_key = route.replace("-", " -> ")
        distance = route_distance_map.get(route_key, None)
        if distance is None:
            # Try reverse mapping
            try:
                parts = route_key.split(" -> ")
                reverse_key = f"{parts[1]} -> {parts[0]}"
                distance = route_distance_map.get(reverse_key, None)
            except Exception:
                distance = None

        # Check distance bounds before loading the file to optimize performance
        if distance is not None:
            if min_distance is not None and distance < min_distance:
                continue
            if max_distance is not None and distance > max_distance:
                continue

        abs_path = BASE_DIR / rel_path
        if not abs_path.exists():
            logger.warning(f"Synthesized file not found: {abs_path}")
            skipped_count += 1
            continue

        try:
            # Synthesized paths have 'altitude' column
            df_file = pd.read_parquet(abs_path, columns=['altitude'])
            max_alt = df_file['altitude'].quantile(q_val)
            processed_count += 1
            
            if pd.notna(max_alt) and distance is not None:
                if max_alt >= min_height:
                    synth_records.append({
                        'route': route_key,
                        'max_height_m': max_alt,
                        'distance_m': distance
                    })
        except Exception as e:
            logger.error(f"Failed to read synthesized file {abs_path.name}: {e}")
            skipped_count += 1

    df_out = pd.DataFrame(synth_records)
    logger.info(f"Synthesized extraction complete. Processed files: {processed_count}, Skipped files: {skipped_count}")
    logger.info(f"Extracted {len(df_out)} synthesized paths with valid altitude ({top_k_percent}% threshold) and distance within bounds.")
    return df_out

def generate_analysis_plot(
    df_raw: pd.DataFrame,
    df_synth: pd.DataFrame,
    output_path: Path,
    top_k_percent: float = 0.0,
    plot_type: str = 'scatter'
):
    """
    Generates a scatter, hexbin, or hist2d plot in SI units containing raw flights and
    optionally synthesized paths, then exports it.
    
    Args:
        df_raw (pd.DataFrame): Dataframe of raw flights.
        df_synth (pd.DataFrame): Dataframe of synthesized paths.
        output_path (Path): Destination path for the saved image.
        top_k_percent (float): The threshold percentage applied.
        plot_type (str): The visual representation type ('scatter', 'hexbin', 'hist2d').
    """
    if df_raw.empty and df_synth.empty:
        logger.error("No raw or synthesized data available to plot.")
        return

    logger.info(f"Generating {plot_type} plot...")
    plt.figure(figsize=(11, 6.5))
    
    # 1. Plot raw flights in blue (scaled to km)
    if not df_raw.empty:
        x_raw = df_raw['distance_m'] / 1000.0
        y_raw = df_raw['max_height_m']
        
        if plot_type == 'hexbin':
            hb = plt.hexbin(
                x_raw, 
                y_raw, 
                gridsize=(40, 25), 
                cmap='Blues', 
                mincnt=1,
                alpha=0.85,
                edgecolors='none'
            )
            cb = plt.colorbar(hb, ax=plt.gca(), label='Flight Count in Bucket')
        elif plot_type == 'hist2d':
            h2d = plt.hist2d(
                x_raw, 
                y_raw, 
                bins=(40, 25), 
                cmap='Blues', 
                cmin=1,
                alpha=0.85
            )
            cb = plt.colorbar(h2d[3], ax=plt.gca(), label='Flight Count in Bucket')
        else: # Default: scatter
            plt.scatter(
                x_raw, 
                y_raw, 
                alpha=0.3, 
                color='#0066cc', 
                edgecolors='none', 
                s=15,
                label='Raw Flights'
            )
        
    # 2. Plot synthesized paths in red (scaled to km)
    if not df_synth.empty:
        plt.scatter(
            df_synth['distance_m'] / 1000.0, 
            df_synth['max_height_m'], 
            alpha=0.85, 
            color='red', 
            edgecolors='black', 
            linewidth=0.5,
            s=35,
            label='Synthesized Paths',
            zorder=5
        )
    
    # Custom visual aesthetics
    title_suffix = f"(Top {top_k_percent}% Height Threshold)" if top_k_percent > 0 else "(Maximum Height)"
    plt.title(f"Airport Geodesic Distance vs. Flight Altitude {title_suffix}", fontsize=12, fontweight='bold', pad=15)
    plt.xlabel("Geodesic Distance between Airports (km)", fontsize=11, labelpad=8)
    
    y_label = f"Flight Altitude (m) - Top {top_k_percent}% threshold" if top_k_percent > 0 else "Maximum Height during Flight (m)"
    plt.ylabel(y_label, fontsize=11, labelpad=8)
    
    # Configure y-axis locators for sublines at every km (1000m) of height
    import matplotlib.ticker as ticker
    plt.gca().yaxis.set_major_locator(ticker.MultipleLocator(5000.0))
    plt.gca().yaxis.set_minor_locator(ticker.MultipleLocator(1000.0))
    
    # Enable major and minor grid lines (sublines for every km)
    plt.grid(True, which='major', linestyle='--', alpha=0.5, color='#aaaaaa')
    plt.grid(True, which='minor', axis='y', linestyle=':', alpha=0.3, color='#cccccc')
    
    # Calculate stats for text box
    num_raw = len(df_raw) if not df_raw.empty else 0
    num_synth = len(df_synth) if not df_synth.empty else 0
    
    stats_lines = []
    if num_raw > 0:
        corr = df_raw['distance_m'].corr(df_raw['max_height_m'])
        stats_lines.append(f"Raw Flights: {num_raw:,} (r={corr:.3f})")
    if num_synth > 0:
        stats_lines.append(f"Synthesized Paths: {num_synth:,}")
        
    stats_text = "\n".join(stats_lines)
    if stats_text:
        plt.gca().text(
            0.05, 0.95, stats_text, 
            transform=plt.gca().transAxes, 
            fontsize=9, 
            verticalalignment='top',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8, edgecolor='#cccccc')
        )
        
    # Show legend only if items exist
    handles, labels = plt.gca().get_legend_handles_labels()
    if handles:
        plt.legend(loc='lower right', framealpha=0.9)
    
    # Format axes with thousand separators
    plt.gca().xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"{int(x):,}"))
    plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda y, p: f"{int(y):,}"))

    # Save to disk
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    logger.info(f"✓ {plot_type.capitalize()} plot successfully saved to: {output_path}")

def print_summary_statistics(df_data: pd.DataFrame, top_k_percent: float = 0.0):
    """Prints basic summary statistics for the processed cohort."""
    if df_data.empty:
        return
        
    metric_name = f"Barometric height - Top {top_k_percent}% threshold" if top_k_percent > 0 else "Maximum barometric height"
    
    print("\n" + "="*50)
    print("      FLIGHT TRAJECTORY ANALYSIS SUMMARY STATISTICS")
    print("="*50)
    print(f"Total flights analyzed      : {len(df_data):,}")
    print(f"Distance between airports (km):")
    print(f"  - Min                     : {df_data['distance_m'].min() / 1000.0:,.1f}")
    print(f"  - Max                     : {df_data['distance_m'].max() / 1000.0:,.1f}")
    print(f"  - Median                  : {df_data['distance_m'].median() / 1000.0:,.1f}")
    print(f"  - Mean                    : {df_data['distance_m'].mean() / 1000.0:,.1f}")
    print(f"{metric_name} (m):")
    print(f"  - Min                     : {df_data['max_height_m'].min():,.1f}")
    print(f"  - Max                     : {df_data['max_height_m'].max():,.1f}")
    print(f"  - Median                  : {df_data['max_height_m'].median():,.1f}")
    print(f"  - Mean                    : {df_data['max_height_m'].mean():,.1f}")
    print("="*50 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Analyze raw flights and plot distance vs. max height in SI units.")
    parser.add_argument(
        "--registry", 
        default=str(GLOBAL_TRAJECTORY_REGISTRY), 
        help="Path to global raw trajectory registry parquet file"
    )
    parser.add_argument(
        "--synthesized-registry", 
        default=str(GLOBAL_MODEL_REGISTRY), 
        help="Path to global synthesized trajectory registry parquet file"
    )
    parser.add_argument(
        "--summary", 
        default=str(ROUTE_SUMMARY_PARQUET), 
        help="Path to master route summary pickle file"
    )
    parser.add_argument(
        "--output-dir", 
        default=str(BASE_DIR / "data" / "analysis" / "plots"), 
        help="Destination directory for the generated plot"
    )
    parser.add_argument(
        "--min-height", 
        type=float, 
        default=0.0, 
        help="Minimum peak height (meters) to filter out incomplete flights"
    )
    parser.add_argument(
        "--min-distance", 
        type=float, 
        default=None, 
        help="Minimum airport geodesic distance (kilometers) to filter flights"
    )
    parser.add_argument(
        "--max-distance", 
        type=float, 
        default=None, 
        help="Maximum airport geodesic distance (kilometers) to filter flights"
    )
    parser.add_argument(
        "--no-synthesized", 
        action="store_false", 
        dest="show_synthesized", 
        help="Disable loading and plotting synthesized route baselines (red dots)"
    )
    parser.add_argument(
        "--top-k-percent", 
        type=float, 
        default=0.0, 
        help="Percentage threshold to define height metric (e.g. 10.0 for 90th percentile, 0.0 for absolute maximum)"
    )
    parser.add_argument(
        "--plot-type", 
        choices=["scatter", "hexbin", "hist2d"], 
        default="scatter", 
        help="Visual style for raw flight distribution. Choices: scatter, hexbin, hist2d. Default: scatter"
    )
    
    args = parser.parse_args()

    registry_path = Path(args.registry)
    synth_registry_path = Path(args.synthesized_registry)
    summary_path = Path(args.summary)
    output_dir = Path(args.output_dir)
    
    # Generate dynamic plot filename based on the CLI parameters (in km)
    min_val_str = f"{args.min_distance:.0f}" if args.min_distance is not None else "0"
    max_val_str = f"{args.max_distance:.0f}" if args.max_distance is not None else "inf"
    topk_val_str = f"{args.top_k_percent:.0f}" if args.top_k_percent is not None else "0"
    synth_val_str = "true" if args.show_synthesized else "false"
    
    filename = f"dist_min_{min_val_str}_max_{max_val_str}_topk_{topk_val_str}_plot_{args.plot_type}_synth_{synth_val_str}.svg"
    output_path = output_dir / filename
    
    # Convert km distance parameters to meters for internal database queries (which are in SI units)
    min_dist_m = args.min_distance * 1000.0 if args.min_distance is not None else None
    max_dist_m = args.max_distance * 1000.0 if args.max_distance is not None else None
    
    logger.info("Initializing flight trajectory analysis workflow...")
    logger.info(f"Targeting Distance Bounds (km): [{min_val_str}, {max_val_str}]")
    logger.info(f"Show Synthesized Paths: {synth_val_str.upper()}")
    logger.info(f"Top K% Height Percentile Parameter: {args.top_k_percent}% (Quantile: {1.0 - args.top_k_percent/100.0:.2f})")
    
    # Process raw files
    df_raw = process_raw_trajectories(
        registry_path=registry_path,
        summary_path=summary_path,
        min_height=args.min_height,
        min_distance=min_dist_m,
        max_distance=max_dist_m,
        top_k_percent=args.top_k_percent
    )
    
    # Process synthesized files if toggled on
    df_synth = pd.DataFrame()
    if args.show_synthesized:
        df_synth = process_synthesized_trajectories(
            synthesized_registry_path=synth_registry_path,
            summary_path=summary_path,
            min_height=args.min_height,
            min_distance=min_dist_m,
            max_distance=max_dist_m,
            top_k_percent=args.top_k_percent
        )
    
    if df_raw.empty and df_synth.empty:
        logger.error("No valid raw or synthesized flight trajectory matches within bounds. Plot generation aborted.")
        sys.exit(1)
        
    # Print raw summary statistics
    if not df_raw.empty:
        print_summary_statistics(df_raw, args.top_k_percent)
    
    # Generate and save chart
    generate_analysis_plot(df_raw, df_synth, output_path, args.top_k_percent, args.plot_type)

if __name__ == "__main__":
    # Configure logging locally inside the script entry point to prevent pollution upon import
    setup_file_logger(log_filename="analysis.log")
    main()
