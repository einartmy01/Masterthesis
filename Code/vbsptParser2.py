#!/usr/bin/env python3
import ctypes
import datetime
import serial
import threading
import subprocess
import time

# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────
VBOX_PORT        = "/dev/ttyACM0"
VBOX_BAUD        = 115200
CORRECT_IF_ABOVE = 0.001   # seconds — only correct if offset > 1 ms
DISCIPLINE_HZ    = 10.0     # how often to apply correction (times per second)

# ─────────────────────────────────────────────────────────────────
# VBSPT Parser
# Reads binary frames from VBOX over serial, extracts GPS ticks
# and interpolates to a full UTC Unix timestamp
# ─────────────────────────────────────────────────────────────────
class VBOXTimeSource:
    SEPARATOR = b'\r\n'

    def __init__(self, port, baud):
        self.port            = port
        self.baud            = baud
        self._lock           = threading.Lock()
        self._last_gps_ticks = None
        self._last_mono_ns   = None
        self._fixed          = False
        self._stop           = threading.Event()

    def start(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name='vbox').start()
        print(f"VBOX reader started on {self.port}")

    def stop(self):
        self._stop.set()
        self._ser.close()

    def has_fix(self) -> bool:
        with self._lock:
            return self._fixed

    def get_unix_time(self) -> float | None:
        """
        Returns current GPS time as a full UTC Unix timestamp in seconds.
        Interpolated using monotonic clock since last received tick.
        Returns None if no fix yet.
        """
        with self._lock:
            if self._last_gps_ticks is None:
                return None

            # Interpolate forward from last known tick using monotonic clock
            elapsed_ns = time.monotonic_ns() - self._last_mono_ns

            # Ticks are in 100ns units → convert to seconds
            gps_tod_seconds = ((self._last_gps_ticks * 10_000_000) + elapsed_ns) / 1_000_000_000

            # Combine with today's UTC midnight to get full Unix timestamp
            today    = datetime.datetime.now(datetime.timezone.utc).date()
            midnight = datetime.datetime.combine(
                today,
                datetime.time(0, 0, 0),
                tzinfo=datetime.timezone.utc
            )
            return midnight.timestamp() + gps_tod_seconds

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
                buf   = buf[idx + len(self.SEPARATOR):]

                if len(frame) < 20:
                    continue

                c1 = frame.find(b',')
                if c1 == -1:
                    continue
                c2 = frame.find(b',', c1 + 1)
                if c2 == -1:
                    continue

                pos = c2 + 1
                if pos + 4 > len(frame):
                    continue

                sats  = frame[pos] & 0x7F
                pos  += 1
                ticks = (frame[pos] << 16) | (frame[pos+1] << 8) | frame[pos+2]

                if ticks == 0:
                    continue

                mono_ns = time.monotonic_ns()
                with self._lock:
                    self._last_gps_ticks = ticks
                    self._last_mono_ns   = mono_ns
                    self._fixed          = sats > 0


# ─────────────────────────────────────────────────────────────────
# Clock setting via ctypes (no subprocess overhead)
# Uses clock_settime(CLOCK_REALTIME) directly
# ─────────────────────────────────────────────────────────────────
class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

_librt         = ctypes.CDLL("librt.so.1", use_errno=True)
CLOCK_REALTIME = 0

def set_clock_realtime(unix_time: float):
    ts = Timespec()
    ts.tv_sec  = int(unix_time)
    ts.tv_nsec = int((unix_time % 1) * 1_000_000_000)
    ret = _librt.clock_settime(CLOCK_REALTIME, ctypes.byref(ts))
    if ret != 0:
        raise OSError(ctypes.get_errno(), "clock_settime failed")


# ─────────────────────────────────────────────────────────────────
# Initial clock set (used once on startup, disables NTP first)
# ─────────────────────────────────────────────────────────────────
def initial_clock_set(gps_unix: float):
    subprocess.run(['timedatectl', 'set-ntp', 'false'], check=True)
    time.sleep(0.3)
    set_clock_realtime(gps_unix)
    dt = datetime.datetime.fromtimestamp(gps_unix, datetime.UTC)
    print(f"Clock set to GPS time: {dt.strftime('%Y-%m-%d %H:%M:%S.%f')} UTC")


# ─────────────────────────────────────────────────────────────────
# Main — continuous discipline loop
# ─────────────────────────────────────────────────────────────────
def main():
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    # Wait for GPS fix
    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")

    # Stabilise for 2 seconds
    time.sleep(2)

    # Initial set
    gps_time    = vbox.get_unix_time()
    system_time = time.time()
    print(f"\nGPS time:    {gps_time:.6f}")
    print(f"System time: {system_time:.6f}")
    print(f"Difference:  {gps_time - system_time:.6f}s")
    initial_clock_set(gps_time)
    print("\nEntering continuous discipline loop. Press Ctrl+C to stop.\n")

    interval = 1.0 / DISCIPLINE_HZ
    try:
        while True:
            loop_start = time.monotonic()

            gps_time    = vbox.get_unix_time()
            system_time = time.time()

            if gps_time is None:
                print("WARNING: lost GPS fix")
            else:
                offset = gps_time - system_time
                if abs(offset) > CORRECT_IF_ABOVE:
                    set_clock_realtime(gps_time)
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
                          f"offset={offset*1000:+.3f}ms  → corrected")
                else:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
                          f"offset={offset*1000:+.3f}ms  ok")

            # Sleep for remainder of interval
            elapsed = time.monotonic() - loop_start
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("\nStopping — re-enabling NTP.")
        subprocess.run(['timedatectl', 'set-ntp', 'true'], check=False)
        vbox.stop()
        print("Done.")


if __name__ == "__main__":
    main()
