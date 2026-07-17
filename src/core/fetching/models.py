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
class RouteFetchResult:
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
class RouteFetchSummary:
    """
    Summary of a single corridor fetch operation within a batch run.
    This represents the result of all the routes and is a slimmed-down version of RouteFetchResult.
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
    duration_seconds: float = 0.0

    @classmethod
    def from_resumed(cls, rank: int, dep: str, arr: str, target: int, duration_seconds: float = 0.0) -> "RouteFetchSummary":
        """Build a RouteFetchSummary entry from a skipped corridor due to resume logic."""
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
            duration_seconds=duration_seconds,
        )

    @classmethod
    def from_fetch_result(cls, rank: int, dep: str, arr: str, res: RouteFetchResult) -> "RouteFetchSummary":
        """Build a RouteFetchSummary entry from a standard RouteFetchResult."""
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
            duration_seconds=res.duration_seconds if getattr(res, "duration_seconds", None) is not None else 0.0,
        )

    @classmethod
    def from_error(cls, rank: int, dep: str, arr: str, target: int, error: Exception | str) -> "RouteFetchSummary":
        """Build a RouteFetchSummary entry for a critical error/failed corridor execution."""
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
            duration_seconds=0.0,
        )


@dataclass
class BatchFetchSummary:
    """Canonical summary of an entire batch fetch execution run."""
    run_id: str
    timestamp: str
    cli_params: dict[str, Any]
    corridor_results: list[RouteFetchSummary]

    total_duration_seconds: float = field(init=False)
    total_corridors_requested: int = field(init=False)
    total_corridors_succeeded: int = field(init=False)
    total_corridors_failed: int = field(init=False)
    total_trajectories_requested: int = field(init=False)
    total_trajectories_succeeded: int = field(init=False)
    total_trajectories_failed: int = field(init=False)
    cache_hits: int = field(init=False)
    restore_from_concat: int = field(init=False)
    fetch_from_trino: int = field(init=False)
    fails: int = field(init=False)

    def __post_init__(self) -> None:
        self.total_duration_seconds = sum(c.duration_seconds for c in self.corridor_results)
        self.total_corridors_requested = len(self.corridor_results)
        self.total_corridors_succeeded = sum(1 for c in self.corridor_results if c.success)
        self.total_corridors_failed = self.total_corridors_requested - self.total_corridors_succeeded
        self.total_trajectories_requested = sum(c.requested for c in self.corridor_results)
        self.total_trajectories_succeeded = sum(c.succeeded for c in self.corridor_results)
        self.total_trajectories_failed = sum(c.failed for c in self.corridor_results)
        self.cache_hits = sum(c.cache_hits for c in self.corridor_results)
        self.restore_from_concat = sum(c.restore_from_concat for c in self.corridor_results)
        self.fetch_from_trino = sum(c.fetch_from_trino for c in self.corridor_results)
        self.fails = sum(c.fails for c in self.corridor_results)


