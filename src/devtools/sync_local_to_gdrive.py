"""
sync_local_to_gdrive.py — Developer Sync Utility
==================================================
Compares the `global_trajectory_registry.parquet` between a LOCAL working copy
of the pipeline and the canonical G: Drive copy, then copies any trajectory
files that exist locally but are missing or older on the G: Drive.

USAGE
-----
1. Set LOCAL_PIPELINE_DIR below to the root of your local pipeline copy.
2. Run:  python src/devtools/sync_local_to_gdrive.py [--dry-run] [--verbose]

WHAT GETS SYNCED
----------------
The script performs TWO operations in order:

  1. Registry diff  — reads both global_trajectory_registry.parquet files
                      into memory to find LOCAL-only flight_ids. Registries
                      are NEVER written or copied — they must be rebuilt by
                      build_global_manifest after the sync.
  2. Trajectory dirs — every route folder under data/trajectories/ whose
                       flight_ids appear in the LOCAL registry but are missing
                       from the G: Drive registry, OR where any individual
                       parquet file is newer locally, OR the folder is entirely
                       absent on the G: Drive.

CORRECT WORKFLOW
----------------
  1. python -m src.common.build_global_manifest --only raw   (on LOCAL machine)
  2. python -m src.devtools.sync_local_to_gdrive --dry-run   (preview)
  3. python -m src.devtools.sync_local_to_gdrive             (apply)
  4. python -m src.common.build_global_manifest --only raw   (on G: Drive)

SAFETY
------
  - Never deletes anything on the G: Drive.
  - Skips files that are already byte-for-byte identical (same size + mtime).
  - Prints a detailed diff summary before any writes are performed.
  - Pass --dry-run to preview all operations without copying a single byte.
"""

from __future__ import annotations

# ============================================================
# ★ USER CONFIGURATION — edit this section before running ★
# ============================================================

LOCAL_PIPELINE_DIR: str = r"C:\Users\Joshu\Projects\flight-physics-pipeline"
"""Root directory of the LOCAL pipeline copy (the source).
Example:  r"C:\\Dev\\PythonPipeline"
"""

# ============================================================
# END USER CONFIGURATION
# ============================================================

# Concat files are excluded from sync: they are derived artefacts that are
# re-generated on each machine from the individual raw/clean parquets.
# Hardcoded intentionally — this script is devtools-only, for code-literate users.
_EXCLUDED_SUFFIXES: tuple[str, ...] = ("_all_raw.parquet", "_all_clean.parquet")

import argparse
import shutil
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _needs_copy(src: Path, dst: Path) -> bool:
    """Return True if src should overwrite dst (newer, larger, or missing)."""
    if not dst.exists():
        return True
    # Same size AND mtime within 2 seconds → already in sync
    src_stat = src.stat()
    dst_stat = dst.stat()
    if src_stat.st_size != dst_stat.st_size:
        return True
    if abs(src_stat.st_mtime - dst_stat.st_mtime) > 2.0:
        return src_stat.st_mtime > dst_stat.st_mtime
    return False


def _copy_file(src: Path, dst: Path, dry_run: bool, verbose: bool) -> int:
    """Copy src → dst, creating parent dirs as needed. Returns bytes copied."""
    if dry_run:
        if verbose:
            print(f"  [DRY-RUN] would copy  {src.name}")
        return src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if verbose:
        print(f"  ✓ copied  {dst.relative_to(dst.anchor)}")
    return src.stat().st_size


def _copy_dir(src: Path, dst: Path, dry_run: bool, verbose: bool) -> tuple[int, int]:
    """Recursively copy every file in src that needs updating in dst.
    Returns (files_copied, bytes_copied)."""
    files, total_bytes = 0, 0
    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        if src_file.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        if _needs_copy(src_file, dst_file):
            total_bytes += _copy_file(src_file, dst_file, dry_run, verbose)
            files += 1
    return files, total_bytes


# ---------------------------------------------------------------------------
# Registry comparison
# ---------------------------------------------------------------------------

def _load_registry(path: Path) -> "pd.DataFrame | None":
    """Load a parquet registry; return None if missing or unreadable."""
    try:
        import pandas as pd
        if path.exists():
            return pd.read_parquet(path)
    except Exception as exc:
        print(f"  [WARN] could not read {path.name}: {exc}")
    return None


def _compare_trajectory_registries(
    local_reg_path: Path,
    gdrive_reg_path: Path,
) -> tuple[set[str], set[str], set[str]]:
    """
    Compare flight_id sets between the two trajectory registries.

    Returns
    -------
    local_only   : flight_ids present locally but absent on G: Drive
    gdrive_only  : flight_ids on G: Drive but absent locally (informational)
    common       : flight_ids present in both
    """
    import pandas as pd

    local_df = _load_registry(local_reg_path)
    gdrive_df = _load_registry(gdrive_reg_path)

    def _ids(df: "pd.DataFrame | None") -> set[str]:
        if df is None or "flight_id" not in df.columns:
            return set()
        return set(df["flight_id"].dropna().astype(str))

    local_ids = _ids(local_df)
    gdrive_ids = _ids(gdrive_df)

    return local_ids - gdrive_ids, gdrive_ids - local_ids, local_ids & gdrive_ids


# ---------------------------------------------------------------------------
# Route-folder discovery
# ---------------------------------------------------------------------------

def _route_folders_for_flight_ids(
    trajectories_dir: Path, flight_ids: set[str]
) -> set[Path]:
    """
    Walk trajectories_dir to find route folders that contain at least one
    parquet file whose stem starts with a flight_id in *flight_ids*.

    Fast path: if a route folder is entirely missing on the destination,
    we flag the whole folder regardless of flight_id matching.
    """
    matched: set[Path] = set()
    if not trajectories_dir.exists():
        return matched

    for route_dir in trajectories_dir.iterdir():
        if not route_dir.is_dir() or route_dir.name == "runs":
            continue
        # Check any parquet inside raw/ or clean/ or at root level
        for pq in route_dir.rglob("*.parquet"):
            stem = pq.stem
            # flight_id typically appears in the filename as a substring
            if any(fid in stem for fid in flight_ids):
                matched.add(route_dir)
                break
    return matched


def _all_route_folders(trajectories_dir: Path) -> list[Path]:
    if not trajectories_dir.exists():
        return []
    return [
        d for d in trajectories_dir.iterdir()
        if d.is_dir() and d.name != "runs"
    ]


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def run_sync(
    local_root: Path,
    gdrive_root: Path,
    dry_run: bool,
    verbose: bool,
) -> None:
    print("\n" + "=" * 70)
    print("  Flight Pipeline — Local → G: Drive Sync Utility")
    print("=" * 70)
    print(f"  SOURCE  : {local_root}")
    print(f"  TARGET  : {gdrive_root}")
    print(f"  MODE    : {'DRY RUN (no files will be written)' if dry_run else 'LIVE COPY'}")
    print("=" * 70 + "\n")

    if not local_root.exists():
        print(f"[ERROR] LOCAL_PIPELINE_DIR does not exist: {local_root}")
        sys.exit(1)
    if not gdrive_root.exists():
        print(f"[ERROR] G: Drive root does not exist: {gdrive_root}")
        sys.exit(1)

    total_files = 0
    total_bytes = 0

    local_reg_dir  = local_root  / "data" / "registries"
    gdrive_reg_dir = gdrive_root / "data" / "registries"

    # ------------------------------------------------------------------
    # Step 1: Compare trajectory registries (READ-ONLY — never written)
    # ------------------------------------------------------------------
    print("─" * 70)
    print("STEP 1 — Trajectory registry diff  (global_trajectory_registry.parquet)")
    print("─" * 70)
    print("  [registries are read into memory only — never copied to G: Drive]")

    local_traj_reg  = local_reg_dir  / "global_trajectory_registry.parquet"
    gdrive_traj_reg = gdrive_reg_dir / "global_trajectory_registry.parquet"

    local_only_ids, gdrive_only_ids, common_ids = _compare_trajectory_registries(
        local_traj_reg, gdrive_traj_reg
    )

    print(f"  LOCAL-only  flight_ids : {len(local_only_ids):>6}")
    print(f"  G-Drive-only flight_ids: {len(gdrive_only_ids):>6}  (informational)")
    print(f"  Common flight_ids      : {len(common_ids):>6}")

    if local_only_ids and verbose:
        preview = sorted(local_only_ids)[:10]
        suffix = f" … (+{len(local_only_ids) - 10} more)" if len(local_only_ids) > 10 else ""
        print(f"\n  Sample LOCAL-only IDs: {', '.join(preview)}{suffix}")

    # ------------------------------------------------------------------
    # Step 2: Copy missing/outdated trajectory route folders
    # ------------------------------------------------------------------
    print()
    print("─" * 70)
    print("STEP 2 — Trajectory route folders  (data/trajectories/)")
    print("─" * 70)

    local_traj_dir = local_root / "data" / "trajectories"
    gdrive_traj_dir = gdrive_root / "data" / "trajectories"

    local_all_routes = _all_route_folders(local_traj_dir)
    gdrive_route_names = {d.name for d in _all_route_folders(gdrive_traj_dir)}

    route_files_copied = 0
    routes_processed = 0

    for route_dir in sorted(local_all_routes):
        dst_route_dir = gdrive_traj_dir / route_dir.name
        is_entirely_missing = route_dir.name not in gdrive_route_names

        # Determine whether this folder is relevant:
        #   a) Entirely absent on G: Drive  → always copy
        #   b) Has flight_ids that are LOCAL-only  → copy updated files
        #   c) Otherwise check file-level mtimes for any file that changed
        should_sync = is_entirely_missing

        if not should_sync and local_only_ids:
            # Quick check: does any parquet in this folder relate to a local-only id?
            for pq in route_dir.rglob("*.parquet"):
                if any(fid in pq.stem for fid in local_only_ids):
                    should_sync = True
                    break

        if not should_sync:
            # Still check mtime on every file for updates (concat files excluded)
            for src_file in route_dir.rglob("*.parquet"):
                if src_file.name.endswith(_EXCLUDED_SUFFIXES):
                    continue
                rel = src_file.relative_to(route_dir)
                dst_file = dst_route_dir / rel
                if _needs_copy(src_file, dst_file):
                    should_sync = True
                    break

        if not should_sync:
            continue

        status = "NEW" if is_entirely_missing else "UPDATE"
        print(f"\n  [{status}] {route_dir.name}")
        f, b = _copy_dir(route_dir, dst_route_dir, dry_run, verbose)
        route_files_copied += f
        total_files += f
        total_bytes += b
        routes_processed += 1

    if routes_processed == 0:
        print("  ✓ All trajectory folders are already up-to-date.")
    else:
        print(
            f"\n  → {routes_processed} route folder(s) processed, "
            f"{route_files_copied} file(s) queued."
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    if dry_run:
        print(
            f"  DRY-RUN COMPLETE — {total_files} file(s) would be copied"
            f"  ({_fmt_bytes(total_bytes)})"
        )
        print("  Run without --dry-run to apply changes.")
    else:
        print(
            f"  SYNC COMPLETE — {total_files} file(s) copied"
            f"  ({_fmt_bytes(total_bytes)})"
        )
    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync local pipeline → G: Drive (trajectory registries + files).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview all operations without writing any files.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each individual file being copied.",
    )
    parser.add_argument(
        "--local-dir",
        type=str,
        default=None,
        help=(
            "Override LOCAL_PIPELINE_DIR from the command line. "
            "Example: --local-dir C:\\Dev\\PythonPipeline"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Resolve local dir (CLI flag overrides the top-level constant)
    local_dir_str = args.local_dir if args.local_dir else LOCAL_PIPELINE_DIR
    local_root = Path(local_dir_str).resolve()

    # G: Drive root is always the canonical project location
    gdrive_root = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie")

    t0 = time.perf_counter()
    run_sync(
        local_root=local_root,
        gdrive_root=gdrive_root,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    elapsed = time.perf_counter() - t0
    print(f"  Elapsed: {elapsed:.1f}s\n")
