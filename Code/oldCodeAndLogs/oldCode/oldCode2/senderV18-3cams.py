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

    # Pipeline log — one row per RTP packet reaching udpsink.
    #
    # 3 probes, A–C:
    #   A  depay sink pad  — incoming RTP packets, marker bit tracked
    #   B  pay src pad     — outgoing RTP packets, marker bit tracked
    #   C  udpsink sink    — final measurement point, logs every RTP packet
    #
    # Columns:
    #   depay_pay_ms  — time from last marked packet at A to last marked packet at B
    #                   updated once per NAL unit (~25/sec), shared across all RTP
    #                   packets of that NAL unit in the log
    #   queue_ms      — time from last marked packet at B to this packet at C
    #   total_ms      — time from last marked packet at A to this packet at C
    pipeline_path   = f"logs/pipeline/sender/sender_latency_{timestamp}.csv"
    pipeline_file   = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_file)
    pipeline_writer.writerow([
        "wall_time",
        "cam",
        "rtp_seq",
        "depay_pay_ms",
        "queue_ms",
        "total_ms",
    ])

    # Transit log — paired with receiver on (cam, rtp_seq) for network latency.
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
            f'rtph264pay pt=96 config-interval=1 name=pay{i} ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_post{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)

# ── Timing state ──────────────────────────────────────────────────────────────
#
# No PTS matching. All state is per-camera, updated on marker bit boundaries.
#
# t_depay_sink[cam]  — monotonic timestamp of the last marked RTP packet at depay sink
#                      represents the end of a NAL unit entering the depayloader
#
# t_pay_src[cam]     — monotonic timestamp of the last marked RTP packet at pay src
#                      represents the end of the same NAL unit leaving the payloader
#
# depay_pay_ms[cam]  — last computed depay+pay duration (t_pay_src - t_depay_sink)
#                      updated once per NAL unit, shared across all RTP packets
#                      of that NAL unit logged at udpsink

t_depay_sink  = [None] * len(CAM_IPs)
t_pay_src     = [None] * len(CAM_IPs)
depay_pay_ms  = [None] * len(CAM_IPs)

# ── Probes ────────────────────────────────────────────────────────────────────
#
# 3 probes per camera, 9 total across 3 cameras:
#
#   A  depay sink pad  — tracks marker bit, updates t_depay_sink on marked packets
#   B  pay src pad     — tracks marker bit, updates t_pay_src and depay_pay_ms on marked packets
#   C  udpsink sink    — logs every RTP packet with queue_ms and total_ms

def make_probe_a(cam_idx):
    """
    depay sink pad — incoming RTP packets from camera.
    Records timestamp of each marked packet (last RTP packet of a NAL unit).
    This timestamp anchors the start of the depay+pay measurement window.
    """
    _mono = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK
        #print(f"A cam{cam_idx} seq={seq} marker={marker}")  # temporary
        if marker:
            t_depay_sink[cam_idx] = _mono()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_probe_b(cam_idx):
    """
    pay src pad — outgoing RTP packets after rtph264pay.
    Records timestamp of each marked packet (last RTP packet of a NAL unit).
    Computes depay_pay_ms = t_pay_src - t_depay_sink on each NAL unit boundary.
    """
    _mono = time.monotonic

    def probe_cb(pad, info):
        print("probe b")

        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK
        print(f"probe b, marker: {marker}")
        if marker:
            t_in = t_depay_sink[cam_idx]
            print(f"B cam{cam_idx} seq={seq} marker={marker} t_in={t_in}")
            if t_in is None:
                return Gst.PadProbeReturn.OK
            t_out = _mono()
            t_pay_src[cam_idx]    = t_out
            depay_pay_ms[cam_idx] = f"{(t_out - t_in) * 1000:.3f}"
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_probe_c(cam_idx):
    """
    udpsink sink pad — every outgoing RTP packet.
    Logs one row per packet using the most recently computed NAL unit timestamps.

    depay_pay_ms — shared value from last NAL unit boundary, same for all
                   RTP packets of that NAL unit
    queue_ms     — time from last marked packet at pay src to this packet
    total_ms     — time from last marked packet at depay sink to this packet
    """
    _mono = time.monotonic
    _time = time.time

    def process_buf(buf):
        t_now = _mono()

        seq, marker = read_rtp_header(buf)
        print(f"C cam{cam_idx} seq={seq} marker={marker}")
        if seq is None:
            return

        t_in  = t_depay_sink[cam_idx]
        t_out = t_pay_src[cam_idx]
        dp_ms = depay_pay_ms[cam_idx]

        if t_in is None or t_out is None or dp_ms is None:
            return

        queue_ms = f"{(t_now - t_out) * 1000:.3f}"
        total_ms = f"{(t_now - t_in)  * 1000:.3f}"

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        pipeline_queue.append((
            now, cam_idx, seq,
            dp_ms, queue_ms, total_ms,
        ))
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
        depay = pipeline.get_by_name(f"depay{i}")
        pay   = pipeline.get_by_name(f"pay{i}")
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # A — depay sink (incoming RTP, tracks marker bit)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_probe_a(i))

        # B — pay src (outgoing RTP, tracks marker bit, computes depay_pay_ms)
        pad = pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_probe_b(i))

        # C — udpsink sink (logs every RTP packet)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_probe_c(i))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(pipeline_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())

    pipeline.set_state(Gst.State.PLAYING)
    time.sleep(1)
    attach_probes(pipeline)

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
