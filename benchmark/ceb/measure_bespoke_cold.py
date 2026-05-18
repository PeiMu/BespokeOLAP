"""
Step 7: Measure Bespoke CEB engine - cold (compile + execute, no data load).
- For each run: delete libquery.so and libbuilder.so to force recompilation
- Loader stays cached (data already loaded from first warm-up run)
- 5 warmup + 10 measured runs per query
- Timing includes: recompilation (builder+query) + execution

Usage:
    python benchmark/ceb/measure_bespoke_cold.py
"""
import csv
import os
import re
import select
import shutil
import statistics
import subprocess
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet" / "sf2"
QUERIES_FILE = BENCHMARK_DIR / "queries_bespoke.txt"
RESULTS_DIR = BENCHMARK_DIR / "results"

WARMUP_RUNS = 5
MEASURED_RUNS = 10

TIMING_RE = re.compile(r"(\d+) \| Execution ms: ([\d.]+)")
INGEST_RE = re.compile(r"Ingest ms: ([\d.]+)")


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
        self.stderr_fd = None

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
        self.stderr_fd = self.proc.stderr.fileno()
        os.set_blocking(self.stderr_fd, False)

    def send_queries(self, query_lines: list[str]):
        for line in query_lines:
            self.stdin.write((line + "\n").encode())
        self.stdin.write(b"\n")
        self.stdin.flush()

    def run(self, timeout: float = 600.0) -> tuple[str, str]:
        """Send 'run' and wait. Returns (stdout, stderr)."""
        os.write(self.p2c_w, b"run\n")

        out_buf = bytearray()
        err_buf = bytearray()
        resp_buf = bytearray()

        while True:
            fds = [self.c2p_r, self.stdout_fd, self.stderr_fd]
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
                elif fd == self.stderr_fd:
                    chunk = os.read(fd, 65536)
                    if chunk:
                        err_buf.extend(chunk)

            if b"\n" in resp_buf:
                # Drain remaining stdout/stderr
                for _ in range(10):
                    rlist2, _, _ = select.select([self.stdout_fd, self.stderr_fd], [], [], 0.1)
                    if not rlist2:
                        break
                    for fd in rlist2:
                        chunk = os.read(fd, 65536)
                        if chunk:
                            if fd == self.stdout_fd:
                                out_buf.extend(chunk)
                            else:
                                err_buf.extend(chunk)
                return (
                    out_buf.decode("utf-8", errors="replace"),
                    err_buf.decode("utf-8", errors="replace"),
                )

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


def force_recompile(build_dir: Path):
    """Delete .so files to force OnChange stages to recompile."""
    for so_name in ["libbuilder.so", "libquery.so"]:
        so_path = build_dir / so_name
        if so_path.exists():
            os.remove(so_path)

    # Rebuild them from scratch using the build script objects
    proj_root = BENCHMARK_DIR.parent.parent
    ceb_src = proj_root / "BespokeOLAP_Artifacts" / "bespoke_ceb"
    api_dir = proj_root / "misc" / "fasttest"

    pkg_cflags = subprocess.check_output(
        ["pkg-config", "--cflags", "arrow", "parquet"], text=True
    ).strip()
    pkg_libs = subprocess.check_output(
        ["pkg-config", "--libs", "arrow", "parquet"], text=True
    ).strip()

    cxx = os.environ.get("CXX", "g++")
    cxxflags = f"-g -std=c++20 -fPIC -O3 -flto -I{ceb_src} -I{api_dir}"
    ldflags_so = "-shared -Wl,--build-id=sha1"

    obj_dir = build_dir / "obj"

    # Recompile builder
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {api_dir}/builder_api.cpp -o {obj_dir}/builder_api.o",
        shell=True, check=True, capture_output=True,
    )
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {ceb_src}/builder_impl.cpp -o {obj_dir}/builder_impl.o",
        shell=True, check=True, capture_output=True,
    )
    subprocess.run(
        f"{cxx} {ldflags_so} -o {build_dir}/libbuilder.so {obj_dir}/builder_api.o {obj_dir}/builder_impl.o {pkg_libs}",
        shell=True, check=True, capture_output=True,
    )

    # Recompile query
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {api_dir}/query_api.cpp -o {obj_dir}/query_api.o",
        shell=True, check=True, capture_output=True,
    )
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {ceb_src}/query_impl.cpp -o {obj_dir}/query_impl.o",
        shell=True, check=True, capture_output=True,
    )
    subprocess.run(
        f"{cxx} {ldflags_so} -o {build_dir}/libquery.so {obj_dir}/query_api.o {obj_dir}/query_impl.o {pkg_libs}",
        shell=True, check=True, capture_output=True,
    )


def parse_timings(stdout: str) -> list[float]:
    timings = []
    for line in stdout.splitlines():
        m = TIMING_RE.match(line.strip())
        if m:
            timings.append(float(m.group(2)))
    return timings


def parse_ingest_time(stderr: str) -> float:
    """Extract builder ingest time from stderr."""
    for line in stderr.splitlines():
        m = INGEST_RE.search(line)
        if m:
            return float(m.group(1))
    return 0.0


def main():
    query_lines = QUERIES_FILE.read_text().strip().splitlines()
    print(f"Loaded {len(query_lines)} queries from {QUERIES_FILE.name}")

    runner = BespokeRunner(BUILD_DIR, PARQUET_DIR)
    print("Starting Bespoke engine...")
    runner.start()

    # Initial run: warm up loader (loads data)
    print("  Initial run (load data + compile)...")
    runner.send_queries(query_lines)
    runner.run(timeout=600)
    print("  Data loaded. Loader cached for subsequent runs.")

    # For cold measurement: each run includes recompile + execute
    total_runs = WARMUP_RUNS + MEASURED_RUNS
    all_compile_times = []
    all_exec_timings = []  # list of (compile_ms, [exec_ms per query])

    for i in range(total_runs):
        phase = "warmup" if i < WARMUP_RUNS else "measured"
        run_num = i + 1

        # Force recompile by rebuilding .so files
        compile_start = time.perf_counter()
        force_recompile(BUILD_DIR)
        compile_ms = (time.perf_counter() - compile_start) * 1000.0

        # Run with freshly compiled .so (OnChange detects new .so)
        runner.send_queries(query_lines)
        stdout, stderr = runner.run(timeout=600)
        exec_timings = parse_timings(stdout)
        ingest_ms = parse_ingest_time(stderr)

        total_ms = compile_ms + ingest_ms + sum(exec_timings)
        print(f"    [{phase}] run {run_num}: compile={compile_ms:.1f}ms ingest={ingest_ms:.1f}ms exec_total={sum(exec_timings):.1f}ms")

        if i >= WARMUP_RUNS:
            all_compile_times.append(compile_ms + ingest_ms)
            all_exec_timings.append(exec_timings)

    runner.stop()

    # Write results: per-query cold time = compile_share + exec
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "bespoke_cold.csv"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "median_ms", "mean_ms", "stddev_ms",
                         "compile_median_ms", "compile_mean_ms"] +
                        [f"run_{i+1}" for i in range(MEASURED_RUNS)])

        # Report compile overhead once (shared across all queries)
        compile_median = statistics.median(all_compile_times)
        compile_mean = statistics.mean(all_compile_times)
        print(f"\n  Compile overhead: median={compile_median:.1f}ms mean={compile_mean:.1f}ms")

        for qi, qline in enumerate(query_lines):
            qname = qline.split()[0]

            # Cold time per query = compile + ingest + execution
            cold_timings = [
                all_compile_times[r] + all_exec_timings[r][qi]
                for r in range(MEASURED_RUNS)
                if qi < len(all_exec_timings[r])
            ]

            if not cold_timings:
                continue

            median_ms = statistics.median(cold_timings)
            mean_ms = statistics.mean(cold_timings)
            stddev_ms = statistics.stdev(cold_timings) if len(cold_timings) > 1 else 0.0

            writer.writerow([qname, round(median_ms, 3), round(mean_ms, 3), round(stddev_ms, 3),
                             round(compile_median, 3), round(compile_mean, 3)] +
                            [round(t, 3) for t in cold_timings])
            print(f"    {qname}: cold_median={median_ms:.1f}ms")

    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
