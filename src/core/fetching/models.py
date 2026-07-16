"""
Data models and return contracts for the Fetching module.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.config import MIN_DISTANCE_KM


@dataclass
class FetchRunParams:
    """Effective route and filter state used to derive run_id and log execution metadata."""
    dep: str
    arr: str
    rank: int | None = None
    strategy: str | None = None
    sample_size: int | None = None
    seed: int = 42
    start_date: str | None = None
    end_date: str | None = None
    typecode: str | None = None
    min_distance: float = MIN_DISTANCE_KM
    fetch_format: str | None = None


@dataclass
class FlightFetchOutcome:
    """Outcome of attempting to retrieve a single flight trajectory."""
    flight_id: str
    source: str   # "registry" | "concat" | "trino" | "failed" | "skipped"
                  # Note: Internal tracking only. Global registry stays 2-column (flight_id, file_path).
    raw_path: Path | None = None
    registry_rel_path: str | None = None   # POSIX relative string for the 2-column registry entry
    error: str | None = None


@dataclass
class FetchResult:
    """Route-level result returned by the fetch worker to the orchestrator or CLI caller."""
    success: bool
    dep: str
    arr: str
    run_id: str
    requested: int
    succeeded: int
    failed: int
    skipped: int
    registry_hits: int
    concat_recoveries: int
    trino_fetches: int
    route_dir: Path
    raw_dir: Path
    concat_path: Path
    registry_entries: list[dict[str, str]] = field(default_factory=list)
    failed_flight_ids: list[str] = field(default_factory=list)
    manifest_path: Path | None = None
    duration_seconds: float | None = None


@dataclass
class BatchResults:
    """
    Summary of a single corridor fetch operation within a batch run.
    This represents the result of all the routes and is a slimmed-down version of FetchResult.
    """
    rank: int
    dep: str
    arr: str
    success: bool
    requested: int
    succeeded: int
    failed: int
    resumed: bool
    cache_hits: int = 0
    restore_from_concat: int = 0
    fetch_from_trino: int = 0
    fails: int = 0
    error: str | None = None
    new_dfs: list[str] = field(default_factory=list)
    concat_path: str | None = None

    @classmethod
    def from_resumed(cls, rank: int, dep: str, arr: str, target: int) -> "BatchResults":
        """Build a BatchResults entry from a skipped corridor due to resume logic."""
        return cls(
            rank=rank,
            dep=dep,
            arr=arr,
            success=True,
            requested=target,
            succeeded=target,
            failed=0,
            resumed=True,
            cache_hits=0,
            restore_from_concat=0,
            fetch_from_trino=0,
            fails=0,
        )

    @classmethod
    def from_fetch_result(cls, rank: int, dep: str, arr: str, res: FetchResult) -> "BatchResults":
        """Build a BatchResults entry from a standard FetchResult."""
        return cls(
            rank=rank,
            dep=dep,
            arr=arr,
            success=res.success,
            requested=res.requested,
            succeeded=res.succeeded,
            failed=res.failed,
            resumed=False,
            new_dfs=res.failed_flight_ids,
            concat_path=str(res.concat_path) if getattr(res, "concat_path", None) else None,
            cache_hits=getattr(res, "registry_hits", 0),
            restore_from_concat=getattr(res, "concat_recoveries", 0),
            fetch_from_trino=getattr(res, "trino_fetches", 0),
            fails=res.failed,
        )

    @classmethod
    def from_error(cls, rank: int, dep: str, arr: str, target: int, error: Exception | str) -> "BatchResults":
        """Build a BatchResults entry for a critical error/failed corridor execution."""
        return cls(
            rank=rank,
            dep=dep,
            arr=arr,
            success=False,
            requested=target,
            succeeded=0,
            failed=target,
            resumed=False,
            cache_hits=0,
            restore_from_concat=0,
            fetch_from_trino=0,
            fails=target,
            error=str(error),
        )

