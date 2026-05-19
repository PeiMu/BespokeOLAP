# JOB Benchmark Reproduction via Claude Code CLI

## Overview

Reproduces BespokeOLAP's LLM synthesis pipeline for the Join Order Benchmark (JOB): all 113 queries on the vanilla IMDB dataset, using Claude Code CLI (`claude -p`) as the LLM backend.

The original pipeline (`run_gen_storage_plan.py` -> `run_gen_base_impl.py` -> `run_optim_loop.py`) uses the OpenAI Agents SDK. We replace the LLM backend with `claude -p` while keeping the same prompts, conversation structure, and optimization stages.

## Quick Start

Activate the environment and run the reproduction script:

```bash
source .venv/bin/activate
bash reproduction_claude.sh
```

This runs the full pipeline end-to-end: data preparation, 3-stage LLM synthesis, and evaluation. The rest of this document explains each step in detail.

## Pipeline

| Stage | Original Script | Claude Equivalent |
|-------|----------------|-------------------|
| 1. Storage Plan | `run_gen_storage_plan.py` | `run_synthesis_claude.py --phase storage` |
| 2. Base Implementation | `run_gen_base_impl.py` | `run_synthesis_claude.py --phase base` |
| 3. Optimization Loop | `run_optim_loop.py` | `run_synthesis_claude.py --phase optimize` |

Stage 3 runs 4 sub-stages per query (from `OptimizationConversation._build_stages()`):
1. **sample_plan** — DuckDB EXPLAIN ANALYZE guided
2. **trace** — trace-based profiling (`-DTRACE`), target 10x speedup
3. **expert_knowledge** — 19 optimization principles, target 2x speedup
4. **human_reference** — Thomas Neumann style, target 2x speedup

All prompts are loaded verbatim from `conversations/prompts/*.txt`.

## Data

JOB uses the vanilla IMDB dataset (no scale factors). The CSV files are at `~/Project/benchmarks/imdb_job-postgres/csv/` (21 tables, ~3.8GB). The 113 SQL query files are at `~/Project/benchmarks/imdb_job-postgres/queries/`.

## Prerequisites

- Linux x86-64
- GCC with C++20 support
- `libarrow-dev`, `libparquet-dev`
- Python virtual environment at `.venv/` with `duckdb`
- Claude Code CLI (`claude`) on PATH
- IMDB CSV files at `~/Project/benchmarks/imdb_job-postgres/csv/`
- JOB SQL files at `~/Project/benchmarks/imdb_job-postgres/queries/`

## Step-by-Step Explanation

The following sections explain what `reproduction_claude.sh` does at each step.

### Step 1: Prepare Data

Convert IMDB CSV files to DuckDB and Parquet.

```bash
python benchmark/job/prepare_data.py
```

Output:
- `benchmark/job/imdb.duckdb`
- `benchmark/job/imdb_parquet/*.parquet` (21 tables)

### Step 2: LLM Synthesis

Run the full 3-stage pipeline. Claude Code CLI generates the C++ engine code in `output/`.

```bash
python run_synthesis_claude.py --phase all --with-storage-plan --clean
```

To test with a subset of queries first:

```bash
python run_synthesis_claude.py --queries 1a,1b,1c,1d,2a --phase all --with-storage-plan --clean
```

To run phases separately (useful for resuming):

```bash
# Stage 1: Storage plan
python run_synthesis_claude.py --phase storage --with-storage-plan --clean

# Stage 2: Base implementation
python run_synthesis_claude.py --phase base --with-storage-plan

# Stage 3: Optimization (resume from saved snapshot)
python run_synthesis_claude.py --phase optimize --with-storage-plan --resume-from-snapshot base_done
```

### Step 3: Evaluate

After synthesis completes, evaluate the generated engine against DuckDB.

```bash
bash benchmark/job/run_all.sh
```

This runs:
1. **Build** — compiles the synthesized C++ code from `output/`
2. **Verify correctness** — compares all 113 queries against DuckDB golden results
3. **Measure DuckDB** — single-threaded and parallel, 5 warmup + 10 measured runs
4. **Measure Bespoke warm** — execution only, data pre-loaded
5. **Measure Bespoke cold** — recompile + execute each run
6. **Aggregate** — produces `benchmark/job/results/summary.csv`

Or run steps individually:

```bash
# Build the engine
bash benchmark/job/build_bespoke.sh

# Verify correctness (113 queries)
python benchmark/job/verify_correctness.py

# DuckDB baseline
python benchmark/job/measure_duckdb.py

# Bespoke warm (execution only)
python benchmark/job/measure_bespoke_warm.py

# Bespoke cold (compile + execute)
python benchmark/job/measure_bespoke_cold.py

# Aggregate results
python benchmark/job/aggregate_results.py
```

## Output

`benchmark/job/results/summary.csv` columns:
- `query` — JOB query name (1a, 1b, ..., 33c)
- `duckdb_1thread_ms` — DuckDB median with threads=1
- `duckdb_parallel_ms` — DuckDB median with default threads
- `bespoke_warm_ms` — Bespoke median (execution only)
- `bespoke_cold_ms` — Bespoke median (compile + execute)
- `speedup_vs_duckdb_1t` — duckdb_1thread / bespoke_warm
- `speedup_vs_duckdb_par` — duckdb_parallel / bespoke_warm

## Files

**Synthesis (new):**
- `run_synthesis_claude.py` — main driver, calls `claude -p` in a scripted loop
- `synthesis/compile_and_run.py` — compile/run/validate/snapshot helper for Claude

**Benchmark evaluation (new, in `benchmark/job/`):**
- `prepare_data.py` — CSV -> DuckDB -> Parquet
- `build_bespoke.sh` — compiles synthesized C++ from `output/`
- `verify_correctness.py` — compares output against DuckDB
- `measure_duckdb.py` — DuckDB baseline timing
- `measure_bespoke_warm.py` — Bespoke execution timing
- `measure_bespoke_cold.py` — Bespoke compile + execution timing
- `aggregate_results.py` — combines all results
- `run_all.sh` — runs all evaluation steps

**JOB dataset integration (new):**
- `dataset/gen_job/__init__.py`
- `dataset/gen_job/gen_job_query.py` — loads all 113 JOB SQL files
- `dataset/gen_job/job_queries.py` — dict of query SQL strings

**Modified:**
- `dataset/query_gen_factory.py` — added `"job"` branches
- `dataset/dataset_tables_dict.py` — added `"job"` (same 21 IMDB tables)
- `tools/validate_tool/sf_list_gen.py` — added `"job"` config
- `tools/validate_tool/duckdb_connection_manager.py` — support flat parquet dir (no `sf{sf}/` subdir)
- `utils/gen_common.py` — added JOB query ID parser
- `utils/general_utils.py` — added `"job"` to query file writer

## Differences from CEB Reproduction

| | CEB | JOB |
|---|---|---|
| Queries | 16 (parameterized templates) | 113 (static SQL) |
| Dataset | IMDB scaled to SF=2 | IMDB vanilla (no scaling) |
| Source code | Pre-built in `BespokeOLAP_Artifacts/bespoke_ceb/` | Synthesized by Claude into `output/` |
| LLM | Not used (evaluating existing code) | Claude Code CLI synthesizes the engine |
