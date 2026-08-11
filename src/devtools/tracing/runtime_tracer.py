"""
Runtime Dynamic Tracer Devtool

Uses sys.settrace to record real-time function calls scoped strictly to
repository-owned code under src/. External libraries (pandas, numpy, pycontrails, etc.)
are not traced.

Features:
  - Each start_tracing() call produces a unique timestamped log file in data/traces/.
  - ProcessPoolExecutor worker processes are automatically instrumented via an
    in-place patch of ProcessPoolExecutor.__init__ on the class object itself.
    This works regardless of import order and requires zero changes to pipeline code.
  - Worker traces are written to per-PID fragment files and merged back into the
    main log on stop_tracing(), sorted by timestamp.
  - stop_tracing() restores the original ProcessPoolExecutor.__init__ fully.

Usage:
    from src.devtools.runtime_tracer import start_tracing, stop_tracing

    start_tracing()               # auto log to data/traces/runtime_trace_YYYYMMDD_HHMMSS.log
    start_tracing("my/path.log")  # explicit path
    # ... run target code ...
    stop_tracing()
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Module-level worker initializer (must be at module scope to be picklable)
# ---------------------------------------------------------------------------

def _worker_trace_init(session_id: str, log_dir_str: str) -> None:
    """
    Injected as the ProcessPoolExecutor initializer= in each spawned worker.
    Creates a per-PID fragment file and attaches sys.settrace.
    This function must remain at module level to be picklable by spawn.
    """
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    pid = os.getpid()
    frag_path = Path(log_dir_str) / f"runtime_trace_{session_id}_worker_{pid}.tmp"
    frag_path.parent.mkdir(parents=True, exist_ok=True)
    frag_handle = open(frag_path, "a", encoding="utf-8", buffering=1)  # noqa: WPS515
    depth = [0]  # list for closure mutability

    def _worker_callback(frame, event, arg):
        code = frame.f_code
        filename = os.path.normpath(code.co_filename)
        if "src" not in filename or "site-packages" in filename:
            return _worker_callback

        if event == "call":
            depth[0] += 1
            indent = "|   " * (depth[0] - 1) + "|-- "
            arg_parts = []
            for var in code.co_varnames[: code.co_argcount]:
                val = frame.f_locals.get(var, "<unbound>")
                s = repr(val)
                if len(s) > 40:
                    s = s[:37] + "..."
                arg_parts.append(f"{var}={s}")
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = (
                f"[PID={pid}][{ts}] {indent}"
                f"CALL {code.co_name}({', '.join(arg_parts)})"
                f" @ {os.path.relpath(filename)}:{frame.f_lineno}\n"
            )
            frag_handle.write(line)
            frag_handle.flush()

        elif event == "return":
            indent = "|   " * (depth[0] - 1) + "`-- "
            ret = repr(arg)
            if len(ret) > 40:
                ret = ret[:37] + "..."
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = (
                f"[PID={pid}][{ts}] {indent}"
                f"RETURN {code.co_name} -> {ret}\n"
            )
            frag_handle.write(line)
            frag_handle.flush()
            depth[0] = max(0, depth[0] - 1)

        return _worker_callback

    sys.settrace(_worker_callback)


def _chained_worker_trace_init(session_id: str, log_dir_str: str, orig_fn, orig_args) -> None:
    """Chains trace init with a user-supplied initializer at top-level for spawn picklability."""
    _worker_trace_init(session_id, log_dir_str)
    if orig_fn:
        orig_fn(*orig_args)


# ---------------------------------------------------------------------------
# Main tracer class
# ---------------------------------------------------------------------------

class RuntimeTracer:
    """Manages a single tracing session for the main process and any worker processes."""

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._log_file: Optional[Path] = None
        self._log_handle = None
        self._original_ppe_init = None
        self._log_dir = Path("data/traces")
        self._depth: int = 0
        self._start_time: float = 0.0
        self._record_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, log_file: Optional[str] = None) -> None:
        """Begin a tracing session. Patches ProcessPoolExecutor and attaches sys.settrace."""
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._log_dir.mkdir(parents=True, exist_ok=True)

        if log_file:
            self._log_file = Path(log_file)
        else:
            self._log_file = self._log_dir / f"runtime_trace_{self._session_id}.log"

        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(self._log_file, "w", encoding="utf-8")  # noqa: WPS515
        self._depth = 0
        self._record_count = 0
        self._start_time = time.time()

        self._patch_process_pool_executor()
        sys.settrace(self._main_callback)

        self._emit(f"=== Runtime Tracer Started | Session: {self._session_id} | Log: {self._log_file} ===")

    def stop(self) -> None:
        """Stop tracing, restore ProcessPoolExecutor.__init__, merge worker fragments."""
        sys.settrace(None)
        self._restore_process_pool_executor()

        elapsed = time.time() - self._start_time
        footer = (
            f"=== Runtime Tracer Stopped | "
            f"Elapsed: {elapsed:.2f}s | "
            f"Main-process calls: {self._record_count} ==="
        )
        if self._log_handle:
            self._log_handle.write(footer + "\n")
            self._log_handle.flush()
            self._log_handle.close()
            self._log_handle = None
        self._safe_print(footer)

        self._merge_worker_fragments()
        self._safe_print(f"[Saved trace log to {self._log_file}]")

    # ------------------------------------------------------------------
    # Main-process trace callback
    # ------------------------------------------------------------------

    def _main_callback(self, frame, event, arg):
        code = frame.f_code
        filename = os.path.normpath(code.co_filename)

        # Scope filter: only repository code, exclude this devtool itself
        if "src" not in filename or "site-packages" in filename or "devtools" in filename:
            return self._main_callback

        if event == "call":
            self._depth += 1
            indent = "|   " * (self._depth - 1) + "|-- "
            arg_parts = []
            for var in code.co_varnames[: code.co_argcount]:
                val = frame.f_locals.get(var, "<unbound>")
                s = repr(val)
                if len(s) > 40:
                    s = s[:37] + "..."
                arg_parts.append(f"{var}={s}")
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = (
                f"[MAIN][{ts}] {indent}"
                f"CALL {code.co_name}({', '.join(arg_parts)})"
                f" @ {os.path.relpath(filename)}:{frame.f_lineno}"
            )
            self._emit(line)

        elif event == "return":
            indent = "|   " * (self._depth - 1) + "`-- "
            ret = repr(arg)
            if len(ret) > 40:
                ret = ret[:37] + "..."
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = f"[MAIN][{ts}] {indent}RETURN {code.co_name} -> {ret}"
            self._emit(line)
            self._depth = max(0, self._depth - 1)

        return self._main_callback

    # ------------------------------------------------------------------
    # ProcessPoolExecutor patching
    # ------------------------------------------------------------------

    def _patch_process_pool_executor(self) -> None:
        """
        Patch ProcessPoolExecutor.__init__ IN PLACE on the class object.

        Replacing the module attribute (concurrent.futures.ProcessPoolExecutor = X)
        only changes one pointer and misses modules that already did
        `from concurrent.futures import ProcessPoolExecutor` before start_tracing().

        Mutating __init__ on the class object itself affects ALL references to the
        class everywhere, including already-imported local bindings in src/ modules.
        Works with both `python -c` and `python -m` invocation styles.
        """
        original_init = concurrent.futures.ProcessPoolExecutor.__init__
        self._original_ppe_init = original_init

        session_id = self._session_id
        log_dir_str = str(self._log_dir)

        def patched_init(
            executor_self,
            max_workers=None,
            mp_context=None,
            initializer=None,
            initargs=(),
            **kwargs,
        ):
            from functools import partial

            if initializer is not None:
                # Chain: run existing initializer after tracer init
                try:
                    import pickle
                    pickle.dumps(initializer)  # test picklability
                    new_init = partial(_chained_worker_trace_init, session_id, log_dir_str, initializer, initargs)
                    new_args = ()
                except Exception:
                    # Original initializer not picklable; use ours only
                    new_init = partial(_worker_trace_init, session_id, log_dir_str)
                    new_args = ()
            else:
                new_init = partial(_worker_trace_init, session_id, log_dir_str)
                new_args = ()

            original_init(
                executor_self,
                max_workers=max_workers,
                mp_context=mp_context,
                initializer=new_init,
                initargs=new_args,
                **kwargs,
            )

        concurrent.futures.ProcessPoolExecutor.__init__ = patched_init  # type: ignore[method-assign]

    def _restore_process_pool_executor(self) -> None:
        if self._original_ppe_init is not None:
            concurrent.futures.ProcessPoolExecutor.__init__ = self._original_ppe_init  # type: ignore[method-assign]
            self._original_ppe_init = None

    # ------------------------------------------------------------------
    # Worker fragment merge
    # ------------------------------------------------------------------

    def _merge_worker_fragments(self) -> None:
        """Collect per-worker .tmp files, wait for them to stabilise, merge into main log."""
        if not self._session_id or not self._log_dir.exists():
            return

        pattern = f"runtime_trace_{self._session_id}_worker_*.tmp"
        frags = list(self._log_dir.glob(pattern))
        if not frags:
            return

        # Grace period: poll until all fragment files stop growing (max 2 s)
        max_wait = 2.0
        poll_interval = 0.1
        waited = 0.0
        prev_sizes: dict = {}
        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval
            sizes = {f: f.stat().st_size for f in frags if f.exists()}
            if sizes == prev_sizes:
                break
            prev_sizes = sizes

        # Collect all lines from main log and fragments
        all_lines: list = []
        if self._log_file and self._log_file.exists():
            all_lines.extend(self._log_file.read_text(encoding="utf-8").splitlines(keepends=True))

        for frag in frags:
            if not frag.exists():
                continue
            content = frag.read_text(encoding="utf-8")
            if not content.strip():
                all_lines.append(f"[INCOMPLETE — worker {frag.stem} wrote nothing]\n")
            else:
                all_lines.extend(content.splitlines(keepends=True))
            frag.unlink()

        # Sort body lines by embedded timestamp [HH:MM:SS.mmm]; keep === headers at edges
        import re

        def _ts_key(line: str) -> str:
            m = re.search(r"\[(\d{2}:\d{2}:\d{2}\.\d{3})\]", line)
            return m.group(1) if m else ""

        headers = [l for l in all_lines if l.startswith("===")]
        body = [l for l in all_lines if not l.startswith("===")]
        body.sort(key=_ts_key)

        merged = (headers[:1] if headers else []) + body + (headers[1:] if len(headers) > 1 else [])

        if self._log_file:
            self._log_file.write_text("".join(merged), encoding="utf-8")

        self._safe_print(f"[Merged {len(frags)} worker fragment(s) into {self._log_file}]")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, line: str) -> None:
        self._record_count += 1
        if self._log_handle:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()
        self._safe_print(line)

    @staticmethod
    def _safe_print(text: str) -> None:
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode("ascii"))


# ---------------------------------------------------------------------------
# Module-level singleton and convenience API
# ---------------------------------------------------------------------------

_tracer = RuntimeTracer()


def start_tracing(log_file: Optional[str] = None) -> None:
    """Start a new tracing session. Each call creates a unique timestamped log file."""
    _tracer.start(log_file=log_file)


def stop_tracing() -> None:
    """Stop tracing, restore ProcessPoolExecutor, and merge worker fragment logs."""
    _tracer.stop()
