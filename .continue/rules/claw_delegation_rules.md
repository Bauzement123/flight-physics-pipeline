---
title: Claw Code Delegation & Invocation Rules
description: Standardized rules and execution protocols for delegating codebase analysis, auditing, and predrafting tasks to Claw Code (gpt-oss-120b).
---

# Claw Code Delegation & Invocation Rules

To maximize engineering velocity while keeping token consumption economical, the repository operates under a **Tiered Agent Delegation Architecture**. Whenever analyzing large modules, auditing cross-module data flow, or drafting initial implementations, agents must delegate heavy reading and predrafting to Claw Code (`gpt-oss-120b`) following the rules established below.

---

## 1. Role Separation & Tiered Delegation

* **Primary Architect & Orchestrator (Antigravity / Frontier AI)**:
  Responsible for system architecture, complex multi-file planning, high-precision code review, git commit verification, and quality gate enforcement. Avoids burning frontier tokens on raw high-volume reading or initial document extraction.
* **Drafting & Synthesis Engine (Claw Code via `gpt-oss-120b`)**:
  Responsible for heavy codebase scanning, reading long scripts or audit logs, drafting initial markdown pattern catalogs, and generating structured predraft tables.

---

## 2. Batched Prompting & Explicit Absolute Paths

When instructing Claw Code to analyze multiple files or modules:
1. **Batch by Component**: Split large analysis tasks into focused batches (e.g., 5–8 files or 1–3 submodules per prompt) to avoid context drift, reading budget exhaustion, and output truncation.
2. **Explicit Absolute Paths**: Always explicitly list the exact absolute file paths to be inspected inside the prompt file:
   ```text
   Read the following exact Python files using your `read` tool:
   - G:/Meine Ablage/UNI/SS26/PythonPipeline - Kopie/src/core/fetching/opensky_fetcher.py
   - G:/Meine Ablage/UNI/SS26/PythonPipeline - Kopie/src/core/fetching/fetcher_orchestrator.py
   ```
   *Why*: Do not rely on relative paths or workspace globbing (`glob_search`) inside Claw Code when running in read-only mode, as glob lookups without absolute paths can fail or return empty result sets on Windows.

---

## 3. Structured Predraft Return Formats

When delegating analytical or comparison tasks, explicitly instruct Claw Code to return outputs in a **structured predraft format**:
* Demand exact Markdown comparison tables, explicit bulleted option lists, or draft code blocks.
* Include a strict output constraint at the end of the prompt text:
  ```text
  IMPORTANT: Output ONLY valid Markdown. Use clean standard ASCII hyphens `-` and spaces. Do not include terminal spinners or extra chatter.
  ```

---

## 4. PowerShell Execution Protocol & Mojibake Prevention

When invoking `claw.exe` from PowerShell scripts or automated agent background tasks where output is captured or saved to disk, you **MUST** adhere to the following execution protocol:

### 4.1 JSON Output Mode (`--output-format json`)
Always invoke `claw.exe` with `--output-format json` and parse out the `.message` property. Never use `--output-format text` when writing to file, as raw text mode embeds ANSI terminal spinners (`Thinking...`) and tool execution logs directly into the output document.

### 4.2 UTF-8 Console & Pipeline Encoding
In Windows PowerShell 5.1, pipeline captures (`| Out-String`) decode external process output using `[Console]::OutputEncoding` (which defaults to Windows-1252). This corrupts multi-byte UTF-8 Unicode characters (such as non-breaking hyphens `‐`, en-dashes `–`, curly apostrophes `’`, and math symbols `≈`, `≤`) into garbled Mojibake strings (`ÔÇæ`, `Ôëñ`). Always force UTF-8 console encoding before running pipeline captures:
```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
```

### 4.3 Environment Variable Refresh
If API credentials (`OPENAI_API_KEY`, `OPENAI_BASE_URL`) were recently set via `setx`, refresh the session environment variables before executing:
```powershell
foreach($level in "Machine","User") {
    [Environment]::GetEnvironmentVariables($level).GetEnumerator() | ForEach-Object {
        if(![string]::IsNullOrEmpty($_.Value)) { Set-Item -Path "env:$($_.Key)" -Value $_.Value }
    }
}
```

### 4.4 Prompt Piping via `--stdin`
Always pass prompt instructions via input files piped to `--stdin` rather than raw command-line string arguments to prevent escaping and argument truncation errors.

### 4.5 Strict Prohibition Against Standalone `.ps1` Scripts
**Never create temporary or standalone `.ps1` script files** (e.g., `run_claw.ps1`, `audit.ps1`) to run external tools or Claw Code. Executing `.ps1` files frequently fails across Windows environments due to PowerShell Execution Policy restrictions (`Restricted`/`RemoteSigned`), UTF-16LE/BOM encoding corruption when redirecting output to `.ps1` files, and argument quote-stripping across sub-shell boundaries. Always execute PowerShell commands directly inline using the command execution tool. Never wrap commands in `.ps1` files.

---

## 5. Standard Copy-Pasteable Execution Template

Use the following exact PowerShell snippet when running analysis or auditing batches via Claw Code:

```powershell
# 1. Force UTF-8 Encoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# 2. Refresh User/Machine Environment Variables
foreach($level in "Machine","User") {
    [Environment]::GetEnvironmentVariables($level).GetEnumerator() | ForEach-Object {
        if(![string]::IsNullOrEmpty($_.Value)) { Set-Item -Path "env:$($_.Key)" -Value $_.Value }
    }
}

# 3. Execute Claw Code via JSON pipeline
$jsonOutput = Get-Content data\temp\plans\batch_prompt.txt | & "C:\Users\Joshu\AppData\Local\Programs\ClawCode\claw.exe" `
    --model gpt-oss-120b `
    --permission-mode read-only `
    --allowedTools read,glob,grep `
    --output-format json prompt --stdin | Out-String

# 4. Extract clean message and save with UTF-8 encoding
$result = $jsonOutput | ConvertFrom-Json
[IO.File]::WriteAllText("$pwd\data\temp\plans\batch_audit.md", $result.message, [System.Text.Encoding]::UTF8)
```

*(Note: For scratchpad script drafting or code generation tasks requiring file creation, change `--permission-mode read-only` to `--permission-mode workspace-write`).*
