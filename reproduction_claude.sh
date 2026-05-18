#!/usr/bin/env bash
# =============================================================================
# BespokeOLAP Reproduction for JOB Benchmark via Claude Code CLI
#
# This script reproduces BespokeOLAP's 3-stage LLM synthesis pipeline:
#   Stage 1: Storage plan generation   (run_gen_storage_plan.py equivalent)
#   Stage 2: Base implementation       (run_gen_base_impl.py equivalent)
#   Stage 3: Optimization loop         (run_optim_loop.py equivalent)
#            4 sub-stages per query: sample_plan, trace, expert_knowledge, human_reference
#
# LLM backend: Claude Code CLI (`claude -p`) instead of OpenAI Agents SDK
# Benchmark: JOB (Join Order Benchmark) — 33 queries (1a–33a) on IMDB dataset
# Dataset: ~/Project/benchmarks/imdb_job-postgres/
# =============================================================================

set -euo pipefail
cd /home/pei/Project/BespokeOLAP

# =============================================================================
# Prerequisites
# =============================================================================
# - Linux x86-64
# - GCC with C++20 support
# - Apache Arrow and Parquet dev libraries (libarrow-dev, libparquet-dev)
# - Python 3.10+ with: duckdb, pyarrow
# - Claude Code CLI (`claude`) on PATH
# - IMDB CSV files at ~/Project/benchmarks/imdb_job-postgres/csv/ (21 tables, ~3.8GB)
# - IMDB SQL files at ~/Project/benchmarks/imdb_job-postgres/queries/ (113 files)

# =============================================================================
# Step 0: Verify prerequisites
# =============================================================================
echo "=== Step 0: Verify prerequisites ==="

claude --version || { echo "ERROR: claude CLI not found. Install Claude Code first."; exit 1; }

python -c "import duckdb; print(f'DuckDB {duckdb.__version__}')"
python -c "import pyarrow; print(f'PyArrow {pyarrow.__version__}')"

test -d ~/Project/benchmarks/imdb_job-postgres/csv/ || { echo "ERROR: IMDB CSV not found"; exit 1; }
test -d ~/Project/benchmarks/imdb_job-postgres/queries/ || { echo "ERROR: JOB SQL not found"; exit 1; }

echo "All prerequisites OK."

# =============================================================================
# Step 1: Prepare scaled parquet data
# =============================================================================
# BespokeOLAP validates at SF=0.25 and SF=0.5, benchmarks at SF=2.
# CEB reproduction already created imdb.duckdb and sf2 parquet.
# We need sf0.25 and sf0.5 additionally.
echo "=== Step 1: Prepare scaled parquet data ==="

DUCKDB_PATH="benchmark/ceb/imdb.duckdb"
PARQUET_BASE="benchmark/ceb/imdb_parquet"

# Create imdb.duckdb from CSV if not exists (reuse from CEB reproduction)
if [ ! -f "$DUCKDB_PATH" ]; then
    echo "Creating imdb.duckdb from CSV files..."
    python benchmark/ceb/prepare_data.py
fi

# Scale down to SF=0.25
if [ ! -d "$PARQUET_BASE/sf0.25" ]; then
    echo "Generating SF=0.25 parquet..."
    python -m dataset.custom_scaler.scale_parquet \
        --duckdb "$DUCKDB_PATH" \
        --output-dir "$PARQUET_BASE" \
        --scale 0.25
fi

# Scale down to SF=0.5
if [ ! -d "$PARQUET_BASE/sf0.5" ]; then
    echo "Generating SF=0.5 parquet..."
    python -m dataset.custom_scaler.scale_parquet \
        --duckdb "$DUCKDB_PATH" \
        --output-dir "$PARQUET_BASE" \
        --scale 0.5
fi

# Scale up to SF=2 (should already exist from CEB reproduction)
if [ ! -d "$PARQUET_BASE/sf2" ]; then
    echo "Generating SF=2 parquet..."
    python -m dataset.custom_scaler.scale_parquet \
        --duckdb "$DUCKDB_PATH" \
        --output-dir "$PARQUET_BASE" \
        --scale 2
fi

echo "Parquet data ready: sf0.25, sf0.5, sf2"

# =============================================================================
# Step 2: Run full synthesis pipeline
# =============================================================================
# This calls `claude -p` in a scripted loop, mirroring the original pipeline:
#   - Stage 1: Storage plan (creative in-memory layout design)
#   - Stage 2: Base implementation (builder + per-query C++ code + validation)
#   - Stage 3: Optimization (4 stages × 33 queries with regression rollback)
#
# Total estimated time: many hours (33 queries × 4 optimization stages)
# Total estimated cost: significant Claude API usage

echo "=== Step 2: Run synthesis pipeline ==="

# Option A: Full pipeline with storage plan (recommended, matches paper)
python run_synthesis_claude.py \
    --phase all \
    --with-storage-plan \
    --clean

# Option B: Run phases separately (useful for resuming after failures)
# python run_synthesis_claude.py --phase storage --with-storage-plan --clean
# python run_synthesis_claude.py --phase base --with-storage-plan
# python run_synthesis_claude.py --phase optimize --with-storage-plan --resume-from-snapshot base_done

# Option C: Run for a subset of queries first (for testing)
# python run_synthesis_claude.py --queries 1a,2a,3a --phase all --with-storage-plan --clean

# =============================================================================
# Step 3: Verify correctness
# =============================================================================
echo "=== Step 3: Verify correctness ==="

# Check all 33 queries produce correct output at SF=0.25 and SF=0.5
python synthesis/compile_and_run.py check-correctness --sf 0.25
python synthesis/compile_and_run.py check-correctness --sf 0.5

echo "Correctness verification complete."

# =============================================================================
# Step 4: Final benchmark at SF=2
# =============================================================================
echo "=== Step 4: Final benchmark ==="

python synthesis/compile_and_run.py run --sf 2 --optimize

echo "=== Synthesis complete ==="
echo "Workspace: output/"
echo "Snapshots: output/.snapshots/"
