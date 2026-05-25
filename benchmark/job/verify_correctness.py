"""
Verify Bespoke JOB engine output correctness against DuckDB golden results.

Usage:
    python benchmark/job/verify_correctness.py
"""
import csv
import os
import re
import select
import subprocess
import sys
from pathlib import Path

import duckdb

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet"
SQL_DIR = Path("/home/pei/Project/benchmarks/imdb_job-postgres/queries")
GOLDEN_DIR = BENCHMARK_DIR / "golden"

sys.path.insert(0, str(BENCHMARK_DIR.parent.parent))
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS

TABLES = [
    "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type",
    "company_name", "company_type", "complete_cast", "info_type", "keyword",
    "kind_type", "link_type", "movie_companies", "movie_info", "movie_info_idx",
    "movie_keyword", "movie_link", "name", "person_info", "role_type", "title",
]


def generate_golden():
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=1")

    print("  Loading tables...")
    for table in TABLES:
        parquet_path = PARQUET_DIR / f"{table}.parquet"
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{parquet_path}')")

    print("  Running queries...")
    for qname in JOB_QUERY_IDS:
        sql_path = SQL_DIR / f"{qname}.sql"
        sql = sql_path.read_text().strip()

        result = con.execute(sql).fetchdf()
        golden_path = GOLDEN_DIR / f"{qname}.csv"
        result.to_csv(golden_path, index=False)
        print(f"    {qname}: {len(result)} rows -> {golden_path.name}")

    con.close()


def run_bespoke():
    # JOB queries are static — each query ID is sent as-is (no parameters)
    query_lines = [qid for qid in JOB_QUERY_IDS]

    p2c_r, p2c_w = os.pipe()
    c2p_r, c2p_w = os.pipe()

    proc = subprocess.Popen(
        [str(BUILD_DIR / "db"), str(PARQUET_DIR) + "/"],
        pass_fds=(p2c_r, c2p_w),
        env={
            **os.environ,
            "P2C_FD": str(p2c_r),
            "C2P_FD": str(c2p_w),
            "LD_LIBRARY_PATH": str(BUILD_DIR),
        },
        cwd=str(BENCHMARK_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(p2c_r)
    os.close(c2p_w)
    os.set_blocking(c2p_r, False)
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    for line in query_lines:
        proc.stdin.write((line + "\n").encode())
    proc.stdin.write(b"\n")
    proc.stdin.flush()

    os.write(p2c_w, b"run\n")

    resp_buf = bytearray()
    while True:
        rlist, _, _ = select.select([c2p_r, proc.stdout.fileno(), proc.stderr.fileno()], [], [], 600)
        if not rlist:
            raise TimeoutError("Bespoke timed out")
        for fd in rlist:
            if fd == c2p_r:
                chunk = os.read(fd, 4096)
                if chunk:
                    resp_buf.extend(chunk)
            else:
                os.read(fd, 65536)
        if b"\n" in resp_buf:
            break

    os.write(p2c_w, b"stop\n")
    os.close(p2c_w)
    os.close(c2p_r)
    proc.wait(timeout=30)


def compare_results():
    results = []

    for qi, qname in enumerate(JOB_QUERY_IDS):
        golden_path = GOLDEN_DIR / f"{qname}.csv"
        bespoke_path = BENCHMARK_DIR / f"result{qi + 1}.csv"

        if not golden_path.exists():
            results.append((qname, "SKIP", "no golden"))
            continue
        if not bespoke_path.exists():
            results.append((qname, "FAIL", "no bespoke result"))
            continue

        golden_rows = _read_csv_sorted(golden_path)
        bespoke_rows = _read_csv_sorted(bespoke_path)

        if len(golden_rows) != len(bespoke_rows):
            results.append((qname, "FAIL", f"row count: golden={len(golden_rows)} bespoke={len(bespoke_rows)}"))
            continue

        mismatches = 0
        for gi, (grow, brow) in enumerate(zip(golden_rows, bespoke_rows)):
            if not _rows_match(grow, brow):
                mismatches += 1
                if mismatches <= 3:
                    print(f"    {qname} row {gi}: golden={grow} bespoke={brow}")

        if mismatches == 0:
            results.append((qname, "PASS", f"{len(golden_rows)} rows match"))
        else:
            results.append((qname, "FAIL", f"{mismatches}/{len(golden_rows)} rows differ"))

    return results


def _read_csv_sorted(path: Path, escapechar=None):
    with open(path) as f:
        if escapechar:
            reader = csv.reader(f, escapechar=escapechar, doublequote=False)
        else:
            reader = csv.reader(f)
        header = next(reader, None)
        rows = [row for row in reader]
    rows.sort()
    return rows


def _rows_match(row1, row2, tol=0.01):
    if len(row1) != len(row2):
        return False
    for v1, v2 in zip(row1, row2):
        if v1 == v2:
            continue
        v1n = v1.replace('\\"', '"').replace('\\\\', '\\')
        v2n = v2.replace('\\"', '"').replace('\\\\', '\\')
        if v1n == v2n:
            continue
        try:
            f1, f2 = float(v1), float(v2)
            if abs(f1 - f2) > tol * max(abs(f1), abs(f2), 1.0):
                return False
        except ValueError:
            return False
    return True


def main():
    print("Step 1: Generate DuckDB golden results")
    generate_golden()

    print("\nStep 2: Run Bespoke engine")
    run_bespoke()

    print("\nStep 3: Compare results")
    results = compare_results()

    print("\n" + "=" * 50)
    print("CORRECTNESS SUMMARY")
    print("=" * 50)
    pass_count = 0
    for qname, status, detail in results:
        icon = "OK" if status == "PASS" else "XX"
        print(f"  [{icon}] {qname:5s} {status:5s} - {detail}")
        if status == "PASS":
            pass_count += 1

    print(f"\n  {pass_count}/{len(results)} queries passed")


if __name__ == "__main__":
    main()
