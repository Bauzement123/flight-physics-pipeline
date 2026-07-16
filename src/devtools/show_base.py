#!/usr/bin/env python3
"""Utility: print resolved BASE_DIR from src.common.config"""
from pathlib import Path
import sys

def main() -> None:
    # Ensure the repository root (parent of 'src') is on sys.path so `import src` works.
    repo_root = Path(__file__).resolve().parent.parent.parent
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    try:
        from src.common import config
    except Exception as exc:
        print("Failed to import src.common.config:", exc)
        # Fallback: import by file path
        config_path = repo_root / "src" / "common" / "config.py"
        if config_path.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("dev_config", str(config_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                print(mod.BASE_DIR)
                return
            except Exception as exc2:
                print("Fallback import failed:", exc2)
                return
        return

    print(config.BASE_DIR)


if __name__ == "__main__":
    main()
