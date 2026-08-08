import pandas as pd
from pathlib import Path
import logging
from src.common.utils import setup_file_logger
from src.common.config import BASE_DIR

logger = logging.getLogger(__name__)

def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 0: Data Merger & Preparation")
    
    # 1. Dynamic Loading
    sources_dir = BASE_DIR / "data" / "calibration" / "PostFilter_callibration" / "data" / "sources"
    if not sources_dir.exists():
        logger.error(f"Sources directory not found: {sources_dir}")
        return
        
    parquet_files = list(sources_dir.glob("*.parquet"))
    if not parquet_files:
        logger.error(f"No parquet files found in {sources_dir}")
        return
        
    logger.info(f"Found {len(parquet_files)} parquet files to merge.")
    
    dfs = []
    for p in parquet_files:
        try:
            df = pd.read_parquet(p)
            dfs.append(df)
            logger.info(f"Loaded {p.name} with {len(df)} rows.")
        except Exception as e:
            logger.error(f"Failed to read {p.name}: {e}")
            
    if not dfs:
        logger.error("No dataframes were loaded successfully.")
        return
        
    # 2. Concatenation & Deduplication
    df_merged = pd.concat(dfs, ignore_index=True)
    initial_len = len(df_merged)
    df_merged = df_merged.drop_duplicates(subset=['flight_id'], keep='first')
    logger.info(f"Merged dataset has a total of {len(df_merged)} unique rows (dropped {initial_len - len(df_merged)} duplicates).")
    
    # 3. Canonical Macro Route Extraction
    def get_macro_route(fid):
        parts = str(fid).split('_')
        if len(parts) > 2:
            route_part = parts[2]
            if '-' in route_part:
                dep, arr = route_part.split('-', 1)
                dep_c, arr_c = dep[:2], arr[:2]
                canon = "-".join(sorted([dep_c, arr_c]))
                return canon
        return "UNK"
        
    df_merged['macro_route'] = df_merged['flight_id'].apply(get_macro_route)
    
    # 4. Save Output
    out_dir = BASE_DIR / "data" / "calibration" / "PostFilter_callibration" / "data"
    out_file = out_dir / "merged_registry.parquet"
    
    df_merged.to_parquet(out_file, index=False)
    logger.info(f"Stage 0 complete. Saved merged registry to {out_file}")
    print(f"Stage 0 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
