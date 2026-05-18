#!/bin/bash
# CEB Benchmark Reproduction - Full Pipeline
# Usage: bash benchmark/ceb/run_all.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV="$SCRIPT_DIR/.venv/bin/python"

cd "$PROJ_ROOT"

echo "=== CEB Benchmark Reproduction ==="
echo "DuckDB version: $($VENV -c 'import duckdb; print(duckdb.__version__)')"
echo ""

# Step 1: Data Preparation (skip if already done)
if [ ! -f "$SCRIPT_DIR/imdb_parquet/sf2/title.parquet" ]; then
    echo "--- Step 1: Prepare Data ---"
    if [ ! -f "$SCRIPT_DIR/imdb.duckdb" ]; then
        $VENV benchmark/ceb/prepare_data.py
    fi
    echo ""
    echo "--- Step 2: Scale to SF2 ---"
    $VENV -m dataset.custom_scaler.scale_parquet \
        --duckdb "$SCRIPT_DIR/imdb.duckdb" \
        --output-dir "$SCRIPT_DIR/imdb_parquet" \
        --scale 2
    echo ""
else
    echo "--- Data already prepared (sf2 parquet exists) ---"
fi

# Step 2: Query Generation (skip if already done)
if [ ! -f "$SCRIPT_DIR/queries_bespoke.txt" ]; then
    echo "--- Step 3: Generate Queries ---"
    $VENV benchmark/ceb/gen_queries.py
    echo ""
else
    echo "--- Queries already generated ---"
fi

# Step 3: Build Bespoke (skip if already done)
if [ ! -f "$SCRIPT_DIR/build/db" ]; then
    echo "--- Build Bespoke Engine ---"
    bash benchmark/ceb/build_bespoke.sh
    echo ""
else
    echo "--- Bespoke already built ---"
fi

# Step 4: Correctness Verification
echo "--- Step 4: Verify Correctness ---"
$VENV benchmark/ceb/verify_correctness.py
echo ""

# Step 5: DuckDB Performance
echo "--- Step 5: Measure DuckDB ---"
$VENV benchmark/ceb/measure_duckdb.py
echo ""

# Step 6: Bespoke Warm Performance
echo "--- Step 6: Measure Bespoke (Warm) ---"
$VENV benchmark/ceb/measure_bespoke_warm.py
echo ""

# Step 7: Bespoke Cold Performance
echo "--- Step 7: Measure Bespoke (Cold) ---"
$VENV benchmark/ceb/measure_bespoke_cold.py
echo ""

# Step 8: Aggregate
echo "--- Step 8: Aggregate Results ---"
$VENV benchmark/ceb/aggregate_results.py
echo ""

echo "=== Done ==="
