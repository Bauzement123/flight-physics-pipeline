import os
import glob
import pandas as pd
from pathlib import Path
import logging

from src.common.config import BASE_DIR, FLIGHT_REGISTRY_DIR

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MANIFEST BUILDER] - %(message)s')

def index_parquet_files(pattern: str, registry_file: Path, search_dirs: list, description: str):
    logging.info(f"--- Rebuilding {description} Registry ---")
    
    # 1. Search directories for matching parquet files
    found_files = []
    for s_dir in search_dirs:
        if s_dir.exists():
            glob_pattern = str(s_dir / "**" / pattern)
            found_files.extend(glob.glob(glob_pattern, recursive=True))
            
    logging.info(f"Found {len(found_files)} files matching '{pattern}' to inspect.")
    
    new_mappings = []
    
    # 2. Read flight_ids from each file
    for filepath_str in found_files:
        filepath = Path(filepath_str)
        rel_path = filepath.resolve().relative_to(BASE_DIR).as_posix()
        
        try:
            # Read only flight_id column to keep memory usage low
            df = pd.read_parquet(filepath, columns=['flight_id'])
            unique_ids = df['flight_id'].dropna().unique()
            
            for f_id in unique_ids:
                new_mappings.append({
                    "flight_id": f_id,
                    "file_path": rel_path
                })
        except Exception as e:
            logging.error(f"Error reading Parquet file {filepath.name}: {e}")
            
    if not new_mappings:
        logging.warning(f"No flight IDs were extracted for {description}.")
        df_new = pd.DataFrame(columns=["flight_id", "file_path"])
    else:
        df_new = pd.DataFrame(new_mappings)
        # Deduplicate (keep first)
        df_new = df_new.drop_duplicates(subset=['flight_id'], keep='first')
        
    # Ensure parent folder exists
    FLIGHT_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    
    # Save registry
    df_new.to_parquet(registry_file, index=False)
    logging.info(f"Successfully generated/updated {description} registry at: {registry_file}")
    logging.info(f"Total flight IDs mapped: {len(df_new):,}\n")


def build_global_manifest():
    # 1. Raw trajectories registry
    index_parquet_files(
        pattern="*_raw.parquet",
        registry_file=FLIGHT_REGISTRY_DIR / "global_trajectory_registry.parquet",
        search_dirs=[
            BASE_DIR / "data" / "01_raw_trajectories",
            BASE_DIR / "data" / "trajectories"
        ],
        description="Raw Trajectory"
    )
    
    # 2. Clean EKF trajectories registry
    index_parquet_files(
        pattern="*_clean_si.parquet",
        registry_file=FLIGHT_REGISTRY_DIR / "global_clean_registry.parquet",
        search_dirs=[
            BASE_DIR / "data" / "02_clean_trajectories",
            BASE_DIR / "data" / "trajectories"
        ],
        description="Clean EKF Trajectory"
    )
    
    # 3. Simulated outputs registry
    index_parquet_files(
        pattern="*_simulated.parquet",
        registry_file=FLIGHT_REGISTRY_DIR / "global_simulation_registry.parquet",
        search_dirs=[
            BASE_DIR / "data" / "results",
            BASE_DIR / "data" / "04_physics_results",
            BASE_DIR / "data" / "trajectories"
        ],
        description="Physics Simulation"
    )

if __name__ == "__main__":
    build_global_manifest()
