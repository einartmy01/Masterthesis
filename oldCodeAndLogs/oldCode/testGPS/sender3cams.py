#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import subprocess
import sys
import serial
import struct
import threading

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
RECEIVER_IP  = "10.238.111.249"
RTP_PORTS    = ["5000", "5002", "5004"]
SYNC_PORT    = 5001          # UDP port for sending frame ID + GPS time to receiver
LOG_FILE     = "logs/sender_timestamps.csv"
VBOX_PORT    = "/dev/ttyACM0"
VBOX_BAUD    = 115200
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
            return (self._last_gps_ticks * 10_000_000) + elapsed_ns  # ticks*10ms + elapsed

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

                # Find second comma to locate start of channel data
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
                    continue  # no fix yet

                mono_ns = time.monotonic_ns()

                with self._lock:
                    self._last_gps_ticks = ticks
                    self._last_mono_ns   = mono_ns
                    self._fixed          = sats > 0


# ── Network setup ─────────────────────────────────────────────────────────────

def setup_network():
    print("Configuring sender network...")
    for i in range(len(CAM_IPs)):
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", f"{LOCAL_IPS[i]}", "dev", INTERFACES[i]], check=True)
        subprocess.run(["sudo", "ip", "link", "set", INTERFACES[i], "up"], check=True)

def check_camera():
    print("Checking camera reachability...")
    for i in range(len(CAM_IPs)):
    #cam_ip = CAM_IP0 # Single cam setup
        ping = subprocess.run(["ping", "-c", "2", CAM_IPs[i]], capture_output=True)
        if ping.returncode != 0:
            print(f"Camera at {CAM_IPs[i]} not reachable."); sys.exit(1)
        rtsp = subprocess.run(["nc", "-z", "-w", "3", CAM_IPs[i], RTSP_PORT], capture_output=True)
        if rtsp.returncode != 0:
            print(f"RTSP port not reachable for camera at {CAM_IPs[i]}."); sys.exit(1)

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'rtph264pay pt=96 config-interval=1 name=pay{i} ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false'
        )
    return " ".join(parts)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    # vbox.start()

    # # Wait for GPS fix before starting stream
    # print("Waiting for GPS fix...", end='', flush=True)
    # while not vbox.has_fix():
    #     time.sleep(0.5)
    #     print('.', end='', flush=True)
    # print(" fix acquired!")

    setup_network()
    check_camera()

    os.makedirs("logs", exist_ok=True)
    Gst.init(None)
    
    pipeline_str = build_pipeline()

    pipeline = Gst.parse_launch(pipeline_str)

    # # Open CSV
    # csv_file = open(LOG_FILE, "w", newline="")
    # writer = csv.writer(csv_file)
    # writer.writerow(["gps_time_ns", "gst_buffer_pts_ns", "stage"])

    # def on_buffer(pad, info, stage):
    #     buf = info.get_buffer()
    #     gps_ns = vbox.get_gps_time_ns()                              # GPS timestamp
    #     pts    = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1   # frame ID
    #     writer.writerow([gps_ns, pts, stage])
    #     csv_file.flush()
    #     return Gst.PadProbeReturn.OK

    # depay = pipeline.get_by_name("depay")
    # depay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_depay")

    pipeline.set_state(Gst.State.PLAYING)
    print(f"Sender running. Logging GPS timestamps to {LOG_FILE}")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        #csv_file.close()
        #vbox.stop()
        print(f"Timestamps saved to {LOG_FILE}")

if __name__ == "__main__":
    main()
