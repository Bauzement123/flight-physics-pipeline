import os
import pickle
import hashlib
import uuid
import re
import pandas as pd
from pathlib import Path
import logging

from src.common.config import FLIGHT_REGISTRY_DIR, TRAJECTORIES_DIR

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

def load_route_summary(summary_path=None) -> pd.DataFrame:
    """
    Safely loads the RouteSummary pickle file and returns a DataFrame.
    """
    if summary_path is None:
        summary_path = FLIGHT_REGISTRY_DIR / "master_flights_RouteSummary.pkl"
    
    path = Path(summary_path)
    if not path.exists():
        logging.error(f"RouteSummary pickle file not found at: {path}")
        return pd.DataFrame()

    try:
        with open(path, 'rb') as f:
            df = pickle.load(f)
        return df
    except Exception as e:
        logging.error(f"Error loading RouteSummary pickle: {e}")
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
    typecode=None
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
        
    base_prefix = "_".join(parts)
    
    # Compute a deterministic hash of the parameters to guarantee unique identification
    hash_suffix = hashlib.md5(base_prefix.encode('utf-8')).hexdigest()[:6]
    
    dataset_name = f"{base_prefix}_{hash_suffix}"
    return dataset_name


def setup_file_logger(out_dir: Path) -> logging.FileHandler:
    """
    Adds a FileHandler to the root logger to mirror console output to <out_dir>/extraction.log.
    If a handler for that file already exists, it does not add a duplicate.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = (out_dir / "extraction.log").resolve()
    
    root_logger = logging.getLogger()
    # Check if already added
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    return handler
            except Exception:
                pass
                
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
    root_logger.addHandler(file_handler)
    return file_handler


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
        logging.info(f"Updated global registry {registry_file.name} with {len(new_entries)} entries.")
    except Exception as e:
        logging.error(f"Failed to update global registry {registry_file.name}: {e}")


