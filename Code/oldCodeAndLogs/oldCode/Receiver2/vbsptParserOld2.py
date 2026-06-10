import datetime
import serial
import threading
import time
import ctypes
import sysv_ipc

# ─────────────────────────────────────────────────────────────────
# Chrony SHM structure - exact memory layout chrony expects
# ─────────────────────────────────────────────────────────────────
class ChronyShm(ctypes.Structure):
    _fields_ = [
        ("mode",          ctypes.c_int),
        ("count",         ctypes.c_int),
        ("clock_sec",     ctypes.c_uint),   # GPS time - whole seconds (UTC Unix)
        ("clock_usec",    ctypes.c_uint),   # GPS time - microseconds
        ("receive_sec",   ctypes.c_uint),   # System time when we got it
        ("receive_usec",  ctypes.c_uint),   # System time microseconds
        ("leap",          ctypes.c_int),
        ("precision",     ctypes.c_int),
        ("nsamples",      ctypes.c_int),
        ("valid",         ctypes.c_int),
    ]

# ─────────────────────────────────────────────────────────────────
# Attach to chrony shared memory
# NOTE: Chrony must be started before this script
# ─────────────────────────────────────────────────────────────────
def open_shm():
    key = 0x4e545030
    return sysv_ipc.SharedMemory(key)

# ─────────────────────────────────────────────────────────────────
# Write GPS time to chrony shared memory
# Uses double-increment pattern so chrony never reads half-written data
# Throttled to once per second - chrony does not need more than that
# ─────────────────────────────────────────────────────────────────
_last_write = 0

def write_to_chrony(shm, gps_unix: float):
    global _last_write
    now = time.time()
    if now - _last_write < 1.0:
        return
    _last_write = now

    gps_sec  = int(gps_unix)
    gps_usec = int((gps_unix - gps_sec) * 1e6)
    now_sec  = int(now)
    now_usec = int((now - now_sec) * 1e6)

    current   = ChronyShm.from_buffer_copy(shm.read(ctypes.sizeof(ChronyShm)))
    new_count = current.count + 1

    # First write: signal update in progress (valid=0)
    data        = ChronyShm()
    data.count  = new_count
    data.valid  = 0
    shm.write(ctypes.string_at(ctypes.addressof(data), ctypes.sizeof(data)))

    # Second write: actual GPS data (valid=1, count incremented again)
    data.count        = new_count + 1
    data.mode         = 0
    data.clock_sec    = gps_sec
    data.clock_usec   = gps_usec
    data.receive_sec  = now_sec
    data.receive_usec = now_usec
    data.leap         = 0
    data.precision    = -1
    data.nsamples     = 3
    data.valid        = 1
    shm.write(ctypes.string_at(ctypes.addressof(data), ctypes.sizeof(data)))

    print(f"Fed to chrony: {gps_unix:.3f}  system: {now:.3f}  diff: {gps_unix - now:.3f}s")

# ─────────────────────────────────────────────────────────────────
# VBSP parser
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

    def has_fix(self):
        with self._lock:
            return self._fixed

    def get_time_ns(self):
        """
        Returns GPS time as a full UTC Unix timestamp in seconds (float).
        Interpolated using monotonic clock since last tick.
        Returns None if no fix yet.
        """
        with self._lock:
            if self._last_gps_ticks is None:
                return None
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
# Configuration
# ─────────────────────────────────────────────────────────────────
VBOX_PORT = "/dev/ttyACM0"
VBOX_BAUD = 115200

# ─────────────────────────────────────────────────────────────────
# Main - start order matters:
#   1. sudo systemctl start chrony   (creates SHM segment)
#   2. sudo python3 vbsptParser.py   (attaches to SHM, feeds GPS time)
# ─────────────────────────────────────────────────────────────────
def main():
    shm  = open_shm()
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)
    vbox.start()

    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")

    print("Feeding GPS time to chrony. Check with: chronyc sources -v")

    while True:
        gps_time = vbox.get_time_ns()
        if gps_time is not None:
            write_to_chrony(shm, gps_time)
        time.sleep(0.1)

if __name__ == "__main__":
    main()
