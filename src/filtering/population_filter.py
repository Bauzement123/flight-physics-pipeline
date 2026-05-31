"""
Module 1.1: Population Corridor Filter
Filters the master flights population CSV/Parquet registry into targeted, route-specific subsets
and saves them as lightweight Parquet lists in the central flight lists directory.
"""
import argparse
import pandas as pd
import logging
from pathlib import Path

from src.common.config import FLIGHT_REGISTRY_DIR, FLIGHT_LISTS_DIR

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

def filter_population_in_memory(
    master_flights,
    route_summary=None,
    start_date: str = None,
    end_date: str = None,
    ranks: list = None,
    typecode: str = None,
    origin: str = None,
    dest: str = None,
    drop_airport_loops: bool = False,
    min_distance: float = 800.0
) -> pd.DataFrame:
    """
    Filters the master registry in-memory without saving anything to disk.
    Accepts either file paths or pre-loaded pandas DataFrames.
    """
    # 1. Load master flights
    if isinstance(master_flights, (str, Path)):
        file_path = str(master_flights)
        logging.info(f"Loading master file from: {file_path}")
        if not Path(file_path).exists():
            logging.error(f"File not found: {file_path}")
            return pd.DataFrame()
        if file_path.endswith('.parquet'):
            # Load only required columns if reading from path directly
            req_cols = ['firstseen', 'estdepartureairport', 'estarrivalairport', 'icao24', 'callsign', 'typecode', 'lastseen']
            try:
                df = pd.read_parquet(file_path, columns=req_cols)
            except Exception:
                df = pd.read_parquet(file_path)
        else:
            df = pd.read_csv(file_path, low_memory=False)
    else:
        df = master_flights

    if df.empty:
        return df

    df_filtered = df

    # Apply date filters first (slicing creates lightweight views)
    if start_date:
        start_dt = pd.to_datetime(start_date, utc=True)
        df_filtered = df_filtered[df_filtered['firstseen'] >= start_dt]
        logging.info(f"Filtered by start date >= {start_date}. Remaining: {len(df_filtered):,}")

    if end_date:
        end_dt_str = f"{end_date} 23:59:59" if len(end_date) <= 10 else end_date
        end_dt = pd.to_datetime(end_dt_str, utc=True)
        df_filtered = df_filtered[df_filtered['firstseen'] <= end_dt]
        logging.info(f"Filtered by end date <= {end_dt_str}. Remaining: {len(df_filtered):,}")

    # Now make a copy of only the sliced subset before performing modifications
    df_filtered = df_filtered.copy()

    # Ensure tz-awareness for datetime columns on the copy
    for col in ['firstseen', 'lastseen']:
        if col in df_filtered.columns:
            if not pd.api.types.is_datetime64_any_dtype(df_filtered[col]):
                df_filtered[col] = pd.to_datetime(df_filtered[col], utc=True)
            elif df_filtered[col].dt.tz is None:
                df_filtered[col] = df_filtered[col].dt.tz_localize('UTC')
            else:
                df_filtered[col] = df_filtered[col].dt.tz_convert('UTC')


    # Load RouteSummary if needed for distance or rank filtering
    df_summary = None
    if (ranks is not None and len(ranks) > 0) or (min_distance is not None):
        from src.common.utils import load_route_summary, split_route_string
        if isinstance(route_summary, (str, Path)) or route_summary is None:
            df_summary = load_route_summary(route_summary)
        else:
            df_summary = route_summary

    # Apply distance filter first using RouteSummary
    if min_distance is not None and df_summary is not None and not df_summary.empty:
        if 'distance_m' in df_summary.columns:
            # Find routes above the minimum distance
            filtered_summary = df_summary[df_summary['distance_m'] >= min_distance * 1000.0].copy()
            valid_routes = set()
            for _, row in filtered_summary.iterrows():
                dep, arr = split_route_string(row['route'])
                if dep != 'UNK' and arr != 'UNK':
                    valid_routes.add((dep, arr))
            
            # Dynamic column resolution for departure/arrival
            dep_col = None
            arr_col = None
            for col in ['estdepartureairport', 'origin', 'departure']:
                if col in df_filtered.columns:
                    dep_col = col
                    break
            for col in ['estarrivalairport', 'destination', 'arrival']:
                if col in df_filtered.columns:
                    arr_col = col
                    break
                    
            if dep_col and arr_col:
                route_keys = df_filtered[dep_col] + '-' + df_filtered[arr_col]
                valid_keys = {f"{dep}-{arr}" for dep, arr in valid_routes}
                df_filtered = df_filtered[route_keys.isin(valid_keys)].copy()
                logging.info(f"Filtered by minimum route distance >= {min_distance} km. Remaining: {len(df_filtered):,}")
            else:
                logging.warning("Could not identify airport columns for distance filtering.")
        else:
            logging.warning("Column 'distance_m' not found in RouteSummary. Skipping distance filtering.")

    # Apply rank filter via RouteSummary
    if ranks is not None and len(ranks) > 0:
        if df_summary is not None and not df_summary.empty:
            filtered_summary = df_summary[df_summary['rank'].isin(ranks)].copy()
            if not filtered_summary.empty:
                target_routes = set()
                for _, row in filtered_summary.iterrows():
                    dep, arr = split_route_string(row['route'])
                    if dep != 'UNK' and arr != 'UNK':
                        target_routes.add((dep, arr))
                
                # Dynamic column resolution for departure/arrival
                dep_col = None
                arr_col = None
                for col in ['estdepartureairport', 'origin', 'departure']:
                    if col in df_filtered.columns:
                        dep_col = col
                        break
                for col in ['estarrivalairport', 'destination', 'arrival']:
                    if col in df_filtered.columns:
                        arr_col = col
                        break
                        
                if dep_col and arr_col:
                    mask = pd.Series(False, index=df_filtered.index)
                    for dep, arr in target_routes:
                        mask |= (df_filtered[dep_col] == dep) & (df_filtered[arr_col] == arr)
                    df_filtered = df_filtered[mask]
                    logging.info(f"Filtered by ranks {ranks}. Remaining: {len(df_filtered):,}")
                else:
                    logging.warning(f"Could not identify airport columns for rank filtering. Columns: {list(df_filtered.columns)}")
            else:
                logging.warning(f"No matching ranks found in RouteSummary. Returning empty DataFrame.")
                return pd.DataFrame()
        else:
            logging.warning("RouteSummary is empty or missing. Skipping rank filtering.")

    # Apply standard attributes filter
    if typecode:
        df_filtered = df_filtered[df_filtered['typecode'] == typecode]
        logging.info(f"Filtered by typecode == {typecode}. Remaining: {len(df_filtered):,}")

    if origin:
        orig_col = 'estdepartureairport' if 'estdepartureairport' in df_filtered.columns else 'estdepatureairport'
        df_filtered = df_filtered[df_filtered[orig_col] == origin]
        logging.info(f"Filtered by origin == {origin}. Remaining: {len(df_filtered):,}")

    if dest:
        df_filtered = df_filtered[df_filtered['estarrivalairport'] == dest]
        logging.info(f"Filtered by destination == {dest}. Remaining: {len(df_filtered):,}")

    # Drop loops if specified
    if drop_airport_loops:
        dep_col = 'estdepartureairport' if 'estdepartureairport' in df_filtered.columns else 'estdepatureairport'
        arr_col = 'estarrivalairport'
        if dep_col in df_filtered.columns and arr_col in df_filtered.columns:
            df_filtered = df_filtered[df_filtered[dep_col] != df_filtered[arr_col]]
            logging.info(f"Dropped airport loops. Remaining: {len(df_filtered):,}")

    return df_filtered

def filter_population(
    file_path: str,
    out_dir: str,
    start_date: str = None,
    end_date: str = None,
    typecode: str = None,
    origin: str = None,
    dest: str = None,
    ranks: list = None,
    route_summary_path: str = None,
    min_distance: float = 800.0
):
    df_filtered = filter_population_in_memory(
        master_flights=file_path,
        route_summary=route_summary_path,
        start_date=start_date,
        end_date=end_date,
        ranks=ranks,
        typecode=typecode,
        origin=origin,
        dest=dest,
        drop_airport_loops=False,
        min_distance=min_distance
    )
    
    if df_filtered.empty:
        logging.warning("Filter resulted in 0 flights. No file will be saved.")
        return

    # Build filename parts dynamically based on filter parameters
    name_parts = []
    if typecode:
        name_parts.append(typecode)
    
    if origin or dest:
        orig_str = origin if origin else "Any"
        dest_str = dest if dest else "Any"
        name_parts.append(f"{orig_str}-{dest_str}")
        
    if ranks:
        ranks_str = "-".join(map(str, sorted(ranks)))
        name_parts.append(f"ranks_{ranks_str}")
        
    if not name_parts:
        name_parts.append("AllFlights")

    # Add temporal string
    date_str = ""
    if start_date or end_date:
        sd = start_date.replace('-', '') if start_date else "Start"
        ed = end_date.replace('-', '') if end_date else "End"
        date_str = f"_{sd}-{ed}"
        
    out_filename = f"{'_'.join(name_parts)}{date_str}.parquet"
    out_path = Path(out_dir) / out_filename

    # If the output file already exists, raise an error to allow skipping in orchestrators
    if out_path.exists():
        raise FileExistsError(f"Filtered file already exists: {out_path}.")

    # Validate columns required for the OpenSky Trino queries
    req_cols = ['icao24', 'callsign', 'firstseen', 'lastseen']
    missing_cols = [c for c in req_cols if c not in df_filtered.columns]
    if missing_cols:
        logging.warning(f"Warning: The following required columns are missing for Trino fetching: {missing_cols}")

    # Save to Parquet
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    df_filtered.to_parquet(out_path, index=False)
    logging.info(f"Successfully saved {len(df_filtered):,} flights to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Filter Master Flight Population Registry")
    parser.add_argument("--csv", "--file", dest="file_path", default=str(FLIGHT_REGISTRY_DIR / "master_flights.parquet"), help="Path to the master CSV or Parquet registry")
    parser.add_argument("--out-dir", default=str(FLIGHT_LISTS_DIR), help="Output directory for sliced flight lists")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--typecode", help="Aircraft Typecode (e.g., B777)")
    parser.add_argument("--origin", help="Origin ICAO (e.g., EGLL)")
    parser.add_argument("--dest", help="Destination ICAO (e.g., KJFK)")
    
    # Ranks corridor / selection CLI parameters
    parser.add_argument("--ranks", type=str, help="Comma-separated list of specific ranks (e.g., '1,76,205')")
    parser.add_argument("--lower-rank", type=int, help="Lower bound of rank corridor")
    parser.add_argument("--upper-rank", type=int, help="Upper bound of rank corridor")
    parser.add_argument("--route-summary", default=str(FLIGHT_REGISTRY_DIR / "master_flights_RouteSummary.pkl"), help="Path to RouteSummary pickle file")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")

    args = parser.parse_args()
    
    # Validate ranks
    if args.lower_rank is not None and args.upper_rank is None:
        parser.error("--upper-rank is required if --lower-rank is specified.")
        
    resolved_ranks = None
    if args.ranks:
        try:
            resolved_ranks = [int(r.strip()) for r in args.ranks.split(",")]
        except ValueError:
            parser.error("--ranks must be a comma-separated list of integers.")
    elif args.lower_rank is not None and args.upper_rank is not None:
        resolved_ranks = list(range(args.lower_rank, args.upper_rank + 1))
    
    try:
        filter_population(
            file_path=args.file_path,
            out_dir=args.out_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            typecode=args.typecode,
            origin=args.origin,
            dest=args.dest,
            ranks=resolved_ranks,
            route_summary_path=args.route_summary,
            min_distance=args.min_distance
        )
    except FileExistsError as e:
        logging.info(str(e))
