import gi
import time
import struct
import csv

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

# ─────────────────────────────────────────
# Log file - one row per frame
# ─────────────────────────────────────────
log = open("latency_log.csv", "w", newline="")
writer = csv.writer(log)
writer.writerow(["frame", "send_time_ns", "arrive_time_ns", "latency_ms"])
frame_count = 0

pipeline = Gst.parse_launch("""
    udpsrc port=5000
        caps="application/x-rtp,media=video,encoding-name=H264,payload=96" !
    rtph264depay !
    fakesink name=sink sync=false
""")

# Note: fakesink means we are NOT decoding the video.
# We grab the timestamp immediately on arrival,
# before any decoding happens.

def on_buffer_receiver(pad, info):
    global frame_count
    buf = info.get_buffer()

    # ─────────────────────────────────────────────────────
    # Read arrival time immediately - this is the
    # GPS-disciplined clock on the receiver machine
    # ─────────────────────────────────────────────────────
    arrive_time_ns = time.time_ns()

    # Extract the sender's timestamp from the buffer meta
    meta = buf.get_custom_meta('send-timestamp')
    if meta is None:
        return Gst.PadProbeReturn.OK

    send_time_ns = meta.structure.get_value('ts')

    # ─────────────────────────────────────────────────────
    # This is your transport latency.
    # Both timestamps come from GPS-disciplined clocks
    # so the subtraction is meaningful.
    # ─────────────────────────────────────────────────────
    latency_ms = (arrive_time_ns - send_time_ns) / 1_000_000

    frame_count += 1
    writer.writerow([frame_count, send_time_ns, arrive_time_ns, latency_ms])
    log.flush()

    print(f"Frame {frame_count:5d} | latency: {latency_ms:.2f} ms")

    return Gst.PadProbeReturn.OK

sink = pipeline.get_by_name("sink")
sinkpad = sink.get_static_pad("sink")
sinkpad.add_probe(Gst.PadProbeType.BUFFER, on_buffer_receiver)

pipeline.set_state(Gst.State.PLAYING)
print("Receiver running...")

try:
    GLib.MainLoop().run()
except KeyboardInterrupt:
    pipeline.set_state(Gst.State.NULL)
    log.close()