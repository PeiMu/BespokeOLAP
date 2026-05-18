"""
Aggregate all JOB benchmark results into a summary table.

Usage:
    python benchmark/job/aggregate_results.py
"""
import csv
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS


def load_csv(filename):
    path = RESULTS_DIR / filename
    if not path.exists():
        return {}
    data = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            data[row["query"]] = row
    return data


def main():
    duckdb_1t = load_csv("duckdb_threads1.csv")
    duckdb_par = load_csv("duckdb_parallel.csv")
    bespoke_warm = load_csv("bespoke_warm.csv")
    bespoke_cold = load_csv("bespoke_cold.csv")

    summary_path = RESULTS_DIR / "summary.csv"
    with open(summary_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "query",
            "duckdb_1thread_ms",
            "duckdb_parallel_ms",
            "bespoke_warm_ms",
            "bespoke_cold_ms",
            "speedup_vs_duckdb_1t",
            "speedup_vs_duckdb_par",
        ])

        for q in JOB_QUERY_IDS:
            d1 = float(duckdb_1t.get(q, {}).get("median_ms", 0)) or None
            dp = float(duckdb_par.get(q, {}).get("median_ms", 0)) or None
            bw = float(bespoke_warm.get(q, {}).get("median_ms", 0)) or None
            bc = float(bespoke_cold.get(q, {}).get("median_ms", 0)) or None

            speedup_1t = f"{d1 / bw:.2f}x" if d1 and bw else "N/A"
            speedup_par = f"{dp / bw:.2f}x" if dp and bw else "N/A"

            writer.writerow([
                q,
                f"{d1:.3f}" if d1 else "N/A",
                f"{dp:.3f}" if dp else "N/A",
                f"{bw:.3f}" if bw else "N/A",
                f"{bc:.3f}" if bc else "N/A",
                speedup_1t,
                speedup_par,
            ])

    print(f"{'Query':<8} {'DuckDB 1T':>12} {'DuckDB Par':>12} {'Bespoke Warm':>14} {'Bespoke Cold':>14} {'Speedup/1T':>12} {'Speedup/Par':>12}")
    print("-" * 90)

    for q in JOB_QUERY_IDS:
        d1 = float(duckdb_1t.get(q, {}).get("median_ms", 0)) or None
        dp = float(duckdb_par.get(q, {}).get("median_ms", 0)) or None
        bw = float(bespoke_warm.get(q, {}).get("median_ms", 0)) or None
        bc = float(bespoke_cold.get(q, {}).get("median_ms", 0)) or None

        speedup_1t = f"{d1 / bw:.1f}x" if d1 and bw else "N/A"
        speedup_par = f"{dp / bw:.1f}x" if dp and bw else "N/A"

        print(f"{q:<8} {d1 or 0:>12.3f} {dp or 0:>12.3f} {bw or 0:>14.3f} {bc or 0:>14.3f} {speedup_1t:>12} {speedup_par:>12}")

    print(f"\nSummary written to {summary_path}")


if __name__ == "__main__":
    main()
