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

DEPAY_MS_WARN  = 1.0
DEPAY_MS_CRIT  = 5.0
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
            # extract log type: rec_<type>_<suffix>.csv → strip suffix, strip "rec_"
            log_type = f.name[4:].replace(f"_{suffix}.csv", "")  # depay / full / decoder
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
    # Pick by newest mtime among the three files of each suffix
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
        cprint(f"\n[INFO] Using newest matched set: {suffix}", GREY)
    else:
        p = Path(arg)
        if p.exists():
            m = SUFFIX_RE.match(p.name)
            if not m:
                raise ValueError(f"Cannot extract date-time suffix from filename: {p.name}")
            suffix = m.group(1)
            log_dir = p.parent
        else:
            suffix = arg  # bare suffix passed directly

    paths = {k: log_dir / f"rec_{k}_{suffix}.csv" for k in ("depay", "full", "decoder")}
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing files for suffix '{suffix}': {[str(paths[k]) for k in missing]}"
        )

    # Confirm all three share the same suffix (timestamp match)
    cprint(f"[INFO] Matched triplet — suffix: {suffix}", GREY)
    for k, p in paths.items():
        cprint(f"       {k:10}: {p.name}", GREY)

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
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def pcts(s: pd.Series, ps=(50, 90, 95, 99)) -> dict:
    return {f"p{p}": round(float(np.percentile(s.dropna(), p)), 4) for p in ps}

def spike_stats(s: pd.Series, warn, crit) -> dict:
    n = len(s)
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

def nal_impact_table(df: pd.DataFrame, ms_col: str) -> dict | None:
    """Compare latency on rows with vs without drops. Returns stats dict or None."""
    rows_with = df[df["dropped"] > 0][ms_col]
    rows_clean = df[df["dropped"] == 0][ms_col]
    if len(rows_with) == 0:
        cprint("  No dropped rows — nothing to compare.", GREY)
        return None

    def ss(s):
        return {"n": len(s), "mean": round(float(s.mean()), 4),
                "p50": round(float(np.percentile(s, 50)), 4),
                "p95": round(float(np.percentile(s, 95)), 4),
                "p99": round(float(np.percentile(s, 99)), 4),
                "max": round(float(s.max()), 4)}

    cs, ds = ss(rows_clean), ss(rows_with)
    w = 10
    cprint(f"  {'':22}  {'no drop':>{w}}  {'dropped':>{w}}  {'delta':>{w}}", bold=True)
    print( f"  {'rows':22}  {cs['n']:>{w},}  {ds['n']:>{w},}")
    for label, ck, dk in [
        ("mean (ms)", cs["mean"], ds["mean"]),
        ("p50  (ms)", cs["p50"],  ds["p50"]),
        ("p95  (ms)", cs["p95"],  ds["p95"]),
        ("p99  (ms)", cs["p99"],  ds["p99"]),
        ("max  (ms)", cs["max"],  ds["max"]),
    ]:
        d = round(dk - ck, 4)
        col = RED if d > 1.0 else YELLOW if d > 0 else GREEN
        print(f"  {label:22}  {ck:>{w}.4f}  {dk:>{w}.4f}  ", end="")
        cprint(f"{d:>+{w}.4f}", col)
    delta_mean = round(ds["mean"] - cs["mean"], 4)
    print()
    col = RED if delta_mean > 1.0 else YELLOW if delta_mean > 0 else GREEN
    cprint(f"  Mean latency on dropped rows is {delta_mean:+.4f} ms vs clean rows.", col)
    return {"clean": cs, "dropped": ds,
            "delta_mean_ms": delta_mean,
            "delta_p99_ms": round(ds["p99"] - cs["p99"], 4)}

def rtp_gap_section(df: pd.DataFrame) -> dict:
    report = {}
    for cam_id, grp in df.groupby("cam_index"):
        seq = grp["rtp_seq"].sort_values().reset_index(drop=True)
        diffs = seq.diff().dropna()
        gaps = diffs[diffs > RTP_GAP_THRESHOLD]
        cam_stat = {
            "rtp_range": [int(seq.min()), int(seq.max())],
            "mean_step": round(float(diffs.mean()), 3),
            f"gaps_above_{RTP_GAP_THRESHOLD}": len(gaps),
            "max_gap": int(diffs.max()) if len(diffs) else 0,
        }
        report[int(cam_id)] = cam_stat
        col = YELLOW if len(gaps) > 0 else GREEN
        cprint(f"\n  Cam {cam_id}", bold=True)
        print( f"    RTP range : {seq.min()} → {seq.max()}")
        print( f"    Mean step : {cam_stat['mean_step']}")
        cprint(f"    Gaps > {RTP_GAP_THRESHOLD} : {len(gaps)}  (max gap = {cam_stat['max_gap']})", col)
    return report

def per_cam_latency(df: pd.DataFrame, ms_col: str, warn, crit) -> dict:
    report = {}
    for cam_id, grp in df.groupby("cam_index"):
        s = grp[ms_col]
        st = spike_stats(s, warn, crit)
        report[int(cam_id)] = st
        col = GREEN if st["mean"] < warn else YELLOW
        cprint(f"\n  Cam {cam_id}  ({len(s):,} rows)", bold=True)
        cprint(f"    mean={st['mean']:.4f} ms  std={st['std']:.4f}  max={st['max']:.4f}", col)
        print( f"    p50={st['p50']}  p90={st['p90']}  p95={st['p95']}  p99={st['p99']}")
        w_pct = st[f"spikes_above_{warn}_pct"]
        c_pct = st[f"spikes_above_{crit}_pct"]
        cprint(f"    spikes > {warn} ms: {w_pct:.2f}%   > {crit} ms: {c_pct:.2f}%",
               YELLOW if w_pct > 0 else GREEN)
    return report

def drop_summary(df: pd.DataFrame, label: str) -> dict:
    total = int(df["dropped"].sum())
    rows  = int((df["dropped"] > 0).sum())
    col   = RED if total > 0 else GREEN
    cprint(f"\n  {label}", bold=True)
    cprint(f"    Total dropped : {total}", col)
    cprint(f"    Rows affected : {rows}", col)
    per_cam = df.groupby("cam_index")["dropped"].sum().astype(int).to_dict()
    for cam_id, val in per_cam.items():
        cprint(f"    Cam {cam_id}: {val}", RED if val > 0 else GREEN)
    return {"total": total, "rows_with_drops": rows, "per_cam": per_cam}


# ──────────────────────────────────────────────────────────────────────────────
# Main analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyse(suffix, net: pd.DataFrame, full: pd.DataFrame, dec: pd.DataFrame,
            net_path: Path) -> dict:

    report = {
        "suffix": suffix,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": {
            "depay": net_path.name,
            "full":    net_path.with_name(f"rec_full_{suffix}.csv").name,
            "decoder": net_path.with_name(f"rec_decoder_{suffix}.csv").name,
        }
    }

    # ── Overview ──────────────────────────────────────────────────────────────
    section("OVERVIEW")

    def overview(df, label):
        dur = (df["wall_time"].max() - df["wall_time"].min()).total_seconds()
        return {
            "rows": len(df),
            "duration_s": round(dur, 3),
            "start": str(df["wall_time"].min().time()),
            "end":   str(df["wall_time"].max().time()),
            "cams":  sorted(df["cam_index"].unique().tolist()),
        }

    report["overview"] = {k: overview(df, k)
                          for k, df in [("depay", net), ("full", full), ("decoder", dec)]}

    w = 12
    cprint(f"  {'log':22}  {'rows':>{w}}  {'duration':>{w}}  {'start':>{w}}  {'end':>{w}}", bold=True)
    for label, df in [("depay", net), ("full", full), ("decoder", dec)]:
        o = report["overview"][label]
        print(f"  {label:22}  {o['rows']:>{w},}  {o['duration_s']:>{w}.1f}s"
              f"  {o['start']:>{w}}  {o['end']:>{w}}")

    # ── depay latency ───────────────────────────────────────────────────────
    section("DEPAY LATENCY  (depay_ms)")
    net_stats = spike_stats(net["depay_ms"], DEPAY_MS_WARN, DEPAY_MS_CRIT)
    print_latency_block(net_stats, DEPAY_MS_WARN, DEPAY_MS_CRIT)
    report["depay_latency"] = {"global": net_stats}

    section("DEPAY LATENCY — per camera")
    report["depay_latency"]["per_cam"] = per_cam_latency(
        net, "depay_ms", DEPAY_MS_WARN, DEPAY_MS_CRIT)

    section("DEPAY — RTP SEQUENCE GAPS")
    report["depay_rtp_gaps"] = rtp_gap_section(net)


    # ── Decoder latency ───────────────────────────────────────────────────────
    section("DECODER LATENCY  (decoder_ms)")
    dec_stats = spike_stats(dec["decoder_ms"], DECODER_MS_WARN, DECODER_MS_CRIT)
    print_latency_block(dec_stats, DECODER_MS_WARN, DECODER_MS_CRIT)
    report["decoder_latency"] = {"global": dec_stats}

    section("DECODER LATENCY — per camera")
    report["decoder_latency"]["per_cam"] = per_cam_latency(
        dec, "decoder_ms", DECODER_MS_WARN, DECODER_MS_CRIT)
    

    # ── Full pipeline latency ─────────────────────────────────────────────────
    section("FULL PIPELINE LATENCY  (full_ms)")
    full_stats = spike_stats(full["full_ms"], FULL_MS_WARN, FULL_MS_CRIT)
    print_latency_block(full_stats, FULL_MS_WARN, FULL_MS_CRIT)
    report["full_latency"] = {"global": full_stats}

    section("FULL PIPELINE LATENCY — per camera")
    report["full_latency"]["per_cam"] = per_cam_latency(
        full, "full_ms", FULL_MS_WARN, FULL_MS_CRIT)


    # ── Dropped packets ───────────────────────────────────────────────────────
    section("DROPPED PACKETS — all logs")
    report["dropped"] = {
        "depay": drop_summary(net,  "rec_depay"),
        "full":    drop_summary(full, "rec_full"),
        "decoder": drop_summary(dec,  "rec_decoder"),
    }

    # ── Drop latency impact ───────────────────────────────────────────────────
    section("DROP LATENCY IMPACT — full_ms")
    report["drop_impact_full"] = nal_impact_table(full, "full_ms")

    section("DROP LATENCY IMPACT — decoder_ms")
    report["drop_impact_decoder"] = nal_impact_table(dec, "decoder_ms")

    # ── Throughput ────────────────────────────────────────────────────────────
    section("THROUGHPUT (rows/sec per log)")
    report["throughput"] = {}
    for label, df in [("depay", net), ("full", full), ("decoder", dec)]:
        df2 = df.copy()
        df2["second"] = df2["wall_time"].dt.floor("s")
        tput = df2.groupby("second").size()
        t = {
            "mean": round(float(tput.mean()), 2),
            "min":  int(tput.min()),
            "max":  int(tput.max()),
            "low_seconds": int((tput < tput.mean() / 2).sum()),
        }
        report["throughput"][label] = t
        col = YELLOW if t["low_seconds"] > 0 else GREEN
        cprint(f"\n  {label}", bold=True)
        print( f"    mean={t['mean']} rows/s   min={t['min']}   max={t['max']}")
        cprint(f"    seconds < half mean throughput: {t['low_seconds']}", col)

    # ── Health summary ────────────────────────────────────────────────────────
    section("HEALTH SUMMARY")

    issues = []
    for log_name, ms_col, warn, crit, stats in [
        ("depay",  "depay_ms", DEPAY_MS_WARN,  DEPAY_MS_CRIT,  net_stats),
        ("full",     "full_ms",    FULL_MS_WARN,      FULL_MS_CRIT,     full_stats),
        ("decoder",  "decoder_ms", DECODER_MS_WARN,   DECODER_MS_CRIT,  dec_stats),
    ]:
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

    for label, t in report["throughput"].items():
        if t["low_seconds"] > 5:
            issues.append(f"NOTICE   [{label}] {t['low_seconds']} seconds with very low throughput")

    report["health"] = {"status": "OK" if not issues else "ISSUES", "issues": issues}

    if not issues:
        cprint("  ✓  No issues detected", GREEN, bold=True)
    else:
        for issue in issues:
            col = RED if "WARNING" in issue else YELLOW
            cprint(f"  ⚠  {issue}", col)

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

    net  = load(net_path)
    full = load(full_path)
    dec  = load(dec_path)

    report = analyse(suffix, net, full, dec, net_path)

    out_path = net_path.parent / f"rec_combined_{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    section("REPORT SAVED")
    cprint(f"  {out_path}", CYAN)
    print()


if __name__ == "__main__":
    main()
