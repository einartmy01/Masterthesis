#!/usr/bin/env python3

import csv
import os
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IPs        = ["192.168.0.100", "192.168.0.101", "192.168.0.102"]
USER           = "admin"
PASS           = "NilsNils"
RTSP_PORT      = "554"
RECEIVER_IP    = "100.70.208.109"
RECEIVER_IFACE = "tailscale0"     # outbound interface to receiver
RTP_PORTS      = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────


# ── Camera check ──────────────────────────────────────────────────────────────

def check_cameras():
    print("Checking camera reachability...")
    for cam_ip in CAM_IPs:
        if subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True).returncode != 0:
            print(f"  ✗ Camera {cam_ip} unreachable"); sys.exit(1)
        if subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True).returncode != 0:
            print(f"  ✗ RTSP port closed on {cam_ip}"); sys.exit(1)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)


# ── RTP header reader ─────────────────────────────────────────────────────────

def read_rtp_header(buf):
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None, None
    try:
        if len(info.data) < 4:
            return None, None
        marker = (info.data[1] & 0x80) != 0
        seq    = struct.unpack_from('!H', info.data, 2)[0]
        return seq, marker
    finally:
        buf.unmap(info)


# ── Background CSV writer ─────────────────────────────────────────────────────

transit_queue  = deque()

def writer_thread(transit_writer):
    while True:
        wrote = False
        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)


def start_writer_thread(transit_writer):
    t = threading.Thread(target=writer_thread, args=(transit_writer,), daemon=True)
    t.start()


# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs(timestamp):
    os.makedirs("logs/transit", exist_ok=True)


    transit_path   = f"logs/transit/send_transit_{timestamp}.csv"
    transit_file   = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_file)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Transit log  : {transit_path}")
    return (transit_file, transit_writer)


# ── Throughput logging ────────────────────────────────────────────────────────

def throughput_logger(iface, interval=1.0):
    """Logs outbound (tx) Mbps on the receiver-facing interface only."""
    os.makedirs("logs/throughput", exist_ok=True)
    timestamp = datetime.now().strftime("%d.%m-%H:%M")
    path = f"logs/throughput/sender_throughput_{timestamp}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_time", "iface", "tx_mbps"])
        prev_tx = None
        while True:
            with open("/proc/net/dev") as nd:
                for line in nd:
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].rstrip(":") != iface:
                        continue
                    tx = int(parts[9])
                    if prev_tx is not None:
                        tx_mbps = (tx - prev_tx) * 8 / interval / 1_000_000
                        w.writerow([
                            datetime.now().strftime("%H:%M:%S"),
                            iface,
                            f"{tx_mbps:.3f}",
                        ])
                    prev_tx = tx
                    break
            f.flush()
            time.sleep(interval)


# ── Probes ────────────────────────────────────────────────────────────────────
def make_probe_out(cam_idx):
    _time = time.time

    def process_buf(buf):
        seq, _ = read_rtp_header(buf)
        if seq is None:
            return
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))

    def probe_cb(pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            buf = info.get_buffer()
            if buf is not None:
                process_buf(buf)
        elif info.type & Gst.PadProbeType.BUFFER_LIST:
            buf_list = info.get_buffer_list()
            if buf_list is not None:
                for j in range(buf_list.length()):
                    process_buf(buf_list.get(j))
        return Gst.PadProbeReturn.OK
    return probe_cb


# ── Attach probes ─────────────────────────────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(CAM_IPs)):
        udpsink = pipeline.get_by_name(f"udpsink{i}")

        # OUT — udpsink sink pad (packet about to go on the wire)
        udpsink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_probe_out(i))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    check_cameras()

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    (transit_file, transit_writer) = open_csv_logs(timestamp)
    start_writer_thread(transit_writer)

    # Throughput logging — tx only on tailscale0
    threading.Thread(target=throughput_logger, args=(RECEIVER_IFACE, 1.0), daemon=True).start()

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)
    pipeline.set_state(Gst.State.PLAYING)

    print("Sender running — logging passthrough latency, transit, and outbound throughput.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)   # let writer thread drain
        transit_file.flush()
        transit_file.close()
        print("CSV logs saved.")


if __name__ == "__main__":
    main()
