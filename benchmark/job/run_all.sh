#!/bin/bash
# JOB Benchmark - Full Evaluation Pipeline (post-synthesis)
# Usage: bash benchmark/job/run_all.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJ_ROOT"

echo "=== JOB Benchmark Evaluation ==="
echo ""

# Step 1: Data Preparation
if [ ! -f "$SCRIPT_DIR/imdb_parquet/title.parquet" ]; then
    echo "--- Step 1: Prepare Data ---"
    python benchmark/job/prepare_data.py
    echo ""
else
    echo "--- Data already prepared (parquet exists) ---"
fi

# Step 2: Build Bespoke (from synthesized code in output/)
if [ ! -f "$SCRIPT_DIR/build/db" ]; then
    echo "--- Step 2: Build Bespoke Engine ---"
    bash benchmark/job/build_bespoke.sh
    echo ""
else
    echo "--- Bespoke already built ---"
fi

# Step 3: Correctness Verification
echo "--- Step 3: Verify Correctness ---"
python benchmark/job/verify_correctness.py
echo ""

# Step 4: DuckDB Performance
echo "--- Step 4: Measure DuckDB ---"
python benchmark/job/measure_duckdb.py
echo ""

# Step 5: Bespoke Warm Performance
echo "--- Step 5: Measure Bespoke (Warm) ---"
python benchmark/job/measure_bespoke_warm.py
echo ""

# Step 6: Bespoke Cold Performance
echo "--- Step 6: Measure Bespoke (Cold) ---"
python benchmark/job/measure_bespoke_cold.py
echo ""

# Step 7: Aggregate
echo "--- Step 7: Aggregate Results ---"
python benchmark/job/aggregate_results.py
echo ""

echo "=== Done ==="
echo "Results: benchmark/job/results/summary.csv"
