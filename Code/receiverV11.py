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

network_queue = deque()  # udpsrc → depay
decoder_queue = deque()  # h264parse → autovideosink
full_queue    = deque()  # udpsrc → autovideosink
transit_queue = deque()  # every RTP packet

def _writer_thread(network_writer, decoder_writer, full_writer, transit_writer):
    while True:
        wrote = False
        while network_queue:
            network_writer.writerow(network_queue.popleft())
            wrote = True
        while decoder_queue:
            decoder_writer.writerow(decoder_queue.popleft())
            wrote = True
        while full_queue:
            full_writer.writerow(full_queue.popleft())
            wrote = True
        while transit_queue:
            transit_writer.writerow(transit_queue.popleft())
            wrote = True
        if not wrote:
            time.sleep(0.005)

def start_writer_thread(network_writer, decoder_writer, full_writer, transit_writer):
    t = threading.Thread(
        target=_writer_thread,
        args=(network_writer, decoder_writer, full_writer, transit_writer),
        daemon=True
    )
    t.start()

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs/pipeline/receiver", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    network_path   = f"logs/pipeline/receiver/rec_network_{timestamp}.csv"
    network_f      = open(network_path, "w", newline="")
    network_writer = csv.writer(network_f)
    network_writer.writerow(["wall_time", "cam_index", "rtp_seq", "network_ms", "dropped"])

    decoder_path   = f"logs/pipeline/receiver/rec_decoder_{timestamp}.csv"
    decoder_f      = open(decoder_path, "w", newline="")
    decoder_writer = csv.writer(decoder_f)
    decoder_writer.writerow(["wall_time", "cam_index", "decoder_ms", "dropped"])

    full_path   = f"logs/pipeline/receiver/rec_full_{timestamp}.csv"
    full_f      = open(full_path, "w", newline="")
    full_writer = csv.writer(full_f)
    full_writer.writerow(["wall_time", "cam_index", "full_ms", "dropped"])

    transit_path   = f"logs/transit/rec_transit_{timestamp}.csv"
    transit_f      = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Network latency log  : {network_path}")
    print(f"Decoder latency log  : {decoder_path}")
    print(f"Full latency log     : {full_path}")
    print(f"Transit log          : {transit_path}")
    return (network_f, network_writer), (decoder_f, decoder_writer), (full_f, full_writer), (transit_f, transit_writer)

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

net_in_queues  = [deque() for _ in RTP_PORTS]  # udpsrc → depay
dec_in_queues  = [deque() for _ in RTP_PORTS]  # h264parse → autovideosink
full_in_queues = [deque() for _ in RTP_PORTS]  # udpsrc → autovideosink

# ── Probes ────────────────────────────────────────────────────────────────────

def make_udpsrc_probe(cam_idx):
    """Fires on udpsrc src pad — RTP header available.
    Logs every packet to transit. Pushes timing entry on marked packets
    into both net_in_queue and full_in_queue."""
    _mono          = time.monotonic
    _time          = time.time
    _net_in_queue  = net_in_queues[cam_idx]
    _full_in_queue = full_in_queues[cam_idx]

    def probe_cb(pad, info):
        #print(f"udpsrc probe fired for cam{cam_idx}")
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return Gst.PadProbeReturn.OK
        #print(f"udpsrc probe fired for cam{cam_idx}, seq={seq}, marker={marker}")
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))
        if marker:
            t = _mono()
            _net_in_queue.append((t, seq))
            _full_in_queue.append(t)
        return Gst.PadProbeReturn.OK
    return probe_cb


def make_depay_probe(cam_idx):
    """Fires on depay src pad.
    On first fire clears all deques to discard stale startup timestamps.
    Afterwards drains net_in_queue and logs network_ms + dropped."""
    _mono          = time.monotonic
    _net_in_queue  = net_in_queues[cam_idx]
    _dec_in_queue  = dec_in_queues[cam_idx]
    _full_in_queue = full_in_queues[cam_idx]
    initialized    = [False]
    dropped        = [0]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None or not _net_in_queue:
            return Gst.PadProbeReturn.OK
        if not initialized[0]:
            _net_in_queue.clear()
            _dec_in_queue.clear()
            _full_in_queue.clear()
            initialized[0] = True
            return Gst.PadProbeReturn.OK
        while len(_net_in_queue) > 1:
            _net_in_queue.popleft()
            dropped[0] += 1
        t_start, seq = _net_in_queue.popleft()
        network_ms = (_mono() - t_start) * 1000
        network_queue.append((
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            cam_idx,
            seq,
            f"{network_ms:.4f}",
            dropped[0],
        ))
        return Gst.PadProbeReturn.OK
    return probe_cb


def make_parse_probe(cam_idx):
    """Fires on h264parse src pad — one buffer per complete NAL unit.
    Pushes a timing entry into dec_in_queue."""
    _mono         = time.monotonic
    _dec_in_queue = dec_in_queues[cam_idx]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        _dec_in_queue.append(_mono())
        return Gst.PadProbeReturn.OK
    return probe_cb


def make_sink_probe(cam_idx):
    """Fires on autovideosink sink pad.
    Drains dec_in_queue and full_in_queue, logging dropped counts
    and latency for both decoder and full receiver measurements."""
    _mono          = time.monotonic
    _dec_in_queue  = dec_in_queues[cam_idx]
    _full_in_queue = full_in_queues[cam_idx]

    def process_buf():
        t_now = _mono()
        now   = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if _dec_in_queue:
            dropped = 0
            while len(_dec_in_queue) > 1:
                dropped += 1
                _dec_in_queue.popleft()
            decoder_ms = (t_now - _dec_in_queue.popleft()) * 1000
            decoder_queue.append((now, cam_idx, f"{decoder_ms:.4f}", dropped))

        if _full_in_queue:
            dropped = 0
            while len(_full_in_queue) > 1:
                dropped += 1
                _full_in_queue.popleft()
            full_ms = (t_now - _full_in_queue.popleft()) * 1000
            full_queue.append((now, cam_idx, f"{full_ms:.4f}", dropped))

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
            Gst.PadProbeType.BUFFER, make_udpsrc_probe(i)
        )
        pipeline.get_by_name(f"depay{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_depay_probe(i)
        )
        pipeline.get_by_name(f"parse{i}").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, make_parse_probe(i)
        )
        pipeline.get_by_name(f"sink{i}").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_sink_probe(i)
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    (network_file, network_writer), (decoder_file, decoder_writer), (full_file, full_writer), (transit_file, transit_writer) = open_csv_logs()
    start_writer_thread(network_writer, decoder_writer, full_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started — logging network, decoder, full, and transit latency.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        time.sleep(0.1)
        network_file.flush()
        decoder_file.flush()
        full_file.flush()
        transit_file.flush()
        network_file.close()
        decoder_file.close()
        full_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
