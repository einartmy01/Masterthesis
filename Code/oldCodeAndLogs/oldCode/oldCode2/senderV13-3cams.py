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

# Max in-flight seg_a / seg_b records per camera before purging stale entries.
# At 30 fps a GOP is ~60 frames. 200 is a safe ceiling with headroom.
MAX_TRACKED  = 200
# ─────────────────────────────────────────────────────────────────────────────

# ── Background writer ─────────────────────────────────────────────────────────
#
# Streaming threads never touch disk.
# Rows are handed off via deques — GIL-atomic in CPython, no lock needed.

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
            time.sleep(0.005)  # 5 ms idle — no busy spin

def start_writer_thread(latency_writer, latency_file, transit_writer, transit_file):
    t = threading.Thread(
        target=_writer_thread,
        args=(latency_writer, latency_file, transit_writer, transit_file),
        daemon=True,
    )
    t.start()

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

# ── ms helper ─────────────────────────────────────────────────────────────────

def ms(t_start, t_end):
    """Return elapsed milliseconds between two monotonic timestamps as a string."""
    return f"{(t_end - t_start) * 1000:.3f}"

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/latency/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    # ── Per-stage latency log ─────────────────────────────────────────────────
    # One row per RTP packet emitted by udpsink.
    #
    # rtph264pay re-packetises NAL units and assigns new PTS values, so the
    # pipeline is split into two independently tracked segments:
    #
    #   Segment A  (pre-pay, keyed by camera PTS)
    #     rtsp_unwrap_ms  rtspsrc src pad → depay sink pad
    #     depay_ms        depay sink pad  → depay src pad
    #
    #   Segment B  (post-pay, keyed by pay's new PTS)
    #     pay_ms          pay sink pad    → pay src pad
    #     udpsink_ms      pay src pad     → udpsink sink pad
    #
    #   total_sender_ms   seg_a t0 → udpsink t5 (full sender wall time)
    #   rtp_seq           links to transit log for network latency analysis

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
    # Paired with receiver transit log on (cam, rtp_seq) to compute
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
    """
    Each camera stream gets its own queue element after depay and after pay.

    queue after depay:
        Gives each stream its own OS thread so the three cameras don't compete
        for a single streaming thread. This is the biggest single fix for frame
        loss with multiple cameras — without it all three block each other.
        max-size-buffers=1 keeps latency minimal (no buffering beyond one frame).

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
# last_pre_pay_pts[cam]: PTS of the most recently completed seg_a record.
# Updated at t2 (depay src). Read at t5 (udpsink) to retrieve seg_a timings.
# Valid because pay processes NAL units in arrival order with no reordering.

seg_a            = [dict() for _ in CAM_IPs]
seg_b            = [dict() for _ in CAM_IPs]
last_pre_pay_pts = [None]  * len(CAM_IPs)

def _purge(d):
    """Purge oldest half of d. Only called when len(d) >= MAX_TRACKED."""
    keys = list(d.keys())
    for k in keys[:len(keys) // 2]:
        del d[k]

# ── Probes ────────────────────────────────────────────────────────────────────
#
# Probe count vs V12:
#   V12: 6 blocking probes per camera (t0–t5) = 18 total
#   V13: 3 blocking probes per camera          = 9 total
#
# Consolidations:
#   t0+t1 → single probe on depay sink:
#     t0 is recorded just before t1 on the same pad. The gap (rtspsrc → depay
#     sink) is captured as the difference. Eliminates one probe on rtspsrc src.
#
#   t3+t4 → single probe on pay src:
#     pay_ms = pay sink → pay src. We capture t3 the moment pay src fires
#     (pay has just finished processing) by reading last_pre_pay_pts which was
#     set at t2. Eliminates one probe on pay sink.
#
#   This halves the number of synchronous Python calls on the streaming thread.

def make_depay_sink_probe(cam_idx):
    """
    Fires on depay sink pad.
    Records t0 (arrives from rtspsrc) and t1 (enters depay) as two
    monotonic timestamps taken in immediate succession on the same pad.
    The difference t1-t0 is effectively zero — what matters is t0 as the
    anchor for rtsp_unwrap_ms relative to when depay src fires at t2.
    """
    _seg_a   = seg_a[cam_idx]
    _mono    = time.monotonic

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        now = _mono()
        if len(_seg_a) >= MAX_TRACKED:
            _purge(_seg_a)
        # Store (t0, t1) — both captured at the same instant on this pad.
        # t0 represents "buffer arrived at sender pipeline entry".
        # t1 = t0 here; depay_ms will be t2 - t1 measured at depay src.
        _seg_a[pts] = (now, now, None)  # (t0, t1, t2=pending)
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_depay_src_probe(cam_idx):
    """
    Fires on depay src pad.
    Completes the seg_a record by recording t2 and updating last_pre_pay_pts.
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
        # Replace tuple with completed record including t2
        _seg_a[pts] = (rec[0], rec[1], _mono())
        _last_pre_pay_pts[cam_idx] = pts
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_pay_src_probe(cam_idx):
    """
    Fires on pay src pad.
    Records t3 (pay started — approximated as pay src fire time minus pay
    processing time, which we cannot directly observe, so t3 ≈ t4 here and
    pay_ms is measured as the gap between pay src fire and udpsink fire).
    Records t4 = now.
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
        now = _mono()
        if len(_seg_b) >= MAX_TRACKED:
            _purge(_seg_b)
        _seg_b[pts] = (now, now)  # (t3≈t4, t4) — pay_ms absorbed into udpsink_ms
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_final_probe(cam_idx):
    """
    Fires on udpsink sink pad — last measurable point on the sender.

    Looks up seg_b by post-pay PTS and seg_a by last_pre_pay_pts.
    Computes all stage deltas, enqueues one latency row and one transit row.
    Both records are removed after use to keep memory flat.
    """
    _seg_a            = seg_a[cam_idx]
    _seg_b            = seg_b[cam_idx]
    _last_pre_pay_pts = last_pre_pay_pts
    _mono             = time.monotonic
    _time             = time.time

    def process_buf(buf):
        t5       = _mono()
        post_pts = buf.pts
        if post_pts == Gst.CLOCK_TIME_NONE:
            return

        rec_b = _seg_b.pop(post_pts, None)
        if rec_b is None:
            return

        pre_pts = _last_pre_pay_pts[cam_idx]
        if pre_pts is None:
            return
        rec_a = _seg_a.pop(pre_pts, None)
        if rec_a is None or rec_a[2] is None:
            return

        seq = read_rtp_seq(buf)
        if seq is None:
            return

        t0, t1, t2 = rec_a
        t3, t4     = rec_b

        rtsp_unwrap_ms  = ms(t0, t1)   # rtspsrc → depay sink  (inter-element gap)
        depay_ms        = ms(t1, t2)   # depay sink → depay src
        pay_ms          = ms(t3, t4)   # pay src recorded twice — near zero by design
        udpsink_ms      = ms(t4, t5)   # pay src → udpsink sink (includes pay+handoff)
        total_sender_ms = ms(t0, t5)   # full sender path

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        latency_queue.append((
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
        src    = pipeline.get_by_name(f"src{i}")
        depay  = pipeline.get_by_name(f"depay{i}")
        pay    = pipeline.get_by_name(f"pay{i}")
        sink   = pipeline.get_by_name(f"udpsink{i}")

        # rtspsrc has a dynamic src pad — attach via pad-added signal
        def on_pad_added(_, pad, cam_idx=i):
            if pad.get_direction() == Gst.PadDirection.SRC:
                pad.add_probe(Gst.PadProbeType.BUFFER, make_depay_sink_probe(cam_idx))
        src.connect("pad-added", on_pad_added)

        # depay src — completes seg_a record, updates last_pre_pay_pts
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_depay_src_probe(i))

        # pay src — records seg_b entry (t3≈t4), post-pay PTS now assigned
        pay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_pay_src_probe(i))

        # udpsink sink — final probe, computes and logs everything
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
        time.sleep(0.1)  # let writer thread drain
        latency_file.flush()
        transit_file.flush()
        latency_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
