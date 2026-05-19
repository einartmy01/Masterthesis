#!/usr/bin/env python3
"""
Usage: python3 cpu_analyse.py 15.05-18:51.log
Files are read from  ../logs/cpu/sender_cpu_<argument>
"""

import sys
import os
import re
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ─── Configuration ────────────────────────────────────────────────────────────
SKIP_SECONDS = 10          # <-- change this to ignore the first N seconds
# ──────────────────────────────────────────────────────────────────────────────


def parse_log(path: str) -> tuple[list[str], list[float]]:
    timestamps, values = [], []

    with open(path) as f:
        for line in f:
            line = line.strip()
            # Skip header / summary lines
            if not line or line.startswith("Linux") or line.startswith("Average"):
                continue
            if "UID" in line or "PID" in line:
                continue

            parts = line.split()
            # pidstat rows: TIME AM/PM  UID  PID  %usr %system %guest %wait %CPU  CPU  Command
            # Time is parts[0] + parts[1] (e.g. "06:51:38" "PM")
            if len(parts) < 11:
                continue
            try:
                cpu = float(parts[8])   # %CPU column (TIME AM/PM UID PID %usr %system %guest %wait %CPU CPU Command)
                ts  = f"{parts[0]} {parts[1]}"
                timestamps.append(ts)
                values.append(cpu)
            except (ValueError, IndexError):
                continue

    if SKIP_SECONDS > 0 and len(values) > SKIP_SECONDS:
        timestamps = timestamps[SKIP_SECONDS:]
        values     = values[SKIP_SECONDS:]

    return timestamps, values


def stats(values: list[float]) -> dict:
    arr = np.array(values)
    return {
        "count":  len(arr),
        "mean":   np.mean(arr),
        "min":    np.min(arr),
        "max":    np.max(arr),
        "p95":    np.percentile(arr, 95),
        "std":    np.std(arr),
    }


def plot(timestamps: list[str], values: list[float], s: dict, filename: str):
    x = list(range(len(values)))

    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")

    # Main CPU line
    ax.plot(x, values, color="#89b4fa", linewidth=1.2, label="%CPU")
    ax.fill_between(x, values, alpha=0.15, color="#89b4fa")

    # Stat lines
    ax.axhline(s["mean"], color="#a6e3a1", linewidth=1.2, linestyle="--", label=f"Mean  {s['mean']:.1f}%")
    ax.axhline(s["p95"],  color="#f38ba8", linewidth=1.2, linestyle=":",  label=f"P95   {s['p95']:.1f}%")
    ax.axhline(s["max"],  color="#fab387", linewidth=1.0, linestyle="-.", label=f"Max   {s['max']:.1f}%")
    ax.axhline(s["min"],  color="#94e2d5", linewidth=1.0, linestyle="-.", label=f"Min   {s['min']:.1f}%")

    # X-axis: show a tick every ~30 seconds
    step = max(1, len(x) // 20)
    tick_pos   = x[::step]
    tick_labels = [timestamps[i] for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7, color="#cdd6f4")

    ax.set_ylim(0, min(110, s["max"] * 1.15))
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f%%"))
    ax.tick_params(axis="y", colors="#cdd6f4", labelsize=8)
    ax.spines[:].set_color("#313244")
    ax.grid(axis="y", color="#313244", linewidth=0.6)

    # Labels & title
    ax.set_title(f"CPU Usage — {os.path.basename(filename)}  "
                 f"(first {SKIP_SECONDS}s skipped,  {s['count']} samples)",
                 color="#cdd6f4", fontsize=11, pad=10)
    ax.set_xlabel("Time", color="#cdd6f4", fontsize=9)
    ax.set_ylabel("%CPU", color="#cdd6f4", fontsize=9)

    legend = ax.legend(loc="upper right", fontsize=8,
                       facecolor="#313244", edgecolor="#45475a", labelcolor="#cdd6f4")

    # Stats box
    box_text = (f"Samples : {s['count']}\n"
                f"Mean    : {s['mean']:.2f}%\n"
                f"Min     : {s['min']:.2f}%\n"
                f"Max     : {s['max']:.2f}%\n"
                f"P95     : {s['p95']:.2f}%\n"
                f"Std dev : {s['std']:.2f}%")
    ax.text(0.01, 0.97, box_text, transform=ax.transAxes,
            verticalalignment="top", fontsize=8, fontfamily="monospace",
            color="#cdd6f4", bbox=dict(boxstyle="round,pad=0.5",
                                       facecolor="#313244", edgecolor="#45475a"))

    plt.tight_layout()

    out = filename.replace(".log", "").replace(".csv", "") + "_analysis.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Graph saved → {out}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 cpu_analyse.py <suffix>")
        print("  e.g. python3 cpu_analyse.py 15.05-18:51.log")
        sys.exit(1)

    suffix   = sys.argv[1]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "..", "logs", "cpu", f"sender_cpu_{suffix}")

    if not os.path.exists(log_path):
        print(f"Error: file not found: {log_path}")
        sys.exit(1)

    print(f"Reading: {log_path}")
    print(f"Skipping first {SKIP_SECONDS} seconds …")

    timestamps, values = parse_log(log_path)

    if not values:
        print("No data found after parsing.")
        sys.exit(1)

    s = stats(values)

    print(f"\n{'─'*35}")
    print(f"  Samples : {s['count']}")
    print(f"  Mean    : {s['mean']:.2f}%")
    print(f"  Min     : {s['min']:.2f}%")
    print(f"  Max     : {s['max']:.2f}%")
    print(f"  P95     : {s['p95']:.2f}%")
    print(f"  Std dev : {s['std']:.2f}%")
    print(f"{'─'*35}\n")

    plot(timestamps, values, s, log_path)


if __name__ == "__main__":
    main()
