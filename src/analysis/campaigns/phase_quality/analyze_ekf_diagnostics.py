#!/usr/bin/env python
"""
Deep EKF Tensor Diagnostic Autopsy CLI Entrypoint
"""

import os
import argparse
from pathlib import Path

from src.common.config import GLOBAL_EKF_DIAG_REGISTRY, DATA_DIR, PhaseControl, init_runtime
from src.common.utils import setup_file_logger
from src.analysis.campaigns.phase_quality import io, orchestration


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deep EKF Tensor Diagnostic Autopsy – compressed modular version"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--route", type=str, help="Specific route ID to audit (e.g. EDDF-LIRF)")
    group.add_argument("--all",   action="store_true", help="Audit all routes in registry")
    
    parser.add_argument("--workers",    type=int, default=os.cpu_count() or 4, help="Max process workers to spawn")
    parser.add_argument("--out-dir",    type=str, help="Override default output root")
    parser.add_argument("--test-limit", type=int, help="Limit maximum flights to audit (debug)")
    parser.add_argument(
        "--disable", nargs="+",
        choices=["nis", "residuals", "condition", "report", "tensor", "flat"],
        help="Explicitly disable modular steps (overrides config)"
    )
    return parser.parse_args()


def _apply_cli_disables(cfg: PhaseControl, disables: list[str] | None) -> PhaseControl:
    if not disables:
        return cfg
    return PhaseControl(
        ENABLE_NIS          = "nis"        not in disables,
        ENABLE_RESIDUALS    = "residuals"  not in disables,
        ENABLE_CONDITION    = "condition"  not in disables,
        ENABLE_REPORTING    = "report"     not in disables,
        ENABLE_TENSOR_SAVE  = "tensor"     not in disables,
        ENABLE_FLAT_TABLE   = "flat"       not in disables,
    )


def main() -> None:
    args = _parse_args()
    init_runtime()
    setup_file_logger(log_filename="calibration.log")
    
    # Resolve PhaseControl configuration based on CLI arguments
    cfg = _apply_cli_disables(PhaseControl(), args.disable)
    
    # Load EKF registry through I/O layer
    registry_df = io.load_registry()
    if registry_df.empty:
        raise SystemExit("Global EKF diagnostic registry has zero rows.")
        
    df_valid = registry_df[registry_df["diag_file_path"].notna()].copy()
    if df_valid.empty:
        raise SystemExit("No flights in registry contain a valid 'diag_file_path'.")
        
    # Extract route_id and filter based on CLI selection
    if args.route:
        df_target = io.filter_by_route(df_valid, args.route)
    else:
        df_valid["route_id"] = df_valid["diag_file_path"].apply(io.resolve_route_id)
        df_target = df_valid[df_valid["route_id"] != "UNKNOWN"].copy()
        
    out_root = Path(args.out_dir) if args.out_dir else \
               DATA_DIR / "calibration" / "phase_quality" / "ekf_diagnostics"
               
    orchestration.run(
        registry_df   = df_target,
        out_root      = out_root,
        worker_count  = args.workers,
        cfg           = cfg,
        test_limit    = args.test_limit,
    )


if __name__ == "__main__":
    main()
