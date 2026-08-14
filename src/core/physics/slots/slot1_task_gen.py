"""
slots/slot1_task_gen.py — Slot 1: Task Generation (Deprecated Shim)

This module has been renamed to `src.core.physics.slots.slot1_flightlist_gen`.
Please import `generate_flightlist` from `slot1_flightlist_gen` directly.
"""

from src.core.physics.slots.slot1_flightlist_gen import generate_flightlist, enumerate_cohort, generate_tasks

__all__ = ["generate_flightlist", "enumerate_cohort", "generate_tasks"]
