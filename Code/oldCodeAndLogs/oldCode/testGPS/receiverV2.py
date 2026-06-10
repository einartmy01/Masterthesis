#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import sys
import serial
import struct
import threading
import socket

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORT   = "5000"
SYNC_PORT  = 5001           # UDP port for receiving sync packets from sender
LOG_FILE   = "logs/receiver_timestamps.csv"
VBOX_PORT  = "/dev/ttyACM0"
VBOX_BAUD  = 115200
# ─────────────────────────────────────────────────────────────────────────────


# ── VBOX GPS time source ──────────────────────────────────────────────────────

class VBOXTimeSource:
    SEPARATOR = b'\r\n'

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()
        self._last_gps_ticks = None
        self._last_mono_ns   = None
        self._stop = threading.Event()
        self._fixed = False

    def start(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        self._stop.clear()
        t = threading.Thread(target=self._run, daemon=True, name='vbox')
        t.start()
        print(f"VBOX reader started on {self.port}")

    def stop(self):
        self._stop.set()
        self._ser.close()

    def get_gps_time_ns(self):
        with self._lock:
            if self._last_gps_ticks is None:
                return None
            elapsed_ns = time.monotonic_ns() - self._last_mono_ns
            return (self._last_gps_ticks * 10_000_000) + elapsed_ns

    def has_fix(self):
        with self._lock:
            return self._fixed

    def _run(self):
        buf = b''
        while not self._stop.is_set():
            try:
                buf += self._ser.read(256)
            except serial.SerialException as e:
                print(f"VBOX serial error: {e}"); break

            while True:
                idx = buf.find(self.SEPARATOR)
                if idx == -1: break
                frame = buf[:idx]
                buf = buf[idx + len(self.SEPARATOR):]
                if len(frame) < 20: continue
                c1 = frame.find(b',')
                if c1 == -1: continue
                c2 = frame.find(b',', c1 + 1)
                if c2 == -1: continue
                pos = c2 + 1
                if pos + 4 > len(frame): continue
                sats  = frame[pos] & 0x7F;  pos += 1
                ticks = (frame[pos] << 16) | (frame[pos+1] << 8) | frame[pos+2]
                if ticks == 0: continue
                mono_ns = time.monotonic_ns()
                with self._lock:
                    self._last_gps_ticks = ticks
                    self._last_mono_ns   = mono_ns
                    self._fixed          = sats > 0


# ── Sync packet listener ──────────────────────────────────────────────────────

class SyncListener:
    """
    Listens for UDP sync packets from the sender.
    Each packet contains: sender_pts (8 bytes) + sender_gps_time_ns (8 bytes).
    Stores the latest sender GPS time per PTS so we can look it up when
    the corresponding video frame arrives.
    """

    def __init__(self, port):
        self.port = port
        self._lock = threading.Lock()
        self._table = {}   # {sender_pts: sender_gps_ns}
        self._stop = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('', self.port))
        self._sock.settimeout(1.0)
        t = threading.Thread(target=self._run, daemon=True, name='sync-listener')
        t.start()
        print(f"Sync listener started on UDP port {self.port}")

    def stop(self):
        self._stop.set()
        self._sock.close()

    def get_sender_gps_ns(self, sender_pts):
        with self._lock:
            return self._table.get(sender_pts)

    def _run(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(16)
                if len(data) == 16:
                    pts, gps_ns = struct.unpack('>QQ', data)
                    with self._lock:
                        self._table[pts] = gps_ns
                        # Keep table size bounded — drop old entries
                        if len(self._table) > 1000:
                            oldest = sorted(self._table.keys())[:200]
                            for k in oldest:
                                del self._table[k]
            except socket.timeout:
                continue
            except Exception:
                break


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")

    sync = SyncListener(SYNC_PORT)
    sync.start()

    os.makedirs("logs", exist_ok=True)
    Gst.init(None)

    pipeline_str = (
        f'udpsrc port={RTP_PORT} '
        f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" name=src ! '
        f'rtph264depay name=depay ! '
        f'h264parse name=parse ! '
        f'avdec_h264 name=decoder ! '
        f'autovideosink sync=false name=sink'
    )

    pipeline = Gst.parse_launch(pipeline_str)

    csv_file = open(LOG_FILE, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["sender_pts", "sender_gps_ns", "receiver_gps_ns", "latency_ms", "stage"])

    def on_buffer(pad, info, stage):
        buf        = info.get_buffer()
        recv_gps   = vbox.get_gps_time_ns()
        recv_pts   = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1

        if recv_gps is None:
            return Gst.PadProbeReturn.OK

        # The receiver PTS is not reliable — we need to match via the sync table.
        # The sync table is keyed by sender_pts. Since we can't map receiver_pts
        # to sender_pts directly, we use the most recently received sync entry
        # whose GPS time is just before this frame's arrival time.
        # Find the closest sender frame sent just before this frame arrived.
        with sync._lock:
            candidates = {
                pts: gps for pts, gps in sync._table.items()
                if gps <= recv_gps  # sender must have sent before receiver got it
            }

        if not candidates:
            return Gst.PadProbeReturn.OK

        # Pick the sender frame sent closest to (but before) this arrival
        sender_pts = max(candidates, key=lambda p: candidates[p])
        sender_gps = candidates[sender_pts]
        latency_ms = (recv_gps - sender_gps) / 1_000_000

        writer.writerow([sender_pts, sender_gps, recv_gps, f"{latency_ms:.3f}", stage])
        csv_file.flush()

        # Remove used entry so it won't match again
        with sync._lock:
            sync._table.pop(sender_pts, None)

        return Gst.PadProbeReturn.OK

    # Probe: right after UDP receive
    depay = pipeline.get_by_name("depay")
    depay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_depay")

    # Probe: after decoding
    decoder = pipeline.get_by_name("decoder")
    decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_decode")

    pipeline.set_state(Gst.State.PLAYING)
    print(f"Receiver running. Logging to {LOG_FILE}")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        sync.stop()
        vbox.stop()
        print(f"Timestamps saved to {LOG_FILE}")

if __name__ == "__main__":
    main()
