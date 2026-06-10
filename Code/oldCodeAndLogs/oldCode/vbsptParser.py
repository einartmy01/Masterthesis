import datetime
import serial
import threading
import subprocess
import time

# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────
VBOX_PORT = "/dev/ttyACM0"
VBOX_BAUD = 115200

# ─────────────────────────────────────────────────────────────────
# VBSP Parser
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
# Set system clock directly from GPS time
# Called once after fix is acquired
# ─────────────────────────────────────────────────────────────────
def set_system_clock(gps_unix: float):
    #dt       = datetime.datetime.utcfromtimestamp(gps_unix)
    dt       = datetime.datetime.fromtimestamp(gps_unix, datetime.UTC)
    time_str = dt.strftime('%Y-%m-%d %H:%M:%S.%f')

    # Temporarily disable NTP so we can set the clock manually
    subprocess.run(['timedatectl', 'set-ntp', 'false'], check=True)
    time.sleep(0.5)

    # Set the clock to GPS time
    subprocess.run(['date', '-u', '-s', time_str], check=True)
    print(f"System clock set to GPS time: {time_str} UTC")

    # Re-enable NTP so chrony keeps it disciplined going forward
    subprocess.run(['timedatectl', 'set-ntp', 'true'], check=True)
    print("NTP re-enabled — chrony will now maintain the clock")


# ─────────────────────────────────────────────────────────────────
# Main
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

    # Give it 2 seconds to stabilize
    time.sleep(2)

    # Get GPS time and show comparison
    gps_time    = vbox.get_unix_time()
    system_time = time.time()

    if gps_time is None:
        print("ERROR: Could not get GPS time after fix. Exiting.")
        return

    print(f"\nGPS time:    {gps_time:.3f}")
    print(f"System time: {system_time:.3f}")
    print(f"Difference:  {gps_time - system_time:.3f}s")

    # Set system clock to GPS time
    set_system_clock(gps_time)

    print("\nDone. System clock is now set from GPS.")
    print("NTP (chrony) will keep it accurate going forward.")
    print("Verify with: chronyc tracking")

    vbox.stop()


if __name__ == "__main__":
    main()
