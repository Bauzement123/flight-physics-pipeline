"""
Module 1.1: Population Corridor Filter
Filters the master flights population CSV/Parquet registry into targeted, route-specific subsets
and saves them as lightweight Parquet lists in the central flight lists directory.
"""
import argparse
import pandas as pd
import logging
from pathlib import Path

from src.common.config import FLIGHT_LISTS_DIR, ROUTE_SUMMARY_PKL, MASTER_FLIGHTS_FILE

# Configure logger
logger = logging.getLogger(__name__)

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
        logger.info(f"Loading master file from: {file_path}")
        if not Path(file_path).exists():
            logger.error(f"File not found: {file_path}")
            return pd.DataFrame()
        if file_path.endswith('.parquet'):
            # Load only required columns if reading from path directly
            # Support both correct and typo departure airport columns
            try:
                import pyarrow.parquet as pq
                schema = pq.read_schema(file_path)
                req_cols = ['firstseen', 'estdepartureairport', 'estdepatureairport', 'estarrivalairport', 'icao24', 'callsign', 'typecode', 'lastseen']
                existing_cols = [c for c in req_cols if c in schema.names]
                df = pd.read_parquet(file_path, columns=existing_cols)
            except Exception:
                df = pd.read_parquet(file_path)
        else:
            df = pd.read_csv(file_path, low_memory=False)
    else:
        df = master_flights

    if df.empty:
        return df

    df_filtered = df.copy()

    # Convert datetime columns to timezone-naive UTC
    for col in ['firstseen', 'lastseen']:
        if col in df_filtered.columns:
            if not pd.api.types.is_datetime64_any_dtype(df_filtered[col]):
                df_filtered[col] = pd.to_datetime(df_filtered[col], utc=True)
            if df_filtered[col].dt.tz is not None:
                df_filtered[col] = df_filtered[col].dt.tz_convert('UTC').dt.tz_localize(None)

    # Apply date filters (using timezone-naive UTC)
    if start_date:
        start_dt = pd.to_datetime(start_date, utc=True).tz_localize(None)
        df_filtered = df_filtered[df_filtered['firstseen'] >= start_dt]
        logger.info(f"Filtered by start date >= {start_date}. Remaining: {len(df_filtered):,}")

    if end_date:
        end_dt_str = f"{end_date} 23:59:59" if len(end_date) <= 10 else end_date
        end_dt = pd.to_datetime(end_dt_str, utc=True).tz_localize(None)
        df_filtered = df_filtered[df_filtered['firstseen'] <= end_dt]
        logger.info(f"Filtered by end date <= {end_dt_str}. Remaining: {len(df_filtered):,}")


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
            
            # Dynamic column resolution for departure/arrival (with typo fallback)
            dep_col = None
            arr_col = None
            for col in ['estdepartureairport', 'estdepatureairport', 'origin', 'departure']:
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
                logger.info(f"Filtered by minimum route distance >= {min_distance} km. Remaining: {len(df_filtered):,}")
            else:
                logger.warning("Could not identify airport columns for distance filtering.")
        else:
            logger.warning("Column 'distance_m' not found in RouteSummary. Skipping distance filtering.")

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
                
                # Dynamic column resolution for departure/arrival (with typo fallback)
                dep_col = None
                arr_col = None
                for col in ['estdepartureairport', 'estdepatureairport', 'origin', 'departure']:
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
                    logger.info(f"Filtered by ranks {ranks}. Remaining: {len(df_filtered):,}")
                else:
                    logger.warning(f"Could not identify airport columns for rank filtering. Columns: {list(df_filtered.columns)}")
            else:
                logger.warning(f"No matching ranks found in RouteSummary. Returning empty DataFrame.")
                return pd.DataFrame()
        else:
            logger.warning("RouteSummary is empty or missing. Skipping rank filtering.")

    # Apply standard attributes filter
    if typecode:
        df_filtered = df_filtered[df_filtered['typecode'] == typecode]
        logger.info(f"Filtered by typecode == {typecode}. Remaining: {len(df_filtered):,}")

    if origin:
        orig_col = 'estdepartureairport' if 'estdepartureairport' in df_filtered.columns else 'estdepatureairport'
        df_filtered = df_filtered[df_filtered[orig_col] == origin]
        logger.info(f"Filtered by origin == {origin}. Remaining: {len(df_filtered):,}")

    if dest:
        df_filtered = df_filtered[df_filtered['estarrivalairport'] == dest]
        logger.info(f"Filtered by destination == {dest}. Remaining: {len(df_filtered):,}")

    # Drop loops if specified
    if drop_airport_loops:
        dep_col = 'estdepartureairport' if 'estdepartureairport' in df_filtered.columns else 'estdepatureairport'
        arr_col = 'estarrivalairport'
        if dep_col in df_filtered.columns and arr_col in df_filtered.columns:
            df_filtered = df_filtered[df_filtered[dep_col] != df_filtered[arr_col]]
            logger.info(f"Dropped airport loops. Remaining: {len(df_filtered):,}")

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
        logger.warning("Filter resulted in 0 flights. No file will be saved.")
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
        logger.warning(f"Warning: The following required columns are missing for Trino fetching: {missing_cols}")

    # Save to Parquet
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    df_filtered.to_parquet(out_path, index=False)
    logger.info(f"Successfully saved {len(df_filtered):,} flights to {out_path}")

if __name__ == "__main__":
    # Configure logging at application entry point
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

    parser = argparse.ArgumentParser(description="Filter Master Flight Population Registry")
    parser.add_argument("--csv", "--file", "--master-file", dest="file_path", default=str(MASTER_FLIGHTS_FILE), help="Path to the master CSV or Parquet registry")
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
    parser.add_argument("--route-summary", default=str(ROUTE_SUMMARY_PKL), help="Path to RouteSummary pickle file")
    parser.add_argument("--min-distance", type=float, default=800.0, help="Minimum route distance in kilometers to process")

    args = parser.parse_args()
    
    # Set up file logger to mirror stdout to extraction.log in output directory
    from src.common.utils import setup_file_logger
    setup_file_logger(Path(args.out_dir), "extraction.log")
    
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
        logger.info(str(e))
