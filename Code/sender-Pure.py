#!/usr/bin/env python3
import subprocess
import sys

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IP0      = "192.168.0.100"
CAM_IP1      = "192.168.1.101"
CAM_IP2      = "192.168.2.102"
CAM_IPs      = [CAM_IP0, CAM_IP1, CAM_IP2]
USER         = "admin"
PASS         = "NilsNils"
RTSP_PORT    = "554"
INTERFACES   = ["eth0", "eth1", "enp0s31f6"]
LOCAL_IPS    = ["192.168.0.50/24", "192.168.1.50/24", "192.168.2.50/24"]
#RECEIVER_IP  = "172.30.154.249"
RECEIVER_IP  = "100.92.97.93"
RTP_PORTS    = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

# ── Network setup ─────────────────────────────────────────────────────────────

def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", LOCAL_IPS[i], "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)

def check_cameras():
    print("Checking camera reachability...")
    for cam_ip in CAM_IPs:
        if subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True).returncode != 0:
            print(f"Camera at {cam_ip} not reachable."); sys.exit(1)
        if subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True).returncode != 0:
            print(f"RTSP port not reachable for camera at {cam_ip}."); sys.exit(1)

# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_pre{i} ! '
            f'rtph264pay config-interval=1 pt=96 name=pay{i} ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_post{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running ")
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
