"""
Measure Bespoke JOB engine - cold (compile + execute, data stays loaded).
- Delete libquery.so and libbuilder.so to force recompilation each run
- 5 warmup + 10 measured runs
- Timing includes: recompilation (builder+query) + execution

Usage:
    python benchmark/job/measure_bespoke_cold.py
"""
import csv
import os
import re
import select
import statistics
import subprocess
import sys
import time
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
INGEST_RE = re.compile(r"Ingest ms: ([\d.]+)")


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
        os.set_blocking(self.proc.stderr.fileno(), False)

    def send_queries(self, query_lines):
        for line in query_lines:
            self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()

    def run(self, timeout=600.0):
        os.write(self.p2c_w, b"run\n")

        out_buf = bytearray()
        err_buf = bytearray()
        resp_buf = bytearray()

        while True:
            fds = [self.c2p_r, self.proc.stdout.fileno(), self.proc.stderr.fileno()]
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
                    chunk = os.read(fd, 65536)
                    if chunk:
                        err_buf.extend(chunk)

            if b"\n" in resp_buf:
                for _ in range(10):
                    rlist2, _, _ = select.select(
                        [self.proc.stdout.fileno(), self.proc.stderr.fileno()], [], [], 0.1)
                    if not rlist2:
                        break
                    for fd in rlist2:
                        chunk = os.read(fd, 65536)
                        if chunk:
                            if fd == self.proc.stdout.fileno():
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


def force_recompile(build_dir):
    for so_name in ["libbuilder.so", "libquery.so"]:
        so_path = build_dir / so_name
        if so_path.exists():
            os.remove(so_path)

    proj_root = BENCHMARK_DIR.parent.parent
    job_src = proj_root / "output"
    api_dir = proj_root / "misc" / "fasttest"

    pkg_cflags = subprocess.check_output(
        ["pkg-config", "--cflags", "arrow", "parquet"], text=True).strip()
    pkg_libs = subprocess.check_output(
        ["pkg-config", "--libs", "arrow", "parquet"], text=True).strip()

    cxx = os.environ.get("CXX", "g++")
    cxxflags = f"-g -std=c++20 -fPIC -O3 -flto -march=native -I{job_src} -I{api_dir}"
    ldflags_so = "-shared -Wl,--build-id=sha1"
    obj_dir = build_dir / "obj"

    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {api_dir}/builder_api.cpp -o {obj_dir}/builder_api.o",
        shell=True, check=True, capture_output=True)
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {job_src}/builder_impl.cpp -o {obj_dir}/builder_impl.o",
        shell=True, check=True, capture_output=True)
    subprocess.run(
        f"{cxx} {ldflags_so} -o {build_dir}/libbuilder.so {obj_dir}/builder_api.o {obj_dir}/builder_impl.o {pkg_libs}",
        shell=True, check=True, capture_output=True)

    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {api_dir}/query_api.cpp -o {obj_dir}/query_api.o",
        shell=True, check=True, capture_output=True)
    subprocess.run(
        f"{cxx} {cxxflags} {pkg_cflags} -c {job_src}/query_impl.cpp -o {obj_dir}/query_impl.o",
        shell=True, check=True, capture_output=True)

    query_objs = f"{obj_dir}/query_api.o {obj_dir}/query_impl.o"
    for qsrc in sorted(job_src.glob("query_q*.cpp")):
        obj_name = qsrc.stem + ".o"
        obj_path = obj_dir / obj_name
        subprocess.run(
            f"{cxx} {cxxflags} {pkg_cflags} -c {qsrc} -o {obj_path}",
            shell=True, check=True, capture_output=True)
        query_objs += f" {obj_path}"

    subprocess.run(
        f"{cxx} {ldflags_so} -o {build_dir}/libquery.so {query_objs} {pkg_libs}",
        shell=True, check=True, capture_output=True)


def parse_timings(stdout):
    timings = []
    for line in stdout.splitlines():
        m = TIMING_RE.match(line.strip())
        if m:
            timings.append(float(m.group(2)))
    return timings


def parse_ingest_time(stderr):
    for line in stderr.splitlines():
        m = INGEST_RE.search(line)
        if m:
            return float(m.group(1))
    return 0.0


def main():
    query_lines = list(JOB_QUERY_IDS)
    print(f"Running {len(query_lines)} JOB queries (cold measurement)")

    runner = BespokeRunner(BUILD_DIR, PARQUET_DIR)
    print("Starting Bespoke engine...")
    runner.start()

    print("  Initial run (load data + compile)...")
    runner.send_queries(query_lines)
    runner.run(timeout=600)
    print("  Data loaded. Loader cached for subsequent runs.")

    total_runs = WARMUP_RUNS + MEASURED_RUNS
    all_compile_times = []
    all_exec_timings = []

    for i in range(total_runs):
        phase = "warmup" if i < WARMUP_RUNS else "measured"

        compile_start = time.perf_counter()
        force_recompile(BUILD_DIR)
        compile_ms = (time.perf_counter() - compile_start) * 1000.0

        runner.send_queries(query_lines)
        stdout, stderr = runner.run(timeout=600)
        exec_timings = parse_timings(stdout)
        ingest_ms = parse_ingest_time(stderr)

        print(f"    [{phase}] run {i+1}: compile={compile_ms:.1f}ms ingest={ingest_ms:.1f}ms exec_total={sum(exec_timings):.1f}ms")

        if i >= WARMUP_RUNS:
            all_compile_times.append(compile_ms + ingest_ms)
            all_exec_timings.append(exec_timings)

    runner.stop()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / "bespoke_cold.csv"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "cold_median_ms", "cold_mean_ms", "cold_stddev_ms",
                         "exec_median_ms", "exec_mean_ms", "exec_stddev_ms",
                         "compile_median_ms", "compile_mean_ms"] +
                        [f"run_{i+1}" for i in range(MEASURED_RUNS)])

        compile_median = statistics.median(all_compile_times)
        compile_mean = statistics.mean(all_compile_times)
        print(f"\n  Compile overhead: median={compile_median:.1f}ms mean={compile_mean:.1f}ms")

        for qi, qname in enumerate(query_lines):
            exec_times = [
                all_exec_timings[r][qi]
                for r in range(MEASURED_RUNS)
                if qi < len(all_exec_timings[r])
            ]
            cold_timings = [
                all_compile_times[r] + all_exec_timings[r][qi]
                for r in range(MEASURED_RUNS)
                if qi < len(all_exec_timings[r])
            ]

            if not cold_timings:
                continue

            cold_median = statistics.median(cold_timings)
            cold_mean = statistics.mean(cold_timings)
            cold_stddev = statistics.stdev(cold_timings) if len(cold_timings) > 1 else 0.0

            exec_median = statistics.median(exec_times)
            exec_mean = statistics.mean(exec_times)
            exec_stddev = statistics.stdev(exec_times) if len(exec_times) > 1 else 0.0

            writer.writerow([qname,
                             round(cold_median, 3), round(cold_mean, 3), round(cold_stddev, 3),
                             round(exec_median, 3), round(exec_mean, 3), round(exec_stddev, 3),
                             round(compile_median, 3), round(compile_mean, 3)] +
                            [round(t, 3) for t in cold_timings])
            print(f"    {qname}: cold={cold_median:.1f}ms exec={exec_median:.3f}ms")

    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
