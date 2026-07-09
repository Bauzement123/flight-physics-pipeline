"""
Module 2.3: Synthesized Trajectory Generator
Aggregates a cohort of raw trajectories for a given route rank
into a single idealized 'synthesized' route, mapped onto a uniform temporal grid
starting at a fixed baseline date (2025-01-01 00:00:00 UTC).
Uses open-source aviation libraries (traffic, openap, pycontrails) for kinematics,
spatial clustering, phase labeling, and temporal resampling.
"""

import argparse
import logging
from pathlib import Path
import pandas as pd
import numpy as np
from collections import Counter
import pyproj
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from pycontrails import Flight
from traffic.core import Traffic, Flight as TrafficFlight
from openap.phase import FlightPhase

from src.common.config import (
    BASE_DIR, CORRIDOR_PATHS_DIR,
    GLOBAL_MODEL_REGISTRY, GLOBAL_TRAJECTORY_REGISTRY,
    GLOBAL_FLIGHT_CLUSTER_MAP,
    UNSUPPORTED_TYPECODE_FLAG, is_supported_typecode
)
from src.core.corridor.pca_compressor import classify_and_normalize_cohort
from src.common.utils import load_route_summary, split_route_string, setup_file_logger, log_skipped_aircraft
from src.common.adapters import (
    parquet_to_pycontrails,
    pycontrails_to_traffic,
    pycontrails_to_parquet,
    traffic_to_pycontrails
)

# Clustering Config
MIN_FLIGHTS_FOR_CLUSTERING = 10
SILHOUETTE_THRESHOLD = 0.35
MAX_K = 4
CHAOS_THRESHOLD = 200.0

logger = logging.getLogger(__name__)


def update_synthesized_registry(registry_file: Path, route: str, rank: int, file_path: str, route_class: int, cluster_id: int):
    """
    Appends a new synthesized flight mapping to the global registry Parquet.
    Deduplicates on route and cluster_id.
    """
    new_entry = {
        "route": route,
        "rank": rank,
        "file_path": file_path,
        "route_class": route_class,
        "cluster_id": cluster_id
    }
    df_new = pd.DataFrame([new_entry])
    if registry_file.exists():
        try:
            df_reg = pd.read_parquet(registry_file)
            for col in ["route_class", "cluster_id"]:
                if col not in df_reg.columns:
                    df_reg[col] = 1 if col == "route_class" else 0
            df_updated = pd.concat([df_reg, df_new]).drop_duplicates(subset=['route', 'cluster_id'], keep='last')
        except Exception as e:
            logger.warning(f"Could not read existing synthesized registry, overwriting: {e}")
            df_updated = df_new
    else:
        df_updated = df_new
    
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    df_updated.to_parquet(registry_file, index=False)
    logger.info(f"Updated global synthesized registry {registry_file.name} for route {route} cluster {cluster_id} (Class {route_class}).")


def update_flight_cluster_map(registry_file: Path, flight_mappings: list[dict]):
    """
    Appends flight_id to cluster_id mappings to a global registry.
    """
    if not flight_mappings:
        return
    df_new = pd.DataFrame(flight_mappings)
    if registry_file.exists():
        try:
            df_reg = pd.read_parquet(registry_file)
            df_updated = pd.concat([df_reg, df_new]).drop_duplicates(subset=['flight_id'], keep='last')
        except Exception as e:
            logger.warning(f"Could not read flight cluster map, overwriting: {e}")
            df_updated = df_new
    else:
        df_updated = df_new
        
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    df_updated.to_parquet(registry_file, index=False)
    logger.info(f"Updated flight cluster map with {len(flight_mappings)} entries.")


def classify_and_cluster_cohort(resampled_traffic_collection):
    """
    Classifies a cohort of spatially resampled trajectories into 1 of 4 route classes:
    Class 1: Single Track
    Class 2: Binary Split
    Class 3: Multi-Track
    Class 4: Chaos (high spatial variance without distinct clusters)
    
    Returns:
        route_class (int): 1, 2, 3, or 4
        best_k (int): Number of optimal clusters (1 to 4)
        sub_cohorts (dict): Map of cluster_id (int) -> list of TrafficFlight objects
    """
    n_flights = len(resampled_traffic_collection)
    
    # 1. Fallback for low sample size
    if n_flights < MIN_FLIGHTS_FOR_CLUSTERING:
        logger.info(f"Cohort size {n_flights} < {MIN_FLIGHTS_FOR_CLUSTERING}. Skipping clustering (Class 1, k=1).")
        return 1, 1, {0: list(resampled_traffic_collection)}
    
    # 2. Extract and Flatten Features (resampled to exactly 100 points per flight to align features)
    features = []
    for flight in resampled_traffic_collection:
        try:
            resampled_flight = flight.resample(100)
            lats = resampled_flight.data['latitude'].values
            lons = resampled_flight.data['longitude'].values
            
            if len(lats) != 100 or len(lons) != 100:
                ts = (resampled_flight.data['timestamp'] - resampled_flight.data['timestamp'].iloc[0]).dt.total_seconds().values
                if len(ts) > 1 and ts[-1] > 0:
                    norm_time = ts / ts[-1]
                    target_time = np.linspace(0, 1, 100)
                    lats = np.interp(target_time, norm_time, lats)
                    lons = np.interp(target_time, norm_time, lons)
                else:
                    lats = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(lats)), lats)
                    lons = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(lons)), lons)
                
            features.append(np.concatenate([lats, lons]))
        except Exception as e:
            logger.warning(f"Failed to resample flight {flight.callsign or flight.flight_id} for clustering features: {e}")
            df = flight.data
            lats = df['latitude'].values
            lons = df['longitude'].values
            ts = (df['timestamp'] - df['timestamp'].iloc[0]).dt.total_seconds().values
            if len(ts) > 1 and ts[-1] > 0:
                norm_time = ts / ts[-1]
                target_time = np.linspace(0, 1, 100)
                lats = np.interp(target_time, norm_time, lats)
                lons = np.interp(target_time, norm_time, lons)
            else:
                lats = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(lats)), lats)
                lons = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(lons)), lons)
            features.append(np.concatenate([lats, lons]))
            
    X = np.array(features)
    
    # Calculate total variance of coordinates across all flights
    total_variance = np.var(X, axis=0).sum()
    
    # 3. K-Means Silhouette Loop
    best_k = 1
    best_score = -1
    best_labels = np.zeros(n_flights, dtype=int)
    
    max_k_possible = min(MAX_K, n_flights - 1)
    
    if max_k_possible >= 2:
        for k in range(2, max_k_possible + 1):
            try:
                kmeans = KMeans(n_clusters=k, random_state=42, n_init='auto')
                labels = kmeans.fit_predict(X)
                score = silhouette_score(X, labels)
                
                logger.info(f"K-Means k={k} Silhouette score: {score:.4f}")
                
                if score > best_score and score >= SILHOUETTE_THRESHOLD:
                    best_k = k
                    best_score = score
                    best_labels = labels
            except Exception as e:
                logger.warning(f"Error running KMeans for k={k}: {e}")
                
    # 4. Determine Class
    if best_k == 1:
        if total_variance > CHAOS_THRESHOLD:
            route_class = 4  # Chaos
            logger.info(f"Classified as Class 4 (Chaos) with total variance {total_variance:.2f} > {CHAOS_THRESHOLD}")
        else:
            route_class = 1  # Single Baseline
            logger.info(f"Classified as Class 1 (Single Baseline) with total variance {total_variance:.2f}")
    elif best_k == 2:
        route_class = 2  # Binary Split
        logger.info(f"Classified as Class 2 (Binary Split) with score {best_score:.4f}")
    else:
        route_class = 3  # Multi-Track
        logger.info(f"Classified as Class {route_class} (Multi-Track) with k={best_k} and score {best_score:.4f}")
        
    sub_cohorts = {}
    for i, label in enumerate(best_labels):
        if label not in sub_cohorts:
            sub_cohorts[label] = []
        sub_cohorts[label].append(resampled_traffic_collection[i])
        
    return route_class, best_k, sub_cohorts


def find_toc_tod(flight: TrafficFlight, labels: list) -> tuple:
    """
    Identifies the indices corresponding to Top of Climb (TOC) and Top of Descent (TOD)
    using the phase labels from OpenAP's FlightPhase and altitude heuristics.
    """
    df = flight.data
    altitudes = df['altitude'].values  # altitude in feet
    max_alt = np.max(altitudes)
    
    # Locate cruise phase indices (where altitude is high and label is cruise/level)
    cruise_indices = [idx for idx, lbl in enumerate(labels) if lbl in ('CR', 'LVL') and altitudes[idx] > 15000]
    
    if cruise_indices:
        toc_idx = cruise_indices[0]
        tod_idx = cruise_indices[-1]
    else:
        # Fallback heuristic: find where altitude is above 90% of max altitude
        cruise_thresh = max_alt * 0.90
        above_thresh = np.where(altitudes >= cruise_thresh)[0]
        if len(above_thresh) > 0:
            toc_idx = above_thresh[0]
            tod_idx = above_thresh[-1]
        else:
            # Absolute fallback
            toc_idx = int(len(df) * 0.2)
            tod_idx = int(len(df) * 0.8)
            
    # Keep within safe physical limits of flight segments
    toc_idx = max(2, min(toc_idx, int(len(df) * 0.4)))
    tod_idx = max(int(len(df) * 0.6), min(tod_idx, len(df) - 3))
    
    return toc_idx, tod_idx


def create_synthesized_trajectory(rank: int, output_parquet: str, time_grid_seconds: int = 60, overwrite: bool = False) -> list:
    # 1. Resolve Rank to Route
    logger.info(f"Resolving rank {rank} to route...")
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        return None
        
    route_row = df_summary[df_summary['rank'] == rank]
    if route_row.empty:
        logger.error(f"Rank {rank} not found in RouteSummary.")
        return None
        
    route_str = route_row['route'].iloc[0]
    dep, arr = split_route_string(route_str)
    if dep == 'UNK' or arr == 'UNK':
        logger.error(f"Failed to parse departure/arrival airports from route '{route_str}'.")
        return None
        
    logger.info(f"Rank {rank} resolved to route: {dep} -> {arr}")
    
    # Registry-based Skip check
    synthesized_registry_file = GLOBAL_MODEL_REGISTRY
    if synthesized_registry_file.exists():
        try:
            df_reg = pd.read_parquet(synthesized_registry_file)
            if rank in df_reg['rank'].values:
                if not overwrite:
                    logger.info(f"Synthesized trajectory for rank {rank} already exists in registry. Skipping computation.")
                    matched_paths = df_reg[df_reg['rank'] == rank]['file_path'].tolist()
                    return [str(BASE_DIR / p) for p in matched_paths]
                else:
                    logger.info(f"Overwrite enabled: regenerating synthesized trajectory for rank {rank}.")
        except Exception as e:
            logger.warning(f"Could not check synthesized registry skip: {e}")
    
    # 2. Query registry to find raw flight files
    raw_registry_file = GLOBAL_TRAJECTORY_REGISTRY
    if not raw_registry_file.exists():
        logger.error("Global raw trajectory registry file not found.")
        return None
        
    df_raw_reg = pd.read_parquet(raw_registry_file)
    route_pattern = f"_{dep}-{arr}_"
    matching_flights = df_raw_reg[df_raw_reg['flight_id'].str.contains(route_pattern, na=False)]
    
    if matching_flights.empty:
        logger.error(f"No raw flights found in registry matching route pattern '{route_pattern}'.")
        return None
        
    # 3. Load flights sequentially and build the traffic cohort
    raw_flights = []
    typecodes = []
    
    # Group registry matches by file path to load only registered flight IDs
    for rel_path, group in matching_flights.groupby('file_path'):
        abs_path = BASE_DIR / rel_path
        if not abs_path.exists():
            logger.warning(f"File listed in registry not found: {abs_path}")
            continue
            
        try:
            flights_dict = parquet_to_pycontrails(str(abs_path))
            
            for flight_id in group['flight_id']:
                if flight_id in flights_dict:
                    fl = flights_dict[flight_id]
                    trf_flight = pycontrails_to_traffic(fl)
                    airborne_flight = trf_flight.airborne()
                    if airborne_flight is not None and len(airborne_flight) >= 10:
                        tc = fl.attrs.get('aircraft_type', None)
                        if is_supported_typecode(tc):
                            raw_flights.append(airborne_flight)
                            typecodes.append(tc)
                        else:
                            log_skipped_aircraft(fl.attrs.get('flight_id', 'UNK'), tc, "ERROR_FLAG: Path generator input flight has invalid/missing typecode")
        except Exception as e:
            logger.error(f"Failed to load flights from {abs_path}: {e}")
            
    if not raw_flights:
        logger.error("No valid airborne flights successfully loaded.")
        return None
        
    logger.info(f"Successfully loaded {len(raw_flights)} airborne trajectories. Identifying phases...")

    # 4. ROCD Classification & Holding-Pattern Renormalization
    # Delegated to pca_compressor.classify_and_normalize_cohort.
    # is_clean_flags is not consumed here (path_generator uses DTW centroid,
    # not medoid selection) but will be used by the Step 4 clustering engine.
    normalized_flights, _ = classify_and_normalize_cohort(raw_flights)

    if not normalized_flights:
        logger.error("Normalization produced no valid flights.")
        return None

    # 7. Spatial Standardization and Projection setup
    combined_df = pd.concat([f.data for f in normalized_flights], ignore_index=True)
    traffic_cohort = Traffic(combined_df)
    
    max_duration_seconds = max(flight.duration.total_seconds() for flight in traffic_cohort)
    min_sample_spacing_seconds = max(time_grid_seconds / 10.0, 1.0)
    nb_samples = int(max_duration_seconds / min_sample_spacing_seconds)
    
    logger.info(f"Oversampling spatial grid: nb_samples={nb_samples} (spacing={min_sample_spacing_seconds:.1f}s)")
    resampled_traffic = traffic_cohort.resample(nb_samples).eval()
    
    # Route Sameness Classification & Clustering (Sub-objective 3.5)
    route_class, optimal_k, sub_cohorts = classify_and_cluster_cohort(resampled_traffic)
    
    final_output_paths = []
    flight_mappings = []
    
    # Setup base output path details
    base_out_path = Path(output_parquet)
    filename = base_out_path.name
    if filename.endswith("_synthesized.parquet"):
        stem = filename[:-20]
    elif filename.endswith(".parquet"):
        stem = filename.replace(".parquet", "").replace("_synthesized", "")
    else:
        stem = f"{dep}-{arr}"
    out_dir = base_out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    
    if not typecodes:
        log_skipped_aircraft(f"{dep}-{arr}", None, "ERROR_FLAG: No supported typecodes found across cohort for corridor synthesis")
        representative_typecode = UNSUPPORTED_TYPECODE_FLAG
    else:
        representative_typecode = Counter(typecodes).most_common(1)[0][0]
    
    # Process each cluster
    for cluster_id, flights_list in sub_cohorts.items():
        logger.info(f"Processing cluster {cluster_id} with {len(flights_list)} flights...")
        
        # Convert flights list back to a Traffic collection
        sub_collection_df = pd.concat([f.data for f in flights_list], ignore_index=True)
        sub_collection = Traffic(sub_collection_df)
        
        # Projection centered on this sub-cohort's mean coordinates
        all_sub_dfs = sub_collection.data
        mean_lat = all_sub_dfs['latitude'].mean()
        mean_lon = all_sub_dfs['longitude'].mean()
        
        proj4_str = f"+proj=laea +lat_0={mean_lat} +lon_0={mean_lon} +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        projection = pyproj.Proj(proj4_str)
        
        # Compute DTW spatial centroid (Sub-objective 4)
        logger.info(f"Computing spatial track centroid for cluster {cluster_id} using DTW...")
        centroid_flight = sub_collection.centroid(nb_samples=nb_samples, projection=projection)
        
        # Build Flight attributes
        attrs = {
            "flight_id": f"{dep}-{arr}_synthesized_c{cluster_id}",
            "aircraft_type": representative_typecode,
            "icao24": "SYNTH",
            "callsign": "SYNTH",
            "route_class": route_class,
            "cluster_id": cluster_id
        }
        
        # Snap Centroid to PyContrails grid & convert to SI using the unified adapter
        pyc_centroid = traffic_to_pycontrails(centroid_flight, typecode=representative_typecode, drop_kinematics=True, **attrs)
        
        # Uniform temporal interpolation
        logger.info(f"Resampling synthesized trajectory to uniform {time_grid_seconds}s grid...")
        synthesized_flight = pyc_centroid.resample_and_fill(freq=f"{time_grid_seconds}s")
        
        # Timeline normalization (Sub-objective 6)
        df_final = synthesized_flight.to_dataframe()
        delta_time = df_final['time'] - df_final['time'].min()
        df_final['time'] = pd.Timestamp("2025-01-01 00:00:00") + delta_time
        
        # Re-inject metadata in final df
        df_final['route_class'] = route_class
        df_final['cluster_id'] = cluster_id
        
        final_flight = Flight(data=df_final, crs="EPSG:4326", **attrs)
        
        out_path = out_dir / f"{stem}_synthesized_c{cluster_id}.parquet"
        
        # Save Parquet
        pycontrails_to_parquet(final_flight, out_path)
        
        # Register in synthesized registry
        try:
            rel_path_to_save = out_path.resolve().relative_to(BASE_DIR).as_posix()
        except ValueError:
            rel_path_to_save = out_path.resolve().as_posix()
        update_synthesized_registry(
            synthesized_registry_file,
            route=f"{dep}-{arr}",
            rank=rank,
            file_path=rel_path_to_save,
            route_class=route_class,
            cluster_id=cluster_id
        )
        
        # Track mappings
        for fl_item in flights_list:
            flight_mappings.append({
                "flight_id": fl_item.flight_id,
                "route": f"{dep}-{arr}",
                "cluster_id": cluster_id,
                "route_class": route_class,
                "is_medoid": False
            })
            
        final_output_paths.append(str(out_path))
        logger.info(f"✓ Synthesized flight saved and registered: {out_path.name}")
        
    update_flight_cluster_map(GLOBAL_FLIGHT_CLUSTER_MAP, flight_mappings)

    return final_output_paths


if __name__ == "__main__":
    setup_file_logger(log_filename="corridor.log")
    setup_file_logger(log_filename="synthesis.log")
    parser = argparse.ArgumentParser(description="Create a Synthesized Trajectory from raw OpenSky cohorts.")
    parser.add_argument("--rank", type=int, required=True, help="Route rank from RouteSummary to process.")
    parser.add_argument("--out-dir", default=str(CORRIDOR_PATHS_DIR), help="Output directory for the synthesized trajectory.")
    parser.add_argument("--grid-seconds", type=int, default=60, help="Time grid resolution in seconds (default: 60).")
    parser.add_argument("--overwrite", action="store_true", help="Force regeneration of synthesized paths even if they already exist.")
    
    args = parser.parse_args()
    
    # Resolve route to verify name
    df_summary = load_route_summary()
    if df_summary.empty:
        logger.error("RouteSummary is empty or missing.")
        exit(1)
        
    route_row = df_summary[df_summary['rank'] == args.rank]
    if route_row.empty:
        logger.error(f"Rank {args.rank} not found in RouteSummary.")
        exit(1)
        
    route_str = route_row['route'].iloc[0]
    dep, arr = split_route_string(route_str)
    
    out_file = Path(args.out_dir) / f"{dep}-{arr}_synthesized.parquet"
    
    create_synthesized_trajectory(args.rank, str(out_file), time_grid_seconds=args.grid_seconds, overwrite=args.overwrite)
