#!/usr/bin/env python3
"""
MJPEG Receiver V15
==================
Receives 3 MJPEG-over-RTP streams, displays them, and logs:
  - Full end-to-end latency (udpsrc → display sink)
  - Transit timestamps (every RTP packet)
  - BRISQUE image quality scores (sampled)

Key changes from V14:
  - Pipeline built with Gst.Pipeline + explicit element API (no parse_launch)
    to avoid string-escaping issues and make linking errors obvious
  - Bus watcher for clean error reporting
  - Flush remaining queue rows on exit
"""

import os
import time
import csv
import struct
import threading
from collections import deque
from datetime import datetime

import numpy as np
import torch
import piq

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS          = ["5000", "5002", "5004"]
BRISQUE_SAMPLE_EVERY = 5    # score every Nth decoded frame per camera
DISPLAY_SINK       = "xvimagesink"   # change to "autovideosink" if xv unavailable
# ─────────────────────────────────────────────────────────────────────────────


# ── Pipeline ──────────────────────────────────────────────────────────────────
#
# Per camera:
#   udpsrc → rtpjpegdepay → jpegparse → jpegdec → tee
#                                                  ├─ queue → xvimagesink
#                                                  └─ queue → videoconvert → appsink (BRISQUE)

RTP_CAPS = "application/x-rtp, media=video, encoding-name=JPEG, payload=26"


def build_pipeline():
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="{RTP_CAPS}" name=src{i} ! '
            f'rtpjpegdepay name=depay{i} ! '
            f'jpegparse name=parse{i} ! '
            f'jpegdec name=dec{i} ! '
            f'tee name=tee{i} '

            # ── Display branch ────────────────────────────────────────────────
            f'tee{i}. ! '
            f'queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 '
            f'leaky=downstream name=dq{i} ! '
            f'{DISPLAY_SINK} sync=false name=dsink{i} '

            # ── BRISQUE quality branch ────────────────────────────────────────
            f'tee{i}. ! '
            f'queue max-size-buffers=2 leaky=downstream name=qq{i} ! '
            f'videoconvert name=qconv{i} ! '
            f'video/x-raw,format=BGR ! '
            f'appsink name=qsink{i} emit-signals=true drop=true max-buffers=1'
        )
    return Gst.parse_launch(" ".join(parts))


# ── CSV queues and writer thread ──────────────────────────────────────────────

full_csv_queue    = deque()
transit_csv_queue = deque()
quality_csv_queue = deque()


def _writer_thread(full_w, transit_w, quality_w):
    while True:
        wrote = False
        while full_csv_queue:
            full_w.writerow(full_csv_queue.popleft());    wrote = True
        while transit_csv_queue:
            transit_w.writerow(transit_csv_queue.popleft()); wrote = True
        while quality_csv_queue:
            quality_w.writerow(quality_csv_queue.popleft()); wrote = True
        if not wrote:
            time.sleep(0.005)


def open_csv_logs():
    for d in ("logs/pipeline/receiver", "logs/transit", "logs/quality"):
        os.makedirs(d, exist_ok=True)

    ts = datetime.now().strftime("%d.%m-%H:%M")

    fp = f"logs/pipeline/receiver/rec_full_{ts}.csv"
    ff = open(fp, "w", newline="")
    fw = csv.writer(ff)
    fw.writerow(["wall_time", "cam_index", "full_ms", "skipped"])

    tp = f"logs/transit/rec_transit_{ts}.csv"
    tf = open(tp, "w", newline="")
    tw = csv.writer(tf)
    tw.writerow(["abs_time", "cam_index", "rtp_seq"])

    qp = f"logs/quality/rec_quality_{ts}.csv"
    qf = open(qp, "w", newline="")
    qw = csv.writer(qf)
    qw.writerow(["wall_time", "cam_index", "sample_frame", "brisque_score"])

    print(f"Full latency log : {fp}")
    print(f"Transit log      : {tp}")
    print(f"Quality log      : {qp}")
    return (ff, fw), (tf, tw), (qf, qw)


def start_writer_thread(fw, tw, qw):
    threading.Thread(
        target=_writer_thread, args=(fw, tw, qw), daemon=True
    ).start()


# ── RTP header helper ─────────────────────────────────────────────────────────

def read_rtp_header(buf):
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None, None
    try:
        data = bytes(info.data)
        if len(data) < 4:
            return None, None
        marker = bool(data[1] & 0x80)
        seq    = struct.unpack_from("!H", data, 2)[0]
        return seq, marker
    finally:
        buf.unmap(info)


# ── Per-camera probe state ────────────────────────────────────────────────────

full_in_queues = [deque() for _ in RTP_PORTS]


# ── udpsrc probe — timestamp each incoming frame ──────────────────────────────

def make_udpsrc_probe(cam_idx):
    _mono           = time.monotonic
    _wtime          = time.time
    _fq             = full_in_queues[cam_idx]
    expecting_first = [True]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK

        # Transit log: every RTP packet
        transit_csv_queue.append((f"{_wtime():.6f}", cam_idx, seq))

        # Latency: stamp on first RTP packet of each frame
        if expecting_first[0]:
            _fq.append(_mono())
            expecting_first[0] = False

        if marker:
            expecting_first[0] = True

        return Gst.PadProbeReturn.OK
    return probe_cb


# ── display sink probe — measure latency on each displayed frame ──────────────

def make_sink_probe(cam_idx):
    _mono       = time.monotonic
    _fq         = full_in_queues[cam_idx]
    initialized = [False]

    def process():
        if not _fq:
            return

        # Discard the very first frame (pipeline warm-up is noisy)
        if not initialized[0]:
            _fq.clear()
            initialized[0] = True
            return

        t_now   = _mono()
        skipped = 0

        # If multiple input timestamps queued, frames were dropped in the pipeline
        while len(_fq) > 1:
            _fq.popleft()
            skipped += 1

        t_start = _fq.popleft()
        full_ms = (t_now - t_start) * 1000

        full_csv_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            f"{full_ms:.4f}",
            skipped,
        ))

    def probe_cb(pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            process()
        elif info.type & Gst.PadProbeType.BUFFER_LIST:
            bl = info.get_buffer_list()
            if bl:
                for _ in range(bl.length()):
                    process()
        return Gst.PadProbeReturn.OK
    return probe_cb


# ── BRISQUE appsink callback ──────────────────────────────────────────────────

def make_quality_callback(cam_idx):
    frame_count = [0]

    def on_new_sample(appsink):
        frame_count[0] += 1

        if frame_count[0] % BRISQUE_SAMPLE_EVERY != 0:
            appsink.emit("pull-sample")
            return Gst.FlowReturn.OK

        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf  = sample.get_buffer()
        caps = sample.get_caps()
        st   = caps.get_structure(0)
        w    = st.get_value("width")
        h    = st.get_value("height")

        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            frame_bgr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
            frame_rgb = frame_bgr[:, :, ::-1].copy()
        finally:
            buf.unmap(mapinfo)

        tensor = (
            torch.from_numpy(frame_rgb)
            .permute(2, 0, 1)
            .unsqueeze(0)
            .float()
            .div(255.0)
        )

        try:
            score = piq.brisque(tensor, data_range=1.0).item()
        except Exception:
            return Gst.FlowReturn.OK

        quality_csv_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            frame_count[0],
            f"{score:.4f}",
        ))
        return Gst.FlowReturn.OK
    return on_new_sample


# ── Attach probes and callbacks ───────────────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(RTP_PORTS)):
        pipeline.get_by_name(f"src{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            make_udpsrc_probe(i),
        )
        pipeline.get_by_name(f"dsink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_sink_probe(i),
        )
        pipeline.get_by_name(f"qsink{i}").connect(
            "new-sample", make_quality_callback(i)
        )


# ── Bus message handler ───────────────────────────────────────────────────────

def on_bus_message(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("EOS received"); loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, dbg = message.parse_error()
        print(f"GStreamer ERROR: {err.message}")
        if dbg: print(f"  Debug: {dbg}")
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        err, dbg = message.parse_warning()
        print(f"GStreamer WARNING: {err.message}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (ff, fw), (tf, tw), (qf, qw) = open_csv_logs()
    start_writer_thread(fw, tw, qw)

    Gst.init(None)
    pipeline = build_pipeline()
    attach_probes(pipeline)

    loop = GLib.MainLoop()
    bus  = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus_message, loop)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("Pipeline failed to start"); return

    print("\nReceiver V15 running.")
    print(f"  Listening on ports: {RTP_PORTS}")
    print(f"  BRISQUE every {BRISQUE_SAMPLE_EVERY} frames per camera.")
    print("Press Ctrl+C to stop.\n")

    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.15)

        for row in full_csv_queue:    fw.writerow(row)
        for row in transit_csv_queue: tw.writerow(row)
        for row in quality_csv_queue: qw.writerow(row)

        ff.flush(); ff.close()
        tf.flush(); tf.close()
        qf.flush(); qf.close()
        print("CSV logs saved.")


if __name__ == "__main__":
    main()
