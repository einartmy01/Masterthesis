#!/usr/bin/env python3


import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
RTP_PORTS = ["5000", "5002", "5004"]
# ─────────────────────────────────────────────────────────────────────────────

# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, port in enumerate(RTP_PORTS):
        parts.append(
            f'udpsrc port={port} '
            f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" ! '
            f'rtph264depay ! '
            f'h264parse ! '
            f'avdec_h264 max-threads=1 ! '
            f'queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'xvimagesink sync=false'
        )
    return " ".join(parts)
# ── Main ──────────────────────────────────────────────────────────────────────

def main():

    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())

    pipeline.set_state(Gst.State.PLAYING)
    print("Receiver started - Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
