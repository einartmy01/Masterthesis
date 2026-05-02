#!/usr/bin/env python3
import os
import time
import csv
import struct
import threading
from collections import deque
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS = ["5000", "5002", "5004"]

FLUSH_EVERY = 50
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────

pipeline_queue = deque()
transit_queue  = deque()

def _writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    p_count = 0
    t_count = 0
    while True:
        wrote = False

        while pipeline_queue:
            pipeline_writer.writerow(pipeline_queue.popleft())
            p_count += 1
            wrote = True
        if p_count >= FLUSH_EVERY:
            pipeline_file.flush()
            p_count = 0

        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            t_count += 1
            wrote = True
        if t_count >= FLUSH_EVERY:
            transit_file.flush()
            t_count = 0

        if not wrote:
            time.sleep(0.005)

def start_writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    t = threading.Thread(
        target=_writer_thread,
        args=(pipeline_writer, pipeline_file, transit_writer, transit_file),
        daemon=True
    )
    t.start()

# ── RTP sequence reader ───────────────────────────────────────────────────────

def read_rtp_seq(buf):
    success, info = buf.map(Gst.MapFlags.READ)
    seq = struct.unpack_from('!H', info.data, 2)[0]
    buf.unmap(info)
    return seq

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/receiver", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    pipeline_path = f"logs/pipeline/receiver/receiver_pipeline_latency_{timestamp}.csv"
    pipeline_f    = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_f)
    pipeline_writer.writerow(["wall_time", "cam_index", "latency_ms"])

    transit_path = f"logs/transit/receiver_transit_{timestamp}.csv"
    transit_f    = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline latency log : {pipeline_path}")
    print(f"Transit log          : {transit_path}")
    return (pipeline_f, pipeline_writer), (transit_f, transit_writer)

# ── Probes ────────────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx):
    """Fires on udpsrc src pad — timestamps arrival and logs RTP seq for transit matching."""
    def probe_cb(pad, info):
        t = time.monotonic()
        entry_times[cam_idx] = t
        transit_queue.append((f"{time.time():.6f}", cam_idx, read_rtp_seq(info.get_buffer())))
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_exit_probe(cam_idx):
    """Fires on autovideosink sink pad — computes full receiver pipeline latency."""
    def probe_cb(pad, info):
        latency_ms = (time.monotonic() - entry_times[cam_idx]) * 1000
        pipeline_queue.append((datetime.now().strftime("%H:%M:%S.%f")[:-3], cam_idx, f"{latency_ms:.4f}"))
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

def attach_probes(pipeline):
    for i in range(len(RTP_PORTS)):
        pipeline.get_by_name(f"src{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_entry_probe(i)
        )
        pipeline.get_by_name(f"sink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_exit_probe(i)
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — logging pipeline latency and RTP transit timestamps.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)
        pipeline_file.flush()
        transit_file.flush()
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
