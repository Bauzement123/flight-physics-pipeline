from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd


@dataclass
class MasterFlightQuery:
    dep_date_start: Optional[pd.Timestamp] = None
    dep_date_end: Optional[pd.Timestamp] = None
    routes: Optional[List[str]] = None
    typecodes: Optional[List[str]] = None
    icao24s: Optional[List[str]] = None
    callsigns: Optional[List[str]] = None
    dep_airports: Optional[List[str]] = None
    arr_airports: Optional[List[str]] = None


@dataclass
class RouteSummaryQuery:
    routes: Optional[List[str]] = None
    dep_airports: Optional[List[str]] = None
    arr_airports: Optional[List[str]] = None


@dataclass
class SimResultQuery:
    sim_fids: Optional[List[str]] = None
    routes: Optional[List[str]] = None
    ef_gt: Optional[float] = None
    fl_lte: Optional[float] = None
    model_config_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Task & Result contracts — flow across all slot boundaries
# ---------------------------------------------------------------------------

@dataclass
class SimTask:
    """Universal task struct passed between all slot modules.

    All fields are derived from master_flights columns, plus cluster_id and fl
    which are assigned by Slot 1. Never pass a raw Base_FID string across
    module boundaries — use to_sim_fid() to construct it lazily at write time.
    """
    icao24: str
    callsign: str
    dep: str           # estdepartureairport
    arr: str           # estarrivalairport
    firstseen: int     # epoch seconds UTC — time-shift anchor + day bucketing
    lastseen: int      # epoch seconds UTC — weather window coverage
    typecode: str      # ICAO aircraft type designator — required by PSFlight
    cluster_id: int
    fl: float          # target flight level in feet

    def to_sim_fid(self) -> str:
        """Construct the canonical SIM_FID string from task components."""
        fs_str = pd.Timestamp(self.firstseen, unit="s", tz="UTC").strftime("%Y%m%d")
        return (
            f"{self.icao24}_{self.callsign}_{self.dep}-{self.arr}"
            f"_{fs_str}_{self.cluster_id}_{int(self.fl)}"
        )


@dataclass
class WorkerResult:
    """Result metadata returned by a single worker execution."""
    sim_fid: str
    ef: float
    fl: float
    model_config_id: str
    status: str        # "success" | "fail"


@dataclass
class EvalResult:
    """Produced by Slot 5 after evaluating one completed batch.

    The orchestrator logs from this object and feeds still_todo back into
    the daily queue. Works identically for O1 and O2 — still_todo is always
    empty for O1, populated with step-down SimTasks for O2.
    """
    succeeded: List[WorkerResult] = field(default_factory=list)   # EF <= 0, done
    failed: List[WorkerResult] = field(default_factory=list)      # crash / exception
    still_todo: List[SimTask] = field(default_factory=list)       # O2: re-queue these
