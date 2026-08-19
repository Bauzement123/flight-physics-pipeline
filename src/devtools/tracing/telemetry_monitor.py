"""
Telemetry Monitor Devtool

Samples Python process resources (RAM, USS, Commit, CPU, Page Faults, I/O rates,
Handle/Thread counts) using psutil at configurable sub-second intervals and logs
pre-computed rates to a CSV file.

Can be run standalone or spawned automatically by harness.py.

Usage:
    python -m src.devtools.tracing.telemetry_monitor --session-id 20260819_150000 --interval-ms 200
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _time_to_seconds(dt: datetime) -> float:
    """Convert datetime to float seconds since midnight."""
    return dt.hour * 3600.0 + dt.minute * 60.0 + dt.second + dt.microsecond / 1_000_000.0


class TelemetryMonitor:
    """Monitors running Python processes and writes resource telemetry to CSV."""

    CSV_HEADERS = [
        "Time_ms",
        "Time_s",
        "Loop_ms",
        "RAM_MB",
        "USS_MB",
        "Commit_MB",
        "CPU_Pct_Python",
        "CPU_Pct_System",
        "CPU_Per_Core",
        "Page_Faults_Delta",
        "Handle_Count",
        "Thread_Count",
        "Worker_Process_Count",
        "Read_MBs",
        "Write_MBs",
        "Read_IOPS",
        "Write_IOPS",
        "Sys_Avail_RAM_MB",
    ]

    def __init__(
        self,
        output_csv: Path,
        interval_ms: int = 200,
        target_pid: Optional[int] = None,
    ) -> None:
        self.output_csv = Path(output_csv)
        self.interval_sec = max(0.05, interval_ms / 1000.0)
        self.target_pid = target_pid
        self.running = True

        # State tracking for rate diffing
        self._prev_time: Optional[float] = None
        self._prev_read_bytes: float = 0.0
        self._prev_write_bytes: float = 0.0
        self._prev_read_ops: float = 0.0
        self._prev_write_ops: float = 0.0
        self._prev_page_faults: int = 0
        self._initialized_diff = False

    def _get_target_processes(self) -> List[any]:
        """Return list of psutil.Process objects to monitor."""
        import psutil

        procs = []
        my_pid = os.getpid()

        if self.target_pid is not None:
            try:
                parent = psutil.Process(self.target_pid)
                procs.append(parent)
                procs.extend(parent.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        else:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if p.pid != my_pid and "python" in p.info["name"].lower():
                        procs.append(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        return procs

    def sample_tick(self) -> Optional[Dict[str, any]]:
        """Collect one sample tick across tracked Python processes."""
        import psutil

        t_start = time.perf_counter()
        now = datetime.now()
        now_s = _time_to_seconds(now)
        time_ms_str = now.strftime("%H:%M:%S.%f")[:-3]

        procs = self._get_target_processes()
        if not procs and self.target_pid is not None:
            # Target process and its children have finished
            return None

        total_rss = 0
        total_uss = 0
        total_vms = 0
        total_cpu_py = 0.0
        total_handles = 0
        total_threads = 0
        total_read_bytes = 0
        total_write_bytes = 0
        total_read_ops = 0
        total_write_ops = 0
        total_page_faults = 0

        for p in procs:
            try:
                with p.oneshot():
                    mem = p.memory_info()
                    total_rss += getattr(mem, "rss", 0)
                    total_vms += getattr(mem, "vms", 0)
                    total_page_faults += getattr(mem, "num_page_faults", 0)

                    try:
                        full_mem = p.memory_full_info()
                        total_uss += getattr(full_mem, "uss", mem.rss)
                    except Exception:
                        total_uss += mem.rss

                    total_cpu_py += p.cpu_percent(interval=None)
                    total_threads += p.num_threads()

                    if sys.platform == "win32":
                        try:
                            total_handles += p.num_handles()
                        except Exception:
                            pass

                    try:
                        io = p.io_counters()
                        total_read_bytes += io.read_bytes
                        total_write_bytes += io.write_bytes
                        total_read_ops += io.read_count
                        total_write_ops += io.write_count
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # System-level metrics
        sys_cpu = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        sys_avail_ram = psutil.virtual_memory().available / (1024.0 * 1024.0)

        # Diffing for rates
        cur_perf = time.perf_counter()
        dt_rate = (cur_perf - self._prev_time) if self._prev_time is not None else self.interval_sec
        if dt_rate <= 0:
            dt_rate = self.interval_sec

        if not self._initialized_diff:
            read_mbs = 0.0
            write_mbs = 0.0
            read_iops = 0.0
            write_iops = 0.0
            faults_delta = 0
            self._initialized_diff = True
        else:
            read_mbs = max(0.0, (total_read_bytes - self._prev_read_bytes) / (1024.0 * 1024.0 * dt_rate))
            write_mbs = max(0.0, (total_write_bytes - self._prev_write_bytes) / (1024.0 * 1024.0 * dt_rate))
            read_iops = max(0.0, (total_read_ops - self._prev_read_ops) / dt_rate)
            write_iops = max(0.0, (total_write_ops - self._prev_write_ops) / dt_rate)
            faults_delta = max(0, total_page_faults - self._prev_page_faults)

        self._prev_time = cur_perf
        self._prev_read_bytes = total_read_bytes
        self._prev_write_bytes = total_write_bytes
        self._prev_read_ops = total_read_ops
        self._prev_write_ops = total_write_ops
        self._prev_page_faults = total_page_faults

        loop_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

        return {
            "Time_ms": time_ms_str,
            "Time_s": round(now_s, 4),
            "Loop_ms": loop_ms,
            "RAM_MB": round(total_rss / (1024.0 * 1024.0), 2),
            "USS_MB": round(total_uss / (1024.0 * 1024.0), 2),
            "Commit_MB": round(total_vms / (1024.0 * 1024.0), 2),
            "CPU_Pct_Python": round(total_cpu_py, 1),
            "CPU_Pct_System": round(sys_cpu, 1),
            "CPU_Per_Core": json.dumps([round(c, 1) for c in per_core]),
            "Page_Faults_Delta": faults_delta,
            "Handle_Count": total_handles,
            "Thread_Count": total_threads,
            "Worker_Process_Count": len(procs),
            "Read_MBs": round(read_mbs, 3),
            "Write_MBs": round(write_mbs, 3),
            "Read_IOPS": round(read_iops, 1),
            "Write_IOPS": round(write_iops, 1),
            "Sys_Avail_RAM_MB": round(sys_avail_ram, 1),
        }

    def run(self) -> None:
        """Main monitoring loop."""
        import psutil

        # Prime CPU percent measurement
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        for p in self._get_target_processes():
            try:
                p.cpu_percent(interval=None)
            except Exception:
                pass

        self.output_csv.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not self.output_csv.exists() or self.output_csv.stat().st_size == 0

        with open(self.output_csv, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.CSV_HEADERS)
            if is_new_file:
                writer.writeheader()
                f.flush()

            while self.running:
                t0 = time.perf_counter()
                sample = self.sample_tick()
                if sample is not None:
                    writer.writerow(sample)
                    f.flush()
                elif self.target_pid is not None:
                    # Target exited
                    break

                elapsed = time.perf_counter() - t0
                sleep_time = max(0.01, self.interval_sec - elapsed)
                time.sleep(sleep_time)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.devtools.tracing.telemetry_monitor",
        description="Monitor Python processes memory, CPU, page faults, and I/O rates.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Session identifier for telemetry output naming.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="data/traces",
        help="Output directory for telemetry CSV (default data/traces).",
    )
    parser.add_argument(
        "--csv-file",
        type=str,
        default=None,
        help="Explicit output CSV file path (overrides --out-dir and --session-id).",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=200,
        help="Polling interval in milliseconds (default 200).",
    )
    parser.add_argument(
        "--target-pid",
        type=int,
        default=None,
        help="Target parent process PID to monitor (including its children).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.csv_file:
        csv_path = Path(args.csv_file)
    else:
        sid = args.session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = Path(args.out_dir) / f"telemetry_{sid}.csv"

    monitor = TelemetryMonitor(
        output_csv=csv_path,
        interval_ms=args.interval_ms,
        target_pid=args.target_pid,
    )

    def _sig_handler(signum, frame):
        monitor.running = False

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        monitor.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
