#!/usr/bin/env python3
import os
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
RTP_PORTS = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────


# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'h264parse name=parse{i} ! '
            f'avdec_h264 max-threads=1 name=decoder{i} ! '
            f'queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_post{i} ! '
            f'xvimagesink sync=false name=sink{i}'
        )
    return " ".join(parts)


# ── Background CSV writer ─────────────────────────────────────────────────────

full_csv_queue    = deque()
transit_csv_queue = deque()


def _writer_thread(full_writer, transit_writer):
    while True:
        wrote = False
        while full_csv_queue:
            full_writer.writerow(full_csv_queue.popleft())
            wrote = True
        while transit_csv_queue:
            transit_writer.writerow(transit_csv_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)


def start_writer_thread(full_writer, transit_writer):
    t = threading.Thread(
        target=_writer_thread,
        args=(full_writer, transit_writer),
        daemon=True,
    )
    t.start()


# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/receiver", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    ts = datetime.now().strftime("%d.%m-%H:%M")

    full_path = f"logs/pipeline/receiver/rec_full_{ts}.csv"
    full_f    = open(full_path, "w", newline="")
    full_w    = csv.writer(full_f)
    full_w.writerow(["wall_time", "cam_index", "frame_in", "frame_out", "full_ms", "skipped"])

    transit_path = f"logs/transit/rec_transit_{ts}.csv"
    transit_f    = open(transit_path, "w", newline="")
    transit_w    = csv.writer(transit_f)
    transit_w.writerow(["abs_time", "cam_index", "frame_in", "rtp_seq"])

    print(f"Full latency log : {full_path}")
    print(f"Transit log      : {transit_path}")
    return (full_f, full_w), (transit_f, transit_w)


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
#
# Each entry in full_in_queues[i] is a tuple:
#     (monotonic_t, frame_in_counter)
#
# • udpsrc probe  → appends one entry per frame (on first RTP packet)
# • sink probe    → pops one entry per decoded frame and logs full_ms
#
# Because the leaky queue between decoder and sink can silently drop frames
# we track how many entries we had to skip (queue depth > 1 at sink) and
# report them as `skipped` so the thesis reader knows input ≠ output only
# when the downstream queue was overloaded, not due to a measurement bug.

full_in_queues = [deque() for _ in RTP_PORTS]   # (mono_t, frame_in)


# ── udpsrc probe — one timestamp per frame ────────────────────────────────────

def make_udpsrc_probe(cam_idx):
    _mono          = time.monotonic
    _wtime         = time.time
    _full_q        = full_in_queues[cam_idx]
    expecting_first = [True]   # state machine: are we waiting for frame start?
    frame_counter   = [0]      # monotonically increasing frame index

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK

        if expecting_first[0]:
            # ── First RTP packet of a new frame ──────────────────────────────
            frame_counter[0] += 1
            fidx = frame_counter[0]
            t    = _mono()

            _full_q.append((t, fidx))

            # Transit log: one row per frame (not per packet)
            transit_csv_queue.append((
                f"{_wtime():.6f}",
                cam_idx,
                fidx,
                seq,
            ))
            expecting_first[0] = False

        if marker:
            # Marker bit set → this is the last packet of the frame
            expecting_first[0] = True

        return Gst.PadProbeReturn.OK
    return probe_cb


# ── sink probe — one decoded frame per buffer ─────────────────────────────────

def make_sink_probe(cam_idx):
    _mono        = time.monotonic
    _full_q      = full_in_queues[cam_idx]
    frame_out    = [0]         # output frame counter (increments here)
    initialized  = [False]     # discard startup burst before queues are stable

    def process_one_frame():
        if not _full_q:
            # Sink fired but no matching entry — pipeline is still warming up
            # or the very first frames arrived before the probe was attached.
            return

        if not initialized[0]:
            # Flush any pre-init entries so we start with a clean slate
            _full_q.clear()
            initialized[0] = True
            return

        t_now = _mono()

        # Count how many input frames we are skipping (leaky queue dropped them
        # between decoder and sink; they have no corresponding sink event).
        skipped = 0
        while len(_full_q) > 1:
            _full_q.popleft()
            skipped += 1

        t_start, frame_in = _full_q.popleft()
        frame_out[0] += 1

        full_ms = (t_now - t_start) * 1000

        full_csv_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            frame_in,
            frame_out[0],
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


# ── Attach probes ─────────────────────────────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(RTP_PORTS)):
        # Input stamp: first RTP packet of each frame
        pipeline.get_by_name(f"src{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            make_udpsrc_probe(i),
        )
        # Output stamp: decoded frame arrives at sink
        pipeline.get_by_name(f"sink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_sink_probe(i),
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (full_file, full_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(full_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — logging full receiver latency and transit.")
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

        full_file.flush();    full_file.close()
        transit_file.flush(); transit_file.close()
        print("CSV logs saved.")


if __name__ == "__main__":
    main()
