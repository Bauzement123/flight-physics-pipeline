import argparse
import logging
from pathlib import Path
import numpy as np
import pandas as pd

from src.common.config import (
    MASTER_FLIGHTS_DB_DIR,
    AIRPORTS_CACHE_PATH,
    EUR_LAT_MIN,
    EUR_LAT_MAX,
    EUR_LON_MIN,
    EUR_LON_MAX,
)
from src.common.utils import setup_file_logger, resolve_airport_coordinates

def find_latest_file(directory: Path, pattern: str) -> Path:
    """Finds the latest file matching pattern in directory recursively (based on modification time)."""
    files = list(directory.rglob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)

def apply_geographic_bbox_filter(
    df: pd.DataFrame,
    airports_db: dict,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
) -> pd.DataFrame:
    """
    Filters flights in df using coordinates from airports_db dictionary and lat/lon bounds.
    """
    initial_len = len(df)

    dep_lats = df["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    dep_lons = df["estdepartureairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))
    arr_lats = df["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lat", np.nan))
    arr_lons = df["estarrivalairport"].map(lambda x: airports_db.get(x, {}).get("lon", np.nan))

    in_box_mask = (
        (dep_lats.between(lat_min, lat_max)) &
        (dep_lons.between(lon_min, lon_max)) &
        (arr_lats.between(lat_min, lat_max)) &
        (arr_lons.between(lon_min, lon_max))
    )

    df_filtered = df[in_box_mask].copy()
    dropped = initial_len - len(df_filtered)
    logging.info(
        f"Geographic Bounding Box Filter complete. Lat: [{lat_min}, {lat_max}], Lon: [{lon_min}, {lon_max}]. "
        f"Dropped {dropped:,} of {initial_len:,} flights. Remaining: {len(df_filtered):,}"
    )
    return df_filtered

def apply_all_filters(
    df: pd.DataFrame,
    airports_db: dict,
    apply_bbox: bool = True,
    lat_min: float = EUR_LAT_MIN,
    lat_max: float = EUR_LAT_MAX,
    lon_min: float = EUR_LON_MIN,
    lon_max: float = EUR_LON_MAX,
) -> pd.DataFrame:
    """
    Modular filter orchestrator. Easily extendable for new columnar or geographical filter functions.
    """
    df_current = df

    if apply_bbox:
        if not airports_db:
            logging.warning("Airports database is empty or missing. Skipping bounding box filter.")
        else:
            df_current = apply_geographic_bbox_filter(
                df_current, airports_db, lat_min, lat_max, lon_min, lon_max
            )

    # Future custom filters can be called here:
    # df_current = apply_custom_filter(df_current, ...)

    return df_current

def main():
    setup_file_logger(log_filename="acquisition.log")

    parser = argparse.ArgumentParser(description="Apply Geographic Bounds and Modular Filters to Flight Population")
    parser.add_argument("--input", help="Path to input merged parquet file (e.g. ParentPopulation_*_target_AirFrames.parquet)")
    parser.add_argument("--output", help="Path to output filtered parquet file (defaults to master_flights.parquet)")
    parser.add_argument("--airports-cache", help="Path to airport_coordinates.json cache")
    parser.add_argument("--lat-min", type=float, default=EUR_LAT_MIN, help=f"Minimum latitude (default: {EUR_LAT_MIN})")
    parser.add_argument("--lat-max", type=float, default=EUR_LAT_MAX, help=f"Maximum latitude (default: {EUR_LAT_MAX})")
    parser.add_argument("--lon-min", type=float, default=EUR_LON_MIN, help=f"Minimum longitude (default: {EUR_LON_MIN})")
    parser.add_argument("--lon-max", type=float, default=EUR_LON_MAX, help=f"Maximum longitude (default: {EUR_LON_MAX})")
    parser.add_argument("--no-bbox", action="store_true", help="Disable geographic bounding box filtering")

    args = parser.parse_args()

    # 1. Resolve Input Path
    if args.input:
        input_path = Path(args.input)
    else:
        input_path = find_latest_file(MASTER_FLIGHTS_DB_DIR, "*_target_AirFrames.parquet")
        if not input_path:
            input_path = find_latest_file(MASTER_FLIGHTS_DB_DIR, "ParentPopulation_*.parquet")

    if not input_path or not input_path.exists():
        raise FileNotFoundError("Input flight file not found. Please specify it using --input.")

    # 2. Resolve Output Path
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = MASTER_FLIGHTS_DB_DIR / "master_flights.parquet"

    logging.info(f"Loading input flight dataset from {input_path}...")
    if input_path.suffix.lower() == ".parquet":
        df_flights = pd.read_parquet(input_path)
    else:
        df_flights = pd.read_csv(input_path, dtype=str)

    logging.info(f"Initial flight count: {len(df_flights):,} rows.")

    # 3. Resolve Airport Coordinates
    deps = set(df_flights["estdepartureairport"].dropna().astype(str).str.strip().unique()) if "estdepartureairport" in df_flights.columns else set()
    arrs = set(df_flights["estarrivalairport"].dropna().astype(str).str.strip().unique()) if "estarrivalairport" in df_flights.columns else set()
    unique_icaos = [ic for ic in deps.union(arrs) if ic and ic != "None" and ic != "nan"]

    airports_db = resolve_airport_coordinates(unique_icaos)
    logging.info(f"Resolved airport coordinates ({len(airports_db)} entries).")

    # 4. Apply Filters
    df_filtered = apply_all_filters(
        df_flights,
        airports_db,
        apply_bbox=not args.no_bbox,
        lat_min=args.lat_min,
        lat_max=args.lat_max,
        lon_min=args.lon_min,
        lon_max=args.lon_max,
    )

    # 4.1 Materialize canonical route string ('DEP-ARR') for native PyArrow pushdown
    if "estdepartureairport" in df_filtered.columns and "estarrivalairport" in df_filtered.columns:
        dep_clean = df_filtered["estdepartureairport"].fillna("").astype(str).str.strip()
        arr_clean = df_filtered["estarrivalairport"].fillna("").astype(str).str.strip()
        df_filtered["route"] = dep_clean + "-" + arr_clean
        mask_incomplete = (
            df_filtered["estdepartureairport"].isna()
            | df_filtered["estarrivalairport"].isna()
            | (df_filtered["route"] == "-")
        )
        df_filtered.loc[mask_incomplete, "route"] = None

    # 5. Save Output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"Saving filtered flights ({len(df_filtered):,} rows) to {out_path}...")
    if out_path.suffix.lower() == ".csv":
        df_filtered.to_csv(out_path, index=False)
    else:
        df_filtered.to_parquet(out_path, index=False)

    logging.info("Successfully completed apply_bounds_and_filters!")

if __name__ == "__main__":
    main()
