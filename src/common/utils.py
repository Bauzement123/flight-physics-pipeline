import os
import pickle
import hashlib
import uuid
import re
import pandas as pd
from pathlib import Path
import logging

from src.common.config import ROUTE_SUMMARY_PARQUET, TRAJECTORIES_DIR

logger = logging.getLogger(__name__)

def load_route_summary(summary_path=None) -> pd.DataFrame:
    """
    Safely loads the RouteSummary file (supports parquet, pickle, csv) and returns a DataFrame.
    """
    if summary_path is None:
        summary_path = ROUTE_SUMMARY_PARQUET
        logger.info(f"Loading RouteSummary from default: {summary_path}")
    
    path = Path(summary_path)
    if not path.exists():
        logger.error(f"RouteSummary file not found at: {path}")
        return pd.DataFrame()

    suffix = path.suffix.lower()
    try:
        if suffix == '.parquet':
            df = pd.read_parquet(path)
        elif suffix in ('.pkl', '.pickle'):
            df = pd.read_pickle(path)
        elif suffix == '.csv':
            df = pd.read_csv(path)
        else:
            logger.warning(f"Unknown extension '{suffix}' for route summary path '{path}'. Trying multiple loaders...")
            try:
                df = pd.read_parquet(path)
            except Exception:
                try:
                    df = pd.read_pickle(path)
                except Exception:
                    df = pd.read_csv(path)
        return df
    except Exception as e:
        logger.error(f"Error loading RouteSummary file ({suffix}): {e}")
        return pd.DataFrame()

def split_route_string(route_str: str) -> tuple:
    """
    Splits a route string of format 'DEP -> ARR' into (departure, arrival).
    Returns ('UNK', 'UNK') on failure.
    """
    if not isinstance(route_str, str) or ' -> ' not in route_str:
        return 'UNK', 'UNK'
    try:
        dep, arr = route_str.split(' -> ', 1)
        return dep.strip(), arr.strip()
    except Exception:
        return 'UNK', 'UNK'

def generate_dataset_name(
    ranks=None,
    lower_rank=None,
    upper_rank=None,
    strategy=None,
    value=None,
    seed=None,
    fetch_format=None,
    start_date=None,
    end_date=None,
    typecode=None,
    min_distance=None
) -> str:
    """
    Generates a unique dataset name incorporating all CLI parameters to make it human-readable,
    plus a deterministic parameter hash suffix.
    e.g., ranks_1-5_strat_fixed_val_50_seed_42_format_roundtrip_a9f1
    """
    parts = []
    
    # 1. Ranks / corridor part
    if ranks:
        if isinstance(ranks, list):
            ranks_str = "-".join(map(str, sorted(ranks)))
        else:
            ranks_str = str(ranks).replace(",", "-").replace(" ", "")
        parts.append(f"ranks_{ranks_str}")
    elif lower_rank is not None and upper_rank is not None:
        parts.append(f"ranks_{lower_rank}to{upper_rank}")
    else:
        parts.append("ranks_AllFlights")
        
    # 2. Strategy part
    if strategy is not None:
        parts.append(f"strat_{strategy}")
        
    # 3. Value part
    if value is not None:
        parts.append(f"val_{value}")
        
    # 4. Seed part
    if seed is not None:
        parts.append(f"seed_{seed}")
        
    # 5. Format part (oneway/roundtrip)
    if fetch_format is not None:
        parts.append(f"format_{fetch_format}")
        
    # 6. Start date part
    if start_date is not None:
        clean_start = str(start_date).replace(":", "-").replace(" ", "_")
        parts.append(f"start_{clean_start}")
        
    # 7. End date part
    if end_date is not None:
        clean_end = str(end_date).replace(":", "-").replace(" ", "_")
        parts.append(f"end_{clean_end}")
        
    # 8. Typecode part
    if typecode is not None:
        parts.append(f"type_{typecode}")
        
    # 9. Min distance part
    if min_distance is not None:
        parts.append(f"mindist_{min_distance}")
        
    base_prefix = "_".join(parts)
    
    # Compute a deterministic hash of the parameters to guarantee unique identification
    hash_suffix = hashlib.md5(base_prefix.encode('utf-8')).hexdigest()[:6]
    
    dataset_name = f"{base_prefix}_{hash_suffix}"
    return dataset_name



def setup_file_logger(out_dir: Path = None, log_filename: str = "extraction.log") -> logging.FileHandler:
    """
    Adds a FileHandler to the root logger to mirror console output to LOGS_DIR / log_filename.
    Also ensures the root logger level is set to INFO and a StreamHandler is present for console logging.
    """
    from src.common.config import LOGS_DIR
    
    # Handle cases where log_filename was passed as the first positional argument
    if isinstance(out_dir, str) and out_dir.endswith(".log"):
        log_filename = out_dir
        
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = (LOGS_DIR / log_filename).resolve()
    
    root_logger = logging.getLogger()
    
    # Ensure root logger level is at least INFO
    if root_logger.level == logging.WARNING or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
        
    # Check if handlers already exist
    file_handler_exists = False
    stream_handler_exists = False
    
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    file_handler_exists = True
            except Exception:
                pass
        elif isinstance(handler, logging.StreamHandler):
            stream_handler_exists = True
            
    # Add StreamHandler for console output if missing
    if not stream_handler_exists:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)
        
    # Add FileHandler if missing
    if not file_handler_exists:
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - [%(name)s] - [%(levelname)s] - %(message)s'))
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        return file_handler
        
    # Find and return the existing file handler
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    return handler
            except Exception:
                pass


def update_global_registry(registry_file: Path, new_entries: list):
    """
    Appends new mapping entries to a global registry Parquet file.
    Deduplicates on flight_id to keep only the latest path.
    new_entries: list of dicts: [{"flight_id": str, "file_path": str}, ...]
    """
    if not new_entries:
        return
        
    try:
        df_new = pd.DataFrame(new_entries)
        if registry_file.exists():
            df_reg = pd.read_parquet(registry_file)
            df_updated = pd.concat([df_reg, df_new]).drop_duplicates(subset=['flight_id'], keep='last')
        else:
            df_updated = df_new
            
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        df_updated.to_parquet(registry_file, index=False)
        logger.info(f"Updated global registry {registry_file.name} with {len(new_entries)} entries.")
    except Exception as e:
        logger.error(f"Failed to update global registry {registry_file.name}: {e}")


