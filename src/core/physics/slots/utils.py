"""
slots/utils.py — Backward-compatibility shim.

Task mutation and step-down generation are owned directly by Slot 2
(src.core.physics.slots.slot2_batcher).
"""

from src.core.physics.slots.slot2_batcher import compute_stepdown_task

__all__ = ["compute_stepdown_task"]
