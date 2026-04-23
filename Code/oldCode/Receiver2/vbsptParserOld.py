import datetime
import serial
import time
import struct
import mmap
import ctypes
import serial
import threading
#cspell:disable
# ─────────────────────────────────────────────
# Chrony SHM segment - this is the exact memory
# structure chrony expects to find
# ─────────────────────────────────────────────

class ChronyShm(ctypes.Structure):
    _fields_ = [
        ("mode",          ctypes.c_int),
        ("count",         ctypes.c_int),
        ("clock_sec",     ctypes.c_uint),   # GPS time - whole seconds
        ("clock_usec",    ctypes.c_uint),   # GPS time - microseconds
        ("receive_sec",   ctypes.c_uint),   # System time when we got it
        ("receive_usec",  ctypes.c_uint),   # System time microseconds
        ("leap",          ctypes.c_int),
        ("precision",     ctypes.c_int),
        ("nsamples",      ctypes.c_int),
        ("valid",         ctypes.c_int),
    ]

def open_shm():
    """Open chrony shared memory segment (unit 0)"""
    import sysv_ipc
    # Chrony SHM key for unit 0 is 0x4e545030
    key = 0x4e545030
    shm = sysv_ipc.SharedMemory(key)
    return shm

last_write = 0

def write_to_chrony(shm, gps_time: float):
    global last_write
    now = time.time()
    if now - last_write < 1.0:
        return
    last_write = now

    now = time.time()

    gps_sec  = int(gps_time)
    gps_usec = int((gps_time - gps_sec) * 1e6)
    now_sec  = int(now)
    now_usec = int((now - now_sec) * 1e6)

    # Read current count
    current = ChronyShm.from_buffer_copy(shm.read(ctypes.sizeof(ChronyShm)))
    new_count = current.count + 1

    # First write - signal that we are about to update (valid=0)
    data = ChronyShm()
    data.count        = new_count
    data.valid        = 0
    shm.write(ctypes.string_at(ctypes.addressof(data), ctypes.sizeof(data)))

    # Second write - write actual data (valid=1, count incremented again)
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

    print(f"Fed to chrony: {gps_time:.6f} (count={new_count + 1})")


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
            gps_tod_seconds = ((self._last_gps_ticks * 10_000_000) + elapsed_ns) / 1_000_000_000

            # Force UTC midnight, not local midnight
            today = datetime.datetime.now(datetime.timezone.utc).date()
            midnight = datetime.datetime.combine(today, datetime.time(0, 0, 0), 
                                                tzinfo=datetime.timezone.utc)
            
            return midnight.timestamp() + gps_tod_seconds
        
    # def get_time_ns(self):
    #     """Returns interpolated GPS time as Unix timestamp in seconds, or None if no fix."""
    #     with self._lock:
    #         if self._last_gps_ticks is None:
    #             return None
    #         elapsed_ns = time.monotonic_ns() - self._last_mono_ns
    #         gps_tod_seconds = ((self._last_gps_ticks * 10_000_000) + elapsed_ns) / 1_000_000_000

    #         today = datetime.datetime.now(datetime.UTC)
    #         midnight = datetime.datetime.combine(today, datetime.time(0, 0, 0))
            
    #         return midnight.timestamp() + gps_tod_seconds
        
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


VBOX_PORT = "/dev/ttyACM0"
VBOX_BAUD = 115200

def main():
    shm = open_shm()
    vbox = VBOXTimeSource(VBOX_PORT, VBOX_BAUD)

    vbox.start()

    print("Waiting for GPS fix...", end='', flush=True)
    while not vbox.has_fix():
        time.sleep(0.5)
        print('.', end='', flush=True)
    print(" fix acquired!")
    
    print("VBSPT parser running, feeding time to chrony...")

    while True:
        gps_time = vbox.get_time_ns()
        
        if gps_time is not None:
            write_to_chrony(shm, gps_time)
            #print(f"Fed to chrony: {gps_time:.6f}")  # Remove in production

if __name__ == "__main__":
    main()