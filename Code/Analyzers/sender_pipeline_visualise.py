#!/usr/bin/env python3
"""
sender_pipeline_visualise.py
────────────────────────────
Visualises sender pipeline log files from logs/pipeline/sender/.

Usage:
    python sender_pipeline_visualise.py                  # newest file
    python sender_pipeline_visualise.py <path/to/file>   # specific file

Output:
    - Opens an interactive matplotlib dashboard
    - Saves a PNG next to the log file (same name, .png extension)
"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import MaxNLocator


# ── Configuration ─────────────────────────────────────────────────────────────

LOG_DIR = Path("../logs/pipeline/sender")

LATENCY_WARN_MS   = 1.0     # Rows above this are flagged as spikes
LATENCY_CRIT_MS   = 2.0     # Rows above this are flagged as critical spikes
RTP_GAP_THRESHOLD = 15      # RTP seq jumps larger than this are flagged as gaps

SKIP_FIRST_SECONDS  = 10     # Ignore rows from the start of the recording
LATENCY_OUTLIER_IQR = 6.0   # Drop latency values above median + N*IQR
DROP_OUTLIER_IQR    = 6.0   # Drop dropped_nals values above median + N*IQR

FIGURE_SIZE      = (20, 14)
THROUGHPUT_BIN   = "1s"     # Pandas offset alias for throughput bucketing
ROLLING_WINDOW   = 30       # Rows for rolling-mean overlay on latency plot

COLOUR_PALETTE   = ["#4e8cff", "#ff6b6b", "#51cf66", "#ffd43b", "#cc5de8"]
COLOUR_WARN      = "#ffd43b"
COLOUR_CRIT      = "#ff6b6b"
COLOUR_OK        = "#51cf66"
COLOUR_GRID      = "#2a2a3a"
BACKGROUND       = "#12121c"
PANEL_BACKGROUND = "#1a1a2e"
TEXT_COLOUR      = "#e0e0f0"


# ── File loading ───────────────────────────────────────────────────────────────

def find_newest_log(log_dir: Path) -> Path:
    files = list(log_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {log_dir}")
    return max(files, key=lambda p: p.stat().st_mtime)


def load_and_parse_log(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)

    required_columns = {"wall_time", "cam", "rtp_seq", "pipeline_ms", "dropped_nals"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    try:
        df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")
    except Exception:
        df["wall_time"] = pd.to_datetime(df["wall_time"])

    df = df.sort_values("wall_time").reset_index(drop=True)
    return df


# ── Filtering / cleaning ───────────────────────────────────────────────────────

def drop_early_timestamps(df: pd.DataFrame, seconds: float) -> pd.DataFrame:
    cutoff = df["wall_time"].min() + pd.Timedelta(seconds=seconds)
    return df[df["wall_time"] >= cutoff].reset_index(drop=True)


def remove_latency_outliers(series: pd.Series, iqr_multiplier: float) -> pd.Series:
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    upper_fence = q3 + iqr_multiplier * (q3 - q1)
    return series[series <= upper_fence]


def remove_drop_outliers(df: pd.DataFrame, iqr_multiplier: float) -> pd.DataFrame:
    col = df["dropped_nals"]
    q1, q3 = col.quantile(0.25), col.quantile(0.75)
    upper_fence = q3 + iqr_multiplier * (q3 - q1)
    return df[col <= upper_fence].reset_index(drop=True)


# ── Theme helpers ──────────────────────────────────────────────────────────────

def apply_dark_theme():
    plt.rcParams.update({
        "figure.facecolor":  BACKGROUND,
        "axes.facecolor":    PANEL_BACKGROUND,
        "axes.edgecolor":    COLOUR_GRID,
        "axes.labelcolor":   TEXT_COLOUR,
        "xtick.color":       TEXT_COLOUR,
        "ytick.color":       TEXT_COLOUR,
        "text.color":        TEXT_COLOUR,
        "grid.color":        COLOUR_GRID,
        "grid.linewidth":    0.6,
        "legend.facecolor":  PANEL_BACKGROUND,
        "legend.edgecolor":  COLOUR_GRID,
        "font.family":       "monospace",
    })


def style_axis(ax, title: str, xlabel: str = "", ylabel: str = ""):
    ax.set_title(title, fontsize=10, color=TEXT_COLOUR, pad=8, loc="left", fontweight="bold")
    ax.set_xlabel(xlabel, fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.grid(True, axis="both", linestyle="--", alpha=0.4)
    ax.tick_params(labelsize=7)


# ── Individual plots ───────────────────────────────────────────────────────────

def plot_latency_over_time(ax, df: pd.DataFrame, camera_ids: list):
    for idx, cam_id in enumerate(camera_ids):
        cam_df   = df[df["cam"] == cam_id].copy()
        elapsed  = (cam_df["wall_time"] - cam_df["wall_time"].min()).dt.total_seconds()
        latency  = remove_latency_outliers(cam_df["pipeline_ms"], LATENCY_OUTLIER_IQR)
        colour   = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]

        ax.scatter(elapsed[:len(latency)], latency, s=1.5, alpha=0.35, color=colour)

        rolling = latency.rolling(ROLLING_WINDOW, min_periods=1).mean()
        ax.plot(elapsed[:len(rolling)], rolling, lw=1.5, color=colour, label=f"Cam {cam_id}")

    ax.axhline(LATENCY_WARN_MS, color=COLOUR_WARN, lw=1, linestyle="--", alpha=0.8, label=f"Warn {LATENCY_WARN_MS} ms")
    ax.axhline(LATENCY_CRIT_MS, color=COLOUR_CRIT, lw=1, linestyle="--", alpha=0.8, label=f"Crit {LATENCY_CRIT_MS} ms")
    ax.legend(fontsize=7, markerscale=4)
    style_axis(ax, "Pipeline Latency Over Time", xlabel="Elapsed (s)", ylabel="pipeline_ms")


def plot_latency_histogram(ax, df: pd.DataFrame, camera_ids: list):
    for idx, cam_id in enumerate(camera_ids):
        cam_ms  = df[df["cam"] == cam_id]["pipeline_ms"]
        cleaned = remove_latency_outliers(cam_ms, LATENCY_OUTLIER_IQR)
        colour  = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]
        ax.hist(cleaned, bins=80, alpha=0.55, color=colour, label=f"Cam {cam_id}", edgecolor="none")

    ax.axvline(LATENCY_WARN_MS, color=COLOUR_WARN, lw=1.2, linestyle="--")
    ax.axvline(LATENCY_CRIT_MS, color=COLOUR_CRIT, lw=1.2, linestyle="--")
    ax.legend(fontsize=7)
    style_axis(ax, "Latency Distribution", xlabel="pipeline_ms", ylabel="Count")


def plot_latency_boxplot(ax, df: pd.DataFrame, camera_ids: list):
    data    = [remove_latency_outliers(df[df["cam"] == c]["pipeline_ms"], LATENCY_OUTLIER_IQR)
               for c in camera_ids]
    labels  = [f"Cam {c}" for c in camera_ids]
    colours = [COLOUR_PALETTE[i % len(COLOUR_PALETTE)] for i in range(len(camera_ids))]

    bp = ax.boxplot(data, labels=labels, patch_artist=True, widths=0.5,
                    medianprops=dict(color="white", lw=2),
                    whiskerprops=dict(color=TEXT_COLOUR),
                    capprops=dict(color=TEXT_COLOUR),
                    flierprops=dict(marker=".", color=TEXT_COLOUR, markersize=2, alpha=0.3))

    for patch, colour in zip(bp["boxes"], colours):
        patch.set_facecolor(colour)
        patch.set_alpha(0.6)

    ax.axhline(LATENCY_WARN_MS, color=COLOUR_WARN, lw=1, linestyle="--", alpha=0.8)
    ax.axhline(LATENCY_CRIT_MS, color=COLOUR_CRIT, lw=1, linestyle="--", alpha=0.8)
    style_axis(ax, "Latency per Camera (box = IQR)", ylabel="pipeline_ms")


def plot_dropped_nals_over_time(ax, df: pd.DataFrame, camera_ids: list):
    cleaned_df = remove_drop_outliers(df, DROP_OUTLIER_IQR)

    for idx, cam_id in enumerate(camera_ids):
        cam_df  = cleaned_df[cleaned_df["cam"] == cam_id]
        elapsed = (cam_df["wall_time"] - cam_df["wall_time"].min()).dt.total_seconds()
        colour  = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]
        ax.bar(elapsed, cam_df["dropped_nals"], width=0.4, alpha=0.7, color=colour, label=f"Cam {cam_id}")

    ax.legend(fontsize=7)
    style_axis(ax, "Dropped NALs Over Time", xlabel="Elapsed (s)", ylabel="dropped_nals")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))


def plot_throughput_over_time(ax, df: pd.DataFrame, camera_ids: list):
    df_copy = df.copy()
    df_copy["second"] = df_copy["wall_time"].dt.floor(THROUGHPUT_BIN)

    for idx, cam_id in enumerate(camera_ids):
        cam_tput = df_copy[df_copy["cam"] == cam_id].groupby("second").size()
        elapsed  = (cam_tput.index - cam_tput.index.min()).total_seconds()
        colour   = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]
        ax.plot(elapsed, cam_tput.values, lw=1.2, color=colour, label=f"Cam {cam_id}", alpha=0.85)

    mean_tput = df_copy.groupby("second").size().mean()
    ax.axhline(mean_tput, color=TEXT_COLOUR, lw=0.8, linestyle=":", alpha=0.5, label="Overall mean")
    ax.legend(fontsize=7)
    style_axis(ax, f"Throughput ({THROUGHPUT_BIN} buckets)", xlabel="Elapsed (s)", ylabel="Rows/s")


def plot_rtp_sequence(ax, df: pd.DataFrame, camera_ids: list):
    for idx, cam_id in enumerate(camera_ids):
        cam_df  = df[df["cam"] == cam_id].sort_values("wall_time")
        elapsed = (cam_df["wall_time"] - cam_df["wall_time"].min()).dt.total_seconds()
        colour  = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]
        ax.plot(elapsed, cam_df["rtp_seq"].values, lw=1, color=colour, label=f"Cam {cam_id}", alpha=0.85)

    ax.legend(fontsize=7)
    style_axis(ax, "RTP Sequence Number Over Time", xlabel="Elapsed (s)", ylabel="rtp_seq")


def plot_percentile_bars(ax, df: pd.DataFrame, camera_ids: list):
    percentiles = [50, 90, 95, 99]
    x           = np.arange(len(percentiles))
    bar_width   = 0.8 / len(camera_ids)

    for idx, cam_id in enumerate(camera_ids):
        cam_ms  = remove_latency_outliers(df[df["cam"] == cam_id]["pipeline_ms"], LATENCY_OUTLIER_IQR)
        values  = [float(np.percentile(cam_ms, p)) for p in percentiles]
        colour  = COLOUR_PALETTE[idx % len(COLOUR_PALETTE)]
        offset  = (idx - len(camera_ids) / 2 + 0.5) * bar_width
        bars    = ax.bar(x + offset, values, bar_width * 0.9, color=colour, alpha=0.8, label=f"Cam {cam_id}")

        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.005,
                    f"{val:.3f}", ha="center", va="bottom", fontsize=6, color=TEXT_COLOUR)

    ax.set_xticks(x)
    ax.set_xticklabels([f"p{p}" for p in percentiles])
    ax.axhline(LATENCY_WARN_MS, color=COLOUR_WARN, lw=1, linestyle="--", alpha=0.7)
    ax.axhline(LATENCY_CRIT_MS, color=COLOUR_CRIT, lw=1, linestyle="--", alpha=0.7)
    ax.legend(fontsize=7)
    style_axis(ax, "Latency Percentiles per Camera", ylabel="pipeline_ms")


# ── Dashboard assembly ─────────────────────────────────────────────────────────

def build_dashboard(df: pd.DataFrame, file_path: Path):
    apply_dark_theme()

    camera_ids = sorted(df["cam"].unique().tolist())
    duration_s = (df["wall_time"].max() - df["wall_time"].min()).total_seconds()

    fig = plt.figure(figsize=FIGURE_SIZE)
    fig.patch.set_facecolor(BACKGROUND)

    header_text = (
        f"  {file_path.name}   |   "
        f"Cameras: {camera_ids}   |   "
        f"Duration: {duration_s:.1f}s   |   "
        f"Rows: {len(df):,}   |   "
        f"Warn >{LATENCY_WARN_MS}ms  Crit >{LATENCY_CRIT_MS}ms   |   "
        f"Skip first {SKIP_FIRST_SECONDS}s  ·  Outlier fence IQR×{LATENCY_OUTLIER_IQR}"
    )
    fig.suptitle(header_text, fontsize=8, color=TEXT_COLOUR, x=0.01, ha="left",
                 fontfamily="monospace", y=0.99)

    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.55, wspace=0.35,
                           left=0.06, right=0.97, top=0.95, bottom=0.05)

    plot_latency_over_time(   fig.add_subplot(gs[0, :2]), df, camera_ids)
    plot_latency_histogram(   fig.add_subplot(gs[0, 2]),  df, camera_ids)
    plot_throughput_over_time(fig.add_subplot(gs[1, :2]), df, camera_ids)
    plot_latency_boxplot(     fig.add_subplot(gs[1, 2]),  df, camera_ids)
    plot_rtp_sequence(        fig.add_subplot(gs[2, 0]),  df, camera_ids)
    plot_dropped_nals_over_time(fig.add_subplot(gs[2, 1]), df, camera_ids)
    plot_percentile_bars(     fig.add_subplot(gs[2, 2]),  df, camera_ids)

    return fig


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        file_path = Path(sys.argv[1])
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
    else:
        file_path = find_newest_log(LOG_DIR)

    df = load_and_parse_log(file_path)
    df = drop_early_timestamps(df, SKIP_FIRST_SECONDS)

    fig = build_dashboard(df, file_path)
    graph_path = "/graphs/sender/"
    output_path = graph_path.with_suffix(".png")
    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=BACKGROUND)

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
