import glob
import pandas as pd
from pathlib import Path
import logging
import numpy as np

from src.common.config import (
    BASE_DIR, TRAJECTORIES_DIR, RESULTS_DIR, CORRIDOR_PATHS_DIR, REGISTRIES_DIR,
    GLOBAL_TRAJECTORY_REGISTRY, GLOBAL_CLEAN_REGISTRY, GLOBAL_SIMULATION_REGISTRY,
    GLOBAL_CORRIDOR_SIM_REGISTRY, GLOBAL_MODEL_REGISTRY, RAW_CONCAT_SUFFIX,
    GLOBAL_EKF_DIAG_REGISTRY
)
from src.common.registry_utils import save_model_registry
from src.common.utils import setup_file_logger, to_project_relative

logger = logging.getLogger(__name__)

def _is_excluded_path(path: Path | str, exclude_filename_suffixes: tuple[str, ...]) -> bool:
    """Return True when a discovered registry candidate should be ignored."""
    return bool(exclude_filename_suffixes) and Path(path).name.endswith(exclude_filename_suffixes)


def index_parquet_files(
    pattern: str,
    registry_file: Path,
    search_dirs: list[Path],
    description: str,
    exclude_filename_suffixes: tuple[str, ...] = (),
    force: bool = False,
):
    logger.info(f"--- Rebuilding/Updating {description} Registry (force={force}) ---")
    
    # 1. Search directories for matching parquet files
    found_files = []
    excluded_count = 0
    for s_dir in search_dirs:
        if s_dir.exists():
            glob_pattern = str(s_dir / "**" / pattern)
            candidates = glob.glob(glob_pattern, recursive=True)
            excluded_count += sum(
                1 for filepath in candidates if _is_excluded_path(filepath, exclude_filename_suffixes)
            )
            found_files.extend(
                filepath for filepath in candidates if not _is_excluded_path(filepath, exclude_filename_suffixes)
            )
            
    logger.info(f"Found {len(found_files)} files matching '{pattern}' on disk.")
    if excluded_count:
        logger.info(f"Excluded {excluded_count} concat/backup files from {description} registry indexing.")
    
    # Load existing registry if it exists
    existing_df = None
    indexed_files = set()
    if not force and registry_file.exists():
        try:
            existing_df = pd.read_parquet(registry_file)
            if not existing_df.empty and 'file_path' in existing_df.columns:
                # Verify that all files in the manifest still exist on disk and are still indexable.
                original_len = len(existing_df)
                existing_df = existing_df[
                    existing_df['file_path'].apply(
                        lambda p: (BASE_DIR / p).exists()
                        and not _is_excluded_path(p, exclude_filename_suffixes)
                    )
                ]
                pruned_count = original_len - len(existing_df)
                
                if pruned_count > 0:
                    logger.info(f"Pruned {pruned_count} stale/excluded entries from {registry_file.name}.")
                
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
        rel_path = to_project_relative(filepath)
        
        if rel_path in indexed_files:
            skipped_count += 1
            continue
            
        try:
            logger.info(f"Indexing new file: {filepath.name}")
            if "_simulated.parquet" in pattern:
                try:
                    df = pd.read_parquet(filepath, columns=['flight_id', 'ef', 'fuel_burn'])
                except Exception:
                    df = pd.read_parquet(filepath, columns=['flight_id'])
                    
                unique_ids = df['flight_id'].dropna().unique()
                for f_id in unique_ids:
                    df_fid = df[df['flight_id'] == f_id]
                    total_ef = float(np.nansum(df_fid['ef'])) if 'ef' in df_fid.columns else 0.0
                    total_fuel = float(np.nansum(df_fid['fuel_burn'])) if 'fuel_burn' in df_fid.columns else 0.0
                    new_mappings.append({
                        "flight_id": f_id,
                        "file_path": rel_path,
                        "total_contrail_ef": total_ef,
                        "total_fuel_burn": total_fuel,
                        "cocip_total": int(np.sign(total_ef)) if total_ef != 0.0 else 0
                    })
            else:
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


def index_corridor_models(registry_file: Path, search_dir: Path, force: bool = False):
    logger.info(f"--- Rebuilding/Updating Corridor Model Registry (force={force}) ---")
    if not search_dir.exists():
        logger.info("Corridor models folder does not exist. Skipping.")
        return
        
    found_files = glob.glob(str(search_dir / "**" / "*_corridor_c*.parquet"), recursive=True)
    logger.info(f"Found {len(found_files)} corridor model files on disk.")
    
    # Load RouteSummary to resolve rank for each route
    from src.common.utils import load_route_summary
    df_summary = load_route_summary()
    route_to_rank = {}
    if not df_summary.empty:
        for _, row in df_summary.iterrows():
            # Standardize route format e.g. "LIRF-LFMN"
            route_key = row['route'].replace(" -> ", "-").strip()
            route_to_rank[route_key] = row['rank']

    # Load existing registry if force is False
    existing_df = None
    indexed_files = set()
    if not force and registry_file.exists():
        try:
            existing_df = pd.read_parquet(registry_file)
            if not existing_df.empty and 'file_path' in existing_df.columns:
                original_len = len(existing_df)
                existing_df = existing_df[
                    existing_df['file_path'].apply(lambda p: (BASE_DIR / p).exists())
                ]
                pruned_count = original_len - len(existing_df)
                if pruned_count > 0:
                    logger.info(f"Pruned {pruned_count} stale entries from {registry_file.name}.")
                
                if not existing_df.empty:
                    indexed_files = set(existing_df['file_path'].unique())
                    logger.info(f"Loaded existing registry with {len(indexed_files)} already-indexed files.")
                else:
                    existing_df = None
        except Exception as e:
            logger.warning(f"Could not load existing registry {registry_file.name} ({e}). Rebuilding from scratch.")
            
    new_entries = []
    skipped_count = 0
    for filepath_str in found_files:
        filepath = Path(filepath_str)
        rel_path = to_project_relative(filepath)
        
        if rel_path in indexed_files:
            skipped_count += 1
            continue
            
        name = filepath.name
        if name.endswith(".parquet") and "_corridor_c" in name:
            try:
                base_part = name.replace(".parquet", "")
                route_part, c_part = base_part.split("_corridor_c")
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
                logger.warning(f"Failed to parse or read corridor model file {name}: {e}")

    if skipped_count > 0:
        logger.info(f"Skipped {skipped_count} corridor model files that were already indexed.")
            
    if not new_entries:
        if existing_df is not None:
            logger.info("No new corridor model files to index. Registry is up to date.")
            df_updated = existing_df
        else:
            logger.warning("No corridor model entries were extracted.")
            df_updated = pd.DataFrame(columns=["route", "rank", "file_path", "route_class", "cluster_id"])
    else:
        df_new = pd.DataFrame(new_entries)
        if existing_df is not None:
            df_updated = pd.concat([existing_df, df_new])
        else:
            df_updated = df_new
        df_updated = df_updated.drop_duplicates(subset=['file_path'], keep='last')
        
    save_model_registry(df_updated)
    logger.info(f"Successfully generated corridor model registry at: {registry_file} ({len(df_updated)} entries)\n")


def rebuild_raw_registry(force: bool = False) -> None:
    index_parquet_files(
        pattern="*_raw.parquet",
        registry_file=GLOBAL_TRAJECTORY_REGISTRY,
        search_dirs=[
            TRAJECTORIES_DIR
        ],
        description="Raw Trajectory",
        exclude_filename_suffixes=(RAW_CONCAT_SUFFIX,),
        force=force
    )


def rebuild_clean_registry(force: bool = False) -> None:
    index_parquet_files(
        pattern="*_clean_si.parquet",
        registry_file=GLOBAL_CLEAN_REGISTRY,
        search_dirs=[
            TRAJECTORIES_DIR
        ],
        description="Clean EKF Trajectory",
        force=force
    )


def rebuild_simulation_registry(force: bool = False) -> None:
    index_parquet_files(
        pattern="*_simulated.parquet",
        registry_file=GLOBAL_SIMULATION_REGISTRY,
        search_dirs=[
            RESULTS_DIR,
            TRAJECTORIES_DIR
        ],
        description="Physics Simulation",
        force=force
    )


def rebuild_corridor_sim_registry(force: bool = False) -> None:
    index_parquet_files(
        pattern="*_simulated.parquet",
        registry_file=GLOBAL_CORRIDOR_SIM_REGISTRY,
        search_dirs=[
            RESULTS_DIR / "corridor_simulations"
        ],
        description="Corridor Physics Simulation",
        force=force
    )


def rebuild_model_registry(force: bool = False) -> None:
    index_corridor_models(
        registry_file=GLOBAL_MODEL_REGISTRY,
        search_dir=CORRIDOR_PATHS_DIR,
        force=force
    )


def extract_metrics_from_diag_file(
    diag_path: Path,
    recompute_metrics: bool,
) -> tuple[float, float, float]:
    """
    Extracts EKF quality metrics from a diagnostic npz archive.
    If recompute_metrics is True, calls the recomputation wrapper.
    Otherwise, reads the stored metrics array, falling back to NaNs if missing/corrupt.
    """
    if recompute_metrics:
        from src.core.processing.kalman_filter import compute_ekf_quality_metrics_from_diag
        return compute_ekf_quality_metrics_from_diag(diag_path)

    import numpy as np
    try:
        with np.load(diag_path) as data:
            metrics = data.get("metrics")
            if metrics is None or len(metrics) < 3:
                return float("nan"), float("nan"), float("nan")
            return float(metrics[0]), float(metrics[1]), float(metrics[2])
    except Exception as e:
        logger.warning(f"Failed to read metrics from {diag_path.name}: {e}. Falling back to NaNs.")
        return float("nan"), float("nan"), float("nan")


def _load_existing_diag_registry(force: bool) -> tuple[pd.DataFrame | None, set[str]]:
    """Loads and prunes the existing EKF diagnostic registry if force is False."""
    existing_df = None
    indexed_files = set()
    if not force and GLOBAL_EKF_DIAG_REGISTRY.exists():
        try:
            existing_df = pd.read_parquet(GLOBAL_EKF_DIAG_REGISTRY, memory_map=False)
            if not existing_df.empty and 'diag_file_path' in existing_df.columns:
                original_len = len(existing_df)
                existing_df = existing_df[
                    existing_df['diag_file_path'].apply(lambda p: (BASE_DIR / p).exists())
                ]
                pruned_count = original_len - len(existing_df)
                if pruned_count > 0:
                    logger.info(f"Pruned {pruned_count} stale entries from {GLOBAL_EKF_DIAG_REGISTRY.name}.")

                if not existing_df.empty:
                    indexed_files = set(existing_df['diag_file_path'].unique())
                    logger.info(f"Loaded existing registry with {len(indexed_files)} already-indexed files.")
                else:
                    existing_df = None
        except Exception as e:
            logger.warning(f"Could not load existing registry {GLOBAL_EKF_DIAG_REGISTRY.name} ({e}). Rebuilding from scratch.")
    return existing_df, indexed_files


def _scan_diag_files() -> list[Path]:
    """Scans the trajectories directory for EKF diagnostic npz files."""
    glob_pattern = str(TRAJECTORIES_DIR / "**" / "diagnostics" / "*_ekf_diag.npz")
    found_files = glob.glob(glob_pattern, recursive=True)
    logger.info(f"Found {len(found_files)} diagnostic NPZ files on disk.")
    return [Path(f) for f in found_files]


def _build_diag_rows(
    found_files: list[Path],
    indexed_files: set[str],
    recompute_metrics: bool,
) -> list[dict]:
    """Iterates over diagnostic files, extracting/recomputing metrics and building rows."""
    new_mappings = []
    skipped_count = 0

    for filepath in found_files:
        rel_path = to_project_relative(filepath)

        # Skip already-indexed files only if we are NOT recomputing metrics
        if not recompute_metrics and rel_path in indexed_files:
            skipped_count += 1
            continue

        try:
            if recompute_metrics and rel_path in indexed_files:
                logger.info(f"Recomputing metrics for diagnostic file: {filepath.name}")
            else:
                logger.info(f"Indexing diagnostic file: {filepath.name}")
            
            flight_id = filepath.name.replace("_ekf_diag.npz", "")
            q, nis, tr = extract_metrics_from_diag_file(filepath, recompute_metrics)
            
            new_mappings.append({
                "flight_id": flight_id,
                "diag_file_path": rel_path,
                "ekf_quality_score": q,
                "ekf_max_trace_p": tr,
                "ekf_mean_nis": nis,
            })
        except Exception as e:
            logger.error(f"Error reading diagnostic NPZ file {filepath.name}: {e}")

    if skipped_count > 0:
        logger.info(f"Skipped {skipped_count} diagnostic files that were already indexed.")

    return new_mappings


def _merge_and_save_diag_registry(
    existing_df: pd.DataFrame | None,
    new_mappings: list[dict],
) -> None:
    """Merges new diagnostic entries with the existing registry, deduplicates, and saves."""
    if not new_mappings:
        if existing_df is not None:
            logger.info("No new files to index. Diagnostic registry is up to date.")
            df_updated = existing_df
        else:
            logger.warning("No EKF diagnostics were extracted.")
            df_updated = pd.DataFrame(columns=["flight_id", "diag_file_path", "ekf_quality_score", "ekf_max_trace_p", "ekf_mean_nis"])
    else:
        df_new = pd.DataFrame(new_mappings)
        if existing_df is not None:
            df_updated = pd.concat([existing_df, df_new])
        else:
            df_updated = df_new
        df_updated = df_updated.drop_duplicates(subset=['flight_id'], keep='last')

    REGISTRIES_DIR.mkdir(parents=True, exist_ok=True)
    df_updated.to_parquet(GLOBAL_EKF_DIAG_REGISTRY, index=False)
    logger.info(f"Successfully generated/updated EKF diagnostic registry at: {GLOBAL_EKF_DIAG_REGISTRY}")
    logger.info(f"Total flight IDs mapped: {len(df_updated):,}\n")


def rebuild_ekf_diag_registry(
    force: bool = False,
    recompute_metrics: bool = False,
) -> None:
    """Orchestrates the rebuilding/updating of the EKF diagnostic registry."""
    logger.info(f"--- Rebuilding/Updating EKF Diagnostic Registry (force={force}, recompute={recompute_metrics}) ---")
    existing_df, indexed_files = _load_existing_diag_registry(force)
    found_files = _scan_diag_files()
    new_mappings = _build_diag_rows(found_files, indexed_files, recompute_metrics)
    _merge_and_save_diag_registry(existing_df, new_mappings)


def build_global_manifest() -> None:
    rebuild_raw_registry()
    rebuild_clean_registry()
    rebuild_simulation_registry()
    rebuild_corridor_sim_registry()
    rebuild_model_registry()
    rebuild_ekf_diag_registry()


def main(args_list: list[str] | None = None) -> None:
    import argparse
    from src.common.config import init_runtime
    init_runtime()
    setup_file_logger(log_filename="manifest.log")

    parser = argparse.ArgumentParser(description="Global manifest/registry builder.")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=["raw", "clean", "simulation", "corridor-sim", "model", "ekf-diag"],
        help="Rebuild only specified registries.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Rebuild all registries.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild from scratch (ignore existing registry files).",
    )
    parser.add_argument(
        "--recompute-ekf-metrics",
        action="store_true",
        help="Recompute EKF quality metrics from diagnostic arrays instead of reading stored metrics.",
    )
    parser.add_argument(
        "--diag-only",
        action="store_true",
        help="Convenience alias to rebuild/update only the EKF diagnostic registry.",
    )

    args = parser.parse_args(args_list)

    # Determine what to rebuild
    to_rebuild = set()
    if args.all:
        to_rebuild = {"raw", "clean", "simulation", "corridor-sim", "model", "ekf-diag"}
    elif args.diag_only:
        to_rebuild = {"ekf-diag"}
    elif args.only:
        to_rebuild = set(args.only)
    else:
        # Default behavior (if no flags are provided, run all)
        to_rebuild = {"raw", "clean", "simulation", "corridor-sim", "model", "ekf-diag"}

    # Execute rebuilds
    if "raw" in to_rebuild:
        rebuild_raw_registry(force=args.force)
    if "clean" in to_rebuild:
        rebuild_clean_registry(force=args.force)
    if "simulation" in to_rebuild:
        rebuild_simulation_registry(force=args.force)
    if "corridor-sim" in to_rebuild:
        rebuild_corridor_sim_registry(force=args.force)
    if "model" in to_rebuild:
        rebuild_model_registry(force=args.force)
    if "ekf-diag" in to_rebuild:
        rebuild_ekf_diag_registry(force=args.force, recompute_metrics=args.recompute_ekf_metrics)


if __name__ == "__main__":
    main()
