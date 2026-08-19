"""
Trace & Telemetry Correlator Devtool

Combines dynamic call trace logs (runtime_trace_*.log) and system telemetry CSVs
(telemetry_*.csv) into a normalized Star-Schema data model:
    1. call_sites.parquet / .csv (Static Dimension Table)
    2. invocations.parquet / .csv (Dynamic Fact Table / Multidimensional Tensor)

Handles multi-process PID merging, pycontrails depth-1 boundary filtering,
concurrency overlap detection (parallelism_flag), and sub-second telemetry interval slicing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd


def _parse_ts_to_seconds(ts_str: str) -> float:
    """Convert 'HH:MM:SS.mmm' to float seconds since midnight."""
    parts = ts_str.split(":")
    h = float(parts[0])
    m = float(parts[1])
    s = float(parts[2])
    return h * 3600.0 + m * 60.0 + s


def _normalize_module_path(filepath: str) -> str:
    """Normalize file path to repo-relative unix-style path."""
    if not filepath:
        return "<unknown>"
    norm = filepath.replace("\\", "/").strip()
    idx = norm.find("src/")
    if idx != -1:
        return norm[idx:]
    idx_pyc = norm.find("pycontrails/")
    if idx_pyc != -1:
        return norm[idx_pyc:]
    return norm


def _make_site_id(func: str, module: str, parent_func: str, parent_module: str) -> str:
    """Generate deterministic 8-char hex site_id."""
    key = f"{func}|{module}|{parent_func}|{parent_module}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"S_{h}"


@dataclass
class StackFrame:
    func: str
    module: str
    lineno: int
    start_ts: str
    start_t: float
    depth: int
    parent_func: str
    parent_module: str
    is_pycontrails_boundary: bool
    suppressed: bool
    child_duration_ms: float = 0.0


@dataclass
class CompletedSpan:
    func: str
    module: str
    parent_func: str
    parent_module: str
    pid: str
    depth: int
    start_ts: str
    end_ts: str
    start_t: float
    end_t: float
    duration_ms: float
    self_time_ms: float
    is_pycontrails_boundary: bool


def parse_trace_log(trace_log_path: Path) -> List[CompletedSpan]:
    """Parse dynamic trace log and reconstruct call spans per PID."""
    spans: List[CompletedSpan] = []
    stacks: Dict[str, List[StackFrame]] = {}

    # Matches: [TAG][HH:MM:SS.mmm] |   |-- CALL func_name(args) @ path:line
    call_pattern = re.compile(
        r"^\[(?P<tag>[^\]]+)\]\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\]\s+(?:[|`\-\s]+)\s+"
        r"CALL\s+(?P<func>\S+?)\((?P<args>.*?)\)\s+@\s+(?P<module>.*?):(?P<line>\d+)$"
    )
    # Matches: [TAG][HH:MM:SS.mmm] |   `-- RETURN func_name -> value
    return_pattern = re.compile(
        r"^\[(?P<tag>[^\]]+)\]\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\]\s+(?:[|`\-\s]+)\s+"
        r"RETURN\s+(?P<func>\S+?)\s+->\s+(?P<ret>.*)$"
    )

    with open(trace_log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("===") or line.startswith("[Saved") or line.startswith("[Merged"):
                continue

            call_match = call_pattern.match(line)
            if call_match:
                pid_str = call_match.group("tag")
                ts_str = call_match.group("ts")
                func_name = call_match.group("func")
                arg_str = call_match.group("args") or ""
                file_str = call_match.group("module") or ""
                lineno_str = call_match.group("line") or "0"
                event_type = "CALL"
            else:
                ret_match = return_pattern.match(line)
                if not ret_match:
                    continue
                pid_str = ret_match.group("tag")
                ts_str = ret_match.group("ts")
                func_name = ret_match.group("func")
                ret_str = ret_match.group("ret") or ""
                file_str = ""
                lineno_str = "0"
                event_type = "RETURN"

            t_sec = _parse_ts_to_seconds(ts_str)
            norm_module = _normalize_module_path(file_str) if file_str else ""

            if pid_str not in stacks:
                stacks[pid_str] = []

            stack = stacks[pid_str]

            if event_type == "CALL":
                lineno = int(lineno_str)
                parent_func = "<root>"
                parent_module = "<root>"

                # Determine parent and suppression state
                parent_is_pycontrails = False
                in_suppression = False

                if stack:
                    top = stack[-1]
                    parent_func = top.func
                    parent_module = top.module
                    parent_is_pycontrails = "pycontrails" in top.module or top.is_pycontrails_boundary
                    in_suppression = top.suppressed

                is_curr_pycontrails = "pycontrails" in norm_module
                is_boundary = is_curr_pycontrails and not parent_is_pycontrails

                # Suppress deeper pycontrails internal frames (depth > 1)
                should_suppress = in_suppression or (is_curr_pycontrails and not is_boundary)

                depth = len(stack) + 1
                frame = StackFrame(
                    func=func_name,
                    module=norm_module,
                    lineno=lineno,
                    start_ts=ts_str,
                    start_t=t_sec,
                    depth=depth,
                    parent_func=parent_func,
                    parent_module=parent_module,
                    is_pycontrails_boundary=is_boundary,
                    suppressed=should_suppress,
                )
                stack.append(frame)

            elif event_type == "RETURN":
                if not stack:
                    continue

                # Search stack from top down for matching frame
                pop_idx = len(stack) - 1
                while pop_idx >= 0 and stack[pop_idx].func != func_name:
                    pop_idx -= 1

                if pop_idx < 0:
                    # No matching frame found; pop top
                    pop_idx = len(stack) - 1

                frame = stack.pop(pop_idx)
                # Unwind any unclosed frames above pop_idx
                while len(stack) > pop_idx:
                    stack.pop()

                duration_ms = max(0.0, (t_sec - frame.start_t) * 1000.0)
                self_time_ms = max(0.0, duration_ms - frame.child_duration_ms)

                if stack:
                    # Accumulate child duration into new top frame
                    stack[-1].child_duration_ms += duration_ms

                if not frame.suppressed:
                    spans.append(
                        CompletedSpan(
                            func=frame.func,
                            module=frame.module,
                            parent_func=frame.parent_func,
                            parent_module=frame.parent_module,
                            pid=pid_str,
                            depth=frame.depth,
                            start_ts=frame.start_ts,
                            end_ts=ts_str,
                            start_t=frame.start_t,
                            end_t=t_sec,
                            duration_ms=round(duration_ms, 2),
                            self_time_ms=round(self_time_ms, 2),
                            is_pycontrails_boundary=frame.is_pycontrails_boundary,
                        )
                    )

    return spans


def mark_parallelism_flags(spans: List[CompletedSpan]) -> List[bool]:
    """
    Check if any other span from a DIFFERENT PID overlaps in [start_t, end_t].
    Returns list of boolean flags aligned with spans list.
    """
    n = len(spans)
    if n == 0:
        return []

    flags = [False] * n
    # Group spans by PID
    by_pid: Dict[str, List[Tuple[float, float, int]]] = {}
    for i, s in enumerate(spans):
        by_pid.setdefault(s.pid, []).append((s.start_t, s.end_t, i))

    if len(by_pid) <= 1:
        # Single process run — no multi-process concurrency
        return flags

    # For each span, check against spans in other PIDs
    for pid, span_list in by_pid.items():
        other_intervals = []
        for other_pid, other_list in by_pid.items():
            if other_pid != pid:
                other_intervals.extend(other_list)

        other_intervals.sort(key=lambda x: x[0])
        other_starts = np.array([x[0] for x in other_intervals])
        other_ends = np.array([x[1] for x in other_intervals])

        for start_t, end_t, idx in span_list:
            # An interval [s, e] overlaps with [os, oe] if os <= end_t and oe >= start_t
            # Find candidate intervals where other_start <= end_t
            cand_idx = np.searchsorted(other_starts, end_t, side="right")
            if cand_idx > 0:
                # Check if any candidate has other_end >= start_t
                if np.any(other_ends[:cand_idx] >= start_t):
                    flags[idx] = True

    return flags


def correlate_trace_and_telemetry(
    trace_log_path: Path,
    telemetry_csv_path: Path,
    out_dir: Path,
    output_format: str = "both",
    min_duration_ms: float = 0.0,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Main correlation entrypoint.
    Reads trace log and telemetry CSV, builds star schema tables, and saves to out_dir.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Parse Trace Log
    spans = parse_trace_log(trace_log_path)
    if min_duration_ms > 0:
        spans = [s for s in spans if s.duration_ms >= min_duration_ms]

    parallelism_flags = mark_parallelism_flags(spans)

    # 2. Load Telemetry CSV
    df_telem = pd.read_csv(telemetry_csv_path)
    t_values = df_telem["Time_s"].to_numpy()
    n_telem = len(t_values)

    # Pre-extract numpy arrays for fast vector slicing
    ram_arr = df_telem["RAM_MB"].to_numpy()
    uss_arr = df_telem["USS_MB"].to_numpy() if "USS_MB" in df_telem.columns else ram_arr
    commit_arr = df_telem["Commit_MB"].to_numpy()
    faults_arr = df_telem["Page_Faults_Delta"].to_numpy() if "Page_Faults_Delta" in df_telem.columns else np.zeros(n_telem)
    read_mbs_arr = df_telem["Read_MBs"].to_numpy() if "Read_MBs" in df_telem.columns else np.zeros(n_telem)
    write_mbs_arr = df_telem["Write_MBs"].to_numpy() if "Write_MBs" in df_telem.columns else np.zeros(n_telem)
    cpu_py_arr = df_telem["CPU_Pct_Python"].to_numpy() if "CPU_Pct_Python" in df_telem.columns else np.zeros(n_telem)
    cpu_sys_arr = df_telem["CPU_Pct_System"].to_numpy() if "CPU_Pct_System" in df_telem.columns else np.zeros(n_telem)
    cpu_core_arr = df_telem["CPU_Per_Core"].to_numpy() if "CPU_Per_Core" in df_telem.columns else np.array(["[]"] * n_telem)
    handles_arr = df_telem["Handle_Count"].to_numpy() if "Handle_Count" in df_telem.columns else np.zeros(n_telem)
    threads_arr = df_telem["Thread_Count"].to_numpy() if "Thread_Count" in df_telem.columns else np.zeros(n_telem)
    procs_arr = df_telem["Worker_Process_Count"].to_numpy() if "Worker_Process_Count" in df_telem.columns else np.ones(n_telem)
    avail_ram_arr = df_telem["Sys_Avail_RAM_MB"].to_numpy() if "Sys_Avail_RAM_MB" in df_telem.columns else np.zeros(n_telem)

    # Estimate average interval dt
    dt_default = float(np.median(np.diff(t_values))) if n_telem > 1 else 0.2

    # 3. Build Static Dimension Table (call_sites) and Dynamic Fact Table (invocations)
    call_sites_map: Dict[str, Dict[str, str]] = {}
    inv_idx_counters: Dict[str, int] = {}
    invocations_rows: List[Dict[str, any]] = []

    for i, span in enumerate(spans):
        site_id = _make_site_id(span.func, span.module, span.parent_func, span.parent_module)

        if site_id not in call_sites_map:
            call_sites_map[site_id] = {
                "site_id": site_id,
                "func": span.func,
                "module": span.module,
                "parent_func": span.parent_func,
                "parent_module": span.parent_module,
            }

        inv_idx = inv_idx_counters.get(site_id, 0)
        inv_idx_counters[site_id] = inv_idx + 1

        # Slicing telemetry interval [span.start_t, span.end_t]
        i_start = int(np.searchsorted(t_values, span.start_t, side="left"))
        i_end = int(np.searchsorted(t_values, span.end_t, side="right"))

        if i_end > i_start and i_start < n_telem:
            # True interval samples available
            i_end_clamp = min(i_end, n_telem)
            r_slice = ram_arr[i_start:i_end_clamp]
            u_slice = uss_arr[i_start:i_end_clamp]
            c_slice = commit_arr[i_start:i_end_clamp]
            f_slice = faults_arr[i_start:i_end_clamp]
            rm_slice = read_mbs_arr[i_start:i_end_clamp]
            wm_slice = write_mbs_arr[i_start:i_end_clamp]
            cpy_slice = cpu_py_arr[i_start:i_end_clamp]
            csy_slice = cpu_sys_arr[i_start:i_end_clamp]
            h_slice = handles_arr[i_start:i_end_clamp]
            th_slice = threads_arr[i_start:i_end_clamp]
            pr_slice = procs_arr[i_start:i_end_clamp]
            ar_slice = avail_ram_arr[i_start:i_end_clamp]

            ram_start = float(r_slice[0])
            ram_peak = float(np.max(r_slice))
            ram_end = float(r_slice[-1])
            ram_delta = ram_end - ram_start

            uss_start = float(u_slice[0])
            uss_peak = float(np.max(u_slice))

            commit_start = float(c_slice[0])
            commit_peak = float(np.max(c_slice))
            commit_delta = float(c_slice[-1]) - commit_start

            swap_pressure = max(0.0, (commit_peak - ram_peak) / max(1.0, ram_peak))
            sys_avail_ram = float(ar_slice[len(ar_slice) // 2])
            page_faults_delta = int(np.sum(f_slice))

            peak_read_mbs = float(np.max(rm_slice))
            peak_write_mbs = float(np.max(wm_slice))
            cum_read_mb = float(np.sum(rm_slice) * dt_default)
            cum_write_mb = float(np.sum(wm_slice) * dt_default)

            cpu_py_mean = float(np.mean(cpy_slice))
            cpu_py_peak = float(np.max(cpy_slice))
            cpu_sys_mean = float(np.mean(csy_slice))

            # Per-core peak calculation
            core_peaks = []
            for core_json in cpu_core_arr[i_start:i_end_clamp]:
                try:
                    c_list = json.loads(core_json)
                    if not core_peaks:
                        core_peaks = c_list
                    else:
                        core_peaks = [max(a, b) for a, b in zip(core_peaks, c_list)]
                except Exception:
                    pass
            cpu_per_core_peak = json.dumps(core_peaks)

            handle_start = int(h_slice[0])
            handle_end = int(h_slice[-1])
            handle_delta = handle_end - handle_start
            thread_mean = float(np.mean(th_slice))
            worker_proc_mean = float(np.mean(pr_slice))
            telemetry_interpolated = False
        else:
            # Sub-interval short span: interpolate from nearest sample
            idx_near = min(max(0, i_start), n_telem - 1) if n_telem > 0 else 0
            ram_val = float(ram_arr[idx_near]) if n_telem > 0 else 0.0
            uss_val = float(uss_arr[idx_near]) if n_telem > 0 else 0.0
            com_val = float(commit_arr[idx_near]) if n_telem > 0 else 0.0

            ram_start = ram_end = ram_peak = ram_val
            ram_delta = 0.0
            uss_start = uss_peak = uss_val
            commit_start = commit_peak = com_val
            commit_delta = 0.0
            swap_pressure = max(0.0, (com_val - ram_val) / max(1.0, ram_val))
            sys_avail_ram = float(avail_ram_arr[idx_near]) if n_telem > 0 else 0.0
            page_faults_delta = 0

            peak_read_mbs = float(read_mbs_arr[idx_near]) if n_telem > 0 else 0.0
            peak_write_mbs = float(write_mbs_arr[idx_near]) if n_telem > 0 else 0.0
            cum_read_mb = 0.0
            cum_write_mb = 0.0

            cpu_py_mean = cpu_py_peak = float(cpu_py_arr[idx_near]) if n_telem > 0 else 0.0
            cpu_sys_mean = float(cpu_sys_arr[idx_near]) if n_telem > 0 else 0.0
            cpu_per_core_peak = str(cpu_core_arr[idx_near]) if n_telem > 0 else "[]"

            handle_start = handle_end = int(handles_arr[idx_near]) if n_telem > 0 else 0
            handle_delta = 0
            thread_mean = float(threads_arr[idx_near]) if n_telem > 0 else 0.0
            worker_proc_mean = float(procs_arr[idx_near]) if n_telem > 0 else 1.0
            telemetry_interpolated = True

        invocations_rows.append(
            {
                "site_id": site_id,
                "inv_idx": inv_idx,
                "pid": span.pid,
                "depth": span.depth,
                "start_ts": span.start_ts,
                "end_ts": span.end_ts,
                "start_t": span.start_t,
                "end_t": span.end_t,
                "duration_ms": span.duration_ms,
                "self_time_ms": span.self_time_ms,
                "pycontrails_boundary": span.is_pycontrails_boundary,
                "parallelism_flag": parallelism_flags[i],
                "telemetry_interpolated": telemetry_interpolated,
                "ram_start_mb": round(ram_start, 2),
                "ram_peak_mb": round(ram_peak, 2),
                "ram_end_mb": round(ram_end, 2),
                "ram_delta_mb": round(ram_delta, 2),
                "uss_start_mb": round(uss_start, 2),
                "uss_peak_mb": round(uss_peak, 2),
                "commit_start_mb": round(commit_start, 2),
                "commit_peak_mb": round(commit_peak, 2),
                "commit_delta_mb": round(commit_delta, 2),
                "swap_pressure": round(swap_pressure, 4),
                "sys_avail_ram_mb": round(sys_avail_ram, 2),
                "page_faults_delta": page_faults_delta,
                "peak_read_mbs": round(peak_read_mbs, 3),
                "peak_write_mbs": round(peak_write_mbs, 3),
                "cum_read_mb": round(cum_read_mb, 3),
                "cum_write_mb": round(cum_write_mb, 3),
                "cpu_pct_python_mean": round(cpu_py_mean, 1),
                "cpu_pct_python_peak": round(cpu_py_peak, 1),
                "cpu_pct_system_mean": round(cpu_sys_mean, 1),
                "cpu_per_core_peak": cpu_per_core_peak,
                "handle_count_start": handle_start,
                "handle_count_end": handle_end,
                "handle_count_delta": handle_delta,
                "thread_count_mean": round(thread_mean, 1),
                "worker_process_count_mean": round(worker_proc_mean, 1),
            }
        )

    df_sites = pd.DataFrame(list(call_sites_map.values()))
    df_invocations = pd.DataFrame(invocations_rows)

    # 4. Save to Parquet and/or CSV
    if output_format in ("parquet", "both"):
        df_sites.to_parquet(out_dir / "call_sites.parquet", index=False)
        df_invocations.to_parquet(out_dir / "invocations.parquet", index=False)

    if output_format in ("csv", "both"):
        df_sites.to_csv(out_dir / "call_sites.csv", index=False)
        df_invocations.to_csv(out_dir / "invocations.csv", index=False)

    print(f"[Correlator] Extracted {len(df_sites)} unique call sites and {len(df_invocations)} invocation instances.")
    print(f"[Correlator] Saved tables to {out_dir}")

    return df_sites, df_invocations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.devtools.tracing.trace_correlator",
        description="Correlate dynamic trace log and telemetry CSV into Star-Schema tables.",
    )
    parser.add_argument(
        "--trace-log",
        type=str,
        required=True,
        help="Path to runtime_trace_*.log file.",
    )
    parser.add_argument(
        "--telemetry-csv",
        type=str,
        required=True,
        help="Path to telemetry_*.csv file.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="data/traces/correlated",
        help="Output directory for generated parquet/csv tables.",
    )
    parser.add_argument(
        "--format",
        choices=["parquet", "csv", "both"],
        default="both",
        help="Output serialization format (default 'both').",
    )
    parser.add_argument(
        "--min-duration-ms",
        type=float,
        default=0.0,
        help="Minimum span duration in ms to retain (default 0.0 = all).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    correlate_trace_and_telemetry(
        trace_log_path=Path(args.trace_log),
        telemetry_csv_path=Path(args.telemetry_csv),
        out_dir=Path(args.out_dir),
        output_format=args.format,
        min_duration_ms=args.min_duration_ms,
    )


if __name__ == "__main__":
    main()
