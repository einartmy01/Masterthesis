#!/usr/bin/env python3
"""
latency.py
----------
Computes latency from receiver_timestamps.csv which already contains
matched sender/receiver GPS times per frame (logged by receiver.py).

Usage:
    python3 latency.py logs/receiver_timestamps.csv

Output:
    - Summary statistics printed to terminal
    - Per-frame latency saved to logs/latency_results.csv
"""

import csv
import sys
import os
import statistics

RECEIVER_LOG = "logs/receiver_timestamps.csv"
OUTPUT_LOG   = "logs/latency_results.csv"
STAGE        = "post_depay"
# Change to "post_decode" to include decode time


def main():
    receiver_file = sys.argv[1] if len(sys.argv) > 1 else RECEIVER_LOG

    if not os.path.exists(receiver_file):
        print(f"Receiver log not found: {receiver_file}"); sys.exit(1)

    print(f"Loading: {receiver_file}  (stage: {STAGE})")

    latencies = []
    rows = []

    with open(receiver_file, newline='') as f:
        for row in csv.DictReader(f):
            if row['stage'] != STAGE:
                continue
            latency_ms = float(row['latency_ms'])
            if latency_ms < 0:
                continue  # skip midnight rollover edge case
            latencies.append(latency_ms)
            rows.append(row)

    if not latencies:
        print("No valid rows found."); sys.exit(1)

    print(f"Frames: {len(latencies)}")

    mean   = statistics.mean(latencies)
    median = statistics.median(latencies)
    stdev  = statistics.stdev(latencies) if len(latencies) > 1 else 0
    min_l  = min(latencies)
    max_l  = max(latencies)
    p95    = sorted(latencies)[int(len(latencies) * 0.95)]
    p99    = sorted(latencies)[int(len(latencies) * 0.99)]

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Latency Results  ({len(latencies)} frames)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Mean:    {mean:8.2f} ms
 Median:  {median:8.2f} ms
 Std dev: {stdev:8.2f} ms
 Min:     {min_l:8.2f} ms
 Max:     {max_l:8.2f} ms
 P95:     {p95:8.2f} ms
 P99:     {p99:8.2f} ms
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

    os.makedirs("logs", exist_ok=True)
    with open(OUTPUT_LOG, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["sender_pts", "sender_gps_ns", "receiver_gps_ns", "latency_ms"])
        for row in rows:
            writer.writerow([row['sender_pts'], row['sender_gps_ns'],
                             row['receiver_gps_ns'], row['latency_ms']])

    print(f"Per-frame results saved to {OUTPUT_LOG}")


if __name__ == "__main__":
    main()
