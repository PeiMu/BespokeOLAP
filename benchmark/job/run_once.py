#!/usr/bin/env python3
"""Run all JOB queries once through the Bespoke engine (warm).

Starts the db process, loads data, runs queries once, then exits.
Designed to be called by hyperfine for benchmarking.

Usage:
    python benchmark/job/run_once.py              # cold: includes data loading
    python benchmark/job/run_once.py --warm        # warm: pre-loads data, measures execution only
"""
import os
import select
import subprocess
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
BUILD_DIR = BENCHMARK_DIR / "build"
PARQUET_DIR = BENCHMARK_DIR / "imdb_parquet"

sys.path.insert(0, str(BENCHMARK_DIR.parent.parent))
from dataset.gen_job.gen_job_query import JOB_QUERY_IDS


def run_once(warm=False):
    query_lines = list(JOB_QUERY_IDS)

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

    def send_queries():
        for line in query_lines:
            proc.stdin.write((line + "\n").encode())
        proc.stdin.write(b"\n")
        proc.stdin.flush()

    def wait_for_done():
        resp_buf = bytearray()
        while True:
            fds = [c2p_r, proc.stdout.fileno(), proc.stderr.fileno()]
            rlist, _, _ = select.select(fds, [], [], 600)
            if not rlist:
                raise TimeoutError("Timed out")
            for fd in rlist:
                if fd == c2p_r:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        resp_buf.extend(chunk)
                else:
                    os.read(fd, 65536)
            if b"\n" in resp_buf:
                return

    if warm:
        send_queries()
        wait_for_done()

    send_queries()
    wait_for_done()

    os.write(p2c_w, b"stop\n")
    os.close(p2c_w)
    os.close(c2p_r)
    proc.wait(timeout=30)


if __name__ == "__main__":
    run_once(warm="--warm" in sys.argv)
