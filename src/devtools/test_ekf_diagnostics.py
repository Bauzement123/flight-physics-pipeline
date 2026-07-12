import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure temp directory exists
(PROJECT_ROOT / "data" / "temp").mkdir(parents=True, exist_ok=True)

from src.common.config import init_runtime
from src.core.processing.kalman_filter import (
    load_ekf_diag_arrays,
    compute_ekf_quality_metrics_from_diag,
    compute_ekf_quality_metrics
)
from src.common.build_global_manifest import (
    extract_metrics_from_diag_file,
    rebuild_ekf_diag_registry,
    main
)

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"

_results: list[tuple[str, bool, str]] = []

def test(name: str):
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

@test("T1: load_ekf_diag_arrays — loads arrays from npz archive")
def t1():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        diag_path = Path(td) / "test_ekf_diag.npz"
        S = np.random.rand(10, 6, 6)
        P = np.random.rand(10, 6, 6)
        e = np.random.rand(10, 6)
        
        np.savez_compressed(diag_path, S_k=S, P_k=P, e_k=e)
        
        S_loaded, P_loaded, e_loaded = load_ekf_diag_arrays(diag_path)
        assert np.allclose(S, S_loaded)
        assert np.allclose(P, P_loaded)
        assert np.allclose(e, e_loaded)

@test("T2: compute_ekf_quality_metrics_from_diag — recomputes metrics correctly")
def t2():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        diag_path = Path(td) / "test_ekf_diag.npz"
        S = np.array([np.eye(6) for _ in range(5)])
        P = np.array([np.eye(6) * 10 for _ in range(5)])
        e = np.array([np.ones(6) for _ in range(5)])
        
        np.savez_compressed(diag_path, S_k=S, P_k=P, e_k=e)
        
        q, nis, tr = compute_ekf_quality_metrics_from_diag(diag_path)
        q_ref, nis_ref, tr_ref = compute_ekf_quality_metrics(S, P, e)
        
        assert np.isclose(q, q_ref)
        assert np.isclose(nis, nis_ref)
        assert np.isclose(tr, tr_ref)

@test("T3: extract_metrics_from_diag_file — recompute and extract stored paths")
def t3():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        diag_path = Path(td) / "test_ekf_diag.npz"
        S = np.array([np.eye(6) for _ in range(5)])
        P = np.array([np.eye(6) * 10 for _ in range(5)])
        e = np.array([np.ones(6) for _ in range(5)])
        stored_metrics = np.array([0.85, 5.5, 60.0])
        
        np.savez_compressed(diag_path, S_k=S, P_k=P, e_k=e, metrics=stored_metrics)
        
        # Test 1: with recompute_metrics = True
        q1, nis1, tr1 = extract_metrics_from_diag_file(diag_path, recompute_metrics=True)
        q_ref, nis_ref, tr_ref = compute_ekf_quality_metrics(S, P, e)
        assert np.isclose(q1, q_ref)
        assert np.isclose(nis1, nis_ref)
        
        # Test 2: with recompute_metrics = False
        q2, nis2, tr2 = extract_metrics_from_diag_file(diag_path, recompute_metrics=False)
        assert np.isclose(q2, stored_metrics[0])
        assert np.isclose(nis2, stored_metrics[1])
        assert np.isclose(tr2, stored_metrics[2])
        
        # Test 3: fallback to nan when metrics missing
        diag_no_metrics_path = Path(td) / "test_ekf_diag_no_metrics.npz"
        np.savez_compressed(diag_no_metrics_path, S_k=S, P_k=P, e_k=e)
        q3, nis3, tr3 = extract_metrics_from_diag_file(diag_no_metrics_path, recompute_metrics=False)
        assert np.isnan(q3)
        assert np.isnan(nis3)
        assert np.isnan(tr3)

@test("T4: rebuild_ekf_diag_registry — scans, compiles and isolates registry")
def t4():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        temp_dir = Path(td)
        mock_registry = temp_dir / "global_ekf_diag_registry.parquet"
        mock_trajectories_dir = temp_dir / "trajectories"
        mock_diag_dir = mock_trajectories_dir / "mock_dataset" / "diagnostics"
        mock_diag_dir.mkdir(parents=True, exist_ok=True)
        
        diag_file = mock_diag_dir / "MOCK123_ekf_diag.npz"
        S = np.array([np.eye(6) for _ in range(5)])
        P = np.array([np.eye(6) * 10 for _ in range(5)])
        e = np.array([np.ones(6) for _ in range(5)])
        stored_metrics = np.array([0.9, 4.0, 50.0])
        np.savez_compressed(diag_file, S_k=S, P_k=P, e_k=e, metrics=stored_metrics)
        
        diag_file2 = mock_diag_dir / "MOCK456_ekf_diag.npz"
        
        with patch("src.common.build_global_manifest.GLOBAL_EKF_DIAG_REGISTRY", mock_registry), \
             patch("src.common.build_global_manifest.TRAJECTORIES_DIR", mock_trajectories_dir):
            
            # Run manifest builder
            rebuild_ekf_diag_registry(force=True, recompute_metrics=False)
            
            assert mock_registry.exists()
            df = pd.read_parquet(mock_registry)
            
            # Verify schema
            expected_cols = ["flight_id", "diag_file_path", "ekf_quality_score", "ekf_max_trace_p", "ekf_mean_nis"]
            assert list(df.columns) == expected_cols, f"Unexpected columns: {list(df.columns)}"
            
            # Verify content
            row = df[df["flight_id"] == "MOCK123"]
            assert len(row) == 1
            assert row["diag_file_path"].iloc[0] == diag_file.resolve().relative_to(PROJECT_ROOT).as_posix()
            assert np.isclose(row["ekf_quality_score"].iloc[0], 0.9)
            assert np.isclose(row["ekf_max_trace_p"].iloc[0], 50.0)
            assert np.isclose(row["ekf_mean_nis"].iloc[0], 4.0)
            
            # Test incremental build (force=False)
            np.savez_compressed(diag_file2, S_k=S, P_k=P, e_k=e, metrics=stored_metrics)
            
            rebuild_ekf_diag_registry(force=False, recompute_metrics=False)
            df_new = pd.read_parquet(mock_registry)
            assert len(df_new) == 2
            assert "MOCK123" in df_new["flight_id"].values
            assert "MOCK456" in df_new["flight_id"].values

@test("T5: force=False, recompute_metrics=True updates pre-indexed entries")
def t5():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        temp_dir = Path(td)
        mock_registry = temp_dir / "global_ekf_diag_registry.parquet"
        mock_trajectories_dir = temp_dir / "trajectories"
        mock_diag_dir = mock_trajectories_dir / "mock_dataset" / "diagnostics"
        mock_diag_dir.mkdir(parents=True, exist_ok=True)
        
        diag_file = mock_diag_dir / "MOCK123_ekf_diag.npz"
        S = np.array([np.eye(6) for _ in range(5)])
        P = np.array([np.eye(6) * 10 for _ in range(5)])
        e = np.array([np.ones(6) for _ in range(5)])
        stored_metrics = np.array([0.9, 4.0, 50.0])
        np.savez_compressed(diag_file, S_k=S, P_k=P, e_k=e, metrics=stored_metrics)
        with patch("src.common.build_global_manifest.GLOBAL_EKF_DIAG_REGISTRY", mock_registry), \
             patch("src.common.build_global_manifest.TRAJECTORIES_DIR", mock_trajectories_dir):
            
            # Index it first (stored_metrics are loaded)
            rebuild_ekf_diag_registry(force=True, recompute_metrics=False)
            df1 = pd.read_parquet(mock_registry, memory_map=False)
            assert np.isclose(df1["ekf_quality_score"].iloc[0], 0.9)
            
            # Recomputing should run EKF quality function, ignoring the stored metrics on disk
            rebuild_ekf_diag_registry(force=False, recompute_metrics=True)
            df2 = pd.read_parquet(mock_registry, memory_map=False)
            assert len(df2) == 1
            # Verify that EKF metrics are updated via recomputation
            assert not np.isclose(df2["ekf_quality_score"].iloc[0], 0.9)
            assert np.isclose(df2["ekf_quality_score"].iloc[0], compute_ekf_quality_metrics(S, P, e)[0])

@test("T6: main CLI entrypoint accepts --diag-only / --only args")
def t6():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        temp_dir = Path(td)
        mock_registry = temp_dir / "global_ekf_diag_registry.parquet"
        mock_trajectories_dir = temp_dir / "trajectories"
        
        with patch("src.common.build_global_manifest.GLOBAL_EKF_DIAG_REGISTRY", mock_registry), \
             patch("src.common.build_global_manifest.TRAJECTORIES_DIR", mock_trajectories_dir):
            
            # Call main with --diag-only. Should run without error and not raise SystemExit
            main(["--diag-only"])
            assert mock_registry.exists()
            df = pd.read_parquet(mock_registry)
            assert df.empty


@test("T7: index_synthesized_files supports force=False incremental updates")
def t7():
    from src.common.build_global_manifest import index_synthesized_files
    from src.common.config import GLOBAL_MODEL_REGISTRY
    
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        temp_dir = Path(td)
        mock_registry = temp_dir / "global_model_registry.parquet"
        mock_corridor_paths_dir = temp_dir / "corridor_paths"
        mock_corridor_paths_dir.mkdir(parents=True, exist_ok=True)
        
        # Create a mock synthesized file
        sf = mock_corridor_paths_dir / "EDDF-LIRF_synthesized_c1.parquet"
        pd.DataFrame({"route_class": [1]}).to_parquet(sf)
        
        with patch("src.common.build_global_manifest.GLOBAL_MODEL_REGISTRY", mock_registry), \
             patch("src.common.build_global_manifest.save_model_registry", lambda df: df.to_parquet(mock_registry, index=False)):
            
            # Rebuild first time
            index_synthesized_files(mock_registry, mock_corridor_paths_dir, force=True)
            assert mock_registry.exists()
            df1 = pd.read_parquet(mock_registry)
            assert len(df1) == 1
            assert df1["route"].iloc[0] == "EDDF-LIRF"
            
            # Add a second mock synthesized file
            sf2 = mock_corridor_paths_dir / "EGLL-BIKF_synthesized_c2.parquet"
            pd.DataFrame({"route_class": [2]}).to_parquet(sf2)
            
            # Rebuild second time with force=False (should load existing and skip first file)
            index_synthesized_files(mock_registry, mock_corridor_paths_dir, force=False)
            df2 = pd.read_parquet(mock_registry)
            assert len(df2) == 2
            assert "EDDF-LIRF" in df2["route"].values
            assert "EGLL-BIKF" in df2["route"].values


if __name__ == "__main__":
    init_runtime()
    print("\n" + "=" * 70)
    print("  EKF DIAGNOSTIC REGISTRY TESTS (ISOLATED)")
    print("=" * 70 + "\n")
    
    t1()
    t2()
    t3()
    t4()
    t5()
    t6()
    t7()
    
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print(f"  RESULTS: {passed} passed / {failed} failed / {len(_results)} total")
    print("=" * 70 + "\n")
    
    if failed:
        sys.exit(1)
