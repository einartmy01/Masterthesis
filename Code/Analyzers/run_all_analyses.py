#!/usr/bin/env python3
"""
run_all_analyses.py  –  Run all analysers for every known timestamp.

Discovers timestamps from all log directories and the graphs folder,
then runs each matching analyser. Interactive windows are suppressed
via MPLBACKEND=Agg.

Usage:
    python3 run_all_analyses.py [timestamp]

    With no argument: processes every discovered timestamp.
    With a timestamp (e.g. 24.05-10:43): processes that one only.
"""

import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR    = (SCRIPT_DIR / ".." / "logs").resolve()
GRAPHS_DIR = SCRIPT_DIR / "graphs"

THROUGHPUT_SCRIPT      = SCRIPT_DIR / "throughput_analyse.py"
TRANSIT_RESULT_SCRIPT  = SCRIPT_DIR / "transit_analyseV5.py"
COMBINE_TRANSIT_SCRIPT = SCRIPT_DIR / "combine_transit.py"
CPU_SCRIPT             = SCRIPT_DIR / "cpu_analyseV2.py"
QUALITY_SCRIPT         = SCRIPT_DIR / "quality_analyse.py"
RECEIVER_PIPE_SCRIPT   = SCRIPT_DIR / "receiver_pipeline_analyseV2.py"
SENDER_PIPE_SCRIPT     = SCRIPT_DIR / "sender_pipeline_analyseV2.py"


def collect_timestamps() -> list[str]:
    timestamps: set[str] = set()

    globs = [
        (LOG_DIR / "throughput",          "sender_throughput_*.csv",  "sender_throughput_", ".csv"),
        (LOG_DIR / "transit",             "send_transit_*.csv",       "send_transit_",      ".csv"),
        (LOG_DIR / "cpu",                 "sender_cpu_*.log",         "sender_cpu_",        ".log"),
        (LOG_DIR / "quality",             "rec_quality_*.csv",        "rec_quality_",       ".csv"),
        (LOG_DIR / "pipeline" / "sender", "send_pipe_*.csv",          "send_pipe_",         ".csv"),
        (LOG_DIR / "pipeline" / "receiver", "rec_full_*.csv",         "rec_full_",          ".csv"),
    ]
    for directory, pattern, prefix, suffix in globs:
        if directory.exists():
            for f in directory.glob(pattern):
                ts = f.name.removeprefix(prefix).removesuffix(suffix)
                timestamps.add(ts)

    if GRAPHS_DIR.exists():
        for d in GRAPHS_DIR.iterdir():
            if d.is_dir():
                timestamps.add(d.name)

    return sorted(timestamps)


def run_script(script: Path, args: list[str], env: dict) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, str(script)] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(SCRIPT_DIR),
    )
    return result.returncode == 0, result.stdout + result.stderr


def process_timestamp(ts: str, env: dict) -> dict[str, bool | None]:
    print(f"\n{'='*55}")
    print(f"  {ts}")
    print(f"{'='*55}")

    results: dict[str, bool | None] = {}

    # ── Throughput ─────────────────────────────────────────────────────────────
    tp_file = LOG_DIR / "throughput" / f"sender_throughput_{ts}.csv"
    if tp_file.exists():
        ok, out = run_script(THROUGHPUT_SCRIPT, [f"{ts}.csv"], env)
        print(f"  throughput       {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)
        results["throughput"] = ok
    else:
        print(f"  throughput       skip (no log)")

    # ── Transit ────────────────────────────────────────────────────────────────
    send_transit = LOG_DIR / "transit" / f"send_transit_{ts}.csv"

    if send_transit.exists():
        ok, out = run_script(COMBINE_TRANSIT_SCRIPT, [f"{ts}.csv"], env)
        print(f"  combine_transit  {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)

        transit_result = LOG_DIR / "transit" / f"transit_result_{ts}.csv"
        if transit_result.exists():
            ok, out = run_script(TRANSIT_RESULT_SCRIPT, [f"{ts}.csv"], env)
            print(f"  transit          {'OK' if ok else 'FAILED'}")
            if not ok:
                print(out)
            results["transit"] = ok
        else:
            print(f"  transit          skip (combine_transit produced no result)")
    else:
        print(f"  transit          skip (no log)")

    # ── CPU ────────────────────────────────────────────────────────────────────
    cpu_file = LOG_DIR / "cpu" / f"sender_cpu_{ts}.log"
    if cpu_file.exists():
        ok, out = run_script(CPU_SCRIPT, [f"{ts}.log"], env)
        print(f"  cpu              {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)
        results["cpu"] = ok
    else:
        print(f"  cpu              skip (no log)")

    # ── Quality ────────────────────────────────────────────────────────────────
    quality_file = LOG_DIR / "quality" / f"rec_quality_{ts}.csv"
    if quality_file.exists():
        ok, out = run_script(QUALITY_SCRIPT, [f"{ts}.csv"], env)
        print(f"  quality          {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)
        results["quality"] = ok
    else:
        print(f"  quality          skip (no log)")

    # ── Receiver pipeline ──────────────────────────────────────────────────────
    rec_pipe = LOG_DIR / "pipeline" / "receiver" / f"rec_full_{ts}.csv"
    if rec_pipe.exists():
        ok, out = run_script(RECEIVER_PIPE_SCRIPT, [f"{ts}.csv"], env)
        print(f"  receiver_pipeline {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)
        results["receiver_pipeline"] = ok
    else:
        print(f"  receiver_pipeline skip (no log)")

    # ── Sender pipeline ────────────────────────────────────────────────────────
    send_pipe = LOG_DIR / "pipeline" / "sender" / f"send_pipe_{ts}.csv"
    if send_pipe.exists():
        ok, out = run_script(SENDER_PIPE_SCRIPT, [f"{ts}.csv"], env)
        print(f"  sender_pipeline  {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out)
        results["sender_pipeline"] = ok
    else:
        print(f"  sender_pipeline  skip (no log)")

    return results


def main():
    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"

    if len(sys.argv) > 1:
        timestamps = [sys.argv[1]]
    else:
        timestamps = collect_timestamps()

    if not timestamps:
        print("No timestamps found.")
        sys.exit(1)

    print(f"Processing {len(timestamps)} timestamp(s): {', '.join(timestamps)}")

    all_results: dict[str, dict] = {}
    for ts in timestamps:
        all_results[ts] = process_timestamp(ts, env)

    print(f"\n{'='*55}")
    print("  Summary")
    print(f"{'='*55}")
    for ts, res in all_results.items():
        if not res:
            print(f"  {ts}: no matching log files")
            continue
        ok_count   = sum(1 for v in res.values() if v is True)
        fail_count = sum(1 for v in res.values() if v is False)
        skip_count = 6 - len(res)
        print(f"  {ts}: {ok_count} OK  {fail_count} FAILED  {skip_count} skipped")


if __name__ == "__main__":
    main()
