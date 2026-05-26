"""
Utility Script: Trajectory Directory Migrator
Iterates through cohort directories in data/trajectories/, identifies flatly dumped 
raw/clean parquet files, partitions them into raw/ and clean/ subfolders, and 
rebuilds the global trajectory registry index.
"""

import os
import shutil
from pathlib import Path
import logging

from src.common.config import BASE_DIR, TRAJECTORIES_DIR
from src.common.build_global_manifest import build_global_manifest

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MIGRATOR] - %(message)s')

def migrate():
    logging.info(f"Scanning trajectories directory: {TRAJECTORIES_DIR}")
    if not TRAJECTORIES_DIR.exists():
        logging.error("Trajectories directory does not exist.")
        return
        
    for cohort_dir in TRAJECTORIES_DIR.iterdir():
        if not cohort_dir.is_dir():
            continue
            
        # Skip special folders
        if cohort_dir.name in ["flight_lists", "flight_registry", "weather", "master_flight_paths", "original_raw"]:
            continue
            
        logging.info(f"Checking cohort directory: {cohort_dir.name}")
        
        # Locate files flatly inside the cohort root directory
        raw_files = list(cohort_dir.glob("*_raw.parquet"))
        clean_files = list(cohort_dir.glob("*_clean_si.parquet"))
        
        if not raw_files and not clean_files:
            logging.info(f"  -> No flat parquet files found in {cohort_dir.name}. Skipping.")
            continue
            
        # Move raw parquet files to raw/
        if raw_files:
            raw_dir = cohort_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            logging.info(f"  -> Moving {len(raw_files)} raw files to {raw_dir.relative_to(BASE_DIR)}")
            for f in raw_files:
                dest = raw_dir / f.name
                shutil.move(str(f), str(dest))
                
        # Move clean parquet files to clean/
        if clean_files:
            clean_dir = cohort_dir / "clean"
            clean_dir.mkdir(parents=True, exist_ok=True)
            logging.info(f"  -> Moving {len(clean_files)} clean files to {clean_dir.relative_to(BASE_DIR)}")
            for f in clean_files:
                dest = clean_dir / f.name
                shutil.move(str(f), str(dest))
                
    # Rebuild the global registry to ensure all cache pointers map to the new "raw/" path structure
    logging.info("Rebuilding global trajectory registry...")
    build_global_manifest()
    logging.info("Migration complete!")

if __name__ == "__main__":
    migrate()
