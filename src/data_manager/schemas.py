from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import pandas as pd
import pyarrow as pa


# ---------------------------------------------------------------------------
# Simulation Trajectory Delta Lake Schema Contracts
# ---------------------------------------------------------------------------

# Canonical schema for the 14 fixed metadata columns written across all trajectory waypoints
SIM_LAKE_METADATA_SCHEMA = pa.schema([
    pa.field("SIM_FID",          pa.string()),       # Primary simulation ID
    pa.field("model_config_id",  pa.string()),       # Model config (e.g. 'kerosene')
    pa.field("fuel",             pa.string()),       # Fuel type ('kerosene' | 'hydrogen')
    pa.field("route",            pa.string()),       # Route key ('LIRF-EGKK')
    pa.field("icao24",           pa.string()),       # Aircraft 24-bit hex
    pa.field("callsign",         pa.string()),       # Sanitized callsign
    pa.field("typecode",         pa.string()),       # ICAO aircraft type
    pa.field("cluster_id",       pa.int32()),        # Medoid cluster ID
    pa.field("FL",               pa.float64()),      # Target flight level (ft)
    pa.field("dep_date",         pa.int32()),        # Departure date int (YYYYMMDD)
    pa.field("firstseen",        pa.timestamp("ns")),# Trajectory start time (tz-naive UTC)
    pa.field("lastseen",         pa.timestamp("ns")),# Trajectory end time (tz-naive UTC)
    pa.field("EF_total",         pa.float64()),      # Total Energy Forcing (J)
    pa.field("total_fuel_burn",  pa.float64()),      # Mission fuel burn (kg)
])

# Complete list of mandatory metadata columns for validation
SIM_LAKE_FIXED_COLUMNS = [f.name for f in SIM_LAKE_METADATA_SCHEMA]

# String column subset that requires explicit python str coercion for PyArrow kernel safety
SIM_LAKE_STR_COLUMNS = [
    f.name for f in SIM_LAKE_METADATA_SCHEMA if pa.types.is_string(f.type)
]


@dataclass
class CorridorCluster:
    """Canonical corridor cluster metadata for physics simulation.

    Stores the resolved absolute path to the cluster parquet file and its
    calibrated flight level (FL) read directly from the corridor model registry.
    """
    path: Path
    fl: float


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
    ranks: Optional[List[int]] = None
    dep_airports: Optional[List[str]] = None
    arr_airports: Optional[List[str]] = None
    min_distance_km: Optional[float] = None


@dataclass
class SimResultQuery:
    sim_fids: Optional[List[str]] = None
    routes: Optional[List[str]] = None
    ef_gt: Optional[float] = None
    fl_lte: Optional[float] = None
    model_config_id: Optional[str] = None
    fuel: Optional[str] = None


# ---------------------------------------------------------------------------
# Task & Result contracts — flow across all slot boundaries
# ---------------------------------------------------------------------------

@dataclass
class FlightCandidate:
    """Intermediate base task carrying flight metadata and available valid clusters."""
    icao24: str
    callsign: str
    dep: str
    arr: str
    firstseen: int
    lastseen: int
    typecode: str
    valid_cluster_ids: List[int]

    @property
    def route_key(self) -> str:
        return f"{self.dep}-{self.arr}"


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
        import re
        fs_dt = pd.Timestamp(self.firstseen, unit="s", tz="UTC")
        fs_str = fs_dt.strftime("%Y%m%d_%H%M")
        clean_cs = re.sub(r"[^A-Z0-9]", "", (self.callsign or "").upper())
        return (
            f"{self.icao24}_{clean_cs}_{self.dep}-{self.arr}"
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
    actual_fl: Optional[float] = None


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
