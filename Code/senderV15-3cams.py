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
# FLUSH_EVERY  = 50

# Max in-flight seg_a / seg_b records per camera before purging stale entries.
# At 30 fps a GOP is ~60 frames. 200 is a safe ceiling with headroom.
MAX_TRACKED  = 200
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────
#
# Streaming threads never touch disk.
# Rows are handed off via deques — GIL-atomic in CPython, no lock needed.

pipeline_queue = deque()
transit_queue = deque()

def writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    #pipeline_count= 0
    #transit_count= 0
    while True:
        wrote = False

        while pipeline_queue:
            pipeline_writer.writerow(pipeline_queue.popleft())
            #pipeline_count+= 1
            wrote = True
        # Safety flsuhers, to avoid a crash to ruin the Data
        # if pipeline_count>= FLUSH_EVERY:
        #     pipeline_file.flush()
        #     pipeline_count= 0

        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            #transit_count+= 1
            wrote = True
        # if transit_count>= FLUSH_EVERY:
        #     transit_file.flush()
        #     transit_count= 0

        if not wrote:
            time.sleep(0.005)  # 5 ms idle — no busy spin

def start_writer_thread(pipeline_writer, pipeline_file, transit_writer, transit_file):
    # daemon is to kill this thread when main exits
    thread = threading.Thread(
        target=writer_thread,
        args=(pipeline_writer, pipeline_file, transit_writer, transit_file),
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

    # Pipeline log
    pipeline_path = f"logs/pipeline/sender/sender_latency_{timestamp}.csv"
    pipeline_file    = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_file)
    pipeline_writer.writerow([
        "wall_time",
        "cam",
        "rtp_seq",
        "rtsp_unwrap_ms",
        "depay_ms",
        "pay_ms",
        "udpsink_ms",
        "total_sender_ms",
    ])

    # Transit log
    transit_path = f"logs/transit/sender_transit_{timestamp}.csv"
    transit_file    = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_file)
    transit_writer.writerow(["abs_time", "cam", "rtp_seq"])

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
# Two dicts per camera — one per pipeline segment (see CSV log comment above).
#
#   seg_a[cam][pre_pts]   = (t0, t1, t2)   monotonic timestamps as a tuple
#   seg_b[cam][post_pts]  = (t3, t4)        monotonic timestamps as a tuple
#
# Tuples are cheaper than dicts for fixed-size records.
#
# post_to_pre[cam][post_pts] = pre_pts
#   Built at t4 where both PTSs are simultaneously known.
#   Consumed at t5 so the final probe can find the correct seg_a/seg_b records
#   using buf.pts directly — no shared guesswork variable needed at t5.
#
# last_pre_pay_pts[cam]: still used as the bridge from t2 → t4.
#   t4 reads it to complete seg_b and write post_to_pre, then t5 ignores it.

seg_a        = [dict() for _ in CAM_IPs]
seg_b        = [dict() for _ in CAM_IPs]
post_to_pre  = [dict() for _ in CAM_IPs]  # maps post-pay PTS → pre-pay PTS, built at t4, read at t5
last_pre_pay_pts = [None]  * len(CAM_IPs)


def _purge(d):
    """Purge oldest half of d. Only called when len(d) >= MAX_TRACKED."""
    keys = list(d.keys())
    for k in keys[:len(keys) // 2]:
        del d[k]

# ── Probes ────────────────────────────────────────────────────────────────────
#
# 5 probes per camera, 15 total across 3 cameras:
#
#   t0  rtspsrc src pad    — dynamic pad, attached via pad-added signal
#   t1  depay sink pad
#   t2  depay src pad      — also updates last_pre_pay_pts
#   t3  pay sink pad
#   t4  pay src pad
#   t5  udpsink sink pad   — final probe, computes and logs everything
#
# seg_a[cam][pre_pts]  = (t0, t1, t2)  — pre-pay segment
# seg_b[cam][post_pts] = (t3, t4)      — post-pay segment

def make_t0_probe(cam_idx):
    """
    rtspsrc src pad — first measurable point on the sender.
    Buffer has been fully received from the camera TCP stream and unwrapped
    from RTSP interleaved framing. Keyed by pre-pay PTS.
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
        _seg_a[pts] = (_mono(), None, None)  # (t0, t1=pending, t2=pending)
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t1_probe(cam_idx):
    """
    depay sink pad — buffer entering rtph264depay.
    t1 - t0 = rtsp_unwrap_ms (inter-element handoff from rtspsrc).
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
        rec = _seg_a.get(pts)
        if rec is None:
            return Gst.PadProbeReturn.OK
        _seg_a[pts] = (rec[0], _mono(), None)  # preserve t0, set t1
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t2_probe(cam_idx):
    """
    depay src pad — NAL unit leaving rtph264depay.
    t2 - t1 = depay_ms. Also updates last_pre_pay_pts so the final probe
    can retrieve the correct seg_a record at udpsink.
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
        if rec is None or rec[1] is None:  # t1 must already be set
            return Gst.PadProbeReturn.OK
        _seg_a[pts] = (rec[0], rec[1], _mono())  # complete: (t0, t1, t2)
        _last_pre_pay_pts[cam_idx] = pts
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t3_probe(cam_idx):
    """
    pay sink pad — NAL unit entering rtph264pay.
    t3 anchors pay_ms. At this point PTS is still the pre-pay camera PTS —
    pay has not yet rewritten it — so we key by pre-pay PTS here.
    """
    _seg_b = seg_b[cam_idx]
    _mono  = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        if len(_seg_b) >= MAX_TRACKED:
            _purge(_seg_b)
        _seg_b[pts] = (_mono(), None)  # (t3, t4=pending)
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_t4_probe(cam_idx):
    """
    pay src pad — RTP packet leaving rtph264pay, post-pay PTS now assigned.
    seg_b was keyed at t3 by the pre-pay PTS (the only PTS available at pay
    sink). We retrieve it using last_pre_pay_pts which was set at t2.
    t4 - t3 = pay_ms.
    At this exact moment we know both PTSs — pre (from last_pre_pay_pts) and
    post (from buf.pts) — so we write post_to_pre[post_pts] = pre_pts here.
    t5 can then look up the correct pre_pts directly from buf.pts.
    """
    _seg_b            = seg_b[cam_idx]
    _last_pre_pay_pts = last_pre_pay_pts
    _post_to_pre      = post_to_pre[cam_idx]
    _mono             = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pre_pts = _last_pre_pay_pts[cam_idx]
        if pre_pts is None:
            return Gst.PadProbeReturn.OK
        rec = _seg_b.get(pre_pts)
        if rec is None or rec[0] is None:
            return Gst.PadProbeReturn.OK
        _seg_b[pre_pts] = (rec[0], _mono())  # complete: (t3, t4)
        post_pts = buf.pts
        if post_pts != Gst.CLOCK_TIME_NONE:
            if len(_post_to_pre) >= MAX_TRACKED:
                _purge(_post_to_pre)
            _post_to_pre[post_pts] = pre_pts  # bridge for t5
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_final_probe(cam_idx):
    """
    Fires on udpsink sink pad — last measurable point on the sender.

    Looks up pre_pts via post_to_pre[buf.pts] — a direct per-packet mapping
    built at t4 where both PTSs are simultaneously known. This replaces the
    old last_pre_pay_pts shared variable which was overwritten too rapidly
    and caused most packets to be skipped.
    Computes all stage deltas, enqueues one pipeline row and one transit row.
    Both records are removed after use to keep memory flat.
    """
    _seg_a       = seg_a[cam_idx]
    _seg_b       = seg_b[cam_idx]
    _post_to_pre = post_to_pre[cam_idx]
    _mono        = time.monotonic
    _time        = time.time

    def process_buf(buf):
        t5       = _mono()
        post_pts = buf.pts
        if post_pts == Gst.CLOCK_TIME_NONE:
            return

        # Look up the pre-pay PTS directly from the post-pay PTS.
        # This mapping was built at t4 where both were simultaneously known.
        pre_pts = _post_to_pre.pop(post_pts, None)
        if pre_pts is None:
            return

        rec_a = _seg_a.pop(pre_pts, None)
        if rec_a is None or rec_a[1] is None or rec_a[2] is None:
            return

        rec_b = _seg_b.pop(pre_pts, None)
        if rec_b is None or rec_b[1] is None:
            return

        seq = read_rtp_seq(buf)
        if seq is None:
            return

        t0, t1, t2 = rec_a
        t3, t4     = rec_b

        def ms(time_a, time_b):
            return f"{(time_b - time_a) * 1000:.3f}"

        rtsp_unwrap_ms  = ms(t0, t1)  # rtspsrc src  → depay sink
        depay_ms        = ms(t1, t2)  # depay sink   → depay src
        pay_ms          = ms(t3, t4)  # pay sink     → pay src
        udpsink_ms      = ms(t4, t5)  # pay src      → udpsink sink
        total_sender_ms = ms(t0, t5)  # rtspsrc src  → udpsink sink (full sender)

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        pipeline_queue.append((
            now, cam_idx, seq,
            rtsp_unwrap_ms, depay_ms, pay_ms, udpsink_ms, total_sender_ms,
        ))
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))

    def probe_cb(pad, info):
        # Fix: check probe type before calling getter — avoids GStreamer assertion
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
        src   = pipeline.get_by_name(f"src{i}")
        depay = pipeline.get_by_name(f"depay{i}")
        pay   = pipeline.get_by_name(f"pay{i}")
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # t0 — rtspsrc src pad is dynamic, waits for pad is created, attach via pad-added signal
        def on_pad_added(_, pad, cam_idx=i):
            if pad.get_direction() == Gst.PadDirection.SRC:
                pad.add_probe(Gst.PadProbeType.BUFFER, make_t0_probe(cam_idx))
        src.connect("pad-added", on_pad_added)

        # t1 — depay sink (enters depayloader)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_t1_probe(i))

        # t2 — depay src (leaves depayloader, updates last_pre_pay_pts)
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t2_probe(i))

        # t3 — pay sink (enters repayloader, pre-pay PTS still intact)
        pay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_t3_probe(i))

        # t4 — pay src (leaves repayloader, post-pay PTS now assigned)
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_t4_probe(i))

        # t5 — udpsink sink (final — compute and log everything) # Can receive one at the time (Buffer) or multiple (Buffer List)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_final_probe(i))

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
