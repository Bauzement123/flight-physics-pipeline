import argparse
import logging
import numpy as np
import pandas as pd
from pathlib import Path

from src.common.config import REPORTS_DIR
from src.common.utils import setup_file_logger

def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculates the great-circle distance between two points on the Earth surface.
    Returns the distance in kilometers.
    """
    R = 6371.0  # Radius of the Earth in kilometers
    
    # Convert coordinates from degrees to radians
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0)**2
    c = 2 * np.arcsin(np.sqrt(a))
    
    return R * c

def resolve_coordinates(df: pd.DataFrame, dep_col: str, arr_col: str) -> pd.DataFrame:
    """
    Queries airports data (using airportsdata or traffic fallbacks) to map
    airport ICAO codes to latitude and longitude coordinates.
    """
    airports_db = {}
    try:
        import airportsdata
        logging.info("Loading airport data using airportsdata...")
        airports_db = airportsdata.load()
    except ImportError:
        logging.warning("airportsdata not available. Falling back to traffic airports...")
        try:
            from traffic.data import airports
            airports_db = {
                row.icao: {"lat": row.latitude, "lon": row.longitude}
                for row in airports.data.dropna(subset=['icao']).itertuples()
            }
        except ImportError:
            logging.error("Could not import airportsdata or traffic. Coordinates cannot be resolved.")
            return df

    df = df.copy()
    df['dep_lat'] = df[dep_col].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    df['dep_lon'] = df[dep_col].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    df['arr_lat'] = df[arr_col].map(lambda x: airports_db.get(x, {}).get('lat', np.nan))
    df['arr_lon'] = df[arr_col].map(lambda x: airports_db.get(x, {}).get('lon', np.nan))
    
    # Drop rows that failed mapping
    initial_len = len(df)
    df = df.dropna(subset=['dep_lat', 'dep_lon', 'arr_lat', 'arr_lon'])
    logging.info(f"Resolved coordinates: mapped {len(df)}/{initial_len} flights.")
    return df

def prepare_matlab_data(input_file: Path, output_file: Path):
    """
    Applies quality and geographic filtering, calculates haversine distance,
    and formats timestamps for circular polar plotting.
    """
    setup_file_logger()
    logging.info(f"Preparing MATLAB data from: {input_file}")
    
    if input_file.suffix.lower() == '.parquet':
        df = pd.read_parquet(input_file)
    else:
        df = pd.read_csv(input_file)

    dep_col = 'estdepartureairport' if 'estdepartureairport' in df.columns else 'estdepatureairport'
    arr_col = 'estarrivalairport'
    if dep_col not in df.columns or arr_col not in df.columns:
        raise KeyError(f"Departure ({dep_col}) or arrival ({arr_col}) columns not found.")

    # 1. Resolve airport coordinates
    df = resolve_coordinates(df, dep_col, arr_col)
    if df.empty:
        logging.warning("No coordinates resolved. Aborting.")
        return

    # 2. Add route flight count on-the-fly for frequency filtering
    logging.info("Calculating route frequency counts...")
    route_counts = df.groupby([dep_col, arr_col]).size().reset_index(name='flight_count')
    df = df.merge(route_counts, on=[dep_col, arr_col], how='left')

    # 3. Calculate flight duration if missing
    if 'flight_duration_minutes' not in df.columns:
        if 'duration' in df.columns:
            df['flight_duration_minutes'] = df['duration']
        elif 'firstseen' in df.columns and 'lastseen' in df.columns:
            try:
                firstseen_num = pd.to_numeric(df['firstseen'])
                lastseen_num = pd.to_numeric(df['lastseen'])
                df['flight_duration_minutes'] = (lastseen_num - firstseen_num) / 60.0
            except (ValueError, TypeError):
                fs_dt = pd.to_datetime(df['firstseen'], errors='coerce', utc=True)
                ls_dt = pd.to_datetime(df['lastseen'], errors='coerce', utc=True)
                df['flight_duration_minutes'] = (ls_dt - fs_dt).dt.total_seconds() / 60.0
        else:
            df['flight_duration_minutes'] = 0.0
            
    df['flight_duration_minutes'] = df['flight_duration_minutes'].fillna(0.0)

    # --- FILTERS ---
    logging.info(f"Applying filters... Initial count: {len(df)}")
    
    # Filter A: Remove circular flights
    df = df[df[dep_col] != df[arr_col]]
    logging.info(f"  Filter A (no circular flights) remaining: {len(df)}")
    
    # Filter B: European lat/lon bounding box
    lat_min, lat_max = 34.0, 72.0
    lon_min, lon_max = -25.0, 45.0
    
    df = df[
        (df['dep_lat'].between(lat_min, lat_max)) &
        (df['arr_lat'].between(lat_min, lat_max)) &
        (df['dep_lon'].between(lon_min, lon_max)) &
        (df['arr_lon'].between(lon_min, lon_max))
    ]
    logging.info(f"  Filter B (European bounding box) remaining: {len(df)}")
    
    # Filter C: Realistic durations (10 to 600 minutes)
    df = df[df['flight_duration_minutes'].between(10.0, 600.0)]
    logging.info(f"  Filter C (duration 10-600 mins) remaining: {len(df)}")
    
    # Calculate Haversine distance
    df['Distance'] = calculate_haversine_distance(df['dep_lat'], df['dep_lon'], df['arr_lat'], df['arr_lon'])
    
    # Filter D: Realistic distances (10 km to 6000 km)
    df = df[df['Distance'].between(10.0, 6000.0)]
    logging.info(f"  Filter D (distance 10-6000 km) remaining: {len(df)}")
    
    # Filter E: Regular routes only (>= 52 flights/year)
    df = df[df['flight_count'] >= 52]
    logging.info(f"  Filter E (weekly route frequency >= 52) remaining: {len(df)}")

    # 4. Circular Time representation
    logging.info("Processing polar time coordinates...")
    time_col = 'firstseen' if 'firstseen' in df.columns else 'departure_time'
    df[time_col] = pd.to_datetime(df[time_col], errors='coerce', utc=True)
    df = df.dropna(subset=[time_col])
    
    df['day_of_year'] = df[time_col].dt.dayofyear
    df['hour_decimal'] = df[time_col].dt.hour + df[time_col].dt.minute / 60.0
    df['time_radians'] = (df['hour_decimal'] / 24.0) * 2.0 * np.pi
    
    # 5. Export
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if output_file.suffix.lower() == '.parquet':
        df.to_parquet(output_file, index=False)
    else:
        df.to_csv(output_file, index=False)
        
    logging.info(f"Successfully exported {len(df):,} records for MATLAB to: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare Flight Data for MATLAB/Visualization")
    parser.add_argument("--input", required=True, help="Path to input flight dataset CSV or Parquet")
    parser.add_argument("--output", required=True, help="Path to save output CSV or Parquet")

    args = parser.parse_args()
    
    prepare_matlab_data(Path(args.input), Path(args.output))
