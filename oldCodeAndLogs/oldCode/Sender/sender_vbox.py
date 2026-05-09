#!/usr/bin/env python3
"""
sender_vbox.py
--------------
Sender using VBOX Sport GPS timestamps.
Also sends a UDP sync packet per frame to the receiver so it can match frames.
Run this when a VBOX is connected via USB.
"""

import time
import socket
import struct
import serial
import threading
import Code.oldCode.Sender.sender_gst as sender_gst

VBOX_PORT   = "/dev/ttyACM0"
VBOX_BAUD   = 115200
SYNC_PORT   = 5001
RECEIVER_IP = sender_gst.RECEIVER_IP


class VBOXTimeSource:
    SEPARATOR = b'\r\n'

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self._lock           = threading.Lock()
        self._last_gps_ticks = None
        self._last_mono_ns   = None
        self._stop           = threading.Event()
        self._fixed          = False

    def start(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name='vbox').start()
        print(f"VBOX reader started on {self.port}")

    def stop(self):
        self._stop.set()
        self._ser.close()

    def get_time_ns(self):
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
                buf   = buf[idx + len(self.SEPARATOR):]
                if len(frame) < 20: continue
                c1 = frame.find(b',')
                if c1 == -1: continue
                c2 = frame.find(b',', c1 + 1)
                if c2 == -1: continue
                pos = c2 + 1
                if pos + 4 > len(frame): continue
                sats  = frame[pos] & 0x7F; pos += 1
                ticks = (frame[pos] << 16) | (frame[pos+1] << 8) | frame[pos+2]
                if ticks == 0: continue
                mono_ns = time.monotonic_ns()
                with self._lock:
                    self._last_gps_ticks = ticks
                    self._last_mono_ns   = mono_ns
                    self._fixed          = sats > 0


def main():
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")

    sync_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def on_frame(cam_idx, pts, time_ns):
        # Pack: 1 byte cam_idx + 8 bytes pts + 8 bytes gps_time_ns = 17 bytes
        packet = struct.pack('>BQQ', cam_idx, pts, time_ns)
        sync_sock.sendto(packet, (RECEIVER_IP, SYNC_PORT))

    try:
        sender_gst.run(get_time_ns=vbox.get_time_ns, on_frame=on_frame)
    finally:
        sync_sock.close()
        vbox.stop()


if __name__ == "__main__":
    main()
