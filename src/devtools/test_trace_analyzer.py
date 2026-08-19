"""
Unit Tests for trace_analyzer.py
"""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.devtools.tracing.trace_analyzer import (
    analyze_distributions,
    compare_runs,
    detect_memory_drift,
    detect_thrashing_events,
    generate_markdown_report,
)


def _make_mock_run(tmp_dir: Path, is_low_mem: bool = False) -> Path:
    """Create a mock correlated run directory with call_sites and invocations."""
    run_dir = tmp_dir / ("run_lowmem" if is_low_mem else "run_standard")
    run_dir.mkdir(parents=True, exist_ok=True)

    df_sites = pd.DataFrame(
        [
            {"site_id": "S_01", "func": "load_cube", "module": "src/core/weather/era5_manager.py", "parent_func": "run_day", "parent_module": "src/core/physics/orchestrator.py"},
            {"site_id": "S_02", "func": "eval", "module": "src/core/physics/models/ps_cocip.py", "parent_func": "_run_batch", "parent_module": "src/core/physics/worker.py"},
        ]
    )

    inv_rows = []
    # 10 invocations of eval
    base_ram = 500.0 if not is_low_mem else 200.0
    drift_step = 20.0 if not is_low_mem else 0.0  # standard leaks memory, lowmem doesn't

    for i in range(10):
        # eval invocation
        r_start = base_ram + i * drift_step
        r_peak = r_start + (150.0 if not is_low_mem else 50.0)
        r_end = r_start + 10.0 if not is_low_mem else r_start
        faults = 600 if (not is_low_mem and i == 5) else 50

        inv_rows.append(
            {
                "site_id": "S_02",
                "inv_idx": i,
                "pid": "MAIN",
                "depth": 3,
                "start_ts": f"12:0{i}:00.000",
                "end_ts": f"12:0{i}:05.000",
                "start_t": 43200.0 + i * 60,
                "end_t": 43205.0 + i * 60,
                "duration_ms": 5000.0 if not is_low_mem else 6500.0,
                "self_time_ms": 100.0,
                "pycontrails_boundary": False,
                "parallelism_flag": False,
                "telemetry_interpolated": False,
                "ram_start_mb": r_start,
                "ram_peak_mb": r_peak,
                "ram_end_mb": r_end,
                "ram_delta_mb": r_end - r_start,
                "uss_start_mb": r_start * 0.9,
                "uss_peak_mb": r_peak * 0.9,
                "commit_start_mb": r_start * 1.2,
                "commit_peak_mb": r_peak * 1.2,
                "commit_delta_mb": (r_end - r_start) * 1.2,
                "swap_pressure": 0.05,
                "sys_avail_ram_mb": 7000.0,
                "page_faults_delta": faults,
                "peak_read_mbs": 5.0,
                "peak_write_mbs": 1.0,
                "cum_read_mb": 25.0,
                "cum_write_mb": 5.0,
                "cpu_pct_python_mean": 80.0,
                "cpu_pct_python_peak": 95.0,
                "cpu_pct_system_mean": 15.0,
                "cpu_per_core_peak": "[]",
                "handle_count_start": 50,
                "handle_count_end": 50,
                "handle_count_delta": 0,
                "thread_count_mean": 4.0,
                "worker_process_count_mean": 1.0,
            }
        )

    df_inv = pd.DataFrame(inv_rows)
    df_sites.to_parquet(run_dir / "call_sites.parquet", index=False)
    df_inv.to_parquet(run_dir / "invocations.parquet", index=False)
    return run_dir


def test_drift_and_thrashing_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        run_std = _make_mock_run(tmp, is_low_mem=False)

        df_sites = pd.read_parquet(run_std / "call_sites.parquet")
        df_inv = pd.read_parquet(run_std / "invocations.parquet")

        drifts = detect_memory_drift(df_sites, df_inv, min_invocations=3, drift_threshold_mb_per_inv=2.0)
        assert len(drifts) == 1
        assert drifts[0]["func"] == "eval"
        assert drifts[0]["net_drift_mb"] >= 180.0

        thrash = detect_thrashing_events(df_sites, df_inv, fault_threshold=500)
        assert len(thrash) == 1
        assert thrash[0]["inv_idx"] == 5
        assert thrash[0]["page_faults_delta"] == 600


def test_comparative_run_diff():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        run_std = _make_mock_run(tmp, is_low_mem=False)
        run_low = _make_mock_run(tmp, is_low_mem=True)

        df_diff = compare_runs(run_std, run_low)
        assert len(df_diff) == 1
        row = df_diff.iloc[0]
        assert row["func"] == "eval"
        assert row["diff_dur_pct"] > 0  # Low mem is slower
        assert row["diff_ram_mb"] < 0  # Low mem uses less RAM


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
