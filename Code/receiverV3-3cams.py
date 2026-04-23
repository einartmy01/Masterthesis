#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import sys
import serial
import struct
import threading
import socket

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS   = ["5000", "5002", "5004"] # List of UDP ports to listen for video streams
# ─────────────────────────────────────────────────────────────────────────────

# ──── Build GStreamer pipeline string for multiple cameras
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


# ── Main 

def main():
    os.makedirs("logs", exist_ok=True)
    Gst.init(None)

    pipeline_str = build_pipeline()
    pipeline = Gst.parse_launch(pipeline_str)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started, waiting for video streams...")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()
