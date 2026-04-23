#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import csv
from datetime import datetime

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
RECEIVER_IP  = "172.30.154.249"
RTP_PORTS    = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_log():
    os.makedirs("logs", exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = f"logs/sender_latency_{timestamp}.csv"
    f = open(path, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(["wall_time", "cam_index", "latency_ms"])
    print(f"Logging to: {path}")
    return f, writer

# ── Latency probes ────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx):
    def probe_cb(pad, info):
        entry_times[cam_idx] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_exit_probe(cam_idx, csv_writer, csv_file):
    def probe_cb(pad, info):
        t_entry = entry_times.get(cam_idx)
        if t_entry is not None:
            latency_ms = (time.monotonic() - t_entry) * 1000
            wall_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[CAM {cam_idx}] sender pipeline latency: {latency_ms:.2f} ms")
            csv_writer.writerow([wall_time, cam_idx, f"{latency_ms:.4f}"])
            csv_file.flush()
        return Gst.PadProbeReturn.OK
    return probe_cb

# ── Network setup ─────────────────────────────────────────────────────────────

def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", f"{LOCAL_IPS[i]}", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)

def check_cameras():
    print("Checking camera reachability...")
    for i, cam_ip in enumerate(CAM_IPs):
        ping = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True)
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

def attach_probes(pipeline, csv_writer, csv_file):
    for i in range(len(CAM_IPs)):
        depay = pipeline.get_by_name(f"depay{i}")
        if depay:
            sink_pad = depay.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(Gst.PadProbeType.BUFFER, make_entry_probe(i))
            else:
                print(f"[WARN] Could not get sink pad for depay{i}")
        else:
            print(f"[WARN] Could not find element depay{i}")

        pay = pipeline.get_by_name(f"pay{i}")
        if pay:
            src_pad = pay.get_static_pad("src")
            if src_pad:
                src_pad.add_probe(Gst.PadProbeType.BUFFER, make_exit_probe(i, csv_writer, csv_file))
            else:
                print(f"[WARN] Could not get src pad for pay{i}")
        else:
            print(f"[WARN] Could not find element pay{i}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    csv_file, csv_writer = open_csv_log()

    Gst.init(None)
    pipeline_str = build_pipeline()
    pipeline = Gst.parse_launch(pipeline_str)

    attach_probes(pipeline, csv_writer, csv_file)

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running — latency will print per buffer per camera.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        print("CSV log saved.")

if __name__ == "__main__":
    main()
