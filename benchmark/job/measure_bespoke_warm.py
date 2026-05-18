"""
Measure Bespoke JOB engine - warm execution (no recompile).
- 5 warmup + 10 measured runs
- Timing from stdout: "<run> | Execution ms: <time>"
- Core pinning handled by Bespoke internally (core 3)

Usage:
    python benchmark/job/measure_bespoke_warm.py
"""
import csv
import os
import re
import select
import statistics
import subprocess
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet"
RESULTS_DIR = BENCHMARK_DIR / "results"

sys.path.insert(0, str(BENCHMARK_DIR.parent.parent))
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS

WARMUP_RUNS = 5
MEASURED_RUNS = 10

TIMING_RE = re.compile(r"(\d+) \| Execution ms: ([\d.]+)")


class BespokeRunner:
    def __init__(self, build_dir, parquet_dir):
        self.build_dir = build_dir
        self.parquet_dir = parquet_dir
        self.proc = None
        self.p2c_w = None
        self.c2p_r = None

    def start(self):
        p2c_r, p2c_w = os.pipe()
        c2p_r, c2p_w = os.pipe()

        self.proc = subprocess.Popen(
            [str(self.build_dir / "db"), str(self.parquet_dir) + "/"],
            pass_fds=(p2c_r, c2p_w),
            env={
                **os.environ,
                "P2C_FD": str(p2c_r),
                "C2P_FD": str(c2p_w),
                "LD_LIBRARY_PATH": str(self.build_dir),
            },
            cwd=str(BENCHMARK_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        os.close(p2c_r)
        os.close(c2p_w)

        self.p2c_w = p2c_w
        self.c2p_r = c2p_r
        os.set_blocking(c2p_r, False)
        os.set_blocking(self.proc.stdout.fileno(), False)
        if self.proc.stderr:
            os.set_blocking(self.proc.stderr.fileno(), False)

    def send_queries(self, query_lines):
        for line in query_lines:
            self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()

    def run(self, timeout=300.0):
        os.write(self.p2c_w, b"run\n")

        out_buf = bytearray()
        resp_buf = bytearray()

        while True:
            fds = [self.c2p_r, self.proc.stdout.fileno()]
            if self.proc.stderr:
                fds.append(self.proc.stderr.fileno())

            rlist, _, _ = select.select(fds, [], [], timeout)
            if not rlist:
                raise TimeoutError(f"Bespoke run timed out after {timeout}s")

            for fd in rlist:
                if fd == self.c2p_r:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        resp_buf.extend(chunk)
                elif fd == self.proc.stdout.fileno():
                    chunk = os.read(fd, 65536)
                    if chunk:
                        out_buf.extend(chunk)
                else:
                    os.read(fd, 65536)

            if b"\n" in resp_buf:
                while True:
                    rlist2, _, _ = select.select([self.proc.stdout.fileno()], [], [], 0.1)
                    if not rlist2:
                        break
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    out_buf.extend(chunk)
                return out_buf.decode("utf-8", errors="replace")

    def stop(self):
        if self.proc is None:
            return
        try:
            os.write(self.p2c_w, b"stop\n")
        except OSError:
            pass
        try:
            os.close(self.p2c_w)
        except OSError:
            pass
        try:
            os.close(self.c2p_r)
        except OSError:
            pass
        self.proc.wait(timeout=10)
        self.proc = None


def parse_timings(stdout):
    timings = []
    for line in stdout.splitlines():
        m = TIMING_RE.match(line.strip())
        if m:
            timings.append(float(m.group(2)))
    return timings


def main():
    # JOB queries are static — just send query IDs
    query_lines = list(JOB_QUERY_IDS)
    print(f"Running {len(query_lines)} JOB queries")

    runner = BespokeRunner(BUILD_DIR, PARQUET_DIR)
    print("Starting Bespoke engine...")
    runner.start()

    print("  Initial run (load data + compile)...")
    runner.send_queries(query_lines)
    initial_out = runner.run(timeout=600)
    initial_timings = parse_timings(initial_out)
    print(f"  Initial run complete. Got {len(initial_timings)} timings.")

    print(f"  Warmup ({WARMUP_RUNS} runs)...")
    for w in range(WARMUP_RUNS):
        runner.send_queries(query_lines)
        runner.run(timeout=300)

    print(f"  Measuring ({MEASURED_RUNS} runs)...")
    all_timings = []
    for r in range(MEASURED_RUNS):
        runner.send_queries(query_lines)
        out = runner.run(timeout=300)
        timings = parse_timings(out)
        all_timings.append(timings)
        if len(timings) != len(query_lines):
            print(f"    WARNING: run {r+1} got {len(timings)} timings, expected {len(query_lines)}")

    runner.stop()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "bespoke_warm.csv"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "median_ms", "mean_ms", "stddev_ms"] +
                        [f"run_{i+1}" for i in range(MEASURED_RUNS)])

        for qi, qname in enumerate(query_lines):
            per_query_timings = [all_timings[r][qi] for r in range(MEASURED_RUNS)
                                 if qi < len(all_timings[r])]

            if not per_query_timings:
                print(f"    {qname}: NO DATA")
                continue

            median_ms = statistics.median(per_query_timings)
            mean_ms = statistics.mean(per_query_timings)
            stddev_ms = statistics.stdev(per_query_timings) if len(per_query_timings) > 1 else 0.0

            writer.writerow([qname, round(median_ms, 3), round(mean_ms, 3), round(stddev_ms, 3)] +
                            [round(t, 3) for t in per_query_timings])
            print(f"    {qname}: median={median_ms:.3f}ms mean={mean_ms:.3f}ms stddev={stddev_ms:.3f}ms")

    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
