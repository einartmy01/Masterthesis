#!/usr/bin/env python3
"""
analyse_sender_pipeline.py
──────────────────────────
Analyses sender pipeline log files from logs/pipeline/sender/.

Usage:
    python analyse_sender_pipeline.py                  # newest file
    python analyse_sender_pipeline.py <path/to/file>   # specific file

Output:
    - Prints a statistics summary to the terminal
    - Saves a JSON report next to the log file (same name, .json extension)
"""

import sys
import json
import glob
import os
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path("../logs/pipeline/sender")
PIPELINE_MS_WARN_THRESHOLD = 1.0   # ms — flag spikes above this
PIPELINE_MS_CRIT_THRESHOLD = 2.0   # ms — flag critical spikes above this
RTP_GAP_WARN_THRESHOLD = 15        # flag gaps in rtp_seq larger than this per cam


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
GREY   = "\033[90m"

def cprint(text, color="", bold=False, end="\n"):
    prefix = (BOLD if bold else "") + color
    print(f"{prefix}{text}{RESET}", end=end)

def section(title):
    width = 60
    bar = "─" * width
    print()
    cprint(bar, CYAN)
    cprint(f"  {title}", CYAN, bold=True)
    cprint(bar, CYAN)


def find_newest_log(log_dir: Path) -> Path:
    """Return the most recently modified .csv in log_dir."""
    files = list(log_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {log_dir}")
    return max(files, key=lambda p: p.stat().st_mtime)


def load_log(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)

    required = {"wall_time", "cam", "rtp_seq", "pipeline_ms", "dropped_nals"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing expected columns: {missing}")

    # Parse wall_time — handle both HH:MM:SS.fff and full ISO timestamps
    try:
        df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")
    except Exception:
        df["wall_time"] = pd.to_datetime(df["wall_time"])

    df = df.sort_values("wall_time").reset_index(drop=True)
    return df


def percentile_dict(series: pd.Series, pcts=(50, 90, 95, 99)) -> dict:
    return {f"p{p}": round(float(np.percentile(series.dropna(), p)), 4) for p in pcts}


# ──────────────────────────────────────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyse(df: pd.DataFrame, file_path: Path) -> dict:
    report = {
        "file": str(file_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    # ── Overview ──────────────────────────────────────────────────────────────
    section("OVERVIEW")

    duration_s = (df["wall_time"].max() - df["wall_time"].min()).total_seconds()
    n_cams = df["cam"].nunique()
    cams = sorted(df["cam"].unique().tolist())
    total_rows = len(df)

    report["overview"] = {
        "total_rows": total_rows,
        "cameras": cams,
        "duration_seconds": round(duration_s, 3),
        "time_start": str(df["wall_time"].min()),
        "time_end": str(df["wall_time"].max()),
        "rows_per_second": round(total_rows / duration_s, 2) if duration_s > 0 else None,
    }

    print(f"  File        : {file_path.name}")
    print(f"  Total rows  : {total_rows:,}")
    print(f"  Cameras     : {cams}")
    print(f"  Duration    : {duration_s:.1f} s  ({duration_s/60:.2f} min)")
    print(f"  Start / End : {df['wall_time'].min().time()} → {df['wall_time'].max().time()}")
    print(f"  Rows/sec    : {report['overview']['rows_per_second']}")

    # ── Pipeline latency — global ─────────────────────────────────────────────
    section("PIPELINE LATENCY  (pipeline_ms)  — all cameras")

    ms = df["pipeline_ms"]
    warn_pct  = 100 * (ms > PIPELINE_MS_WARN_THRESHOLD).sum() / len(ms)
    crit_pct  = 100 * (ms > PIPELINE_MS_CRIT_THRESHOLD).sum() / len(ms)
    spikes_warn = (ms > PIPELINE_MS_WARN_THRESHOLD).sum()
    spikes_crit = (ms > PIPELINE_MS_CRIT_THRESHOLD).sum()

    global_lat = {
        "mean_ms":   round(float(ms.mean()), 4),
        "std_ms":    round(float(ms.std()),  4),
        "min_ms":    round(float(ms.min()),  4),
        "max_ms":    round(float(ms.max()),  4),
        **percentile_dict(ms),
        f"spikes_above_{PIPELINE_MS_WARN_THRESHOLD}ms": int(spikes_warn),
        f"spikes_above_{PIPELINE_MS_WARN_THRESHOLD}ms_pct": round(warn_pct, 3),
        f"spikes_above_{PIPELINE_MS_CRIT_THRESHOLD}ms": int(spikes_crit),
        f"spikes_above_{PIPELINE_MS_CRIT_THRESHOLD}ms_pct": round(crit_pct, 3),
    }
    report["latency_global"] = global_lat

    label_color = GREEN if ms.mean() < PIPELINE_MS_WARN_THRESHOLD else YELLOW
    cprint(f"  Mean   : {global_lat['mean_ms']:.4f} ms  (σ = {global_lat['std_ms']:.4f})", label_color)
    print( f"  Min    : {global_lat['min_ms']:.4f} ms")
    print( f"  Max    : {global_lat['max_ms']:.4f} ms")
    print( f"  p50    : {global_lat['p50']:.4f} ms")
    print( f"  p90    : {global_lat['p90']:.4f} ms")
    print( f"  p95    : {global_lat['p95']:.4f} ms")
    print( f"  p99    : {global_lat['p99']:.4f} ms")

    warn_col = YELLOW if spikes_warn > 0 else GREEN
    crit_col = RED    if spikes_crit > 0 else GREEN
    cprint(f"  Spikes > {PIPELINE_MS_WARN_THRESHOLD} ms : {spikes_warn:,}  ({warn_pct:.2f}%)", warn_col)
    cprint(f"  Spikes > {PIPELINE_MS_CRIT_THRESHOLD} ms : {spikes_crit:,}  ({crit_pct:.2f}%)", crit_col)

    # ── Pipeline latency — per camera ─────────────────────────────────────────
    section("PIPELINE LATENCY — per camera")

    report["latency_per_cam"] = {}
    for cam_id, group in df.groupby("cam"):
        ms_c = group["pipeline_ms"]
        w_pct = 100 * (ms_c > PIPELINE_MS_WARN_THRESHOLD).sum() / len(ms_c)
        c_pct = 100 * (ms_c > PIPELINE_MS_CRIT_THRESHOLD).sum() / len(ms_c)
        cam_stats = {
            "rows":    len(ms_c),
            "mean_ms": round(float(ms_c.mean()), 4),
            "std_ms":  round(float(ms_c.std()),  4),
            "max_ms":  round(float(ms_c.max()),  4),
            **percentile_dict(ms_c),
            f"spikes_above_{PIPELINE_MS_WARN_THRESHOLD}ms_pct": round(w_pct, 3),
            f"spikes_above_{PIPELINE_MS_CRIT_THRESHOLD}ms_pct": round(c_pct, 3),
        }
        report["latency_per_cam"][int(cam_id)] = cam_stats

        col = GREEN if ms_c.mean() < PIPELINE_MS_WARN_THRESHOLD else YELLOW
        cprint(f"\n  Cam {cam_id}  ({len(ms_c):,} rows)", bold=True)
        cprint(f"    mean={ms_c.mean():.4f} ms  std={ms_c.std():.4f}  max={ms_c.max():.4f}", col)
        print( f"    p50={cam_stats['p50']}  p90={cam_stats['p90']}  p95={cam_stats['p95']}  p99={cam_stats['p99']}")
        cprint(f"    spikes > {PIPELINE_MS_WARN_THRESHOLD} ms: {w_pct:.2f}%   > {PIPELINE_MS_CRIT_THRESHOLD} ms: {c_pct:.2f}%",
               YELLOW if w_pct > 0 else GREEN)

    # ── Dropped NALs ──────────────────────────────────────────────────────────
    section("DROPPED NALs")

    total_dropped = int(df["dropped_nals"].sum())
    rows_with_drops = int((df["dropped_nals"] > 0).sum())
    drop_color = RED if total_dropped > 0 else GREEN

    report["dropped_nals"] = {
        "total_dropped": total_dropped,
        "rows_with_drops": rows_with_drops,
        "per_cam": df.groupby("cam")["dropped_nals"].sum().astype(int).to_dict(),
    }

    cprint(f"  Total dropped NALs : {total_dropped}", drop_color, bold=True)
    cprint(f"  Rows with drops    : {rows_with_drops}", drop_color)
    for cam_id, val in report["dropped_nals"]["per_cam"].items():
        col = RED if val > 0 else GREEN
        cprint(f"    Cam {cam_id}: {val}", col)

    # ── NAL drop latency impact ───────────────────────────────────────────────
    section("NAL DROP LATENCY IMPACT")

    if rows_with_drops == 0:
        cprint("  No dropped NAL rows in this file — nothing to compare.", GREY)
        report["nal_drop_impact"] = None
    else:
        dropped_rows = df[df["dropped_nals"] > 0]["pipeline_ms"]
        clean_rows   = df[df["dropped_nals"] == 0]["pipeline_ms"]

        def short_stats(s):
            return {
                "n":       len(s),
                "mean_ms": round(float(s.mean()), 4),
                "p50_ms":  round(float(np.percentile(s, 50)), 4),
                "p95_ms":  round(float(np.percentile(s, 95)), 4),
                "p99_ms":  round(float(np.percentile(s, 99)), 4),
                "max_ms":  round(float(s.max()), 4),
            }

        clean_s   = short_stats(clean_rows)
        dropped_s = short_stats(dropped_rows)
        delta_mean = round(dropped_s["mean_ms"] - clean_s["mean_ms"], 4)

        report["nal_drop_impact"] = {
            "clean_rows":    clean_s,
            "dropped_rows":  dropped_s,
            "delta_mean_ms": delta_mean,
            "delta_p99_ms":  round(dropped_s["p99_ms"] - clean_s["p99_ms"], 4),
        }

        w = 10
        cprint(f"  {'':22}  {'no drop':>{w}}  {'dropped':>{w}}  {'delta':>{w}}", bold=True)
        print( f"  {'rows':22}  {clean_s['n']:>{w},}  {dropped_s['n']:>{w},}")
        for label, ck, dk in [
            ("mean (ms)",  clean_s["mean_ms"],  dropped_s["mean_ms"]),
            ("p50  (ms)",  clean_s["p50_ms"],   dropped_s["p50_ms"]),
            ("p95  (ms)",  clean_s["p95_ms"],   dropped_s["p95_ms"]),
            ("p99  (ms)",  clean_s["p99_ms"],   dropped_s["p99_ms"]),
            ("max  (ms)",  clean_s["max_ms"],   dropped_s["max_ms"]),
        ]:
            d = round(dk - ck, 4)
            col = RED if d > 0.5 else YELLOW if d > 0 else GREEN
            line = f"  {label:22}  {ck:>{w}.4f}  {dk:>{w}.4f}  "
            print(line, end="")
            cprint(f"{d:>+{w}.4f}", col)

        print()
        col = RED if delta_mean > 0.5 else YELLOW if delta_mean > 0 else GREEN
        cprint(f"  Mean latency on dropped rows is {delta_mean:+.4f} ms vs clean rows.", col)

    # ── RTP sequence gaps ─────────────────────────────────────────────────────
    section("RTP SEQUENCE GAPS (per camera)")

    report["rtp_gaps"] = {}
    for cam_id, group in df.groupby("cam"):
        seq = group["rtp_seq"].sort_values().reset_index(drop=True)
        diffs = seq.diff().dropna()
        gaps = diffs[diffs > RTP_GAP_WARN_THRESHOLD]

        cam_gap = {
            "rtp_range": [int(seq.min()), int(seq.max())],
            "expected_range": int(seq.max() - seq.min()),
            "actual_rows": len(seq),
            "mean_step": round(float(diffs.mean()), 3),
            f"gaps_above_{RTP_GAP_WARN_THRESHOLD}": len(gaps),
            "max_gap": int(diffs.max()) if len(diffs) else 0,
        }
        report["rtp_gaps"][int(cam_id)] = cam_gap

        gap_col = YELLOW if len(gaps) > 0 else GREEN
        cprint(f"\n  Cam {cam_id}", bold=True)
        print( f"    RTP range  : {seq.min()} → {seq.max()}")
        print( f"    Mean step  : {cam_gap['mean_step']}")
        cprint(f"    Gaps > {RTP_GAP_WARN_THRESHOLD} : {len(gaps)}  (max gap = {cam_gap['max_gap']})", gap_col)

    # ── Throughput over time ───────────────────────────────────────────────────
    section("THROUGHPUT (rows/sec bucketed per second)")

    df["second"] = df["wall_time"].dt.floor("s")
    tput = df.groupby("second").size()

    report["throughput"] = {
        "mean_rows_per_sec": round(float(tput.mean()), 2),
        "min_rows_per_sec":  int(tput.min()),
        "max_rows_per_sec":  int(tput.max()),
        "std_rows_per_sec":  round(float(tput.std()), 2),
        "low_seconds_below_half_mean": int((tput < tput.mean() / 2).sum()),
    }
    t = report["throughput"]
    print(f"  Mean : {t['mean_rows_per_sec']} rows/s")
    print(f"  Min  : {t['min_rows_per_sec']} rows/s")
    print(f"  Max  : {t['max_rows_per_sec']} rows/s")
    col = YELLOW if t["low_seconds_below_half_mean"] > 0 else GREEN
    cprint(f"  Seconds with < half mean throughput : {t['low_seconds_below_half_mean']}", col)

    # ── Summary / health ──────────────────────────────────────────────────────
    section("HEALTH SUMMARY")

    issues = []
    if total_dropped > 0:
        issues.append(f"CRITICAL: {total_dropped} dropped NALs")
    if crit_pct > 1.0:
        issues.append(f"WARNING: {crit_pct:.2f}% of packets exceed {PIPELINE_MS_CRIT_THRESHOLD} ms")
    elif warn_pct > 5.0:
        issues.append(f"NOTICE: {warn_pct:.2f}% of packets exceed {PIPELINE_MS_WARN_THRESHOLD} ms")
    for cam_id, g in report["rtp_gaps"].items():
        if g[f"gaps_above_{RTP_GAP_WARN_THRESHOLD}"] > 0:
            issues.append(f"NOTICE: cam {cam_id} has {g[f'gaps_above_{RTP_GAP_WARN_THRESHOLD}']} RTP sequence gaps")
    if t["low_seconds_below_half_mean"] > 5:
        issues.append(f"NOTICE: {t['low_seconds_below_half_mean']} seconds with very low throughput")

    report["health"] = {"status": "OK" if not issues else "ISSUES", "issues": issues}

    if not issues:
        cprint("  ✓  No issues detected", GREEN, bold=True)
    else:
        for issue in issues:
            col = RED if "CRITICAL" in issue else YELLOW
            cprint(f"  ⚠  {issue}", col)

    return report


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        file_path = Path(sys.argv[1])
        if not file_path.exists():
            print(f"[ERROR] File not found: {file_path}")
            sys.exit(1)
    else:
        if not LOG_DIR.exists():
            print(f"[ERROR] Log directory not found: {LOG_DIR}")
            print("        Create it or pass a file path as an argument.")
            sys.exit(1)
        file_path = find_newest_log(LOG_DIR)
        cprint(f"\n[INFO] Using newest log: {file_path}", GREY)

    df = load_log(file_path)
    report = analyse(df, file_path)

    # Save JSON report
    out_path = file_path.with_suffix(".json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    section("REPORT SAVED")
    cprint(f"  {out_path}", CYAN)
    print()


if __name__ == "__main__":
    main()
