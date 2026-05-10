#!/usr/bin/env python3
"""
analyse_transit.py
──────────────────
Computes network transit latency by joining sender and receiver transit CSVs
on (cam_index, rtp_seq). Matches files by timestamp suffix with ±1 min wiggle.

File naming convention:
    ../logs/transit/send_transit_DD.MM-HH:MM.csv
    ../logs/transit/rec_transit_DD.MM-HH:MM.csv

Usage:
    python3 analyse_transit.py                        # newest matched pair
    python3 analyse_transit.py 07.05-15:45            # specific suffix (send or rec)
    python3 analyse_transit.py path/to/send_transit_07.05-15:45.csv  # explicit file

Output:
    - Colour-coded terminal summary (per camera + overall)
    - CSV saved to ../logs/transit/transit_result_DD.MM-HH:MM.csv
    - JSON report saved to ../logs/transit/transit_result_DD.MM-HH:MM.json
"""

import sys
import re
import csv
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR          = Path("../logs/transit")
WIGGLE_MINUTES   = 1          # max allowed timestamp difference between pair
TRANSIT_WARN_MS  = 100.0       # yellow flag above this
TRANSIT_CRIT_MS  = 500.0       # red flag above this
RTP_GAP_THRESHOLD = 15

# RTP rollover
MAX_SEQ      = 65536
ROLLOVER_GAP = 32768

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
# File discovery — with ±1 min wiggle room
# ──────────────────────────────────────────────────────────────────────────────

SUFFIX_RE  = re.compile(r"(?:send|rec)_transit_(\d{2}\.\d{2}-\d{2}:\d{2})\.csv$")

def parse_suffix(suffix: str) -> datetime:
    """Parse DD.MM-HH:MM into a datetime (year=2000, irrelevant)."""
    return datetime.strptime(suffix, "%d.%m-%H:%M")

def suffix_minutes(suffix: str) -> int:
    """Suffix as total minutes (day*1440 + hour*60 + min) for easy delta."""
    dt = parse_suffix(suffix)
    return dt.day * 1440 + dt.hour * 60 + dt.minute

def minutes_apart(s1: str, s2: str) -> int:
    return abs(suffix_minutes(s1) - suffix_minutes(s2))

def find_all_suffixes(log_dir: Path) -> tuple[list[str], list[str]]:
    """Return (send_suffixes, rec_suffixes) found in log_dir."""
    send, rec = [], []
    for f in log_dir.glob("*.csv"):
        m = SUFFIX_RE.match(f.name)
        if not m:
            continue
        suffix = m.group(1)
        if f.name.startswith("send_"):
            send.append(suffix)
        elif f.name.startswith("rec_"):
            rec.append(suffix)
    return sorted(send), sorted(rec)

def best_pair(send_suffixes: list[str], rec_suffixes: list[str],
              wiggle: int = WIGGLE_MINUTES) -> tuple[str, str] | None:
    """
    Find the best send/rec pair: exact timestamp match (delta=0) wins over
    wiggle matches. Within each tier, pick by newest file mtime.
    Returns None if no pair found within wiggle minutes.
    """
    def pair_mtime(pair):
        ss, rs = pair
        return max(
            (LOG_DIR / f"send_transit_{ss}.csv").stat().st_mtime,
            (LOG_DIR / f"rec_transit_{rs}.csv").stat().st_mtime,
        )

    exact, fuzzy = [], []
    for ss in send_suffixes:
        for rs in rec_suffixes:
            delta = minutes_apart(ss, rs)
            if delta == 0:
                exact.append((ss, rs))
            elif delta <= wiggle:
                fuzzy.append((ss, rs))

    if exact:
        return max(exact, key=pair_mtime)   # prefer exact match, newest first
    if fuzzy:
        return max(fuzzy, key=pair_mtime)   # fall back to wiggle, newest first
    return None

def resolve_pair(arg: str | None, log_dir: Path) -> tuple[Path, Path, str, str]:
    """
    Returns (send_path, rec_path, send_suffix, rec_suffix).
    arg = None         → newest matched pair
    arg = 'DD.MM-HH:MM' → treat as send suffix, find best rec match
    arg = path         → extract suffix, find best match for the other side
    """
    if arg is None:
        send_suf, rec_suf = find_all_suffixes(log_dir)
        pair = best_pair(send_suf, rec_suf)
        if pair is None:
            raise FileNotFoundError(
                f"No send/rec transit pair within ±{WIGGLE_MINUTES} min found in {log_dir}"
            )
        ss, rs = pair
        cprint(f"\n[INFO] Auto-selected newest matched pair:", GREY)
    else:
        p = Path(arg)
        if p.exists():
            m = SUFFIX_RE.match(p.name)
            if not m:
                raise ValueError(f"Cannot extract suffix from: {p.name}")
            log_dir = p.parent
            given = m.group(1)
            is_send = p.name.startswith("send_")
        else:
            given  = arg
            is_send = True  # assume send if bare suffix given; will find best rec

        send_suf, rec_suf = find_all_suffixes(log_dir)

        if is_send:
            matches = [(given, rs) for rs in rec_suf if minutes_apart(given, rs) <= WIGGLE_MINUTES]
            if not matches:
                raise FileNotFoundError(
                    f"No rec_transit file within ±{WIGGLE_MINUTES} min of send suffix '{given}'"
                )
            ss, rs = min(matches, key=lambda p: minutes_apart(p[0], p[1]))
        else:
            matches = [(ss, given) for ss in send_suf if minutes_apart(ss, given) <= WIGGLE_MINUTES]
            if not matches:
                raise FileNotFoundError(
                    f"No send_transit file within ±{WIGGLE_MINUTES} min of rec suffix '{given}'"
                )
            ss, rs = min(matches, key=lambda p: minutes_apart(p[0], p[1]))

    send_path = log_dir / f"send_transit_{ss}.csv"
    rec_path  = log_dir / f"rec_transit_{rs}.csv"

    for path in (send_path, rec_path):
        if not path.exists():
            raise FileNotFoundError(f"Expected file not found: {path}")

    delta = minutes_apart(ss, rs)
    cprint(f"[INFO] send : {send_path.name}", GREY)
    cprint(f"[INFO] rec  : {rec_path.name}  (Δ{delta} min)", GREY)

    return send_path, rec_path, ss, rs


# ──────────────────────────────────────────────────────────────────────────────
# Loading — with RTP rollover normalisation
# ──────────────────────────────────────────────────────────────────────────────

def normalize_seq(seq, prev_seq, rollover_count):
    if prev_seq is not None:
        delta = seq - prev_seq
        if delta < -ROLLOVER_GAP:
            rollover_count += 1
        elif delta > ROLLOVER_GAP:
            rollover_count -= 1
    return seq + rollover_count * MAX_SEQ, rollover_count

def load_transit(path: Path) -> dict:
    """Returns {(cam_index, norm_rtp_seq): abs_time_float}"""
    records         = {}
    rollover_counts = defaultdict(int)
    prev_seqs       = defaultdict(lambda: None)

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cam = int(row["cam_index"])
            seq = int(row["rtp_seq"])
            t   = float(row["abs_time"])
            norm_seq, rollover_counts[cam] = normalize_seq(
                seq, prev_seqs[cam], rollover_counts[cam]
            )
            prev_seqs[cam] = seq
            records[(cam, norm_seq)] = t

    return records


# ──────────────────────────────────────────────────────────────────────────────
# Stats helpers
# ──────────────────────────────────────────────────────────────────────────────

def pcts(arr, ps=(50, 90, 95, 99)):
    return {f"p{p}": round(float(np.percentile(arr, p)), 4) for p in ps}

def full_stats(arr, warn, crit):
    a = np.array(arr)
    return {
        "n":        len(a),
        "mean_ms":  round(float(a.mean()), 4),
        "std_ms":   round(float(a.std()),  4),
        "min_ms":   round(float(a.min()),  4),
        "max_ms":   round(float(a.max()),  4),
        **pcts(a),
        f"spikes_above_{warn}ms":     int((a > warn).sum()),
        f"spikes_above_{warn}ms_pct": round(100 * float((a > warn).mean()), 3),
        f"spikes_above_{crit}ms":     int((a > crit).sum()),
        f"spikes_above_{crit}ms_pct": round(100 * float((a > crit).mean()), 3),
    }

def print_stats_block(st, warn, crit):
    col = GREEN if st["mean_ms"] < warn else YELLOW
    cprint(f"  Mean   : {st['mean_ms']:.4f} ms  (σ = {st['std_ms']:.4f})", col)
    print( f"  Min    : {st['min_ms']:.4f} ms")
    print( f"  Max    : {st['max_ms']:.4f} ms")
    print( f"  p50    : {st['p50']:.4f}   p90 : {st['p90']:.4f}")
    print( f"  p95    : {st['p95']:.4f}   p99 : {st['p99']:.4f}")
    w_n = st[f"spikes_above_{warn}ms"]
    c_n = st[f"spikes_above_{crit}ms"]
    cprint(f"  Spikes > {warn} ms : {w_n:,}  ({st[f'spikes_above_{warn}ms_pct']:.2f}%)",
           YELLOW if w_n > 0 else GREEN)
    cprint(f"  Spikes > {crit} ms : {c_n:,}  ({st[f'spikes_above_{crit}ms_pct']:.2f}%)",
           RED if c_n > 0 else GREEN)


# ──────────────────────────────────────────────────────────────────────────────
# Main analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyse(send_path: Path, rec_path: Path, send_suf: str, rec_suf: str) -> dict:

    send_records = load_transit(send_path)
    rec_records  = load_transit(rec_path)

    # Join on (cam, seq)
    matched = []
    for key, t_send in send_records.items():
        if key in rec_records:
            transit_ms = (rec_records[key] - t_send) * 1000
            cam, seq   = key
            matched.append((cam, seq, transit_ms))
    for key, t_rec in rec_records.items():
        if key not in send_records:
            cam, seq = key
            cprint(f"  [WARN] Unmatched rec record: cam={cam} seq={seq} abs_time={t_rec:.4f}", YELLOW)

    n_send    = len(send_records)
    n_rec     = len(rec_records)
    n_matched = len(matched)
    n_lost    = n_send - n_matched
    loss_pct  = 100 * n_lost / n_send if n_send > 0 else 0.0

    report = {
        "send_file":   send_path.name,
        "rec_file":    rec_path.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "matching": {
            "send_packets":    n_send,
            "rec_packets":     n_rec,
            "matched":         n_matched,
            "unmatched":       n_lost,
            "loss_pct":        round(loss_pct, 3),
        }
    }

    if n_matched == 0:
        section("ERROR")
        cprint("  No matching RTP sequence numbers found.", RED, bold=True)
        cprint("  Check that both files are from the same recording session.", RED)
        return report

    all_transit = [t for _, _, t in matched]

    # ── Overview ──────────────────────────────────────────────────────────────
    section("MATCHING OVERVIEW")
    print(f"  Send packets   : {n_send:,}")
    print(f"  Rec packets    : {n_rec:,}")
    print(f"  Matched pairs  : {n_matched:,}")
    loss_col = RED if loss_pct > 5 else YELLOW if loss_pct > 1 else GREEN
    cprint(f"  Unmatched      : {n_lost:,}  ({loss_pct:.2f}%)", loss_col)

    # ── Global transit stats ──────────────────────────────────────────────────
    section("TRANSIT LATENCY — all cameras")
    global_st = full_stats(all_transit, TRANSIT_WARN_MS, TRANSIT_CRIT_MS)
    print_stats_block(global_st, TRANSIT_WARN_MS, TRANSIT_CRIT_MS)
    report["transit_global"] = global_st

    # ── Per-camera ────────────────────────────────────────────────────────────
    section("TRANSIT LATENCY — per camera")
    cam_results = defaultdict(list)
    for cam, seq, t in matched:
        cam_results[cam].append(t)

    report["transit_per_cam"] = {}
    for cam_id in sorted(cam_results):
        vals = cam_results[cam_id]
        st   = full_stats(vals, TRANSIT_WARN_MS, TRANSIT_CRIT_MS)
        report["transit_per_cam"][cam_id] = st
        col = GREEN if st["mean_ms"] < TRANSIT_WARN_MS else YELLOW
        cprint(f"\n  Cam {cam_id}  ({st['n']:,} packets)", bold=True)
        cprint(f"    mean={st['mean_ms']:.4f} ms  std={st['std_ms']:.4f}  max={st['max_ms']:.4f}", col)
        print( f"    p50={st['p50']}  p90={st['p90']}  p95={st['p95']}  p99={st['p99']}")
        w_pct = st[f"spikes_above_{TRANSIT_WARN_MS}ms_pct"]
        c_pct = st[f"spikes_above_{TRANSIT_CRIT_MS}ms_pct"]
        cprint(f"    spikes > {TRANSIT_WARN_MS} ms: {w_pct:.2f}%   > {TRANSIT_CRIT_MS} ms: {c_pct:.2f}%",
               YELLOW if w_pct > 0 else GREEN)

    # ── Negative transit check ────────────────────────────────────────────────
    section("CLOCK SYNC CHECK")
    negatives = [(cam, seq, t) for cam, seq, t in matched if t < 0]
    n_neg = len(negatives)
    neg_col = RED if n_neg > 10 else YELLOW if n_neg > 0 else GREEN
    cprint(f"  Negative transit values : {n_neg}", neg_col)
    if n_neg > 0:
        neg_ms = [t for _, _, t in negatives]
        cprint(f"  Min negative            : {min(neg_ms):.4f} ms", YELLOW)
        cprint(f"  (Negative values suggest clock skew between sender and receiver)", GREY)
    else:
        cprint("  ✓ No negative transit times — clocks appear well synced", GREEN)
    report["clock_sync"] = {
        "negative_transit_count": n_neg,
        "min_transit_ms": round(float(min(all_transit)), 4),
    }

    # ── Health summary ────────────────────────────────────────────────────────
    section("HEALTH SUMMARY")
    issues = []
    c_pct = global_st[f"spikes_above_{TRANSIT_CRIT_MS}ms_pct"]
    w_pct = global_st[f"spikes_above_{TRANSIT_WARN_MS}ms_pct"]
    if c_pct > 0:
        issues.append(f"WARNING  {c_pct:.2f}% of packets exceed {TRANSIT_CRIT_MS} ms transit")
    elif w_pct > 5:
        issues.append(f"NOTICE   {w_pct:.2f}% of packets exceed {TRANSIT_WARN_MS} ms transit")
    if loss_pct > 5:
        issues.append(f"WARNING  {loss_pct:.2f}% of sent packets had no matching receive record")
    elif loss_pct > 1:
        issues.append(f"NOTICE   {loss_pct:.2f}% unmatched packets")
    if n_neg > 10:
        issues.append(f"WARNING  {n_neg} negative transit values — possible clock skew")
    elif n_neg > 0:
        issues.append(f"NOTICE   {n_neg} negative transit values — minor clock skew")

    report["health"] = {"status": "OK" if not issues else "ISSUES", "issues": issues}
    if not issues:
        cprint("  ✓  No issues detected", GREEN, bold=True)
    else:
        for issue in issues:
            col = RED if "WARNING" in issue else YELLOW
            cprint(f"  ⚠  {issue}", col)

    return report, matched


# ──────────────────────────────────────────────────────────────────────────────
# Save outputs
# ──────────────────────────────────────────────────────────────────────────────

def save_outputs(matched: list, report: dict, log_dir: Path, send_suf: str):
    timestamp   = datetime.now().strftime("%d.%m-%H:%M")
    base        = log_dir / f"transit_result_{send_suf}"
    csv_path    = base.with_suffix(".csv")
    json_path   = base.with_suffix(".json")

    # CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["cam_index", "rtp_seq", "transit_ms"])
        for cam, seq, transit_ms in sorted(matched):
            writer.writerow([cam, seq, f"{transit_ms:.4f}"])

    # JSON
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    section("OUTPUTS SAVED")
    cprint(f"  CSV  : {csv_path}", CYAN)
    cprint(f"  JSON : {json_path}", CYAN)
    print()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not LOG_DIR.exists() and arg is None:
        print(f"[ERROR] Log directory not found: {LOG_DIR}")
        sys.exit(1)

    send_path, rec_path, send_suf, rec_suf = resolve_pair(arg, LOG_DIR)

    result = analyse(send_path, rec_path, send_suf, rec_suf)

    # analyse() returns (report, matched) or just report on error
    if isinstance(result, tuple):
        report, matched = result
        save_outputs(matched, report, send_path.parent, send_suf)
    else:
        report = result

if __name__ == "__main__":
    main()
