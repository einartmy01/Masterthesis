#!/usr/bin/env python3
"""
Usage:
    python3 quality_analyse.py 15.05-19:56.csv
"""

import sys
import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ── Config ────────────────────────────────────────────────────────────────────
SKIP_SECONDS = 5
# ─────────────────────────────────────────────────────────────────────────────

if len(sys.argv) < 2:
    print("Usage: python3 quality_analyse.py <timestamp>.csv")
    sys.exit(1)

path = f"../logs/quality/rec_quality_{sys.argv[1]}"

df = pd.read_csv(path)
df["wall_time"] = pd.to_datetime(df["wall_time"], format="%H:%M:%S.%f")

t0     = df["wall_time"].min()
cutoff = t0 + pd.Timedelta(seconds=SKIP_SECONDS)
before = len(df)
df     = df[df["wall_time"] >= cutoff].reset_index(drop=True)
skip_line = f"Skipped first {SKIP_SECONDS}s ({before - len(df)} rows dropped, {len(df)} remain)."
print(skip_line)

# ── Stats ─────────────────────────────────────────────────────────────────────
stat_lines = [
    f"{'─'*52}",
    f"{'Camera':<10} {'Mean':>8} {'Min':>8} {'Max':>8} {'P95':>8}  {'Samples':>8}",
    f"{'─'*52}",
]
for cam, g in df.groupby("cam_index"):
    s = g["brisque_score"]
    stat_lines.append(f"  Cam {cam:<5}  {s.mean():>8.2f} {s.min():>8.2f} {s.max():>8.2f} "
                      f"{s.quantile(0.95):>8.2f}  {len(s):>8}")
s = df["brisque_score"]
stat_lines += [
    f"{'─'*52}",
    f"  {'ALL':<7}  {s.mean():>8.2f} {s.min():>8.2f} {s.max():>8.2f} "
    f"{s.quantile(0.95):>8.2f}  {len(s):>8}",
    f"{'─'*52}",
    "  BRISQUE: 0–100, lower = better quality.",
]
print("\n" + "\n".join(stat_lines) + "\n")

# ── Plot ──────────────────────────────────────────────────────────────────────
cameras = sorted(df["cam_index"].unique())
colors  = ["#2196F3", "#4CAF50", "#FF5722"]

fig, axes = plt.subplots(len(cameras), 1, figsize=(12, 3.5 * len(cameras)), sharey=True)
if len(cameras) == 1:
    axes = [axes]

for ax, cam, color in zip(axes, cameras, colors):
    g = df[df["cam_index"] == cam].sort_values("wall_time").reset_index(drop=True)
    x, y = range(len(g)), g["brisque_score"]
    mean, p95 = y.mean(), y.quantile(0.95)

    ax.plot(x, y, color=color, linewidth=0.8, alpha=0.7, label="BRISQUE score")
    ax.axhline(mean, color=color, linewidth=1.5, linestyle="--", label=f"Mean {mean:.1f}")
    ax.axhline(p95,  color="red", linewidth=1.2, linestyle=":",  label=f"P95  {p95:.1f}")
    ax.fill_between(x, y, mean, where=(y > mean), alpha=0.15, color=color)

    ax.set_title(f"Camera {cam}", fontsize=12, fontweight="bold")
    ax.set_ylabel("BRISQUE score\n(lower = better)")
    ax.set_ylim(0, 100)
    ax.yaxis.set_minor_locator(ticker.MultipleLocator(5))
    ax.grid(True, which="major", linestyle="--", alpha=0.4)
    ax.grid(True, which="minor", linestyle=":",  alpha=0.2)
    ax.legend(loc="upper right", fontsize=9)

axes[-1].set_xlabel("Sample index")
fig.suptitle("BRISQUE Video Quality per Camera", fontsize=14, fontweight="bold", y=1.01)
fig.tight_layout()

timestamp = sys.argv[1].replace(".csv", "")
script_dir = os.path.dirname(os.path.abspath(__file__))
out_dir    = os.path.join(script_dir, "graphs", timestamp)
os.makedirs(out_dir, exist_ok=True)
out        = os.path.join(out_dir, f"quality_plot_{timestamp}.png")
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"Plot saved → {out}")

txt_path = os.path.join(out_dir, f"quality_stats_{timestamp}.txt")
with open(txt_path, "w") as f:
    f.write(f"BRISQUE Quality Stats  –  {sys.argv[1]}\n\n")
    f.write(skip_line + "\n\n")
    f.write("\n".join(stat_lines) + "\n")
print(f"Stats saved → {txt_path}")
