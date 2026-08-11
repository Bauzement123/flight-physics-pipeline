"""
Static Trace Generator Devtool — Multi-File Full src/ Resolution

Traces all repository-owned (src/) function calls recursively from a root entrypoint file,
crossing file boundaries automatically. Stops strictly at external library boundaries.

Resolution rule:
  - Any call resolving to a function in src/ (imported OR locally defined in the same file)
    is always recursed into. No depth limit — only a visited set prevents infinite loops.
  - Any call resolving outside src/ is tagged [EXT] and not expanded.
  - A visited set (filepath, func_name) prevents infinite loops on circular calls.

Usage:
    python -m src.devtools.static_trace_generator --file src/core/corridor/corridor_clustering_cli.py
    python -m src.devtools.static_trace_generator --file src/core/physics/clone_simulation.py --hide-ext
"""

from __future__ import annotations

import ast
import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Known disk I/O call name fragments -> category
_IO_READ_FRAGMENTS = {"read_parquet", "read_csv", "read_json", "read_pickle", "read_feather", "open"}
_IO_CHECK_FRAGMENTS = {"exists", "is_file", "is_dir"}
_IO_WRITE_FRAGMENTS = {
    "to_parquet", "to_csv", "to_pickle", "to_feather", "mkdir",
    "write_text", "write_bytes", "log_skipped_aircraft",
}


def _classify_io(call_name: str) -> Optional[str]:
    base = call_name.split(".")[-1]
    if any(p in base for p in _IO_READ_FRAGMENTS):
        return "READ"
    if any(p in base for p in _IO_CHECK_FRAGMENTS):
        return "CHECK"
    if any(p in base for p in _IO_WRITE_FRAGMENTS):
        return "WRITE"
    return None


def _get_call_name(node: ast.AST) -> str:
    """Recursively extract a dotted call name from a Call/Attribute/Name AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _get_call_name(node.value)
        return f"{owner}.{node.attr}"
    if isinstance(node, ast.Call):
        return _get_call_name(node.func)
    return "<complex>"


def _resolve_imports(tree: ast.AST) -> Dict[str, Tuple[Path, str]]:
    """
    Build a map: local_symbol_name -> (source_file_Path, original_symbol_name)
    for all `from src.x.y import z` statements in the file's AST.
    Star imports are skipped.
    """
    mapping: Dict[str, Tuple[Path, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("src."):
            continue
        parts = node.module.split(".")
        mod_rel = Path(*parts)
        candidates = [
            BASE_DIR / (str(mod_rel) + ".py"),
            BASE_DIR / mod_rel / "__init__.py",
        ]
        resolved = next((c for c in candidates if c.exists()), None)
        if resolved is None:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            mapping[local_name] = (resolved, alias.name)
    return mapping


def _find_local_funcs(tree: ast.AST) -> Dict[str, ast.FunctionDef]:
    """Map function names -> FunctionDef nodes for all functions defined in the file."""
    funcs: Dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node  # type: ignore[assignment]
    return funcs


def _extract_direct_calls(stmt: ast.stmt) -> List[ast.Call]:
    """
    Extract Call nodes from the DIRECT expression parts of a statement ONLY.
    Does NOT recurse into compound statement bodies (if/for/while/with/try bodies).
    Prevents double-processing when _walk_stmts explicitly recurses into those bodies.
    """
    sources: List[ast.expr] = []
    if isinstance(stmt, ast.Expr):
        sources = [stmt.value]
    elif isinstance(stmt, ast.Assign):
        sources = [stmt.value] if stmt.value else []
    elif isinstance(stmt, ast.AugAssign):
        sources = [stmt.value]
    elif isinstance(stmt, ast.AnnAssign):
        sources = [stmt.value] if stmt.value else []
    elif isinstance(stmt, ast.Return):
        sources = [stmt.value] if stmt.value else []
    elif isinstance(stmt, ast.Delete):
        sources = list(stmt.targets)  # type: ignore[assignment]
    elif isinstance(stmt, ast.Assert):
        sources = [stmt.test] + ([stmt.msg] if stmt.msg else [])
    elif isinstance(stmt, ast.Raise):
        sources = [stmt.exc] if stmt.exc else []  # type: ignore[list-item]
    elif isinstance(stmt, ast.If):
        sources = [stmt.test]        # condition only — body handled by _walk_stmts
    elif isinstance(stmt, ast.For):
        sources = [stmt.iter]        # iterator only
    elif isinstance(stmt, ast.While):
        sources = [stmt.test]        # condition only
    elif isinstance(stmt, ast.With):
        sources = [item.context_expr for item in stmt.items]  # context managers only
    # ast.Try, Import, Pass, Break, Continue, etc. contribute no direct calls

    calls: List[ast.Call] = []
    for src in sources:
        if src is not None:
            for node in ast.walk(src):
                if isinstance(node, ast.Call):
                    calls.append(node)
    return sorted(calls, key=lambda n: getattr(n, "lineno", 0))


class StaticTraceAnalyzer:
    """
    Recursively traces repository function calls across src/ files using AST analysis.
    Stops at external (non-src/) boundaries. Uses a visited set to prevent loops.
    """

    def __init__(self, root_file: Path, hide_ext: bool = False) -> None:
        self.root_file = root_file.resolve()
        self.hide_ext = hide_ext
        self.visited: Set[Tuple[str, str]] = set()
        self.output_lines: List[str] = []
        self.io_ops: List[Dict[str, Any]] = []
        self.config_constants: Set[str] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyze(self) -> str:
        self._analyze_file(self.root_file, func_name=None, indent=0)
        return self._format_output()

    # ------------------------------------------------------------------
    # Core file / function analysis
    # ------------------------------------------------------------------

    def _analyze_file(self, filepath: Path, func_name: Optional[str], indent: int) -> None:
        """Parse filepath and trace either a specific function or the module top-level."""
        if not filepath.exists():
            self.output_lines.append(f"{' ' * (indent * 2)}[MISSING FILE: {filepath}]")
            return

        try:
            code = filepath.read_text(encoding="utf-8")
            tree = ast.parse(code, filename=str(filepath))
        except SyntaxError as exc:
            self.output_lines.append(f"{' ' * (indent * 2)}[PARSE ERROR: {filepath.name} — {exc}]")
            return

        imports = _resolve_imports(tree)
        local_funcs = _find_local_funcs(tree)
        rel = self._rel(filepath)

        if func_name is None:
            # Module-level: walk the module body directly
            self.output_lines.append(f"{'  ' * indent}=== {rel} [module] ===")
            self._walk_stmts(tree.body, "<module>", filepath, imports, local_funcs, indent + 1)
        else:
            # Find the target function definition anywhere in the file
            target = local_funcs.get(func_name)
            if target is None:
                self.output_lines.append(f"{'  ' * indent}[NOT FOUND: {func_name} in {rel}]")
                return
            self.output_lines.append(f"{'  ' * indent}--- {rel}::{func_name} ---")
            self._walk_stmts(target.body, func_name, filepath, imports, local_funcs, indent + 1)


    # ------------------------------------------------------------------
    # Statement-order AST walker
    # ------------------------------------------------------------------

    def _walk_stmts(
        self,
        stmts: List[ast.stmt],
        current_fn: str,
        filepath: Path,
        imports: Dict[str, Tuple[Path, str]],
        local_funcs: Dict[str, ast.FunctionDef],
        indent: int,
    ) -> None:
        """Walk a list of AST statements in source order, recording calls."""
        for stmt in stmts:
            # Nested function definitions: skip inline (traced lazily when called)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Record calls in direct expressions of this statement only (no sub-body recursion)
            self._process_stmt(stmt, current_fn, filepath, imports, local_funcs, indent)

            # Recurse into compound statement bodies to preserve execution order
            if isinstance(stmt, ast.If):
                self._walk_stmts(stmt.body, current_fn, filepath, imports, local_funcs, indent)
                if stmt.orelse:
                    self._walk_stmts(stmt.orelse, current_fn, filepath, imports, local_funcs, indent)
            elif isinstance(stmt, (ast.For, ast.While)):
                self._walk_stmts(stmt.body, current_fn, filepath, imports, local_funcs, indent)
                if getattr(stmt, "orelse", None):
                    self._walk_stmts(stmt.orelse, current_fn, filepath, imports, local_funcs, indent)
            elif isinstance(stmt, ast.With):
                self._walk_stmts(stmt.body, current_fn, filepath, imports, local_funcs, indent)
            elif isinstance(stmt, ast.Try):
                self._walk_stmts(stmt.body, current_fn, filepath, imports, local_funcs, indent)
                for handler in stmt.handlers:
                    self._walk_stmts(handler.body, current_fn, filepath, imports, local_funcs, indent)
                if stmt.orelse:
                    self._walk_stmts(stmt.orelse, current_fn, filepath, imports, local_funcs, indent)
                if stmt.finalbody:
                    self._walk_stmts(stmt.finalbody, current_fn, filepath, imports, local_funcs, indent)

    def _get_callback_names(self, call: ast.Call) -> List[str]:
        call_name = _get_call_name(call.func)
        if call_name.endswith((".submit", ".apply_async")) and call.args:
            return [_get_call_name(call.args[0])]
        if call_name.endswith((".map", ".imap", ".imap_unordered", ".starmap", ".starmap_async")) and call.args:
            return [_get_call_name(call.args[0])]
        return []

    def _process_stmt(
        self,
        stmt: ast.stmt,
        current_fn: str,
        filepath: Path,
        imports: Dict[str, Tuple[Path, str]],
        local_funcs: Dict[str, ast.FunctionDef],
        indent: int,
    ) -> None:
        """Emit calls from direct expressions of one statement (no sub-body double-processing)."""
        calls = _extract_direct_calls(stmt)

        # Sweep the full stmt subtree for uppercase config constant references
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id.isupper() and len(node.id) > 2:
                self.config_constants.add(node.id)

        seen_in_stmt: Set[str] = set()
        prefix = "  " * indent
        rel = self._rel(filepath)

        calls_to_process: List[Tuple[ast.Call, str, bool]] = []
        for call in calls:
            calls_to_process.append((call, _get_call_name(call.func), False))
            for cb_name in self._get_callback_names(call):
                calls_to_process.append((call, cb_name, True))

        for call, call_name, is_callback in calls_to_process:
            if call_name in seen_in_stmt:
                continue
            seen_in_stmt.add(call_name)

            lineno = getattr(call, "lineno", 0)
            base = call_name.split(".")[0]

            # Record I/O operations
            io_type = _classify_io(call_name)
            if io_type:
                self.io_ops.append({
                    "file": rel,
                    "function": current_fn,
                    "line": lineno,
                    "call": call_name,
                    "type": io_type,
                    "indent": indent,
                })

            cb_tag = "[CALLBACK] " if is_callback else ""

            # --- Resolution priority ---
            # 1. Cross-file src/ import
            if base in imports:
                target_file, target_symbol = imports[base]
                self.output_lines.append(
                    f"{prefix}[SRC] {cb_tag}{rel}::{current_fn} -> {call_name} (L{lineno})"
                )
                key = (str(target_file), target_symbol)
                if key not in self.visited:
                    self.visited.add(key)
                    self._analyze_file(target_file, func_name=target_symbol, indent=indent + 1)
                else:
                    self.output_lines.append(
                        f"{prefix}  [ALREADY TRACED — {target_symbol} in {target_file.name}]"
                    )

            # 2. Locally-defined function in the same file
            elif base in local_funcs:
                self.output_lines.append(
                    f"{prefix}[SRC] {cb_tag}{rel}::{current_fn} -> {call_name} (L{lineno})"
                )
                key = (str(filepath), base)
                if key not in self.visited:
                    self.visited.add(key)
                    self._analyze_file(filepath, func_name=base, indent=indent + 1)
                else:
                    self.output_lines.append(
                        f"{prefix}  [ALREADY TRACED — {base} in {filepath.name}]"
                    )

            # 3. External boundary
            else:
                if not self.hide_ext:
                    self.output_lines.append(
                        f"{prefix}[EXT] {rel}::{current_fn} -> {call_name} (L{lineno})"
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rel(self, filepath: Path) -> str:
        try:
            return str(filepath.relative_to(BASE_DIR))
        except ValueError:
            return filepath.name

    def _format_output(self) -> str:
        root_rel = self._rel(self.root_file)
        lines = [
            f"# Static Trace: `{root_rel}`",
            f"_External calls shown: {'yes (use --hide-ext to suppress)' if not self.hide_ext else 'hidden'}_",
            "",
            "## Disk I/O Operations",
        ]
        if not self.io_ops:
            lines.append("  None detected.")
        else:
            for op in self.io_ops:
                p = "  " * (op["indent"] + 1)
                lines.append(
                    f"{p}* L{op['line']} [{op['file']}::{op['function']}] **{op['type']}** `{op['call']}`"
                )

        lines += [
            "",
            "## Config Constants Referenced",
            "  " + (", ".join(f"`{c}`" for c in sorted(self.config_constants)) or "None"),
            "",
            "## Call Trace",
            "",
            "```text"
        ]
        lines.extend(self.output_lines)
        lines.append("```")
        return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Multi-File Static AST Trace Generator — full src/ resolution"
    )
    parser.add_argument("--file", required=True, help="Root Python file to trace from")
    parser.add_argument(
        "--hide-ext",
        action="store_true",
        help="Hide external [EXT] calls (show only [SRC] repository calls)",
    )
    parser.add_argument(
        "--output",
        help="Output file path. Defaults to data/traces/static_trace_<stem>.md",
    )
    args = parser.parse_args()

    root_p = Path(args.file)
    if not root_p.exists():
        print(f"Error: {root_p} not found.")
    else:
        analyzer = StaticTraceAnalyzer(root_p, hide_ext=args.hide_ext)
        result = analyzer.analyze()
        print(result)

        out_p = (
            Path(args.output)
            if args.output
            else Path("data/traces") / f"static_trace_{root_p.stem}.md"
        )
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(result, encoding="utf-8")
        print(f"\n[Saved to {out_p}]")
