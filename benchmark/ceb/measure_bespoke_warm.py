"""
Step 6: Measure Bespoke CEB engine - warm execution (no recompile).
- 5 warmup + 10 measured runs per query
- Timing from stdout: "<run> | Execution ms: <time>"
- Core pinning handled by Bespoke internally (core 3)

Usage:
    python benchmark/ceb/measure_bespoke_warm.py
"""
import csv
import os
import re
import select
import statistics
import subprocess
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet" / "sf2"
QUERIES_FILE = BENCHMARK_DIR / "queries_bespoke.txt"
RESULTS_DIR = BENCHMARK_DIR / "results"

WARMUP_RUNS = 5
MEASURED_RUNS = 10

TIMING_RE = re.compile(r"(\d+) \| Execution ms: ([\d.]+)")


class BespokeRunner:
    """Manages the ./db subprocess with P2C/C2P pipe protocol."""

    def __init__(self, build_dir: Path, parquet_dir: Path):
        self.build_dir = build_dir
        self.parquet_dir = parquet_dir
        self.proc = None
        self.p2c_w = None
        self.c2p_r = None
        self.stdin = None
        self.stdout_fd = None

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
        self.stdin = self.proc.stdin
        self.stdout_fd = self.proc.stdout.fileno()
        os.set_blocking(self.stdout_fd, False)
        if self.proc.stderr:
            os.set_blocking(self.proc.stderr.fileno(), False)

    def send_queries(self, query_lines: list[str]):
        """Send query arg lines to stdin, followed by empty line."""
        for line in query_lines:
            self.stdin.write((line + "\n").encode())
        self.stdin.write(b"\n")
        self.stdin.flush()

    def run(self, timeout: float = 300.0) -> str:
        """Send 'run' command and wait for completion. Returns stdout text."""
        os.write(self.p2c_w, b"run\n")

        out_buf = bytearray()
        resp_buf = bytearray()
        stderr_buf = bytearray()

        while True:
            fds = [self.c2p_r, self.stdout_fd]
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
                elif fd == self.stdout_fd:
                    chunk = os.read(fd, 65536)
                    if chunk:
                        out_buf.extend(chunk)
                elif self.proc.stderr and fd == self.proc.stderr.fileno():
                    chunk = os.read(fd, 65536)
                    if chunk:
                        stderr_buf.extend(chunk)

            if b"\n" in resp_buf:
                # Drain remaining stdout
                while True:
                    rlist2, _, _ = select.select([self.stdout_fd], [], [], 0.1)
                    if not rlist2:
                        break
                    chunk = os.read(self.stdout_fd, 65536)
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


def parse_timings(stdout: str) -> list[float]:
    """Extract timing values from stdout."""
    timings = []
    for line in stdout.splitlines():
        m = TIMING_RE.match(line.strip())
        if m:
            timings.append(float(m.group(2)))
    return timings


def main():
    query_lines = QUERIES_FILE.read_text().strip().splitlines()
    print(f"Loaded {len(query_lines)} queries from {QUERIES_FILE.name}")

    runner = BespokeRunner(BUILD_DIR, PARQUET_DIR)
    print("Starting Bespoke engine...")
    runner.start()

    # Initial run: triggers loader + builder + query (first compilation)
    print("  Initial run (load data + compile)...")
    runner.send_queries(query_lines)
    initial_out = runner.run(timeout=600)
    initial_timings = parse_timings(initial_out)
    print(f"  Initial run complete. Got {len(initial_timings)} timings.")

    # Warmup runs (query re-executes without recompile)
    print(f"  Warmup ({WARMUP_RUNS} runs)...")
    for w in range(WARMUP_RUNS):
        runner.send_queries(query_lines)
        runner.run(timeout=300)

    # Measured runs
    print(f"  Measuring ({MEASURED_RUNS} runs)...")
    all_timings = []  # list of lists, one per run
    for r in range(MEASURED_RUNS):
        runner.send_queries(query_lines)
        out = runner.run(timeout=300)
        timings = parse_timings(out)
        all_timings.append(timings)
        if len(timings) != len(query_lines):
            print(f"    WARNING: run {r+1} got {len(timings)} timings, expected {len(query_lines)}")

    runner.stop()

    # Aggregate: per-query statistics across runs
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "bespoke_warm.csv"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "median_ms", "mean_ms", "stddev_ms"] +
                        [f"run_{i+1}" for i in range(MEASURED_RUNS)])

        for qi, qline in enumerate(query_lines):
            qname = qline.split()[0]
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
