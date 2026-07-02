"""
CLI utility to manually trigger calibration plot generation.
"""

import argparse
import sys
from pathlib import Path

# Add project root to sys.path to allow running as script directly
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.calibration.plot_helpers import get_or_create_config_plot


def main():
    parser = argparse.ArgumentParser(description="Generate clustered cohort visualizations.")
    parser.add_argument("--route", required=True, help="Route ID (e.g. LOWW-EHAM)")
    parser.add_argument(
        "--config-type",
        choices=["oracle", "pareto"],
        default="oracle",
        help="Type of configuration (oracle or pareto)",
    )
    parser.add_argument("--n0", type=int, default=64, help="Initial sample size N_0 (for pareto)")
    parser.add_argument("--tau", type=float, default=0.10, help="Stability threshold tau (for pareto)")
    parser.add_argument("--kmax", type=int, default=4, help="Maximum clusters K_max (for pareto)")
    parser.add_argument("--replicate", type=int, default=0, help="Replicate seed (for pareto)")

    args = parser.parse_args()

    try:
        print(f"Generating {args.config_type.upper()} plot for route {args.route}...")
        path = get_or_create_config_plot(
            route_id=args.route,
            config_type=args.config_type,
            n0=args.n0,
            tau=args.tau,
            kmax=args.kmax,
            replicate=args.replicate,
        )
        print(f"Success! Saved to: {path}")
    except Exception as e:
        print(f"Error generating plot: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
