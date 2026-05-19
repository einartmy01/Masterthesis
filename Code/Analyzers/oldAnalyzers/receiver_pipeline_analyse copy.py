#!/usr/bin/env python3
"""
analyse_receiver_pipeline.py
─────────────────────────────
Analyses receiver pipeline logs from ../logs/pipeline/receiver/.

Expects three files per test run, matched by date+time suffix:
    rec_depay_DD.MM-HH:MM.csv
    rec_full_DD.MM-HH:MM.csv
    rec_decoder_DD.MM-HH:MM.csv

Usage:
    python analyse_receiver_pipeline.py                    # newest matched set
    python analyse_receiver_pipeline.py 07.05-15:45       # specific suffix
    python analyse_receiver_pipeline.py path/to/rec_depay_07.05-15:45.csv  # any one file of the set

Output:
    - Colour-coded terminal summary
    - JSON report saved as rec_combined_<suffix>.json in the same folder
"""

import sys
import re
import json
import os
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path("../logs/pipeline/receiver")

DEPAY_MS_WARN    = 1.0
DEPAY_MS_CRIT    = 5.0
DECODER_MS_WARN  = 5.0
DECODER_MS_CRIT  = 20.0
FULL_MS_WARN     = 10.0
FULL_MS_CRIT     = 25.0
RTP_GAP_THRESHOLD = 15

# ──────────────────────────────────────────────────────────────────────────────
# Terminal colours
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
    bar = "─" * 60
    print()
    cprint(bar, CYAN)
    cprint(f"  {title}", CYAN, bold=True)
    cprint(bar, CYAN)


# ──────────────────────────────────────────────────────────────────────────────
# File discovery & validation
# ──────────────────────────────────────────────────────────────────────────────

SUFFIX_RE = re.compile(r"rec_(?:depay|full|decoder)_(\d{2}\.\d{2}-\d{2}:\d{2})\.csv$")

def all_suffixes(log_dir: Path) -> list[str]:
    """Return all unique date-time suffixes that have all three files present."""
    found: dict[str, set] = {}
    for f in log_dir.glob("rec_*.csv"):
        m = SUFFIX_RE.match(f.name)
        if m:
            suffix = m.group(1)
            log_type = f.name[4:].replace(f"_{suffix}.csv", "")
            found.setdefault(suffix, set()).add(log_type)
    complete = [s for s, kinds in found.items()
                if {"depay", "full", "decoder"}.issubset(kinds)]
    return sorted(complete)


def newest_suffix(log_dir: Path) -> str:
    suffixes = all_suffixes(log_dir)
    if not suffixes:
        raise FileNotFoundError(
            f"No complete rec_depay/full/decoder triplet found in {log_dir}"
        )
    def triplet_mtime(s):
        return max(
            (log_dir / f"rec_{k}_{s}.csv").stat().st_mtime
            for k in ("depay", "full", "decoder")
        )
    return max(suffixes, key=triplet_mtime)


def resolve_suffix(arg: str | None, log_dir: Path) -> tuple[str, Path, Path, Path]:
    """
    Return (suffix, depay_path, full_path, decoder_path).
    arg can be None (newest), a bare suffix, or a path to any one of the three files.
    """
    if arg is None:
        suffix = newest_suffix(log_dir)
    else:
        p = Path(arg)
        if p.exists():
            m = SUFFIX_RE.match(p.name)
            if not m:
                raise ValueError(f"Cannot extract date-time suffix from filename: {p.name}")
            suffix = m.group(1)
            log_dir = p.parent
        else:
            suffix = arg

    paths = {k: log_dir / f"rec_{k}_{suffix}.csv" for k in ("depay", "full", "decoder")}
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing files for suffix '{suffix}': {[str(paths[k]) for k in missing]}"
        )
    return suffix, paths["depay"], paths["full"], paths["decoder"]


# ──────────────────────────────────────────────────────────────────────────────
# Loading
# ──────────────────────────────────────────────────────────────────────────────

def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    try:
        df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")
    except Exception:
        df["wall_time"] = pd.to_datetime(df["wall_time"])
    return df.sort_values("wall_time").reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
# Compute helpers
# ──────────────────────────────────────────────────────────────────────────────

def pcts(s: pd.Series, ps=(50, 90, 95, 99)) -> dict:
    return {f"p{p}": round(float(np.percentile(s.dropna(), p)), 4) for p in ps}

def spike_stats(s: pd.Series, warn, crit) -> dict:
    return {
        "mean":      round(float(s.mean()), 4),
        "std":       round(float(s.std()),  4),
        "min":       round(float(s.min()),  4),
        "max":       round(float(s.max()),  4),
        **pcts(s),
        f"spikes_above_{warn}":      int((s > warn).sum()),
        f"spikes_above_{warn}_pct":  round(100 * (s > warn).mean(), 3),
        f"spikes_above_{crit}":      int((s > crit).sum()),
        f"spikes_above_{crit}_pct":  round(100 * (s > crit).mean(), 3),
    }

def compute_overview(df: pd.DataFrame) -> dict:
    dur = (df["wall_time"].max() - df["wall_time"].min()).total_seconds()
    return {
        "rows":       len(df),
        "duration_s": round(dur, 3),
        "start":      str(df["wall_time"].min().time()),
        "end":        str(df["wall_time"].max().time()),
        "cams":       sorted(df["cam_index"].unique().tolist()),
    }

def compute_drop_summary(df: pd.DataFrame) -> dict:
    total   = int(df["dropped"].sum())
    rows    = int((df["dropped"] > 0).sum())
    per_cam = df.groupby("cam_index")["dropped"].sum().astype(int).to_dict()
    return {"total": total, "rows_with_drops": rows, "per_cam": per_cam}

def compute_rtp_gaps(df: pd.DataFrame) -> dict:
    report = {}
    for cam_id, grp in df.groupby("cam_index"):
        seq   = grp["rtp_seq"].sort_values().reset_index(drop=True)
        diffs = seq.diff().dropna()
        gaps  = diffs[diffs > RTP_GAP_THRESHOLD]
        report[int(cam_id)] = {
            "rtp_range":                    [int(seq.min()), int(seq.max())],
            "mean_step":                    round(float(diffs.mean()), 3),
            f"gaps_above_{RTP_GAP_THRESHOLD}": len(gaps),
            "max_gap":                      int(diffs.max()) if len(diffs) else 0,
        }
    return report

def compute_per_cam_latency(df: pd.DataFrame, ms_col: str, warn, crit) -> dict:
    report = {}
    for cam_id, grp in df.groupby("cam_index"):
        s  = grp[ms_col]
        st = spike_stats(s, warn, crit)
        st["n_rows"] = len(s)
        report[int(cam_id)] = st
    return report

def compute_health(report: dict) -> dict:
    issues = []

    checks = [
        ("depay",   DEPAY_MS_WARN,   DEPAY_MS_CRIT,   report["depay_latency"]["global"]),
        ("full",    FULL_MS_WARN,    FULL_MS_CRIT,    report["full_latency"]["global"]),
        ("decoder", DECODER_MS_WARN, DECODER_MS_CRIT, report["decoder_latency"]["global"]),
    ]
    for log_name, warn, crit, stats in checks:
        c_pct = stats[f"spikes_above_{crit}_pct"]
        w_pct = stats[f"spikes_above_{warn}_pct"]
        if c_pct > 0:
            issues.append(f"WARNING  [{log_name}] {c_pct:.2f}% of rows exceed {crit} ms")
        elif w_pct > 5:
            issues.append(f"NOTICE   [{log_name}] {w_pct:.2f}% of rows exceed {warn} ms")

    for log_name, d in report["dropped"].items():
        if d["total"] > 0:
            issues.append(f"WARNING  [{log_name}] {d['total']} dropped packets "
                          f"across {d['rows_with_drops']} rows")

    for cam_id, g in report["depay_rtp_gaps"].items():
        n = g[f"gaps_above_{RTP_GAP_THRESHOLD}"]
        if n > 0:
            issues.append(f"NOTICE   [depay] cam {cam_id}: {n} RTP gaps > {RTP_GAP_THRESHOLD}")

    return {"status": "OK" if not issues else "ISSUES", "issues": issues}


# ──────────────────────────────────────────────────────────────────────────────
# Print helpers
# ──────────────────────────────────────────────────────────────────────────────

def print_latency_block(stats: dict, warn, crit, unit="ms"):
    col = GREEN if stats["mean"] < warn else YELLOW
    cprint(f"  Mean   : {stats['mean']:.4f} {unit}  (σ = {stats['std']:.4f})", col)
    print( f"  Min    : {stats['min']:.4f} {unit}")
    print( f"  Max    : {stats['max']:.4f} {unit}")
    print( f"  p50    : {stats['p50']:.4f}   p90 : {stats['p90']:.4f}")
    print( f"  p95    : {stats['p95']:.4f}   p99 : {stats['p99']:.4f}")
    w_n = stats[f"spikes_above_{warn}"]
    c_n = stats[f"spikes_above_{crit}"]
    cprint(f"  Spikes > {warn} {unit} : {w_n:,}  ({stats[f'spikes_above_{warn}_pct']:.2f}%)",
           YELLOW if w_n > 0 else GREEN)
    cprint(f"  Spikes > {crit} {unit} : {c_n:,}  ({stats[f'spikes_above_{crit}_pct']:.2f}%)",
           RED if c_n > 0 else GREEN)

def print_drop_summary(label: str, summary: dict):
    col = RED if summary["total"] > 0 else GREEN
    cprint(f"\n  {label}", bold=True)
    cprint(f"    Total dropped : {summary['total']}", col)
    cprint(f"    Rows affected : {summary['rows_with_drops']}", col)
    for cam_id, val in summary["per_cam"].items():
        cprint(f"    Cam {cam_id}: {val}", RED if val > 0 else GREEN)

def print_rtp_gaps(report: dict):
    for cam_id, cam_stat in report.items():
        gaps = cam_stat[f"gaps_above_{RTP_GAP_THRESHOLD}"]
        col  = YELLOW if gaps > 0 else GREEN
        cprint(f"\n  Cam {cam_id}", bold=True)
        print( f"    RTP range : {cam_stat['rtp_range'][0]} → {cam_stat['rtp_range'][1]}")
        print( f"    Mean step : {cam_stat['mean_step']}")
        cprint(f"    Gaps > {RTP_GAP_THRESHOLD} : {gaps}  (max gap = {cam_stat['max_gap']})", col)

def print_per_cam_latency(report: dict, warn, crit):
    for cam_id, st in report.items():
        col   = GREEN if st["mean"] < warn else YELLOW
        w_pct = st[f"spikes_above_{warn}_pct"]
        c_pct = st[f"spikes_above_{crit}_pct"]
        cprint(f"\n  Cam {cam_id}  ({st['n_rows']:,} rows)", bold=True)
        cprint(f"    mean={st['mean']:.4f} ms  std={st['std']:.4f}  max={st['max']:.4f}", col)
        print( f"    p50={st['p50']}  p90={st['p90']}  p95={st['p95']}  p99={st['p99']}")
        cprint(f"    spikes > {warn} ms: {w_pct:.2f}%   > {crit} ms: {c_pct:.2f}%",
               YELLOW if w_pct > 0 else GREEN)

def print_report(report: dict):
    section("OVERVIEW")
    w = 12
    cprint(f"  {'log':22}  {'rows':>{w}}  {'duration':>{w}}  {'start':>{w}}  {'end':>{w}}", bold=True)
    for label in ("depay", "full", "decoder"):
        o = report["overview"][label]
        print(f"  {label:22}  {o['rows']:>{w},}  {o['duration_s']:>{w}.1f}s"
              f"  {o['start']:>{w}}  {o['end']:>{w}}")

    section("DROPPED PACKETS — all logs")
    for label, summary in report["dropped"].items():
        print_drop_summary(f"rec_{label}", summary)

    section("DEPAY LATENCY  (depay_ms)")
    print_latency_block(report["depay_latency"]["global"], DEPAY_MS_WARN, DEPAY_MS_CRIT)

    section("DEPAY LATENCY — per camera")
    print_per_cam_latency(report["depay_latency"]["per_cam"], DEPAY_MS_WARN, DEPAY_MS_CRIT)

    section("DEPAY — RTP SEQUENCE GAPS")
    print_rtp_gaps(report["depay_rtp_gaps"])

    section("DECODER LATENCY  (decoder_ms)")
    print_latency_block(report["decoder_latency"]["global"], DECODER_MS_WARN, DECODER_MS_CRIT)

    section("DECODER LATENCY — per camera")
    print_per_cam_latency(report["decoder_latency"]["per_cam"], DECODER_MS_WARN, DECODER_MS_CRIT)

    section("FULL PIPELINE LATENCY  (full_ms)")
    print_latency_block(report["full_latency"]["global"], FULL_MS_WARN, FULL_MS_CRIT)

    section("FULL PIPELINE LATENCY — per camera")
    print_per_cam_latency(report["full_latency"]["per_cam"], FULL_MS_WARN, FULL_MS_CRIT)

    section("HEALTH SUMMARY")
    if not report["health"]["issues"]:
        cprint("  ✓  No issues detected", GREEN, bold=True)
    else:
        for issue in report["health"]["issues"]:
            col = RED if "WARNING" in issue else YELLOW
            cprint(f"  ⚠  {issue}", col)


# ──────────────────────────────────────────────────────────────────────────────
# Main analysis  (pure computation — no prints)
# ──────────────────────────────────────────────────────────────────────────────

def analyse(suffix, net: pd.DataFrame, full: pd.DataFrame, dec: pd.DataFrame,
            net_path: Path) -> dict:

    report = {
        "suffix":       suffix,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": {
            "depay":   net_path.name,
            "full":    net_path.with_name(f"rec_full_{suffix}.csv").name,
            "decoder": net_path.with_name(f"rec_decoder_{suffix}.csv").name,
        }
    }

    report["overview"] = {
        k: compute_overview(df)
        for k, df in [("depay", net), ("full", full), ("decoder", dec)]
    }

    report["dropped"] = {
        "depay":   compute_drop_summary(net),
        "full":    compute_drop_summary(full),
        "decoder": compute_drop_summary(dec),
    }

    # Filter out dropped rows — all measurements below exclude them
    net  = net[net["dropped"] == 0].reset_index(drop=True)
    full = full[full["dropped"] == 0].reset_index(drop=True)
    dec  = dec[dec["dropped"] == 0].reset_index(drop=True)

    report["depay_latency"] = {
        "global":  spike_stats(net["depay_ms"], DEPAY_MS_WARN, DEPAY_MS_CRIT),
        "per_cam": compute_per_cam_latency(net, "depay_ms", DEPAY_MS_WARN, DEPAY_MS_CRIT),
    }
    report["depay_rtp_gaps"] = compute_rtp_gaps(net)

    report["decoder_latency"] = {
        "global":  spike_stats(dec["decoder_ms"], DECODER_MS_WARN, DECODER_MS_CRIT),
        "per_cam": compute_per_cam_latency(dec, "decoder_ms", DECODER_MS_WARN, DECODER_MS_CRIT),
    }

    report["full_latency"] = {
        "global":  spike_stats(full["full_ms"], FULL_MS_WARN, FULL_MS_CRIT),
        "per_cam": compute_per_cam_latency(full, "full_ms", FULL_MS_WARN, FULL_MS_CRIT),
    }

    report["health"] = compute_health(report)

    return report


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not LOG_DIR.exists() and arg is None:
        print(f"[ERROR] Log directory not found: {LOG_DIR}")
        sys.exit(1)

    suffix, net_path, full_path, dec_path = resolve_suffix(arg, LOG_DIR)
    if arg is None:
        cprint(f"\n[INFO] Using newest matched set: {suffix}", GREY)
    cprint(f"[INFO] Matched triplet — suffix: {suffix}", GREY)
    for k, p in [("depay", net_path), ("full", full_path), ("decoder", dec_path)]:
        cprint(f"       {k:10}: {p.name}", GREY)

    net  = load(net_path)
    full = load(full_path)
    dec  = load(dec_path)

    report = analyse(suffix, net, full, dec, net_path)
    print_report(report)

    out_path = net_path.parent / f"rec_combined_{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    section("REPORT SAVED")
    cprint(f"  {out_path}", CYAN)
    print()


if __name__ == "__main__":
    main()
