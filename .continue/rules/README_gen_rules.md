---
description: README generation and verification standards for the Flight Physics Pipeline
---

# README Generation Rules

These rules define how module READMEs are generated, reviewed, and kept synchronized with the Flight Physics Pipeline source code.

---

## 1. Source-of-Truth Principle

1. Python source code is the source of truth.
2. Existing README files, architecture documents, chat histories, and temporary plans may be stale.
3. Before editing a README, inspect the relevant module code, especially:
   - `argparse` definitions,
   - imports from `src.common.config`,
   - registry reads/writes,
   - cache behavior,
   - logging setup,
   - output path construction,
   - exception and fallback handling.

---

## 2. Required README Structure

Every module README must contain these sections:

1. **Title & Introduction** — state what the module solves and where it sits in the pipeline.
2. **Module Structure** — text tree of directories and files.
3. **Function Analysis Solution Tree (FAST)** — map objectives to functions, scripts, inputs, outputs, and safety behavior.
4. **Data Workflow** — one workflow subsection per distinct CLI path or execution mode.
5. **CLI Usage Guide** — copy-pasteable Bash and PowerShell examples plus parameter tables.
6. **Prerequisites & Dependencies** — libraries, registry files, config constants, and links to `src/conventions.md`.

---

## 3. Function Analysis Solution Tree (FAST)

The FAST tree must be implementation-specific, not generic. It must show:

- high-level module objective,
- sub-objectives,
- implementing function/script names,
- input contracts,
- output contracts,
- cache/registry side effects,
- fallback or skip behavior,
- safety/logging behavior.

Example outline:

```text
Module Objective
 └── Sub-objective
      └── Solution: script.py::function_name()
           ├── Inputs: ...
           ├── Outputs: ...
           ├── Registry/cache effects: ...
           └── Safety behavior: ...
```

---

## 4. Data Workflow Rules

1. Use **one Mermaid flowchart per distinct workflow**.
2. Do not combine unrelated workflows into a single diagram.
3. Each workflow subsection must be numbered and named with the script file:

```markdown
### 4.1 Workflow A — Short Name (`script_name.py`)
```

4. Every Mermaid diagram must show:
   - files read,
   - registries/caches updated,
   - core operations performed,
   - output files saved,
   - failure/skip branches where important.
5. Every Mermaid diagram must be followed immediately by a numbered **Step-by-step** fallback explanation.
6. If Mermaid rendering is unreliable, the step-by-step text must still fully explain the workflow.

---

## 5. CLI Usage Guide Rules

Each README must include separate copy-pasteable examples for:

- Bash,
- PowerShell.

CLI examples must use module execution from the project root:

```bash
python -m src.core.<module>.<script>
```

Do not change meaningful example values, especially curated rank lists, unless the user explicitly requests it.

Each CLI entrypoint must have a parameter table containing:

| Option | Type | Default | Required | Description |
|---|---|---|---|---|

The table must match `argparse` exactly, including:

- flag names,
- aliases,
- defaults,
- `required=True`,
- choices,
- boolean `store_true` / `store_false` behavior,
- path defaults,
- output side effects.

---

## 6. Config Constant and Registry Parity

For every README update:

1. Inspect imports from `src.common.config` in the module.
2. Mention every relevant used config constant by name in the README.
3. Verify registry names against `src/common/config.py`.
4. Do not document stale registry names such as outdated synthesized placeholders.
5. Prefer canonical paths such as:
   - `data/registries/`,
   - `GLOBAL_TRAJECTORY_REGISTRY`,
   - `GLOBAL_CLEAN_REGISTRY`,
   - `GLOBAL_SIMULATION_REGISTRY`,
   - `GLOBAL_CORRIDOR_SIM_REGISTRY`,
   - `CALIBRATION_PLOT_REGISTRY`.

---

## 7. Logging Parity

Each README must state:

- which entrypoint configures logging,
- which log file is written,
- where skipped/failure records go,
- whether failures abort the run or skip a unit of work.

Log file references must match centralized logging policy in `src.common.utils.setup_file_logger()` and `LOGS_DIR` from `src.common.config`.

---

## 8. Performance, Memory, and Fallback Documentation

If a module has any of the following, document them explicitly:

- batch size controls,
- worker/thread count controls,
- low-memory mode,
- vectorized mode,
- sequential fallback after parallel failure,
- cache-hit path,
- corrupted cache handling,
- resume behavior,
- skipped-aircraft behavior,
- retry/backoff behavior.

---

## 9. Stale Reference Audit

Before finalizing README changes, grep for stale names and paths:

```bash
grep -RIn "src/acquisition\|src/fetching\|src/filtering\|src/processing\|src/physics\|src/calibration\|src/synthesis" src --include="README.md"
grep -RIn "data/flight_registry\|global_synthesized\|global_corridor_registry\|global_synthesized_simulation_registry" src --include="README.md"
```

Expected modern namespaces include:

```text
src/core/acquisition/
src/core/fetching/
src/core/filtering/
src/core/processing/
src/core/corridor/
src/core/weather/
src/core/physics/
src/analysis/campaigns/
src/analysis/verification/
```

---

## 10. Review Ordering Rule

Before doing a broad README rewrite, perform a module-wise code review and record code-quality issues in the temporary audit notes. If the review uncovers design problems that should change code behavior, discuss or plan those code changes before freezing the README as the source-of-truth documentation.
