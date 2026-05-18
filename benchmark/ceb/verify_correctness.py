"""
Step 4: Verify Bespoke output correctness against DuckDB golden results.
- Run each SQL in DuckDB, save golden CSV
- Run queries through Bespoke, compare output

Usage:
    python benchmark/ceb/verify_correctness.py
"""
import csv
import os
import re
import select
import subprocess
from pathlib import Path

import duckdb

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet" / "sf2"
SQL_DIR = BENCHMARK_DIR / "sql"
QUERIES_FILE = BENCHMARK_DIR / "queries_bespoke.txt"
GOLDEN_DIR = BENCHMARK_DIR / "golden"

TABLES = [
    "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type",
    "company_name", "company_type", "complete_cast", "info_type", "keyword",
    "kind_type", "link_type", "movie_companies", "movie_info", "movie_info_idx",
    "movie_keyword", "movie_link", "name", "person_info", "role_type", "title",
]

QUERY_TEMPLATES = [
    "1a", "2a", "2b", "2c", "3a", "3b", "4a", "5a",
    "6a", "7a", "8a", "9a", "9b", "10a", "11a", "11b",
]


def generate_golden():
    """Run SQL queries in DuckDB and save golden results."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=1")

    print("  Loading tables...")
    for table in TABLES:
        parquet_path = PARQUET_DIR / f"{table}.parquet"
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{parquet_path}')")

    print("  Running queries...")
    for qname in QUERY_TEMPLATES:
        sql_path = SQL_DIR / f"{qname}.sql"
        sql = sql_path.read_text().strip()

        result = con.execute(sql).fetchdf()
        golden_path = GOLDEN_DIR / f"{qname}.csv"
        result.to_csv(golden_path, index=False)
        print(f"    {qname}: {len(result)} rows -> {golden_path.name}")

    con.close()


def run_bespoke():
    """Run Bespoke engine and collect result CSVs."""
    query_lines = QUERIES_FILE.read_text().strip().splitlines()

    # Start db process
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

    # Send queries
    for line in query_lines:
        proc.stdin.write((line + "\n").encode())
    proc.stdin.write(b"\n")
    proc.stdin.flush()

    # Trigger run
    os.write(p2c_w, b"run\n")

    # Wait for completion
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
                os.read(fd, 65536)  # drain
        if b"\n" in resp_buf:
            break

    # Stop
    os.write(p2c_w, b"stop\n")
    os.close(p2c_w)
    os.close(c2p_r)
    proc.wait(timeout=10)


def compare_results():
    """Compare Bespoke result CSVs against golden."""
    results = []

    for qi, qname in enumerate(QUERY_TEMPLATES):
        golden_path = GOLDEN_DIR / f"{qname}.csv"
        bespoke_path = BENCHMARK_DIR / f"result{qi + 1}.csv"

        if not golden_path.exists():
            results.append((qname, "SKIP", "no golden"))
            continue
        if not bespoke_path.exists():
            results.append((qname, "FAIL", "no bespoke result"))
            continue

        golden_rows = _read_csv_sorted(golden_path)
        bespoke_rows = _read_csv_sorted(bespoke_path, escapechar="\\")

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


def _read_csv_sorted(path: Path, escapechar: str | None = None) -> list[list[str]]:
    """Read CSV and sort rows for order-independent comparison."""
    with open(path) as f:
        if escapechar:
            reader = csv.reader(f, escapechar=escapechar, doublequote=False)
        else:
            reader = csv.reader(f)
        header = next(reader, None)
        rows = [row for row in reader]
    rows.sort()
    return rows


def _normalize_quotes(s: str) -> str:
    """Normalize backslash-escaped quotes to match DuckDB's CSV output."""
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _rows_match(row1: list[str], row2: list[str], tol: float = 0.01) -> bool:
    """Compare two CSV rows with tolerance for numeric values and quote escaping."""
    if len(row1) != len(row2):
        return False
    for v1, v2 in zip(row1, row2):
        if v1 == v2:
            continue
        if _normalize_quotes(v1) == _normalize_quotes(v2):
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
