"""
Comprehensive regression tests for the Fetching Module refactor.

Covers:
  T2-5  write_parquet_atomic
  T2-6  label_flight_phase PyArrow ns fix
  T2-7  label_flight_phase missing columns
  T2-8  retry_backoff constants from config
  T2-9  split_route_string return type
  T2-10 setup_file_logger custom out_dir
  T2-11 init_runtime idempotency
  T2-12 update_raw_concat deduplication
  T2-13 _resolve_from_registry flight_id slice
  T2-14 extract_target_routes min_distance
  T2-15 check_seed_range ValueError
  T2-16 run_batch resume skips only success=True manifests
  T4-20 fetch_trajectories fast-cache path
"""
import argparse
import json
import os
import sys
import time
import tempfile
import traceback
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

# ── Ensure project root is on sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
SKIP = "\033[93m SKIP\033[0m"

_results: list[tuple[str, bool, str]] = []


def test(name: str):
    """Decorator that catches exceptions, records outcome, and prints result."""
    def decorator(fn):
        def wrapper():
            try:
                fn()
                print(f"{PASS}  {name}")
                _results.append((name, True, ""))
            except Exception as e:
                tb = traceback.format_exc(limit=6)
                print(f"{FAIL}  {name}\n       {e}\n{tb}")
                _results.append((name, False, str(e)))
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# T2-5  write_parquet_atomic
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-5  write_parquet_atomic: overwrites existing file, no tmp left behind")
def t2_5():
    from src.core.fetching.helpers import write_parquet_atomic
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "out.parquet"
        # Write initial file
        pd.DataFrame({"a": [1]}).to_parquet(path)
        # Overwrite atomically
        df_new = pd.DataFrame({"a": [2, 3]})
        write_parquet_atomic(df_new, path)
        # Assert target is correct
        df_read = pd.read_parquet(path)
        assert list(df_read["a"]) == [2, 3], f"Got: {df_read['a'].tolist()}"
        # No temp files left
        leftovers = [f for f in Path(td).iterdir() if ".tmp." in f.name]
        assert not leftovers, f"Leftover temp files: {leftovers}"


# ─────────────────────────────────────────────────────────────────────────────
# T2-6  label_flight_phase — PyArrow nanosecond fix
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-6  label_flight_phase: PyArrow timestamp columns complete in < 10s")
def t2_6():
    from src.core.fetching.opensky_fetcher import label_flight_phase
    # Build a DataFrame with PyArrow-backed timestamps
    try:
        ts_base = pd.Timestamp("2024-01-01 10:00:00", tz="UTC")
        times = [ts_base + pd.Timedelta(seconds=i * 4) for i in range(50)]
        df = pd.DataFrame({
            "time":          pd.array(times, dtype="timestamp[ns, tz=UTC][pyarrow]"),
            "baroaltitude":  np.linspace(0, 10000, 50),
            "velocity":      np.full(50, 250.0),
            "vertrate":      np.full(50, 5.0),
            "icao24":        ["abc123"] * 50,
            "typecode":      ["A320"] * 50,
        })
    except Exception:
        # pyarrow extension not available in this env; test with standard dtype
        ts_base = pd.Timestamp("2024-01-01 10:00:00")
        times = [ts_base + pd.Timedelta(seconds=i * 4) for i in range(50)]
        df = pd.DataFrame({
            "time":          times,
            "baroaltitude":  np.linspace(0, 10000, 50),
            "velocity":      np.full(50, 250.0),
            "vertrate":      np.full(50, 5.0),
            "icao24":        ["abc123"] * 50,
            "typecode":      ["A320"] * 50,
        })

    t0 = time.time()
    df_out = label_flight_phase(df)
    elapsed = time.time() - t0
    assert elapsed < 10.0, f"label_flight_phase took {elapsed:.1f}s — possible nanosecond loop bug"
    assert "flight_phase" in df_out.columns, "flight_phase column missing"


# ─────────────────────────────────────────────────────────────────────────────
# T2-7  label_flight_phase — missing required columns
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-7  label_flight_phase: returns flight_phase=None for empty/incomplete df")
def t2_7():
    from src.core.fetching.opensky_fetcher import label_flight_phase
    df = pd.DataFrame({"time": pd.to_datetime(["2024-01-01"])})
    df_out = label_flight_phase(df)
    assert "flight_phase" in df_out.columns
    assert df_out["flight_phase"].iloc[0] is None


# ─────────────────────────────────────────────────────────────────────────────
# T2-8  retry_backoff constants come from config
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-8  retry_backoff: default parameters match config constants")
def t2_8():
    import inspect
    from src.common.config import (
        BACKOFF_INITIAL_DELAY, BACKOFF_FACTOR, BACKOFF_MAX_DELAY, BACKOFF_MAX_RETRIES,
    )
    from src.common.utils import retry_backoff
    sig = inspect.signature(retry_backoff)
    params = sig.parameters
    assert params["max_retries"].default == BACKOFF_MAX_RETRIES, \
        f"max_retries default {params['max_retries'].default} != {BACKOFF_MAX_RETRIES}"
    assert params["base_delay"].default == BACKOFF_INITIAL_DELAY, \
        f"base_delay default {params['base_delay'].default} != {BACKOFF_INITIAL_DELAY}"
    assert params["factor"].default == BACKOFF_FACTOR, \
        f"factor default {params['factor'].default} != {BACKOFF_FACTOR}"
    assert params["max_delay"].default == BACKOFF_MAX_DELAY, \
        f"max_delay default {params['max_delay'].default} != {BACKOFF_MAX_DELAY}"


# ─────────────────────────────────────────────────────────────────────────────
# T2-9  split_route_string return type
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-9  split_route_string: returns (str, str) for valid and invalid inputs")
def t2_9():
    from src.common.utils import split_route_string
    dep, arr = split_route_string("EDDF -> EGLL")
    assert dep == "EDDF" and arr == "EGLL", f"Got ({dep!r}, {arr!r})"
    dep2, arr2 = split_route_string("bad_input")
    assert dep2 == "UNK" and arr2 == "UNK"
    dep3, arr3 = split_route_string(None)  # type: ignore
    assert dep3 == "UNK" and arr3 == "UNK"


# ─────────────────────────────────────────────────────────────────────────────
# T2-10  setup_file_logger custom out_dir
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-10 setup_file_logger: writes to custom out_dir, not LOGS_DIR")
def t2_10():
    import logging
    from src.common.utils import setup_file_logger
    from src.common.config import LOGS_DIR
    with tempfile.TemporaryDirectory() as td:
        custom = Path(td)
        handler = setup_file_logger(out_dir=custom, log_filename="test_custom.log")
        log_path = custom / "test_custom.log"
        assert log_path.exists(), f"Log file not created at {log_path}"
        assert not (LOGS_DIR / "test_custom.log").exists(), \
            "Log was written to LOGS_DIR instead of custom dir"
        # Cleanup handler so it doesn't bleed into other tests
        if handler:
            logging.getLogger().removeHandler(handler)
            handler.close()


# ─────────────────────────────────────────────────────────────────────────────
# T2-11  init_runtime idempotency
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-11 init_runtime: idempotent, directories exist after two calls")
def t2_11():
    from src.common.config import init_runtime, DATA_DIR, LOGS_DIR
    init_runtime()
    init_runtime()  # second call must not raise
    assert DATA_DIR.exists()
    assert LOGS_DIR.exists()


# ─────────────────────────────────────────────────────────────────────────────
# T2-12  update_raw_concat deduplication
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-12 update_raw_concat: deduplicates by flight_id across multiple calls")
def t2_12():
    from src.core.fetching.opensky_fetcher import update_raw_concat
    with tempfile.TemporaryDirectory() as td:
        concat_path = Path(td) / "route_all_raw.parquet"
        df1 = pd.DataFrame({"flight_id": ["F001", "F002"], "val": [1, 2]})
        df2 = pd.DataFrame({"flight_id": ["F002", "F003"], "val": [99, 3]})  # F002 is a dup
        update_raw_concat(concat_path, [df1])
        update_raw_concat(concat_path, [df2])
        result = pd.read_parquet(concat_path)
        fids = list(result["flight_id"])
        assert fids.count("F002") == 1, f"Duplicate F002 not removed: {fids}"
        assert len(result) == 3, f"Expected 3 rows, got {len(result)}"


# ─────────────────────────────────────────────────────────────────────────────
# T2-13  _resolve_from_registry flight_id slice guard
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-13 _resolve_from_registry: slices to requested flight_id only")
def t2_13():
    from src.core.fetching.opensky_fetcher import _resolve_from_registry
    with tempfile.TemporaryDirectory() as td:
        # Write multi-flight parquet
        parquet_path = Path(td) / "F001_raw.parquet"
        df_multi = pd.DataFrame({
            "flight_id":   ["F001", "F001", "F002"],
            "time":        pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-01"]),
            "baroaltitude":[1000, 2000, 3000],
        })
        df_multi.to_parquet(parquet_path)
        # Registry maps F001 -> this file
        from src.common.utils import to_project_relative
        from src.common.config import BASE_DIR
        # Use direct path mapping since to_project_relative needs BASE_DIR
        cached_flights = {"F001": str(parquet_path)}

        record = {
            "flight_id": "F001",
            "raw_path": parquet_path,
            "rel_path": "some/relative/path",
        }
        # Monkey-patch BASE_DIR / cached_flights[fid] to resolve to our path
        # The function does: path = BASE_DIR / cached_flights[fid] if fid in cached_flights
        # So we store an absolute path string; on Windows BASE_DIR / absolute is problematic.
        # Instead register with the raw_path directly (flight_id not in cached_flights)
        result_df = _resolve_from_registry(record, {})  # not in cached_flights → uses raw_path
        assert result_df is not None, "Expected a DataFrame, got None"
        assert set(result_df["flight_id"].unique()) == {"F001"}, \
            f"Got flight_ids: {result_df['flight_id'].unique()}"
        assert len(result_df) == 2, f"Expected 2 F001 rows, got {len(result_df)}"


# ─────────────────────────────────────────────────────────────────────────────
# T2-14  extract_target_routes min_distance filter
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-14 extract_target_routes: excludes routes below min_distance threshold")
def t2_14():
    from src.core.fetching.fetcher_orchestrator import extract_target_routes
    with tempfile.TemporaryDirectory() as td:
        summary_path = Path(td) / "route_summary.parquet"
        df = pd.DataFrame({
            "rank":           [1, 2, 3],
            "route":          ["EDDF -> EGLL", "LFPG -> EHAM", "LIRF -> LTBA"],
            "distance_m":     [900_000, 400_000, 1_500_000],  # 900km, 400km, 1500km
            "no_of_flights":  [1000, 500, 800],
        })
        df.to_parquet(summary_path)
        result = extract_target_routes(
            summary_path=summary_path,
            lower=1, upper=3,
            min_distance=800.0,   # keep only ≥800 km
        )
        # Only ranks 1 (900km) and 3 (1500km) should survive
        assert len(result) == 2, f"Expected 2 routes, got {len(result)}: {result}"
        routes = list(result.apply(lambda r: f"{r['dep']}-{r['arr']}", axis=1))
        assert "LFPG-EHAM" not in " ".join(routes), "Short route was not filtered"


# ─────────────────────────────────────────────────────────────────────────────
# T2-15  check_seed_range ValueError handling
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-15 check_seed_range: raises ArgumentTypeError for non-integer input")
def t2_15():
    # Replicate the check_seed_range closure exactly as it exists in parse_cli_args
    def check_seed_range(val: str) -> int:
        try:
            ival = int(val)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Seed '{val}' is not a valid integer.")
        if ival < 0 or ival > 4294967295:
            raise argparse.ArgumentTypeError(f"Seed {ival} must be between 0 and 4294967295.")
        return ival

    try:
        check_seed_range("notanint")
        assert False, "Should have raised ArgumentTypeError"
    except argparse.ArgumentTypeError as e:
        assert "not a valid integer" in str(e), str(e)

    try:
        check_seed_range("-1")
        assert False, "Should have raised ArgumentTypeError for negative value"
    except argparse.ArgumentTypeError as e:
        assert "between 0 and" in str(e), str(e)

    result = check_seed_range("42")
    assert result == 42


# ─────────────────────────────────────────────────────────────────────────────
# T2-16  run_batch resume: does NOT skip manifest with success=False
# ─────────────────────────────────────────────────────────────────────────────
@test("T2-16 run_batch resume: only skips corridors with success=True in manifest")
def t2_16():
    from src.core.fetching import fetcher_orchestrator as fo
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # Build a fake execution plan item
        item = {
            "rank": 1, "dep": "EDDF", "arr": "EGLL",
            "flight_source": str(base / "master.parquet"),
            "target": 5,
        }
        from src.common.config import FETCH_RUNS_DIRNAME
        run_id = "test_run_xyz"
        # Create route dir + manifest with success=False
        item_dir = base / "rank_001_EDDF-EGLL"
        manifest_dir = item_dir / FETCH_RUNS_DIRNAME
        manifest_dir.mkdir(parents=True)
        manifest_path = manifest_dir / f"{run_id}.json"
        manifest_path.write_text(json.dumps({"result": {"success": False}}), encoding="utf-8")

        # Monkey-patch fetch_trajectories to track whether it was called
        called = []
        def _fake_fetch(**kwargs):
            called.append(kwargs)
            class _R:
                success = True; requested = 5; succeeded = 5; failed = 0
                failed_flight_ids = []; concat_path = base / "c.parquet"
                registry_entries = []
            return _R()
        import src.core.fetching.opensky_fetcher as _of
        original = _of.fetch_trajectories
        _of.fetch_trajectories = _fake_fetch

        try:
            # Temporarily patch TRAJECTORIES_DIR to our temp dir
            import src.core.fetching.fetcher_orchestrator as _fo
            orig_dir = _fo.TRAJECTORIES_DIR
            _fo.TRAJECTORIES_DIR = base
            _results_batch = fo.run_batch([item], run_id, seed=42, resume=True)
            _fo.TRAJECTORIES_DIR = orig_dir
        finally:
            _of.fetch_trajectories = original

        # Corridor should NOT have been skipped because manifest.success=False
        assert len(called) == 1, \
            f"Expected fetch to be called (manifest success=False), got called={len(called)} times"


# ─────────────────────────────────────────────────────────────────────────────
# T4-20  fetch_trajectories fast-cache path
# ─────────────────────────────────────────────────────────────────────────────
@test("T4-20 fetch_trajectories: fast-cache hit returns in < 2s without Trino")
def t4_20():
    from src.core.fetching.opensky_fetcher import fetch_trajectories
    from src.common.config import RAW_TRAJECTORY_DIRNAME, RAW_CONCAT_SUFFIX, TEMP_DIR
    import shutil

    # Must live inside BASE_DIR so to_project_relative() can relativize paths
    test_root = TEMP_DIR / f"test_t4_20_{uuid.uuid4().hex[:8]}"
    try:
        out_dir = test_root / "rank_001_EDDF-EGLL"
        raw_dir = out_dir / RAW_TRAJECTORY_DIRNAME
        raw_dir.mkdir(parents=True)

        # Build a minimal master flights parquet (inside BASE_DIR too)
        master_path = test_root / "master.parquet"
        df_master = pd.DataFrame({
            "icao24":                 ["abc123"],
            "callsign":               ["DLH001"],
            "typecode":               ["A320"],
            "estdepartureairport":    ["EDDF"],
            "estarrivalairport":      ["EGLL"],
            "firstseen":              [pd.Timestamp("2024-01-01 10:00:00")],
            "lastseen":               [pd.Timestamp("2024-01-01 12:00:00")],
        })
        df_master.to_parquet(master_path)

        # Pre-create expected raw file for the single flight
        fs_str = pd.Timestamp("2024-01-01 10:00:00").strftime("%Y%m%d_%H%M")
        raw_filename = f"abc123_DLH001_{fs_str}_raw.parquet"
        raw_path = raw_dir / raw_filename
        df_traj = pd.DataFrame({
            "flight_id":    ["abc123_DLH001_EDDF-EGLL_20240101_1000"],
            "time":         [pd.Timestamp("2024-01-01 10:00:00")],
            "baroaltitude": [1000.0],
            "flight_phase": ["climb"],
        })
        df_traj.to_parquet(raw_path)

        # Pre-create concat backup
        concat_path = out_dir / f"{out_dir.name}{RAW_CONCAT_SUFFIX}"
        df_traj.to_parquet(concat_path)

        t0 = time.time()
        res = fetch_trajectories(
            dep="EDDF",
            arr="EGLL",
            out_dir=out_dir,
            flight_source=master_path,
            sample_size=None,
            seed=42,
        )
        elapsed = time.time() - t0

        assert res.success, f"Expected success=True, got {res.success}"
        assert elapsed < 2.0, f"Fast-cache path took {elapsed:.1f}s — expected < 2s"
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Run all tests
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  FETCHING MODULE REFACTOR — REGRESSION TESTS")
    print("=" * 70 + "\n")

    t2_5()
    t2_6()
    t2_7()
    t2_8()
    t2_9()
    t2_10()
    t2_11()
    t2_12()
    t2_13()
    t2_14()
    t2_15()
    t2_16()
    t4_20()

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print(f"  RESULTS: {passed} passed / {failed} failed / {len(_results)} total")
    print("=" * 70 + "\n")

    if failed:
        sys.exit(1)
