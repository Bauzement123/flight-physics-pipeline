"""
Trace Probe Devtool

Generic runtime injection shim using runpy. Runs any pipeline module under
runtime_tracer without requiring any code changes in production modules.

Usage:
    python -m src.devtools.tracing.trace_probe src.core.physics.cli --start-date 2025-01-01 ...
    python -m src.devtools.tracing.trace_probe --trace-log data/traces/my_run.log src.core.physics.cli ...
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import List, Optional

from src.devtools.tracing.runtime_tracer import start_tracing, stop_tracing


def main() -> None:
    raw_args = sys.argv[1:]
    if not raw_args:
        print("Usage: python -m src.devtools.tracing.trace_probe [--trace-log PATH] <target.module> [args...]")
        sys.exit(1)

    trace_log: Optional[str] = None
    target_idx = 0

    while target_idx < len(raw_args):
        arg = raw_args[target_idx]
        if arg == "--trace-log" and target_idx + 1 < len(raw_args):
            trace_log = raw_args[target_idx + 1]
            target_idx += 2
        elif arg.startswith("--trace-log="):
            trace_log = arg.split("=", 1)[1]
            target_idx += 1
        else:
            break

    if target_idx >= len(raw_args):
        print("Error: No target module specified.")
        sys.exit(1)

    target_module = raw_args[target_idx]
    target_args = raw_args[target_idx + 1 :]

    # Reconstruct sys.argv for the target module
    sys.argv = [target_module] + target_args

    start_tracing(log_file=trace_log)
    try:
        runpy.run_module(target_module, run_name="__main__", alter_sys=True)
    finally:
        stop_tracing()


if __name__ == "__main__":
    main()
