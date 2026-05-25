#!/usr/bin/env python3
"""Fix incorrect query implementations and optimize them.

For each failing query:
  1. Call Claude CLI to fix the query_q{qid}.cpp implementation
  2. Verify correctness against DuckDB
  3. Run optimization stages (sample_plan, trace)

Usage:
    python fix_and_optimize.py --queries 6a,6c,6e,...
    python fix_and_optimize.py --queries 6a,6c --max-fix-attempts 5
"""
import argparse
import logging
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from run_synthesis_claude import (
    claude_call, check_correctness, run_engine, save_snapshot,
    restore_snapshot, delete_result_csvs, build_system_prompt,
    get_duckdb_plan,
    _optim_prompt_pretext, _optim_prompt_pretext_optim,
    _optim_prompt_constraints, _optim_prompt_pinning,
    _optim_prompt_with_sample_plan, _optim_prompt_w_trace,
    _load_expert_knowledge,
    _safe_import_sf_list_gen,
    BENCHMARK, CORE_ID, WORKSPACE, HELPER_SCRIPT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SQL_DIR = Path("~/Project/benchmarks/imdb_job-postgres/queries/").expanduser()


def fix_single_query(qid: str, benchmark_sf: float, sys_prompt: str, max_attempts: int = 3) -> bool:
    correct, output = check_correctness(sf=benchmark_sf, query_ids=[qid])
    if correct:
        logger.info("Q%s: Already correct, skipping fix", qid)
        return True

    sql = (SQL_DIR / f"{qid}.sql").read_text().strip()

    prompt = f"""The query Q{qid} implementation in output/query_q{qid}.cpp is producing INCORRECT results.

Here is the SQL query it should implement:
```sql
{sql}
```

The correctness check output (showing expected vs got):
{output}

Please:
1. Read the current implementation in output/query_q{qid}.cpp
2. Read the SQL query carefully and identify the bug (wrong joins, filters, aggregation, string comparison, etc.)
3. Fix the implementation
4. Compile with: {HELPER_SCRIPT} compile --optimize
5. Verify correctness with: {HELPER_SCRIPT} check-correctness --sf {benchmark_sf} --query {qid}
6. If still incorrect, analyze the diff between expected and actual output and fix again.

Common bugs to look for:
- String comparison: MIN() on strings should use lexicographic ordering matching DuckDB/PostgreSQL (case-sensitive, ASCII order)
- Numeric stored as string: info fields like ratings are stored as strings but may need numeric comparison (e.g., '7.0' < '10.0' should compare as numbers when the SQL uses < operator)
- Missing or wrong join conditions
- Missing or wrong WHERE filters
- Wrong aggregation (COUNT vs SUM, etc.)
"""

    session_id = str(uuid.uuid4())
    for attempt in range(max_attempts):
        logger.info("Q%s: Fix attempt %d/%d", qid, attempt + 1, max_attempts)

        if attempt == 0:
            claude_call(
                prompt,
                session_id=session_id,
                system_prompt=sys_prompt,
                max_turns=50,
                timeout=3600,
            )
        else:
            correct, new_output = check_correctness(sf=benchmark_sf, query_ids=[qid])
            if correct:
                return True
            claude_call(
                f"The query Q{qid} is still incorrect after your last fix.\n\n"
                f"Correctness check output:\n{new_output}\n\n"
                f"Please re-read the SQL query, compare expected vs actual values carefully, "
                f"and fix the implementation. Then compile and verify again.",
                session_id=session_id,
                resume=True,
                max_turns=50,
                timeout=3600,
            )

        correct, _ = check_correctness(sf=benchmark_sf, query_ids=[qid])
        if correct:
            logger.info("Q%s: Fixed successfully on attempt %d", qid, attempt + 1)
            return True
        else:
            logger.warning("Q%s: Still incorrect after attempt %d", qid, attempt + 1)

    logger.error("Q%s: Failed to fix after %d attempts", qid, max_attempts)
    return False


def optimize_single_query(
    qid: str,
    benchmark_sf: float,
    sys_prompt: str,
    bespoke_storage: bool,
    stages: list,
) -> None:
    pretext_optim = _optim_prompt_pretext_optim(bespoke_storage=bespoke_storage)
    mandatory_constraints = _optim_prompt_constraints(allow_storage_changes=bespoke_storage)

    duckdb_plan = get_duckdb_plan(qid, sf=benchmark_sf)

    success, timings, _ = run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True)
    if success and timings:
        rt_key = list(timings.keys())[0]
        impl_rt_ms = timings[rt_key]
    else:
        impl_rt_ms = 10000.0

    run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True, trace=True)

    stage_builders = {
        "sample_plan": {
            "fn": lambda rt_ms: _optim_prompt_with_sample_plan(
                query_id=qid, constraints_str=mandatory_constraints,
                duckdb_plan=duckdb_plan, sf=benchmark_sf,
            ),
            "target_factor": None,
        },
        "trace": {
            "fn": lambda rt_ms: _optim_prompt_w_trace(
                query_id=qid, constraints_str=mandatory_constraints,
                target_rt_ms=rt_ms / 10, current_rt_ms=rt_ms, sf=benchmark_sf,
                factor=10, storage_is_bespoke=bespoke_storage,
            ),
            "target_factor": 10,
        },
    }

    for stage_name in stages:
        if stage_name not in stage_builders:
            logger.warning("Unknown stage '%s', skipping", stage_name)
            continue

        logger.info("Q%s: Optimization stage '%s' (current: %.1f ms)", qid, stage_name, impl_rt_ms)

        target_factor = stage_builders[stage_name]["target_factor"]
        if target_factor is not None:
            target_rt_ms = impl_rt_ms / target_factor
            gap_ms = impl_rt_ms - target_rt_ms
            if gap_ms < 10.0:
                logger.info("Q%s: Stage '%s': SKIPPED (gap %.2f ms < 10 ms, current=%.1f ms, target=%.1f ms)", qid, stage_name, gap_ms, impl_rt_ms, target_rt_ms)
                delete_result_csvs()
                continue
        else:
            if impl_rt_ms < 10.0:
                logger.info("Q%s: Stage '%s': SKIPPED (current runtime %.2f ms already < 10 ms)", qid, stage_name, impl_rt_ms)
                delete_result_csvs()
                continue

        save_snapshot(f"fix_opt_{stage_name}_q{qid}_before")

        stage_prompt = stage_builders[stage_name]["fn"](impl_rt_ms)
        full_prompt = pretext_optim + "\n" + stage_prompt

        max_turns = 125 if stage_name == "trace" else None
        query_session_id = str(uuid.uuid4())

        claude_call(
            full_prompt,
            session_id=query_session_id,
            system_prompt=sys_prompt,
            max_turns=max_turns,
            timeout=7200,
        )

        success_after, timings_after, _ = run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True)
        if success_after and timings_after:
            rt_key = list(timings_after.keys())[0]
            rt_after_ms = timings_after[rt_key]
        else:
            rt_after_ms = float("inf")

        correct_after, _ = check_correctness(sf=benchmark_sf, query_ids=[qid])
        improved = correct_after and (impl_rt_ms - rt_after_ms) >= 1.0

        if improved:
            impl_rt_ms = rt_after_ms
            logger.info("Q%s: Stage '%s': %.1f ms -> %.1f ms (improved)", qid, stage_name, impl_rt_ms, rt_after_ms)
            save_snapshot(f"fix_opt_{stage_name}_q{qid}_improved")
        else:
            logger.warning("Q%s: Stage '%s': %.1f ms -> %.1f ms (correct=%s). REVERTING.", qid, stage_name, impl_rt_ms, rt_after_ms, correct_after)
            restore_snapshot(f"fix_opt_{stage_name}_q{qid}_before")

        delete_result_csvs()


def main():
    parser = argparse.ArgumentParser(description="Fix and optimize failing queries")
    parser.add_argument("--queries", type=str, required=True, help="Comma-separated query IDs")
    parser.add_argument("--max-fix-attempts", type=int, default=3)
    parser.add_argument("--stages", type=str, default="sample_plan,trace", help="Optimization stages to run after fixing")
    parser.add_argument("--skip-fix", action="store_true", help="Skip the fix step, only optimize")
    args = parser.parse_args()

    query_ids = [q.strip() for q in args.queries.split(",")]
    stages = [s.strip() for s in args.stages.split(",")]

    _gen_sf = _safe_import_sf_list_gen()
    _, benchmark_sf = _gen_sf(BENCHMARK)

    sys_prompt = build_system_prompt(query_ids, "optimize")

    logger.info("Queries: %s", query_ids)
    logger.info("Stages: %s", stages)
    logger.info("Benchmark SF: %s", benchmark_sf)

    fixed = []
    failed = []

    for qid in query_ids:
        logger.info("=" * 60)
        logger.info("=== Processing Q%s ===", qid)
        logger.info("=" * 60)

        save_snapshot(f"fix_q{qid}_before")

        if not args.skip_fix:
            ok = fix_single_query(qid, benchmark_sf, sys_prompt, max_attempts=args.max_fix_attempts)
            if not ok:
                failed.append(qid)
                logger.error("Q%s: Could not fix, skipping optimization", qid)
                continue

        correct, _ = check_correctness(sf=benchmark_sf, query_ids=[qid])
        if not correct:
            logger.error("Q%s: Not correct, skipping optimization", qid)
            failed.append(qid)
            continue

        fixed.append(qid)
        save_snapshot(f"fix_q{qid}_fixed")

        optimize_single_query(qid, benchmark_sf, sys_prompt, bespoke_storage=True, stages=stages)

        delete_result_csvs()

    logger.info("=" * 60)
    logger.info("=== SUMMARY ===")
    logger.info("Fixed and optimized: %s (%d/%d)", fixed, len(fixed), len(query_ids))
    if failed:
        logger.info("Failed to fix: %s", failed)

    save_snapshot("fix_and_optimize_done")


if __name__ == "__main__":
    main()
