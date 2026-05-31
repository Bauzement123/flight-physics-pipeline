"""
Shared Flight Serialization & Metadata Adapter
Provides unified conversion interfaces between Pandas DataFrames and pycontrails.Flight objects,
centralizing Parquet I/O and preventing pyarrow Timestamp JSON serialization crashes.
"""

import pandas as pd
import logging
from pathlib import Path
from pycontrails import Flight

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger(__name__)

def dataframe_to_pycontrails(df_flight: pd.DataFrame, typecode: str = "UNKNOWN") -> Flight:
    """
    Transforms a single cleaned flight DataFrame into a pycontrails Flight object.
    
    Args:
        df_flight (pd.DataFrame): Cleaned trajectory for a SINGLE flight.
        typecode (str): The aircraft type designator (e.g., 'B777'). Crucial for PSFlight.
        
    Returns:
        Flight: A pycontrails Flight object.
    """
    if df_flight is None or df_flight.empty:
        logger.warning("Empty dataframe provided to adapter.")
        return None
        
    # Extract metadata from the first row
    icao24 = df_flight['icao24'].iloc[0] if 'icao24' in df_flight.columns else 'UNK'
    callsign = df_flight['callsign'].iloc[0] if 'callsign' in df_flight.columns else 'UNK'
    flight_id = df_flight['flight_id'].iloc[0] if 'flight_id' in df_flight.columns else f"{icao24}_{callsign}"
    firstseen = df_flight['firstseen'].iloc[0] if 'firstseen' in df_flight.columns else None
    lastseen = df_flight['lastseen'].iloc[0] if 'lastseen' in df_flight.columns else None
    dep = df_flight['estdepartureairport'].iloc[0] if 'estdepartureairport' in df_flight.columns else None
    arr = df_flight['estarrivalairport'].iloc[0] if 'estarrivalairport' in df_flight.columns else None

    # Resolve typecode if not passed or "UNKNOWN"
    if typecode == "UNKNOWN" and 'typecode' in df_flight.columns:
        typecode = df_flight['typecode'].iloc[0]

    # 1. Map columns back to Pycontrails API
    rename_map = {
        'timestamp': 'time',
        'groundspeed': 'gs',
        'track': 'heading',
        'vertical_rate': 'rocd',
    }
    
    df_pc = df_flight.rename(columns=rename_map).copy()
    
    # Drop EKF projection specific columns to match standard PyContrails schemas
    ekf_cols = ['x', 'y', 'track_unwrapped']
    df_pc = df_pc.drop(columns=[c for c in ekf_cols if c in df_pc.columns], errors='ignore')
    
    # Explicitly drop true_airspeed / tas / velocity columns so that PSFlight
    # calculates True Airspeed (TAS) manually using groundspeed (gs) and wind.
    airspeed_cols = ['true_airspeed', 'tas', 'velocity']
    df_pc = df_pc.drop(columns=[c for c in airspeed_cols if c in df_pc.columns], errors='ignore')
    
    # 2. Build Pycontrails Attributes Dictionary
    attrs = {
        "flight_id": flight_id,
        "aircraft_type": typecode,  # PSFlight will use this to look up BADA/aircraft parameters
        "icao24": icao24,
        "callsign": callsign,
    }
    if pd.notna(firstseen):
        attrs["firstseen"] = firstseen
    if pd.notna(lastseen):
        attrs["lastseen"] = lastseen
    route_class = df_flight['route_class'].iloc[0] if 'route_class' in df_flight.columns else None
    cluster_id = df_flight['cluster_id'].iloc[0] if 'cluster_id' in df_flight.columns else None

    if pd.notna(dep):
        attrs["estdepartureairport"] = dep
    if pd.notna(arr):
        attrs["estarrivalairport"] = arr
    if pd.notna(route_class):
        attrs["route_class"] = int(route_class)
    if pd.notna(cluster_id):
        attrs["cluster_id"] = int(cluster_id)
    
    # 3. Instantiate and return the Pycontrails Flight
    try:
        # Pass attrs as kwargs to store them as Flight attributes.
        # Pass drop_duplicated_times=True and crs="EPSG:4326" for safety.
        pc_flight = Flight(data=df_pc, drop_duplicated_times=True, crs="EPSG:4326", **attrs)
        return pc_flight
    except Exception as e:
        logger.error(f"Failed to create pycontrails.Flight for {flight_id}: {e}")
        return None

def read_flights_from_parquet(parquet_path: str) -> dict:
    """
    Reads a consolidated Parquet file containing multiple flights,
    groups them by flight_id, and converts each group into a pycontrails.Flight object.
    
    Returns:
        dict: A dictionary mapping flight_id -> pycontrails.Flight
    """
    df = pd.read_parquet(parquet_path)
    flights = {}
    
    for flight_id, group_df in df.groupby('flight_id'):
        fl = dataframe_to_pycontrails(group_df)
        if fl is not None:
            flights[flight_id] = fl
            
    return flights

def write_flights_to_parquet(flights: list, out_path: Path):
    """
    Consolidates a list of pycontrails.Flight objects, flattens them,
    re-injects static metadata columns from Flight attributes, clears consolidated_df.attrs
    to prevent pyarrow JSON serialization errors, and writes to Parquet.
    """
    if not flights:
        logger.warning("No flights provided to write.")
        return

    dataframes = []
    for fl in flights:
        df_fl = fl.to_dataframe()
        
        # Pull metadata from Flight attributes and inject them as repeated columns
        df_fl['flight_id'] = fl.attrs.get('flight_id', 'UNK')
        df_fl['icao24'] = fl.attrs.get('icao24', 'UNK')
        df_fl['callsign'] = fl.attrs.get('callsign', 'UNK')
        df_fl['typecode'] = fl.attrs.get('aircraft_type', 'UNK')
        
        for attr in ['firstseen', 'lastseen', 'estdepartureairport', 'estarrivalairport', 'route_class', 'cluster_id']:
            if attr in fl.attrs:
                df_fl[attr] = fl.attrs[attr]
                
        dataframes.append(df_fl)
        
    consolidated_df = pd.concat(dataframes, ignore_index=True)
    
    # Clear DataFrame attributes to prevent pyarrow JSON serialization errors
    consolidated_df.attrs = {}
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    consolidated_df.to_parquet(out_path, index=False)
    logger.info(f"✓ Saved {len(consolidated_df):,} waypoints to {out_path}")

def parquet_to_pycontrails(path: str) -> dict:
    """
    Reads a Parquet file (supporting both raw OpenSky and cleaned schemas),
    groups by flight_id, and converts each group into a pycontrails.Flight.
    """
    df = pd.read_parquet(path)
    
    # 1. Normalize raw OpenSky columns to PyContrails standards if raw columns are present
    rename_raw = {
        'lat': 'latitude',
        'lon': 'longitude',
        'baroaltitude': 'altitude',
        'heading': 'track',
        'velocity': 'groundspeed',
        'vertrate': 'vertical_rate'
    }
    
    # Check if raw columns exist and rename
    rename_map = {k: v for k, v in rename_raw.items() if k in df.columns}
    if rename_map:
        df = df.rename(columns=rename_map)
        
    # Ensure time/timestamp columns are parsed to datetime
    if 'time' in df.columns:
        if pd.api.types.is_numeric_dtype(df['time']):
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        else:
            df['time'] = pd.to_datetime(df['time'])
    elif 'timestamp' in df.columns:
        if pd.api.types.is_numeric_dtype(df['timestamp']):
            df['time'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
        else:
            df['time'] = pd.to_datetime(df['timestamp'])
            
    # Drop rows with NaN in critical columns if they exist
    critical_cols = ['latitude', 'longitude', 'altitude', 'time']
    existing_crit = [c for c in critical_cols if c in df.columns]
    if len(existing_crit) == len(critical_cols):
        df = df.dropna(subset=critical_cols)
        
    flights = {}
    for flight_id, group_df in df.groupby('flight_id'):
        group_df = group_df.drop_duplicates(subset=['time'])
        if group_df.empty or len(group_df) < 5:
            continue
            
        typecode = group_df['typecode'].iloc[0] if 'typecode' in group_df.columns else 'B738'
        
        fl = dataframe_to_pycontrails(group_df, typecode=typecode)
        if fl is not None:
            flights[flight_id] = fl
            
    return flights

def pycontrails_to_traffic(pyc_flight: Flight) -> "traffic.core.Flight":
    """
    Transforms a pyc_flight object into an optimized traffic.core.Flight object
    and converts SI units (meters, m/s, m/s) to standard aviation units (feet, knots, ft/min).
    """
    from traffic.core import Flight as TrafficFlight
    
    df = pyc_flight.to_dataframe().copy()
    
    # Map PyContrails columns back to Traffic expectations
    if 'time' in df.columns:
        df = df.rename(columns={'time': 'timestamp'})
        
    rename_back = {
        'gs': 'groundspeed',
        'heading': 'track',
        'rocd': 'vertical_rate',
    }
    df = df.rename(columns={k: v for k, v in rename_back.items() if k in df.columns})
    
    # Scale SI units to standard aviation units for traffic & OpenAP
    if 'altitude' in df.columns:
        df['altitude'] = df['altitude'] * 3.28084
    if 'groundspeed' in df.columns:
        df['groundspeed'] = df['groundspeed'] * 1.943844
    if 'vertical_rate' in df.columns:
        df['vertical_rate'] = df['vertical_rate'] * 196.8504
        
    # Re-inject static metadata from Flight attributes as repeated columns
    df['flight_id'] = pyc_flight.attrs.get('flight_id', 'UNK')
    df['icao24'] = pyc_flight.attrs.get('icao24', 'UNK')
    df['callsign'] = pyc_flight.attrs.get('callsign', 'UNK')
    df['typecode'] = pyc_flight.attrs.get('aircraft_type', 'UNK')
    
    # Ensure timestamp is datetime with UTC timezone
    if 'timestamp' in df.columns and not isinstance(df['timestamp'].dtype, pd.DatetimeTZDtype):
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        
    return TrafficFlight(df)

def pycontrails_to_parquet(flight: Flight, out_path: Path):
    """
    Serializes a finished pycontrails.Flight object directly to a standardized Parquet format on disk.
    """
    write_flights_to_parquet([flight], out_path)

