#!/usr/bin/env python3
"""
analyze_transit.py

Computes network transit latency by joining sender and receiver transit CSVs
on (cam_index, rtp_seq). Run this offline after collecting logs from both machines.

Usage:
    python3 analyze_transit.py sender_transit_2026-04-23_14-35.csv \
                                receiver_transit_2026-04-23_14-35.csv

Output:
    - Summary statistics printed to terminal (per camera)
    - Combined CSV saved to logs/transit_analysis_<timestamp>.csv
"""

import sys
import csv
import os
from datetime import datetime
from collections import defaultdict

# ── RTP sequence number rollover handling ─────────────────────────────────────
# RTP seq is a 16-bit counter (0–65535). After 65535 it wraps back to 0.
# We normalise by detecting large backward jumps and offsetting accordingly.

MAX_SEQ      = 65536
ROLLOVER_GAP = 32768  # if seq drops by more than this, assume rollover

def normalize_seq(seq, prev_seq, rollover_count):
    if prev_seq is not None:
        delta = seq - prev_seq
        if delta < -ROLLOVER_GAP:
            rollover_count += 1
        elif delta > ROLLOVER_GAP:
            rollover_count -= 1  # rare backwards rollover edge case
    return seq + rollover_count * MAX_SEQ, rollover_count

# ── Load a transit CSV ────────────────────────────────────────────────────────

def load_transit_csv(path):
    """
    Returns dict: (cam_index, normalized_rtp_seq) -> abs_time (float)
    Handles RTP sequence number rollover per camera stream.
    """
    records = {}
    rollover_counts  = defaultdict(int)
    prev_seqs        = defaultdict(lambda: None)

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cam   = int(row["cam_index"])
            seq   = int(row["rtp_seq"])
            t     = float(row["abs_time"])

            norm_seq, rollover_counts[cam] = normalize_seq(
                seq, prev_seqs[cam], rollover_counts[cam]
            )
            prev_seqs[cam] = seq
            records[(cam, norm_seq)] = t

    return records

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 analyze_transit.py <sender_transit.csv> <receiver_transit.csv>")
        sys.exit(1)

    sender_path   = sys.argv[1]
    receiver_path = sys.argv[2]

    print(f"Loading sender  : {sender_path}")
    print(f"Loading receiver: {receiver_path}")

    sender_records   = load_transit_csv(sender_path)
    receiver_records = load_transit_csv(receiver_path)

    # Join on matching (cam_index, rtp_seq)
    results = []  # list of (cam_idx, rtp_seq, transit_ms)

    for key, t_send in sender_records.items():
        if key in receiver_records:
            t_recv       = receiver_records[key]
            transit_ms   = (t_recv - t_send) * 1000
            cam_idx, seq = key
            results.append((cam_idx, seq, transit_ms))

    if not results:
        print("\nNo matching RTP sequence numbers found between the two files.")
        print("Check that both files are from the same recording session.")
        sys.exit(1)

    # ── Per-camera statistics ─────────────────────────────────────────────────
    from statistics import mean, median, stdev

    cam_results = defaultdict(list)
    for cam_idx, seq, transit_ms in results:
        cam_results[cam_idx].append(transit_ms)

    print(f"\n{'='*55}")
    print(f"  Network transit latency — {len(results)} matched packets")
    print(f"{'='*55}")

    for cam_idx in sorted(cam_results.keys()):
        values = cam_results[cam_idx]
        print(f"\n  CAM {cam_idx}  ({len(values)} packets matched)")
        print(f"    Min    : {min(values):.3f} ms")
        print(f"    Max    : {max(values):.3f} ms")
        print(f"    Mean   : {mean(values):.3f} ms")
        print(f"    Median : {median(values):.3f} ms")
        if len(values) > 1:
            print(f"    Stdev  : {stdev(values):.3f} ms")

    total_matched = len(sender_records)
    total_joined  = len(results)
    print(f"\n  Packets in sender log  : {len(sender_records)}")
    print(f"  Packets in receiver log: {len(receiver_records)}")
    print(f"  Matched pairs          : {total_joined}")
    if total_matched > 0:
        loss_pct = (1 - total_joined / len(sender_records)) * 100
        print(f"  Unmatched (dropped?)   : {len(sender_records) - total_joined} ({loss_pct:.1f}%)")
    print(f"{'='*55}\n")

    # ── Save combined CSV ─────────────────────────────────────────────────────
    os.makedirs("logs", exist_ok=True)
    timestamp   = datetime.now().strftime("%Y-%m-%d_%H-%M")
    output_path = f"logs/transit_analysis_{timestamp}.csv"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["cam_index", "rtp_seq", "transit_ms"])
        for cam_idx, seq, transit_ms in sorted(results):
            writer.writerow([cam_idx, seq, f"{transit_ms:.4f}"])

    print(f"Combined analysis saved to: {output_path}")

if __name__ == "__main__":
    main()
