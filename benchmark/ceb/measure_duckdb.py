"""
Step 5: Measure DuckDB query execution time.
- threads=1 (single-threaded) and default parallel
- 5 warmup + 10 measured runs per query
- Pin to core 3
- Timing via time.perf_counter (parse + optimize + execute, no data load)

Usage:
    python benchmark/ceb/measure_duckdb.py
"""
import csv
import os
import statistics
import time
from pathlib import Path

import duckdb

BENCHMARK_DIR = Path(__file__).resolve().parent
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet" / "sf2"
SQL_DIR = BENCHMARK_DIR / "sql"
RESULTS_DIR = BENCHMARK_DIR / "results"

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

WARMUP_RUNS = 5
MEASURED_RUNS = 10
PIN_CORE = 3


def load_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Load sf2 parquet files into in-memory DuckDB tables."""
    for table in TABLES:
        parquet_path = PARQUET_DIR / f"{table}.parquet"
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{parquet_path}')"
        )


def measure_query(con: duckdb.DuckDBPyConnection, sql: str) -> float:
    """Execute query and return latency in ms via perf_counter."""
    start = time.perf_counter()
    con.execute(sql).fetchall()
    return (time.perf_counter() - start) * 1000.0


def run_benchmark(threads_config: str) -> list[dict]:
    """Run all queries with a given thread configuration."""
    if threads_config == "1":
        os.sched_setaffinity(0, {PIN_CORE})
    else:
        os.sched_setaffinity(0, set(range(os.cpu_count())))

    con = duckdb.connect(":memory:")

    if threads_config == "1":
        con.execute("PRAGMA threads=1")

    print(f"  Loading tables into memory...")
    load_tables(con)
    print(f"  Tables loaded.")

    results = []
    for qname in QUERY_TEMPLATES:
        sql_path = SQL_DIR / f"{qname}.sql"
        sql = sql_path.read_text().strip()

        # Warmup
        for _ in range(WARMUP_RUNS):
            measure_query(con, sql)

        # Measured runs
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


def write_results(results: list[dict], filename: str) -> None:
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
