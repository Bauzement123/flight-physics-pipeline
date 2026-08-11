"""
Step 0: Audit Common & Config Imports
Scans all Python files under src/ (excluding src/scratchpad/) for imports from
src.common (config, utils, registry_utils, etc.) and records:
1. Aliased imports (e.g. from src.common.config import MASTER_FLIGHTS_FILE as MFF)
2. Wildcard imports (e.g. from src.common.config import *)
3. Direct function/constant imports for alias tracking
"""

import ast
import json
import sys
from pathlib import Path

# Base workspace directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BASE_DIR / "src"
OUTPUT_JSON = BASE_DIR / "data" / "temp" / "plans" / "common_import_aliases.json"

TARGET_MODULES = {
    "src.common.config",
    "src.common.utils",
    "src.common.registry_utils",
    "src.common.adapters",
    "src.common.build_global_manifest",
    "src.core.fetching.helpers",
}

def audit_imports():
    results = {
        "aliased_imports": [],  # list of {file, module, original_name, alias_name}
        "wildcard_imports": [], # list of {file, module}
        "summary": {
            "total_files_scanned": 0,
            "files_with_aliases": 0,
            "files_with_wildcards": 0,
            "total_aliases_found": 0,
        },
        "per_file_alias_map": {} # file_rel_path -> {alias_name: original_name}
    }

    if not SRC_DIR.exists():
        print(f"Error: {SRC_DIR} does not exist.")
        sys.exit(1)

    py_files = [
        p for p in SRC_DIR.rglob("*.py")
        if "scratchpad" not in p.parts
    ]

    results["summary"]["total_files_scanned"] = len(py_files)

    for py_file in sorted(py_files):
        rel_path = py_file.relative_to(BASE_DIR).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except Exception as e:
            print(f"Warning: Could not parse {rel_path}: {e}")
            continue

        file_alias_map = {}
        has_wildcard = False

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod in TARGET_MODULES or any(mod.startswith(tm) for tm in TARGET_MODULES):
                    for alias in node.names:
                        if alias.name == "*":
                            has_wildcard = True
                            results["wildcard_imports"].append({
                                "file": rel_path,
                                "module": mod
                            })
                        elif alias.asname:
                            results["aliased_imports"].append({
                                "file": rel_path,
                                "module": mod,
                                "original_name": alias.name,
                                "alias_name": alias.asname
                            })
                            file_alias_map[alias.asname] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in TARGET_MODULES and alias.asname:
                        results["aliased_imports"].append({
                            "file": rel_path,
                            "module": alias.name,
                            "original_name": alias.name,
                            "alias_name": alias.asname
                        })
                        file_alias_map[alias.asname] = alias.name

        if file_alias_map:
            results["per_file_alias_map"][rel_path] = file_alias_map
            results["summary"]["files_with_aliases"] += 1
            results["summary"]["total_aliases_found"] += len(file_alias_map)

        if has_wildcard:
            results["summary"]["files_with_wildcards"] += 1

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    
    print("=" * 60)
    print("STEP 0: IMPORT AUDIT SUMMARY")
    print("=" * 60)
    print(f"Total Python files scanned: {results['summary']['total_files_scanned']}")
    print(f"Files with wildcard imports (from X import *): {results['summary']['files_with_wildcards']}")
    print(f"Files with aliased imports (from X import Y as Z): {results['summary']['files_with_aliases']}")
    print(f"Total aliased symbols found: {results['summary']['total_aliases_found']}")
    print(f"\nDetailed JSON results saved to: {OUTPUT_JSON}")
    print("=" * 60)

    return results

if __name__ == "__main__":
    audit_imports()
