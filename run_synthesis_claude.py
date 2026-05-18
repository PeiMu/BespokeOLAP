#!/usr/bin/env python3
"""Drive BespokeOLAP synthesis for JOB queries using Claude Code CLI.

Full-fidelity reproduction of the 3-stage BespokeOLAP pipeline:
  Stage 1: Storage plan generation    (run_gen_storage_plan.py equivalent)
  Stage 2: Base implementation        (run_gen_base_impl.py equivalent)
  Stage 3: Optimization loop          (run_optim_loop.py / OptimizationConversation equivalent)
    - 4 sub-stages per query: sample_plan, trace, expert_knowledge, human_reference

Usage:
    python run_synthesis_claude.py [--queries 1a,2a,...] [--phase storage|base|optimize|all]
    python run_synthesis_claude.py --queries 1a,2a,3a --phase all --with-storage-plan
    python run_synthesis_claude.py --phase optimize --resume-from-snapshot base_done

Requires: `claude` CLI available on PATH.
"""
import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from string import Template
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dataset.gen_job.gen_job_query import JOB_QUERY_IDS


def _lazy_imports():
    """Lazy-import modules that pull in heavy deps (OpenAI Agents SDK via llm_cache)."""
    import importlib
    mods = {}
    mods["dataset_tables_dict"] = importlib.import_module("dataset.dataset_tables_dict")
    mods["query_gen_factory"] = importlib.import_module("dataset.query_gen_factory")
    mods["fasttest_utils"] = importlib.import_module("tools.fasttest.utils")
    mods["sf_list_gen"] = importlib.import_module("tools.validate_tool.sf_list_gen")
    mods["general_utils"] = importlib.import_module("utils.general_utils")
    return mods


def _safe_import_benchmark_schema():
    """Import get_benchmark_schema without triggering llm_cache chain if possible."""
    try:
        from dataset.dataset_tables_dict import get_benchmark_schema
        return get_benchmark_schema
    except (ImportError, ModuleNotFoundError):
        from dataset.gen_ceb.imdb_schema import imdb_schema
        return lambda benchmark: imdb_schema


def _safe_import_sf_list_gen():
    try:
        from tools.validate_tool.sf_list_gen import gen_sf
        return gen_sf
    except (ImportError, ModuleNotFoundError):
        def gen_sf_fallback(benchmark):
            if benchmark == "job":
                return [1], 1
            raise ValueError(f"Unknown benchmark {benchmark}")
        return gen_sf_fallback


def _safe_import_affinity():
    try:
        from utils.general_utils import get_affinity_prompt
        return get_affinity_prompt
    except (ImportError, ModuleNotFoundError):
        def get_affinity_prompt_fallback(include_numa=False, filename="cpu_affinity.hpp"):
            return textwrap.dedent(f"""\
                CPU affinity helpers is predefined in {filename}.
                You have to use the following functions, no need to implement them yourself,
                they are already provided by the runtime:

                CPU affinity:
                  Pin the process to a single logical CPU for deterministic execution:
                    void pin_process_to_cpu(int cpu_id);

                  Restore affinity to all available CPUs:
                    void unpin_process_from_cpus();
            """)
        return get_affinity_prompt_fallback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

WORKSPACE = PROJECT_ROOT / "output"
PROMPTS_DIR = PROJECT_ROOT / "conversations" / "prompts"
HELPER_SCRIPT = "python synthesis/compile_and_run.py"

BENCHMARK = "job"
CORE_ID = 3


# ---------------------------------------------------------------------------
# Prompt loading helpers (mirror conversations/prompts_gen.py exactly)
# ---------------------------------------------------------------------------

def _load_txt(path: Path) -> str:
    return path.read_text()


def _optim_prompt_pretext(queries_path: str, num_queries: int) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_pretext_general.txt"))
    query_str = "query" if num_queries == 1 else "queries"
    return t.substitute(queries_path=queries_path, num_queries=num_queries, query_str=query_str)


def _optim_prompt_pretext_optim(bespoke_storage: bool) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_pretext_optim.txt"))
    storage_layout = "storage layout, " if bespoke_storage else ""
    return t.substitute(storage_layout=storage_layout)


def _optim_prompt_constraints(allow_storage_changes: bool = True) -> str:
    txt = _load_txt(PROMPTS_DIR / "optim_constraints.txt")
    if not allow_storage_changes:
        txt += "\n- You are NOT allowed to change the storage layout. Leave it as Struct-of-Arrays. Do not change the ordering of columns."
    return txt


def _optim_prompt_pinning(core_id: int) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_pinning.txt"))
    _get_affinity = _safe_import_affinity()
    affinity_prompt = _get_affinity(include_numa=False)
    return t.substitute(query_impl_path="query_impl.cpp", affinity_prompt=affinity_prompt, core_id=core_id)


def _optim_prompt_add_timings() -> str:
    return _load_txt(PROMPTS_DIR / "optim_add_timings_collect_stats.txt")


def _optim_prompt_add_timings_per_query(qids_str: str, refer_to_prev: bool, sf: float) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_add_timings_collect_stats_per_query.txt"))
    return t.substitute(
        qids_str=qids_str,
        refer_to_prev=" Align instrumentation with previous queries." if refer_to_prev else "",
        sf=sf,
    )


def _optim_prompt_with_sample_plan(query_id: str, constraints_str: str, duckdb_plan: str, sf: float) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_with_sample_plan.txt"))
    return t.substitute(query_id=query_id, constraints=constraints_str, duckdb_plan=duckdb_plan, sf=sf)


def _optim_prompt_w_trace(query_id: str, constraints_str: str, current_rt_ms: float,
                          target_rt_ms: float, sf: float, factor: float, storage_is_bespoke: bool) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_w_trace.txt"))
    return t.substitute(
        query_id=query_id, constraints=constraints_str,
        target_rt=f"{int(target_rt_ms)}ms", current_rt=f"{int(current_rt_ms)}ms",
        sf=sf, factor=factor,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns" if storage_is_bespoke else "",
    )


def _optim_prompt_with_expert_knowledge(query_id: str, constraints_str: str, expert_knowledge: str,
                                        current_rt_ms: float, target_rt_ms: float, sf: float,
                                        storage_is_bespoke: bool) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_w_expert_knowledge.txt"))
    return t.substitute(
        query_id=query_id, constraints=constraints_str, expert_knowledge=expert_knowledge,
        target_rt=f"{int(target_rt_ms)}ms", current_rt=f"{int(current_rt_ms)}ms",
        sf=sf,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns" if storage_is_bespoke else "",
    )


def _optim_prompt_with_human_reference(query_id: str, constraints_str: str, current_rt_ms: float,
                                       target_rt_ms: float, sf: float, storage_is_bespoke: bool) -> str:
    t = Template(_load_txt(PROMPTS_DIR / "optim_w_human_reference.txt"))
    return t.substitute(
        query_id=query_id, constraints=constraints_str,
        target_rt=f"{int(target_rt_ms)}ms", current_rt=f"{int(current_rt_ms)}ms",
        sf=sf,
        bespoke_storage_related=" e.g. changes to the storage layout and especially ordering of columns" if storage_is_bespoke else "",
    )


def _load_expert_knowledge() -> str:
    return _load_txt(PROMPTS_DIR / "expert_knowledge.txt")


# ---------------------------------------------------------------------------
# Claude CLI wrapper
# ---------------------------------------------------------------------------

def claude_call(
    prompt: str,
    session_id: Optional[str] = None,
    resume: bool = False,
    system_prompt: Optional[str] = None,
    max_turns: Optional[int] = None,
    timeout: int = 1800,
) -> str:
    """Call claude -p and return the text output."""
    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]

    if session_id:
        if resume:
            cmd.extend(["--resume", session_id])
        else:
            cmd.extend(["--session-id", session_id])

    cmd.extend(["--add-dir", str(WORKSPACE)])
    cmd.extend(["--allowedTools", "Bash Edit Read Write"])

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])

    logger.info("Claude call (session=%s, resume=%s, max_turns=%s)", session_id, resume, max_turns)
    logger.info("Prompt: %.300s...", prompt.replace("\n", " "))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
    )

    if result.returncode != 0:
        logger.error("claude -p failed (rc=%d): %s", result.returncode, result.stderr[:1000])
        return f"ERROR: {result.stderr[:1000]}"

    output = result.stdout.strip()
    logger.info("Response: %.500s...", output)
    return output


# ---------------------------------------------------------------------------
# Snapshot management (file-based, replaces GitSnapshotter)
# ---------------------------------------------------------------------------

def save_snapshot(name: str) -> None:
    subprocess.run(
        [sys.executable, "synthesis/compile_and_run.py", "snapshot", "--save", name],
        cwd=str(PROJECT_ROOT), check=True, capture_output=True,
    )
    logger.info("Snapshot saved: %s", name)


def restore_snapshot(name: str) -> None:
    subprocess.run(
        [sys.executable, "synthesis/compile_and_run.py", "snapshot", "--restore", name],
        cwd=str(PROJECT_ROOT), check=True, capture_output=True,
    )
    logger.info("Snapshot restored: %s", name)


def delete_result_csvs() -> None:
    subprocess.run(
        [sys.executable, "synthesis/compile_and_run.py", "delete-results"],
        cwd=str(PROJECT_ROOT), check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Engine execution helpers
# ---------------------------------------------------------------------------

def run_engine(
    sf: float,
    query_ids: Optional[List[str]] = None,
    optimize: bool = True,
    trace: bool = False,
) -> Tuple[bool, Dict[str, float], str]:
    """Run the engine and return (success, {query_id: ms}, raw_output)."""
    cmd = [
        sys.executable, "synthesis/compile_and_run.py", "run",
        "--sf", str(sf),
        "--benchmark", BENCHMARK,
    ]
    if optimize:
        cmd.append("--optimize")
    else:
        cmd.append("--no-optimize")
    if trace:
        cmd.append("--trace")
    if query_ids:
        cmd.extend(["--query"] + query_ids)

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600)
    output = result.stdout + "\n" + result.stderr

    if result.returncode != 0:
        return False, {}, output

    timings = {}
    for line in result.stdout.split("\n"):
        line = line.strip()
        if line.startswith("JSON:"):
            try:
                data = json.loads(line[5:].strip())
                timings = {k: v for k, v in data.get("measurements", {}).items()}
            except json.JSONDecodeError:
                pass

    return True, timings, output


def check_correctness(
    sf: float,
    query_ids: Optional[List[str]] = None,
    trace: bool = False,
) -> Tuple[bool, str]:
    """Check output correctness against DuckDB. Returns (all_correct, output)."""
    cmd = [
        sys.executable, "synthesis/compile_and_run.py", "check-correctness",
        "--sf", str(sf),
        "--benchmark", BENCHMARK,
    ]
    if trace:
        cmd.append("--trace")
    if query_ids:
        cmd.extend(["--query"] + query_ids)

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600)
    output = result.stdout + "\n" + result.stderr
    return result.returncode == 0, output


def get_duckdb_plan(query_id: str, sf: float) -> str:
    """Get DuckDB EXPLAIN ANALYZE plan for a query."""
    cmd = [
        sys.executable, "synthesis/compile_and_run.py", "duckdb-plan",
        "--sf", str(sf), "--query", query_id,
        "--benchmark", BENCHMARK,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120)
    return result.stdout


# ---------------------------------------------------------------------------
# Workspace setup
# ---------------------------------------------------------------------------

def _copy_template_to(destination_dir: Path, benchmark: str) -> None:
    """Copy C++ template files to workspace (inlined from tools.fasttest.utils to avoid agents dep)."""
    import re
    from dataset.dataset_tables_dict import get_tables_for_benchmark

    src_dir = PROJECT_ROOT / "misc" / "fasttest"
    tables = get_tables_for_benchmark(benchmark)

    indent = " " * 4
    table_defs = "\n".join(f"{indent}ArrowTable {name};" for name in tables)
    table_reads = "\n".join(
        f'{indent}tables->{name} = ReadParquetTable(path + "{name}.parquet");'
        for name in tables
    )

    files = [
        "loader_impl.hpp", "loader_impl.cpp",
        "builder_impl.hpp", "builder_impl.cpp",
        "query_impl.hpp", "query_impl.cpp",
    ]

    def replace_block(text, marker_name, replacement):
        name = re.escape(marker_name)
        pattern = re.compile(
            rf"(?ms)^[ \t]*//[ \t]*start:[ \t]*{name}[ \t]*\r?\n?.*?^[ \t]*//[ \t]*end:[ \t]*{name}[ \t]*(?:\r?\n|$)",
            re.VERBOSE,
        )
        if replacement and not replacement.endswith(("\n", "\r\n")):
            replacement += "\n"
        result, n = pattern.subn(replacement, text, count=1)
        if n != 1:
            raise ValueError(f"expected exactly one replacement for '{marker_name}', got {n}")
        return result

    for filename in files:
        source = src_dir / filename
        if not source.is_file():
            raise FileNotFoundError(f"Source file not found: {source}")
        content = source.read_text()
        if filename == "loader_impl.hpp":
            content = replace_block(content, "table-defs", table_defs)
        elif filename == "loader_impl.cpp":
            content = replace_block(content, "table-reads", table_reads)
        dest = destination_dir / filename
        logger.info("Writing %s to %s", filename, dest)
        dest.write_text(content)


def setup_workspace(query_ids: List[str]) -> None:
    WORKSPACE.mkdir(exist_ok=True)

    logger.info("Copying C++ template to %s", WORKSPACE)
    _copy_template_to(WORKSPACE, benchmark=BENCHMARK)

    logger.info("Writing queries.txt and args_parser.hpp for %d queries", len(query_ids))
    from dataset.query_gen_factory import get_placeholders_fn
    from utils.general_utils import write_query_and_args_file
    gen_placeholders_fn = get_placeholders_fn(BENCHMARK)
    write_query_and_args_file(
        benchmark_name=BENCHMARK,
        gen_placeholders_fn=gen_placeholders_fn,
        query_list=query_ids,
        out_dir=str(WORKSPACE),
        use_fasttest_format=True,
        storage_plan=None,
    )
    logger.info("Workspace ready at %s", WORKSPACE)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt(query_ids: List[str], phase: str = "general") -> str:
    _get_schema = _safe_import_benchmark_schema()
    schema = _get_schema(BENCHMARK)
    constraints = _optim_prompt_constraints(allow_storage_changes=True)
    expert = _load_expert_knowledge()

    _gen_sf = _safe_import_sf_list_gen()
    verify_sf_list, max_sf = _gen_sf(BENCHMARK)
    sf_str = ", ".join(str(s) for s in verify_sf_list)

    return f"""You are an expert database engineer and skilled programmer.
You are building a specialized high-performance C++ OLAP engine for {len(query_ids)} JOB (Join Order Benchmark) queries on the IMDB dataset.

IMDB Schema:
{schema}

The workspace is at {WORKSPACE}. Key files:
- queries.txt — the SQL queries to implement
- args_parser.hpp — C++ argument parser (auto-generated)
- loader_impl.{{cpp,hpp}} — loads Parquet data into ArrowTable structs
- builder_impl.{{cpp,hpp}} — transforms ArrowTable into custom Database struct
- query_impl.{{cpp,hpp}} — executes queries on the Database struct

To compile: {HELPER_SCRIPT} compile --optimize
To compile with trace: {HELPER_SCRIPT} compile --optimize --trace
To run: {HELPER_SCRIPT} run --sf <SF> [--query <id>...]
To run with trace: {HELPER_SCRIPT} run --sf <SF> --trace [--query <id>...]
To check correctness: {HELPER_SCRIPT} check-correctness --sf <SF> [--query <id>...]
To save snapshot: {HELPER_SCRIPT} snapshot --save <name>
To restore snapshot: {HELPER_SCRIPT} snapshot --restore <name>

Verification scale factors: {sf_str}
Max benchmark scale factor: {max_sf}

CSV output format: delimiter=',', escapechar='\\\\', quotechar='"', header=True.
Each query prints: "<RUN_NR> | Execution ms: <TIME>"
"""


# ---------------------------------------------------------------------------
# STAGE 1: Storage Plan Generation
# (mirrors run_gen_storage_plan.py exactly)
# ---------------------------------------------------------------------------

def phase_storage_plan(query_ids: List[str], session_id: str) -> str:
    _get_schema = _safe_import_benchmark_schema()
    schema = _get_schema(BENCHMARK)
    queries_path = "queries.txt"

    prompt = f"""Your task is to analyze the workload and produce a creative in-memory storage-layout summary for the tables accessed by the query. You have the flexibility to return detailed, free-form text that explores not only conventional storage-layout recommendations but also unconventional, novel, and even 'crazy' storage designs.
You are encouraged to include additional ideas, new partitioning strategies, speculative encoding techniques, or experimental ways of grouping and organizing columns or data.
For each accessed table, feel free to be inventive and elaborate on possibilities such as hybrid layouts, speculative SoA/AoS (Array of Structures/Structure of Arrays) approaches, novel column encodings, or adaptive partitioning.
Use this as an opportunity to push beyond current norms and propose storage techniques that might be futuristic or outlandish.
Output the storage layout for each table. Output only the final storage layout.

Important:
- store all the data, and store them in a way that it could be flattened back to the original data
- do not store data redundantly, but you can use compression or encoding, meta data, or special datastructures
- optimized for in-memory (single-node) analytical query processing

The queries are listed in the file: {queries_path}.
The schema is:
{schema}

Based on the given queries and schema, provide a detailed and creative storage layout summary for the tables accessed by the query. Feel free to explore unconventional and novel storage designs, including speculative encoding techniques or experimental ways of organizing data. Write it to the file: `storage_plan.txt`."""

    return claude_call(
        prompt,
        session_id=session_id,
        system_prompt=build_system_prompt(query_ids, "storage"),
        timeout=600,
    )


# ---------------------------------------------------------------------------
# STAGE 2: Base Implementation
# (mirrors run_gen_base_impl.py:create_conversation() exactly)
# ---------------------------------------------------------------------------

def phase_base_impl(query_ids: List[str], session_id: str, with_storage_plan: bool = False) -> None:
    _gen_sf = _safe_import_sf_list_gen()
    verify_sf_list, max_sf = _gen_sf(BENCHMARK)

    if len(verify_sf_list) == 1:
        sf_verify_str = str(verify_sf_list[0])
    elif len(verify_sf_list) == 2:
        sf_verify_str = f"{verify_sf_list[0]} and {verify_sf_list[1]}"
    else:
        sf_verify_str = ", ".join(map(str, verify_sf_list[:-1])) + f", and {verify_sf_list[-1]}"

    example_query = "Q42a"
    example_query_params = "42a"

    parquet_path = f"benchmark/job/imdb_parquet"
    builder_path = "`builder_impl.hpp`/`builder_impl.cpp`"
    query_impl_path = "`query_impl.cpp`"
    args_path = "args_parser.hpp"

    if with_storage_plan:
        storage_plan = "storage_plan.txt"
        storage_hint = f"The storage plan is described in the file `{storage_plan}`. It describes how to store the parquet data in-memory for optimal query execution. Use this storage plan to implement the in-memory data structure accordingly. "
    else:
        storage_hint = "The minimum should be a struct-of-arrays."

    sys_prompt = build_system_prompt(query_ids, "base")

    # --- Prompt 1: Create TODO plan (don't implement yet) ---
    prompt_plan = f"""You are an expert database engineer and skilled programmer.
Write a specialized high-performance database engine in C++ that is optimized to only execute a predefined set of queries.
The database engine should run the SQL queries described in `queries.txt` ({len(query_ids)} {"query" if len(query_ids) == 1 else "queries"}).
Datatypes and operators can be hard-coded into the program to avoid interpretation overhead.

First convert the tables from ArrowTable into a custom data-structure (build) in file {builder_path}.
Store it as a Database struct object (predefined in {builder_path}).
Use an efficient in-memory representation of the data that allows fast execution of the queries. {storage_hint}

Then execute the queries on this data structure (execution). The execution logic interface is predefined in {query_impl_path}.
The query interface on an QueryRequest list where each request is one query to be executed.
Each query is specified as: `<QUERY_NR> <QUERY_PARAMETERS>` (e.g. for query {example_query} with parameters "EUROPE" and "1995-01-01": "{example_query_params} EUROPE 1995-01-01").
To parse the QUERY_NR and PARAMETERS, use the header-only C++ parser defined in `{args_path}`.
The database engine should execute all queries specified in the arg list sequentially.

Write the output of each query into a separate csv file.
Name it `result<RUN_NR>.csv` where `<RUN_NR>` is the position of the query in the arg list (starting from 1).
CSV arguments: (delimiter=',', escapechar='\\', quotechar='"', header=True).

You can get the table schemas like this: `parquet-dump-schema {parquet_path}/lineitem.parquet`.

Create a TODO plan with steps to implement such an database engine. Include conceptual comments of the data structure/... as well. Do not start the implementation yet.
Write the steps and the query into a file where they can be later marked as done.
Do not execute the steps yet."""

    logger.info("=== Base: TODO Plan ===")
    claude_call(prompt_plan, session_id=session_id, system_prompt=sys_prompt, timeout=600)

    # --- Prompt 2: Implement builder with stubs ---
    prompt_build = f"finish all todos. Focus on the build logic to convert ArrowTable into an efficient in-memory data structure ({builder_path}). For now use stubs for the query execution logic in {query_impl_path}."

    logger.info("=== Base: Implement Builder ===")
    claude_call(prompt_build, session_id=session_id, resume=True, timeout=1200)

    # --- Prompt 3: Compile and test ---
    if sf_verify_str == str(max_sf):
        prompt_compile = f"Execute and check termination without error. First call the compile tool, then check the run tool (scale_factor {sf_verify_str}). If there are errors, fix the implementation accordingly."
    else:
        prompt_compile = f"Execute and check termination without error. First call the compile tool, then check the run tool (scalefactors {sf_verify_str} and also {max_sf}). If there are errors, fix the implementation accordingly."

    logger.info("=== Base: Compile & Test ===")
    claude_call(prompt_compile, session_id=session_id, resume=True, timeout=600)

    # --- Prompt 4: Add time measurement ---
    prompt_timing = 'add time measurement for execution. Exclude the csv output writing from the timing. Print/Output: once after each execution: "<RUN_NR> | Execution ms: YYY".'

    logger.info("=== Base: Add Timing ===")
    claude_call(prompt_timing, session_id=session_id, resume=True, timeout=600)

    # --- Per-query implement + validate loop ---
    for i, qid in enumerate(query_ids):
        if i == 0:
            prefix = "Lets start implementing the query execution logic. Implement all queries in the next steps step by step. Start with"
        else:
            prefix = "Next, continue implementing the query execution logic for"

        # Get sample args for this query (JOB has no placeholders, but keep the pattern)
        sample_args_str = ""

        prompt_impl = f"{prefix} query {qid}. Create a separate file for the implementation of this query. Do not print file contents after you are done.{sample_args_str}"

        logger.info("=== Base: Implement Q%s (%d/%d) ===", qid, i + 1, len(query_ids))
        claude_call(prompt_impl, session_id=session_id, resume=True, timeout=1200)

        # Check correctness
        prompt_check = f'Execute and check correctness by using the run tool. Run with query_id "{qid}" and scale_factor {sf_verify_str}. If there are errors, fix the implementation accordingly.'

        logger.info("=== Base: Validate Q%s ===", qid)
        claude_call(prompt_check, session_id=session_id, resume=True, timeout=600)

    # --- Final correctness check ---
    prompt_final_check = f"Check correctness of the output of all queries by using the run tool. Run with scale_factor {sf_verify_str}. Call the run tool once for all queries together. If there are errors, fix the implementation accordingly."

    logger.info("=== Base: Final Correctness Check ===")
    claude_call(prompt_final_check, session_id=session_id, resume=True, timeout=600)

    # --- Benchmark at max scale factor ---
    prompt_benchmark = f"Call the run tool with scale_factor {max_sf}. Benchmark the execution time of all queries. Fix any error if occurs."

    logger.info("=== Base: Benchmark at SF=%s ===", max_sf)
    claude_call(prompt_benchmark, session_id=session_id, resume=True, timeout=600)

    # --- Optimize build time ---
    if sf_verify_str == str(max_sf):
        prompt_optim_build = f"Optimize the build implementation. You should reduce build time to below 10 seconds for scale factor {max_sf}. Use multithreading, and make build as fast as duckdb. Run the implementation with scale_factor {sf_verify_str} to check for correctness."
    else:
        prompt_optim_build = f"Optimize the build implementation. You should reduce build time to below 10 seconds for scale factor {max_sf}. Use multithreading, and make build as fast as duckdb. Run the implementation with scale_factor {sf_verify_str} to check for correctness and measure speedup build time with scale_factor {max_sf}."

    logger.info("=== Base: Optimize Build ===")
    claude_call(prompt_optim_build, session_id=session_id, resume=True, timeout=1200)

    # Save snapshot after base implementation
    save_snapshot("base_done")
    logger.info("Base implementation complete. Snapshot saved as 'base_done'.")


# ---------------------------------------------------------------------------
# STAGE 3: Optimization Loop
# (mirrors OptimizationConversation.run() from optimization_conversation.py)
# ---------------------------------------------------------------------------

def phase_optimize(query_ids: List[str], session_id: str, bespoke_storage: bool = True) -> None:
    _gen_sf = _safe_import_sf_list_gen()
    verify_sf_list, max_sf = _gen_sf(BENCHMARK)
    benchmark_sf = max_sf

    pretext = _optim_prompt_pretext(queries_path="queries.txt", num_queries=len(query_ids))
    pretext_optim = _optim_prompt_pretext_optim(bespoke_storage=bespoke_storage)
    mandatory_constraints = _optim_prompt_constraints(allow_storage_changes=bespoke_storage)
    pinning_prompt = _optim_prompt_pinning(core_id=CORE_ID)
    expert_knowledge = _load_expert_knowledge()

    sys_prompt = build_system_prompt(query_ids, "optimize")

    # --- Step 1: Check initial correctness ---
    logger.info("=== Optim: Checking initial correctness ===")
    correct, output = check_correctness(sf=benchmark_sf, query_ids=query_ids)
    if not correct:
        logger.error("Initial implementation is not correct. Output: %s", output[:500])
        logger.error("Fix the base implementation before running optimization.")
        sys.exit(1)

    # --- Step 2: CPU Pinning ---
    logger.info("=== Optim: CPU Pinning ===")
    claude_call(
        pretext + "\n" + pinning_prompt,
        session_id=session_id,
        system_prompt=sys_prompt,
        timeout=600,
    )

    # --- Step 3: Run validation at all SFs to get initial runtimes ---
    logger.info("=== Optim: Initial Runtimes ===")
    initial_runtimes: Dict[float, Dict[str, float]] = {}
    for sf in verify_sf_list + [benchmark_sf]:
        success, timings, _ = run_engine(sf=sf, query_ids=query_ids, optimize=True)
        if success:
            initial_runtimes[sf] = timings
            logger.info("  SF=%.2f: %s", sf, {k: f"{v:.1f}ms" for k, v in timings.items()})

    # --- Step 4: Add timing instrumentation in 3-query batches ---
    add_timings_base = _optim_prompt_add_timings()

    for i in range(0, len(query_ids), 3):
        batch = query_ids[i:min(i + 3, len(query_ids))]
        qids_str = ", ".join(batch)

        per_query_prompt = _optim_prompt_add_timings_per_query(
            qids_str=qids_str,
            refer_to_prev=i > 0,
            sf=benchmark_sf,
        )

        if i == 0:
            full_prompt = add_timings_base + "\n" + per_query_prompt
        else:
            full_prompt = per_query_prompt

        logger.info("=== Optim: Add Timings for %s ===", qids_str)
        claude_call(full_prompt, session_id=session_id, resume=True, timeout=900)

        # Check correctness with both trace=False and trace=True
        for trace_mode in [False, True]:
            for attempt in range(3):
                correct, out = check_correctness(sf=benchmark_sf, query_ids=batch, trace=trace_mode)
                if correct:
                    break
                logger.warning("Correctness check failed (trace=%s, attempt=%d). Asking Claude to fix...", trace_mode, attempt + 1)
                claude_call(
                    f"Validation check reported results are incorrect (with trace_mode={trace_mode}, qids={batch}). Please fix the instrumentation to ensure correctness while still collecting the necessary timing information.",
                    session_id=session_id, resume=True, timeout=600,
                )
            else:
                logger.error("Failed to fix correctness after 3 attempts for trace=%s, qids=%s", trace_mode, batch)

    # --- Step 5: Delete result CSVs ---
    delete_result_csvs()

    # --- Step 6: Redirect tracing output to file ---
    logger.info("=== Optim: Redirect Tracing to File ===")
    claude_call(
        "Instead of writing tracing output to stdout, write it to a file `tracing_output.log`.",
        session_id=session_id, resume=True, timeout=600,
    )

    # Save snapshot before optimization stages
    save_snapshot("pre_optimization")

    # --- Step 7: Collect DuckDB plans for all queries ---
    logger.info("=== Optim: Collecting DuckDB Plans ===")
    duckdb_plans: Dict[str, str] = {}
    for qid in query_ids:
        plan = get_duckdb_plan(qid, sf=benchmark_sf)
        duckdb_plans[qid] = plan
        logger.info("  Got plan for Q%s (%.0f chars)", qid, len(plan))

    # --- Step 8: Per-query optimization loop ---
    # Outer loop: stages, Inner loop: queries
    # (matches OptimizationConversation exactly)

    query_rt_log: Dict[str, float] = {}

    stage_configs = [
        {
            "name": "sample_plan",
            "get_prompt": lambda qid, rt_ms, **kw: _optim_prompt_with_sample_plan(
                query_id=qid, constraints_str=mandatory_constraints,
                duckdb_plan=duckdb_plans.get(qid, ""), sf=benchmark_sf,
            ),
            "max_turns": None,
        },
        {
            "name": "trace",
            "get_prompt": lambda qid, rt_ms, **kw: _optim_prompt_w_trace(
                query_id=qid, constraints_str=mandatory_constraints,
                target_rt_ms=rt_ms / 10, current_rt_ms=rt_ms, sf=benchmark_sf,
                factor=10, storage_is_bespoke=bespoke_storage,
            ),
            "max_turns": 125,
        },
        {
            "name": "expert_knowledge",
            "get_prompt": lambda qid, rt_ms, **kw: _optim_prompt_with_expert_knowledge(
                query_id=qid, constraints_str=mandatory_constraints,
                expert_knowledge=expert_knowledge,
                current_rt_ms=rt_ms, target_rt_ms=rt_ms / 2, sf=benchmark_sf,
                storage_is_bespoke=bespoke_storage,
            ),
            "max_turns": 150,
        },
        {
            "name": "human_reference",
            "get_prompt": lambda qid, rt_ms, **kw: _optim_prompt_with_human_reference(
                query_id=qid, constraints_str=mandatory_constraints,
                target_rt_ms=rt_ms / 2, current_rt_ms=rt_ms, sf=benchmark_sf,
                storage_is_bespoke=bespoke_storage,
            ),
            "max_turns": 125,
        },
    ]

    # Collect initial runtime for all queries
    logger.info("=== Optim: Initial Benchmark ===")
    success, timings, _ = run_engine(sf=benchmark_sf, query_ids=query_ids, optimize=True)
    if success:
        for k, v in timings.items():
            query_rt_log[k] = v

    # Also run with trace to have fresh stats
    run_engine(sf=benchmark_sf, query_ids=query_ids, optimize=True, trace=True)

    for stage_idx, stage in enumerate(stage_configs):
        stage_name = stage["name"]
        max_turns = stage["max_turns"]

        logger.info("=" * 60)
        logger.info("=== OPTIMIZATION STAGE %d/%d: %s ===", stage_idx + 1, len(stage_configs), stage_name)
        logger.info("=" * 60)

        for qid in query_ids:
            logger.info("--- Stage '%s' for Q%s ---", stage_name, qid)

            # Save snapshot before this stage for this query (for regression rollback)
            snap_name = f"stage_{stage_name}_q{qid}_before"
            save_snapshot(snap_name)

            # Measure current runtime for this query
            success, timings, _ = run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True)
            if success and timings:
                rt_key = list(timings.keys())[0]
                impl_rt_ms = timings[rt_key]
                query_rt_log[qid] = impl_rt_ms / 1000.0
            elif qid in query_rt_log:
                impl_rt_ms = query_rt_log[qid] * 1000.0
            else:
                impl_rt_ms = 10000.0

            # Also run with trace to have fresh stats
            run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True, trace=True)

            # Build the optimization prompt
            stage_prompt = stage["get_prompt"](qid=qid, rt_ms=impl_rt_ms)
            full_prompt = pretext_optim + "\n" + stage_prompt

            # Use a separate session per query per stage for conversation branching
            query_session_id = f"{session_id}-{stage_name}-{qid}"

            logger.info("  Current runtime: %.1f ms. Running optimization...", impl_rt_ms)

            claude_call(
                full_prompt,
                session_id=query_session_id,
                system_prompt=sys_prompt,
                max_turns=max_turns,
                timeout=2400,
            )

            # Measure performance after optimization
            success_after, timings_after, _ = run_engine(sf=benchmark_sf, query_ids=[qid], optimize=True)

            if success_after and timings_after:
                rt_key = list(timings_after.keys())[0]
                rt_after_ms = timings_after[rt_key]
            else:
                rt_after_ms = float("inf")

            # Check correctness
            correct_after, _ = check_correctness(sf=benchmark_sf, query_ids=[qid])

            improved = correct_after and rt_after_ms < impl_rt_ms

            if improved:
                query_rt_log[qid] = rt_after_ms / 1000.0
                logger.info(
                    "  Q%s | Stage '%s': %.1f ms -> %.1f ms (improved x%.2f)",
                    qid, stage_name, impl_rt_ms, rt_after_ms, impl_rt_ms / rt_after_ms if rt_after_ms > 0 else float("inf"),
                )
                # Save the improved version
                save_snapshot(f"stage_{stage_name}_q{qid}_improved")
            else:
                logger.warning(
                    "  Q%s | Stage '%s': %.1f ms -> %.1f ms (correct=%s). REVERTING.",
                    qid, stage_name, impl_rt_ms, rt_after_ms, correct_after,
                )
                restore_snapshot(snap_name)

                # Verify correctness after rollback
                correct_reverted, _ = check_correctness(sf=benchmark_sf, query_ids=[qid])
                if not correct_reverted:
                    logger.error("  Reverted version is also incorrect! Asking Claude to fix...")
                    claude_call(
                        f"I rolled back your changes since the output was not correct. But after rollback, the results are still wrong. Please re-evaluate your implementation of query {qid} and make sure that it produces correct results!",
                        session_id=query_session_id, resume=True, timeout=600,
                    )

            # Clean up result CSVs
            delete_result_csvs()

        # Full benchmark at end of each stage
        logger.info("=== Stage '%s' Complete: Full Benchmark ===", stage_name)
        success, timings, output = run_engine(sf=benchmark_sf, query_ids=query_ids, optimize=True)
        if success:
            total_ms = sum(timings.values())
            logger.info("  Total: %.1f ms across %d queries", total_ms, len(timings))
            for k, v in sorted(timings.items()):
                logger.info("    Q%s: %.1f ms", k, v)

        # Save stage-end snapshot
        save_snapshot(f"stage_{stage_name}_done")

    logger.info("=== Optimization Complete ===")

    # Final benchmark
    success, timings, _ = run_engine(sf=benchmark_sf, query_ids=query_ids, optimize=True)
    if success:
        total_ms = sum(timings.values())
        logger.info("Final total: %.1f ms across %d queries at SF=%s", total_ms, len(timings), benchmark_sf)
        for k, v in sorted(timings.items()):
            logger.info("  Q%s: %.1f ms", k, v)

    save_snapshot("optimization_done")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BespokeOLAP synthesis via Claude Code CLI")
    parser.add_argument(
        "--queries", type=str, default=None,
        help="Comma-separated query IDs (default: all 113 JOB queries)",
    )
    parser.add_argument(
        "--phase", choices=["storage", "base", "optimize", "all"], default="all",
    )
    parser.add_argument(
        "--with-storage-plan", action="store_true", default=False,
        help="Generate and use a storage plan (Stage 1 before base impl)",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Remove output/ before starting",
    )
    parser.add_argument(
        "--resume-from-snapshot", type=str, default=None,
        help="Restore a snapshot before starting (e.g., 'base_done' to skip base impl)",
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Explicit session ID (default: auto-generated UUID)",
    )
    args = parser.parse_args()

    if args.queries:
        query_ids = [q.strip() for q in args.queries.split(",")]
    else:
        query_ids = list(JOB_QUERY_IDS)

    logger.info("Target queries: %s (%d total)", ", ".join(query_ids[:5]) + ("..." if len(query_ids) > 5 else ""), len(query_ids))

    # Verify claude is available
    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.error("claude CLI not found. Install Claude Code first.")
        sys.exit(1)

    if args.clean and WORKSPACE.exists():
        logger.info("Cleaning workspace %s", WORKSPACE)
        shutil.rmtree(WORKSPACE)

    # Determine phases
    phases = args.phase
    if phases == "all":
        if args.with_storage_plan:
            phases = ["storage", "base", "optimize"]
        else:
            phases = ["base", "optimize"]
    else:
        phases = [phases]

    # Restore snapshot if requested
    if args.resume_from_snapshot:
        restore_snapshot(args.resume_from_snapshot)
        logger.info("Restored snapshot: %s", args.resume_from_snapshot)
    elif "base" in phases or "storage" in phases:
        setup_workspace(query_ids)

    session_id = args.session_id or str(uuid.uuid4())
    logger.info("Session ID: %s", session_id)

    bespoke_storage = args.with_storage_plan

    if "storage" in phases:
        logger.info("=" * 60)
        logger.info("=== STAGE 1: Storage Plan Generation ===")
        logger.info("=" * 60)
        phase_storage_plan(query_ids, session_id)

    if "base" in phases:
        logger.info("=" * 60)
        logger.info("=== STAGE 2: Base Implementation ===")
        logger.info("=" * 60)
        base_session = f"{session_id}-base"
        phase_base_impl(query_ids, base_session, with_storage_plan=bespoke_storage)

    if "optimize" in phases:
        logger.info("=" * 60)
        logger.info("=== STAGE 3: Optimization Loop (4 sub-stages) ===")
        logger.info("=" * 60)
        optim_session = f"{session_id}-optim"
        phase_optimize(query_ids, optim_session, bespoke_storage=bespoke_storage)

    logger.info("=" * 60)
    logger.info("=== Synthesis Complete ===")
    logger.info("Workspace: %s", WORKSPACE)
    logger.info("To run: %s run --sf 1", HELPER_SCRIPT)


if __name__ == "__main__":
    main()
