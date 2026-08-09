"""
Migration tool to rename legacy rank_NNN_DEP-ARR trajectory folders to DEP-ARR format.
"""

import argparse
import logging
import re
import shutil
from pathlib import Path
from typing import Tuple

from src.common.config import TRAJECTORIES_DIR
from src.common.utils import extract_route_id_from_path, setup_file_logger

logger = logging.getLogger(__name__)


def patch_json_content(content_str: str, route_id: str) -> str:
    """
    Replaces any rank_NNN_ROUTE segment with ROUTE in JSON text.
    """
    pattern = r"rank_\d+_(" + re.escape(route_id) + r")"
    return re.sub(pattern, r"\1", content_str)


def patch_manifest_file(manifest_path: Path, route_id: str, dry_run: bool = False) -> bool:
    """
    Patches source_list in manifest JSON file by replacing rank_NNN_ROUTE with ROUTE.
    Returns True if content was modified.
    """
    if not manifest_path.exists():
        return False
    try:
        content = manifest_path.read_text(encoding="utf-8")
        patched = patch_json_content(content, route_id)
        if content != patched:
            if not dry_run:
                manifest_path.write_text(patched, encoding="utf-8")
            return True
    except Exception as e:
        logger.error(f"Failed to patch manifest file {manifest_path}: {e}")
    return False


def patch_runs_files(runs_dir: Path, route_id: str, dry_run: bool = False) -> int:
    """
    Patches concat_path in all runs/*.json files.
    Returns count of patched files.
    """
    if not runs_dir.exists() or not runs_dir.is_dir():
        return 0
    patched_count = 0
    for run_file in runs_dir.glob("*.json"):
        try:
            content = run_file.read_text(encoding="utf-8")
            patched = patch_json_content(content, route_id)
            if content != patched:
                if not dry_run:
                    run_file.write_text(patched, encoding="utf-8")
                patched_count += 1
        except Exception as e:
            logger.error(f"Failed to patch run file {run_file}: {e}")
    return patched_count


def merge_trajectory_folders(
    source_dir: Path,
    target_dir: Path,
    route_id: str,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """
    Merges contents of source_dir into existing target_dir.
    Returns tuple of (moved_count, skipped_count, patched_count).
    """
    moved_count = 0
    skipped_count = 0
    patched_count = 0

    for item in list(source_dir.iterdir()):
        if item.is_dir():
            target_subdir = target_dir / item.name
            if not dry_run:
                target_subdir.mkdir(parents=True, exist_ok=True)

            for child in list(item.iterdir()):
                if child.is_file():
                    target_child = target_subdir / child.name
                    if target_child.exists():
                        skipped_count += 1
                        logger.warning(
                            f"Skipping {child.name} — already exists at {target_child}"
                        )
                    else:
                        if item.name == "runs" and child.suffix == ".json":
                            content = child.read_text(encoding="utf-8")
                            patched = patch_json_content(content, route_id)
                            if not dry_run:
                                target_child.write_text(patched, encoding="utf-8")
                                child.unlink()
                            if content != patched:
                                patched_count += 1
                        else:
                            if not dry_run:
                                shutil.move(str(child), str(target_child))
                        moved_count += 1
                        logger.info(f"Moved {child.name} -> {target_subdir}")

        elif item.is_file():
            new_filename = item.name
            if item.name.startswith(source_dir.name):
                new_filename = route_id + item.name[len(source_dir.name) :]

            target_file = target_dir / new_filename
            if target_file.exists():
                skipped_count += 1
                logger.warning(
                    f"Skipping {item.name} — already exists at {target_file}"
                )
            else:
                if "manifest" in item.name.lower() and item.suffix == ".json":
                    content = item.read_text(encoding="utf-8")
                    patched = patch_json_content(content, route_id)
                    if not dry_run:
                        target_file.write_text(patched, encoding="utf-8")
                        item.unlink()
                    if content != patched:
                        patched_count += 1
                else:
                    if not dry_run:
                        shutil.move(str(item), str(target_file))
                moved_count += 1
                logger.info(f"Moved {item.name} -> {target_file}")

    if not dry_run:
        try:
            shutil.rmtree(source_dir)
            logger.info(f"Removed source directory {source_dir.name} after merge")
        except OSError as e:
            logger.error(f"Failed to remove source directory {source_dir}: {e}")

    return moved_count, skipped_count, patched_count


def rename_trajectory_folder(
    source_dir: Path,
    target_dir: Path,
    route_id: str,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """
    Renames source_dir to target_dir when target_dir does not exist,
    renames internal files, and patches JSON manifests/runs.
    Returns tuple of (moved_count, skipped_count, patched_count).
    """
    if dry_run:
        file_count = sum(1 for p in source_dir.rglob("*") if p.is_file())
        return file_count, 0, 1

    source_name = source_dir.name
    logger.info(f"Renaming folder {source_name} -> {target_dir.name}")
    source_dir.rename(target_dir)

    moved_count = 0
    patched_count = 0

    for item in list(target_dir.iterdir()):
        if item.is_file() and item.name.startswith(source_name):
            new_filename = route_id + item.name[len(source_name) :]
            new_path = target_dir / new_filename
            item.rename(new_path)
            moved_count += 1
            logger.info(f"Renamed file {item.name} -> {new_filename}")

    manifest_path = target_dir / f"{route_id}_manifest.json"
    if manifest_path.exists():
        if patch_manifest_file(manifest_path, route_id, dry_run=False):
            patched_count += 1
            logger.info(f"Patched manifest JSON {manifest_path.name}")

    runs_dir = target_dir / "runs"
    if runs_dir.exists():
        p_count = patch_runs_files(runs_dir, route_id, dry_run=False)
        patched_count += p_count
        if p_count > 0:
            logger.info(f"Patched {p_count} run JSON files in {runs_dir}")

    return moved_count, 0, patched_count


def process_trajectories(trajectories_dir: Path, commit: bool = False) -> None:
    """Scans and renames legacy rank_NNN_DEP-ARR folders."""
    if not trajectories_dir.exists():
        logger.error(f"Trajectories directory does not exist: {trajectories_dir}")
        return

    rank_folders = sorted(
        [
            d
            for d in trajectories_dir.iterdir()
            if d.is_dir() and d.name.startswith("rank_")
        ]
    )

    if not rank_folders:
        msg = (
            "[COMMIT] No legacy rank_ folders found to process."
            if commit
            else "[DRY-RUN] No legacy rank_ folders found to process."
        )
        print(msg)
        return

    dry_run = not commit
    prefix = "[COMMIT]" if commit else "[DRY-RUN]"

    if dry_run:
        print(f"{prefix} Trajectory Rank Rename Plan (Dry Run)")
        print(f"{prefix} {'=' * 85}")
        print(
            f"{prefix} {'Source Folder':<30} {'Target Folder':<20} {'Action':<10} {'Status':<15}"
        )
        print(f"{prefix} {'-' * 85}")

    renamed_folders = 0
    merged_folders = 0
    error_folders = 0
    total_files_moved = 0

    for source_dir in rank_folders:
        try:
            route_suffix = source_dir.name.split("_")[-1]
            validated_route = extract_route_id_from_path(source_dir)

            if validated_route == "UNKNOWN" or validated_route != route_suffix:
                logger.warning(
                    f"Skipping invalid rank folder: {source_dir.name} (validated: {validated_route})"
                )
                if dry_run:
                    print(
                        f"{prefix} {source_dir.name:<30} {'N/A':<20} {'SKIP':<10} {'Invalid Route':<15}"
                    )
                continue

            target_dir = trajectories_dir / route_suffix
            action = "MERGE" if target_dir.exists() else "RENAME"

            if dry_run:
                print(
                    f"{prefix} {source_dir.name:<30} {target_dir.name:<20} {action:<10} {'Would execute':<15}"
                )
                if action == "MERGE":
                    merged_folders += 1
                else:
                    renamed_folders += 1
            else:
                if target_dir.exists():
                    m_count, s_count, p_count = merge_trajectory_folders(
                        source_dir, target_dir, route_suffix, dry_run=False
                    )
                    merged_folders += 1
                    total_files_moved += m_count
                    logger.info(
                        f"Merged {source_dir.name} into {target_dir.name} ({m_count} files moved, {s_count} skipped)"
                    )
                else:
                    m_count, s_count, p_count = rename_trajectory_folder(
                        source_dir, target_dir, route_suffix, dry_run=False
                    )
                    renamed_folders += 1
                    total_files_moved += m_count
                    logger.info(f"Renamed {source_dir.name} -> {target_dir.name}")

        except OSError as e:
            error_folders += 1
            logger.error(f"OSError processing {source_dir.name}: {e}")
            if dry_run:
                print(
                    f"{prefix} {source_dir.name:<30} {'ERROR':<20} {'FAIL':<10} {str(e):<15}"
                )

    if dry_run:
        print(f"{prefix} {'=' * 85}")
        print(
            f"{prefix} Total folders to process: {len(rank_folders)} ({renamed_folders} RENAME, {merged_folders} MERGE, {error_folders} ERROR)"
        )
        print(f"{prefix} Run with --commit to execute these changes.")
    else:
        print(f"{prefix} Trajectory Rank Rename Execution Complete")
        print(
            f"{prefix} Folders processed: {len(rank_folders)} (Renamed: {renamed_folders}, Merged: {merged_folders}, Errors: {error_folders})"
        )
        print(f"{prefix} Files moved/renamed: {total_files_moved}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename legacy rank_NNN_DEP-ARR trajectory folders to DEP-ARR format."
    )
    parser.add_argument(
        "--commit", action="store_true", help="Execute the rename (default is dry-run)"
    )
    parser.add_argument(
        "--trajectories-dir",
        type=Path,
        default=None,
        help="Override TRAJECTORIES_DIR from config",
    )
    args = parser.parse_args()

    traj_dir = args.trajectories_dir if args.trajectories_dir else TRAJECTORIES_DIR
    process_trajectories(traj_dir, commit=args.commit)


if __name__ == "__main__":
    setup_file_logger(log_filename="manifest.log")
    main()
