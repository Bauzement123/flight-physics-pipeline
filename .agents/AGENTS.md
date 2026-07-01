# Agent Guidelines and Project Rules

This document defines workspace-scoped guidelines and rules for agents working on the Flight Physics Pipeline codebase.

---

## 1. Module README Standards & Structure

Every module in the codebase must have a detailed `README.md` that serves as a highly precise, technical source of truth. The document must maintain a high level of detail and adhere to the following structure:

1. **Title & Introduction**: Summarize the system module's purpose.
2. **Module Structure**: A text-based tree diagram of the directory and its files.
3. **Function Analysis Solution Tree (FAST)**: A hierarchy mapping high-level module objectives to their code implementations, highlighting input/output contracts and safety/fallback behaviors.
4. **Data Workflow**:
   * Include a visual Mermaid flowchart showing files read, cache databases updated, core operations, and outputs saved.
   * **Mandatory Step-by-Step Description**: Directly below every Mermaid flowchart, provide a detailed, numbered, text-based walkthrough of the workflow. This serves as a fallback for non-rendering environments and details logical transitions.
   * Include sub-sections detailing performance profiles (e.g., **Optimization & Memory Modes** such as Standard vs. Low-Memory) and metric/progress logging formats.
5. **CLI Usage Guide**:
   * Provide separate copy-pasteable syntax blocks for **Bash** and **PowerShell**.
   * Include detailed **Parameter Reference** tables listing each command-line option, its type, its default value, and its description.
6. **Prerequisites & Dependencies**: List library dependencies and specific registry files referenced by the module, linking to the global `conventions.md` file.

---

## 2. Code-README Analysis & Verification Workflow

When examining a module, updating its documentation, or reviewing changes, agents must follow this systematic verification workflow:

1. **Trace Configuration Constants**:
   * Compare all file path constructions and registries in the module with [config.py](file:///g:/Meine%20Ablage/UNI/SS26/PythonPipeline%20-%20Kopie/src/common/config.py).
   * Ensure the README references correct, standardized registry files (e.g., `global_corridor_simulation_registry.parquet` rather than outdated placeholder names like `global_synthesized_simulation_registry.parquet`).
2. **Scan for Dead/Unused Code**:
   * Inspect module scripts for unused imported constants (e.g., `GLOBAL_MODEL_REGISTRY`) and variables defined but never referenced (e.g., local registry file definitions).
   * Remove unused variables and imports to keep scripts clean and maintainable.
3. **Audit CLI Argument Defaults**:
   * Inspect the `argparse` setups in `simulation.py`, `clone_simulation.py`, and other entrypoints.
   * Verify that the defaults, requirements, and allowed options in the code match the README's parameter tables exactly (e.g., matching standard directories like `data/results/corridor_simulations`).
4. **Inspect Weather, Coordinate, and timezone logic**:
   * Check logic for time zones (aware vs. naive UTC conversions) and geographic coordinate projections (WGS84 lat/lon bounding boxes vs. local cartesian coordinate projections) to ensure descriptions match the code implementation and `conventions.md`.
5. **Verify Vectorized vs. Sequential Fallbacks**:
   * Verify batch size partitioning, thread-safety, and exception handling logic in parallel executors. Ensure documented warnings, sequential fallback loops, and error logs (e.g., `skipped_aircraft.log`) match the runtime execution flow.
