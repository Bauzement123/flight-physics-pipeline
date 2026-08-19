"""
Profiling Harness Devtool

End-to-end benchmarking orchestrator for the Flight Physics Pipeline.
Spawns background telemetry monitoring (psutil), attaches dynamic runtime tracing (sys.settrace),
executes any target module, and automatically triggers post-run correlation and analysis.

Usage:
    python -m src.devtools.tracing.harness \
        --module src.core.physics.cli \
        --args "--start-date 2025-01-01 --end-date 2025-01-01 --lower-rank 1 --upper-rank 3" \
        --out-dir data/traces/run_standard
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.devtools.tracing.harness",
        description="End-to-end benchmarking harness with telemetry monitor and dynamic tracing.",
    )
    parser.add_argument(
        "--module",
        type=str,
        required=True,
        help="Target Python module dotted path (e.g. 'src.core.physics.cli').",
    )
    parser.add_argument(
        "--args",
        type=str,
        default="",
        help="Command-line arguments string forwarded to the target module.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Output directory for trace logs, telemetry CSV, and star-schema tables (default 'data/traces/<session_id>').",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=200,
        help="Telemetry polling interval in milliseconds (default 200).",
    )
    parser.add_argument(
        "--format",
        choices=["parquet", "csv", "both"],
        default="both",
        help="Output format for correlated tables (default 'both').",
    )
    parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Skip automated performance analysis after correlation.",
    )
    parser.add_argument(
        "--compare",
        type=str,
        default=None,
        metavar="DIR",
        help="Optional second run output directory to generate a comparative performance diff.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path("data/traces") / f"run_{session_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    telemetry_csv = out_dir / f"telemetry_{session_id}.csv"
    trace_log = out_dir / f"runtime_trace_{session_id}.log"

    print("=" * 70)
    print(f"=== Starting Profiling Harness | Session: {session_id} ===")
    print(f"Target Module : {args.module}")
    print(f"Target Args   : {args.args}")
    print(f"Output Dir    : {out_dir}")
    print(f"Telemetry CSV : {telemetry_csv}")
    print(f"Trace Log     : {trace_log}")
    print("=" * 70)

    # 1. Spawn Telemetry Monitor
    monitor_cmd = [
        sys.executable,
        "-m",
        "src.devtools.tracing.telemetry_monitor",
        "--csv-file",
        str(telemetry_csv),
        "--interval-ms",
        str(args.interval_ms),
    ]
    monitor_proc = subprocess.Popen(monitor_cmd)
    time.sleep(0.3)  # Allow monitor to prime counters

    # 2. Parse target module arguments
    import shlex
    target_arg_list = shlex.split(args.args) if args.args else []

    # 3. Spawn Trace Probe with Target Module
    probe_cmd = [
        sys.executable,
        "-m",
        "src.devtools.tracing.trace_probe",
        "--trace-log",
        str(trace_log),
        args.module,
    ] + target_arg_list

    t0 = time.time()
    return_code = 0
    try:
        probe_proc = subprocess.Popen(probe_cmd)
        return_code = probe_proc.wait()
    except KeyboardInterrupt:
        print("\n[Harness] Interrupted by user. Terminating processes...")
        probe_proc.terminate()
        return_code = 130
    finally:
        # 4. Terminate Telemetry Monitor
        monitor_proc.terminate()
        try:
            monitor_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            monitor_proc.kill()

    elapsed = time.time() - t0
    print(f"\n[Harness] Execution completed in {elapsed:.2f}s (Exit code: {return_code})")

    # 5. Run Correlation
    if trace_log.exists() and telemetry_csv.exists():
        print("\n[Harness] Correlating Trace and Telemetry into Star Schema...")
        from src.devtools.tracing.trace_correlator import correlate_trace_and_telemetry

        correlate_trace_and_telemetry(
            trace_log_path=trace_log,
            telemetry_csv_path=telemetry_csv,
            out_dir=out_dir,
            output_format=args.format,
        )
    else:
        print("[Harness] Warning: Missing trace log or telemetry CSV. Skipping correlation.")

    # 6. Run Analysis
    if not args.skip_analyze and (out_dir / "invocations.parquet").exists():
        print("\n[Harness] Running Performance & Drift Analysis...")
        from src.devtools.tracing.trace_analyzer import analyze_performance

        report_path = out_dir / f"report_{out_dir.name}.md"
        analyze_performance(
            data_dir=out_dir,
            compare_dir=Path(args.compare) if args.compare else None,
            report_path=report_path,
        )
        print(f"[Harness] Analysis report saved to {report_path}")

    print(f"\n=== Profiling Harness Finished | Artifacts in {out_dir} ===")


if __name__ == "__main__":
    main()
