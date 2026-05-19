#!/usr/bin/env python3
"""
receiver_pipeline_visualise.py
Saves PNGs into graphs/receiver/<suffix>/

Usage:
    python receiver_pipeline_visualise.py                  # newest set
    python receiver_pipeline_visualise.py 07.05-15:45      # specific suffix
    python receiver_pipeline_visualise.py path/to/file.csv # any file of the set
"""

import sys
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


LOG_DIR    = Path("../logs/pipeline/receiver")
OUTPUT_DIR = Path("graphs/receiver")

SKIP_FIRST_SECONDS = 20
ROLLING_WINDOW     = 30
LATENCY_MIN_MS     = 0.1    # Remove values below this
LATENCY_MAX_MS     = 100.0   # Remove values above this

COLOURS     = ["#4472C4", "#ED7D31", "#70AD47", "#FFC000", "#7030A0"]
WARN_COLOUR = "#FFC000"
CRIT_COLOUR = "#C00000"


# ── Helpers ────────────────────────────────────────────────────────────────────

def load(path):
    df = pd.read_csv(path)
    try:
        df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")
    except Exception:
        df["wall_time"] = pd.to_datetime(df["wall_time"])
    df = df.sort_values("wall_time").reset_index(drop=True)
    cutoff = df["wall_time"].min() + pd.Timedelta(seconds=SKIP_FIRST_SECONDS)
    return df[df["wall_time"] >= cutoff].reset_index(drop=True)


def clean(series):
    return series[(series >= LATENCY_MIN_MS) & (series <= LATENCY_MAX_MS)]


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path.name}")


def new_fig(title):
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.set_title(title, fontsize=11)
    ax.grid(True, linestyle="--", alpha=0.4)
    return fig, ax


def add_thresholds(ax, warn, crit):
    ax.axhline(warn, color=WARN_COLOUR, linestyle="--", lw=1.2, label=f"Warn {warn} ms")
    ax.axhline(crit, color=CRIT_COLOUR, linestyle="--", lw=1.2, label=f"Crit {crit} ms")


def cams(df):
    return sorted(df["cam_index"].unique())


# ── Graph functions ────────────────────────────────────────────────────────────

def latency_over_time(df, col, warn, crit, title, out):
    fig, ax = new_fig(title)
    for i, cam in enumerate(cams(df)):
        sub = df[df["cam_index"] == cam]
        t   = (sub["wall_time"] - sub["wall_time"].min()).dt.total_seconds()
        lat = clean(sub[col])
        c   = COLOURS[i % len(COLOURS)]
        ax.scatter(t.iloc[:len(lat)], lat, s=2, alpha=0.3, color=c)
        ax.plot(t.iloc[:len(lat)], lat.rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).mean(),
                lw=1.8, color=c, label=f"Cam {cam}")
    add_thresholds(ax, warn, crit)
    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel(col)
    ax.legend()
    save(fig, out)


def latency_histogram(df, col, warn, crit, title, out):
    fig, ax = new_fig(title)
    for i, cam in enumerate(cams(df)):
        ax.hist(clean(df[df["cam_index"] == cam][col]), bins=80,
                alpha=0.55, color=COLOURS[i % len(COLOURS)], label=f"Cam {cam}")
    ax.axvline(warn, color=WARN_COLOUR, linestyle="--", lw=1.2, label=f"Warn {warn} ms")
    ax.axvline(crit, color=CRIT_COLOUR, linestyle="--", lw=1.2, label=f"Crit {crit} ms")
    ax.set_xlabel(col)
    ax.set_ylabel("Count")
    ax.legend()
    save(fig, out)


def latency_percentiles(df, col, warn, crit, title, out):
    fig, ax = new_fig(title)
    pcts     = [50, 90, 95, 99]
    cam_list = cams(df)
    bw       = 0.8 / len(cam_list)
    for i, cam in enumerate(cam_list):
        lat    = clean(df[df["cam_index"] == cam][col])
        vals   = [float(lat.quantile(p / 100)) for p in pcts]
        offset = (i - len(cam_list) / 2 + 0.5) * bw
        bars   = ax.bar([x + offset for x in range(len(pcts))], vals,
                        bw * 0.9, color=COLOURS[i % len(COLOURS)], alpha=0.8, label=f"Cam {cam}")
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.003,
                    f"{v:.3f}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(pcts)))
    ax.set_xticklabels([f"p{p}" for p in pcts])
    ax.axhline(warn, color=WARN_COLOUR, linestyle="--", lw=1)
    ax.axhline(crit, color=CRIT_COLOUR, linestyle="--", lw=1)
    ax.set_ylabel(col)
    ax.legend()
    save(fig, out)


def summary_table(df, col, title, out):
    cam_list = cams(df)
    labels   = [f"Cam {c}" for c in cam_list] + ["All"]
    colours  = [COLOURS[i % len(COLOURS)] for i in range(len(labels))]
    x        = list(range(len(labels)))
    all_lat  = df[col]

    fig, axes = plt.subplots(1, 3, figsize=(max(14, len(labels) * 1.5 + 6), 5))
    fig.suptitle(title, fontsize=12, fontweight="bold")

    stat_data = [
        ([df[df["cam_index"] == c][col].min()  for c in cam_list] + [all_lat.min()],  "Min"),
        ([df[df["cam_index"] == c][col].max()  for c in cam_list] + [all_lat.max()],  "Max"),
        ([df[df["cam_index"] == c][col].mean() for c in cam_list] + [all_lat.mean()], "Mean"),
    ]
    for ax, (vals, stat) in zip(axes, stat_data):
        bars = ax.bar(x, vals, 0.6, color=colours, alpha=0.82, edgecolor="white", linewidth=0.5)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() / 2,
                    f"{v:.3f}", ha="center", va="center", fontsize=8, color="white", fontweight="bold")
        ax.set_title(stat, fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
        ax.set_ylabel("ms")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in colours]
    fig.legend(handles, labels, title="Camera", fontsize=8, title_fontsize=8,
               loc="lower center", ncol=len(labels), bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out.name}")

# ── Find Files ────────────────────────────────────────────────────────────

def find_latest_suffix(log_dir):
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg is None:
        seen = {}
        for f in LOG_DIR.glob("rec_*.csv"):
            parts = f.stem.split("_", 2)
            if len(parts) == 3:
                seen.setdefault(parts[2], set()).add(parts[1])
        complete = [s for s, kinds in seen.items() if {"depay", "full", "decoder"} <= kinds]
        if not complete:
            raise FileNotFoundError(f"No complete triplet in {LOG_DIR}")
        suffix  = max(complete, key=lambda s: max(
            (LOG_DIR / f"rec_{k}_{s}.csv").stat().st_mtime for k in ("depay", "full", "decoder")
        ))
        log_dir = LOG_DIR
    elif Path(arg).exists():
        suffix, log_dir = Path(arg).stem.split("_", 2)[2], Path(arg).parent
    else:
        suffix, log_dir = arg, LOG_DIR
    return suffix, log_dir

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    
    suffix, log_dir = find_latest_suffix(LOG_DIR)
    print(f"Loading: {suffix}")
    depay   = load(log_dir / f"rec_depay_{suffix}.csv")
    full    = load(log_dir / f"rec_full_{suffix}.csv")
    decoder = load(log_dir / f"rec_decoder_{suffix}.csv")

    out = OUTPUT_DIR / suffix
    out.mkdir(parents=True, exist_ok=True)
    print(f"Saving to: {out}\n")

    for df, col, warn, crit, prefix in [
        (depay,   "depay_ms",    1.0,  2.0, "depay"),
        (decoder, "decoder_ms",  5.0, 10.0, "decoder"),
        (full,    "full_ms",     7.5, 15.0, "full"),
    ]:
        name = prefix.capitalize()
        latency_over_time  (df, col, warn, crit, f"{name} Latency Over Time",    out / f"{prefix}_latency_over_time.png")
        latency_histogram  (df, col, warn, crit, f"{name} Latency Distribution", out / f"{prefix}_latency_histogram.png")
        latency_percentiles(df, col, warn, crit, f"{name} Latency Percentiles",  out / f"{prefix}_latency_percentiles.png")
        summary_table      (df, col,             f"{name} Latency Summary (ms)", out / f"{prefix}_summary_table.png")

    print(f"\nDone. Cameras: {cams(depay)}")


if __name__ == "__main__":
    main()
