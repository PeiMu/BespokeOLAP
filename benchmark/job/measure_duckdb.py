"""
Measure DuckDB query execution time on vanilla IMDB (JOB benchmark).
- threads=1 (single-threaded) and default parallel
- 5 warmup + 10 measured runs per query
- Pin to core 3

Usage:
    python benchmark/job/measure_duckdb.py
"""
import csv
import os
import statistics
import sys
import time
from pathlib import Path

import duckdb

BENCHMARK_DIR = Path(__file__).resolve().parent
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet"
SQL_DIR = Path("/home/pei/Project/benchmarks/imdb_job-postgres/queries")
RESULTS_DIR = BENCHMARK_DIR / "results"

sys.path.insert(0, str(BENCHMARK_DIR.parent.parent))
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS

TABLES = [
    "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type",
    "company_name", "company_type", "complete_cast", "info_type", "keyword",
    "kind_type", "link_type", "movie_companies", "movie_info", "movie_info_idx",
    "movie_keyword", "movie_link", "name", "person_info", "role_type", "title",
]

WARMUP_RUNS = 5
MEASURED_RUNS = 10
PIN_CORE = 3


def load_tables(con):
    for table in TABLES:
        parquet_path = PARQUET_DIR / f"{table}.parquet"
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{parquet_path}')")


def measure_query(con, sql):
    start = time.perf_counter()
    con.execute(sql).fetchall()
    return (time.perf_counter() - start) * 1000.0


def run_benchmark(threads_config):
    if threads_config == "1":
        os.sched_setaffinity(0, {PIN_CORE})
    else:
        os.sched_setaffinity(0, set(range(os.cpu_count())))

    con = duckdb.connect(":memory:")
    if threads_config == "1":
        con.execute("PRAGMA threads=1")

    print("  Loading tables into memory...")
    load_tables(con)
    print("  Tables loaded.")

    results = []
    for qname in JOB_QUERY_IDS:
        sql_path = SQL_DIR / f"{qname}.sql"
        sql = sql_path.read_text().strip()

        for _ in range(WARMUP_RUNS):
            measure_query(con, sql)

        timings = []
        for _ in range(MEASURED_RUNS):
            t = measure_query(con, sql)
            timings.append(t)

        median_ms = statistics.median(timings)
        mean_ms = statistics.mean(timings)
        stddev_ms = statistics.stdev(timings) if len(timings) > 1 else 0.0

        results.append({
            "query": qname,
            "median_ms": round(median_ms, 3),
            "mean_ms": round(mean_ms, 3),
            "stddev_ms": round(stddev_ms, 3),
            "runs": timings,
        })
        print(f"    {qname}: median={median_ms:.1f}ms mean={mean_ms:.1f}ms stddev={stddev_ms:.1f}ms")

    con.close()
    return results


def write_results(results, filename):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / filename

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "median_ms", "mean_ms", "stddev_ms"] +
                        [f"run_{i+1}" for i in range(MEASURED_RUNS)])
        for r in results:
            writer.writerow([r["query"], r["median_ms"], r["mean_ms"], r["stddev_ms"]] +
                            [round(t, 3) for t in r["runs"]])

    print(f"  Results written to {output_path}")


def main():
    print("DuckDB Benchmark (threads=1)")
    results_1 = run_benchmark("1")
    write_results(results_1, "duckdb_threads1.csv")

    print("\nDuckDB Benchmark (default parallel)")
    results_par = run_benchmark("default")
    write_results(results_par, "duckdb_parallel.csv")


if __name__ == "__main__":
    main()
