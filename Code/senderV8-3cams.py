#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import csv
import struct
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
# ─────────────────────────────────────────────────────────────────────────────

# ── Safe RTP sequence number reader ──────────────────────────────────────────
#
# The RTP fixed header is 12 bytes:
#   Byte 0:   V(2) P(1) X(1) CC(4)
#   Byte 1:   M(1) PT(7)
#   Bytes 2-3: Sequence Number  <-- what we want, big-endian uint16
#   Bytes 4-7: Timestamp
#   Bytes 8-11: SSRC
#
# We read raw bytes directly with Gst.Buffer.map() — no GstRtp bindings needed.
# This is safe because Gst.Buffer.map/unmap is stable in the Python GI layer.

def read_rtp_seq(buf):
    """
    Extracts the RTP sequence number by reading raw buffer bytes.
    Returns None if the buffer is too small or mapping fails.
    Never raises — all errors return None silently.
    """
    info = None
    try:
        success, info = buf.map(Gst.MapFlags.READ)
        if not success or info is None:
            return None
        data = info.data
        if len(data) < 4:
            return None
        # Bytes 2-3 are the sequence number, big-endian
        seq = struct.unpack_from('!H', data, 2)[0]
        return seq
    except Exception:
        return None
    finally:
        if info is not None:
            try:
                buf.unmap(info)
            except Exception:
                pass

# ── CSV logging setup ─────────────────────────────────────────────────────────

def open_csv_logs():
    os.makedirs("logs", exist_ok=True)
    os.makedirs("logs/pipeline", exist_ok=True)
    os.makedirs("logs/pipeline/sender", exist_ok=True)
    os.makedirs("logs/transit", exist_ok=True)

    timestamp = datetime.now().strftime("%d.%m-%H:%M")

    pipeline_path = f"logs/pipeline/sender/sender_pipeline_latency_{timestamp}.csv"
    pipeline_f = open(pipeline_path, "w", newline="")
    pipeline_writer = csv.writer(pipeline_f)
    pipeline_writer.writerow(["wall_time", "cam_index", "latency_ms"])

    transit_path = f"logs/transit/sender_transit_{timestamp}.csv"
    transit_f = open(transit_path, "w", newline="")
    transit_writer = csv.writer(transit_f)
    transit_writer.writerow(["abs_time", "cam_index", "rtp_seq"])

    print(f"Pipeline latency log : {pipeline_path}")
    print(f"Transit log          : {transit_path}")
    return (pipeline_f, pipeline_writer), (transit_f, transit_writer)



# ── Network setup ─────────────────────────────────────────────────────────────

def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", f"{LOCAL_IPS[i]}", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)

def check_cameras():
    print("Checking camera reachability...")
    for i, cam_ip in enumerate(CAM_IPs):
        ping = subprocess.run(["ping", "-c", "2", cam_ip], capture_output=True) #c and 2 is capping at 2 pings
        if ping.returncode != 0:
            print(f"Camera at {cam_ip} not reachable."); sys.exit(1)
        rtsp = subprocess.run(["nc", "-z", "-w", "3", cam_ip, RTSP_PORT], capture_output=True)#Check if port is open
        if rtsp.returncode != 0:
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

# ── Latency Probe setup ─────────────────────────────────────────────────────────────

entry_times = {}

def make_entry_probe(cam_idx):
    """Records monotonic time when a buffer enters rtph264depay."""
    def probe_cb():
        entry_times[cam_idx] = time.monotonic()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_pipeline_exit_probe(cam_idx, pipeline_writer, pipeline_file):
    """Fires on depay src pad — logs depay processing latency per frame."""
    def probe_cb():
        t_entry = entry_times.get(cam_idx)
        if t_entry is not None:
            latency_ms = (time.monotonic() - t_entry) * 1000
            wall_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            pipeline_writer.writerow([wall_time, cam_idx, f"{latency_ms:.4f}"])
            pipeline_file.flush()
        return Gst.PadProbeReturn.OK
    return probe_cb

def make_transit_probe(cam_idx, transit_writer, transit_file):
    def process_buf(buf):
        seq = read_rtp_seq(buf)
        if seq is not None:
            abs_time = time.time()
            transit_writer.writerow([f"{abs_time:.6f}", cam_idx, seq])
            transit_file.flush()

    def probe_cb(info):
        # Try single buffer first
        buf = info.get_buffer()
        if buf is not None:
            process_buf(buf)
            return Gst.PadProbeReturn.OK

        # Fall through to buffer list
        buf_list = info.get_buffer_list()
        if buf_list is not None:
            for j in range(buf_list.length()):
                buf = buf_list.get(j)
                if buf is not None:
                    process_buf(buf)

        return Gst.PadProbeReturn.OK
    return probe_cb

def attach_probes(pipeline, pipeline_writer, pipeline_file, transit_writer, transit_file):
    for i in range(len(CAM_IPs)):
        # Pipeline latency: depay sink → depay src (clean 1-to-1, no packet splitting)
        depay = pipeline.get_by_name(f"depay{i}")
        if depay:
            sink_pad = depay.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(Gst.PadProbeType.BUFFER, make_entry_probe(i))
            else:
                print(f"[WARN] Could not get sink pad for depay{i}")

            src_pad = depay.get_static_pad("src")
            if src_pad:
                src_pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    make_pipeline_exit_probe(i, pipeline_writer, pipeline_file)
                )
            else:
                print(f"[WARN] Could not get src pad for depay{i}")
        else:
            print(f"[WARN] Could not find element depay{i}")

        # Transit: pay src pad — fires per RTP packet, matches receiver UDP packet count
        # Transit: udpsink sink pad with BUFFER_LIST support
        udpsink = pipeline.get_by_name(f"udpsink{i}")
        if udpsink:
            sink_pad = udpsink.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(
                    Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                    make_transit_probe(i, transit_writer, transit_file)
                )
            else:
                print(f"[WARN] Could not get sink pad for udpsink{i}")
        else:
            print(f"[WARN] Could not find element udpsink{i}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_network()
    check_cameras()

    (pipeline_file, pipeline_writer), (transit_file, transit_writer) = open_csv_logs()

    Gst.init(None)
    pipeline_str = build_pipeline()
    pipeline = Gst.parse_launch(pipeline_str)

    attach_probes(pipeline, pipeline_writer, pipeline_file, transit_writer, transit_file)

    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running — logging pipeline latency and RTP transit timestamps.")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        pipeline_file.close()
        transit_file.close()
        print("CSV logs saved.")

if __name__ == "__main__":
    main()
