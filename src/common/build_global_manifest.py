import os
import glob
import pandas as pd
from pathlib import Path
import logging

from src.common.config import (
    BASE_DIR, TRAJECTORIES_DIR, RESULTS_DIR, CORRIDOR_PATHS_DIR, REGISTRIES_DIR,
    GLOBAL_TRAJECTORY_REGISTRY, GLOBAL_CLEAN_REGISTRY, GLOBAL_SIMULATION_REGISTRY,
    GLOBAL_CORRIDOR_SIM_REGISTRY, GLOBAL_MODEL_REGISTRY
)
from src.common.registry_utils import save_model_registry

logger = logging.getLogger(__name__)

def index_parquet_files(pattern: str, registry_file: Path, search_dirs: list, description: str):
    logger.info(f"--- Rebuilding/Updating {description} Registry ---")
    
    # 1. Search directories for matching parquet files
    found_files = []
    for s_dir in search_dirs:
        if s_dir.exists():
            glob_pattern = str(s_dir / "**" / pattern)
            found_files.extend(glob.glob(glob_pattern, recursive=True))
            
    logger.info(f"Found {len(found_files)} files matching '{pattern}' on disk.")
    
    # Load existing registry if it exists
    existing_df = None
    indexed_files = set()
    if registry_file.exists():
        try:
            existing_df = pd.read_parquet(registry_file)
            if not existing_df.empty and 'file_path' in existing_df.columns:
                # Verify that all files in the manifest still exist on disk
                original_len = len(existing_df)
                existing_df = existing_df[existing_df['file_path'].apply(lambda p: (BASE_DIR / p).exists())]
                pruned_count = original_len - len(existing_df)
                
                if pruned_count > 0:
                    logger.info(f"Pruned {pruned_count} stale entries from {registry_file.name} (associated files were deleted).")
                
                if not existing_df.empty:
                    indexed_files = set(existing_df['file_path'].unique())
                    logger.info(f"Loaded existing registry with {len(indexed_files)} already-indexed files.")
                else:
                    existing_df = None
        except Exception as e:
            logger.warning(f"Could not load existing registry {registry_file.name} ({e}). Rebuilding from scratch.")

    new_mappings = []
    
    # 2. Read flight_ids from only the new/unindexed files
    skipped_count = 0
    for filepath_str in found_files:
        filepath = Path(filepath_str)
        rel_path = filepath.resolve().relative_to(BASE_DIR).as_posix()
        
        if rel_path in indexed_files:
            skipped_count += 1
            continue
            
        try:
            logger.info(f"Indexing new file: {filepath.name}")
            # Read only flight_id column to keep memory usage low
            df = pd.read_parquet(filepath, columns=['flight_id'])
            unique_ids = df['flight_id'].dropna().unique()
            
            for f_id in unique_ids:
                new_mappings.append({
                    "flight_id": f_id,
                    "file_path": rel_path
                })
        except Exception as e:
            logger.error(f"Error reading Parquet file {filepath.name}: {e}")
            
    if skipped_count > 0:
        logger.info(f"Skipped {skipped_count} files that were already indexed.")

    # 3. Merge and save
    if not new_mappings:
        if existing_df is not None:
            logger.info(f"No new files to index. Registry is up to date.")
            df_updated = existing_df
        else:
            logger.warning(f"No flight IDs were extracted for {description}.")
            df_updated = pd.DataFrame(columns=["flight_id", "file_path"])
    else:
        df_new = pd.DataFrame(new_mappings)
        if existing_df is not None:
            df_updated = pd.concat([existing_df, df_new])
        else:
            df_updated = df_new
        # Deduplicate (keep last)
        df_updated = df_updated.drop_duplicates(subset=['flight_id'], keep='last')
        
    # Ensure parent folder exists
    REGISTRIES_DIR.mkdir(parents=True, exist_ok=True)
    
    # Save registry
    df_updated.to_parquet(registry_file, index=False)
    logger.info(f"Successfully generated/updated {description} registry at: {registry_file}")
    logger.info(f"Total flight IDs mapped: {len(df_updated):,}\n")


def index_synthesized_files(registry_file: Path, search_dir: Path):
    logger.info("--- Rebuilding/Updating Synthesized Registry ---")
    if not search_dir.exists():
        logger.info("Synthesized folder does not exist. Skipping.")
        return
        
    found_files = glob.glob(str(search_dir / "**" / "*_synthesized_c*.parquet"), recursive=True)
    logger.info(f"Found {len(found_files)} synthesized files on disk.")
    
    # Load RouteSummary to resolve rank for each route
    from src.common.utils import load_route_summary
    df_summary = load_route_summary()
    route_to_rank = {}
    if not df_summary.empty:
        for _, row in df_summary.iterrows():
            # Standardize route format e.g. "LIRF-LFMN"
            route_key = row['route'].replace(" -> ", "-").strip()
            route_to_rank[route_key] = row['rank']
            
    new_entries = []
    for filepath_str in found_files:
        filepath = Path(filepath_str)
        rel_path = filepath.resolve().relative_to(BASE_DIR).as_posix()
        
        name = filepath.name
        if name.endswith(".parquet") and "_synthesized_c" in name:
            try:
                base_part = name.replace(".parquet", "")
                route_part, c_part = base_part.split("_synthesized_c")
                route = route_part.strip()
                cluster_id = int(c_part.strip())
                rank = route_to_rank.get(route, -1)
                
                # Load route_class column from file to get metadata
                try:
                    df_first = pd.read_parquet(filepath, columns=['route_class'])
                    route_class = int(df_first['route_class'].iloc[0])
                except Exception:
                    route_class = 1 # fallback
                    
                new_entries.append({
                    "route": route,
                    "rank": rank,
                    "file_path": rel_path,
                    "route_class": route_class,
                    "cluster_id": cluster_id
                })
            except Exception as e:
                logger.warning(f"Failed to parse or read synthesized file {name}: {e}")
            
    df_updated = pd.DataFrame(new_entries)
    if df_updated.empty:
        df_updated = pd.DataFrame(columns=["route", "rank", "file_path", "route_class", "cluster_id"])
        
    save_model_registry(df_updated)
    logger.info(f"Successfully generated synthesized registry at: {registry_file} ({len(df_updated)} entries)\n")


def build_global_manifest():
    # 1. Raw trajectories registry
    index_parquet_files(
        pattern="*_raw.parquet",
        registry_file=GLOBAL_TRAJECTORY_REGISTRY,
        search_dirs=[
            TRAJECTORIES_DIR
        ],
        description="Raw Trajectory"
    )
    
    # 2. Clean EKF trajectories registry
    index_parquet_files(
        pattern="*_clean_si.parquet",
        registry_file=GLOBAL_CLEAN_REGISTRY,
        search_dirs=[
            TRAJECTORIES_DIR
        ],
        description="Clean EKF Trajectory"
    )
    
    # 3. Simulated outputs registry
    index_parquet_files(
        pattern="*_simulated.parquet",
        registry_file=GLOBAL_SIMULATION_REGISTRY,
        search_dirs=[
            RESULTS_DIR,
            TRAJECTORIES_DIR
        ],
        description="Physics Simulation"
    )
    
    # 4. Corridor simulated outputs registry
    index_parquet_files(
        pattern="*_simulated.parquet",
        registry_file=GLOBAL_CORRIDOR_SIM_REGISTRY,
        search_dirs=[
            RESULTS_DIR / "corridor_simulations"
        ],
        description="Corridor Physics Simulation"
    )
    
    # 5. Corridor paths registry
    index_synthesized_files(
        registry_file=GLOBAL_MODEL_REGISTRY,
        search_dir=CORRIDOR_PATHS_DIR
    )

if __name__ == "__main__":
    setup_file_logger(log_filename="manifest.log")
    build_global_manifest()
