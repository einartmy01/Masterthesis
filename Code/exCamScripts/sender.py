#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import subprocess
import sys

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IP       = "192.168.1.100"
USER         = "admin"
PASS         = "NilsNils"
RTSP_PORT    = "554"
INTERFACE    = "enp0s31f6"
LOCAL_IP     = "192.168.1.20"
RECEIVER_IP  = "10.185.193.249"
RTP_PORT     = "5000"
LOG_FILE     = "logs/sender_timestamps.csv"
# ─────────────────────────────────────────────────────────────────────────────

def setup_network():
    print("Configuring sender network...")
    subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACE], check=True)
    subprocess.run(["sudo", "ip", "addr", "add", f"{LOCAL_IP}/24", "dev", INTERFACE], check=True)
    subprocess.run(["sudo", "ip", "link", "set", INTERFACE, "up"], check=True)

def check_camera():
    print("Checking camera reachability...")
    r = subprocess.run(["ping", "-c", "2", CAM_IP], capture_output=True)
    if r.returncode != 0:
        print("Camera not reachable."); sys.exit(1)
    r = subprocess.run(["nc", "-z", "-w", "3", CAM_IP, RTSP_PORT], capture_output=True)
    if r.returncode != 0:
        print("RTSP port not reachable."); sys.exit(1)

def main():
    setup_network()
    check_camera()

    os.makedirs("logs", exist_ok=True)
    Gst.init(None)

    pipeline_str = (
        f'rtspsrc location="rtsp://{USER}:{PASS}@{CAM_IP}:{RTSP_PORT}/Streaming/Channels/101" '
        f'protocols=tcp latency=0 name=src ! '
        f'rtph264depay name=depay ! '
        f'rtph264pay pt=96 config-interval=1 name=pay ! '
        f'udpsink host={RECEIVER_IP} port={RTP_PORT} sync=false async=false'
    )

    pipeline = Gst.parse_launch(pipeline_str)

    # Open CSV
    csv_file = open(LOG_FILE, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["wall_time_ns", "gst_buffer_pts_ns", "stage"])

    # Probe: after depay (post-camera, pre-network) — marks when frame left camera pipeline
    depay = pipeline.get_by_name("depay")
    src_pad = depay.get_static_pad("src")

    def on_buffer(pad, info, stage):
        buf = info.get_buffer()
        wall_ns = time.time_ns()
        pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1
        writer.writerow([wall_ns, pts, stage])
        csv_file.flush()
        return Gst.PadProbeReturn.OK

    src_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_depay")

    # Start pipeline
    pipeline.set_state(Gst.State.PLAYING)
    print(f"Sender running. Logging timestamps to {LOG_FILE}")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        print(f"Timestamps saved to {LOG_FILE}")

if __name__ == "__main__":
    main()
