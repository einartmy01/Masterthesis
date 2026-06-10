#!/usr/bin/env python3
import ctypes
import ctypes.util
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
DISCIPLINE_HZ    = 50.0    # how often to compute and apply correction
STEP_THRESHOLD   = 0.010   # seconds — step clock if offset > 10ms (only at startup after initial set)
MAX_SLEW_PPM     = 500     # max slew rate in ppm (500ppm = 0.5ms/s, kernel hard limit is 500)

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

            elapsed_ns = time.monotonic_ns() - self._last_mono_ns

            # Ticks are in 100ns units → convert to seconds
            gps_tod_seconds = ((self._last_gps_ticks * 10_000_000) + elapsed_ns) / 1_000_000_000

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
# Clock control via ctypes
#
# step_clock  — used once at startup to snap clock to GPS time
# slew_clock  — used continuously; tells kernel to run faster/slower
#               by N ppm to smoothly close the offset gap
# reset_slew  — zeroes the frequency correction on exit
# ─────────────────────────────────────────────────────────────────

class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

class Timex(ctypes.Structure):
    _fields_ = [
        ("modes",     ctypes.c_uint),
        ("offset",    ctypes.c_long),
        ("freq",      ctypes.c_long),   # frequency offset in ppm << 16
        ("maxerror",  ctypes.c_long),
        ("esterror",  ctypes.c_long),
        ("status",    ctypes.c_int),
        ("constant",  ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time",      Timespec),
        ("tick",      ctypes.c_long),
        ("ppsfreq",   ctypes.c_long),
        ("jitter",    ctypes.c_long),
        ("shift",     ctypes.c_int),
        ("stabil",    ctypes.c_long),
        ("jitcnt",    ctypes.c_long),
        ("calcnt",    ctypes.c_long),
        ("errcnt",    ctypes.c_long),
        ("stbcnt",    ctypes.c_long),
        ("tai",       ctypes.c_int),
        ("_pad",      ctypes.c_int * 11),
    ]

_libc          = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_librt         = ctypes.CDLL("librt.so.1",                  use_errno=True)
CLOCK_REALTIME = 0
ADJ_FREQUENCY  = 0x0002

def step_clock(unix_time: float):
    """Snap the system clock to the given Unix timestamp instantly."""
    ts = Timespec()
    ts.tv_sec  = int(unix_time)
    ts.tv_nsec = int((unix_time % 1) * 1_000_000_000)
    ret = _librt.clock_settime(CLOCK_REALTIME, ctypes.byref(ts))
    if ret != 0:
        raise OSError(ctypes.get_errno(), "clock_settime failed")

def slew_clock(offset_sec: float, interval_sec: float):
    """
    Adjust the kernel clock frequency to slew toward GPS time.

    offset_sec   : GPS - system (positive means system is behind)
    interval_sec : discipline loop interval

    Computes ppm needed to close the offset within one interval,
    clamped to MAX_SLEW_PPM so we never exceed the kernel hard limit.
    """
    ppm_needed  = (offset_sec / interval_sec) * 1_000_000
    ppm_clamped = max(-MAX_SLEW_PPM, min(MAX_SLEW_PPM, ppm_needed))

    tx = Timex()
    tx.modes = ADJ_FREQUENCY
    tx.freq  = int(ppm_clamped * 65536)   # kernel uses ppm << 16
    ret = _libc.adjtimex(ctypes.byref(tx))
    if ret < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return ppm_clamped

def reset_slew():
    """Zero the frequency correction — called on exit."""
    tx = Timex()
    tx.modes = ADJ_FREQUENCY
    tx.freq  = 0
    _libc.adjtimex(ctypes.byref(tx))


# ─────────────────────────────────────────────────────────────────
# Initial clock set — disables NTP, steps to GPS time
# ─────────────────────────────────────────────────────────────────
def initial_clock_set(gps_unix: float):
    subprocess.run(['timedatectl', 'set-ntp', 'false'], check=True)
    time.sleep(0.3)
    step_clock(gps_unix)
    dt = datetime.datetime.fromtimestamp(gps_unix, datetime.UTC)
    print(f"Clock stepped to GPS time: {dt.strftime('%Y-%m-%d %H:%M:%S.%f')} UTC")


# ─────────────────────────────────────────────────────────────────
# Main — continuous discipline loop using slewing
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

    # Initial step to GPS time
    gps_time    = vbox.get_unix_time()
    system_time = time.time()
    print(f"\nGPS time:    {gps_time:.6f}")
    print(f"System time: {system_time:.6f}")
    print(f"Difference:  {gps_time - system_time:.6f}s")
    initial_clock_set(gps_time)
    print("\nEntering slew discipline loop. Press Ctrl+C to stop.\n")

    interval = 1.0 / DISCIPLINE_HZ

    try:
        while True:
            loop_start = time.monotonic()

            gps_time    = vbox.get_unix_time()
            system_time = time.time()

            if gps_time is None:
                print("WARNING: lost GPS fix — holding last slew rate")
            else:
                offset = gps_time - system_time

                if abs(offset) > STEP_THRESHOLD:
                    # Large offset — step once to get back in range, then resume slewing
                    step_clock(gps_time)
                    ppm = 0.0
                    tag = f"→ STEPPED (offset was {offset*1000:+.3f}ms)"
                else:
                    ppm = slew_clock(offset, interval)
                    tag = f"→ slewing {ppm:+.1f}ppm"

                print(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                      f"offset={offset*1000:+.4f}ms  {tag}")

            # Sleep for remainder of interval
            elapsed = time.monotonic() - loop_start
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("\nStopping — zeroing slew and re-enabling NTP.")
        reset_slew()
        subprocess.run(['timedatectl', 'set-ntp', 'true'], check=False)
        vbox.stop()
        print("Done.")


if __name__ == "__main__":
    main()
