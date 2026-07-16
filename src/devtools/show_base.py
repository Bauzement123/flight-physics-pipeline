#!/usr/bin/env python3
"""Print the resolved path representing the repository root."""
from pathlib import Path

if __name__ == "__main__":
    print(Path(__file__).resolve().parent.parent.parent)
