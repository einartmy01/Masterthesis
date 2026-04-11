#!/usr/bin/env python3
import pandas as pd
import sys

# ── Config ────────────────────────────────────────────────────────────────────
SENDER_LOG   = "logs/sender_timestamps.csv"
RECEIVER_LOG = "logs/receiver_timestamps.csv"
OUTPUT_FILE  = "logs/latency_analysis.csv"
# ─────────────────────────────────────────────────────────────────────────────

def load_and_split(filepath):
    df = pd.read_csv(filepath)
    return {stage: g.reset_index(drop=True) for stage, g in df.groupby("stage")}

def align_by_pts(sender, receiver):
    """Match frames by GStreamer PTS value — same buffer = same PTS."""
    merged = pd.merge(
        sender[["wall_time_ns", "gst_buffer_pts_ns"]].rename(columns={"wall_time_ns": "sender_wall_ns"}),
        receiver[["wall_time_ns", "gst_buffer_pts_ns"]].rename(columns={"wall_time_ns": "receiver_wall_ns"}),
        on="gst_buffer_pts_ns",
        how="inner"
    )
    return merged

def main():
    sender_file   = sys.argv[1] if len(sys.argv) > 1 else SENDER_LOG
    receiver_file = sys.argv[2] if len(sys.argv) > 2 else RECEIVER_LOG
    output_file   = sys.argv[3] if len(sys.argv) > 3 else OUTPUT_FILE

    print(f"Loading sender log:   {sender_file}")
    print(f"Loading receiver log: {receiver_file}")

    sender_stages   = load_and_split(sender_file)
    receiver_stages = load_and_split(receiver_file)

    # ── Network latency: sender post_depay → receiver post_depay ──────────────
    net = align_by_pts(sender_stages["post_depay"], receiver_stages["post_depay"])
    net["network_latency_ms"] = (net["receiver_wall_ns"] - net["sender_wall_ns"]) / 1_000_000

    # ── Decode latency: receiver post_depay → receiver post_decode ────────────
    dec = pd.merge(
        receiver_stages["post_depay"][["gst_buffer_pts_ns", "wall_time_ns"]].rename(columns={"wall_time_ns": "pre_decode_ns"}),
        receiver_stages["post_decode"][["gst_buffer_pts_ns", "wall_time_ns"]].rename(columns={"wall_time_ns": "post_decode_ns"}),
        on="gst_buffer_pts_ns",
        how="inner"
    )
    dec["decode_latency_ms"] = (dec["post_decode_ns"] - dec["pre_decode_ns"]) / 1_000_000

    # ── Merge all ──────────────────────────────────────────────────────────────
    result = pd.merge(net[["gst_buffer_pts_ns", "sender_wall_ns", "network_latency_ms"]],
                      dec[["gst_buffer_pts_ns", "decode_latency_ms"]],
                      on="gst_buffer_pts_ns", how="inner")

    result["total_latency_ms"] = result["network_latency_ms"] + result["decode_latency_ms"]
    result["frame_index"] = range(len(result))

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n─── Latency Summary ───────────────────────────────")
    for col in ["network_latency_ms", "decode_latency_ms", "total_latency_ms"]:
        print(f"\n{col}:")
        print(f"  mean:   {result[col].mean():.3f} ms")
        print(f"  min:    {result[col].min():.3f} ms")
        print(f"  max:    {result[col].max():.3f} ms")
        print(f"  stddev: {result[col].std():.3f} ms")
    print(f"\nMatched frames: {len(result)}")
    print("───────────────────────────────────────────────────")

    result.to_csv(output_file, index=False)
    print(f"\nSaved to {output_file}")

if __name__ == "__main__":
    main()
