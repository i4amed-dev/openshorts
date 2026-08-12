#!/usr/bin/env python3
"""Measure what one Autopilot source actually costs on this machine.

Run it against a real source before trusting an unattended schedule:

    docker compose exec backend python ops/benchmark.py --url "https://youtu.be/..."
    docker compose exec backend python ops/benchmark.py --input /app/uploads/sample.mp4

It samples RSS and CPU while the *real* pipeline runs (``main.py``, the same
subprocess ``/api/process`` starts) and reports peak memory, wall time and disk
growth. The point is not a speed score — it is the three numbers that decide
whether a Mac survives a month of this:

  * **peak memory** vs. the container's limit (an OOM kill mid-render loses the job),
  * **wall time per source** vs. the discovery interval (slower than the schedule
    means an ever-growing queue),
  * **disk per source** vs. free space and the retention window.

Nothing here tunes the pipeline. If a benchmark suggests changing WHISPER_MODEL,
DETECT_STRIDE or the encoder, change one at a time and re-run — those defaults
were chosen for output quality, and "faster" is not on its own a reason.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _rss_bytes(pid):
    """Resident set size of a process tree, in bytes (Linux /proc)."""
    total = 0
    for target in _descendants(pid):
        try:
            with open(f"/proc/{target}/statm") as handle:
                pages = int(handle.read().split()[1])
            total += pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    return total


def _descendants(pid):
    """pid plus every child, so FFmpeg workers are counted too."""
    found = [pid]
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as handle:
            children = [int(x) for x in handle.read().split()]
    except (OSError, ValueError):
        children = []
    for child in children:
        found.extend(_descendants(child))
    return found


def _cpu_percent_sampler(pid, stop_event, samples):
    """Sample CPU time deltas of the process tree."""
    last_cpu, last_wall = None, None
    while not stop_event.is_set():
        total_ticks = 0
        for target in _descendants(pid):
            try:
                with open(f"/proc/{target}/stat") as handle:
                    fields = handle.read().rsplit(") ", 1)[-1].split()
                total_ticks += int(fields[11]) + int(fields[12])  # utime + stime
            except (OSError, ValueError, IndexError):
                continue
        now = time.time()
        if last_cpu is not None and now > last_wall:
            hz = os.sysconf("SC_CLK_TCK")
            used = (total_ticks - last_cpu) / hz
            samples.append(100.0 * used / (now - last_wall))
        last_cpu, last_wall = total_ticks, now
        stop_event.wait(1.0)


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _container_memory_limit():
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as handle:
                raw = handle.read().strip()
            if raw and raw != "max":
                value = int(raw)
                if value < 2 ** 62:
                    return value
        except (OSError, ValueError):
            continue
    return None


def run(args) -> int:
    output_dir = os.path.join(args.output_root, f"bench_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)

    cmd = [sys.executable, "-u", "main.py"]
    cmd.extend(["-u", args.url] if args.url else ["-i", args.input])
    cmd.extend(["-o", output_dir, "--format", args.format])

    env = os.environ.copy()
    if not env.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set — the analysis stage will fail.", file=sys.stderr)
        return 2

    idle_rss = _rss_bytes(os.getpid())
    limit = _container_memory_limit()
    disk_before = _dir_size(args.output_root)

    print(f"▶ benchmarking: {' '.join(cmd)}")
    print(f"  idle RSS of this process: {idle_rss / 1024**2:.0f} MB")
    if limit:
        print(f"  container memory limit:   {limit / 1024**3:.1f} GB")

    started = time.time()
    process = subprocess.Popen(cmd, cwd=REPO, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    peak = 0
    stage_peaks = {}
    current_stage = "startup"
    cpu_samples = []
    stop = threading.Event()
    threading.Thread(target=_cpu_percent_sampler,
                     args=(process.pid, stop, cpu_samples), daemon=True).start()

    def sample_memory():
        nonlocal peak
        while not stop.is_set():
            rss = _rss_bytes(process.pid)
            peak = max(peak, rss)
            stage_peaks[current_stage] = max(stage_peaks.get(current_stage, 0), rss)
            stop.wait(0.5)

    threading.Thread(target=sample_memory, daemon=True).start()

    # Stage attribution comes from the pipeline's own log lines, so a memory
    # spike can be blamed on transcription vs. reframing rather than guessed at.
    STAGES = {
        "Downloading video": "download",
        "Transcribing": "transcription",
        "Whisper": "transcription",
        "scene": "scene detection",
        "Found": "analysis",
        "clip": "clip render",
        "vertical": "reframing",
    }

    for raw in process.stdout:
        line = raw.decode(errors="replace").rstrip()
        for needle, stage in STAGES.items():
            if needle.lower() in line.lower():
                current_stage = stage
                break
        if args.verbose:
            print("   " + line[:160])

    process.wait()
    stop.set()
    elapsed = time.time() - started
    disk_after = _dir_size(args.output_root)
    job_bytes = _dir_size(output_dir)

    clips = len([n for n in os.listdir(output_dir) if n.endswith(".mp4")])
    result = {
        "exit_code": process.returncode,
        "wall_seconds": round(elapsed, 1),
        "peak_rss_mb": round(peak / 1024 ** 2, 1),
        "peak_rss_by_stage_mb": {k: round(v / 1024 ** 2, 1)
                                 for k, v in sorted(stage_peaks.items(),
                                                    key=lambda kv: -kv[1])},
        "container_limit_gb": round(limit / 1024 ** 3, 2) if limit else None,
        "headroom_pct": (round(100 * (1 - peak / limit), 1) if limit else None),
        "cpu_avg_pct": round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None,
        "cpu_peak_pct": round(max(cpu_samples), 1) if cpu_samples else None,
        "clips_produced": clips,
        "job_disk_mb": round(job_bytes / 1024 ** 2, 1),
        "output_growth_mb": round((disk_after - disk_before) / 1024 ** 2, 1),
        "free_disk_gb": round(shutil.disk_usage(args.output_root).free / 1024 ** 3, 1),
    }

    if not args.keep:
        shutil.rmtree(output_dir, ignore_errors=True)

    print("\n── result " + "─" * 50)
    print(json.dumps(result, indent=2))

    if limit and peak / limit > 0.85:
        print("\n⚠️  Peak memory is within 15% of the container limit. Raise Docker "
              "Desktop's memory allowance or lower the maximum source duration.")
    if result["exit_code"] != 0:
        print("\n❌ The pipeline exited non-zero — the numbers above are for a failed run.")
    return result["exit_code"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-u", "--url", help="YouTube URL to benchmark")
    source.add_argument("-i", "--input", help="local video file to benchmark")
    parser.add_argument("-o", "--output-root", default="output")
    parser.add_argument("--format", default="vertical",
                        choices=["auto", "vertical", "horizontal", "square"])
    parser.add_argument("--keep", action="store_true", help="keep the generated clips")
    parser.add_argument("-v", "--verbose", action="store_true", help="stream pipeline logs")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
