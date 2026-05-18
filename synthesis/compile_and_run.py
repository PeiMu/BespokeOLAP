#!/usr/bin/env python3
"""Helper for Claude Code to compile and run the bespoke engine.

Usage:
    python synthesis/compile_and_run.py compile [--optimize] [--trace]
    python synthesis/compile_and_run.py run --sf <scale_factor> [--query <id>...] [--optimize] [--trace]
    python synthesis/compile_and_run.py validate --sf <scale_factor> [--query <id>...] [--benchmark <name>]
    python synthesis/compile_and_run.py check-correctness --sf <scale_factor> [--query <id>...] [--trace] [--benchmark <name>]
    python synthesis/compile_and_run.py snapshot [--save <name>] [--restore <name>]
    python synthesis/compile_and_run.py duckdb-plan --sf <scale_factor> --query <id> [--benchmark <name>]
"""
import argparse
import csv
import io
import json
import logging
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dataset.dataset_tables_dict import get_dataset_name
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS
from dataset.query_gen_factory import get_query_gen
from misc.fasttest.compiler import Compiler
from misc.fasttest.fasttest_proc import FasttestProc


def format_args_string(
    query_list: List[str], placeholder_list: List[Dict]
) -> List[str]:
    args_list = []
    for qid_str, placeholders in zip(query_list, placeholder_list):
        tmp_vals = []
        for v in placeholders.values():
            if isinstance(v, str) and v.startswith("("):
                tmp_vals.append(v)
            else:
                tmp_vals.append(f'"{v}"')
        tmp_vals_str = " ".join(tmp_vals)
        args_list.append(f"{qid_str} {tmp_vals_str}")
    return args_list


def _parse_output(
    stdout: str,
    stderr: str,
    resp: str,
    expect_ingest_time: bool = False,
):
    lines = stdout.strip().split("\n")

    ingest_lines = [line for line in lines if line.startswith("Ingest ms:")]
    if expect_ingest_time and len(ingest_lines) == 0:
        return (
            "Error: no ingest line found in program stdout. "
            "Expected line like: 'Ingest ms: <num>'.\n"
            + f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
        )

    timing_lines = [
        line for line in lines if line.count("|") == 1 and "Execution ms:" in line
    ]
    if len(timing_lines) == 0:
        return (
            "Error: no timing lines found in program stdout. "
            "Expected lines like: '<run> | Execution ms: <num>'.\n"
            + f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
        )

    if expect_ingest_time and len(ingest_lines) > 1:
        return (
            "Error: multiple ingest lines found in program stdout. "
            "Expected only one line like: 'Ingest ms: <num>'.\n"
            + f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
        )

    if len(ingest_lines) == 1:
        ingest_time_ms_str = ingest_lines[0].strip()
        ingest_time_ms_str = ingest_time_ms_str[len("Ingest ms:"):].strip().strip(":").strip()
        ingest_time_ms = float(ingest_time_ms_str)
    else:
        ingest_time_ms = None

    measurements = []
    for timing_line in timing_lines:
        query_name, exec_time = timing_line.split("|")
        query_name = query_name.strip()
        exec_time = exec_time.strip()
        assert exec_time.startswith("Execution ms:"), (
            f"Unexpected exec time format: \"{exec_time}\""
        )
        exec_time = exec_time[len("Execution ms:"):].strip().strip(":").strip()
        if not query_name.isdigit():
            return (
                "Error: timing line run number is not an integer.\n"
                + f"Bad line: {timing_line}\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}\nResp:\n{resp}"
            )
        measurements.append((query_name, exec_time))

    return ingest_time_ms, measurements

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

WORKSPACE = PROJECT_ROOT / "output"
API_PATH = PROJECT_ROOT / "misc" / "fasttest"
SNAPSHOTS_DIR = WORKSPACE / ".snapshots"


def _resolve_parquet_dir(sf: float, benchmark: str = "job", parquet_dir: Optional[str] = None) -> str:
    if parquet_dir is not None:
        d = parquet_dir
    else:
        dataset = get_dataset_name(benchmark)
        if benchmark == "job":
            candidates = [
                str(PROJECT_ROOT / "benchmark" / "job" / "imdb_parquet") + "/",
                f"/mnt/labstore/bespoke_olap/{dataset}_parquet/",
                str(PROJECT_ROOT / f"{dataset}_parquet") + "/",
            ]
        else:
            candidates = [
                f"/mnt/labstore/bespoke_olap/{dataset}_parquet/sf{sf}/",
                str(PROJECT_ROOT / "benchmark" / "ceb" / f"{dataset}_parquet" / f"sf{sf}") + "/",
                str(PROJECT_ROOT / f"{dataset}_parquet" / f"sf{sf}") + "/",
            ]
        d = None
        for c in candidates:
            if Path(c.rstrip("/")).exists():
                d = c
                break
        if d is None:
            print(f"Parquet directory not found. Searched: {candidates}", file=sys.stderr)
            sys.exit(1)
    if not Path(d.rstrip("/")).exists():
        print(f"Parquet directory not found: {d}", file=sys.stderr)
        sys.exit(1)
    return d


def make_compiler(optimize: bool = True, trace: bool = False) -> Compiler:
    cxx_flags = []
    if optimize:
        cxx_flags.extend(["-O3", "-flto"])
    if trace:
        cxx_flags.append("-DTRACE")
    return Compiler(
        working_dir=WORKSPACE,
        libs={
            "loader": [
                API_PATH / "loader_api.cpp",
                "loader_impl.cpp",
                API_PATH / "loader_utils.cpp",
            ],
            "builder": [API_PATH / "builder_api.cpp", "builder_impl.cpp"],
            "query": [API_PATH / "query_api.cpp", "query_impl.cpp"],
        },
        main_src=API_PATH / "db.cpp",
        include_dirs=[API_PATH],
        app_extra_srcs=[API_PATH / "utils" / "build_id.cpp"],
        build_dir="build",
        link_libs=[],
        pkgconfig_libs=["arrow", "parquet"],
        extra_cxxflags=cxx_flags,
    )


def _run_engine(
    sf: float,
    query_ids: List[str],
    optimize: bool = True,
    trace: bool = False,
    benchmark: str = "job",
    parquet_dir: Optional[str] = None,
    timeout: int = 300,
) -> Tuple[Optional[float], list, str, str]:
    """Compile, run the engine, and return (ingest_time_ms, measurements, stdout, stderr).

    measurements is a list of (run_nr, exec_time_ms_str) tuples on success,
    or an error string on failure.
    """
    compiler = make_compiler(optimize=optimize, trace=trace)
    err = compiler.build()
    if err is not None:
        return None, f"COMPILE ERROR:\n{err}", "", ""

    pdir = _resolve_parquet_dir(sf, benchmark, parquet_dir)

    gen_query_fn = get_query_gen(benchmark)
    rnd = random.Random(42)
    query_list = []
    placeholder_list = []
    for qid in query_ids:
        _template, _sql, placeholders = gen_query_fn(query_name=f"Q{qid}", rnd=rnd)
        query_list.append(qid)
        placeholder_list.append(placeholders)

    args_list = format_args_string(query_list, placeholder_list)

    cmd = f"./db {pdir}"
    proc = FasttestProc(cmd, echo_output=True, cwd=WORKSPACE)

    for arg in args_list:
        proc.send(arg)
    proc.send("")

    resp, out, err_str = proc.run(timeout=timeout)

    parsed = _parse_output(out, err_str, resp)
    if isinstance(parsed, str):
        return None, parsed, out, err_str

    ingest_time_ms, measurements = parsed
    return ingest_time_ms, measurements, out, err_str


def do_compile(args):
    compiler = make_compiler(optimize=args.optimize, trace=args.trace)
    err = compiler.build()
    if err is not None:
        print(f"COMPILE ERROR:\n{err}", file=sys.stderr)
        sys.exit(1)
    flags = []
    if args.optimize:
        flags.append("-O3 -flto")
    if args.trace:
        flags.append("-DTRACE")
    print(f"Compilation successful. Flags: {' '.join(flags) if flags else '(none)'}")


def do_run(args):
    query_ids = args.query or list(JOB_QUERY_IDS)
    benchmark = getattr(args, "benchmark", "job")
    timeout = int(max(120, args.sf * len(query_ids) * 5))

    ingest_ms, measurements, out, err_str = _run_engine(
        sf=args.sf,
        query_ids=query_ids,
        optimize=args.optimize,
        trace=args.trace,
        benchmark=benchmark,
        parquet_dir=args.parquet_dir,
        timeout=timeout,
    )

    if isinstance(measurements, str):
        print(f"EXECUTION ERROR: {measurements}", file=sys.stderr)
        if out:
            print(f"STDOUT:\n{out}", file=sys.stderr)
        if err_str:
            print(f"STDERR:\n{err_str}", file=sys.stderr)
        sys.exit(1)

    if ingest_ms is not None:
        print(f"Ingest: {ingest_ms:.2f} ms")
    print(f"Executed {len(measurements)} queries successfully.")
    total_ms = 0.0
    for run_nr, time_ms_str in measurements:
        t = float(time_ms_str)
        total_ms += t
        print(f"  {run_nr}: {t:.2f} ms")
    print(f"Total execution: {total_ms:.2f} ms")

    result = {
        "ingest_ms": ingest_ms,
        "measurements": {nr: float(t) for nr, t in measurements},
        "total_ms": total_ms,
    }
    print(f"\nJSON: {json.dumps(result)}")


def do_check_correctness(args):
    """Compile, run, and compare output CSVs against DuckDB ground truth."""
    query_ids = args.query or list(JOB_QUERY_IDS)
    benchmark = getattr(args, "benchmark", "job")
    trace = getattr(args, "trace", False)

    ingest_ms, measurements, out, err_str = _run_engine(
        sf=args.sf,
        query_ids=query_ids,
        optimize=True,
        trace=trace,
        benchmark=benchmark,
        parquet_dir=args.parquet_dir,
    )

    if isinstance(measurements, str):
        print(f"EXECUTION ERROR: {measurements}", file=sys.stderr)
        sys.exit(1)

    from tools.validate_tool.duckdb_connection_manager import DuckDBConnectionManager

    pdir = _resolve_parquet_dir(args.sf, benchmark, args.parquet_dir)
    duckdb_con = DuckDBConnectionManager(
        pre_load_duckdb_tables=True,
        parquet_path=pdir,
        sf=args.sf,
        pin_worker=False,
        benchmark=benchmark,
    )

    gen_query_fn = get_query_gen(benchmark)
    rnd = random.Random(42)
    all_correct = True
    results_detail = {}

    for idx, qid in enumerate(query_ids):
        run_nr = idx + 1
        _template, sql, _placeholders = gen_query_fn(query_name=f"Q{qid}", rnd=rnd)
        time_ms, duckdb_result, _ = duckdb_con.duckdb_sql(sql)

        result_csv = WORKSPACE / f"result{run_nr}.csv"
        if not result_csv.exists():
            print(f"  Q{qid}: FAIL - result{run_nr}.csv not found")
            all_correct = False
            results_detail[qid] = {"correct": False, "reason": "missing CSV"}
            continue

        csv_content = result_csv.read_text()
        reader = csv.reader(io.StringIO(csv_content))
        impl_rows = list(reader)

        duckdb_rows = len(duckdb_result) if duckdb_result is not None else 0
        impl_data_rows = len(impl_rows) - 1 if impl_rows else 0

        if impl_data_rows == duckdb_rows:
            print(f"  Q{qid}: OK ({impl_data_rows} rows, DuckDB {time_ms:.1f}ms)")
            results_detail[qid] = {"correct": True, "rows": impl_data_rows}
        else:
            print(f"  Q{qid}: FAIL - got {impl_data_rows} rows, expected {duckdb_rows}")
            all_correct = False
            results_detail[qid] = {
                "correct": False,
                "got_rows": impl_data_rows,
                "expected_rows": duckdb_rows,
            }

    status = "ALL CORRECT" if all_correct else "SOME FAILURES"
    print(f"\nValidation: {status}")
    print(f"JSON: {json.dumps({'all_correct': all_correct, 'detail': results_detail})}")
    if not all_correct:
        sys.exit(1)


def do_validate(args):
    parquet_dir = _resolve_parquet_dir(args.sf, getattr(args, "benchmark", "job"), args.parquet_dir)

    from tools.validate_tool.duckdb_connection_manager import DuckDBConnectionManager

    benchmark = getattr(args, "benchmark", "job")
    gen_query_fn = get_query_gen(benchmark)
    query_ids = args.query or list(JOB_QUERY_IDS)

    duckdb_con = DuckDBConnectionManager(
        pre_load_duckdb_tables=True,
        parquet_path=parquet_dir,
        sf=args.sf,
        pin_worker=True,
        benchmark=benchmark,
    )

    rnd = random.Random(42)
    print(f"Validating {len(query_ids)} queries at SF={args.sf}...")
    passed = 0
    failed = 0
    for qid in query_ids:
        _template, sql, _placeholders = gen_query_fn(query_name=f"Q{qid}", rnd=rnd)
        time_ms, result, _ = duckdb_con.duckdb_sql(sql)
        print(f"  Q{qid}: DuckDB {time_ms:.2f} ms, {len(result)} rows")
        passed += 1

    print(f"\nDuckDB baseline: {passed} passed, {failed} failed")


def do_snapshot(args):
    """Save or restore workspace snapshots (file-based, no git required)."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.save:
        snap_dir = SNAPSHOTS_DIR / args.save
        if snap_dir.exists():
            shutil.rmtree(snap_dir)
        snap_dir.mkdir(parents=True)
        for f in WORKSPACE.iterdir():
            if f.name in (".snapshots", "build", "__pycache__"):
                continue
            if f.name.startswith("result") and f.suffix == ".csv":
                continue
            dest = snap_dir / f.name
            if f.is_dir():
                shutil.copytree(f, dest)
            else:
                shutil.copy2(f, dest)
        print(f"Snapshot saved: {args.save}")

    elif args.restore:
        snap_dir = SNAPSHOTS_DIR / args.restore
        if not snap_dir.exists():
            print(f"Snapshot not found: {args.restore}", file=sys.stderr)
            sys.exit(1)
        for f in WORKSPACE.iterdir():
            if f.name in (".snapshots", "build", "__pycache__"):
                continue
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
        for f in snap_dir.iterdir():
            dest = WORKSPACE / f.name
            if f.is_dir():
                shutil.copytree(f, dest)
            else:
                shutil.copy2(f, dest)
        print(f"Snapshot restored: {args.restore}")

    elif args.list:
        if SNAPSHOTS_DIR.exists():
            snaps = sorted(d.name for d in SNAPSHOTS_DIR.iterdir() if d.is_dir())
            for s in snaps:
                print(s)
        else:
            print("No snapshots found.")
    else:
        print("Specify --save <name>, --restore <name>, or --list", file=sys.stderr)
        sys.exit(1)


def do_duckdb_plan(args):
    """Get DuckDB EXPLAIN ANALYZE plan for a query."""
    benchmark = getattr(args, "benchmark", "job")
    pdir = _resolve_parquet_dir(args.sf, benchmark, args.parquet_dir)

    from tools.validate_tool.duckdb_connection_manager import DuckDBConnectionManager

    gen_query_fn = get_query_gen(benchmark)
    rnd = random.Random(42)
    qid = args.query[0] if isinstance(args.query, list) else args.query
    _template, sql, _placeholders = gen_query_fn(query_name=f"Q{qid}", rnd=rnd)

    duckdb_con = DuckDBConnectionManager(
        pre_load_duckdb_tables=True,
        parquet_path=pdir,
        sf=args.sf,
        pin_worker=False,
        benchmark=benchmark,
    )

    explain_sql = f"EXPLAIN ANALYZE {sql}"
    _time_ms, result, _ = duckdb_con.duckdb_sql(explain_sql)

    plan_text = ""
    if result is not None:
        for row in result:
            if isinstance(row, (list, tuple)):
                plan_text += str(row[-1]) + "\n"
            else:
                plan_text += str(row) + "\n"

    print(f"DuckDB EXPLAIN ANALYZE for Q{qid} at SF={args.sf}:")
    print(plan_text)


def do_delete_results(args):
    """Delete all result*.csv files from workspace."""
    count = 0
    for f in WORKSPACE.glob("result*.csv"):
        f.unlink()
        count += 1
    print(f"Deleted {count} result CSV files.")


def main():
    parser = argparse.ArgumentParser(description="Compile and run bespoke engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile")
    p_compile.add_argument("--optimize", action="store_true", default=True)
    p_compile.add_argument("--no-optimize", dest="optimize", action="store_false")
    p_compile.add_argument("--trace", action="store_true", default=False)

    p_run = sub.add_parser("run")
    p_run.add_argument("--sf", type=float, required=True)
    p_run.add_argument("--query", nargs="+")
    p_run.add_argument("--optimize", action="store_true", default=True)
    p_run.add_argument("--no-optimize", dest="optimize", action="store_false")
    p_run.add_argument("--trace", action="store_true", default=False)
    p_run.add_argument("--parquet-dir", type=str, default=None)
    p_run.add_argument("--benchmark", type=str, default="job")

    p_check = sub.add_parser("check-correctness")
    p_check.add_argument("--sf", type=float, required=True)
    p_check.add_argument("--query", nargs="+")
    p_check.add_argument("--trace", action="store_true", default=False)
    p_check.add_argument("--parquet-dir", type=str, default=None)
    p_check.add_argument("--benchmark", type=str, default="job")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--sf", type=float, required=True)
    p_validate.add_argument("--query", nargs="+")
    p_validate.add_argument("--parquet-dir", type=str, default=None)
    p_validate.add_argument("--benchmark", type=str, default="job")

    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--save", type=str, default=None)
    p_snap.add_argument("--restore", type=str, default=None)
    p_snap.add_argument("--list", action="store_true", default=False)

    p_plan = sub.add_parser("duckdb-plan")
    p_plan.add_argument("--sf", type=float, required=True)
    p_plan.add_argument("--query", type=str, required=True)
    p_plan.add_argument("--parquet-dir", type=str, default=None)
    p_plan.add_argument("--benchmark", type=str, default="job")

    p_clean = sub.add_parser("delete-results")

    args = parser.parse_args()
    if args.command == "compile":
        do_compile(args)
    elif args.command == "run":
        do_run(args)
    elif args.command == "check-correctness":
        do_check_correctness(args)
    elif args.command == "validate":
        do_validate(args)
    elif args.command == "snapshot":
        do_snapshot(args)
    elif args.command == "duckdb-plan":
        do_duckdb_plan(args)
    elif args.command == "delete-results":
        do_delete_results(args)


if __name__ == "__main__":
    main()
