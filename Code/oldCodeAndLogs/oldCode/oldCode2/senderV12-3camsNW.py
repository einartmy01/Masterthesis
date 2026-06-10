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

# Max in-flight records per camera before stale entries are purged.
# One record is created per NAL unit at depay src (t2).
# At 30 fps a GOP of ~60 frames = ~60 records peak. 300 is a safe ceiling.
MAX_TRACKED  = 300
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────
#
# The streaming thread should never touch disk.
# All CSV writes are handed off to a single background thread via a deque.
# deque.append / deque.popleft are GIL-atomic in CPython — no lock needed.

latency_queue = deque()
transit_queue = deque()

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
        daemon=True,  # dies automatically when main exits
    )
    t.start()

# ── RTP sequence reader ───────────────────────────────────────────────────────

def read_rtp_seq(buf):
    """
    Extract the RTP sequence number from a GStreamer buffer.
    Returns the sequence number (int) or None if the buffer cannot be read
    or is too short to contain a valid RTP header (minimum 12 bytes).
    """
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        if len(info.data) < 4:
            return None
        return struct.unpack_from('!H', info.data, 2)[0]
    finally:
        buf.unmap(info)

# ── ms helper ─────────────────────────────────────────────────────────────────

def ms(t_start, t_end):
    """Format a monotonic time delta as a millisecond string."""
    return f"{(t_end - t_start) * 1000:.3f}"

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/latency/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    # ── Per-stage latency log ─────────────────────────────────────────────────
    # One row per RTP packet emitted by udpsink.
    #
    # Because rtph264pay re-packetises NAL units into new RTP packets with new
    # PTS values, the pipeline is split into two independently tracked segments:
    #
    #   Segment A  (keyed by pre-pay PTS — survives rtspsrc → depay intact)
    #     rtsp_unwrap_ms  rtspsrc src pad → depay sink pad
    #     depay_ms        depay sink pad  → depay src pad
    #
    #   Segment B  (keyed by post-pay PTS — assigned by rtph264pay)
    #     pay_ms          pay sink pad    → pay src pad
    #     udpsink_ms      pay src pad     → udpsink sink pad
    #
    #   total_sender_ms = rtsp_unwrap_ms + depay_ms + pay_ms + udpsink_ms
    #   (the t2→t3 gap between depay src and pay sink is immeasurably small
    #    in a direct GStreamer link with no queue element between them)
    #
    #   rtp_seq — links each row to the transit log for network latency analysis

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
    # Paired with the receiver's transit log on (cam, rtp_seq) to compute
    # end-to-end network transit time.
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
# The pipeline has a PTS discontinuity at rtph264pay: depay outputs raw NAL
# units whose PTS comes from the camera, but pay re-packetises them and assigns
# new PTS values. A single PTS key cannot span the full pipeline.
#
# Solution — two independent tracking dicts per camera:
#
#   seg_a[cam][pre_pay_pts]  = {"t0": mono, "t1": mono, "t2": mono}
#     Populated by probes on: rtspsrc src, depay sink, depay src
#     Key: PTS of the raw H.264 NAL buffer (stable across rtspsrc → depay)
#
#   seg_b[cam][post_pay_pts] = {"t3": mono, "t4": mono}
#     Populated by probes on: pay sink, pay src
#     Key: PTS of the re-packetised RTP buffer (assigned by rtph264pay)
#
# At udpsink (t5) the outgoing buffer carries the post-pay PTS, so we look up
# seg_b with that. For seg_a we use the most recently completed pre-pay record
# (the NAL unit that pay just consumed to produce this RTP packet).
# This is valid because pay processes NAL units in order and emits RTP packets
# immediately — there is no reordering between depay src and pay src.

seg_a = [dict() for _ in CAM_IPs]   # pre-pay:  pts → {t0, t1, t2}
seg_b = [dict() for _ in CAM_IPs]   # post-pay: pts → {t3, t4}

# Holds the PTS of the most recently completed seg_a record per camera.
# Updated at t2 (depay src). Consumed at t5 (udpsink) to retrieve seg_a timings.
last_pre_pay_pts = [None] * len(CAM_IPs)

def _purge_oldest(d):
    """Remove the oldest half of dict d when it exceeds MAX_TRACKED."""
    if len(d) >= MAX_TRACKED:
        for k in list(d.keys())[:MAX_TRACKED // 2]:
            del d[k]

# ── Probes ────────────────────────────────────────────────────────────────────

def make_t0_probe(cam_idx):
    """rtspsrc src pad — buffer entering the sender pipeline from the camera."""
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        _purge_oldest(seg_a[cam_idx])
        seg_a[cam_idx].setdefault(pts, {})["t0"] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t1_probe(cam_idx):
    """depay sink pad — buffer entering rtph264depay."""
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        seg_a[cam_idx].setdefault(pts, {})["t1"] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t2_probe(cam_idx):
    """
    depay src pad — NAL unit leaving rtph264depay.
    Also records this PTS as the latest pre-pay PTS for this camera,
    so the final probe can retrieve the correct seg_a record.
    """
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        seg_a[cam_idx].setdefault(pts, {})["t2"] = time.monotonic()
        last_pre_pay_pts[cam_idx] = pts
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t3_probe(cam_idx):
    """pay sink pad — NAL unit entering rtph264pay."""
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        _purge_oldest(seg_b[cam_idx])
        seg_b[cam_idx].setdefault(pts, {})["t3"] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t4_probe(cam_idx):
    """pay src pad — RTP packet leaving rtph264pay (post-pay PTS assigned here)."""
    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        seg_b[cam_idx].setdefault(pts, {})["t4"] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_final_probe(cam_idx):
    """
    udpsink sink pad — last measurable point on the sender.

    Retrieves:
      - seg_b record by post-pay PTS (t3, t4)
      - seg_a record by last_pre_pay_pts (t0, t1, t2)

    Computes all stage deltas and enqueues one latency row and one transit row.
    Both records are removed after use.
    """
    def process_buf(buf):
        t5         = time.monotonic()
        post_pts   = buf.pts
        if post_pts == Gst.CLOCK_TIME_NONE:
            return

        # Segment B — pay timings keyed by post-pay PTS
        rec_b = seg_b[cam_idx].pop(post_pts, None)
        if rec_b is None or "t3" not in rec_b or "t4" not in rec_b:
            return

        # Segment A — pre-pay timings keyed by last depay src PTS
        pre_pts = last_pre_pay_pts[cam_idx]
        if pre_pts is None:
            return
        rec_a = seg_a[cam_idx].pop(pre_pts, None)
        if rec_a is None or not all(k in rec_a for k in ("t0", "t1", "t2")):
            return

        seq = read_rtp_seq(buf)
        if seq is None:
            return

        rtsp_unwrap_ms  = ms(rec_a["t0"], rec_a["t1"])
        depay_ms        = ms(rec_a["t1"], rec_a["t2"])
        pay_ms          = ms(rec_b["t3"], rec_b["t4"])
        udpsink_ms      = ms(rec_b["t4"], t5)
        total_sender_ms = ms(rec_a["t0"], t5)

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

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

# ── Attach probes ─────────────────────────────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(CAM_IPs)):
        src   = pipeline.get_by_name(f"src{i}")
        depay = pipeline.get_by_name(f"depay{i}")
        pay   = pipeline.get_by_name(f"pay{i}")
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # t0 — rtspsrc src pad is dynamic; attach probe via pad-added signal
        def on_pad_added(_, pad, cam_idx=i):
            if pad.get_direction() == Gst.PadDirection.SRC:
                pad.add_probe(Gst.PadProbeType.BUFFER, make_t0_probe(cam_idx))
        src.connect("pad-added", on_pad_added)

        # t1 — depay sink (enters depayloader)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_t1_probe(i))

        # t2 — depay src (leaves depayloader, records last pre-pay PTS)
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t2_probe(i))

        # t3 — pay sink (enters repayloader)
        pay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_t3_probe(i))

        # t4 — pay src (leaves repayloader, post-pay PTS now assigned)
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t4_probe(i))

        # t5 — udpsink sink (final — compute and log everything)
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
