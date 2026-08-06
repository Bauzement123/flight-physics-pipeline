import pandas as pd
import numpy as np
from pathlib import Path
import logging
from src.common.utils import setup_file_logger, extract_target_routes
from src.common.config import BASE_DIR, DEFAULT_PREFILTER_THRESHOLDS

logger = logging.getLogger(__name__)

def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 5: Master Population Terminal Analysis")

    # 1. Get the Top 4000 routes used by the pipeline
    df_top4k = extract_target_routes(lower=1, upper=4000)
    if df_top4k.empty:
        logger.error("Failed to load Top 4000 routes from summary.")
        return
    df_top4k['route'] = df_top4k['dep'] + " -> " + df_top4k['arr']
    valid_route_strs = set(df_top4k['route'].tolist())
    logger.info(f"Loaded {len(valid_route_strs)} target routes from summary.")

    # 2. Load the absolute raw universe of flights
    master_path = BASE_DIR / "data" / "databases" / "master_flights" / "master_flights.parquet"
    if not master_path.exists():
        logger.error(f"Master flights not found at {master_path}")
        return
        
    logger.info("Loading master flights database...")
    df_master = pd.read_parquet(master_path)
    
    # 3. Filter down to Top 4k
    df_master["estdepartureairport"] = df_master["estdepartureairport"].astype(str).str.strip()
    df_master["estarrivalairport"] = df_master["estarrivalairport"].astype(str).str.strip()
    df_master["route"] = df_master["estdepartureairport"] + " -> " + df_master["estarrivalairport"]
    
    df_filtered = df_master[df_master["route"].isin(valid_route_strs)].copy()
    logger.info(f"Filtered down to {len(df_filtered)} flights in the Top 4000 corridors.")
    
    # 4. Map to Canonical Route
    def get_canon(r_str):
        parts = r_str.split(' -> ')
        if len(parts) == 2:
            return "-".join(sorted([parts[0][:2], parts[1][:2]]))
        return None
        
    df_filtered['Canonical_Route'] = df_filtered['route'].apply(get_canon)
    df_filtered = df_filtered.dropna(subset=['Canonical_Route'])
    
    route_counts = df_filtered['Canonical_Route'].value_counts()
    valid_canonicals = route_counts[route_counts >= 50].index  # Requires at least 50 flights in raw DB
    df_filtered = df_filtered[df_filtered['Canonical_Route'].isin(valid_canonicals)].copy()

    # 5. Define sweep grids matching Stage 5 Plan
    grids = {
        'estdepartureairportvertdistance': np.arange(1000, 11000, 1000),
        'estarrivalairportvertdistance': np.arange(1000, 11000, 1000),
        'estdepartureairporthorizdistance': np.arange(15000, 52500, 2500),
        'estarrivalairporthorizdistance': np.arange(15000, 52500, 2500)
    }

    results = []
    
    for metric, threshold_grid in grids.items():
        if metric not in df_filtered.columns:
            logger.warning(f"Metric {metric} missing, skipping.")
            continue
            
        metric_df = df_filtered[['Canonical_Route', metric]].dropna().copy()
        total_global = len(metric_df)
        
        # Global curve
        for t in threshold_grid:
            survived_global = (metric_df[metric] <= t).sum()
            rate_global = survived_global / total_global if total_global > 0 else 0
            results.append({
                'Canonical_Route': 'GLOBAL_AVERAGE',
                'Metric': metric,
                'Threshold': float(t),
                'Survival_Rate': float(rate_global)
            })
            
        # Regional curve
        grouped = metric_df.groupby('Canonical_Route')[metric]
        totals = grouped.count()
        
        for canon in totals.index:
            total_route = totals.loc[canon]
            route_vals = metric_df.loc[metric_df['Canonical_Route'] == canon, metric]
            
            for t in threshold_grid:
                survived_route = (route_vals <= t).sum()
                rate_route = survived_route / total_route if total_route > 0 else 0
                results.append({
                    'Canonical_Route': canon,
                    'Metric': metric,
                    'Threshold': float(t),
                    'Survival_Rate': float(rate_route)
                })

    final_df = pd.DataFrame(results)
    
    out_dir = Path("data/calibration/PostFilter_callibration/stage5")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage5_master_sensitivity.parquet"
    
    final_df.to_parquet(out_file)
    logger.info(f"Stage 5 complete. Saved master sensitivity to {out_file}")
    print(f"Stage 5 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
