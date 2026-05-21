#!/usr/bin/env python3
"""
transit_analyseV5.py  –  Transit time stats + graph
Usage: python3 transit_analyseV5.py <filename>

The file is always looked up at:
    ../logs/transit/transit_result_<filename>

Example:
    python3 transit_analyseV5.py 15.05-18:51.csv
"""

import sys
import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker


# ── 1. Build the file path ────────────────────────────────────────────────────

if len(sys.argv) < 2:
    print("Usage: python3 transit_analyseV5.py <filename>")
    print("Example: python3 transit_analyseV5.py 15.05-18:51.csv")
    sys.exit(1)

filename  = sys.argv[1]
script_dir = os.path.dirname(os.path.abspath(__file__))
file_path  = os.path.join(script_dir, "..", "logs", "transit",
                          "transit_result_" + filename)
file_path  = os.path.normpath(file_path)

if not os.path.exists(file_path):
    print(f"ERROR: File not found:\n  {file_path}")
    sys.exit(1)

print(f"Reading: {file_path}")


# ── 2. Load data ──────────────────────────────────────────────────────────────

df = pd.read_csv(file_path)

if "transit_ms" not in df.columns:
    print("ERROR: Expected a column named 'transit_ms' in the CSV.")
    sys.exit(1)

ms = df["transit_ms"].dropna()
print(f"Rows loaded: {len(ms):,}\n")


# ── 3. Stats ──────────────────────────────────────────────────────────────────

stats = {
    "Count"  : len(ms),
    "Mean"   : ms.mean(),
    "Min"    : ms.min(),
    "Max"    : ms.max(),
    "P50"    : ms.quantile(0.50),
    "P95"    : ms.quantile(0.95),
    "P99"    : ms.quantile(0.99),
    "Std Dev": ms.std(),
}

print("─" * 35)
print(f"  {'Stat':<10} {'Value':>12}")
print("─" * 35)
for name, val in stats.items():
    if name == "Count":
        print(f"  {name:<10} {val:>12,}")
    else:
        print(f"  {name:<10} {val:>11.2f} ms")
print("─" * 35)


# ── 4. Graph ──────────────────────────────────────────────────────────────────

fig, axes = plt.subplots(1, 2, figsize=(14, 5))
fig.suptitle(f"Transit Time Analysis  –  {filename}", fontsize=14, fontweight="bold")

# --- Left: time-series line plot ---
ax1 = axes[0]
ax1.plot(ms.values, linewidth=0.5, color="steelblue", alpha=0.8)
ax1.axhline(stats["Mean"], color="orange",  linewidth=1.5, linestyle="--", label=f"Mean  {stats['Mean']:.1f} ms")
ax1.axhline(stats["P95"],  color="crimson", linewidth=1.5, linestyle="--", label=f"P95   {stats['P95']:.1f} ms")
ax1.set_title("Transit Time over Packets")
ax1.set_xlabel("Packet index")
ax1.set_ylabel("Transit (ms)")
ax1.legend(fontsize=9)
ax1.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f ms"))
ax1.grid(True, alpha=0.3)

# --- Right: histogram ---
ax2 = axes[1]
ax2.hist(ms, bins=80, color="steelblue", edgecolor="white", linewidth=0.3)
ax2.axvline(stats["Mean"], color="orange",  linewidth=2, linestyle="--", label=f"Mean  {stats['Mean']:.1f} ms")
ax2.axvline(stats["P95"],  color="crimson", linewidth=2, linestyle="--", label=f"P95   {stats['P95']:.1f} ms")
ax2.set_title("Distribution of Transit Times")
ax2.set_xlabel("Transit (ms)")
ax2.set_ylabel("Count")
ax2.legend(fontsize=9)
ax2.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f ms"))
ax2.grid(True, alpha=0.3, axis="y")

plt.tight_layout()

# Save under graphs/<timestamp>/
timestamp = filename.replace(".csv", "")
out_dir   = os.path.join(script_dir, "graphs", timestamp)
os.makedirs(out_dir, exist_ok=True)
out_name  = "transit_result_" + timestamp + "_analysis.png"
out_path  = os.path.join(out_dir, out_name)
plt.savefig(out_path, dpi=150)
print(f"\nGraph saved: {out_path}")
plt.show()
