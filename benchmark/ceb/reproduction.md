# CEB Benchmark Reproduction

## Differences from Official README

The official `BespokeOLAP_Artifacts/run.py ceb` runs each query once with no warmup. The official `python -m benchmark` requires git snapshot hashes from a remote cache server.

This reproduction instead:
- Builds the engine directly from `BespokeOLAP_Artifacts/bespoke_ceb/` source files
- Generates scaled data from scratch (no dependency on `/mnt/labstore/` for parquet)
- Measures with 5 warmup + 10 measured runs per query (hyperfine-style)
- Reports median, mean, and stddev
- Measures Bespoke warm (execution only) and cold (compile + execute)
- Measures DuckDB with threads=1 and default parallel

Query generation uses the same shared `Random(42)` state, same `format_args_string` logic, and same source code as the official pipeline.

## Fixes Applied to Official Code

The official `dataset/gen_ceb/gen_ceb_query.py` has a bug in `_extract_literal` that splits IN-list values on commas without respecting single quotes. For example, `'LAB:FotoKem Laboratory, Burbank (CA), USA'` becomes three separate values. This causes DuckDB and Bespoke to execute different queries.

We fix this by adding a `_split_in_list()` function that parses quoted values correctly. The generated SQL and Bespoke args are now consistent.

Additionally, the SQL is post-processed to remove `OR col IS NULL` and bare `NULL` from IN-lists. The Bespoke C++ code ignores NULL in all its string/int filters (`build_string_filter_for_column` skips `<<NULL>>`/`NULL`, `allow_null` stays false). Removing these from SQL ensures DuckDB executes the same logical query.

With these fixes, all 16/16 queries produce identical results between DuckDB and Bespoke.

## Prerequisites

- Linux x86-64
- GCC with C++20 support
- Apache Arrow and Parquet dev libraries (`libarrow-dev`, `libparquet-dev`)
- Python 3.10+
- IMDB CSV files at `~/Project/benchmarks/imdb_job-postgres/csv/`
- CEB pickle files at `/mnt/labstore/bespoke_olap/datasets/ceb/imdb/`

## Setup

```bash
cd /home/pei/Project/BespokeOLAP

# Create venv with DuckDB 1.4.4
python -m venv benchmark/ceb/.venv
benchmark/ceb/.venv/bin/pip install duckdb==1.4.4
```

## Run Everything

```bash
bash benchmark/ceb/run_all.sh
```

This runs all steps below in sequence and produces `benchmark/ceb/results/summary.csv`.

## Step-by-Step

All commands assume you are in `/home/pei/Project/BespokeOLAP` with the venv active:

```bash
source benchmark/ceb/.venv/bin/activate
```

### Step 1: Prepare Data

Creates `imdb.duckdb` from IMDB CSV files.

```bash
python benchmark/ceb/prepare_data.py
```

### Step 2: Scale to SF2

Uses GRACEFUL's upscaler to produce 2x-scaled parquet files.

```bash
python -m dataset.custom_scaler.scale_parquet \
    --duckdb benchmark/ceb/imdb.duckdb \
    --output-dir benchmark/ceb/imdb_parquet \
    --scale 2
```

Output: `benchmark/ceb/imdb_parquet/sf2/{table}.parquet` (21 tables)

### Step 3: Generate Queries

Instantiates 1 query per CEB template with seed=42 using a shared random state (same as official `run.py`).

```bash
python benchmark/ceb/gen_queries.py
```

Output: `benchmark/ceb/sql/*.sql` and `benchmark/ceb/queries_bespoke.txt`

### Step 4: Build Bespoke Engine

Compiles the C++ engine with -O3 -flto.

```bash
bash benchmark/ceb/build_bespoke.sh
```

Output: `benchmark/ceb/build/db`, `libloader.so`, `libbuilder.so`, `libquery.so`

### Step 5: Verify Correctness

Compares Bespoke output against DuckDB golden results. All 16/16 queries should pass.

```bash
python benchmark/ceb/verify_correctness.py
```

### Step 6: Measure DuckDB

5 warmup + 10 measured runs, pinned to core 3.

```bash
python benchmark/ceb/measure_duckdb.py
```

Output: `benchmark/ceb/results/duckdb_threads1.csv`, `benchmark/ceb/results/duckdb_parallel.csv`

### Step 7: Measure Bespoke (Warm)

Execution only, no recompilation. Data and compiled code stay cached.

```bash
python benchmark/ceb/measure_bespoke_warm.py
```

Output: `benchmark/ceb/results/bespoke_warm.csv`

### Step 8: Measure Bespoke (Cold)

Recompiles libbuilder.so and libquery.so before each run. Data stays loaded.

```bash
python benchmark/ceb/measure_bespoke_cold.py
```

Output: `benchmark/ceb/results/bespoke_cold.csv`

### Step 9: Aggregate

Combines all results into a summary table with speedup calculations.

```bash
python benchmark/ceb/aggregate_results.py
```

Output: `benchmark/ceb/results/summary.csv`

## Output Format

`summary.csv` columns:
- `query` — CEB template name (1a, 2a, ..., 11b)
- `duckdb_1thread_ms` — DuckDB median with threads=1
- `duckdb_parallel_ms` — DuckDB median with default threads
- `bespoke_warm_ms` — Bespoke median (execution only)
- `bespoke_cold_ms` — Bespoke median (compile + execute)
- `speedup_vs_duckdb_1t` — duckdb_1thread / bespoke_warm
- `speedup_vs_duckdb_par` — duckdb_parallel / bespoke_warm

## Why Not Hyperfine

DuckDB could use hyperfine (stateless process per invocation). Bespoke cannot because:
- Warm measurement requires a persistent process with data already loaded. Hyperfine starts fresh each time.
- Cold measurement requires data to stay loaded while only .so files are recompiled. The `db` process uses a stateful pipe protocol (P2C/C2P) that cannot be expressed as a single shell command.

## Notes

- Core pinning: DuckDB is pinned to core 3 via `os.sched_setaffinity`. Bespoke pins itself internally via `CpuAffinityGuard` in query_impl.cpp.
- The 16 CEB templates: 1a, 2a, 2b, 2c, 3a, 3b, 4a, 5a, 6a, 7a, 8a, 9a, 9b, 10a, 11a, 11b.
- Cold measurement includes C++ compilation time (~200-400ms) plus execution, amortized across all 16 queries per run.
