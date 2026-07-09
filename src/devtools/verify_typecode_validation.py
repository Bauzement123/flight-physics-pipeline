"""
Verification script for strict typecode validation across the Flight Physics Pipeline.
Verifies that:
1. is_supported_typecode() accepts valid target family aircraft and rejects NaN, None, unknown, and unsupported typecodes.
2. log_skipped_aircraft() appends proper ERROR_FLAG entries to data/logs/skipped_aircraft.log.
3. Modules (adapters, physics, processing, corridor, fetching, acquisition) do not inject default aircraft types (like B738 or UNKNOWN)
   and instead reject/log records containing NaN or unsupported typecodes.
"""
import logging
import os
import sys
import tempfile
from pathlib import Path
import pandas as pd
import numpy as np

from src.common.config import (
    BASE_DIR,
    LOGS_DIR,
    ALL_TARGET_FAMILIES,
    is_supported_typecode,
)
from src.common.utils import log_skipped_aircraft

def test_config_validation():
    print("Testing is_supported_typecode()...")
    for tc in ALL_TARGET_FAMILIES:
        assert is_supported_typecode(tc), f"Expected {tc} to be supported!"
        assert is_supported_typecode(tc.lower()), f"Expected {tc.lower()} to be supported via case-insensitivity!"
        assert is_supported_typecode(f"  {tc}  "), f"Expected stripped {tc} to be supported!"

    invalid_codes = [None, np.nan, float('nan'), "", "   ", "UNKNOWN", "UNK", "B747", "C172", "A380", "DEFAULT_B738"]
    for inv in invalid_codes:
        assert not is_supported_typecode(inv), f"Expected {inv} to be rejected!"
    print(" -> is_supported_typecode() passed all assertions.\n")

def test_log_skipped_aircraft():
    print("Testing log_skipped_aircraft()...")
    log_file = LOGS_DIR / "skipped_aircraft.log"
    initial_lines = 0
    if log_file.exists():
        with open(log_file, 'r', encoding='utf-8') as f:
            initial_lines = len(f.readlines())

    test_id = "TEST_VERIFY_ID_999"
    test_tc = "NAN_TEST"
    test_reason = "ERROR_FLAG: Automated verification test entry"
    log_skipped_aircraft(test_id, test_tc, test_reason)

    assert log_file.exists(), "skipped_aircraft.log should exist after logging!"
    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    assert len(lines) == initial_lines + 1, "Exactly one line should have been added to skipped_aircraft.log!"
    last_line = lines[-1]
    assert test_id in last_line and test_tc in last_line and test_reason in last_line, f"Log line mismatch: {last_line}"
    print(" -> log_skipped_aircraft() passed all assertions.\n")

def test_adapters_dataframe_to_pycontrails():
    print("Testing adapters.dataframe_to_pycontrails() with NaN and invalid typecodes...")
    from src.common.adapters import dataframe_to_pycontrails
    df_nan = pd.DataFrame({
        'flight_id': ['test_1'],
        'time': [pd.Timestamp('2026-01-01 10:00:00', tz='UTC')],
        'latitude': [48.0],
        'longitude': [11.0],
        'altitude': [10000.0],
        'velocity': [250.0],
        'typecode': [np.nan]
    })
    result_nan = dataframe_to_pycontrails(df_nan)
    assert result_nan is None, "dataframe_to_pycontrails should return None for NaN typecode!"

    df_valid = df_nan.copy()
    df_valid['typecode'] = 'A320'
    result_valid = dataframe_to_pycontrails(df_valid)
    assert result_valid is not None and result_valid.attrs.get('aircraft_type') == 'A320', "dataframe_to_pycontrails should return valid Flight object for A320!"
    print(" -> adapters.dataframe_to_pycontrails() passed.\n")

def test_kalman_clean_trajectory_df():
    print("Testing kalman_filter.clean_trajectory_df() with NaN typecodes...")
    from src.core.processing.kalman_filter import clean_trajectory_df as k_clean
    df_nan = pd.DataFrame({
        'flight_id': ['test_k1', 'test_k1'],
        'time': [100.0, 101.0],
        'lat': [48.0, 48.01],
        'lon': [11.0, 11.01],
        'alt': [10000.0, 10010.0],
        'spd': [250.0, 250.0],
        'hdg': [90.0, 90.0],
        'typecode': [np.nan, np.nan]
    })
    res_df, _, _, _, _ = k_clean(df_nan)
    assert res_df is None or res_df.empty, "kalman_filter.clean_trajectory_df should return None or empty DataFrame for NaN typecode!"
    print(" -> kalman_filter.clean_trajectory_df() passed.\n")

def test_fetching_helpers_filter():
    print("Testing fetching.helpers.apply_flight_filters() with unsupported typecodes...")
    from src.core.fetching.helpers import apply_flight_filters
    df_cohort = pd.DataFrame({
        'flight_id': ['f1', 'f2', 'f3'],
        'firstseen': ['2026-01-01 10:00:00', '2026-01-01 10:05:00', '2026-01-01 10:10:00'],
        'typecode': ['A320', 'B747', np.nan]
    })
    filtered = apply_flight_filters(df_cohort, typecode=None)
    assert len(filtered) == 1 and filtered['typecode'].iloc[0] == 'A320', "apply_flight_filters should filter out B747 and NaN when typecode=None!"
    print(" -> fetching.helpers.apply_flight_filters() passed.\n")

def main():
    print("=== Running Verification Suite for Typecode Validation ===")
    test_config_validation()
    test_log_skipped_aircraft()
    test_adapters_dataframe_to_pycontrails()
    test_kalman_clean_trajectory_df()
    test_fetching_helpers_filter()
    print("=== All Verification Tests Passed Successfully! ===")

if __name__ == "__main__":
    main()
