# Tracing and Auditing Tools

This directory contains advanced code analysis, tracing, and auditing tools designed to map out execution flow, trace disk I/O, and inspect multiprocess execution within the Flight Physics Pipeline.

By default, the outputs of these tools are saved to `data/traces/` (or `data/temp/plans/` for the registry dataflow).

## Tools Overview

### 1. `runtime_tracer.py` (Dynamic Analyzer)
A high-fidelity runtime tracer that hooks directly into the Python interpreter's C-level evaluation loop (`sys.settrace`). 
- **Features**: Generates exact, chronological execution trees showing exactly what code ran and what files were touched. It automatically instruments `ProcessPoolExecutor` via monkey-patching during `spawn`/`fork`, allowing it to perfectly trace multi-worker physical simulations across process boundaries without modifying pipeline code.
- **Usage**: 
  ```python
  from src.devtools.tracing.runtime_tracer import start_tracing, stop_tracing
  start_tracing()
  main()
  stop_tracing()
  ```

### 2. `static_trace_generator.py` (Static AST Analyzer)
An Abstract Syntax Tree (AST) parser that analyzes a target Python script and recursively follows all repository function calls to build a Call Trace and Disk I/O Ledger *without running the code*.
- **Features**: Safely inspects dangerous or slow code paths without side-effects. It has specialized logic to recognize multiprocessing callbacks (`pool.submit`, `pool.map`) to cross static concurrency boundaries. Note that it cannot detect custom callbacks or Pandas `.apply()` calls.
- **Usage**:
  ```powershell
  python -m src.devtools.tracing.static_trace_generator --file src/core/physics/clone_simulation.py --hide-ext
  ```

#### Spotting and Resolving Static Gaps
Because static AST parsing cannot resolve dynamic types, execution paths sometimes "disappear" from the trace. You can usually spot these gaps by looking for:
1. **Pandas `.apply()` or `.map()` boundaries:** If you see `[EXT] ... -> <complex>.apply`, any custom Python function you passed into it will be missing from the tree.
2. **Custom Callbacks:** If you see a function call that takes an argument like `on_complete=my_func`, the execution of `my_func` will be missing.
3. **Missing Output I/O:** If a trace claims to map a pipeline module but the `Disk I/O Ledger` shows no parquet files being written, it is almost guaranteed the output logic is hiding inside an untraced callback.

**How to resolve:** When you spot one of these gaps, you can manually trace the missing function by passing it directly to the static tracer via the `--func` flag to generate a supplementary trace:
```powershell
python -m src.devtools.tracing.static_trace_generator --file src/core/physics/engine.py --func my_custom_callback
```

### 3. `trace_registry_dataflow.py` (Dataflow Mapper)
An automated AST scanner that traverses the entire repository to find every place a parquet file, registry, or I/O boundary is touched (`read_parquet`, `to_parquet`, etc.).
- **Features**: Traces the call chains upwards to the module orchestrator and generates a reverse ASCII I/O tree, proving exactly which scripts own which I/O boundaries.

### 4. `audit_common_imports.py` (Import Standardizer)
A dependency auditor that scans the repository for `src.common` imports to check for standard alias enforcement and wildcard usage. Used internally by the Dataflow Mapper to resolve constant references across files.
