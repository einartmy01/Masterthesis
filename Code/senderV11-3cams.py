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

latency_queue = deque()
transit_queue  = deque()

def _writer_thread(latency_writer, latency_file, transit_writer, transit_file):
    l_count = 0
    t_count = 0
    while True:
        wrote = False

        while latency_queue:
            latency_writer.writerow(latency_queue.popleft())
            l_count += 1
            wrote = True
        if l_count >= FLUSH_EVERY:
            latency_file.flush()
            l_count = 0

        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            t_count += 1
            wrote = True
        if t_count >= FLUSH_EVERY:
            transit_file.flush()
            t_count = 0

        if not wrote:
            time.sleep(0.005)  # 5 ms idle sleep — no busy spin

def start_writer_thread(latency_writer, latency_file, transit_writer, transit_file):
    t = threading.Thread(
        target=_writer_thread,
        args=(latency_writer, latency_file, transit_writer, transit_file),
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
    os.makedirs("logs/latency/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    # ── Per-stage latency log ─────────────────────────────────────────────────
    # One row per buffer, all stages in columns so you can read across a row
    # and immediately see where time was spent.
    #
    # Columns:
    #   wall_time        — human clock when the buffer reached udpsink (end of sender)
    #   cam              — camera index (0 / 1 / 2)
    #   rtp_seq          — RTP sequence number (ties rows to transit log)
    #   rtsp_unwrap_ms   — time inside rtspsrc (TCP receive + RTP unwrap)
    #   depay_ms         — time inside rtph264depay
    #   pay_ms           — time inside rtph264pay (re-packetisation)
    #   udpsink_ms       — time from pay src → udpsink sink (GStreamer hand-off)
    #   total_sender_ms  — full rtspsrc-src → udpsink-sink wall time on this machine

    latency_path = f"logs/latency/sender/sender_latency_{timestamp}.csv"
    latency_f    = open(latency_path, "w", newline="")
    latency_writer = csv.writer(latency_f)
    latency_writer.writerow([
        "wall_time",
        "cam",
        "rtp_seq",
        "rtsp_unwrap_ms",
        "depay_ms",
        "pay_ms",
        "udpsink_ms",
        "total_sender_ms",
    ])

    # ── Transit log ───────────────────────────────────────────────────────────
    # Kept separate — paired with receiver log on rtp_seq to compute
    # end-to-end transit time across the network.
    transit_path = f"logs/transit/sender_transit_{timestamp}.csv"
    transit_f    = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam", "rtp_seq"])

    print(f"Latency log : {latency_path}")
    print(f"Transit log : {transit_path}")
    return (latency_f, latency_writer), (transit_f, transit_writer)

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

# ── Timing state ──────────────────────────────────────────────────────────────
#
# stage_times[cam_idx] is a dict keyed by buffer PTS (presentation timestamp).
# Each value is itself a dict of stage → monotonic time.
#
# Why PTS as key:
#   t0–t4 probes fire on raw H.264 buffers — no RTP sequence number exists yet.
#   PTS is assigned by the camera and survives through depay and pay unchanged,
#   so it uniquely identifies a buffer across all stages before udpsink.
#
# At t5 (udpsink) we read the PTS off the outgoing RTP buffer to look up the
# correct per-packet timing record, compute all deltas, then delete the record
# to keep memory bounded.
#
# MAX_TRACKED caps the dict size in case a packet is dropped before t5 and
# its record would otherwise linger forever.

MAX_TRACKED = 500

stage_times = [dict() for _ in CAM_IPs]

# ── Probes ────────────────────────────────────────────────────────────────────

def make_stage_probe(cam_idx, stage_key):
    """
    Records monotonic time for one pipeline stage, keyed by buffer PTS.
    If the dict is over MAX_TRACKED (stale entries from dropped packets),
    purge the oldest half before inserting.
    """
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK

        st = stage_times[cam_idx]
        if pts not in st:
            if len(st) >= MAX_TRACKED:
                # Drop oldest half by insertion order (Python 3.7+ dicts are ordered)
                purge = list(st.keys())[:MAX_TRACKED // 2]
                for k in purge:
                    del st[k]
            st[pts] = {}

        st[pts][stage_key] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_final_probe(cam_idx):
    """
    Fires at udpsink sink pad — the last measurable point on the sender.
    Looks up the per-packet timing record by PTS, computes all stage deltas,
    enqueues one row for the latency log and one for the transit log,
    then removes the record from the dict.
    """
    def process_buf(buf):
        t5  = time.monotonic()
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return

        st = stage_times[cam_idx]
        record = st.pop(pts, None)
        if record is None:
            return

        # Guard: all upstream stages must have fired for this packet
        if not all(k in record for k in ("t0", "t1", "t2", "t3", "t4")):
            return

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        seq = read_rtp_seq(buf)

        def ms(a, b):
            return f"{(b - a) * 1000:.3f}"

        rtsp_unwrap_ms  = ms(record["t0"], record["t1"])  # rtspsrc src  → depay sink
        depay_ms        = ms(record["t1"], record["t2"])  # depay sink   → depay src
        pay_ms          = ms(record["t3"], record["t4"])  # pay sink     → pay src
        udpsink_ms      = ms(record["t4"], t5)            # pay src      → udpsink sink
        total_sender_ms = ms(record["t0"], t5)            # full sender path

        latency_queue.append((
            now,
            cam_idx,
            seq,
            rtsp_unwrap_ms,
            depay_ms,
            pay_ms,
            udpsink_ms,
            total_sender_ms,
        ))

        transit_queue.append((f"{time.time():.6f}", cam_idx, seq))

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
    """
    Attach probes at every stage boundary in order:

        rtspsrc src  →  depay sink  →  depay src
                     →  pay sink    →  pay src
                     →  udpsink sink  (final)
    """
    for i in range(len(CAM_IPs)):
        src   = pipeline.get_by_name(f"src{i}")
        depay = pipeline.get_by_name(f"depay{i}")
        pay   = pipeline.get_by_name(f"pay{i}")
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # t0 — rtspsrc src pad (packet fully unwrapped from TCP/RTSP)
        # Dynamic pad — must connect via pad-added signal
        def on_pad_added(_, pad, cam_idx=i):
            if pad.get_direction() == Gst.PadDirection.SRC:
                pad.add_probe(Gst.PadProbeType.BUFFER, make_stage_probe(cam_idx, "t0"))
        src.connect("pad-added", on_pad_added)

        # t1 — depay sink pad (enters depayloader)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_stage_probe(i, "t1"))

        # t2 — depay src pad (leaves depayloader)
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_stage_probe(i, "t2"))

        # t3 — pay sink pad (enters repayloader)
        pay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_stage_probe(i, "t3"))

        # t4 — pay src pad (leaves repayloader)
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_stage_probe(i, "t4"))

        # t5 — udpsink sink pad (final probe — computes and logs everything)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_final_probe(i))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    (latency_file, latency_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(latency_writer, latency_file, transit_writer, transit_file)

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
        time.sleep(0.1)  # let writer thread drain the queues
        latency_file.flush()
        transit_file.flush()
        latency_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
