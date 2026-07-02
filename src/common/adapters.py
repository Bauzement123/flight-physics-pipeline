"""
Shared Flight Serialization & Metadata Adapter
Provides unified conversion interfaces between Pandas DataFrames and pycontrails.Flight objects,
centralizing Parquet I/O and preventing pyarrow Timestamp JSON serialization crashes.
"""

import pandas as pd
import logging
from pathlib import Path
from pycontrails import Flight
from src.common.config import M_TO_FT, MPS_TO_KT, MPS_TO_FPM

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
    if 'time' in df_pc.columns:
        df_pc['time'] = pd.to_datetime(df_pc['time'])
        if df_pc['time'].dt.tz is not None:
            df_pc['time'] = df_pc['time'].dt.tz_localize(None)
    
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
    for col in ['time', 'timestamp']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
            if df[col].dt.tz is not None:
                df[col] = df[col].dt.tz_localize(None)
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
        
    # Ensure time/timestamp columns are parsed to datetime (timezone-naive UTC)
    if 'time' in df.columns:
        if pd.api.types.is_numeric_dtype(df['time']):
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_localize(None)
        else:
            df['time'] = pd.to_datetime(df['time'])
            if df['time'].dt.tz is not None:
                df['time'] = df['time'].dt.tz_localize(None)
    elif 'timestamp' in df.columns:
        if pd.api.types.is_numeric_dtype(df['timestamp']):
            df['time'] = pd.to_datetime(df['timestamp'], unit='s', utc=True).dt.tz_localize(None)
        else:
            df['time'] = pd.to_datetime(df['timestamp'])
            if df['time'].dt.tz is not None:
                df['time'] = df['time'].dt.tz_localize(None)
            
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

def df_si_to_df_nautic(df_si: pd.DataFrame) -> pd.DataFrame:
    """
    Converts a DataFrame containing state vectors in SI units
    to a DataFrame containing state vectors in aviation/nautical units.
    """
    df = df_si.copy()
    
    # 1. Normalize column spelling variants to standard traffic/OpenAP names
    rename_map = {
        'time': 'timestamp',
        'lat': 'latitude',
        'lon': 'longitude',
        'baroaltitude': 'altitude',
        'geoaltitude': 'geoaltitude_nautic',
        'velocity': 'groundspeed',
        'gs': 'groundspeed',
        'heading': 'track',
        'vertrate': 'vertical_rate',
        'rocd': 'vertical_rate'
    }
    
    # Filter mapping to only rename columns that actually exist
    rename_cols = {k: v for k, v in rename_map.items() if k in df.columns and v not in df.columns}
    if rename_cols:
        df = df.rename(columns=rename_cols)
        
    # 2. Parse time to timezone-naive datetime if needed
    for time_col in ['timestamp', 'time']:
        if time_col in df.columns:
            if pd.api.types.is_numeric_dtype(df[time_col]):
                df[time_col] = pd.to_datetime(df[time_col], unit='s', utc=True).dt.tz_localize(None)
            else:
                df[time_col] = pd.to_datetime(df[time_col])
                if df[time_col].dt.tz is not None:
                    df[time_col] = df[time_col].dt.tz_localize(None)
                    
    # 3. Scale physical parameters from SI to standard aviation units
    if 'altitude' in df.columns:
        df['altitude'] = df['altitude'] * M_TO_FT
    if 'groundspeed' in df.columns:
        df['groundspeed'] = df['groundspeed'] * MPS_TO_KT
    if 'vertical_rate' in df.columns:
        df['vertical_rate'] = df['vertical_rate'] * MPS_TO_FPM
        
    return df

def df_nautic_to_df_si(df_nautic: pd.DataFrame) -> pd.DataFrame:
    """
    Converts a DataFrame containing state vectors in aviation/nautical units
    to a DataFrame containing state vectors in SI units.
    """
    df = df_nautic.copy()
    
    # 1. Normalize spelling variants back to standard SI names
    rename_map = {
        'timestamp': 'time',
        'latitude': 'lat',
        'longitude': 'lon',
        'altitude': 'baroaltitude',
        'groundspeed': 'velocity',
        'track': 'heading',
        'vertical_rate': 'vertrate',
    }
    
    # Filter mapping to only rename columns that actually exist
    rename_cols = {k: v for k, v in rename_map.items() if k in df.columns and v not in df.columns}
    if rename_cols:
        df = df.rename(columns=rename_cols)
        
    # 2. Scale physical parameters from standard aviation units back to SI units
    if 'baroaltitude' in df.columns:
        df['baroaltitude'] = df['baroaltitude'] / M_TO_FT
    if 'velocity' in df.columns:
        df['velocity'] = df['velocity'] / MPS_TO_KT
    if 'vertrate' in df.columns:
        df['vertrate'] = df['vertrate'] / MPS_TO_FPM
        
    return df

def df_to_traffic(df: pd.DataFrame, is_si: bool = True) -> "traffic.core.Flight":
    """
    Transforms a trajectory DataFrame into an optimized traffic.core.Flight object.
    
    Args:
        df (pd.DataFrame): Trajectory DataFrame.
        is_si (bool): If True, performs SI-to-aviation unit conversion first.
        
    Returns:
        traffic.core.Flight: A traffic Flight object.
    """
    from traffic.core import Flight as TrafficFlight
    df_copy = df.copy()
    if is_si:
        df_copy = df_si_to_df_nautic(df_copy)
    else:
        # Just rename timestamp column if needed for traffic
        if 'time' in df_copy.columns and 'timestamp' not in df_copy.columns:
            df_copy = df_copy.rename(columns={'time': 'timestamp'})
            
    # Ensure timestamp is datetime and timezone-naive
    if 'timestamp' in df_copy.columns:
        df_copy['timestamp'] = pd.to_datetime(df_copy['timestamp'])
        if df_copy['timestamp'].dt.tz is not None:
            df_copy['timestamp'] = df_copy['timestamp'].dt.tz_localize(None)
            
    return TrafficFlight(df_copy)

def traffic_to_df(flight: "traffic.core.Flight", to_si: bool = True) -> pd.DataFrame:
    """
    Transforms a traffic.core.Flight object into a DataFrame.
    
    Args:
        flight (traffic.core.Flight): Traffic Flight object.
        to_si (bool): If True, performs aviation-to-SI unit conversion.
        
    Returns:
        pd.DataFrame: Trajectory DataFrame.
    """
    df = flight.data.copy()
    
    # Re-inject static metadata from traffic Flight attributes if present
    for attr in ['flight_id', 'icao24', 'callsign', 'typecode']:
        if hasattr(flight, attr) and getattr(flight, attr) is not None:
            df[attr] = getattr(flight, attr)
            
    if to_si:
        df = df_nautic_to_df_si(df)
        
    return df

def pycontrails_to_traffic(pyc_flight: Flight) -> "traffic.core.Flight":
    """
    Transforms a pyc_flight object into an optimized traffic.core.Flight object
    and converts SI units (meters, m/s, m/s) to standard aviation units (feet, knots, ft/min).
    """
    df_si = pyc_flight.to_dataframe().copy()
    
    # Map pyc column names to standard SI names
    rename_pyc = {
        'gs': 'velocity',
        'heading': 'heading',
        'rocd': 'vertrate',
        'time': 'time'
    }
    df_si = df_si.rename(columns=rename_pyc)
    
    # Re-inject attributes from pyc_flight
    df_si['flight_id'] = pyc_flight.attrs.get('flight_id', 'UNK')
    df_si['icao24'] = pyc_flight.attrs.get('icao24', 'UNK')
    df_si['callsign'] = pyc_flight.attrs.get('callsign', 'UNK')
    df_si['typecode'] = pyc_flight.attrs.get('aircraft_type', 'UNK')
    
    for attr in ['firstseen', 'lastseen', 'estdepartureairport', 'estarrivalairport', 'route_class', 'cluster_id']:
        if attr in pyc_flight.attrs:
            df_si[attr] = pyc_flight.attrs[attr]
            
    return df_to_traffic(df_si, is_si=True)

def pycontrails_to_parquet(flight: Flight, out_path: Path):
    """
    Serializes a finished pycontrails.Flight object directly to a standardized Parquet format on disk.
    """
    write_flights_to_parquet([flight], out_path)

def traffic_to_pycontrails(flight_or_df, typecode: str = "B738", drop_kinematics: bool = False, **attrs) -> Flight:
    """
    Transforms a traffic.core.Flight object or standard aviation DataFrame 
    back into a pycontrails.Flight object, re-scaling standard aviation units 
    (feet, knots, ft/min) back to SI units.
    """
    if hasattr(flight_or_df, 'data'):
        # It is a traffic.core.Flight or similar object
        df_nautic = flight_or_df.data.copy()
        flight_attrs = getattr(flight_or_df, 'attrs', {})
    else:
        # It is a pandas DataFrame
        df_nautic = flight_or_df.copy()
        flight_attrs = {}

    df_si = df_nautic_to_df_si(df_nautic)

    # Map back to Pycontrails standard columns (time, latitude, longitude, altitude, gs, heading, rocd)
    rename_to_pyc = {
        'lat': 'latitude',
        'lon': 'longitude',
        'baroaltitude': 'altitude',
        'velocity': 'gs',
        'vertrate': 'rocd',
    }
    df_pc = df_si.rename(columns=rename_to_pyc).copy()
    
    # Drop derived/outdated columns
    cols_to_drop = ['x', 'y', 'track_unwrapped']
    if drop_kinematics:
        cols_to_drop.extend(['gs', 'heading', 'rocd', 'velocity', 'vertrate'])
    df_pc = df_pc.drop(columns=[c for c in cols_to_drop if c in df_pc.columns], errors='ignore')
    
    # Extract and inject metadata
    flight_id = flight_attrs.get('flight_id', df_pc['flight_id'].iloc[0] if 'flight_id' in df_pc.columns else 'UNK')
    icao24 = flight_attrs.get('icao24', df_pc['icao24'].iloc[0] if 'icao24' in df_pc.columns else 'UNK')
    callsign = flight_attrs.get('callsign', df_pc['callsign'].iloc[0] if 'callsign' in df_pc.columns else 'UNK')
    
    final_attrs = {
        "flight_id": flight_id,
        "aircraft_type": typecode,
        "icao24": icao24,
        "callsign": callsign,
    }
    # Merge additional attributes
    for k, v in flight_attrs.items():
        if k not in ['flight_id', 'aircraft_type', 'icao24', 'callsign']:
            final_attrs[k] = v
    for k, v in attrs.items():
        final_attrs[k] = v
        
    return Flight(data=df_pc, crs="EPSG:4326", drop_duplicated_times=True, **final_attrs)


