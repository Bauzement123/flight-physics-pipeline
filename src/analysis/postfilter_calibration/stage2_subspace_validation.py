import pandas as pd
from pathlib import Path
import logging
from src.common.utils import setup_file_logger, split_route_string

logger = logging.getLogger(__name__)


def _split_canonical(canon: str) -> tuple[str, str]:
    """Splits a 2-letter country-prefix canonical route like 'ED-EG' -> ('ED', 'EG').
    These are PostFilter calibration internal format, not 4-letter ICAOs."""
    parts = canon.split('-', 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ('UNK', 'UNK')


def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 2: Subspace Failure Comparison (Cross-mapped)")

    in_file = Path("data/calibration/postfilter_calibration/stage1_directional.csv")
    if not in_file.exists():
        logger.error(f"Input file not found: {in_file}")
        return

    df = pd.read_csv(in_file)
    df = df[df['Total_Flights'] >= 10].copy()

    def get_canonical(directed_route):
        dep, arr = split_route_string(directed_route)
        return "-".join(sorted([dep, arr]))
        
    df['Canonical_Route'] = df['Directed_Route'].apply(get_canonical)
    
    # We only want canonical routes that have data for BOTH directions
    routes_with_pairs = df.groupby('Canonical_Route')['Directed_Route'].nunique()
    valid_canonicals = routes_with_pairs[routes_with_pairs == 2].index

    logger.info(f"Found {len(valid_canonicals)} canonical routes with bi-directional data.")

    from src.common.config import (
        METRIC_COL_MAX_HORIZ_VEL,
        METRIC_COL_MAX_VERT_VEL,
        METRIC_COL_MAX_COORD_HORIZ_VEL,
        METRIC_COL_MAX_COORD_VERT_VEL,
        METRIC_COL_MAX_ACCEL,
        METRIC_COL_DEP_HORIZ_DIST,
        METRIC_COL_DEP_VERT_DIST,
        METRIC_COL_ARR_HORIZ_DIST,
        METRIC_COL_ARR_VERT_DIST,
    )

    comparisons = [
        (METRIC_COL_MAX_HORIZ_VEL, METRIC_COL_MAX_HORIZ_VEL, METRIC_COL_MAX_HORIZ_VEL),
        (METRIC_COL_MAX_VERT_VEL, METRIC_COL_MAX_VERT_VEL, METRIC_COL_MAX_VERT_VEL),
        (METRIC_COL_MAX_COORD_HORIZ_VEL, METRIC_COL_MAX_COORD_HORIZ_VEL, METRIC_COL_MAX_COORD_HORIZ_VEL),
        (METRIC_COL_MAX_COORD_VERT_VEL, METRIC_COL_MAX_COORD_VERT_VEL, METRIC_COL_MAX_COORD_VERT_VEL),
        (METRIC_COL_MAX_ACCEL, METRIC_COL_MAX_ACCEL, METRIC_COL_MAX_ACCEL),
        
        # Cross-mapped Asymmetric terminal metrics
        ('crossmap_Country1_horiz_radar', METRIC_COL_DEP_HORIZ_DIST, METRIC_COL_ARR_HORIZ_DIST),
        ('crossmap_Country2_horiz_radar', METRIC_COL_ARR_HORIZ_DIST, METRIC_COL_DEP_HORIZ_DIST),
        ('crossmap_Country1_vert_radar', METRIC_COL_DEP_VERT_DIST, METRIC_COL_ARR_VERT_DIST),
        ('crossmap_Country2_vert_radar', METRIC_COL_ARR_VERT_DIST, METRIC_COL_DEP_VERT_DIST)
    ]

    percentiles = ['75th', '80th', '85th', '90th', '95th', '99th']
    results = []

    for comp_name, m_A, m_B in comparisons:
        for pct in percentiles:
            dir_a_vals = []
            dir_b_vals = []
            
            for canon in valid_canonicals:
                # Dir A is always the alphabetical first string, Dir B is the second
                dir_a_name, dir_b_name = _split_canonical(canon)
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
    
    out_dir = Path("data/calibration/postfilter_calibration")
    out_file = out_dir / "stage2_subspace_validation.csv"
    
    res_df.to_csv(out_file, index=False)
    logger.info(f"Stage 2 complete. Saved validation metrics to {out_file}")
    print(f"Stage 2 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
