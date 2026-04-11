#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import sys
import serial
import threading

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORT   = "5000"
LOG_FILE   = "logs/receiver_timestamps.csv"
VBOX_PORT  = "/dev/ttyACM0"
VBOX_BAUD  = 115200
# ─────────────────────────────────────────────────────────────────────────────


# ── VBOX GPS time source ──────────────────────────────────────────────────────

class VBOXTimeSource:
    """
    Reads VBSPT frames from the VBOX Sport in a background thread.
    Interpolates GPS time between 10 Hz fixes using the host monotonic clock.
    Call get_gps_time_ns() from any thread to get current GPS time in nanoseconds.
    """

    SEPARATOR = b'\r\n'

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()
        self._last_gps_ticks = None   # 10ms ticks since midnight UTC
        self._last_mono_ns   = None   # host monotonic time of last GPS fix (ns)
        self._thread = None
        self._stop = threading.Event()
        self._fixed = False

    def start(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='vbox')
        self._thread.start()
        print(f"VBOX reader started on {self.port}")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._ser.close()

    def get_gps_time_ns(self):
        """
        Returns current GPS time-of-day in nanoseconds.
        Interpolates from last fix using host monotonic clock.
        Returns None if no fix yet.
        """
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
                print(f"VBOX serial error: {e}")
                break

            while True:
                idx = buf.find(self.SEPARATOR)
                if idx == -1:
                    break

                frame = buf[:idx]
                buf = buf[idx + len(self.SEPARATOR):]

                if len(frame) < 20:
                    continue

                c1 = frame.find(b',')
                if c1 == -1: continue
                c2 = frame.find(b',', c1 + 1)
                if c2 == -1: continue

                pos = c2 + 1
                if pos + 4 > len(frame):
                    continue

                sats  = frame[pos] & 0x7F;  pos += 1
                ticks = (frame[pos] << 16) | (frame[pos+1] << 8) | frame[pos+2]

                if ticks == 0:
                    continue

                mono_ns = time.monotonic_ns()

                with self._lock:
                    self._last_gps_ticks = ticks
                    self._last_mono_ns   = mono_ns
                    self._fixed          = sats > 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Start VBOX time source first
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    # Wait for GPS fix before starting
    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")

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

    # Open CSV
    csv_file = open(LOG_FILE, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["gps_time_ns", "gst_buffer_pts_ns", "stage"])

    def on_buffer(pad, info, stage):
        buf    = info.get_buffer()
        gps_ns = vbox.get_gps_time_ns()                              # GPS timestamp
        pts    = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1   # frame ID
        writer.writerow([gps_ns, pts, stage])
        csv_file.flush()
        return Gst.PadProbeReturn.OK

    # Probe 1: right after UDP receive (pre-decode)
    depay = pipeline.get_by_name("depay")
    depay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_depay")

    # Probe 2: after decoding (pre-display)
    decoder = pipeline.get_by_name("decoder")
    decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_decode")

    pipeline.set_state(Gst.State.PLAYING)
    print(f"Receiver running. Logging GPS timestamps to {LOG_FILE}")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        vbox.stop()
        print(f"Timestamps saved to {LOG_FILE}")

if __name__ == "__main__":
    main()
