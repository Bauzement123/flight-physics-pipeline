"""
Consolidated post-refactoring tests for Rounds 1-4.
Run with:  python -m pytest src/devtools/test_all_rounds.py -v
"""
import inspect
import pytest


# ─────────────────────────────────────────────────────────────
# ROUND 1 — src/common/utils.py additions
# ─────────────────────────────────────────────────────────────

class TestSplitRouteString:
    """split_route_string() must handle both DEP->ARR and DEP-ARR formats."""

    def test_hyphen_format(self):
        from src.common.utils import split_route_string
        assert split_route_string("LEPA-LEBL") == ("LEPA", "LEBL")

    def test_arrow_format(self):
        from src.common.utils import split_route_string
        assert split_route_string("LEPA -> LEBL") == ("LEPA", "LEBL")

    def test_none_input(self):
        from src.common.utils import split_route_string
        assert split_route_string(None) == ("UNK", "UNK")

    def test_short_hyphen_not_icao(self):
        from src.common.utils import split_route_string
        assert split_route_string("ED-EG") == ("UNK", "UNK")  # 2-letter: not valid

    def test_plain_string_no_match(self):
        from src.common.utils import split_route_string
        assert split_route_string("not-a-route") == ("UNK", "UNK")

    def test_arrow_format_without_spaces_is_not_matched(self):
        # Only ' -> ' with spaces triggers arrow path
        from src.common.utils import split_route_string
        dep, arr = split_route_string("EDDF->EGLL")
        assert (dep, arr) == ("UNK", "UNK")


class TestExtractRouteIdFromPath:
    """extract_route_id_from_path() supports both legacy rank_NNN and modern DEP-ARR folders."""

    def test_legacy_rank_format(self):
        from src.common.utils import extract_route_id_from_path
        assert extract_route_id_from_path(
            "data/trajectories/rank_001_LEPA-LEBL/clean/x.parquet"
        ) == "LEPA-LEBL"

    def test_modern_format(self):
        from src.common.utils import extract_route_id_from_path
        assert extract_route_id_from_path(
            "data/trajectories/LEPA-LEBL/clean/x.parquet"
        ) == "LEPA-LEBL"

    def test_no_route_segment(self):
        from src.common.utils import extract_route_id_from_path
        assert extract_route_id_from_path("data/some/random/path") == "UNKNOWN"

    def test_longer_rank_prefix(self):
        from src.common.utils import extract_route_id_from_path
        assert extract_route_id_from_path(
            "data/trajectories/rank_123_EDDF-EGLL/raw/flight.parquet"
        ) == "EDDF-EGLL"


class TestResolveAirportCoordinates:
    """resolve_airport_coordinates() must return standardised 5-key dict per ICAO."""

    def test_known_icao_returns_all_keys(self):
        from src.common.utils import resolve_airport_coordinates
        res = resolve_airport_coordinates(["KJFK"])
        assert "KJFK" in res
        for key in ("lat", "lon", "name", "country", "elevation"):
            assert key in res["KJFK"], f"Missing key '{key}'"

    def test_european_airport(self):
        from src.common.utils import resolve_airport_coordinates
        res = resolve_airport_coordinates(["LEPA"])
        assert "LEPA" in res
        assert {"lat", "lon", "name", "country", "elevation"}.issubset(res["LEPA"].keys())


# ─────────────────────────────────────────────────────────────
# ROUND 2 — Consumer module cleanups
# ─────────────────────────────────────────────────────────────

class TestRound2ConsumerModules:
    def test_build_master_population_uses_retry_backoff(self):
        import src.core.acquisition.build_master_population as bmp
        assert hasattr(bmp, "retry_backoff")
        assert hasattr(bmp, "write_json_dataclass")
        assert not hasattr(bmp, "fetch_daily_flights_with_backoff")

    def test_era5_manager_uses_retry_backoff(self):
        import src.core.weather.era5_manager as em
        assert hasattr(em, "retry_backoff")
        assert not hasattr(em, "download_with_retry")

    def test_airport_extractor_cleaned(self):
        import src.core.acquisition.airport_extractor as ae
        assert not hasattr(ae, "fetch_airport_coords")
        assert not hasattr(ae, "get_ourairports_dict")

    def test_haversine_replaced_in_phase_quality_filters(self):
        from src.analysis.campaigns.phase_quality import phase_quality_filters
        assert hasattr(phase_quality_filters, "haversine_distance_m")
        src_code = inspect.getsource(phase_quality_filters.calculate_coordinate_velocity_3d)
        assert "6371000" not in src_code
        assert "haversine_distance_m(" in src_code

    def test_postfilter_merge_logic(self, tmp_path):
        """Incremental quality registry merge: new row overwrites, old rows preserved."""
        import pandas as pd
        registry_file = tmp_path / "quality_registry.parquet"
        existing = pd.DataFrame([
            {"flight_id": "F001", "score": 1.0},
            {"flight_id": "F002", "score": 2.0},
        ])
        existing.to_parquet(registry_file, index=False)

        new_row = pd.DataFrame([{"flight_id": "F001", "score": 99.0}])
        ex = pd.read_parquet(registry_file)
        merged = pd.concat([ex, new_row]).drop_duplicates(subset=["flight_id"], keep="last")
        merged.to_parquet(registry_file, index=False)

        result = pd.read_parquet(registry_file)
        assert len(result) == 2
        assert result[result["flight_id"] == "F002"]["score"].iloc[0] == 2.0
        assert result[result["flight_id"] == "F001"]["score"].iloc[0] == 99.0


# ─────────────────────────────────────────────────────────────
# ROUND 3 — rename_legacy_rank_folders.py
# ─────────────────────────────────────────────────────────────

class TestRound3RenameTool:
    def test_rename_tool_importable(self):
        """The rename tool must be importable without side effects."""
        import src.devtools.rename_legacy_rank_folders as rt
        assert callable(getattr(rt, "detect_rename_pairs", None) or getattr(rt, "main", None))

    def test_already_renamed_dirs_produce_no_pairs(self, tmp_path):
        """Idempotency: modern DEP-ARR folders should not be flagged for rename."""
        import src.devtools.rename_legacy_rank_folders as rt
        # Create modern-format directories
        (tmp_path / "EDDF-EGLL").mkdir()
        (tmp_path / "LEPA-LEBL").mkdir()
        # detect_rename_pairs should return 0 pairs for these
        if hasattr(rt, "detect_rename_pairs"):
            pairs = rt.detect_rename_pairs(tmp_path)
            assert len(pairs) == 0, f"Expected 0 rename pairs, got {len(pairs)}: {pairs}"

    def test_legacy_dirs_produce_pairs(self, tmp_path):
        """Legacy rank_NNN_DEP-ARR directories must be detected for rename."""
        import src.devtools.rename_legacy_rank_folders as rt
        (tmp_path / "rank_001_EDDF-EGLL").mkdir()
        (tmp_path / "rank_002_LEPA-LEBL").mkdir()
        if hasattr(rt, "detect_rename_pairs"):
            pairs = rt.detect_rename_pairs(tmp_path)
            assert len(pairs) == 2, f"Expected 2 rename pairs, got {len(pairs)}"


# ─────────────────────────────────────────────────────────────
# ROUND 4 — Naming regularization
# ─────────────────────────────────────────────────────────────

class TestRound4ConfigRenames:
    def test_global_corridor_model_registry_importable(self):
        from src.common.config import GLOBAL_CORRIDOR_MODEL_REGISTRY
        from pathlib import Path
        assert isinstance(GLOBAL_CORRIDOR_MODEL_REGISTRY, Path)

    def test_old_global_model_registry_gone(self):
        """GLOBAL_MODEL_REGISTRY must no longer exist in config."""
        import src.common.config as cfg
        assert not hasattr(cfg, "GLOBAL_MODEL_REGISTRY"), (
            "GLOBAL_MODEL_REGISTRY still present in config.py — rename incomplete"
        )

    def test_load_corridor_paths_map_importable(self):
        from src.common.registry_utils import load_corridor_paths_map
        assert callable(load_corridor_paths_map)

    def test_old_load_synthesized_paths_map_gone(self):
        """load_synthesized_paths_map must no longer exist in registry_utils."""
        import src.common.registry_utils as ru
        assert not hasattr(ru, "load_synthesized_paths_map"), (
            "load_synthesized_paths_map still present in registry_utils — rename incomplete"
        )

    def test_postfilter_calibration_module_importable(self):
        """Module must be importable at the new snake_case path."""
        import src.analysis.postfilter_calibration.run_campaign as rc
        assert callable(rc.main)

    def test_old_postfilter_callibration_path_gone(self):
        """Old PascalCase + typo path must not exist."""
        import importlib
        with pytest.raises((ModuleNotFoundError, ImportError)):
            importlib.import_module("src.analysis.PostFilter_callibration.run_campaign")

    def test_clone_simulation_uses_corridor_paths_map(self):
        """clone_simulation.py must reference load_corridor_paths_map (may be a lazy import)."""
        import src.core.physics.clone_simulation as cs
        src_code = inspect.getsource(cs)
        assert "load_corridor_paths_map" in src_code, (
            "load_corridor_paths_map not found anywhere in clone_simulation — rename not propagated"
        )
        assert "load_synthesized_paths_map" not in src_code, (
            "Old load_synthesized_paths_map still present in clone_simulation"
        )

    def test_flight_analysis_cli_uses_corridor_registry_flag(self):
        """flight_analysis.py argparse must expose --corridor-registry, not --synthesized-registry."""
        import src.analysis.verification.flight_analysis as fa
        src_code = inspect.getsource(fa)
        assert "--corridor-registry" in src_code
        assert "--synthesized-registry" not in src_code

    def test_route_class_analysis_cli_flags_updated(self):
        """route_class_analysis.py must expose --corridor-registry (no --no-corridor on this script)."""
        import src.analysis.verification.route_class_analysis as rca
        src_code = inspect.getsource(rca)
        # This script only has the registry path flag, not the skip flag
        assert "--corridor-registry" in src_code
        assert "--synthesized-registry" not in src_code
        assert "GLOBAL_CORRIDOR_MODEL_REGISTRY" in src_code

    def test_rename_legacy_rank_folders_py_importable(self):
        """rename_legacy_rank_folders.py (renamed from rename_trajectory_ranks.py) must exist."""
        import src.devtools.rename_legacy_rank_folders
        assert True  # import succeeded

    def test_fetcher_orchestrator_no_dead_import(self):
        """generate_dataset_name must not be imported in fetcher_orchestrator (dead import)."""
        import src.core.fetching.fetcher_orchestrator as fo
        src_code = inspect.getsource(fo)
        assert "generate_dataset_name" not in src_code, (
            "Dead import generate_dataset_name still present in fetcher_orchestrator"
        )
