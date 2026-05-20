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
CAM_IP0        = "192.168.0.100"
CAM_IP1        = "192.168.1.101"
CAM_IP2        = "192.168.3.103"
CAM_IPs        = [CAM_IP0, CAM_IP1, CAM_IP2]
USER           = "admin"
PASS           = "NilsNils"
RTSP_PORT      = "554"
INTERFACES     = ["enx0c3796ba2d67", "enx0c3796ba2d6a", "enp0s31f6"]
LOCAL_IPS      = ["192.168.0.50/24", "192.168.1.50/24", "192.168.3.50/24"]
#RECEIVER_IP   = "172.30.154.249" # Private laptop
#RECEIVER_IP   = "100.92.97.93"  # Thinkpad laptop
RECEIVER_IP    = "100.70.208.109" # DELL laptop
RECEIVER_IFACE = "tailscale0"     # outbound interface to receiver (ip route get 100.70.208.109)
RTP_PORTS      = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

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

# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'h264parse ! '
            f'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'avdec_h264 ! '
            f'videoconvert ! '
            f'x264enc tune=zerolatency bitrate=8000 speed-preset=ultrafast key-int-max=15 threads=0 ! '
            f'h264parse ! '
            f'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'rtph264pay config-interval=1 pt=96 name=pay{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)
# ── Background writer ─────────────────────────────────────────────────────────

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
    thread = threading.Thread(
        target=writer_thread,
        args=(pipeline_writer, transit_writer),
        daemon=True,
    )
    thread.start()

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs(timestamp):
    os.makedirs("logs/pipeline/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    pipeline_path   = f"logs/pipeline/sender/send_pipe_{timestamp}.csv"
    pipeline_file   = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_file)
    pipeline_writer.writerow(["wall_time", "cam", "rtp_seq", "pipeline_ms", "dropped_nals"])

    transit_path   = f"logs/transit/send_transit_{timestamp}.csv"
    transit_file   = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_file)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline log : {pipeline_path}")
    print(f"Transit log  : {transit_path}")
    return (pipeline_file, pipeline_writer), (transit_file, transit_writer)

# ── Throughput logging ────────────────────────────────────────────────────────

def throughput_logger(iface, interval=1.0):
    """Logs outbound (tx) Mbps on the receiver-facing interface only."""
    os.makedirs("logs/throughput", exist_ok=True)
    timestamp = datetime.now().strftime("%d.%m-%H:%M")
    path = f"logs/throughput/sender_throughput_{timestamp}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_time", "iface", "tx_mbps"])
        prev_tx = None
        while True:
            with open("/proc/net/dev") as nd:
                for line in nd:
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].rstrip(":") != iface:
                        continue
                    tx = int(parts[9])
                    if prev_tx is not None:
                        tx_mbps = (tx - prev_tx) * 8 / interval / 1_000_000
                        w.writerow([
                            datetime.now().strftime("%H:%M:%S"),
                            iface,
                            f"{tx_mbps:.3f}",
                        ])
                    prev_tx = tx
                    break
            f.flush()
            time.sleep(interval)

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

t_in_queues = [deque() for _ in CAM_IPs]

# ── Probes ────────────────────────────────────────────────────────────────────

def make_probe_in(cam_idx):
    _mono       = time.monotonic
    _t_in_queue = t_in_queues[cam_idx]

    def probe_cb(pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        _, marker = read_rtp_header(buf)
        if marker:
            _t_in_queue.append(_mono())
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_probe_out(cam_idx):
    _mono       = time.monotonic
    _time       = time.time
    _t_in_queue = t_in_queues[cam_idx]

    def process_buf(buf):
        seq, marker = read_rtp_header(buf)
        if seq is None:
            return

        # Always log to transit for network latency analysis
        transit_queue.append((f"{_time():.6f}", cam_idx, seq))

        # Only log pipeline_ms on marked packets — one row per frame
        if not marker or not _t_in_queue:
            return

        dropped_nals = 0
        while len(_t_in_queue) > 1:
            dropped_nals += 1
            _t_in_queue.popleft()

        t_now       = _mono()
        t_start     = _t_in_queue.popleft()
        pipeline_ms = f"{(t_now - t_start) * 1000:.3f}"
        now         = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        pipeline_queue.append((now, cam_idx, seq, pipeline_ms, dropped_nals))

    def probe_cb(pad, info):
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
        sink  = pipeline.get_by_name(f"udpsink{i}")

        # IN — depay sink (incoming RTP, timestamps on marked/last packets of frame)
        depay.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, make_probe_in(i))

        # OUT — udpsink sink (outgoing RTP, logs pipeline_ms on marked packets)
        sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            make_probe_out(i))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs(timestamp)
    start_writer_thread(pipeline_writer, transit_writer)

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    attach_probes(pipeline)

    # ── CPU logging — pidstat, one line/sec for this process only ────────────
    os.makedirs("logs/cpu", exist_ok=True)
    cpu_log = open(f"logs/cpu/sender_cpu_{timestamp}.log", "w")
    cpu_proc = subprocess.Popen(
        ["sar", "-u", "ALL", "1"], 
        stdout=cpu_log,
        stderr=subprocess.DEVNULL,
    )

    # ── Throughput logging — tx only on tailscale0 (outbound to receiver) ────
    throughput_thread = threading.Thread(
        target=throughput_logger,
        args=(RECEIVER_IFACE, 1.0),
        daemon=True,
    )
    throughput_thread.start()

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running — logging pipeline latency, transit, CPU, and outbound throughput.")
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
        cpu_proc.terminate()
        cpu_proc.wait()
        cpu_log.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
