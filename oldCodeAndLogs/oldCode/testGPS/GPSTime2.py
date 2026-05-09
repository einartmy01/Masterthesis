import serial
import struct

PORT = '/dev/ttyACM0'
BAUD = 115200
SEPARATOR = b'\r\n'
ser = serial.Serial(PORT, BAUD, timeout=1)
buf = b''
print("Reading frames...\n")

while True:
    buf += ser.read(256)

    while True:
        idx = buf.find(SEPARATOR)
        if idx == -1:
            break

        frame = buf[:idx]
        buf = buf[idx + len(SEPARATOR):]

        if len(frame) < 20:
            continue

        c1 = frame.find(b',')
        if c1 == -1: continue
        c2 = frame.find(b',', c1 + 1)
        if c2 == -1: continue

        pos = c2 + 1

        sats  = frame[pos] & 0x7F; pos += 1
        ticks = (frame[pos] << 16) | (frame[pos+1] << 8) | frame[pos+2]; pos += 3

        if ticks == 0:
            continue  # skip empty frames

        tod_s = ticks * 0.01
        h = int(tod_s // 3600)
        m = int((tod_s % 3600) // 60)
        s = tod_s % 60

        print(f"Sats: {sats:2d}  |  GPS time: {h:02d}:{m:02d}:{s:06.3f} UTC")