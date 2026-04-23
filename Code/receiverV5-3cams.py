#!/usr/bin/env python3
import os
import time
import csv
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_log():
    os.makedirs("logs", exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = f"logs/receiver_latency_{timestamp}.csv"
    f = open(path, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(["wall_time", "cam_index", "latency_ms"])
    print(f"Logging to: {path}")
    return f, writer

# ── Latency probes ────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx):
    """Records wall-clock time when a UDP packet buffer arrives off the wire."""
    def probe_cb(pad, info):
        entry_times[cam_idx] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_exit_probe(cam_idx, csv_writer, csv_file):
    """Computes and logs latency when a decoded frame reaches the video sink."""
    def probe_cb(pad, info):
        t_entry = entry_times.get(cam_idx)
        if t_entry is not None:
            latency_ms = (time.monotonic() - t_entry) * 1000
            wall_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[CAM {cam_idx}] receiver pipeline latency: {latency_ms:.2f} ms")
            csv_writer.writerow([wall_time, cam_idx, f"{latency_ms:.4f}"])
            csv_file.flush()
        return Gst.PadProbeReturn.OK
    return probe_cb

# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'h264parse name=parse{i} ! '
            f'avdec_h264 name=decoder{i} ! '
            f'autovideosink sync=false name=sink{i}'
        )
    return " ".join(parts)

def attach_probes(pipeline, csv_writer, csv_file):
    for i in range(len(RTP_PORTS)):
        # Entry: src pad of udpsrc (buffer just arrived from the network)
        src = pipeline.get_by_name(f"src{i}")
        if src:
            src_pad = src.get_static_pad("src")
            if src_pad:
                src_pad.add_probe(Gst.PadProbeType.BUFFER, make_entry_probe(i))
            else:
                print(f"[WARN] Could not get src pad for src{i}")
        else:
            print(f"[WARN] Could not find element src{i}")

        # Exit: sink pad of autovideosink (frame is about to be displayed)
        sink = pipeline.get_by_name(f"sink{i}")
        if sink:
            sink_pad = sink.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(Gst.PadProbeType.BUFFER, make_exit_probe(i, csv_writer, csv_file))
            else:
                print(f"[WARN] Could not get sink pad for sink{i}")
        else:
            print(f"[WARN] Could not find element sink{i}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    csv_file, csv_writer = open_csv_log()

    Gst.init(None)
    pipeline_str = build_pipeline()
    pipeline = Gst.parse_launch(pipeline_str)

    attach_probes(pipeline, csv_writer, csv_file)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — latency will print per frame per camera.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        print("CSV log saved.")

if __name__ == "__main__":
    main()
