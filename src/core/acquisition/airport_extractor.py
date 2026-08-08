import argparse
import json
import logging
import urllib.request
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
from src.common.utils import setup_file_logger

def find_latest_file(directory: Path, pattern: str, exclude_keywords: list[str] = None) -> Path:
    """Finds the latest file matching pattern in directory recursively (excluding specified keywords)."""
    files = list(directory.rglob(pattern))
    if exclude_keywords:
        files = [f for f in files if not any(kw in f.name for kw in exclude_keywords)]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)

def fetch_airport_coords(airport_icao: str, ourairports_db: dict = None) -> dict:
    """
    Attempts to look up airport coordinates using OurAirports offline DB, traffic, openap, or airportsdata libraries.
    Returns dict with 'lat', 'lon', 'name' if found, else None.
    """
    # 0. Try OurAirports
    if ourairports_db and airport_icao in ourairports_db:
        return ourairports_db[airport_icao]

    # 1. Try traffic library
    try:
        from traffic.data import airports
        ap = airports[airport_icao]
        if ap is not None and hasattr(ap, 'lat') and hasattr(ap, 'lon'):
            return {
                "lat": float(ap.lat),
                "lon": float(ap.lon),
                "name": str(getattr(ap, 'name', airport_icao)),
                "country": str(getattr(ap, 'country', ''))
            }
    except Exception:
        pass

    # 2. Try airportsdata package fallback
    try:
        import airportsdata
        airports_db = airportsdata.load('ICAO')
        if airport_icao in airports_db:
            ap = airports_db[airport_icao]
            return {
                "lat": float(ap["lat"]),
                "lon": float(ap["lon"]),
                "name": str(ap.get("name", airport_icao)),
                "country": str(ap.get("country", ""))
            }
    except Exception:
        pass

    # 3. Try openap fallback
    try:
        from openap import extra
        aps = extra.airports()
        if airport_icao in aps:
            ap = aps[airport_icao]
            return {
                "lat": float(ap["lat"]),
                "lon": float(ap["lon"]),
                "name": str(ap.get("name", airport_icao)),
                "country": str(ap.get("country", ""))
            }
    except Exception:
        pass

    return None

def get_ourairports_dict() -> dict:
    """Downloads and parses the global OurAirports CSV dataset."""
    csv_path = Path("data/registries/ourairports.csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    if not csv_path.exists():
        logging.info("Downloading global OurAirports dataset (10MB)...")
        url = "https://davidmegginson.github.io/ourairports-data/airports.csv"
        try:
            urllib.request.urlretrieve(url, csv_path)
            logging.info("Downloaded OurAirports dataset successfully.")
        except Exception as e:
            logging.warning(f"Failed to download OurAirports: {e}")
            return {}
            
    try:
        df = pd.read_csv(csv_path, low_memory=False)
        ourairports_db = {}
        for _, row in df.iterrows():
            ident = str(row.get('ident', '')).strip()
            if ident and ident != 'nan':
                ourairports_db[ident] = {
                    "lat": float(row.get('latitude_deg', 0.0)),
                    "lon": float(row.get('longitude_deg', 0.0)),
                    "name": str(row.get('name', ident)),
                    "country": str(row.get('iso_country', ''))
                }
        return ourairports_db
    except Exception as e:
        logging.warning(f"Failed to parse OurAirports CSV: {e}")
        return {}

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

    # Load existing JSON if present
    airports_db = {}
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                airports_db = json.load(f)
            logging.info(f"Loaded existing airport coordinates from {json_path} ({len(airports_db)} entries).")
        except Exception as e:
            logging.warning(f"Failed to load existing JSON: {e}")

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

    all_airports = raw_airports.union(fleet_airports).union(set(airports_db.keys()))

    if not args.update_labels_only:
        logging.info(f"Fetching coordinates for missing airports across total {len(all_airports):,} unique airports...")
        ourairports_db = get_ourairports_dict()
        missing_count = 0
        found_count = 0
        for code in all_airports:
            if not code or code == "None" or code == "nan":
                continue
            if code not in airports_db or "lat" not in airports_db[code]:
                coords = fetch_airport_coords(code, ourairports_db)
                if coords:
                    airports_db[code] = coords
                    found_count += 1
                else:
                    missing_count += 1

        logging.info(f"Coordinate fetching done: {found_count} new coordinates found, {missing_count} missing.")

    # Re-evaluate metadata labels
    logging.info(f"Updating metadata labels (is_icao_schema, has_target_airframe, survived_bbox with lat=[{args.lat_min},{args.lat_max}], lon=[{args.lon_min},{args.lon_max}])...")
    for code, entry in airports_db.items():
        if not isinstance(entry, dict):
            continue

        # 1. ICAO schema check
        entry["is_icao_schema"] = (len(code) == 4 and code.isalpha())

        # 2. Target airframe check
        if fleet_airports:
            entry["has_target_airframe"] = (code in fleet_airports)
        elif "has_target_airframe" not in entry:
            entry["has_target_airframe"] = True

        # 3. Bounding box survival check (Pure geometrical calculation)
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is not None and lon is not None:
            entry["survived_bbox"] = (args.lat_min <= lat <= args.lat_max) and (args.lon_min <= lon <= args.lon_max)
        else:
            entry["survived_bbox"] = False

    # Save JSON
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(airports_db, f, indent=2)

    logging.info(f"SUCCESS: Saved enriched airport database ({len(airports_db):,} entries) to: {json_path}")

if __name__ == "__main__":
    main()
