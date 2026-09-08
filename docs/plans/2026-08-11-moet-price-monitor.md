# MOET Essential Goods Price Monitor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Windows-ready Python 3.12 monitor that checks the MOET source version hourly and appends only initial, new, changed, or removed product-branch price events to CSV.

**Architecture:** `MoetClient` owns HTTP retries, timestamp parsing, pagination, and schema validation. `HistoryStore` owns snapshot identity, append-only event generation, UTF-8 BOM CSV output, state, and crash recovery; `PriceMonitor` coordinates one run. Windows Task Scheduler calls the one-shot CLI once per hour.

**Tech Stack:** Python 3.12, standard library (`csv`, `json`, `tomllib`, `logging`, `pathlib`, `zoneinfo`), `requests`, `pytest`, PowerShell ScheduledTasks.

---

### Task 1: Configuration contract

**Files:**
- Modify: `core/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_project_contract.py`

1. Add failing tests for TOML loading, path resolution, valid defaults, and rejected page sizes/timeouts/log levels.
2. Run `python -m pytest tests/test_project_contract.py -q`; expect failures from the TODO stub.
3. Implement immutable `Settings` and `load_settings(Path)` using `tomllib`; resolve `data_dir` relative to the config file.
4. Run the same test; expect pass.

### Task 2: Source client

**Files:**
- Modify: `core/client.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_client.py`

1. Add mocked-response tests for `Last Updated`, retry success/exhaustion, 5,000-row pagination boundaries, total-count mismatch, duplicate identity, and invalid prices.
2. Run `python -m pytest tests/test_client.py -q`; expect failures.
3. Implement a `requests.Session` with a descriptive User-Agent, bounded timeout tuple, three-attempt exponential retry, an initial `totalCount` probe followed by one `page=1&pageSize=totalCount` snapshot, Decimal-backed price normalization, and exact count validation. The single-response strategy avoids the live endpoint's unstable ordering across ordinary pages.
4. Run `python -m pytest tests/test_client.py -q`; expect pass.

### Task 3: History and recoverable commit

**Files:**
- Modify: `core/history.py`
- Modify: `tests/test_history.py`

1. Add failing tests for initial/new/changed/removed events, identical-snapshot idempotency, BOM CSV headers, UAE ISO timestamps, duplicate keys, interrupted staging, and restart recovery.
2. Run `python -m pytest tests/test_history.py -q`; expect failures.
3. Implement JSON state with full prior records, deterministic fingerprints, staged replacement CSV/state files, a transaction manifest, and startup recovery. Rewrite the history CSV through a staged file so CSV and state can be reconciled after interruption.
4. Run `python -m pytest tests/test_history.py -q`; expect pass.

### Task 4: One-run service and logging

**Files:**
- Modify: `core/service.py`
- Modify: `tests/test_service.py`

1. Add failing tests proving unchanged versions never call the full endpoint, changed versions commit once, and failures never advance state.
2. Run `python -m pytest tests/test_service.py -q`; expect failures.
3. Implement `RunResult` and `PriceMonitor.run_once()` with recovery first, source-version comparison, full fetch only on change, one collection timestamp, diff, commit, and structured log messages.
4. Run `python -m pytest tests/test_service.py -q`; expect pass.

### Task 5: CLI and delivery files

**Files:**
- Modify: `run_monitor.py`
- Modify: `core/__init__.py`
- Modify: `requirements.txt`
- Modify: `README.md`
- Test: `tests/test_project_contract.py`

1. Add tests for `--config`, success exit code 0, failure exit code 1, package import, dependency declarations, and required README commands.
2. Run `python -m pytest tests/test_project_contract.py -q`; expect failures.
3. Implement CLI dependency construction, rotating file logs plus console logs, actionable exception reporting, package version, pinned compatible dependencies, and Chinese setup/usage/troubleshooting documentation.
4. Run the same test; expect pass.

### Task 6: Windows scheduled task

**Files:**
- Modify: `install_task.ps1`
- Modify: `tests/test_install_task.py`

1. Add a contract test for the absolute `.venv\\Scripts\\python.exe` command, working directory, hourly repetition, `IgnoreNew` overlap policy, and `-WhatIf` support.
2. Run `python -m pytest tests/test_install_task.py -q`; expect failure.
3. Implement idempotent scheduled-task registration using `ScheduledTasks` cmdlets and `SupportsShouldProcess`; refuse to overwrite an unrelated existing task without an explicit force switch.
4. Run the same test and PowerShell parser validation; expect pass.

### Task 7: Full automated and live verification

**Files:**
- Runtime outputs: `data/prices_history.csv`, `data/state.json`, `logs/monitor.log`

1. Create `.venv` with `py -3.12 -m venv .venv` (fall back only to the verified Python 3.12 executable), install `requirements.txt`, and run `python -m pytest -q`; expect all tests pass.
2. Copy `config.example.toml` to `config.toml` only if absent, then run `.venv\\Scripts\\python.exe run_monitor.py --config config.toml`; expect a complete initial snapshot and exit code 0.
3. Run the command again; expect no full fetch, no extra CSV rows, and exit code 0.
4. Verify CSV BOM, headers, row count, unique `(product_id, branch_id)` initial keys, source timestamp, UAE collection timestamps, state count, and log evidence.

### Task 8: Install and verify hourly automation

**Files:**
- External Windows state: Task Scheduler task `MOET Essential Goods Price Monitor`

1. Query the task name first; if absent, run `powershell -ExecutionPolicy Bypass -File .\\install_task.ps1`.
2. Inspect the registered task action, trigger, repetition interval, working directory, and multiple-instance policy; expect one-hour repetition and `IgnoreNew`.
3. Start the task once, wait for completion, and verify last result `0` plus unchanged-data log evidence.
4. Run the final test suite and inspect the project for TODO stubs, accidental generated files, secrets, and unapproved scope.

**Repository note:** The target directory is not a Git worktree, so commit steps are intentionally omitted.
