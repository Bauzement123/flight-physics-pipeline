import pandas as pd
from pathlib import Path
import logging

from src.common.utils import setup_file_logger, split_route_string
from src.common.registry_utils import load_clean_cohort

import argparse

logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-registry', type=str, help='Path to merged registry')
    args = parser.parse_args()

    setup_file_logger(log_filename="calibration.log")
    logger.info("Starting Stage 1: Directional 1D Isolation")

    if args.input_registry:
        df = pd.read_parquet(args.input_registry)
    else:
        df = load_clean_cohort(require_metrics=True)
        
    if df.empty:
        logger.error("Cohort registry is empty.")
        return

    # Extract directed macro route (e.g., ED-EY) from flight_id
    # Format: icao24_callsign_dep-arr_timestamp
    def get_directed_route(fid):
        parts = fid.split('_')
        if len(parts) > 2:
            route_part = parts[2]
            dep, arr = split_route_string(route_part)
            if dep != "UNK" and arr != "UNK":
                return f"{dep[:2]}-{arr[:2]}"
        return "UNK"
        
    df['directed_route'] = df['flight_id'].apply(get_directed_route)
    df = df[df['directed_route'] != "UNK"]

    # Metrics to analyze
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
    percentiles = [0.75, 0.80, 0.85, 0.90, 0.95, 0.99]

    # Calculate global thresholds
    global_thresholds = {}
    for m in available_metrics:
        global_thresholds[m] = {p: df[m].quantile(p) for p in percentiles}

    logger.info("Calculated global percentiles.")

    results = []
    
    # Calculate flight drops per directed route per threshold
    route_groups = df.groupby('directed_route')
    for route, group in route_groups:
        total_flights = len(group)
        for m in available_metrics:
            for p in percentiles:
                thresh_val = global_thresholds[m][p]
                
                # Number of flights in this route exceeding the global threshold
                lost = (group[m] > thresh_val).sum()
                pct_lost = (lost / total_flights) * 100 if total_flights > 0 else 0
                
                results.append({
                    "Directed_Route": route,
                    "Total_Flights": total_flights,
                    "Metric": m,
                    "Percentile": f"{int(p*100)}th",
                    "Threshold_Value": thresh_val,
                    "Flights_Lost": lost,
                    "Percentage_Lost": pct_lost
                })

    res_df = pd.DataFrame(results)
    
    out_dir = Path("data/calibration/postfilter_calibration")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stage1_directional.csv"
    
    res_df.to_csv(out_file, index=False)
    logger.info(f"Saved {len(res_df)} records to {out_file}")
    print(f"Stage 1 complete. Saved to {out_file}")

if __name__ == "__main__":
    main()
