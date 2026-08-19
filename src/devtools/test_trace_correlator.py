"""
Unit Tests for trace_correlator.py
"""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.devtools.tracing.trace_correlator import (
    CompletedSpan,
    correlate_trace_and_telemetry,
    mark_parallelism_flags,
    parse_trace_log,
)


def test_trace_log_parsing_and_boundary():
    """Verify trace log parsing, stack unwinding, and pycontrails boundary filtering."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trace_file = tmp / "test_trace.log"

        trace_content = """=== Runtime Tracer Started ===
[MAIN][14:00:00.000] |-- CALL main() @ src/core/physics/cli.py:50
[MAIN][14:00:00.100] |   |-- CALL _run_batch() @ src/core/physics/worker.py:100
[MAIN][14:00:00.200] |   |   |-- CALL eval() @ site-packages/pycontrails/models/cocip.py:80
[MAIN][14:00:00.300] |   |   |   |-- CALL downselect() @ site-packages/pycontrails/core/met.py:40
[MAIN][14:00:00.400] |   |   |   `-- RETURN downselect -> None
[MAIN][14:00:00.500] |   |   `-- RETURN eval -> Flight
[MAIN][14:00:00.600] |   `-- RETURN _run_batch -> None
[MAIN][14:00:00.700] `-- RETURN main -> 0
=== Runtime Tracer Stopped ===
"""
        trace_file.write_text(trace_content, encoding="utf-8")

        spans = parse_trace_log(trace_file)
        func_names = [s.func for s in spans]

        # downselect should be SUPPRESSED by pycontrails depth-1 filter
        assert "downselect" not in func_names

        # eval should be present and marked as pycontrails boundary
        eval_span = next(s for s in spans if s.func == "eval")
        assert eval_span.is_pycontrails_boundary is True
        assert eval_span.parent_func == "_run_batch"
        assert np.isclose(eval_span.duration_ms, 300.0, atol=1.0)

        # _run_batch should have main as parent
        batch_span = next(s for s in spans if s.func == "_run_batch")
        assert batch_span.parent_func == "main"
        assert np.isclose(batch_span.duration_ms, 500.0, atol=1.0)


def test_correlate_trace_and_telemetry():
    """Verify star-schema generation and telemetry interval slicing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trace_file = tmp / "trace.log"
        telem_file = tmp / "telemetry.csv"
        out_dir = tmp / "correlated"

        trace_content = """[MAIN][12:00:00.000] |-- CALL run() @ src/module.py:10
[MAIN][12:00:00.500] |   |-- CALL compute() @ src/module.py:20
[MAIN][12:00:01.500] |   `-- RETURN compute -> ok
[MAIN][12:00:02.000] `-- RETURN run -> ok
"""
        trace_file.write_text(trace_content, encoding="utf-8")

        # Telemetry across 12:00:00 to 12:00:02 (43200s to 43202s)
        # Compute starts at 12:00:00.5 (43200.5) and ends at 12:00:01.5 (43201.5)
        telem_df = pd.DataFrame(
            {
                "Time_ms": ["12:00:00.000", "12:00:00.500", "12:00:01.000", "12:00:01.500", "12:00:02.000"],
                "Time_s": [43200.0, 43200.5, 43201.0, 43201.5, 43202.0],
                "Loop_ms": [2.0, 2.0, 2.0, 2.0, 2.0],
                "RAM_MB": [100.0, 150.0, 300.0, 250.0, 100.0],
                "USS_MB": [80.0, 120.0, 280.0, 220.0, 80.0],
                "Commit_MB": [200.0, 250.0, 400.0, 350.0, 200.0],
                "CPU_Pct_Python": [10.0, 50.0, 90.0, 80.0, 10.0],
                "CPU_Pct_System": [5.0, 20.0, 30.0, 25.0, 5.0],
                "CPU_Per_Core": ["[]", "[]", "[]", "[]", "[]"],
                "Page_Faults_Delta": [0, 100, 500, 200, 0],
                "Handle_Count": [10, 12, 15, 12, 10],
                "Thread_Count": [2, 4, 4, 4, 2],
                "Worker_Process_Count": [1, 1, 1, 1, 1],
                "Read_MBs": [0.0, 5.0, 10.0, 2.0, 0.0],
                "Write_MBs": [0.0, 0.0, 2.0, 1.0, 0.0],
                "Read_IOPS": [0, 50, 100, 20, 0],
                "Write_IOPS": [0, 0, 20, 10, 0],
                "Sys_Avail_RAM_MB": [8000.0, 7900.0, 7700.0, 7800.0, 8000.0],
            }
        )
        telem_df.to_csv(telem_file, index=False)

        df_sites, df_inv = correlate_trace_and_telemetry(
            trace_log_path=trace_file,
            telemetry_csv_path=telem_file,
            out_dir=out_dir,
            output_format="both",
        )

        assert len(df_sites) == 2  # run and compute
        assert len(df_inv) == 2

        compute_row = df_inv[df_inv["site_id"] == df_sites[df_sites["func"] == "compute"]["site_id"].iloc[0]].iloc[0]
        assert compute_row["duration_ms"] == 1000.0
        assert compute_row["ram_start_mb"] == 150.0
        assert compute_row["ram_peak_mb"] == 300.0
        assert compute_row["ram_delta_mb"] == 100.0  # 250 - 150
        assert compute_row["page_faults_delta"] == 800  # 100 + 500 + 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
