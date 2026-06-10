import gi
import time
import struct

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

pipeline = Gst.parse_launch("""
    v4l2src device=/dev/video0 !
    video/x-raw,width=1280,height=720,framerate=30/1 !
    videoconvert !
    x264enc tune=zerolatency bitrate=2000 !
    rtph264pay name=pay pt=96 !
    udpsink host=RECEIVER_IP port=5000
""")

def on_buffer_sender(pad, info):
    buf = info.get_buffer()

    # ─────────────────────────────────────────────────────
    # This is the GPS-disciplined wall clock time.
    # Because chrony has locked this to your VBOX GPS,
    # this timestamp is directly comparable to the same
    # call on the receiver machine.
    # ─────────────────────────────────────────────────────
    send_time_ns = time.time_ns()

    # Pack the timestamp as 8 bytes (unsigned 64-bit integer)
    timestamp_bytes = struct.pack('>Q', send_time_ns)

    # Attach it to the buffer as a custom meta.
    # GStreamer carries this through the pipeline for us.
    buf = info.get_buffer()
    buf.add_custom_meta('send-timestamp')
    meta = buf.get_custom_meta('send-timestamp')
    meta.structure.set_value('ts', send_time_ns)

    return Gst.PadProbeReturn.OK

pay = pipeline.get_by_name("pay")
srcpad = pay.get_static_pad("src")
srcpad.add_probe(Gst.PadProbeType.BUFFER, on_buffer_sender)

pipeline.set_state(Gst.State.PLAYING)
print("Sender running...")

try:
    GLib.MainLoop().run()
except KeyboardInterrupt:
    pipeline.set_state(Gst.State.NULL)