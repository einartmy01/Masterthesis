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
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────
#
# Streaming threads never touch disk.
# Rows are handed off via deques — GIL-atomic in CPython, no lock needed.

pipeline_queue = deque()
transit_queue  = deque()

def writer_thread(pipeline_writer, transit_writer):
    while True:
        wrote = False
        while pipeline_queue:
            pipeline_writer.writerow(pipeline_queue.popleft())
            wrote = True
        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)  # 5 ms idle — no busy spin

def start_writer_thread(pipeline_writer, transit_writer):
    thread = threading.Thread(
        target=writer_thread,
        args=(pipeline_writer, transit_writer),
        daemon=True,
    )
    thread.start()

# ── RTP header reader ─────────────────────────────────────────────────────────

def read_rtp_header(buf):
    """
    Read RTP sequence number and marker bit from a GStreamer buffer.
    Returns (seq, marker) or (None, None) if the buffer cannot be read.

    RTP header layout (first 4 bytes):
      byte 0: V(2) P(1) X(1) CC(4)
      byte 1: M(1) PT(7)       ← marker bit is MSB
      byte 2-3: sequence number (16-bit big-endian)
    """
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

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    # Pipeline log — one row per NAL unit (~25/sec per camera).
    #
    # 2 probes:
    #   IN   depay sink pad  — appends timestamp on every marked packet
    #   OUT  udpsink sink    — pops oldest t_in on every marked packet,
    #                          computes and logs pipeline_ms
    #
    # pipeline_ms — time from marked packet at IN to marked packet at OUT.
    #               Represents the full sender pipeline duration for one NAL unit.
    pipeline_path   = f"logs/pipeline/sender/sender_latency_{timestamp}.csv"
    pipeline_file   = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_file)
    pipeline_writer.writerow([
        "wall_time",
        "cam",
        "rtp_seq",
        "pipeline_ms",
        "dropped_nals",
    ])

    # Transit log — one row per RTP packet at udpsink.
    # Paired with receiver on (cam, rtp_seq) for network latency.
    transit_path   = f"logs/transit/sender_transit_{timestamp}.csv"
    transit_file   = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_file)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    return (pipeline_file, pipeline_writer), (transit_file, transit_writer)

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
    """
    Each camera stream gets its own queue element after depay and after pay.

    queue after depay:
        Gives each stream its own OS thread so the three cameras don't compete
        for a single streaming thread. Important fix for frame loss with multiple cameras.
        max-size-buffers=1, just holds 1 frame at the time.
        Leaky downstream says to move on to next frame instead of stalling.

    queue after pay:
        Decouples rtph264pay from udpsink so pay never stalls waiting for the
        network stack to accept a packet.
    """
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

# ── Timing state ──────────────────────────────────────────────────────────────
#
# One deque of timestamps per camera.
# Probe IN appends a timestamp on every marked packet at depay sink.
# Probe OUT popleft()s the oldest timestamp on every marked packet at udpsink.
# Under normal operation the deque holds at most 1-2 entries per camera.

t_in_queues = [deque() for _ in CAM_IPs]

# ── Probes ────────────────────────────────────────────────────────────────────
#
# 2 probes per camera, 6 total across 3 cameras:
#
#   IN   depay sink pad  — appends timestamp on marked packets
#   OUT  udpsink sink    — pops oldest timestamp on marked packets, logs one row

def make_probe_in(cam_idx):
    """
    depay sink pad — first measurable point, incoming RTP from camera.
    Appends current timestamp to the queue on every marked packet.
    Each entry represents the arrival time of one NAL unit boundary.
    """
    _mono       = time.monotonic
    _t_in_queue = t_in_queues[cam_idx]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        _, marker = read_rtp_header(buf)
        if marker:
            _t_in_queue.append(_mono())
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_probe_out(cam_idx):
    """
    udpsink sink pad — last measurable point, outgoing RTP to receiver.

    On marked packets only:
      - pops the oldest t_in from the queue (matching NAL unit boundary)
      - computes pipeline_ms = now - t_in
      - logs one pipeline row and one transit row
    
    Non-marked packets are only logged to the transit file for network analysis.
    """
    _mono       = time.monotonic
    _time       = time.time
    _t_in_queue = t_in_queues[cam_idx]

    def process_buf(buf):
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return

        # Always log to transit for network latency analysis
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))

        # Only log pipeline_ms on marked packets — one row per NAL unit
        if not marker or not _t_in_queue:
            return
        # if len(_t_in_queue) > 1:
        #     print(f"cam{cam_idx} queue_len={len(_t_in_queue)}")

        dropped_nals = 0 # if len(_t_in_queue) > 1:
        #     print(f"cam{cam_idx} queue_len={len(_t_in_queue)}")

        while len(_t_in_queue) > 1:
            dropped_nals += 1
            _t_in_queue.popleft()
        
        t_now        = _mono()
        t_start      = _t_in_queue.popleft()
        pipeline_ms  = f"{(t_now - t_start) * 1000:.3f}"
        now          = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        pipeline_queue.append((
            now, cam_idx, seq, pipeline_ms, dropped_nals
        ))

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
        depay = pipeline.get_by_name(f"depay{i}")
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # IN — depay sink (incoming RTP, appends timestamp on marked packets)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_probe_in(i))

        # OUT — udpsink sink (outgoing RTP, logs pipeline_ms on marked packets)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_probe_out(i))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(pipeline_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running — logging per-stage latency and RTP transit timestamps.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)  # let writer thread drain
        pipeline_file.flush()
        transit_file.flush()
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
