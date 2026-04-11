#!/usr/bin/env python3
"""
receiver_gst.py
---------------
Shared GStreamer pipeline core for the receiver.
Not meant to be run directly — imported by receiver_vbox.py and receiver_wall.py.

Builds one decode pipeline per camera, attaches buffer probes on depay and decoder,
and calls the provided get_time_ns() function to timestamp each frame.
Logs to one CSV file per camera.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import csv
import os

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS = ["5000", "5002", "5004"]
LOG_DIR   = "logs"
# ─────────────────────────────────────────────────────────────────────────────


def run(get_time_ns):
    """
    Build and run the GStreamer receiver pipeline.

    Parameters
    ----------
    get_time_ns : callable
        Function that returns the current timestamp in nanoseconds (int),
        or None if no timestamp is available yet.
        Called once per buffer probe hit.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    Gst.init(None)

    # One pipeline string covering all cameras
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" '
            f'name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'h264parse name=parse{i} ! '
            f'avdec_h264 name=decoder{i} ! '
            f'autovideosink sync=false name=sink{i}'
        )
    pipeline_str = " ".join(parts)
    pipeline = Gst.parse_launch(pipeline_str)

    # Open one CSV per camera
    csv_files = []
    writers   = []
    for i in range(len(RTP_PORTS)):
        path = os.path.join(LOG_DIR, f"camera{i}_timestamps.csv")
        f = open(path, "w", newline="")
        w = csv.writer(f)
        w.writerow(["time_ns", "gst_buffer_pts_ns", "stage"])
        csv_files.append(f)
        writers.append(w)
        print(f"Logging camera {i} → {path}")

    def make_probe(cam_idx, stage):
        def on_buffer(pad, info):
            buf     = info.get_buffer()
            time_ns = get_time_ns()
            pts     = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1

            if time_ns is None:
                return Gst.PadProbeReturn.OK

            writers[cam_idx].writerow([time_ns, pts, stage])
            csv_files[cam_idx].flush()
            return Gst.PadProbeReturn.OK
        return on_buffer

    # Attach probes for each camera
    for i in range(len(RTP_PORTS)):
        depay   = pipeline.get_by_name(f"depay{i}")
        decoder = pipeline.get_by_name(f"decoder{i}")
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_probe(i, "post_depay"))
        decoder.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_probe(i, "post_decode"))

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver pipeline running. Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        for f in csv_files:
            f.close()
        print("Done.")
