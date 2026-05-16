#!/usr/bin/env python3
"""
sender_pipeline_visualise.py
────────────────────────────
Saves one PNG per graph into graphs/sender/<timestamp>/

Usage:
    python sender_pipeline_visualise.py                  # newest log file
    python sender_pipeline_visualise.py <path/to/file>   # specific file
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt


# ── Configuration ─────────────────────────────────────────────────────────────

LOG_DIR            = Path("../logs/pipeline/sender")
OUTPUT_DIR         = Path("graphs/sender")

SKIP_FIRST_SECONDS = 10      # Cut this many seconds from the start
LATENCY_WARN_MS    = 1.0    # Warning threshold line on latency plots
LATENCY_CRIT_MS    = 2.0    # Critical threshold line on latency plots
LATENCY_IQR_FENCE  = 500.0    # Remove latency outliers beyond IQR * this value
ROLLING_WINDOW     = 30     # Smoothing window (number of rows) on time-series
THROUGHPUT_BIN     = "1s"   # Bucket size for rows/sec: "1s", "500ms", etc.

COLOURS     = ["#4472C4", "#ED7D31", "#70AD47", "#FFC000", "#7030A0"]
WARN_COLOUR = "#FFC000"
CRIT_COLOUR = "#C00000"


# ── Loading and filtering ──────────────────────────────────────────────────────

def find_newest_log(log_dir):
    files = list(log_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files in {log_dir}")
    return max(files, key=lambda p: p.stat().st_mtime)


def load_log(path):
    df = pd.read_csv(path)
    try:
        df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")
    except Exception:
        df["wall_time"] = pd.to_datetime(df["wall_time"])
    return df.sort_values("wall_time").reset_index(drop=True)


def skip_early_rows(df):
    cutoff = df["wall_time"].min() + pd.Timedelta(seconds=SKIP_FIRST_SECONDS)
    return df[df["wall_time"] >= cutoff].reset_index(drop=True)


def remove_latency_outliers(series):
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    upper  = q3 + LATENCY_IQR_FENCE * (q3 - q1)
    return series[series <= upper]


def elapsed_seconds(time_series):
    return (time_series - time_series.min()).dt.total_seconds()


# ── Shared plot helpers ────────────────────────────────────────────────────────

def new_figure(title):
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.set_title(title, fontsize=11, pad=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    return fig, ax


def add_threshold_lines(ax):
    ax.axhline(LATENCY_WARN_MS, color=WARN_COLOUR, linestyle="--", linewidth=1.2, label=f"Warn {LATENCY_WARN_MS} ms")
    ax.axhline(LATENCY_CRIT_MS, color=CRIT_COLOUR, linestyle="--", linewidth=1.2, label=f"Crit {LATENCY_CRIT_MS} ms")


def save(fig, output_dir, filename):
    fig.tight_layout()
    fig.savefig(output_dir / filename, dpi=150)
    plt.close(fig)
    print(f"  Saved: {filename}")


# ── Graphs ─────────────────────────────────────────────────────────────────────

def graph_latency_over_time(df, camera_ids, output_dir):
    fig, ax = new_figure("Pipeline Latency Over Time")

    for i, cam in enumerate(camera_ids):
        cam_df  = df[df["cam"] == cam]
        elapsed = elapsed_seconds(cam_df["wall_time"])
        latency = remove_latency_outliers(cam_df["pipeline_ms"])
        colour  = COLOURS[i % len(COLOURS)]

        ax.scatter(elapsed.iloc[:len(latency)], latency, s=2, alpha=0.3, color=colour)
        rolling = latency.rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).mean()
        ax.plot(elapsed.iloc[:len(rolling)], rolling, lw=1.8, color=colour, label=f"Cam {cam}")

    add_threshold_lines(ax)
    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel("pipeline_ms")
    ax.legend()
    save(fig, output_dir, "latency_over_time.png")


def graph_latency_histogram(df, camera_ids, output_dir):
    fig, ax = new_figure("Latency Distribution")

    for i, cam in enumerate(camera_ids):
        latency = remove_latency_outliers(df[df["cam"] == cam]["pipeline_ms"])
        ax.hist(latency, bins=80, alpha=0.55, color=COLOURS[i % len(COLOURS)], label=f"Cam {cam}")

    add_threshold_lines(ax)
    ax.set_xlabel("pipeline_ms")
    ax.set_ylabel("Count")
    ax.legend()
    save(fig, output_dir, "latency_histogram.png")


def graph_latency_percentiles(df, camera_ids, output_dir):
    fig, ax = new_figure("Latency Percentiles per Camera")

    percentiles = [50, 90, 95, 99]
    x           = np.arange(len(percentiles))
    bar_width   = 0.8 / len(camera_ids)

    for i, cam in enumerate(camera_ids):
        latency = remove_latency_outliers(df[df["cam"] == cam]["pipeline_ms"])
        values  = [float(np.percentile(latency, p)) for p in percentiles]
        offset  = (i - len(camera_ids) / 2 + 0.5) * bar_width
        bars    = ax.bar(x + offset, values, bar_width * 0.9, color=COLOURS[i % len(COLOURS)], alpha=0.8, label=f"Cam {cam}")

        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.003,
                    f"{val:.3f}", ha="center", va="bottom", fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels([f"p{p}" for p in percentiles])
    ax.axhline(LATENCY_WARN_MS, color=WARN_COLOUR, linestyle="--", linewidth=1)
    ax.axhline(LATENCY_CRIT_MS, color=CRIT_COLOUR, linestyle="--", linewidth=1)
    ax.set_ylabel("pipeline_ms")
    ax.legend()
    save(fig, output_dir, "latency_percentiles.png")


def graph_throughput(df, camera_ids, output_dir):
    fig, ax = new_figure(f"Throughput per Camera ({THROUGHPUT_BIN} buckets)")

    df_copy = df.copy()
    df_copy["bucket"] = df_copy["wall_time"].dt.floor(THROUGHPUT_BIN)

    for i, cam in enumerate(camera_ids):
        tput    = df_copy[df_copy["cam"] == cam].groupby("bucket").size()
        tput    = tput.iloc[1:-1]  # drop first and last partial buckets
        elapsed = elapsed_seconds(tput.index.to_series())
        ax.plot(elapsed, tput.values, lw=1.5, color=COLOURS[i % len(COLOURS)], label=f"Cam {cam}")

    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel("Rows/s")
    ax.set_ylim(20, 30)
    ax.legend()
    save(fig, output_dir, "throughput.png")


def graph_dropped_nals(df, camera_ids, output_dir):
    fig, ax = new_figure("Dropped NALs Over Time")

    full_elapsed = elapsed_seconds(df["wall_time"])
    x_max = full_elapsed.max()

    for i, cam in enumerate(camera_ids):
        cam_df  = df[df["cam"] == cam]
        drops   = cam_df[cam_df["dropped_nals"] > 0]
        elapsed = (drops["wall_time"] - df["wall_time"].min()).dt.total_seconds()
        ax.bar(elapsed, drops["dropped_nals"], width=0.4, color=COLOURS[i % len(COLOURS)], alpha=0.7, label=f"Cam {cam}")

    ax.set_xlim(0, x_max)
    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel("dropped_nals")
    ax.legend()
    save(fig, output_dir, "dropped_nals.png")


def graph_summary_table(df, camera_ids, output_dir):
    per_cam = {}
    for cam in camera_ids:
        lat = df[df["cam"] == cam]["pipeline_ms"]
        per_cam[cam] = {"mean": lat.mean(), "min": lat.min(), "max": lat.max()}

    bar_labels = [f"Cam {c}" for c in camera_ids] + ["All"]
    all_lat    = df["pipeline_ms"]

    mins  = [per_cam[c]["min"]  for c in camera_ids] + [all_lat.min()]
    maxs  = [per_cam[c]["max"]  for c in camera_ids] + [all_lat.max()]
    means = [per_cam[c]["mean"] for c in camera_ids] + [all_lat.mean()]

    n_bars  = len(bar_labels)
    colours = [COLOURS[i % len(COLOURS)] for i in range(n_bars)]
    x       = np.arange(n_bars)
    bar_w   = 0.6

    fig, axes = plt.subplots(1, 3, figsize=(max(14, n_bars * 1.5 + 6), 5), sharey=False)
    fig.suptitle("Pipeline Latency Summary (ms)", fontsize=12, fontweight="bold")

    for ax, stat_vals, stat_label in zip(axes, [mins, maxs, means], ["Min", "Max", "Mean"]):
        bars = ax.bar(x, stat_vals, bar_w, color=colours, alpha=0.82, edgecolor="white", linewidth=0.5)
        for bar, val in zip(bars, stat_vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() / 2,
                    f"{val:.3f}", ha="center", va="center", fontsize=8, color="white", fontweight="bold")
        ax.set_title(stat_label, fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels(bar_labels, rotation=20, ha="right", fontsize=9)
        ax.set_ylabel("ms")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    handles = [plt.Rectangle((0, 0), 1, 1, color=colours[i]) for i in range(n_bars)]
    fig.legend(handles, bar_labels, title="Camera", fontsize=8, title_fontsize=8,
               loc="lower center", ncol=n_bars, bbox_to_anchor=(0.5, -0.02))

    fig.tight_layout(rect=[0, 0.08, 1, 1])
    fig.savefig(output_dir / "summary_table.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("  Saved: summary_table.png")


def graph_rtp_sequence(df, camera_ids, output_dir):
    fig, ax = new_figure("RTP Sequence Number Over Time")

    for i, cam in enumerate(camera_ids):
        cam_df  = df[df["cam"] == cam].sort_values("wall_time")
        elapsed = elapsed_seconds(cam_df["wall_time"])
        ax.plot(elapsed, cam_df["rtp_seq"].values, lw=1.2, color=COLOURS[i % len(COLOURS)], label=f"Cam {cam}")

    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel("rtp_seq")
    ax.legend()
    save(fig, output_dir, "rtp_sequence.png")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        file_path = Path(sys.argv[1])
    else:
        file_path = find_newest_log(LOG_DIR)

    print(f"Loading: {file_path}")

    df         = load_log(file_path)
    df         = skip_early_rows(df)
    camera_ids = sorted(df["cam"].unique().tolist())

    timestamp  = file_path.stem
    output_dir = OUTPUT_DIR / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Saving graphs to: {output_dir}\n")

    graph_latency_over_time(df, camera_ids, output_dir)
    graph_latency_histogram(df, camera_ids, output_dir)
    graph_latency_percentiles(df, camera_ids, output_dir)
    graph_throughput(df, camera_ids, output_dir)
    graph_dropped_nals(df, camera_ids, output_dir)
    graph_rtp_sequence(df, camera_ids, output_dir)
    graph_summary_table(df, camera_ids, output_dir)

if __name__ == "__main__":
    main()
