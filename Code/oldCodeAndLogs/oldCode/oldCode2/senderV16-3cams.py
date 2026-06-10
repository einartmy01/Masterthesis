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

# Max in-flight records per camera before purging stale entries.
MAX_TRACKED  = 200
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
    # daemon is to kill this thread when main exits
    thread = threading.Thread(
        target=writer_thread,
        args=(pipeline_writer, transit_writer),
        daemon=True,
    )
    thread.start()

# ── RTP sequence reader ───────────────────────────────────────────────────────

def read_rtp_seq(buf):
    """
    Return the RTP sequence number (int) from a GStreamer buffer,
    or None if the buffer cannot be mapped or is too short.
    RTP sequence number is a 16-bit big-endian value at byte offset 2.
    Minimum valid RTP header is 12 bytes; we only need the first 4.
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

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    # Pipeline log — one row per RTP packet reaching udpsink.
    #
    # 4 probes, t0–t3:
    #   t0  depay src pad   — NAL unit leaving depayloader, first measurement point
    #   t1  pay sink pad    — NAL unit entering payloader
    #   t2  pay src pad     — RTP packet leaving payloader, PTS rewritten here
    #   t3  udpsink sink    — final measurement point
    #
    #   pay_ms      t1 → t2  time inside rtph264pay
    #   udpsink_ms  t2 → t3  time from pay src to udpsink
    #   total_ms    t0 → t3  full pipeline from depay src to udpsink
    pipeline_path   = f"logs/pipeline/sender/sender_latency_{timestamp}.csv"
    pipeline_file   = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_file)
    pipeline_writer.writerow([
        "wall_time",
        "cam",
        "rtp_seq",
        "pay_ms",
        "udpsink_ms",
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
#   seg_a[cam][pre_pts] = (t0, t1)
#       Keyed by pre-pay PTS. t0 recorded at depay src, t1 at pay sink.
#
#   post_to_pre[cam][post_pts] = (pre_pts, t2)
#       Built at t2 (pay src) where both PTSs are simultaneously known.
#       Consumed at t3 so the final probe looks up records directly from
#       buf.pts without any shared guesswork variable.
#
#   last_pre_pay_pts[cam]: bridge from t1 → t2.
#       Updated at t1 (pay sink), read at t2 (pay src) to find the matching
#       seg_a record before the PTS is rewritten by pay.

seg_a            = [dict() for _ in CAM_IPs]
post_to_pre      = [dict() for _ in CAM_IPs]
last_pre_pay_pts = [None] * len(CAM_IPs)

def _purge(d):
    """Purge oldest half of d. Only called when len(d) >= MAX_TRACKED."""
    keys = list(d.keys())
    for k in keys[:len(keys) // 2]:
        del d[k]

# ── Probes ────────────────────────────────────────────────────────────────────
#
# 4 probes per camera, 12 total across 3 cameras:
#
#   t0  depay src pad   — NAL unit leaving depayloader, first measurement point
#   t1  pay sink pad    — NAL unit entering payloader, updates last_pre_pay_pts
#   t2  pay src pad     — RTP leaving payloader, PTS rewritten, builds post_to_pre
#   t3  udpsink sink    — final probe, computes and logs everything

def make_t0_probe(cam_idx):
    """
    depay src pad — first measurable point.
    NAL unit has been fully reassembled from RTP packets by rtph264depay.
    Keyed by pre-pay PTS.
    """
    _seg_a = seg_a[cam_idx]
    _mono  = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        if len(_seg_a) >= MAX_TRACKED:
            _purge(_seg_a)
        _seg_a[pts] = (_mono(), None)  # (t0, t1=pending)
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t1_probe(cam_idx):
    """
    pay sink pad — NAL unit entering rtph264pay.
    PTS is still the pre-pay camera PTS here.
    Completes seg_a and updates last_pre_pay_pts for the t2 bridge.
    """
    _seg_a            = seg_a[cam_idx]
    _last_pre_pay_pts = last_pre_pay_pts
    _mono             = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        rec = _seg_a.get(pts)
        if rec is None:
            return Gst.PadProbeReturn.OK
        _seg_a[pts] = (rec[0], _mono())  # complete: (t0, t1)
        _last_pre_pay_pts[cam_idx] = pts
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t2_probe(cam_idx):
    """
    pay src pad — RTP packet leaving rtph264pay, post-pay PTS now assigned.
    Reads last_pre_pay_pts to find the matching seg_a record, then builds
    post_to_pre[post_pts] = (pre_pts, t2) so t3 can look up the correct
    record directly from buf.pts without any shared guesswork variable.
    """
    _last_pre_pay_pts = last_pre_pay_pts
    _post_to_pre      = post_to_pre[cam_idx]
    _mono             = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        post_pts = buf.pts
        if post_pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        pre_pts = _last_pre_pay_pts[cam_idx]
        if pre_pts is None:
            return Gst.PadProbeReturn.OK
        if len(_post_to_pre) >= MAX_TRACKED:
            _purge(_post_to_pre)
        _post_to_pre[post_pts] = (pre_pts, _mono())  # store pre_pts and t2
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t3_probe(cam_idx):
    """
    udpsink sink pad — last measurable point on the sender.

    Looks up post_to_pre by buf.pts to get pre_pts and t2.
    Looks up seg_a by pre_pts to get t0 and t1.
    Computes all stage deltas, enqueues one pipeline row and one transit row.
    All records are removed after use to keep memory flat.
    """
    _seg_a       = seg_a[cam_idx]
    _post_to_pre = post_to_pre[cam_idx]
    _mono        = time.monotonic
    _time        = time.time

    def process_buf(buf):
        t3       = _mono()
        post_pts = buf.pts
        if post_pts == Gst.CLOCK_TIME_NONE:
            return

        # Look up pre_pts and t2 directly from post-pay PTS — no guesswork
        bridge = _post_to_pre.pop(post_pts, None)
        if bridge is None:
            return
        pre_pts, t2 = bridge

        rec_a = _seg_a.pop(pre_pts, None)
        if rec_a is None or rec_a[1] is None:
            return

        seq = read_rtp_seq(buf)
        if seq is None:
            return

        t0, t1 = rec_a

        def ms(time_a, time_b):
            return f"{(time_b - time_a) * 1000:.3f}"

        pay_ms     = ms(t1, t2)  # pay sink  → pay src
        udpsink_ms = ms(t2, t3)  # pay src   → udpsink sink
        total_ms   = ms(t0, t3)  # depay src → udpsink sink (full pipeline)

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        pipeline_queue.append((
            now, cam_idx, seq,
            pay_ms, udpsink_ms, total_ms,
        ))
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))

    def probe_cb(pad, info):
        # Check probe type before calling getter — avoids GStreamer assertion
        # spam when a BUFFER_LIST arrives and get_buffer() is called on it.
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

        # t0 — depay src (leaves depayloader, first measurement point)
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t0_probe(i))

        # t1 — pay sink (enters payloader, pre-pay PTS still intact)
        pay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_t1_probe(i))

        # t2 — pay src (leaves payloader, post-pay PTS now assigned, builds post_to_pre)
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t2_probe(i))

        # t3 — udpsink sink (final — compute and log everything)
        # Can receive one at a time (Buffer) or multiple (Buffer List)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_t3_probe(i))

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
