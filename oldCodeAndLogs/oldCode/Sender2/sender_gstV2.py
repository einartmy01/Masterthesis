#!/usr/bin/env python3
"""
sender_gst.py
-------------
GStreamer pipeline for the sender (moving machine).

Receives video from 3 IP cameras over RTSP, repackages as RTP,
and forwards to the receiver over UDP.

On each frame, stamps the current system time (GPS-disciplined via
vbsptParser.py run at startup) into a CSV log.

Run vbsptParser.py once before starting this script to ensure the
system clock is set from GPS.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import csv
import os
import subprocess
import sys
import time

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IPs     = ["192.168.0.100", "192.168.1.101", "192.168.2.102"]
USER        = "admin"
PASS        = "NilsNils"
RTSP_PORT   = "554"
INTERFACES  = ["eth0", "eth1", "enp0s31f6"]
LOCAL_IPS   = ["192.168.0.50/24", "192.168.1.50/24", "192.168.2.50/24"]
RECEIVER_IP = "10.238.111.249"
RTP_PORTS   = ["5000", "5002", "5004"]
LOG_DIR     = "logs"
# ─────────────────────────────────────────────────────────────────────────────


def setup_network():
    print("Configuring network interfaces...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", LOCAL_IPS[i], "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)
    print("Network configured.")


def check_cameras():
    print("Checking camera reachability...")
    for i, cam_ip in enumerate(CAM_IPs):
        ping = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True)
        if ping.returncode != 0:
            print(f"ERROR: Camera {i} at {cam_ip} not reachable.")
            sys.exit(1)
        rtsp = subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True)
        if rtsp.returncode != 0:
            print(f"ERROR: RTSP port not open for camera {i} at {cam_ip}.")
            sys.exit(1)
    print("All cameras reachable.")


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


def run():
    setup_network()
    check_cameras()

    os.makedirs(LOG_DIR, exist_ok=True)
    Gst.init(None)

    pipeline = Gst.parse_launch(build_pipeline())

    frame_counters = [0] * len(CAM_IPs)

    # One CSV log per camera
    csv_files = []
    writers   = []
    for i in range(len(CAM_IPs)):
        path = os.path.join(LOG_DIR, f"camera{i}_sender.csv")
        f    = open(path, "w", newline="")
        w    = csv.writer(f)
        w.writerow(["frame_seq", "send_time_ns", "gst_pts_ns"])
        csv_files.append(f)
        writers.append(w)
        print(f"Camera {i} logging → {path}")

    def make_probe(cam_idx):
        def on_buffer(pad, info):
            buf = info.get_buffer()

            # ── Timestamp ────────────────────────────────────────────────────
            # time.time_ns() is the GPS-disciplined system clock.
            # This is what the receiver will compare against on arrival.
            # ─────────────────────────────────────────────────────────────────
            send_time_ns = time.time_ns()

            pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1
            if pts == -1:
                return Gst.PadProbeReturn.OK

            seq = frame_counters[cam_idx]
            writers[cam_idx].writerow([seq, send_time_ns, pts])
            csv_files[cam_idx].flush()
            frame_counters[cam_idx] += 1

            return Gst.PadProbeReturn.OK
        return on_buffer

    # Probe on pay src pad — as late as possible before the packet leaves
    for i in range(len(CAM_IPs)):
        pay = pipeline.get_by_name(f"pay{i}")
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_probe(i))

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running. Press Ctrl+C to stop.")

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


if __name__ == "__main__":
    run()
