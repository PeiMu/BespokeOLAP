#!/bin/bash
# Benchmark Bespoke JOB engine using hyperfine (5 warmup, 10 runs).
#
# Measures total wall time for all 113 queries per invocation.
# Each invocation starts the db process, loads data, runs queries, and exits.
#
# Usage:
#   bash benchmark/job/bench_hyperfine.sh          # cold (includes data loading)
#   bash benchmark/job/bench_hyperfine.sh --warm    # warm (pre-loads data, measures execution only)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJ_ROOT"

# Activate venv if not already active
if [ -z "$VIRTUAL_ENV" ] && [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Check prerequisites
command -v hyperfine >/dev/null 2>&1 || { echo "ERROR: hyperfine not found. Install with: apt install hyperfine"; exit 1; }
[ -f "$SCRIPT_DIR/build/db" ] || { echo "ERROR: Bespoke not built. Run: bash benchmark/job/build_bespoke.sh"; exit 1; }
[ -f "$SCRIPT_DIR/imdb_parquet/title.parquet" ] || { echo "ERROR: Parquet data missing. Run: python benchmark/job/prepare_data.py"; exit 1; }

RESULTS_DIR="$SCRIPT_DIR/results"
mkdir -p "$RESULTS_DIR"

MODE="cold"
RUN_ONCE_ARGS=""
if [ "$1" = "--warm" ]; then
    MODE="warm"
    RUN_ONCE_ARGS="--warm"
fi

echo "=== Bespoke JOB Benchmark (hyperfine, ${MODE}) ==="
echo "  Warmup: 5 runs"
echo "  Measured: 10 runs"
echo ""

hyperfine \
    --warmup 5 \
    --runs 10 \
    --export-json "$RESULTS_DIR/hyperfine_bespoke_${MODE}.json" \
    --export-markdown "$RESULTS_DIR/hyperfine_bespoke_${MODE}.md" \
    "python benchmark/job/run_once.py ${RUN_ONCE_ARGS}"

echo ""
echo "Results saved to:"
echo "  $RESULTS_DIR/hyperfine_bespoke_${MODE}.json"
echo "  $RESULTS_DIR/hyperfine_bespoke_${MODE}.md"
