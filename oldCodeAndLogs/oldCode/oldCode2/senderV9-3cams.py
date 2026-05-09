#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import csv
import struct
import threading
from collections import deque
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

# How many rows to accumulate before flushing to disk
FLUSH_EVERY  = 50
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────
#
# The streaming thread should never touch disk.
# All CSV writes are handed off to a single background thread via a deque.
# deque.append / deque.popleft are GIL-atomic in CPython — no lock needed.

pipeline_queue = deque()
transit_queue  = deque()

def _writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    p_count = 0
    t_count = 0
    while True:
        wrote = False

        while pipeline_queue:
            pipeline_writer.writerow(pipeline_queue.popleft())
            p_count += 1
            wrote = True
        if p_count >= FLUSH_EVERY:
            pipeline_file.flush()
            p_count = 0

        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            t_count += 1
            wrote = True
        if t_count >= FLUSH_EVERY:
            transit_file.flush()
            t_count = 0

        if not wrote:
            time.sleep(0.005)  # 5 ms idle sleep — no busy spin

def start_writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    t = threading.Thread(
        target=_writer_thread,
        args=(pipeline_writer, pipeline_file, transit_writer, transit_file),
        daemon=True  # dies automatically when main exits
    )
    t.start()

# ── RTP sequence reader ───────────────────────────────────────────────────────
# Assumes buffer is valid RTP — skips all safety checks for speed.

def read_rtp_seq(buf):
    success, info = buf.map(Gst.MapFlags.READ)
    seq = struct.unpack_from('!H', info.data, 2)[0]
    buf.unmap(info)
    return seq

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    pipeline_path = f"logs/pipeline/sender/sender_pipeline_latency_{timestamp}.csv"
    pipeline_f    = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_f)
    pipeline_writer.writerow(["wall_time", "cam_index", "latency_ms"])

    transit_path = f"logs/transit/sender_transit_{timestamp}.csv"
    transit_f    = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline latency log : {pipeline_path}")
    print(f"Transit log          : {transit_path}")
    return (pipeline_f, pipeline_writer), (transit_f, transit_writer)

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

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'rtph264pay pt=96 config-interval=1 name=pay{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)

# ── Probes ────────────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx):
    def probe_cb(pad, info):
        entry_times[cam_idx] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_pipeline_exit_probe(cam_idx):
    def probe_cb(pad, info):
        latency_ms = (time.monotonic() - entry_times[cam_idx]) * 1000
        pipeline_queue.append((datetime.now().strftime("%H:%M:%S.%f")[:-3], cam_idx, f"{latency_ms:.4f}"))
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_transit_probe(cam_idx):
    def process_buf(buf):
        transit_queue.append((f"{time.time():.6f}", cam_idx, read_rtp_seq(buf)))

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is not None:
            process_buf(buf)
            return Gst.PadProbeReturn.OK
        buf_list = info.get_buffer_list()
        for j in range(buf_list.length()):
            process_buf(buf_list.get(j))
        return Gst.PadProbeReturn.OK
    return probe_cb

def attach_probes(pipeline):
    for i in range(len(CAM_IPs)):
        depay = pipeline.get_by_name(f"depay{i}")
        depay.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, make_entry_probe(i))
        depay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, make_pipeline_exit_probe(i))

        udpsink = pipeline.get_by_name(f"udpsink{i}")
        udpsink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_transit_probe(i)
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running — logging pipeline latency and RTP transit timestamps.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)  # let writer thread drain the queues
        pipeline_file.flush()
        transit_file.flush()
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
