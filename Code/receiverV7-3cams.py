#!/usr/bin/env python3
import os
import time
import csv
import struct
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

# ── Safe RTP sequence number reader ──────────────────────────────────────────
#
# The RTP fixed header is 12 bytes:
#   Byte 0:   V(2) P(1) X(1) CC(4)
#   Byte 1:   M(1) PT(7)
#   Bytes 2-3: Sequence Number  <-- what we want, big-endian uint16
#   Bytes 4-7: Timestamp
#   Bytes 8-11: SSRC
#
# We read raw bytes directly with Gst.Buffer.map() — no GstRtp bindings needed.
# This avoids the segfault caused by the unstable GstRtp Python GI memory layer.

def read_rtp_seq(buf):
    """
    Extracts the RTP sequence number by reading raw buffer bytes.
    Returns None if the buffer is too small or mapping fails.
    Never raises — all errors return None silently.
    """
    info = None
    try:
        success, info = buf.map(Gst.MapFlags.READ)
        if not success or info is None:
            return None
        data = info.data
        if len(data) < 4:
            return None
        # Bytes 2-3 are the sequence number, big-endian
        seq = struct.unpack_from('!H', data, 2)[0]
        return seq
    except Exception:
        return None
    finally:
        if info is not None:
            try:
                buf.unmap(info)
            except Exception:
                pass

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs", exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")

    pipeline_path = f"logs/receiver_pipeline_latency_{timestamp}.csv"
    pipeline_f = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_f)
    pipeline_writer.writerow(["wall_time", "cam_index", "latency_ms"])

    transit_path = f"logs/receiver_transit_{timestamp}.csv"
    transit_f = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline latency log : {pipeline_path}")
    print(f"Transit log          : {transit_path}")
    return (pipeline_f, pipeline_writer), (transit_f, transit_writer)

# ── Latency probes ────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx, transit_writer, transit_file):
    """
    Fires when a buffer arrives off the wire at udpsrc.
    - Records monotonic time for pipeline latency calculation.
    - Reads RTP seq from raw bytes and logs absolute time for network transit matching.
    """
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        # ── Pipeline latency: start timer ─────────────────────────────────────
        entry_times[cam_idx] = time.monotonic()

        # ── Network transit: read seq from raw RTP header bytes ───────────────
        seq = read_rtp_seq(buf)
        if seq is not None:
            abs_time = time.time()  # GPS-disciplined wall clock
            transit_writer.writerow([f"{abs_time:.6f}", cam_idx, seq])
            transit_file.flush()

        return Gst.PadProbeReturn.OK
    return probe_cb

def make_exit_probe(cam_idx, pipeline_writer, pipeline_file):
    """Computes and logs pipeline latency when a decoded frame reaches the video sink."""
    def probe_cb(pad, info):
        t_entry = entry_times.get(cam_idx)
        if t_entry is not None:
            latency_ms = (time.monotonic() - t_entry) * 1000
            wall_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[CAM {cam_idx}] receiver pipeline latency: {latency_ms:.2f} ms")
            pipeline_writer.writerow([wall_time, cam_idx, f"{latency_ms:.4f}"])
            pipeline_file.flush()
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

def attach_probes(pipeline, pipeline_writer, pipeline_file, transit_writer, transit_file):
    for i in range(len(RTP_PORTS)):
        src = pipeline.get_by_name(f"src{i}")
        if src:
            src_pad = src.get_static_pad("src")
            if src_pad:
                src_pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    make_entry_probe(i, transit_writer, transit_file)
                )
            else:
                print(f"[WARN] Could not get src pad for src{i}")
        else:
            print(f"[WARN] Could not find element src{i}")

        sink = pipeline.get_by_name(f"sink{i}")
        if sink:
            sink_pad = sink.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    make_exit_probe(i, pipeline_writer, pipeline_file)
                )
            else:
                print(f"[WARN] Could not get sink pad for sink{i}")
        else:
            print(f"[WARN] Could not find element sink{i}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()

    Gst.init(None)
    pipeline_str = build_pipeline()
    pipeline = Gst.parse_launch(pipeline_str)

    attach_probes(pipeline, pipeline_writer, pipeline_file, transit_writer, transit_file)

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
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
