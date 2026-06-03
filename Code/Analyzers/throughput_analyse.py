#!/usr/bin/env python3
"""
throughput_analyse.py  –  Analyse sender throughput CSV logs.

Usage:
    python3 throughput_analyse.py <filename>

Example:
    python3 throughput_analyse.py 15_05-18:51.csv

The script looks for the file at:
    ../logs/throughput/sender_throughput_<filename>

The CSV may optionally start with a comment line like:
    # skip_seconds: 10
If present, that many seconds of data are dropped from the start before stats
are computed (warm-up / ramp-up period).
"""

import sys
import os
import csv
import statistics
import re
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker


# ── helpers ──────────────────────────────────────────────────────────────────

def die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_file(path: str):
    """Return (skip_seconds, rows) where rows = list of (wall_time_str, tx_mbps, parsed_time)."""
    skip_seconds = 10  # default: always skip first 10 s warm-up
    rows = []

    with open(path, newline="") as fh:
        for raw_line in fh:
            line = raw_line.strip()

            # Comment / metadata line  →  look for  "# skip_seconds: N"
            if line.startswith("#"):
                m = re.search(r"skip_seconds\s*[:=]\s*(\d+)", line, re.IGNORECASE)
                if m:
                    skip_seconds = int(m.group(1))
                continue

            # Header row
            if line.lower().startswith("wall_time"):
                continue

            # Data row
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                tx = float(parts[2])
                t  = datetime.strptime(parts[0].strip(), "%H:%M:%S")
            except ValueError:
                continue
            rows.append((parts[0].strip(), tx, t))

    return skip_seconds, rows


def compute_stats(values: list[float]):
    values_sorted = sorted(values)
    n = len(values_sorted)
    p95_idx = int(0.95 * n) - 1  # 0-based; clamp to valid range
    p95_idx = max(0, min(p95_idx, n - 1))
    return {
        "count":  n,
        "mean":   statistics.mean(values_sorted),
        "min":    min(values_sorted),
        "max":    max(values_sorted),
        "p95":    values_sorted[p95_idx],
        "stdev":  statistics.stdev(values_sorted) if n > 1 else 0.0,
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 2:
        die("Usage: python3 throughput_analyse.py <filename>\n"
            "  e.g.  python3 throughput_analyse.py 15_05-18:51.csv")

    suffix   = sys.argv[1]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, "..", "logs", "throughput",
                            f"sender_throughput_{suffix}")
    csv_path = os.path.normpath(csv_path)

    if not os.path.isfile(csv_path):
        die(f"File not found: {csv_path}")

    skip_seconds, rows = parse_file(csv_path)

    if not rows:
        die("No data rows found in file.")

    # Drop warm-up rows based on elapsed wall time
    if skip_seconds > 0:
        t0 = rows[0][2]
        rows = [r for r in rows if (r[2] - t0).seconds >= skip_seconds]
        print(f"Skipping first {skip_seconds} second(s) of data (warm-up).")

    if not rows:
        die("No data left after skipping warm-up rows.")

    times  = [r[0] for r in rows]
    values = [r[1] for r in rows]
    st     = compute_stats(values)

    # ── stats lines ──────────────────────────────────────────────────────────
    label_w = 10
    stat_lines = [
        "=" * 42,
        f"  Throughput stats  –  {suffix}",
        "=" * 42,
        f"  {'Samples':<{label_w}} {st['count']}",
        f"  {'Mean':<{label_w}} {st['mean']:.3f} Mbps",
        f"  {'Min':<{label_w}} {st['min']:.3f} Mbps",
        f"  {'Max':<{label_w}} {st['max']:.3f} Mbps",
        f"  {'P95':<{label_w}} {st['p95']:.3f} Mbps",
        f"  {'Std dev':<{label_w}} {st['stdev']:.3f} Mbps",
        "=" * 42,
    ]
    print("\n" + "\n".join(stat_lines) + "\n")

    # ── plot ─────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 5))

    x = range(len(values))
    ax.set_ylim(0, max(values) * 1.1)
    ax.plot(x, values, color="#4C9BE8", linewidth=1.0, label="tx_mbps")
    ax.axhline(st["mean"], color="#E8954C", linewidth=1.5,
               linestyle="--", label=f"Mean  {st['mean']:.2f}")
    ax.axhline(st["p95"],  color="#D94F4F", linewidth=1.5,
               linestyle=":",  label=f"P95   {st['p95']:.2f}")
    ax.axhline(st["min"],  color="#6DBD6D", linewidth=1.0,
               linestyle="-.", label=f"Min   {st['min']:.2f}")
    ax.axhline(st["max"],  color="#B56DBD", linewidth=1.0,
               linestyle="-.", label=f"Max   {st['max']:.2f}")

    # X-axis: show ~10 evenly spaced time labels
    n = len(times)
    step = max(1, n // 10)
    tick_positions = list(range(0, n, step))
    tick_labels    = [times[i] for i in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=30, ha="right", fontsize=8)

    ax.set_xlabel("Wall time", fontsize=10)
    ax.set_ylabel("Throughput (Mbps)", fontsize=10)
    ax.set_title(f"Sender throughput  –  {suffix}", fontsize=12, fontweight="bold")
    ax.legend().set_visible(False)
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax.grid(True, which="major", linestyle="--", alpha=0.4)
    ax.grid(True, which="minor", linestyle=":",  alpha=0.2)

    plt.tight_layout()

    # Save under graphs/<timestamp>/
    timestamp = suffix.replace(".csv", "")
    out_dir   = os.path.join(script_dir, "graphs", timestamp)
    os.makedirs(out_dir, exist_ok=True)
    out_name  = f"throughput_{timestamp.replace(':', '-').replace('/', '_')}.png"
    out_path  = os.path.join(out_dir, out_name)
    plt.savefig(out_path, dpi=150)
    print(f"Graph saved → {out_path}")

    txt_name = f"throughput_{timestamp.replace(':', '-').replace('/', '_')}_stats.txt"
    txt_path = os.path.join(out_dir, txt_name)
    with open(txt_path, "w") as f:
        f.write("\n".join(stat_lines) + "\n")
    print(f"Stats saved → {txt_path}")

    plt.show()


if __name__ == "__main__":
    main()
