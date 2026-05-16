#!/usr/bin/env python3
"""
cpu_analyse.py  –  Analyse sar CPU logs (total system, all cores combined)
Usage: python3 cpu_analyse.py 15.05-18:51.log
Files are read from  ../logs/cpu/sender_cpu_<argument>

Log is produced by:
    sar -u ALL 1   (one summary line per second, 'all' cores combined)
"""

import sys
import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ─── Configuration ────────────────────────────────────────────────────────────
SKIP_SECONDS = 10          # <-- change this to ignore the first N seconds
# ──────────────────────────────────────────────────────────────────────────────


def parse_log(path: str):
    """
    Parse sar -u ALL log and return:
        timestamps, cpu_total, cpu_usr, cpu_system
    Only reads 'all' rows (system-wide summary).
    Skips the first SKIP_SECONDS samples.

    sar row format:
        TIME  AM/PM  all  %usr  %nice  %system  %iowait  %steal  %irq  %soft  %guest  %gnice  %idle
        idx:   0      1    2     3      4        5        6       7     8      9       10      11     12
    """
    timestamps  = []
    cpu_total   = []
    cpu_usr     = []
    cpu_system  = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()

            # Only process 'all' summary rows
            if len(parts) < 13:
                continue
            if parts[2] != "all":
                continue
            # Skip header row
            if parts[3] == "%usr":
                continue

            try:
                usr    = float(parts[3])
                system = float(parts[5])
                idle   = float(parts[12])
                total  = 100.0 - idle

                ts = f"{parts[0]} {parts[1]}"
                timestamps.append(ts)
                cpu_total.append(total)
                cpu_usr.append(usr)
                cpu_system.append(system)
            except (ValueError, IndexError):
                continue

    # Skip warmup period
    if SKIP_SECONDS > 0 and len(cpu_total) > SKIP_SECONDS:
        timestamps = timestamps[SKIP_SECONDS:]
        cpu_total  = cpu_total[SKIP_SECONDS:]
        cpu_usr    = cpu_usr[SKIP_SECONDS:]
        cpu_system = cpu_system[SKIP_SECONDS:]

    return timestamps, cpu_total, cpu_usr, cpu_system


def stats(values):
    arr = np.array(values)
    return {
        "count": len(arr),
        "mean":  np.mean(arr),
        "min":   np.min(arr),
        "max":   np.max(arr),
        "p95":   np.percentile(arr, 95),
        "std":   np.std(arr),
    }


def plot(timestamps, cpu_total, cpu_usr, cpu_system, s, filepath):
    x = list(range(len(cpu_total)))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                                    gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#1e1e2e")
    for ax in (ax1, ax2):
        ax.set_facecolor("#1e1e2e")
        ax.spines[:].set_color("#313244")
        ax.grid(axis="y", color="#313244", linewidth=0.6)
        ax.tick_params(colors="#cdd6f4")

    # ── Top chart: total CPU ──────────────────────────────────────────────────
    ax1.plot(x, cpu_total, color="#89b4fa", linewidth=1.2, label="Total %CPU (all cores)")
    ax1.fill_between(x, cpu_total, alpha=0.15, color="#89b4fa")

    ax1.axhline(s["mean"], color="#a6e3a1", linewidth=1.2, linestyle="--",
                label=f"Mean  {s['mean']:.1f}%")
    ax1.axhline(s["p95"],  color="#f38ba8", linewidth=1.2, linestyle=":",
                label=f"P95   {s['p95']:.1f}%")
    ax1.axhline(s["max"],  color="#fab387", linewidth=1.0, linestyle="-.",
                label=f"Max   {s['max']:.1f}%")
    ax1.axhline(s["min"],  color="#94e2d5", linewidth=1.0, linestyle="-.",
                label=f"Min   {s['min']:.1f}%")

    ax1.set_ylim(0, 105)
    ax1.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f%%"))
    ax1.set_ylabel("Total CPU %", color="#cdd6f4", fontsize=9)
    ax1.set_title(
        f"System CPU Usage (all cores combined) — {os.path.basename(filepath)}\n"
        f"(first {SKIP_SECONDS}s skipped,  {s['count']} samples)",
        color="#cdd6f4", fontsize=11, pad=8)
    ax1.legend(loc="upper right", fontsize=8,
               facecolor="#313244", edgecolor="#45475a", labelcolor="#cdd6f4")

    # Stats box
    box_text = (f"Samples : {s['count']}\n"
                f"Mean    : {s['mean']:.2f}%\n"
                f"Min     : {s['min']:.2f}%\n"
                f"Max     : {s['max']:.2f}%\n"
                f"P95     : {s['p95']:.2f}%\n"
                f"Std dev : {s['std']:.2f}%")
    ax1.text(0.01, 0.97, box_text, transform=ax1.transAxes,
             verticalalignment="top", fontsize=8, fontfamily="monospace",
             color="#cdd6f4",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#313244", edgecolor="#45475a"))

    # ── Bottom chart: usr vs system breakdown ─────────────────────────────────
    ax2.plot(x, cpu_usr,    color="#cba6f7", linewidth=1.0, label="%usr")
    ax2.plot(x, cpu_system, color="#f38ba8", linewidth=1.0, label="%system")
    ax2.fill_between(x, cpu_usr,    alpha=0.15, color="#cba6f7")
    ax2.fill_between(x, cpu_system, alpha=0.15, color="#f38ba8")
    ax2.set_ylabel("Breakdown %", color="#cdd6f4", fontsize=9)
    ax2.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f%%"))
    ax2.legend(loc="upper right", fontsize=8,
               facecolor="#313244", edgecolor="#45475a", labelcolor="#cdd6f4")

    # ── X-axis ticks ──────────────────────────────────────────────────────────
    step = max(1, len(x) // 20)
    tick_pos    = x[::step]
    tick_labels = [timestamps[i] for i in tick_pos]
    ax2.set_xticks(tick_pos)
    ax2.set_xticklabels(tick_labels, rotation=45, ha="right",
                        fontsize=7, color="#cdd6f4")
    ax2.set_xlabel("Time", color="#cdd6f4", fontsize=9)

    plt.tight_layout()

    out = filepath.replace(".log", "").replace(".csv", "") + "_analysis.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Graph saved → {out}")
    plt.show()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 cpu_analyse.py <suffix>")
        print("  e.g. python3 cpu_analyse.py 15.05-18:51.log")
        sys.exit(1)

    suffix     = sys.argv[1]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path   = os.path.join(script_dir, "..", "logs", "cpu", f"sender_cpu_{suffix}")

    if not os.path.exists(log_path):
        print(f"Error: file not found: {log_path}")
        sys.exit(1)

    print(f"Reading : {log_path}")
    print(f"Skipping first {SKIP_SECONDS} seconds …")

    timestamps, cpu_total, cpu_usr, cpu_system = parse_log(log_path)

    if not cpu_total:
        print("No data found — is this a sar log? (expected 'all' rows)")
        sys.exit(1)

    s = stats(cpu_total)

    print(f"\n{'─'*35}")
    print(f"  Samples : {s['count']}")
    print(f"  Mean    : {s['mean']:.2f}%")
    print(f"  Min     : {s['min']:.2f}%")
    print(f"  Max     : {s['max']:.2f}%")
    print(f"  P95     : {s['p95']:.2f}%")
    print(f"  Std dev : {s['std']:.2f}%")
    print(f"{'─'*35}\n")

    plot(timestamps, cpu_total, cpu_usr, cpu_system, s, log_path)


if __name__ == "__main__":
    main()
