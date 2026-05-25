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
# Benchmark: JOB (Join Order Benchmark) — all 113 queries on vanilla IMDB dataset
# Dataset: ~/Project/benchmarks/imdb_job-postgres/
# =============================================================================

set -euo pipefail
cd /home/pei/Project/BespokeOLAP

# Activate Python environment (has duckdb, pyarrow, etc.)
source /home/pei/Project/BespokeOLAP/.venv/bin/activate

# =============================================================================
# Prerequisites
# =============================================================================
# - Linux x86-64
# - GCC with C++20 support
# - Apache Arrow and Parquet dev libraries (libarrow-dev, libparquet-dev)
# - Python 3.10+ with: duckdb, pyarrow
# - Claude Code CLI (`claude`) on PATH
# - IMDB CSV files at ~/Project/benchmarks/imdb_job-postgres/csv/ (21 tables, ~3.8GB)
# - JOB SQL files at ~/Project/benchmarks/imdb_job-postgres/queries/ (113 files)

# =============================================================================
# Step 0: Verify prerequisites
# =============================================================================
echo "=== Step 0: Verify prerequisites ==="

claude --version || { echo "ERROR: claude CLI not found. Install Claude Code first."; exit 1; }

python -c "import duckdb; print(f'DuckDB {duckdb.__version__}')"

test -d ~/Project/benchmarks/imdb_job-postgres/csv/ || { echo "ERROR: IMDB CSV not found"; exit 1; }
test -d ~/Project/benchmarks/imdb_job-postgres/queries/ || { echo "ERROR: JOB SQL not found"; exit 1; }

echo "All prerequisites OK."

# =============================================================================
# Step 1: Prepare data (CSV -> DuckDB -> Parquet)
# =============================================================================
# JOB uses the vanilla IMDB dataset (no scale factors).
echo "=== Step 1: Prepare data ==="

if [ ! -f "benchmark/job/imdb_parquet/title.parquet" ]; then
    echo "Converting IMDB CSV to Parquet..."
    python benchmark/job/prepare_data.py
else
    echo "Parquet data already exists."
fi

echo "Data ready at benchmark/job/imdb_parquet/"

# =============================================================================
# Step 2: Run full synthesis pipeline
# =============================================================================
# This calls `claude -p` in a scripted loop, mirroring the original pipeline:
#   - Stage 1: Storage plan (creative in-memory layout design)
#   - Stage 2: Base implementation (builder + per-query C++ code + validation)
#   - Stage 3: Optimization (4 stages x 113 queries with regression rollback)
#
# Total estimated time: many hours (113 queries x 4 optimization stages)
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

## Option C: Run for a subset of queries first (for testing)
#python run_synthesis_claude.py --queries 16b,7c,10c,8c,6f,31c,25a,30c --phase all --with-storage-plan --clean

# =============================================================================
# Step 3: Evaluate (build, verify, benchmark)
# =============================================================================
echo "=== Step 3: Evaluate ==="

bash benchmark/job/run_all.sh

echo "=== Reproduction complete ==="
echo "Workspace: output/"
echo "Snapshots: output/.snapshots/"
echo "Results: benchmark/job/results/summary.csv"
