import pandas as pd
from pathlib import Path
import logging
from src.common.utils import setup_file_logger
from src.common.registry_utils import load_clean_cohort

logger = logging.getLogger(__name__)

def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 3: Undirected Re-aggregation & Directional Stress Test")

    df = load_clean_cohort(require_metrics=True)
    if df.empty:
        logger.error("Cohort registry is empty.")
        return

    # Metrics to correlate
    metrics = [
        'metric_max_horiz_speed_kt', 
        'metric_max_vert_speed_fpm', 
        'metric_max_coord_horiz_speed_kt', 
        'metric_max_coord_vert_speed_fpm', 
        'metric_max_acceleration_mps2', 
        'metric_dep_horiz_dist_m', 
        'metric_dep_vert_dist_m', 
        'metric_arr_horiz_dist_m', 
        'metric_arr_vert_dist_m'
    ]

    available_metrics = [m for m in metrics if m in df.columns]
    df = df[['flight_id'] + available_metrics].dropna(subset=available_metrics).copy()

    def get_route_info(fid):
        parts = fid.split('_')
        if len(parts) > 2:
            route_part = parts[2]
            if '-' in route_part:
                dep, arr = route_part.split('-', 1)
                dep_c, arr_c = dep[:2], arr[:2]
                canon = "-".join(sorted([dep_c, arr_c]))
                # Dir A is the alphabetical first (which matches canon)
                is_dir_a = (f"{dep_c}-{arr_c}" == canon)
                return canon, is_dir_a
        return None, None

    route_info = df['flight_id'].apply(get_route_info)
    df['Canonical_Route'] = [x[0] for x in route_info]
    df['Is_Dir_A'] = [x[1] for x in route_info]
    
    df = df.dropna(subset=['Canonical_Route'])

    # Filter to only routes that have at least 10 flights in BOTH directions
    counts = df.groupby(['Canonical_Route', 'Is_Dir_A']).size().unstack(fill_value=0)
    valid_canonicals = counts[(counts[True] >= 10) & (counts[False] >= 10)].index
    
    df = df[df['Canonical_Route'].isin(valid_canonicals)].copy()
    logger.info(f"Found {len(valid_canonicals)} canonical routes with >= 10 flights in both directions.")

    # Cross-map the terminal metrics for Dir B (Is_Dir_A == False)
    # This aligns the radar metrics to Country 1 and Country 2
    dir_b_mask = ~df['Is_Dir_A']
    
    if 'metric_dep_horiz_dist_m' in available_metrics and 'metric_arr_horiz_dist_m' in available_metrics:
        temp_dep = df.loc[dir_b_mask, 'metric_dep_horiz_dist_m'].copy()
        temp_arr = df.loc[dir_b_mask, 'metric_arr_horiz_dist_m'].copy()
        df.loc[dir_b_mask, 'metric_dep_horiz_dist_m'] = temp_arr
        df.loc[dir_b_mask, 'metric_arr_horiz_dist_m'] = temp_dep
        
    if 'metric_dep_vert_dist_m' in available_metrics and 'metric_arr_vert_dist_m' in available_metrics:
        temp_dep = df.loc[dir_b_mask, 'metric_dep_vert_dist_m'].copy()
        temp_arr = df.loc[dir_b_mask, 'metric_arr_vert_dist_m'].copy()
        df.loc[dir_b_mask, 'metric_dep_vert_dist_m'] = temp_arr
        df.loc[dir_b_mask, 'metric_arr_vert_dist_m'] = temp_dep

    # Calculate correlation matrices
    all_matrices = []
    
    # We use Spearman rank correlation because these metrics have extreme outliers (GPS glitches)
    for canon, group in df.groupby('Canonical_Route'):
        group_A = group[group['Is_Dir_A']][available_metrics]
        group_B = group[~group['Is_Dir_A']][available_metrics]
        group_AUB = group[available_metrics]
        
        corr_A = group_A.corr(method='spearman')
        corr_B = group_B.corr(method='spearman')
        corr_AUB = group_AUB.corr(method='spearman')
        corr_Diff = corr_A - corr_B
        
        # Add index identifiers
        corr_A['Direction_Type'] = 'Dir_A'
        corr_B['Direction_Type'] = 'Dir_B'
        corr_AUB['Direction_Type'] = 'Dir_AUB'
        corr_Diff['Direction_Type'] = 'Dir_A_minus_B'
        
        for corr_df in [corr_A, corr_B, corr_AUB, corr_Diff]:
            corr_df['Canonical_Route'] = canon
            corr_df['Metric_Row'] = corr_df.index
            all_matrices.append(corr_df)
            
    if not all_matrices:
        logger.warning("No matrices generated.")
        return
        
    final_df = pd.concat(all_matrices, ignore_index=True)
    
    # Calculate Global Mean and Median for Dir_AUB using Fisher Z-transform
    # 1. Filter out the AUB matrices
    aub_df = final_df[final_df['Direction_Type'] == 'Dir_AUB'].copy()
    
    # 2. Extract 3D numpy array of the matrices [N_routes, 9_metrics, 9_metrics]
    # We group by Canonical_Route to ensure ordered extraction
    matrices = []
    for canon, group in aub_df.groupby('Canonical_Route'):
        group_sorted = group.set_index('Metric_Row').loc[available_metrics, available_metrics]
        matrices.append(group_sorted.values.astype(float))
        
    import numpy as np
    matrices = np.array(matrices)
    
    # 3. Fisher Z-transform: z = arctanh(r). Clip r to avoid infinity on diagonals
    r_clipped = np.clip(matrices, -0.999999, 0.999999)
    z_matrices = np.arctanh(r_clipped)
    
    # 4. Calculate Mean and Median in Z-space
    z_mean = np.nanmean(z_matrices, axis=0)
    z_median = np.nanmedian(z_matrices, axis=0)
    
    # 5. Inverse transform back to r-space: r = tanh(z)
    r_mean = np.tanh(z_mean)
    r_median = np.tanh(z_median)
    
    # Force exact 1.0 on diagonals
    np.fill_diagonal(r_mean, 1.0)
    np.fill_diagonal(r_median, 1.0)
    
    # 6. Format back into DataFrames
    mean_df = pd.DataFrame(r_mean, index=available_metrics, columns=available_metrics)
    mean_df['Direction_Type'] = 'Dir_AUB'
    mean_df['Canonical_Route'] = 'GLOBAL_MEAN'
    mean_df['Metric_Row'] = mean_df.index
    
    median_df = pd.DataFrame(r_median, index=available_metrics, columns=available_metrics)
    median_df['Direction_Type'] = 'Dir_AUB'
    median_df['Canonical_Route'] = 'GLOBAL_MEDIAN'
    median_df['Metric_Row'] = median_df.index
    
    # Append to final_df
    final_df = pd.concat([final_df, mean_df, median_df], ignore_index=True)
    
    # Set MultiIndex
    final_df = final_df.set_index(['Canonical_Route', 'Direction_Type', 'Metric_Row'])
    # Ensure columns are just the 9 metrics
    final_df = final_df[available_metrics]
    
    out_dir = Path("data/calibration/PostFilter_callibration/stage3")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage3_correlations.parquet"
    
    final_df.to_parquet(out_file)
    logger.info(f"Stage 3 complete. Saved 3 correlation matrices per canonical route to {out_file}")
    print(f"Stage 3 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
