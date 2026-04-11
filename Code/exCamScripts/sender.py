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
CAM_IP0      = "192.168.1.100"
CAM_IP1      = "192.168.1.101"
CAM_IP2      = "192.168.1.102"  
CAM_IPs      = [CAM_IP0, CAM_IP1, CAM_IP2]     
USER         = "admin"
PASS         = "NilsNils"
RTSP_PORT    = "554"
INTERFACE    = "enp0s31f6"
LOCAL_IP     = "192.168.1.20"
RECEIVER_IP  = "10.238.111.249"
RTP_PORT     = "5000"
LOG_FILE     = "logs/sender_timestamps.csv"
# ─────────────────────────────────────────────────────────────────────────────

# Setup makes sure the sender computer is set to correct IP and can reach the cameras before starting the GStreamer pipeline.
def setup_network():
    print("Configuring sender network...")
    subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACE], check=True)
    subprocess.run(["sudo", "ip", "addr", "add", f"{LOCAL_IP}/24", "dev", INTERFACE], check=True)
    subprocess.run(["sudo", "ip", "link", "set", INTERFACE, "up"], check=True)

# Camera check pings the cameras and tests RTSP port connectivity before starting the pipeline.
def check_camera():
    print("Checking camera reachability...")
    #for cam_ip in CAM_IPs:
    cam_ip       = CAM_IP0


    # Ping of cameras
    pingResponse = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True)
    if pingResponse.returncode != 0:
        print(f"Camera at {cam_ip} not reachable."); sys.exit(1)

    # Test RTSP port connectivity
    # nc, netcat, is a simple utility for testing TCP/UDP connectivity. Here we use it to check if the RTSP port is open on the camera.
    # -z: zero-I/O mode, no data is sent, just checking if the port is open
    # -w 3: wait 3 seconds for a connection before timing out
    rtspResponse = subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True)
    if rtspResponse.returncode != 0:
        print(f"RTSP port not reachable for camera at {cam_ip}."); sys.exit(1)


def main():
    setup_network()
    check_camera()

    os.makedirs("logs", exist_ok=True)
    Gst.init(None)

    pipeline_str = (
        # RTSP connection
        f'rtspsrc location="rtsp://{USER}:{PASS}@{CAM_IP0}:{RTSP_PORT}/Streaming/Channels/101" '
        # RTSP setup 
        f'protocols=tcp latency=0 name=src ! '
        f'rtph264depay name=depay ! '
        f'rtph264pay pt=96 config-interval=1 name=pay ! '

        # Sending RTP packets
        f'udpsink host={RECEIVER_IP} port={RTP_PORT} sync=false async=false'
    )

    pipeline = Gst.parse_launch(pipeline_str)

    # Open CSV
    csv_file = open(LOG_FILE, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["wall_time_ns", "gst_buffer_pts_ns", "stage"])

    # Probe: after depay (post-camera, pre-network) — marks when frame left camera pipeline
    depay = pipeline.get_by_name("depay")

    # It's grabbing the output side of depay — the point where processed data leaves that element.
    src_pad = depay.get_static_pad("src")

    def on_buffer(pad, info, stage):
        buf = info.get_buffer()          # grab the data buffer passing through
        wall_ns = time.time_ns()         # record exact wall clock time RIGHT NOW
        pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1   # grab buffer timestamp
        writer.writerow([wall_ns, pts, stage])   # log to CSV
        csv_file.flush()                 # write immediately, dont wait
        return Gst.PadProbeReturn.OK     # tell GStreamer "all good, let it through"

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
