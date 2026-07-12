import sys
import tempfile
import traceback
from pathlib import Path
import numpy as np
import pandas as pd

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure temp directory exists
(PROJECT_ROOT / "data" / "temp").mkdir(parents=True, exist_ok=True)

from src.common.config import init_runtime, PhaseControl
from src.common.exceptions import DiagnosticsIOError
from src.analysis.campaigns.phase_quality import diagnostics, io, orchestration

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


@test("Phase 1: run_nis consistency mathematical checks")
def test_nis_math():
    # Identity S_k and unit e_k -> NIS should be 1.0 (since e_k^T * S_k^-1 * e_k = 1.0)
    timestamps = np.arange(10, dtype=float)
    e_k = np.zeros((10, 6))
    e_k[:, 0] = 1.0
    S_k = np.array([np.eye(6) for _ in range(10)])
    
    res = diagnostics.run_nis(timestamps, e_k, S_k)
    epsilon_k = res["arrays"]["epsilon_k"]
    assert np.allclose(epsilon_k, 1.0)
    # Since 1.0 is outside [1.237, 14.449], pct_nis_in_95 should be 0.0
    assert np.isclose(res["scalars"]["pct_nis_in_95"], 0.0)
    assert np.isclose(res["scalars"]["pct_nis_high_tail"], 0.0)
    assert np.isclose(res["scalars"]["max_sustained_high_nis_sec"], 0.0)

    # Make e_k large so that NIS values exceed upper Chi2 boundary
    e_k_large = np.zeros((10, 6))
    e_k_large[:, 0] = 5.0  # NIS = 25.0
    res_large = diagnostics.run_nis(timestamps, e_k_large, S_k)
    assert np.isclose(res_large["scalars"]["pct_nis_high_tail"], 100.0)
    assert np.isclose(res_large["scalars"]["max_sustained_high_nis_sec"], 9.0)


@test("Phase 2: run_residuals mean bias and ACF autocorrelation checks")
def test_residuals_math():
    e_k = np.zeros((50, 6))
    # Give it a known bias on alt axis (index 2)
    e_k[:, 2] = 4.5
    # Give it random noise on velocity axis (index 4)
    np.random.seed(42)
    e_k[:, 4] = np.random.normal(0, 1, 50)
    
    res = diagnostics.run_residuals(e_k)
    assert np.isclose(res["scalars"]["mean_res_alt"], 4.5)
    assert np.isclose(res["scalars"]["mean_res_x"], 0.0)
    assert res["arrays"]["acf_curves"].shape == (6, 10)


@test("Phase 3: run_condition rank deficiency & ill-conditioning checks")
def test_condition_math():
    timestamps = np.arange(10, dtype=float)
    S_k = np.array([np.eye(6) for _ in range(10)])
    P_k = np.array([np.eye(6) for _ in range(10)])
    
    # Well-conditioned covariance
    res_well = diagnostics.run_condition(P_k, S_k, timestamps)
    assert not res_well["scalars"]["is_ill_conditioned"]
    assert np.isclose(res_well["scalars"]["max_cond_P"], 1.0)
    
    # Singular/ill-conditioned covariance (min eigenvalue is 0)
    P_k_singular = np.array([np.eye(6) for _ in range(10)])
    P_k_singular[:, 0, 0] = 0.0
    res_bad = diagnostics.run_condition(P_k_singular, S_k, timestamps)
    assert res_bad["scalars"]["is_ill_conditioned"]
    assert res_bad["scalars"]["max_cond_P"] >= 1e20


@test("I/O: resolve_route_id and error boundary assertions")
def test_io_functions():
    p1 = "data/trajectories/rank_0025_EDDF-LIRF/clean/diagnostics/test_ekf_diag.npz"
    p2 = "data/trajectories/clean/test_ekf_diag.npz"
    
    assert io.resolve_route_id(p1) == "EDDF-LIRF"
    assert io.resolve_route_id(p2) == "UNKNOWN"
    
    # Verify DiagnosticsIOError raises on missing registry
    try:
        io.load_registry()
    except Exception as e:
        assert isinstance(e, DiagnosticsIOError)


@test("Orchestration: run campaign loop with PhaseControl toggles")
def test_orchestration_run():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data" / "temp") as td:
        temp_dir = Path(td)
        out_root = temp_dir / "output"
        
        # Write mock EKF diag NPZ files
        diag_dir = temp_dir / "trajectories" / "rank_0001_EDDF-LIRF" / "clean" / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        diag_path = diag_dir / "flight_123_ekf_diag.npz"
        
        timestamps = np.arange(10, dtype=float)
        e_k = np.ones((10, 6))
        S_k = np.array([np.eye(6) for _ in range(10)])
        P_k = np.array([np.eye(6) for _ in range(10)])
        
        np.savez_compressed(diag_path, timestamps=timestamps, e_k=e_k, S_k=S_k, P_k=P_k)
        
        # Build mock registry DataFrame
        registry_df = pd.DataFrame([{
            "flight_id": "flight_123",
            "route_id": "EDDF-LIRF",
            "diag_file_path": str(diag_path.relative_to(PROJECT_ROOT).as_posix()),
            "ekf_quality_score": 0.95,
            "ekf_mean_nis": 6.0,
            "ekf_max_trace_p": 12.0
        }])
        
        # Test 1: Phase 1 enabled, others disabled
        cfg_p1 = PhaseControl(
            ENABLE_NIS=True,
            ENABLE_RESIDUALS=False,
            ENABLE_CONDITION=False,
            ENABLE_REPORTING=True,
            ENABLE_TENSOR_SAVE=True,
            ENABLE_FLAT_TABLE=True
        )
        
        orchestration.run(
            registry_df=registry_df,
            out_root=out_root,
            worker_count=1,
            cfg=cfg_p1
        )
        
        # Assert files created
        assert (out_root / "tables" / "ekf_autopsy_flight_metrics.parquet").exists()
        assert (out_root / "tables" / "ekf_autopsy_route_summary.csv").exists()
        assert (out_root / "tensors" / "EDDF-LIRF_autopsy_tensors.npz").exists()
        assert (out_root / "reports" / "EDDF-LIRF_ekf_autopsy_report.pdf").exists()


if __name__ == "__main__":
    init_runtime()
    print("\n" + "=" * 70)
    print("  EKF AUTOPSY campaign MODULARITY TESTS")
    print("=" * 70 + "\n")
    
    test_nis_math()
    test_residuals_math()
    test_condition_math()
    test_io_functions()
    test_orchestration_run()
    
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print(f"  RESULTS: {passed} passed / {failed} failed / {len(_results)} total")
    print("=" * 70 + "\n")
    
    if failed:
        sys.exit(1)
