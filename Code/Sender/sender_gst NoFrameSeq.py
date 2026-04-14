#!/usr/bin/env python3
"""
sender_gst.py
-------------
Shared GStreamer pipeline core for the sender.
Not meant to be run directly — imported by sender_vbox.py and sender_wall.py.

Builds one RTSP source + RTP forward pipeline per camera, attaches buffer
probes on depay, and calls the provided get_time_ns() function to timestamp
each frame. Logs to one CSV file per camera.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import csv
import os
import subprocess
import sys

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IP0    = "192.168.0.100"
CAM_IP1    = "192.168.1.101"
CAM_IP2    = "192.168.2.102"
CAM_IPs    = [CAM_IP0, CAM_IP1, CAM_IP2]
USER       = "admin"
PASS       = "NilsNils"
RTSP_PORT  = "554"
INTERFACES = ["eth0", "eth1", "enp0s31f6"]
LOCAL_IPS  = ["192.168.0.50/24", "192.168.1.50/24", "192.168.2.50/24"]
RECEIVER_IP = "10.238.111.249"
RTP_PORTS  = ["5000", "5002", "5004"]
LOG_DIR    = "logs"
# ─────────────────────────────────────────────────────────────────────────────


def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", LOCAL_IPS[i], "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)


def check_cameras():
    print("Checking camera reachability...")
    for i, cam_ip in enumerate(CAM_IPs):
        ping = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True)
        if ping.returncode != 0:
            print(f"Camera {i} at {cam_ip} not reachable."); sys.exit(1)
        rtsp = subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True)
        if rtsp.returncode != 0:
            print(f"RTSP port not reachable for camera {i} at {cam_ip}."); sys.exit(1)


def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'rtph264pay pt=96 config-interval=1 name=pay{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false'
        )
    return " ".join(parts)


def run(get_time_ns, on_frame=None):
    """
    Build and run the GStreamer sender pipeline.

    Parameters
    ----------
    get_time_ns : callable
        Returns current timestamp in nanoseconds (int), or None if unavailable.
    on_frame : callable, optional
        Called on each buffer with (cam_idx, pts, time_ns).
        Use this for sending sync packets in sender_vbox.py.
    """
    setup_network()
    check_cameras()

    os.makedirs(LOG_DIR, exist_ok=True)
    Gst.init(None)

    pipeline = Gst.parse_launch(build_pipeline())

    # One CSV per camera
    csv_files = []
    writers   = []
    for i in range(len(CAM_IPs)):
        path = os.path.join(LOG_DIR, f"camera{i}_sender_timestamps.csv")
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

            if time_ns is None or pts == -1:
                return Gst.PadProbeReturn.OK

            writers[cam_idx].writerow([time_ns, pts, stage])
            csv_files[cam_idx].flush()

            if on_frame is not None:
                on_frame(cam_idx, pts, time_ns)

            return Gst.PadProbeReturn.OK
        return on_buffer

    for i in range(len(CAM_IPs)):
        depay = pipeline.get_by_name(f"depay{i}")
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_probe(i, "post_depay"))

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender pipeline running. Press Ctrl+C to stop.")

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
