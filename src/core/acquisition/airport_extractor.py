import argparse
import logging
from pathlib import Path
import pandas as pd

from src.common.config import (
    MASTER_FLIGHTS_DB_DIR,
    AIRPORTS_CACHE_PATH,
    EUR_LAT_MIN,
    EUR_LAT_MAX,
    EUR_LON_MIN,
    EUR_LON_MAX,
)
from src.common.utils import setup_file_logger, resolve_airport_coordinates, write_json_dataclass

def find_latest_file(directory: Path, pattern: str, exclude_keywords: list[str] = None) -> Path:
    """Finds the latest file matching pattern in directory recursively (excluding specified keywords)."""
    files = list(directory.rglob(pattern))
    if exclude_keywords:
        files = [f for f in files if not any(kw in f.name for kw in exclude_keywords)]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)

def extract_airport_set(df: pd.DataFrame) -> set:
    """Extracts unique non-null departure and arrival airport strings from df."""
    deps = set(df['estdepartureairport'].dropna().astype(str).str.strip().unique()) if 'estdepartureairport' in df.columns else set()
    arrs = set(df['estarrivalairport'].dropna().astype(str).str.strip().unique()) if 'estarrivalairport' in df.columns else set()
    return deps.union(arrs)

def main():
    setup_file_logger(log_filename="acquisition.log")

    parser = argparse.ArgumentParser(description="Extract and Label Airport Coordinates JSON Cache")
    parser.add_argument("--raw-flights", help="Path to raw ParentPopulation parquet")
    parser.add_argument("--fleet-flights", help="Path to ParentPopulation_*_target_AirFrames.parquet")
    parser.add_argument("--output-json", help="Path to output airport_coordinates.json")
    parser.add_argument("--update-labels-only", action="store_true", help="Only update boolean metadata labels in existing JSON without fetching new coordinates")
    parser.add_argument("--lat-min", type=float, default=EUR_LAT_MIN, help="Bounding box lat min for label evaluation")
    parser.add_argument("--lat-max", type=float, default=EUR_LAT_MAX, help="Bounding box lat max for label evaluation")
    parser.add_argument("--lon-min", type=float, default=EUR_LON_MIN, help="Bounding box lon min for label evaluation")
    parser.add_argument("--lon-max", type=float, default=EUR_LON_MAX, help="Bounding box lon max for label evaluation")

    args = parser.parse_args()

    json_path = Path(args.output_json) if args.output_json else AIRPORTS_CACHE_PATH

    # Resolve parquets
    raw_path = Path(args.raw_flights) if args.raw_flights else find_latest_file(MASTER_FLIGHTS_DB_DIR, "ParentPopulation_*.parquet", exclude_keywords=["target_AirFrames", "backup"])
    fleet_path = Path(args.fleet_flights) if args.fleet_flights else find_latest_file(MASTER_FLIGHTS_DB_DIR, "*_target_AirFrames.parquet", exclude_keywords=["backup"])

    raw_airports = set()
    fleet_airports = set()

    if raw_path and raw_path.exists():
        logging.info(f"Reading raw ParentPopulation flights: {raw_path}")
        df_raw = pd.read_parquet(raw_path) if raw_path.suffix == ".parquet" else pd.read_csv(raw_path, dtype=str)
        raw_airports = extract_airport_set(df_raw)
        logging.info(f"Found {len(raw_airports):,} unique airports in raw ParentPopulation flights.")

    if fleet_path and fleet_path.exists():
        logging.info(f"Reading fleet-filtered flights: {fleet_path}")
        df_fleet = pd.read_parquet(fleet_path) if fleet_path.suffix == ".parquet" else pd.read_csv(fleet_path, dtype=str)
        fleet_airports = extract_airport_set(df_fleet)
        logging.info(f"Found {len(fleet_airports):,} unique airports in fleet-filtered flights.")

    all_airports = list(raw_airports.union(fleet_airports))
    all_icaos = [code for code in all_airports if code and code != "None" and code != "nan"]

    if args.update_labels_only:
        logging.info("Updating labels only from existing cache...")
        airports_db = resolve_airport_coordinates([])
    else:
        logging.info(f"Resolving coordinates for {len(all_icaos):,} unique airports...")
        airports_db = resolve_airport_coordinates(all_icaos)

    # Re-evaluate metadata labels
    logging.info(f"Updating metadata labels (is_icao_schema, has_target_airframe, survived_bbox with lat=[{args.lat_min},{args.lat_max}], lon=[{args.lon_min},{args.lon_max}])...")
    for code, entry in airports_db.items():
        if not isinstance(entry, dict):
            continue

        # 1. ICAO schema check: 4 uppercase alphabetic characters
        entry["is_icao_schema"] = (len(code) == 4 and code.isalpha() and code.isupper())

        # 2. Target airframe check
        if fleet_airports:
            entry["has_target_airframe"] = (code in fleet_airports)
        elif "has_target_airframe" not in entry:
            entry["has_target_airframe"] = True

        # 3. Bounding box survival check
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is not None and lon is not None:
            entry["survived_bbox"] = (args.lat_min <= lat <= args.lat_max) and (args.lon_min <= lon <= args.lon_max)
        else:
            entry["survived_bbox"] = False

    # Save JSON via write_json_dataclass
    write_json_dataclass(json_path, airports_db)
    logging.info(f"SUCCESS: Saved enriched airport database ({len(airports_db):,} entries) to: {json_path}")

if __name__ == "__main__":
    main()

