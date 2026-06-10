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
            f'avdec_h264 name=decoder{i} ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream name=q_post{i} ! '
            f'autovideosink sync=false name=sink{i}'
        )
    return " ".join(parts)

# ── Background writer ─────────────────────────────────────────────────────────

pipeline_queue = deque()
transit_queue  = deque()

def _writer_thread(pipeline_writer, transit_writer):
    while True:
        wrote = False
        while pipeline_queue:
            pipeline_writer.writerow(pipeline_queue.popleft())
            wrote = True
        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)

def start_writer_thread(pipeline_writer, transit_writer):
    t = threading.Thread(
        target=_writer_thread,
        args=(pipeline_writer, transit_writer),
        daemon=True
    )
    t.start()

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/receiver", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    pipeline_path   = f"logs/pipeline/receiver/rec_pipe_{timestamp}.csv"
    pipeline_f      = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_f)
    pipeline_writer.writerow(["wall_time", "cam_index", "pipeline_ms"])

    transit_path   = f"logs/transit/rec_transit_{timestamp}.csv"
    transit_f      = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline latency log : {pipeline_path}")
    print(f"Transit log          : {transit_path}")
    return (pipeline_f, pipeline_writer), (transit_f, transit_writer)

# ── RTP header reader ─────────────────────────────────────────────────────────

def read_rtp_header(buf):
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

# ── Timing state ──────────────────────────────────────────────────────────────

t_in_queues = [deque() for _ in RTP_PORTS]

# ── Probes ────────────────────────────────────────────────────────────────────

def make_entry_probe(cam_idx):
    _mono       = time.monotonic
    _time       = time.time
    _t_in_queue = t_in_queues[cam_idx]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))
        if marker:
            _t_in_queue.append(_mono())
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_exit_probe(cam_idx):
    _mono       = time.monotonic
    _t_in_queue = t_in_queues[cam_idx]

    def process_buf():
        if not _t_in_queue:
            return
        print(f"Cam{cam_idx}, Queue length {_t_in_queue.__len__()}")

        latency_ms = (_mono() - _t_in_queue.popleft()) * 1000
        pipeline_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            f"{latency_ms:.4f}",
        ))

    def probe_cb(pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            process_buf()
        elif info.type & Gst.PadProbeType.BUFFER_LIST:
            buf_list = info.get_buffer_list()
            if buf_list is not None:
                for _ in range(buf_list.length()):
                    process_buf()
        return Gst.PadProbeReturn.OK
    return probe_cb

# ── Attach probes ─────────────────────────────────────────────────────────────

def attach_probes(pipeline):
    for i in range(len(RTP_PORTS)):
        pipeline.get_by_name(f"src{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_entry_probe(i)
        )
        pipeline.get_by_name(f"sink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_exit_probe(i)
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(pipeline_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — logging pipeline latency and RTP transit timestamps.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)
        pipeline_file.flush()
        transit_file.flush()
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
