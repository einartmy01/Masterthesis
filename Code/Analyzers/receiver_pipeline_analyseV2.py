#!/usr/bin/env python3
"""
receiver_pipeline_analyse.py
Usage: python3 receiver_pipeline_analyse.py 15.05-19:56.csv

Reads ../logs/pipeline/receiver/rec_full_<arg>
- Ignores the first N seconds (set IGNORE_FIRST_SECONDS below)
- Ignores rows where skipped > 0
- Prints stats (mean, min, max, p95) for full_ms
- Prints skip summary
- Shows a simple graph
"""

import sys
import os
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# ── CONFIG ──────────────────────────────────────────────────────────────────
IGNORE_FIRST_SECONDS = 10   # <-- change this to skip more/less warmup time
# ────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 receiver_pipeline_analyse.py <filename>")
        print("Example: python3 receiver_pipeline_analyse.py 15.05-19:56.csv")
        sys.exit(1)

    filename = sys.argv[1]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(script_dir, "..", "logs", "pipeline", "receiver", f"rec_full_{filename}")

    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    print(f"\n📂  Loading: {filepath}")

    # ── LOAD ─────────────────────────────────────────────────────────────────
    df = pd.read_csv(filepath)

    # Parse wall_time as timedelta from midnight so we can do arithmetic
    df["wall_time"] = pd.to_timedelta(df["wall_time"])

    # ── SKIP WARMUP ──────────────────────────────────────────────────────────
    t_start = df["wall_time"].iloc[0]
    cutoff   = t_start + pd.Timedelta(seconds=IGNORE_FIRST_SECONDS)
    before   = len(df)
    df       = df[df["wall_time"] >= cutoff].copy()
    after    = len(df)

    print(f"⏩  Skipping first {IGNORE_FIRST_SECONDS}s  ({before - after} rows removed, {after} rows kept)")

    # ── SKIP SUMMARY (before filtering them out) ──────────────────────────────
    skipped_rows  = (df["skipped"] > 0).sum()
    skipped_total = df["skipped"].sum()

    print(f"\n{'─'*45}")
    print(f"  Rows with skipped frames : {skipped_rows}")
    print(f"  Total frames skipped     : {skipped_total}")
    print(f"{'─'*45}")

    # ── FILTER OUT SKIPPED ROWS FOR STATS ────────────────────────────────────
    clean = df[df["skipped"] == 0]["full_ms"]

    if clean.empty:
        print("\nNo clean rows left after filtering — cannot compute stats.")
        sys.exit(1)

    # ── STATS ─────────────────────────────────────────────────────────────────
    p95  = np.percentile(clean, 95)
    mean = clean.mean()
    mn   = clean.min()
    mx   = clean.max()
    cnt  = len(clean)

    print(f"\n  full_ms stats  (n={cnt:,} clean rows)")
    print(f"{'─'*45}")
    print(f"  Mean  : {mean:>10.3f} ms")
    print(f"  Min   : {mn:>10.3f} ms")
    print(f"  Max   : {mx:>10.3f} ms")
    print(f"  P95   : {p95:>10.3f} ms")
    print(f"{'─'*45}\n")

    # ── GRAPH ─────────────────────────────────────────────────────────────────
    # Convert wall_time to seconds-since-start for the x-axis
    df["t_sec"] = (df["wall_time"] - t_start).dt.total_seconds()

    fig, axes = plt.subplots(2, 1, figsize=(12, 7))
    fig.suptitle(f"Receiver Pipeline — {filename}", fontsize=13, fontweight="bold")

    # -- Top: time-series of full_ms (clean only) -----------------------------
    ax1 = axes[0]
    clean_df = df[df["skipped"] == 0]
    ax1.plot(clean_df["t_sec"], clean_df["full_ms"],
             linewidth=0.6, color="steelblue", alpha=0.7, label="full_ms")
    ax1.axhline(mean, color="orange", linewidth=1.2, linestyle="--", label=f"Mean {mean:.1f} ms")
    ax1.axhline(p95,  color="red",    linewidth=1.2, linestyle=":",  label=f"P95  {p95:.1f} ms")
    ax1.set_ylabel("full_ms")
    ax1.set_xlabel("Seconds since start")
    ax1.legend(fontsize=8, loc="upper right")
    ax1.set_title("Pipeline latency over time (skipped rows excluded)")

    # -- Bottom: histogram of full_ms -----------------------------------------
    ax2 = axes[1]
    ax2.hist(clean, bins=80, color="steelblue", edgecolor="white", linewidth=0.3)
    ax2.axvline(mean, color="orange", linewidth=1.5, linestyle="--", label=f"Mean {mean:.1f} ms")
    ax2.axvline(p95,  color="red",    linewidth=1.5, linestyle=":",  label=f"P95  {p95:.1f} ms")
    ax2.set_xlabel("full_ms")
    ax2.set_ylabel("Count")
    ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("Latency distribution")

    plt.tight_layout()

    # Save under graphs/<timestamp>/
    timestamp = filename.replace(".csv", "")
    out_dir   = os.path.join(script_dir, "graphs", timestamp)
    os.makedirs(out_dir, exist_ok=True)
    out_name  = f"receiver_pipeline_{timestamp}_analysis.png"
    out_path  = os.path.join(out_dir, out_name)
    plt.savefig(out_path, dpi=150)
    print(f"Graph saved → {out_path}")
    plt.show()


if __name__ == "__main__":
    main()
