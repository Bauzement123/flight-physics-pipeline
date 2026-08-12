import pandas as pd
import numpy as np
import logging
from src.common.utils import setup_file_logger, split_route_string
from src.common.registry_utils import load_clean_cohort
from src.common.config import BASE_DIR

import argparse

logger = logging.getLogger(__name__)

def main():
    setup_file_logger(log_filename="calibration.log")
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-registry', type=str, help='Path to merged registry')
    args = parser.parse_args()

    logger.info("Starting Stage 4: Local Sensitivity Analysis (Discrete Sweep)")

    if args.input_registry:
        df = pd.read_parquet(args.input_registry)
    else:
        df = load_clean_cohort(require_metrics=True)
        
    if df.empty:
        logger.error("Cohort registry is empty.")
        return

    def get_route_info(fid):
        parts = fid.split('_')
        if len(parts) > 2:
            route_part = parts[2]
            dep, arr = split_route_string(route_part)
            if dep != "UNK" and arr != "UNK":
                dep_c, arr_c = dep[:2], arr[:2]
                canon = "-".join(sorted([dep_c, arr_c]))
                return canon
        return None

    df['Canonical_Route'] = df['flight_id'].apply(get_route_info)
    df = df.dropna(subset=['Canonical_Route'])

    # Filter to only routes that have at least 10 flights total
    route_counts = df['Canonical_Route'].value_counts()
    valid_canonicals = route_counts[route_counts >= 10].index
    df = df[df['Canonical_Route'].isin(valid_canonicals)].copy()
    
    logger.info(f"Found {len(valid_canonicals)} canonical routes with >= 10 flights for sensitivity analysis.")

    grids = {
        'metric_max_coord_horiz_speed_kt': np.arange(100, 1050, 50),
        'metric_max_coord_vert_speed_fpm': np.arange(1000, 10500, 500),
        'metric_max_acceleration_mps2': np.arange(5, 55, 5),
        'metric_dep_horiz_dist_m': np.arange(10000, 110000, 10000),
        'metric_arr_horiz_dist_m': np.arange(10000, 110000, 10000),
        'metric_dep_vert_dist_m': np.arange(10000, 110000, 10000),
        'metric_arr_vert_dist_m': np.arange(10000, 110000, 10000)
    }

    results = []

    for metric, threshold_grid in grids.items():
        if metric not in df.columns:
            logger.warning(f"Metric {metric} not found in dataframe, skipping.")
            continue
            
        metric_df = df[['Canonical_Route', metric]].dropna().copy()
        
        # Calculate survival for global average
        total_global = len(metric_df)
        for t in threshold_grid:
            survived_global = (metric_df[metric] <= t).sum()
            rate_global = survived_global / total_global if total_global > 0 else 0
            results.append({
                'Canonical_Route': 'GLOBAL_AVERAGE',
                'Metric': metric,
                'Threshold': float(t),
                'Survival_Rate': float(rate_global)
            })
            
        # Calculate survival per route
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
    
    out_dir = BASE_DIR / "data" / "calibration" / "postfilter_calibration" / "stage4"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage4_sensitivity_sweep.parquet"
    
    final_df.to_parquet(out_file)
    logger.info(f"Stage 4 complete. Saved sensitivity grid to {out_file}")
    print(f"Stage 4 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
