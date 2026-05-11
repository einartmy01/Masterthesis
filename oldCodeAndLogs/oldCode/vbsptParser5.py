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
VBOX_PORT      = "/dev/ttyACM0"
VBOX_BAUD      = 115200
STEP_THRESHOLD = 0.010   # seconds — step clock if offset > 10ms, otherwise slew
MAX_SLEW_PPM   = 500     # kernel hard limit is 500ppm

# ─────────────────────────────────────────────────────────────────
# Clock control via ctypes
# ─────────────────────────────────────────────────────────────────

class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

class Timex(ctypes.Structure):
    _fields_ = [
        ("modes",     ctypes.c_uint),
        ("offset",    ctypes.c_long),
        ("freq",      ctypes.c_long),
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
    ts = Timespec()
    ts.tv_sec  = int(unix_time)
    ts.tv_nsec = int((unix_time % 1) * 1_000_000_000)
    ret = _librt.clock_settime(CLOCK_REALTIME, ctypes.byref(ts))
    if ret != 0:
        raise OSError(ctypes.get_errno(), "clock_settime failed")

def slew_clock(ppm: float):
    """Tell the kernel to run the clock faster/slower by ppm."""
    tx = Timex()
    tx.modes = ADJ_FREQUENCY
    tx.freq  = int(ppm * 65536)   # kernel uses ppm << 16
    ret = _libc.adjtimex(ctypes.byref(tx))
    if ret < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")

def reset_slew():
    tx = Timex()
    tx.modes = ADJ_FREQUENCY
    tx.freq  = 0
    _libc.adjtimex(ctypes.byref(tx))


# ─────────────────────────────────────────────────────────────────
# VBSPT Parser
#
# Clock discipline happens directly inside the serial thread the
# moment each frame arrives — no separate polling loop, no
# interpolation across long gaps.
#
# On each frame:
#   1. Compute GPS time from ticks + today's UTC midnight
#   2. Compare to system time at that exact moment
#   3. Step if offset > STEP_THRESHOLD, otherwise slew
# ─────────────────────────────────────────────────────────────────
class VBOXTimeSource:
    SEPARATOR = b'\r\n'

    def __init__(self, port, baud):
        self.port    = port
        self.baud    = baud
        self._fixed  = False
        self._stop   = threading.Event()
        self._initialized = False

    def start(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name='vbox').start()
        print(f"VBOX reader started on {self.port}")

    def stop(self):
        self._stop.set()
        self._ser.close()

    def has_fix(self) -> bool:
        return self._fixed

    def _gps_unix(self, ticks: int) -> float:
        """
        Convert raw VBSPT ticks to a full UTC Unix timestamp.
        Ticks are time-of-day in 100ns units.
        """
        gps_tod_seconds = (ticks * 10_000_000) / 1_000_000_000

        today    = datetime.datetime.now(datetime.timezone.utc).date()
        midnight = datetime.datetime.combine(
            today,
            datetime.time(0, 0, 0),
            tzinfo=datetime.timezone.utc
        )
        return midnight.timestamp() + gps_tod_seconds

    def _discipline(self, ticks: int):
        """
        Called immediately when a fresh frame arrives.
        Reads system time at this moment, computes offset, steps or slews.
        """
        gps_time    = self._gps_unix(ticks)
        system_time = time.time()
        offset      = gps_time - system_time

        if not self._initialized:
            # First frame — always step to get into range immediately
            step_clock(gps_time)
            self._initialized = True
            tag = "→ INITIAL STEP"
        elif abs(offset) > STEP_THRESHOLD:
            step_clock(gps_time)
            tag = f"→ STEPPED (offset was {offset*1000:+.3f}ms)"
        else:
            # Slew proportionally to offset — simple P controller
            # Scale: 1ms offset → 500ppm (closes 1ms in ~2 frames at 3Hz)
            ppm = max(-MAX_SLEW_PPM, min(MAX_SLEW_PPM, offset * 1_000 * MAX_SLEW_PPM))
            slew_clock(ppm)
            tag = f"→ slewing {ppm:+.1f}ppm"

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
              f"offset={offset*1000:+.4f}ms  {tag}")

    def _run(self):
        buf      = b''
        prev_mono = None

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

                # Report frame gap for diagnostics
                mono_now = time.monotonic_ns()
                if prev_mono is not None:
                    gap_ms = (mono_now - prev_mono) / 1_000_000
                    if gap_ms > 30:
                        print(f"  [vbox] frame gap: {gap_ms:.1f}ms")
                prev_mono = mono_now

                self._fixed = sats > 0
                if self._fixed:
                    self._discipline(ticks)


# ─────────────────────────────────────────────────────────────────
# Initial setup — disable NTP
# ─────────────────────────────────────────────────────────────────
def disable_ntp():
    subprocess.run(['timedatectl', 'set-ntp', 'false'], check=True)
    time.sleep(0.3)
    print("NTP disabled.")


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────
def main():
    disable_ntp()

    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")
    print("\nDisciplining clock directly on each frame. Press Ctrl+C to stop.\n")

    try:
        # Main thread just keeps alive — all work happens in the serial thread
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping — zeroing slew and re-enabling NTP.")
        reset_slew()
        subprocess.run(['timedatectl', 'set-ntp', 'true'], check=False)
        vbox.stop()
        print("Done.")


if __name__ == "__main__":
    main()
