import numpy as np
import scipy.linalg

# Chi-Square (df=6) theoretical properties
CHI2_DF = 6
# Two-sided 95% confidence bounds for Chi-Square df=6
CHI2_LOWER = 1.237
CHI2_UPPER = 14.449

# Target state dimension names
STATE_COLS = ["x", "y", "alt_baro", "math_angle", "velocity", "vert_rate"]


def sym_cond(M: np.ndarray) -> float:
    """Computes the condition number of a symmetric matrix M using eigvalsh."""
    try:
        w = np.linalg.eigvalsh(M)
        min_w, max_w = w[0], w[-1]
        if min_w <= 1e-30:
            return 1e30
        return float(max_w / min_w)
    except Exception:
        return 1e30


def run_nis(timestamps: np.ndarray, e_k: np.ndarray, S_k: np.ndarray) -> dict:
    """Phase 1: NIS consistency checks against Chi-Square (df=6) distribution."""
    T = len(timestamps)
    epsilon_k = np.zeros(T)
    for i in range(T):
        try:
            # Solve S_k * x = e_k for stability
            x = scipy.linalg.solve(S_k[i], e_k[i])
            epsilon_k[i] = np.dot(e_k[i], x)
        except Exception:
            # Fallback to pseudo-inverse if singular
            try:
                epsilon_k[i] = np.dot(e_k[i], np.dot(np.linalg.pinv(S_k[i]), e_k[i]))
            except Exception:
                epsilon_k[i] = np.nan
                
    # Mask out NaNs
    valid_mask = ~np.isnan(epsilon_k)
    valid_eps = epsilon_k[valid_mask]
    valid_t = timestamps[valid_mask]
    
    if len(valid_eps) == 0:
        raise ValueError("All computed NIS values are NaN.")
        
    pct_nis_in_95 = float(np.sum((valid_eps >= CHI2_LOWER) & (valid_eps <= CHI2_UPPER)) / len(valid_eps) * 100)
    pct_nis_high_tail = float(np.sum(valid_eps > CHI2_UPPER) / len(valid_eps) * 100)
    
    # Compute maximum sustained high NIS duration (seconds)
    is_high = valid_eps > CHI2_UPPER
    max_sustained_high_nis_sec = 0.0
    current_sustained = 0.0
    for idx in range(len(is_high)):
        if is_high[idx]:
            if idx > 0 and is_high[idx - 1]:
                current_sustained += float(valid_t[idx] - valid_t[idx - 1])
            else:
                current_sustained = 0.0
            if current_sustained > max_sustained_high_nis_sec:
                max_sustained_high_nis_sec = current_sustained
        else:
            current_sustained = 0.0
            
    return {
        "scalars": {
            "pct_nis_in_95": pct_nis_in_95,
            "pct_nis_high_tail": pct_nis_high_tail,
            "max_sustained_high_nis_sec": max_sustained_high_nis_sec,
        },
        "arrays": {
            "epsilon_k": epsilon_k
        }
    }


def run_residuals(e_k: np.ndarray) -> dict:
    """Phase 2: Zero-mean residual bias and autocorrelation whiteness audits."""
    T = len(e_k)
    mean_residuals = np.mean(e_k, axis=0)  # shape (6,)
    std_residuals = np.std(e_k, axis=0)    # shape (6,)
    
    # Autocorrelation (ACF) Lags 1-10 per axis
    acf_curves = np.zeros((6, 10))
    for axis_idx in range(6):
        res_axis = e_k[:, axis_idx]
        mean_val = mean_residuals[axis_idx]
        var_val = np.var(res_axis)
        if var_val > 1e-12:
            for lag in range(1, 11):
                if lag < T:
                    num = np.sum((res_axis[:-lag] - mean_val) * (res_axis[lag:] - mean_val))
                    den = np.sum((res_axis - mean_val) ** 2)
                    acf_curves[axis_idx, lag - 1] = num / den if den > 1e-12 else 0.0
                    
    max_acf_lag1 = float(np.max(acf_curves[:, 0]))
    return {
        "scalars": {
            "mean_res_x": float(mean_residuals[0]),
            "mean_res_y": float(mean_residuals[1]),
            "mean_res_alt": float(mean_residuals[2]),
            "mean_res_theta": float(mean_residuals[3]),
            "mean_res_v": float(mean_residuals[4]),
            "mean_res_vz": float(mean_residuals[5]),
            "std_res_alt": float(std_residuals[2]),
            "std_res_v": float(std_residuals[4]),
            "max_acf_lag1": max_acf_lag1,
        },
        "arrays": {
            "acf_curves": acf_curves
        }
    }


def run_condition(P_k: np.ndarray, S_k: np.ndarray, timestamps: np.ndarray) -> dict:
    """Phase 3: Condition number analysis and unobservable state drift tracking."""
    T = len(timestamps)
    cond_P_series = np.array([sym_cond(P_k[i]) for i in range(T)])
    cond_S_series = np.array([sym_cond(S_k[i]) for i in range(T)])
    
    # Mid-flight condition: check steps between 10% and 90% of duration
    start_idx = int(0.1 * T)
    end_idx = int(0.9 * T)
    if start_idx >= end_idx:
        start_idx, end_idx = 0, T
        
    cond_P_mid = cond_P_series[start_idx:end_idx]
    max_cond_P_midflight = float(np.max(cond_P_mid)) if len(cond_P_mid) > 0 else 1.0
    
    # Ill-conditioned duration check
    is_bad_cond = cond_P_series > 1e6
    max_bad_cond_dur = 0.0
    curr_bad_cond_dur = 0.0
    for idx in range(len(is_bad_cond)):
        if is_bad_cond[idx]:
            if idx > 0 and is_bad_cond[idx - 1]:
                curr_bad_cond_dur += float(timestamps[idx] - timestamps[idx - 1])
            else:
                curr_bad_cond_dur = 0.0
            if curr_bad_cond_dur > max_bad_cond_dur:
                max_bad_cond_dur = curr_bad_cond_dur
        else:
            curr_bad_cond_dur = 0.0
            
    is_ill_conditioned = bool((max_cond_P_midflight > 1e6) or (max_bad_cond_dur > 60.0))
    
    # Covariance state variance drift (diagonals of P_k)
    diag_P_series = np.array([np.diagonal(P_k[i]) for i in range(T)])  # (T, 6)
    max_var_alt = float(np.max(diag_P_series[:, 2]))
    max_var_vz = float(np.max(diag_P_series[:, 5]))
    
    return {
        "scalars": {
            "max_cond_P": float(np.max(cond_P_series)),
            "max_cond_P_midflight": max_cond_P_midflight,
            "max_cond_S": float(np.max(cond_S_series)),
            "max_bad_cond_dur_sec": max_bad_cond_dur,
            "is_ill_conditioned": is_ill_conditioned,
            "max_var_alt": max_var_alt,
            "max_var_vz": max_var_vz,
        },
        "arrays": {
            "cond_P_series": cond_P_series,
            "diag_P_series": diag_P_series
        }
    }
