"""
Step 1 & Step 2: Automated AST Registry I/O & Call-Graph Tracer
Scans all Python files under src/ (excluding src/scratchpad/), tracks imports, aliases,
parameter defaults, direct I/O calls (read_parquet, to_parquet, update_global_registry, etc.),
and traces upward call chains to orchestrators.

Generates the reverse ASCII I/O tree in registry_io_tree.md.
"""

import ast
import json
import sys
from pathlib import Path

# Base workspace directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BASE_DIR / "src"
ALIAS_JSON = BASE_DIR / "data" / "temp" / "plans" / "common_import_aliases.json"
OUTPUT_MD = BASE_DIR / "data" / "temp" / "plans" / "registry_io_tree.md"

# Target state constants and their canonical descriptions
TARGET_CONSTANTS = {
    "MASTER_FLIGHTS_FILE": "data/databases/master_flights/master_flights.parquet",
    "ROUTE_SUMMARY_PARQUET": "data/databases/master_flights/master_flights_route_summary.parquet",
    "ROUTE_SUMMARY_PKL": "data/databases/master_flights/master_flights_route_summary.pkl",
    "ROUTE_SUMMARY_CSV": "data/databases/master_flights/master_flights_route_summary.csv",
    "GLOBAL_TRAJECTORY_REGISTRY": "data/registries/global_trajectory_registry.parquet",
    "GLOBAL_CLEAN_REGISTRY": "data/registries/global_clean_registry.parquet",
    "GLOBAL_CLEAN_QUALITY_REGISTRY": "data/registries/global_clean_quality_registry.parquet",
    "GLOBAL_RAW_QUALITY_REGISTRY": "data/registries/global_raw_quality_registry.parquet",
    "GLOBAL_SIMULATION_REGISTRY": "data/registries/global_simulation_registry.parquet",
    "GLOBAL_CORRIDOR_MODEL_REGISTRY": "data/registries/global_model_registry.parquet",
    "GLOBAL_CORRIDOR_SIM_REGISTRY": "data/registries/global_corridor_simulation_registry.parquet",
    "GLOBAL_STABILITY_REGISTRY": "data/registries/global_stability_registry.parquet",
    "GLOBAL_FLIGHT_CLUSTER_MAP": "data/registries/global_flight_cluster_map.parquet",
    "GLOBAL_EKF_DIAG_REGISTRY": "data/registries/global_ekf_diag_registry.parquet",
    "CALIBRATION_FLIGHT_CLUSTER_MAP": "data/calibration/calibration_flight_cluster_map.parquet",
    "CALIBRATION_PLOT_REGISTRY": "data/registries/calibration_plot_registry.parquet",
    "AUDIT_CANDIDATE_POOL_REGISTRY": "data/calibration/phase_quality/registries/audit_candidate_pool.parquet",
    "AUDIT_COHORT_MAP_REGISTRY": "data/calibration/phase_quality/registries/audit_cohort_map.parquet",
}

# Known helper IO functions and their default target constant if unspecified
READ_HELPERS = {
    "read_parquet": None,
    "read_csv": None,
    "load": None,
    "load_trajectory_registry": "GLOBAL_TRAJECTORY_REGISTRY",
    "load_stability_registry": "GLOBAL_STABILITY_REGISTRY",
    "load_model_registry": "GLOBAL_CORRIDOR_MODEL_REGISTRY",
    "load_clean_cohort": "GLOBAL_CLEAN_REGISTRY",
    "load_raw_cohort": "GLOBAL_TRAJECTORY_REGISTRY",
    "join_flight_registries": None,
    "load_diagnostic_manifest": "GLOBAL_EKF_DIAG_REGISTRY",
    "get_route_summary": "ROUTE_SUMMARY_PARQUET",
    "get_stability_medoids": "GLOBAL_STABILITY_REGISTRY",
    "get_stability_record": "GLOBAL_STABILITY_REGISTRY",
    "get_oracle_cluster_mapping": "GLOBAL_FLIGHT_CLUSTER_MAP",
    "get_flight_trajectories": "GLOBAL_TRAJECTORY_REGISTRY",
    "load_master_flights": "MASTER_FLIGHTS_FILE",
}

WRITE_HELPERS = {
    "to_parquet": None,
    "to_csv": None,
    "dump": None,
    "update_global_registry": None,
    "write_parquet_atomic": None,
    "write_parquet_atomic_PyOpenSky": None,
    "save_model_registry": "GLOBAL_CORRIDOR_MODEL_REGISTRY",
    "save_stability_registry": "GLOBAL_STABILITY_REGISTRY",
    "update_stability_record": "GLOBAL_STABILITY_REGISTRY",
    "batch_update_stability_registry": "GLOBAL_STABILITY_REGISTRY",
    "register_corridors": "GLOBAL_CORRIDOR_MODEL_REGISTRY",
    "batch_register_corridors": "GLOBAL_CORRIDOR_MODEL_REGISTRY",
    "batch_register_flight_cluster_map": "GLOBAL_FLIGHT_CLUSTER_MAP",
    "register_calibration_plot": "CALIBRATION_PLOT_REGISTRY",
    "export_summaries": "ROUTE_SUMMARY_PARQUET",
}


class ScopeVisitor(ast.NodeVisitor):
    """AST Visitor that tracks enclosing functions, local variable aliases, and I/O calls."""
    def __init__(self, rel_path, content, imported_aliases=None):
        self.rel_path = rel_path
        self.content = content
        self.lines = content.splitlines()
        self.hits = []
        self.current_func = "[module-level]"
        self.local_aliases = dict(imported_aliases) if imported_aliases else {}
        self.module_aliases = {} # e.g. cfg -> src.common.config
        self.func_param_defaults = {}

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.asname:
                self.local_aliases[alias.asname] = alias.name
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.module_aliases[alias.asname] = alias.name
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        old_func = self.current_func
        self.current_func = node.name
        
        # Track function parameter default values
        defaults = node.args.defaults
        num_params = len(node.args.args)
        num_defaults = len(defaults)
        first_default_idx = num_params - num_defaults
        
        param_defaults = {}
        for i, default in enumerate(defaults):
            param_name = node.args.args[first_default_idx + i].arg
            res = self.resolve_constant(default)
            if res:
                param_defaults[param_name] = res

        self.func_param_defaults[node.name] = param_defaults
        self.generic_visit(node)
        self.current_func = old_func

    # Bug 4 Fix: Visit async functions identical to standard function defs
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        # Track local variable assignments like `reg = GLOBAL_TRAJECTORY_REGISTRY` or `reg = MFF`
        res = self.resolve_constant(node.value)
        if res:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.local_aliases[target.id] = res
        self.generic_visit(node)

    def resolve_constant(self, node):
        """Resolves an AST expression node to a target constant string if matched."""
        if isinstance(node, ast.Name):
            name = node.id
            if name in TARGET_CONSTANTS:
                return name
            if name in self.local_aliases:
                resolved = self.local_aliases[name]
                if resolved in TARGET_CONSTANTS:
                    return resolved
                return resolved
        elif isinstance(node, ast.Attribute):
            if node.attr in TARGET_CONSTANTS:
                return node.attr
            if isinstance(node.value, ast.Name):
                # e.g., cfg.MASTER_FLIGHTS_FILE
                mod_name = self.module_aliases.get(node.value.id, node.value.id)
                if node.attr in TARGET_CONSTANTS:
                    return node.attr
        elif isinstance(node, ast.Call):
            # Check for path helpers like Path(MASTER_FLIGHTS_FILE)
            for arg in node.args:
                res = self.resolve_constant(arg)
                if res:
                    return res
        return None

    def visit_Call(self, node):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = self.local_aliases.get(node.func.id, node.func.id)
        elif isinstance(node.func, ast.Attribute):
            func_name = self.local_aliases.get(node.func.attr, node.func.attr)

        if func_name:
            matched_constant = None
            direction = None
            helper_type = None

            # Bug 1 Fix: Strictly check READ vs WRITE helpers only (no CONSUME noise)
            if func_name in READ_HELPERS:
                direction = "READ"
                helper_type = READ_HELPERS[func_name]
            elif func_name in WRITE_HELPERS:
                direction = "WRITE"
                helper_type = WRITE_HELPERS[func_name]

            if direction:
                # 1. Try resolving explicit arguments passed to the call
                for arg in node.args:
                    matched_constant = self.resolve_constant(arg)
                    if matched_constant:
                        break

                if not matched_constant and node.keywords:
                    for kw in node.keywords:
                        matched_constant = self.resolve_constant(kw.value)
                        if matched_constant:
                            break

                # 2. Fall back to helper default constant if unspecified
                if not matched_constant and helper_type:
                    matched_constant = helper_type

                if matched_constant:
                    line_num = node.lineno
                    snippet = self.lines[line_num - 1].strip() if line_num <= len(self.lines) else ""
                    self.hits.append({
                        "file": self.rel_path,
                        "line_num": line_num,
                        "call_func": func_name,
                        "call_expr": snippet,
                        "enclosing_function": self.current_func,
                        "target_constant": matched_constant,
                        "direction": direction,
                    })

        self.generic_visit(node)


def build_caller_map(py_files):
    """
    Bug 3 Fix: Scans all Python files to map (callee_name, callee_file) -> set of (caller_file, caller_func).
    Prevents cross-file main() collisions.
    """
    func_callers = {} # (callee_name, callee_file) -> set of (caller_file, caller_func)
    
    # Map function names to their defining file(s)
    func_definitions = {} # func_name -> set of file_paths
    for py_file in py_files:
        rel_path = py_file.relative_to(BASE_DIR).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name not in func_definitions:
                        func_definitions[node.name] = set()
                    func_definitions[node.name].add(rel_path)
        except Exception:
            continue

    for py_file in py_files:
        rel_path = py_file.relative_to(BASE_DIR).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                caller = node.name
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        callee = None
                        if isinstance(child.func, ast.Name):
                            callee = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            callee = child.func.attr
                        
                        if callee:
                            # Resolve target file: same file first, or imported file
                            target_files = func_definitions.get(callee, set())
                            if rel_path in target_files:
                                key = (callee, rel_path)
                                if key not in func_callers:
                                    func_callers[key] = set()
                                func_callers[key].add((rel_path, caller))
                            else:
                                for tf in target_files:
                                    # Cross-file call (exclude main collisions)
                                    if callee != "main":
                                        key = (callee, tf)
                                        if key not in func_callers:
                                            func_callers[key] = set()
                                        func_callers[key].add((rel_path, caller))
    return func_callers


def build_main_block_callers(py_files):
    """
    Bug B Fix: Detects functions called directly from `if __name__ == "__main__":` blocks.
    Returns a set of (file, func_name) pairs that are direct CLI entrypoints even without
    a wrapping main() function.
    """
    main_block_callers = set()
    for py_file in py_files:
        rel_path = py_file.relative_to(BASE_DIR).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                is_main_block = (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                    and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == "__main__"
                )
                if is_main_block:
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            callee = None
                            if isinstance(child.func, ast.Name):
                                callee = child.func.id
                            elif isinstance(child.func, ast.Attribute):
                                callee = child.func.attr
                            if callee:
                                main_block_callers.add((rel_path, callee))
    return main_block_callers


def trace_upward_chain(file, func_name, func_callers, main_block_callers=None, visited=None):
    """
    Traces caller functions upward to orchestrator entrypoints.
    Handles terminal nodes correctly when enclosing function is main() or is
    called directly from an `if __name__ == "__main__":` block.
    """
    # Terminal: named entrypoint functions
    if func_name in ("main", "run_pipeline", "run_campaign", "run_orchestrator"):
        return [f"{func_name}() \u2014 {file} [CLI Entrypoint]"]
    if func_name == "[module-level]":
        return [f"{file} (module-level execution)"]

    if visited is None:
        visited = set()
    chain = []
    key = (func_name, file)
    if key in visited:
        return chain
    visited.add(key)

    callers = func_callers.get(key, set())
    for caller_file, caller_func in sorted(callers):  # sorted for deterministic output
        if caller_func in ("main", "run_pipeline", "run_campaign", "run_orchestrator"):
            chain.append(f"{caller_func}() \u2014 {caller_file} [CLI Entrypoint]")
        elif main_block_callers and (caller_file, caller_func) in main_block_callers:
            # Catch __main__-block callers at this level to avoid self-referential duplication
            chain.append(f"{caller_func}() \u2014 {caller_file} [CLI Entrypoint via __main__]")
        else:
            sub = trace_upward_chain(caller_file, caller_func, func_callers, main_block_callers, visited)
            if sub:
                for item in sub:
                    chain.append(f"{caller_func}() \u2014 {caller_file} -> {item}")
            else:
                chain.append(f"{caller_func}() \u2014 {caller_file}")
    return chain


def check_symlink_safety(file_path):
    """Audits devtool files for symlink safety (to_registry_path vs Path.resolve)."""
    try:
        content = (BASE_DIR / file_path).read_text(encoding="utf-8")
        if "to_registry_path" in content or "os.path.relpath" in content:
            return "[SYMLINK-SAFE]"
        elif ".resolve()" in content:
            return "[SYMLINK-UNSAFE]"
    except Exception:
        pass
    return ""


def run_tracer():
    # Bug 2 Fix: Load Step 0 alias JSON
    step0_alias_map = {}
    if ALIAS_JSON.exists():
        try:
            alias_data = json.loads(ALIAS_JSON.read_text(encoding="utf-8"))
            step0_alias_map = alias_data.get("per_file_alias_map", {})
            print(f"Loaded Step 0 alias map for {len(step0_alias_map)} files.")
        except Exception as e:
            print(f"Warning: Could not load Step 0 alias JSON: {e}")

    py_files = [
        p for p in SRC_DIR.rglob("*.py")
        if "scratchpad" not in p.parts
    ]

    all_hits = []
    for py_file in py_files:
        rel_path = py_file.relative_to(BASE_DIR).as_posix()
        imported_aliases = step0_alias_map.get(rel_path, {})
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
            visitor = ScopeVisitor(rel_path, content, imported_aliases=imported_aliases)
            visitor.visit(tree)
            all_hits.extend(visitor.hits)
        except Exception as e:
            print(f"Warning: Failed to parse {rel_path}: {e}")

    func_callers = build_caller_map(py_files)
    main_block_callers = build_main_block_callers(py_files)
    print(f"Detected {len(main_block_callers)} functions called directly from __main__ blocks.")

    # Group hits by target constant
    grouped = {const: [] for const in TARGET_CONSTANTS}
    for hit in all_hits:
        const = hit["target_constant"]
        if const in grouped:
            grouped[const].append(hit)

    # Format Markdown document
    lines = []
    lines.append("# Pipeline Registry & Data File I/O Architecture — Reverse ASCII Tree")
    lines.append("")
    lines.append("Automated static analysis trace mapping every physical I/O call across `src/` (excluding `scratchpad/`) to its enclosing function, callers, and orchestrators.")
    lines.append("")

    sec_idx = 1
    for const, path in TARGET_CONSTANTS.items():
        hits = grouped[const]
        lines.append(f"# Section {sec_idx}: `{const}` (`{path}`)")
        lines.append("")
        if not hits:
            lines.append("*No direct I/O read/write calls detected across `src/`.*")
            lines.append("")
            sec_idx += 1
            continue

        # Group hits by direction (READ vs WRITE)
        read_hits = [h for h in hits if h["direction"] == "READ"]
        write_hits = [h for h in hits if h["direction"] == "WRITE"]

        sub_idx = 1
        for direction, dir_hits in [("READ", read_hits), ("WRITE", write_hits)]:
            if not dir_hits:
                continue

            lines.append(f"## {sec_idx}.{sub_idx} [{direction}]")
            lines.append("```text")

            for mech_idx, h in enumerate(dir_hits, start=1):
                file = h["file"]
                dev_tag = ""
                if file.startswith("src/devtools/"):
                    dev_tag = f" [DEVTOOL] {check_symlink_safety(file)}"

                lines.append(f"[call_mechanism_{mech_idx}] {h['call_expr']}")
                lines.append(f"in {file} [LOC:{h['line_num']}]{dev_tag}")
                lines.append(f"└── [Enclosing Function] {h['enclosing_function']}() — {file}")

                chain = trace_upward_chain(file, h["enclosing_function"], func_callers, main_block_callers)
                if chain:
                    for caller in chain:
                        lines.append(f"    └── [Caller] {caller}")
                else:
                    lines.append(f"    └── [Orchestrator] {file} (no external callers found)")

                if mech_idx < len(dir_hits):
                    lines.append("")

            lines.append("```")
            lines.append("")
            sub_idx += 1

        sec_idx += 1

    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("=" * 60)
    print("STEP 1 & 2 AST TRACER COMPLETED")
    print("=" * 60)
    print(f"Total I/O hits extracted: {len(all_hits)}")
    print(f"Reverse ASCII Tree written to: {OUTPUT_MD}")
    print("=" * 60)

    return all_hits

if __name__ == "__main__":
    run_tracer()