import json
import sys
from pathlib import Path

# Add the project root to the Python path to allow importing src modules
project_root = Path(__file__).resolve().parents[2]
sys.path.append(str(project_root))

from src.common.config import AIRPORTS_CACHE_PATH

def find_bounding_airports():
    """
    Reads the airport coordinates JSON file and extracts the four airports
    that define the bounding box (extremes of latitude and longitude)
    containing all other airports.
    """
    if not AIRPORTS_CACHE_PATH.exists():
        print(f"Error: Airport coordinates cache JSON not found at: {AIRPORTS_CACHE_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading airport coordinates from: {AIRPORTS_CACHE_PATH}")
    with open(AIRPORTS_CACHE_PATH, "r", encoding="utf-8") as f:
        airports_db = json.load(f)

    if not airports_db:
        print("Error: The airport coordinates database is empty.", file=sys.stderr)
        sys.exit(1)

    # Initialize variables for the extremes
    min_lat = float('inf')
    max_lat = float('-inf')
    min_lon = float('inf')
    max_lon = float('-inf')

    southern_most = None
    northern_most = None
    western_most = None
    eastern_most = None

    for icao, coords in airports_db.items():
        lat = coords.get("lat")
        lon = coords.get("lon")

        if lat is None or lon is None:
            continue

        # Latitude extremes (North/South)
        if lat > max_lat:
            max_lat = lat
            northern_most = icao
        if lat < min_lat:
            min_lat = lat
            southern_most = icao

        # Longitude extremes (East/West)
        if lon > max_lon:
            max_lon = lon
            eastern_most = icao
        if lon < min_lon:
            min_lon = lon
            western_most = icao

    print("\n--- Bounding Box Definition ---")
    print(f"Latitude Range:  [{min_lat:.6f}, {max_lat:.6f}]")
    print(f"Longitude Range: [{min_lon:.6f}, {max_lon:.6f}]")
    print("\n--- Bounding Airports ---")
    
    if northern_most:
        print(f"Northern-most (Max Lat): {northern_most}")
        print(f"  Coordinates: Lat = {max_lat:.6f}, Lon = {airports_db[northern_most]['lon']:.6f}")
    if southern_most:
        print(f"Southern-most (Min Lat): {southern_most}")
        print(f"  Coordinates: Lat = {min_lat:.6f}, Lon = {airports_db[southern_most]['lon']:.6f}")
    if western_most:
        print(f"Western-most (Min Lon):  {western_most}")
        print(f"  Coordinates: Lat = {airports_db[western_most]['lat']:.6f}, Lon = {min_lon:.6f}")
    if eastern_most:
        print(f"Eastern-most (Max Lon):  {eastern_most}")
        print(f"  Coordinates: Lat = {airports_db[eastern_most]['lat']:.6f}, Lon = {max_lon:.6f}")

if __name__ == "__main__":
    find_bounding_airports()
