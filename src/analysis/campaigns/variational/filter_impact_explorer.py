import argparse
import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from src.common.registry_utils import load_clean_cohort
from src.core.corridor.stability_worker import _load_route_flights
from src.common.adapters import dataframe_to_pycontrails

logger = logging.getLogger(__name__)

def explore_thresholds(
    threshold_speed: float, 
    threshold_dist: float, 
    num_plots: int,
    out_dir: Path
):
    df = load_clean_cohort(require_metrics=True)
    if df.empty:
        print("Cohort registry is empty.")
        return

    df['route'] = df['flight_id'].apply(lambda x: x.split('_')[2] if len(x.split('_')) > 2 else 'UNK')

    print("\n--- Global Distribution ---")
    metrics = ['metric_max_coord_horiz_speed_kt', 'metric_dep_horiz_dist_m', 'metric_arr_horiz_dist_m']
    for m in metrics:
        if m in df.columns:
            print(f"\n{m} percentiles:")
            print(df[m].quantile([0.5, 0.75, 0.8, 0.9, 0.95, 0.99, 1.0]).to_string())

    print(f"\n--- Testing Thresholds: Speed > {threshold_speed} kt OR Dist > {threshold_dist} m ---")
    
    # Evaluate fails
    speed_fail = df['metric_max_coord_horiz_speed_kt'] > threshold_speed if 'metric_max_coord_horiz_speed_kt' in df.columns else False
    dep_fail = df['metric_dep_horiz_dist_m'] > threshold_dist if 'metric_dep_horiz_dist_m' in df.columns else False
    arr_fail = df['metric_arr_horiz_dist_m'] > threshold_dist if 'metric_arr_horiz_dist_m' in df.columns else False
    
    df['failed_thresholds'] = speed_fail | dep_fail | arr_fail

    route_impact = df.groupby('route')['failed_thresholds'].agg(['count', 'sum']).rename(columns={'count': 'total', 'sum': 'dropped'})
    route_impact['drop_pct'] = (route_impact['dropped'] / route_impact['total'] * 100).round(1)
    
    print("\nTop 15 routes by drop percentage (min 50 flights):")
    large_routes = route_impact[route_impact['total'] >= 50].sort_values('drop_pct', ascending=False)
    print(large_routes.head(15))
    
    if num_plots > 0:
        print(f"\nGenerating plots for the top {num_plots} most impacted routes...")
        out_dir.mkdir(parents=True, exist_ok=True)
        routes_to_plot = large_routes.head(num_plots).index.tolist()
        
        for route in routes_to_plot:
            plot_route_cheap(route, df, out_dir)

def plot_route_cheap(route: str, df_reg: pd.DataFrame, out_dir: Path):
    """
    Very cheap plotting: just plots longitude/latitude without map backgrounds 
    to see the shape of accepted vs rejected flights.
    """
    print(f"Loading flights for {route}...")
    flights = _load_route_flights(route, n_target=9999, registry_df=df_reg)
    
    route_reg = df_reg[df_reg['route'] == route].set_index('flight_id')
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    kept_count = 0
    dropped_count = 0
    
    for f in flights:
        fid = f.flight_id if hasattr(f, 'flight_id') else f.callsign
        if fid not in route_reg.index:
            continue
            
        failed = route_reg.loc[fid, 'failed_thresholds']
        
        df_traj = f.data if hasattr(f, 'data') else f
        if 'longitude' in df_traj.columns and 'latitude' in df_traj.columns:
            lons = df_traj['longitude']
            lats = df_traj['latitude']
            
            if failed:
                # Plot dropped flights as red and thin
                ax.plot(lons, lats, color='red', alpha=0.3, linewidth=0.5, zorder=1)
                dropped_count += 1
            else:
                # Plot kept flights as green and thick
                ax.plot(lons, lats, color='green', alpha=0.5, linewidth=1, zorder=2)
                kept_count += 1
                
    ax.set_title(f"{route} (Kept: {kept_count}, Dropped: {dropped_count})")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, linestyle="--", alpha=0.6)
    
    out_path = out_dir / f"{route}_impact.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved plot to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=1000.0, help="Max coord horizontal speed threshold (kt)")
    parser.add_argument("--dist", type=float, default=20000.0, help="Max airport distance threshold (m)")
    parser.add_argument("--plots", type=int, default=3, help="Number of top routes to plot")
    parser.add_argument("--out", type=str, default="data/temp/plots/filter_impact", help="Output directory for plots")
    
    args = parser.parse_args()
    
    # Use standard logger if needed, or just prints for exploration
    explore_thresholds(args.speed, args.dist, args.plots, Path(args.out))
