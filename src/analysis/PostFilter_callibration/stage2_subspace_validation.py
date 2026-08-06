import pandas as pd
from pathlib import Path
import logging
from src.common.utils import setup_file_logger

logger = logging.getLogger(__name__)

def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 2: Subspace Failure Comparison (Cross-mapped)")

    in_file = Path("data/calibration/PostFilter_callibration/stage1_directional.csv")
    if not in_file.exists():
        logger.error(f"Input file not found: {in_file}")
        return

    df = pd.read_csv(in_file)
    df = df[df['Total_Flights'] >= 10].copy()

    def get_canonical(directed_route):
        dep, arr = directed_route.split('-')
        return "-".join(sorted([dep, arr]))
        
    df['Canonical_Route'] = df['Directed_Route'].apply(get_canonical)
    
    # We only want canonical routes that have data for BOTH directions
    routes_with_pairs = df.groupby('Canonical_Route')['Directed_Route'].nunique()
    valid_canonicals = routes_with_pairs[routes_with_pairs == 2].index

    logger.info(f"Found {len(valid_canonicals)} canonical routes with bi-directional data.")

    # Define the comparisons
    # Tuple: (Comparison_Name, Dir_A_Metric, Dir_B_Metric)
    comparisons = [
        ('metric_max_horiz_speed_kt', 'metric_max_horiz_speed_kt', 'metric_max_horiz_speed_kt'),
        ('metric_max_vert_speed_fpm', 'metric_max_vert_speed_fpm', 'metric_max_vert_speed_fpm'),
        ('metric_max_coord_horiz_speed_kt', 'metric_max_coord_horiz_speed_kt', 'metric_max_coord_horiz_speed_kt'),
        ('metric_max_coord_vert_speed_fpm', 'metric_max_coord_vert_speed_fpm', 'metric_max_coord_vert_speed_fpm'),
        ('metric_max_acceleration_mps2', 'metric_max_acceleration_mps2', 'metric_max_acceleration_mps2'),
        
        # Cross-mapped Asymmetric terminal metrics
        ('crossmap_Country1_horiz_radar', 'metric_dep_horiz_dist_m', 'metric_arr_horiz_dist_m'),
        ('crossmap_Country2_horiz_radar', 'metric_arr_horiz_dist_m', 'metric_dep_horiz_dist_m'),
        ('crossmap_Country1_vert_radar', 'metric_dep_vert_dist_m', 'metric_arr_vert_dist_m'),
        ('crossmap_Country2_vert_radar', 'metric_arr_vert_dist_m', 'metric_dep_vert_dist_m')
    ]

    percentiles = ['75th', '80th', '85th', '90th', '95th', '99th']
    results = []

    for comp_name, m_A, m_B in comparisons:
        for pct in percentiles:
            dir_a_vals = []
            dir_b_vals = []
            
            for canon in valid_canonicals:
                # Dir A is always the alphabetical first string, Dir B is the second
                dir_a_name, dir_b_name = canon.split('-')
                dir_a_route = f"{dir_a_name}-{dir_b_name}"
                dir_b_route = f"{dir_b_name}-{dir_a_name}"
                
                # Fetch Dir A percentage lost
                a_row = df[(df['Directed_Route'] == dir_a_route) & (df['Metric'] == m_A) & (df['Percentile'] == pct)]
                # Fetch Dir B percentage lost
                b_row = df[(df['Directed_Route'] == dir_b_route) & (df['Metric'] == m_B) & (df['Percentile'] == pct)]
                
                if not a_row.empty and not b_row.empty:
                    dir_a_vals.append(a_row.iloc[0]['Percentage_Lost'])
                    dir_b_vals.append(b_row.iloc[0]['Percentage_Lost'])
                    
            if not dir_a_vals:
                continue
                
            temp_df = pd.DataFrame({'Dir_A': dir_a_vals, 'Dir_B': dir_b_vals})
            mae = (temp_df['Dir_A'] - temp_df['Dir_B']).abs().mean()
            corr = temp_df['Dir_A'].corr(temp_df['Dir_B'])
            
            results.append({
                "Comparison": comp_name,
                "Percentile": pct,
                "BiDirectional_Pairs_Count": len(temp_df),
                "Mean_Absolute_Error_%": mae,
                "Pearson_Correlation": corr
            })
            
    res_df = pd.DataFrame(results)
    
    out_dir = Path("data/calibration/PostFilter_callibration")
    out_file = out_dir / "stage2_subspace_validation.csv"
    
    res_df.to_csv(out_file, index=False)
    logger.info(f"Stage 2 complete. Saved validation metrics to {out_file}")
    print(f"Stage 2 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
