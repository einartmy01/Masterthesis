#!/usr/bin/env python3
"""
combine_transit.py  —  Transit Analyser V5
Combines send/rec transit CSVs, skips the first N seconds of data,
and writes a single result CSV.

Usage:
    python3 combine_transit.py [time_suffix]

Examples:
    python3 combine_transit.py 15.05-18:51.csv
    python3 combine_transit.py 15.05-18:51
    python3 combine_transit.py send_transit_15.05-18:51.csv
    python3 combine_transit.py              # auto-picks newest matched pair
"""

import sys
import re
import csv
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR        = Path("../logs/transit")
GRAPHS_DIR     = Path("graphs")
WIGGLE_MINUTES = 1      # max allowed timestamp difference between matched pair
SKIP_FIRST_N_SECONDS = 10.0   # skip rows whose abs_time falls in the first N
                               # seconds after the earliest abs_time in the file

# RTP rollover
MAX_SEQ      = 65536
ROLLOVER_GAP = 32768

# ──────────────────────────────────────────────────────────────────────────────
# File discovery — with ±1 min wiggle room
# ──────────────────────────────────────────────────────────────────────────────

SUFFIX_RE = re.compile(r"(?:send|rec)_transit_(\d{2}\.\d{2}-\d{2}:\d{2})\.csv$")
# Also match bare time suffix (no send_/rec_ prefix)
BARE_SUFFIX_RE = re.compile(r"^(\d{2}\.\d{2}-\d{2}:\d{2})(?:\.csv)?$")


def parse_suffix(suffix: str) -> datetime:
    return datetime.strptime(suffix, "%d.%m-%H:%M")


def suffix_minutes(suffix: str) -> int:
    dt = parse_suffix(suffix)
    return dt.day * 1440 + dt.hour * 60 + dt.minute


def minutes_apart(s1: str, s2: str) -> int:
    return abs(suffix_minutes(s1) - suffix_minutes(s2))


def find_all_suffixes(log_dir: Path) -> tuple[list[str], list[str]]:
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
              log_dir: Path, wiggle: int = WIGGLE_MINUTES):
    def pair_mtime(pair):
        ss, rs = pair
        return max(
            (log_dir / f"send_transit_{ss}.csv").stat().st_mtime,
            (log_dir / f"rec_transit_{rs}.csv").stat().st_mtime,
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
        return max(exact, key=pair_mtime)
    if fuzzy:
        return max(fuzzy, key=pair_mtime)
    return None


def resolve_pair(arg: str | None, log_dir: Path) -> tuple[Path, Path, str, str]:
    """Return (send_path, rec_path, send_suffix, rec_suffix)."""

    if arg is None:
        # Auto-select newest matched pair
        send_suf, rec_suf = find_all_suffixes(log_dir)
        pair = best_pair(send_suf, rec_suf, log_dir)
        if pair is None:
            raise FileNotFoundError(
                f"No send/rec transit pair within ±{WIGGLE_MINUTES} min found in {log_dir}"
            )
        ss, rs = pair
        print(f"[INFO] Auto-selected newest matched pair:")
    else:
        # Strip any directory component to get just the filename
        filename = Path(arg).name

        # Try to extract a bare time suffix (e.g. "15.05-18:51" or "15.05-18:51.csv")
        bare = BARE_SUFFIX_RE.match(filename)
        full = SUFFIX_RE.match(filename)

        if bare:
            given   = bare.group(1)
            log_dir = Path(arg).parent if Path(arg).parent != Path(".") else log_dir
            is_send = True  # bare suffix — match to best rec
        elif full:
            given   = full.group(1)
            log_dir = Path(arg).parent if Path(arg).parent != Path(".") else log_dir
            is_send = filename.startswith("send_")
        else:
            raise ValueError(
                f"Cannot extract time suffix from: {filename!r}\n"
                f"Expected format: 15.05-18:51.csv or send_transit_15.05-18:51.csv"
            )

        send_suf_list, rec_suf_list = find_all_suffixes(log_dir)

        if is_send:
            matches = [
                (given, rs) for rs in rec_suf_list
                if minutes_apart(given, rs) <= WIGGLE_MINUTES
            ]
            if not matches:
                raise FileNotFoundError(
                    f"No rec_transit file within ±{WIGGLE_MINUTES} min of suffix '{given}' in {log_dir}"
                )
            ss, rs = min(matches, key=lambda p: minutes_apart(p[0], p[1]))
        else:
            matches = [
                (ss, given) for ss in send_suf_list
                if minutes_apart(ss, given) <= WIGGLE_MINUTES
            ]
            if not matches:
                raise FileNotFoundError(
                    f"No send_transit file within ±{WIGGLE_MINUTES} min of suffix '{given}' in {log_dir}"
                )
            ss, rs = min(matches, key=lambda p: minutes_apart(p[0], p[1]))

    send_path = log_dir / f"send_transit_{ss}.csv"
    rec_path  = log_dir / f"rec_transit_{rs}.csv"

    for path in (send_path, rec_path):
        if not path.exists():
            raise FileNotFoundError(f"Expected file not found: {path}")

    delta = minutes_apart(ss, rs)
    print(f"[INFO] send : {send_path.name}")
    print(f"[INFO] rec  : {rec_path.name}  (Δ{delta} min)")

    return send_path, rec_path, ss, rs


# ──────────────────────────────────────────────────────────────────────────────
# Loading — skip first N seconds, with RTP rollover normalisation
# ──────────────────────────────────────────────────────────────────────────────

def normalize_seq(seq: int, prev_seq, rollover_count: int) -> tuple[int, int]:
    if prev_seq is not None:
        delta = seq - prev_seq
        if delta < -ROLLOVER_GAP:
            rollover_count += 1
        elif delta > ROLLOVER_GAP:
            rollover_count -= 1
    return seq + rollover_count * MAX_SEQ, rollover_count


def load_transit(path: Path, skip_seconds: float) -> dict:
    """
    Returns {(cam_index, norm_rtp_seq): abs_time_float}

    Rows whose abs_time falls within the first `skip_seconds` seconds
    after the file's earliest abs_time are silently discarded.

    Also handles an old-style 'deltatime' column in place of 'abs_time':
    if 'abs_time' is absent but 'deltatime' is present the cumulative sum
    of deltatime (seconds) is used as the absolute timestamp.
    """
    raw_rows = []

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        has_abs_time  = "abs_time"  in fieldnames
        has_deltatime = "deltatime" in fieldnames

        if not has_abs_time and not has_deltatime:
            raise ValueError(
                f"{path.name}: expected column 'abs_time' or 'deltatime', "
                f"got: {fieldnames}"
            )

        cumulative_time = 0.0
        for row in reader:
            cam = int(row["cam_index"])
            seq = int(row["rtp_seq"])

            if has_abs_time:
                t = float(row["abs_time"])
            else:
                # old deltatime format: delta is in seconds (float)
                cumulative_time += float(row["deltatime"])
                t = cumulative_time

            raw_rows.append((cam, seq, t))

    if not raw_rows:
        return {}

    # Determine cutoff based on the earliest timestamp in the file
    t_min   = min(t for _, _, t in raw_rows)
    cutoff  = t_min + skip_seconds

    records         = {}
    rollover_counts = defaultdict(int)
    prev_seqs       = defaultdict(lambda: None)

    for cam, seq, t in raw_rows:
        if t < cutoff:
            continue  # skip warm-up period

        norm_seq, rollover_counts[cam] = normalize_seq(
            seq, prev_seqs[cam], rollover_counts[cam]
        )
        prev_seqs[cam] = seq
        records[(cam, norm_seq)] = t

    skipped = sum(1 for _, _, t in raw_rows if t < cutoff)
    kept    = len(raw_rows) - skipped
    print(f"[INFO] {path.name}: {len(raw_rows):,} rows read, {skipped:,} skipped (first {skip_seconds}s), {kept:,} kept")

    return records


# ──────────────────────────────────────────────────────────────────────────────
# Match & write CSV
# ──────────────────────────────────────────────────────────────────────────────

def combine(send_path: Path, rec_path: Path,
            send_suf: str, skip_seconds: float) -> Path:

    send_records = load_transit(send_path, skip_seconds)
    rec_records  = load_transit(rec_path,  0.0)

    matched = []
    for key, t_send in send_records.items():
        if key in rec_records:
            transit_ms = (rec_records[key] - t_send) * 1000
            cam, seq   = key
            matched.append((cam, seq, transit_ms))

    n_send    = len(send_records)
    n_rec     = len(rec_records)
    n_matched = len(matched)
    rec_only  = n_rec - n_matched   # receiver packets with no matching sender
    eff_rec   = n_rec - rec_only    # == n_matched
    n_lost    = n_send - n_matched
    loss_pct  = 100 * n_lost / n_send if n_send > 0 else 0.0

    transit_values = [t for _, _, t in matched]
    min_transit    = min(transit_values)
    max_transit    = max(transit_values)
    mean_transit   = sum(transit_values) / len(transit_values)

    stat_lines = [
        f"Sender packets       : {n_send:,}",
        f"Receiver packets     : {n_rec:,}  (raw)",
        f"  - no-sender pkts   : {rec_only:,}",
        f"  = effective rec    : {eff_rec:,}",
        f"Matched              : {n_matched:,}",
        f"Packet loss          : {n_lost:,}  ({loss_pct:.2f}%)",
        f"",
        f"Transit (ms) — raw",
        f"  Min                : {min_transit:.4f}",
        f"  Max                : {max_transit:.4f}",
        f"  Mean               : {mean_transit:.4f}",
    ]

    negative_min = min_transit < 0
    if negative_min:
        offset  = -min_transit
        adj_min = 0.0
        adj_max      = max_transit + offset
        adj_mean     = mean_transit + offset
        stat_lines += [
            f"",
            f"Transit (ms) — adjusted (offset +{offset:.4f} ms, min→0)",
            f"  NOTE: CSV uses adjusted values",
            f"  Min                : {adj_min:.4f}",
            f"  Max                : {adj_max:.4f}",
            f"  Mean               : {adj_mean:.4f}",
        ]

    out_dir = GRAPHS_DIR / send_suf
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_path = out_dir / f"transit_stats_{send_suf}.txt"
    with open(stats_path, "w") as f:
        f.write("\n".join(stat_lines) + "\n")
    print(f"[OK]   Stats TXT     : {stats_path}")

    if n_matched == 0:
        print("[ERROR] No matching RTP sequence numbers found. "
              "Check that both files are from the same session.")
        sys.exit(1)

    out_path = send_path.parent / f"transit_result_{send_suf}.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["cam_index", "rtp_seq", "transit_ms"])
        if negative_min:
            rows = [(cam, seq, t + offset) for cam, seq, t in sorted(matched)]
        else:
            rows = sorted(matched)
        for cam, seq, transit_ms in rows:
            writer.writerow([cam, seq, f"{transit_ms:.4f}"])

    if negative_min:
        print(f"[INFO] Negative min transit detected ({min_transit:.4f} ms); "
              f"CSV values offset by +{offset:.4f} ms")
    print(f"[OK]   Result CSV   : {out_path}")
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not LOG_DIR.exists() and arg is None:
        print(f"[ERROR] Log directory not found: {LOG_DIR}")
        sys.exit(1)

    send_path, rec_path, send_suf, rec_suf = resolve_pair(arg, LOG_DIR)

    combine(send_path, rec_path, send_suf, SKIP_FIRST_N_SECONDS)


if __name__ == "__main__":
    main()
