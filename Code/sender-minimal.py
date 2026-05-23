#!/usr/bin/env python3

import subprocess
import sys

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IPs       = ["192.168.0.100", "192.168.0.101", "192.168.0.102"]
USER          = "admin"
PASS          = "NilsNils"
RTSP_PORT     = "554"
RECEIVER_IP   = "100.70.208.109"
RTP_PORTS     = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────


def check_cameras():
    print("Checking camera reachability...")
    for cam_ip in CAM_IPs:
        if subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True).returncode != 0:
            print(f"  ✗ Camera {cam_ip} unreachable"); sys.exit(1)
        if subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True).returncode != 0:
            print(f"  ✗ RTSP port closed on {cam_ip}"); sys.exit(1)

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false'
        )
    return " ".join(parts)


def main():
    check_cameras()

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    pipeline.set_state(Gst.State.PLAYING)
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
