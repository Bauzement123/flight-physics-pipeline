import os
import argparse
import logging
import pickle
import json
import numpy as np
import pandas as pd
from pathlib import Path

from src.common.config import (
    REPORTS_DIR, 
    AIRPORTS_CACHE_PATH, 
    EUR_LAT_MIN, 
    EUR_LAT_MAX, 
    EUR_LON_MIN, 
    EUR_LON_MAX
)
from src.common.utils import setup_file_logger

def load_dataset(input_file: Path) -> pd.DataFrame:
    """
    Loads flight records from either a CSV or Parquet file.
    """
    logging.info(f"Loading population from: {input_file}")
    if input_file.suffix.lower() == '.parquet':
        df = pd.read_parquet(input_file)
    else:
        df = pd.read_csv(input_file)
    logging.info(f"Loaded {len(df):,} flight records.")
    return df

def calculate_durations(df: pd.DataFrame) -> pd.Series:
    """
    Parses timestamps and calculates flight durations in minutes.
    Handles epoch integers/floats, UNIX seconds, and formatted ISO strings.
    """
    if 'duration' in df.columns:
        return df['duration'].fillna(0)

    if 'firstseen' not in df.columns or 'lastseen' not in df.columns:
        logging.warning("Missing 'firstseen' or 'lastseen' columns. Setting duration to 0.")
        return pd.Series(0, index=df.index)

    # Attempt float/numeric conversion (e.g. UNIX epoch timestamps as strings or floats)
    try:
        firstseen_num = pd.to_numeric(df['firstseen'])
        lastseen_num = pd.to_numeric(df['lastseen'])
        duration = (lastseen_num - firstseen_num) / 60.0
    except (ValueError, TypeError):
        # Fallback to datetime parsing (e.g., "2025-01-01 12:00:00")
        firstseen_dt = pd.to_datetime(df['firstseen'], errors='coerce', utc=True)
        lastseen_dt = pd.to_datetime(df['lastseen'], errors='coerce', utc=True)
        duration = (lastseen_dt - firstseen_dt).dt.total_seconds() / 60.0
        
    return duration.fillna(0)

def calculate_haversine_distance_meters(
    lat1: pd.Series, 
    lon1: pd.Series, 
    lat2: pd.Series, 
    lon2: pd.Series
) -> pd.Series:
    """
    Calculates the great-circle distance between two points in meters using a vectorized Haversine formula.
    """
    R = 6371000.0  # Volumetric mean radius of the Earth in meters
    
    # Convert degrees to radians
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(np.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula components
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = np.sin(dlat / 2.0)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2.0)**2
    c = 2.0 * np.arcsin(np.sqrt(a))
    
    return c * R

def resolve_airport_coordinates(unique_icaos: list) -> dict:
    """
    Resolves coordinates for unique airport ICAOs.
    First checks the local JSON cache. If missing, resolves them using airportsdata or traffic.
    """
    airports_db = {}
    cache_updated = False

    # 1. Try to load from local JSON cache first
    if AIRPORTS_CACHE_PATH.exists():
        logging.info(f"Loading airport coordinates from local cache: {AIRPORTS_CACHE_PATH}")
        try:
            with open(AIRPORTS_CACHE_PATH, 'r', encoding='utf-8') as f:
                airports_db = json.load(f)
        except Exception as e:
            logging.error(f"Failed to read airport coordinates cache: {e}")

    # 2. Check if there are any missing ICAOs
    missing_icaos = [icao for icao in unique_icaos if icao not in airports_db]

    if missing_icaos:
        logging.info(f"Found {len(missing_icaos)} airports missing from local coordinates cache. Resolving them...")
        
        # Try airportsdata library
        resolved_new = {}
        try:
            import airportsdata
            logging.info("Resolving missing airport coordinates using airportsdata library...")
            data = airportsdata.load()
            for icao in missing_icaos:
                if icao in data:
                    resolved_new[icao] = {"lat": data[icao]["lat"], "lon": data[icao]["lon"]}
        except ImportError:
            logging.warning("airportsdata not installed. Falling back to traffic library airports database...")
            try:
                from traffic.data import airports
                # Convert the traffic database to a lookup dict
                traffic_db = {
                    row.icao: {"lat": row.latitude, "lon": row.longitude}
                    for row in airports.data.dropna(subset=['icao']).itertuples()
                }
                for icao in missing_icaos:
                    if icao in traffic_db:
                        resolved_new[icao] = traffic_db[icao]
            except ImportError:
                logging.error("Could not import airportsdata or traffic. Airport coordinates cannot be resolved.")

        # Update the main dictionary and mark cache as updated
        if resolved_new:
            airports_db.update(resolved_new)
            cache_updated = True
            logging.info(f"Successfully resolved {len(resolved_new)} new airport coordinate entries.")

    # 3. Save cache back to file if new entries were added
    if cache_updated:
        try:
            AIRPORTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AIRPORTS_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(airports_db, f, indent=4)
            logging.info(f"Saved updated airport coordinates cache to: {AIRPORTS_CACHE_PATH}")
        except Exception as e:
            logging.error(f"Failed to write airport coordinates cache: {e}")

    return airports_db

def run_first_pass(df: pd.DataFrame, output_dir: Path, name: str) -> pd.DataFrame:
    """
    Performs unfiltered aggregation, rankings text generation, and Pareto stats.
    Returns the unfiltered summary DataFrame.
    """
    # 1. Create standard route string representation
    dep_col = 'estdepartureairport' if 'estdepartureairport' in df.columns else 'estdepatureairport'
    arr_col = 'estarrivalairport'
    if dep_col not in df.columns or arr_col not in df.columns:
        raise KeyError(f"Required airport columns ({dep_col}, {arr_col}) not found in database.")
        
    df['route'] = df[dep_col].astype(str) + " -> " + df[arr_col].astype(str)
    
    # 2. Sub-aggregation (per route, typecode, and airframe) - UNFILTERED
    logging.info("Calculating sub-aggregations by route and airframe (unfiltered)...")
    df_sub = df.groupby(['route', 'typecode', 'icao24']).agg(
        callsigns=('callsign', 'unique'),
        sub_count=('route', 'size'),
        duration_min=('duration', 'min'),
        duration_max=('duration', 'max'),
        duration_median=('duration', 'median')
    )
    
    # 3. Total-aggregation (per route) - UNFILTERED
    logging.info("Calculating route-level totals (unfiltered)...")
    df_total = df.groupby('route').agg(
        total_route_count=('route', 'size'),
        route_duration_min=('duration', 'min'),
        route_duration_max=('duration', 'max'),
        route_duration_median=('duration', 'median'),
        route_duration_sum=('duration', 'sum')
    )
    
    # Join aggregations safely using pandas merge on reset indices to avoid MultiIndex join bugs
    df_sub_reset = df_sub.reset_index()
    df_total_reset = df_total.reset_index()
    df_joint = pd.merge(df_sub_reset, df_total_reset, on='route', how='left').sort_values(
        by=['total_route_count', 'sub_count', 'route'],
        ascending=[False, False, True]
    )
    
    # Save detailed counts - UNFILTERED
    detailed_csv = output_dir / f"{name}_detailed_counts.csv"
    df_joint.to_csv(detailed_csv, index=False)
    logging.info(f"Saved detailed sub-aggregations (unfiltered) to: {detailed_csv}")
    
    # 4. Route Rankings and Summary Compilation - UNFILTERED
    logging.info("Compiling route summaries (unfiltered)...")
    summary_df = df_joint.groupby('route').agg(
        total_route_count=('total_route_count', 'first'),
        route_duration_min=('route_duration_min', 'first'),
        route_duration_max=('route_duration_max', 'first'),
        route_duration_median=('route_duration_median', 'first'),
        route_duration_sum=('route_duration_sum', 'first'),
        unique_typecodes=('typecode', 'unique')
    ).sort_values(by='total_route_count', ascending=False).reset_index()
    
    summary_df['rank'] = summary_df.index + 1
    
    # 5. Route Volume Distribution (Pareto stats) - UNFILTERED
    logging.info("Calculating cumulative volume statistics (unfiltered)...")
    total_routes = len(summary_df)
    total_flights = summary_df['total_route_count'].sum()
    
    summary_df['cum_flights'] = summary_df['total_route_count'].cumsum()
    summary_df['cum_flights_percent'] = (summary_df['cum_flights'] / total_flights) * 100.0
    summary_df['cum_routes_percent'] = (summary_df['rank'] / total_routes) * 100.0
    
    # Export distribution statistics - UNFILTERED
    dist_csv = output_dir / f"{name}_route_distribution.csv"
    summary_df.to_csv(dist_csv, index=False)
    logging.info(f"Saved route distribution analysis (unfiltered) to: {dist_csv}")
    
    # Log Pareto metrics - UNFILTERED
    for p in [10, 20, 50]:
        under_p = summary_df[summary_df['cum_routes_percent'] <= p]
        if not under_p.empty:
            vol_percent = under_p['cum_flights_percent'].iloc[-1]
            logging.info(f"Pareto check (unfiltered): Top {p}% of routes account for {vol_percent:.2f}% of total flights.")

    return summary_df

def export_rankings_txt(summary_df: pd.DataFrame, output_dir: Path, name: str):
    """
    Exports a human-readable route rankings log for the filtered routes,
    including the calculated geodesic distance in kilometers where available.
    """
    ranking_txt = output_dir / f"{name}_route_rankings.txt"
    logging.info(f"Saving route rankings log (filtered, with distance) to: {ranking_txt}")
    with open(ranking_txt, 'w', encoding='utf-8') as f:
        f.write("Rank | Route | Total_Flights | Distance | Duration (Min/Med/Max) | (Unique_Typecodes)\n")
        f.write("-" * 110 + "\n")
        for _, row in summary_df.iterrows():
            typecodes = [str(tc) for tc in row['unique_typecodes'] if pd.notna(tc) and str(tc).lower() != 'nan']
            typecodes_str = ", ".join(typecodes)
            
            dist_val = row.get('distance_m', np.nan)
            dist_str = f"{dist_val / 1000.0:.1f} km" if pd.notna(dist_val) else "N/A"
            
            f.write(f"{int(row['rank'])}. {row['route']} | {row['total_route_count']} | "
                    f"{dist_str} | "
                    f"{row['route_duration_min']:.1f}/{row['route_duration_median']:.1f}/{row['route_duration_max']:.1f} | "
                    f"({typecodes_str})\n")

def filter_routes(summary_df: pd.DataFrame, airports_db: dict) -> pd.DataFrame:
    """
    Filters out circular routes (departure == arrival) and routes that fall outside
    the 5-degree padded European coordinates bounding box.
    """
    initial_len = len(summary_df)
    initial_flights = summary_df['total_route_count'].sum()
    logging.info(f"Applying route filters... Initial unique routes: {initial_len:,} (representing {initial_flights:,} total flights)")

    # Filter A: Remove circular routes
    df_filtered = summary_df[summary_df['estdepartureairport'] != summary_df['estarrivalairport']].copy()
    circular_dropped_routes = initial_len - len(df_filtered)
    circular_dropped_flights = initial_flights - df_filtered['total_route_count'].sum()
    logging.info(f"  Filter A (no circular flights) complete. Dropped {circular_dropped_routes:,} routes ({circular_dropped_flights:,} flights). "
                 f"Remaining: {len(df_filtered):,} routes")

    # Map coordinates exactly once on the remaining non-circular routes before checking bounding box limits
    dep_lats = df_filtered['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    dep_lons = df_filtered['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    arr_lats = df_filtered['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    arr_lons = df_filtered['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))

    # Filter B: 5-degree Padded European bounding box limits check
    in_box_mask = (
        (dep_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (dep_lons.between(EUR_LON_MIN, EUR_LON_MAX)) &
        (arr_lats.between(EUR_LAT_MIN, EUR_LAT_MAX)) &
        (arr_lons.between(EUR_LON_MIN, EUR_LON_MAX))
    )
    
    df_final = df_filtered[in_box_mask].copy()
    out_of_bounds_dropped_routes = len(df_filtered) - len(df_final)
    out_of_bounds_dropped_flights = df_filtered['total_route_count'].sum() - df_final['total_route_count'].sum()
    
    logging.info(f"  Filter B (5-degree padded European bounding box: Lat [{EUR_LAT_MIN}, {EUR_LAT_MAX}], Lon [{EUR_LON_MIN}, {EUR_LON_MAX}]) complete. "
                 f"Dropped {out_of_bounds_dropped_routes:,} routes ({out_of_bounds_dropped_flights:,} flights). "
                 f"Remaining: {len(df_final):,} routes (representing {df_final['total_route_count'].sum():,} flights)")

    return df_final

def run_second_pass(summary_df: pd.DataFrame, output_dir: Path, name: str, airports_db: dict):
    """
    Applies filters, calculates geodesic distances, re-ranks, and saves route_summary.
    """
    logging.info("Starting second-pass filtering and distance enrichment...")
    
    # Filter routes
    summary_filtered_df = filter_routes(summary_df, airports_db)
    
    if summary_filtered_df.empty:
        logging.warning("No routes remained after applying filters. route_summary was not saved.")
        return

    # Assign new rankings to the remaining filtered dataset
    summary_filtered_df['rank'] = range(1, len(summary_filtered_df) + 1)
    
    # Map coordinates for the filtered routes to calculate distances
    dep_lats = summary_filtered_df['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    dep_lons = summary_filtered_df['estdepartureairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    arr_lats = summary_filtered_df['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    arr_lons = summary_filtered_df['estarrivalairport'].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    
    logging.info("Calculating great-circle distances (in meters) for filtered routes...")
    summary_filtered_df['distance_m'] = calculate_haversine_distance_meters(dep_lats, dep_lons, arr_lats, arr_lons)
    
    # Reorder and filter columns to match the exact schema of the reference route_summary.pkl
    desired_cols = [
        'route', 'total_route_count', 'route_duration_min', 'route_duration_max',
        'route_duration_median', 'route_duration_sum', 'unique_typecodes', 'rank', 'distance_m'
    ]
    summary_filtered_df = summary_filtered_df[desired_cols].reset_index(drop=True)
    
    # Save route_summary as CSV and Pickle (dual export)
    csv_out = output_dir / f"{name}_route_summary.csv"
    pkl_out = output_dir / f"{name}_route_summary.pkl"
    
    logging.info(f"Saving filtered route_summary CSV to: {csv_out}")
    summary_filtered_df.to_csv(csv_out, index=False)
    
    logging.info(f"Saving filtered route_summary Pickle to: {pkl_out}")
    with open(pkl_out, 'wb') as f:
        pickle.dump(summary_filtered_df, f)
        
    # Export human-readable text rankings with distance column included (filtered)
    export_rankings_txt(summary_filtered_df, output_dir, name)
        
    logging.info("Successfully completed route summary extraction and enrichment!")

def process_population(input_file: Path, output_dir: Path, name: str):
    """
    Aggregates flight records, outputs unfiltered detailed counts, rankings, and distribution,
    resolves airport coordinates with a local cache, and runs the second-pass filtering and distance calculations.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_file_logger(output_dir)

    # 1. Ingestion
    df = load_dataset(input_file)
    df['duration'] = calculate_durations(df)
    
    # 2. First Pass (Unfiltered Statistics & Output)
    summary_df = run_first_pass(df, output_dir, name)
    
    # 3. Split Route for Coordinate Lookups
    parsed_routes = summary_df['route'].str.split(r"\s*->\s*", expand=True)
    summary_df['estdepartureairport'] = parsed_routes[0].str.strip()
    summary_df['estarrivalairport'] = parsed_routes[1].str.strip()
    
    # 4. Resolve Coordinates
    unique_icaos = pd.concat([summary_df['estdepartureairport'], summary_df['estarrivalairport']]).dropna().unique()
    airports_db = resolve_airport_coordinates(unique_icaos)
    
    # Log unresolved airport codes
    unresolved = [icao for icao in unique_icaos if icao not in airports_db or airports_db[icao].get('lat') is None]
    if unresolved:
        logging.warning(f"Failed to resolve coordinates for {len(unresolved)} unique airports: {unresolved}")
    else:
        logging.info("All unique airports successfully resolved coordinates.")
        
    # 5. Second Pass (Filtering, Re-ranking, Geodesic Distance calculations, and Exporting rankings)
    run_second_pass(summary_df, output_dir, name, airports_db)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze and Summarize Flight Populations")
    parser.add_argument("--input", required=True, help="Path to input flight dataset CSV or Parquet")
    parser.add_argument("--out-dir", help="Directory to save output reports")
    parser.add_argument("--name", default="master_flights", help="Name prefix for output report files")

    args = parser.parse_args()
    
    input_path = Path(args.input)
    out_dir = Path(args.out_dir) if args.out_dir else REPORTS_DIR
    
    process_population(input_path, out_dir, args.name)
