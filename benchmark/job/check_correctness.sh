#!/bin/bash
# Check correctness of all Bespoke JOB queries against DuckDB golden results.
# Usage: bash benchmark/job/check_correctness.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJ_ROOT"

# Activate venv if not already active
if [ -z "$VIRTUAL_ENV" ] && [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Build if needed
if [ ! -f "$SCRIPT_DIR/build/db" ]; then
    echo "--- Building Bespoke engine ---"
    bash benchmark/job/build_bespoke.sh
    echo ""
fi

# Prepare data if needed
if [ ! -f "$SCRIPT_DIR/imdb_parquet/title.parquet" ]; then
    echo "--- Preparing data ---"
    python benchmark/job/prepare_data.py
    echo ""
fi

echo "--- Checking correctness of all JOB queries ---"
python benchmark/job/verify_correctness.py
