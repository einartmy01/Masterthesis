#!/usr/bin/env python3
"""
sender_gst.py
-------------
Receives video from IP cameras over RTSP and forwards to receiver over UDP/RTP.

Before sending each frame, stamps the current GPS-disciplined wall clock time
into the RTP header extension so the receiver can compute transport latency.

Prerequisite: run vbsptParser.py first to set system clock from GPS.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import struct
import subprocess
import sys

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IPs     = ["192.168.0.100", "192.168.1.101", "192.168.2.102"]
USER        = "admin"
PASS        = "NilsNils"
RTSP_PORT   = "554"
INTERFACES  = ["eth0", "eth1", "enp0s31f6"] #Be aware, must be same order as IPs, also might change for computer
LOCAL_IPS   = ["192.168.0.50/24", "192.168.1.50/24", "192.168.2.50/24"]
RECEIVER_IP = "10.238.111.249"
RTP_PORTS   = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────


def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", LOCAL_IPS[i], "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)

def check_cameras():
    print("Checking camera reachability...")
    for cam_ip in enumerate(CAM_IPs):
        ping = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True) #Send 2 ping packets
        if ping.returncode != 0:
            print(f"Camera at {cam_ip} not reachable."); sys.exit(1)
        rtsp = subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True)
        if rtsp.returncode != 0:
            print(f"RTSP port not reachable for camera at {cam_ip}."); sys.exit(1)


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

def extract_timestamp(buffer_with_header):
    """Extract timestamp from buffer header"""
    timestamp_ns = struct.unpack('>Q', buffer_with_header[:8])[0]
    h264_data = buffer_with_header[8:]
    return timestamp_ns, h264_data

def make_probe():
    def on_buffer(info):
        buf = info.get_buffer()

        # ── Stamp GPS wall time into buffer ───────────────────────────────────
        # time.time_ns() is the system clock, disciplined to GPS by vbsptParser.
        # We pack it as 8 bytes and write it into the GStreamer buffer meta.
        # The receiver reads this same value on arrival and subtracts to get
        # transport latency.
        # ─────────────────────────────────────────────────────────────────────
        send_time_ns = time.time_ns()

        buf = info.get_buffer()
        buf = buf.make_writable()

        # Store timestamp in buffer's pts field as a carry-through mechanism.
        # We use the buffer reference time rather than the pipeline pts,
        # by storing our wall-clock value directly.
        meta = buf.add_reference_timestamp_meta(
            Gst.Caps.from_string("timestamp/x-wall-clock"),
            send_time_ns,
            Gst.CLOCK_TIME_NONE
        )

        return Gst.PadProbeReturn.OK
    return on_buffer


def run():
    setup_network()
    check_cameras()

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())

    # Probe on pay src — as late as possible before packet leaves machine
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
        print("Done.")


if __name__ == "__main__":
    run()
