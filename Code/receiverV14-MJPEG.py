#!/usr/bin/env python3
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
RTP_PORTS = ["5000", "5002", "5004"]

# BRISQUE: score every Nth decoded frame per camera (1 = every frame).
# BRISQUE takes ~20-50 ms per frame in Python, so sampling is recommended
# to avoid becoming a bottleneck. Set to 1 to score every frame.
BRISQUE_SAMPLE_EVERY = 5
# ─────────────────────────────────────────────────────────────────────────────


# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="application/x-rtp, media=video, encoding-name=JPEG, payload=26" name=src{i} ! '
            f'rtpjpegdepay name=depay{i} ! '
            f'jpegparse name=parse{i} ! '
            f'jpegdec name=decoder{i} ! '
            f'tee name=tee{i} '

            # ── Display branch (unchanged from V12) ──────────────────────────
            f'tee{i}. ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_post{i} ! '
            f'xvimagesink sync=false name=sink{i} '

            # ── Quality branch ───────────────────────────────────────────────
            # videoconvert → BGR so we can read raw bytes as a numpy array.
            # drop=true + max-buffers=1 means appsink never blocks the pipeline;
            # it simply discards frames it cannot keep up with.
            f'tee{i}. ! '
            f'queue max-size-buffers=1 leaky=downstream ! '
            f'videoconvert ! '
            f'video/x-raw,format=BGR ! '
            f'appsink name=qsink{i} emit-signals=true drop=true max-buffers=1'
        )
    return " ".join(parts)


# ── Background CSV writer ─────────────────────────────────────────────────────

full_csv_queue    = deque()
transit_csv_queue = deque()
quality_csv_queue = deque()


def _writer_thread(full_writer, transit_writer, quality_writer):
    while True:
        wrote = False
        while full_csv_queue:
            full_writer.writerow(full_csv_queue.popleft())
            wrote = True
        while transit_csv_queue:
            transit_writer.writerow(transit_csv_queue.popleft())
            wrote = True
        while quality_csv_queue:
            quality_writer.writerow(quality_csv_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)


def start_writer_thread(full_writer, transit_writer, quality_writer):
    t = threading.Thread(
        target=_writer_thread,
        args=(full_writer, transit_writer, quality_writer),
        daemon=True,
    )
    t.start()


# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/receiver", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)
    os.makedirs("logs/quality", exist_ok=True)

    ts = datetime.now().strftime("%d.%m-%H:%M")

    full_path = f"logs/pipeline/receiver/rec_full_{ts}.csv"
    full_f    = open(full_path, "w", newline="")
    full_w    = csv.writer(full_f)
    full_w.writerow(["wall_time", "cam_index", "full_ms", "skipped"])

    transit_path = f"logs/transit/rec_transit_{ts}.csv"
    transit_f    = open(transit_path, "w", newline="")
    transit_w    = csv.writer(transit_f)
    transit_w.writerow(["abs_time", "cam_index", "rtp_seq"])

    quality_path = f"logs/quality/rec_quality_{ts}.csv"
    quality_f    = open(quality_path, "w", newline="")
    quality_w    = csv.writer(quality_f)
    # brisque_score: 0–100, lower = better quality.
    # Lower scores mean the frame looks more like a natural, undistorted image.
    quality_w.writerow(["wall_time", "cam_index", "sample_frame", "brisque_score"])

    print(f"Full latency log : {full_path}")
    print(f"Transit log      : {transit_path}")
    print(f"Quality log      : {quality_path}")
    return (full_f, full_w), (transit_f, transit_w), (quality_f, quality_w)


# ── RTP header parsing ────────────────────────────────────────────────────────

def read_rtp_header(buf):
    """Return (seq, marker) from an RTP buffer, or (None, None) on failure."""
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None, None
    try:
        data = info.data
        if len(data) < 4:
            return None, None
        marker = bool(data[1] & 0x80)
        seq    = struct.unpack_from('!H', data, 2)[0]
        return seq, marker
    finally:
        buf.unmap(info)


# ── Per-camera timing state ───────────────────────────────────────────────────

full_in_queues = [deque() for _ in RTP_PORTS]   # mono_t


# ── udpsrc probe — one timestamp per frame ────────────────────────────────────

def make_udpsrc_probe(cam_idx):
    _mono           = time.monotonic
    _wtime          = time.time
    _full_q         = full_in_queues[cam_idx]
    expecting_first = [True]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK

        # ── Transit log: every RTP packet, matches sender granularity ────────
        transit_csv_queue.append((
            f"{_wtime():.6f}",
            cam_idx,
            seq,
        ))

        # ── Pipeline latency: first NAL of each frame only ───────────────────
        if expecting_first[0]:
            _full_q.append(_mono())
            expecting_first[0] = False

        if marker:
            expecting_first[0] = True

        return Gst.PadProbeReturn.OK
    return probe_cb


# ── sink probe — one decoded frame per buffer ─────────────────────────────────

def make_sink_probe(cam_idx):
    _mono        = time.monotonic
    _full_q      = full_in_queues[cam_idx]
    initialized  = [False]

    def process_one_frame():
        if not _full_q:
            return

        if not initialized[0]:
            _full_q.clear()
            initialized[0] = True
            return

        t_now = _mono()

        skipped = 0
        while len(_full_q) > 1:
            _full_q.popleft()
            skipped += 1

        t_start = _full_q.popleft()

        full_ms = (t_now - t_start) * 1000

        full_csv_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            f"{full_ms:.4f}",
            skipped,
        ))

    def probe_cb(pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            process_one_frame()
        elif info.type & Gst.PadProbeType.BUFFER_LIST:
            buf_list = info.get_buffer_list()
            if buf_list is not None:
                for _ in range(buf_list.length()):
                    process_one_frame()
        return Gst.PadProbeReturn.OK
    return probe_cb


# ── BRISQUE appsink callback ──────────────────────────────────────────────────

def make_quality_callback(cam_idx):
    """
    Called by GStreamer on the appsink's new-sample signal.

    Flow:
      1. Pull the sample and map the raw BGR bytes.
      2. Reshape into an (H, W, 3) numpy array.
      3. Convert BGR → RGB, normalise to [0, 1] float32.
      4. Wrap in a (1, 3, H, W) torch tensor (piq expects NCHW).
      5. Compute BRISQUE score and push to the quality CSV queue.

    Only every BRISQUE_SAMPLE_EVERY-th frame is scored to keep CPU load
    manageable.  The appsink already drops frames it cannot keep up with
    (drop=true, max-buffers=1), so this counter is an additional control
    on top of that.
    """
    frame_count = [0]

    def on_new_sample(appsink):
        frame_count[0] += 1
        if frame_count[0] % BRISQUE_SAMPLE_EVERY != 0:
            # Pull and discard so the appsink doesn't back up
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
            # BGR numpy array
            frame_bgr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
            # Convert BGR → RGB
            frame_rgb = frame_bgr[:, :, ::-1].copy()
        finally:
            buf.unmap(mapinfo)

        # piq.brisque expects a float32 tensor in [0, 1], shape (N, C, H, W)
        tensor = (
            torch.from_numpy(frame_rgb)
            .permute(2, 0, 1)          # HWC → CHW
            .unsqueeze(0)              # → NCHW
            .float()
            .div(255.0)
        )

        try:
            score = piq.brisque(tensor, data_range=1.0).item()
        except Exception:
            # piq can raise if the frame is too small or all-black
            return Gst.FlowReturn.OK

        quality_csv_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            frame_count[0],
            f"{score:.4f}",
        ))

        return Gst.FlowReturn.OK
    return on_new_sample


# ── Attach probes and appsink callbacks ───────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(RTP_PORTS)):
        # Input stamp: first RTP packet of each frame
        pipeline.get_by_name(f"src{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            make_udpsrc_probe(i),
        )
        # Output stamp: decoded frame arrives at display sink
        pipeline.get_by_name(f"sink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_sink_probe(i),
        )
        # BRISQUE quality: connect appsink signal
        qsink = pipeline.get_by_name(f"qsink{i}")
        qsink.connect("new-sample", make_quality_callback(i))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (full_file, full_writer), (transit_file, transit_writer), (quality_file, quality_writer) = open_csv_logs()
    start_writer_thread(full_writer, transit_writer, quality_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — logging full receiver latency, transit, and BRISQUE quality.")
    print(f"BRISQUE sampling: every {BRISQUE_SAMPLE_EVERY} frames per camera.")
    print("Press Ctrl+C to stop.\n")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)

        # Flush any rows that the writer thread hasn't consumed yet
        for row in full_csv_queue:
            full_writer.writerow(row)
        for row in transit_csv_queue:
            transit_writer.writerow(row)
        for row in quality_csv_queue:
            quality_writer.writerow(row)

        full_file.flush();    full_file.close()
        transit_file.flush(); transit_file.close()
        quality_file.flush(); quality_file.close()
        print("CSV logs saved.")


if __name__ == "__main__":
    main()
