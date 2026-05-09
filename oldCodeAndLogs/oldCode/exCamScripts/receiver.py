#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time
import csv
import os
import sys

RTP_PORT  = "5000"
LOG_FILE  = "logs/receiver_timestamps.csv"

def main():
    os.makedirs("logs", exist_ok=True)
    Gst.init(None)

    pipeline_str = (
        f'udpsrc port={RTP_PORT} '
        f'caps="application/x-rtp, media=video, encoding-name=H264, payload=96" name=src ! '
        f'rtph264depay name=depay ! '
        f'h264parse name=parse ! '
        f'avdec_h264 name=decoder ! '
        f'autovideosink sync=false name=sink'
    )

    pipeline = Gst.parse_launch(pipeline_str)

    # Open CSV
    csv_file = open(LOG_FILE, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["wall_time_ns", "gst_buffer_pts_ns", "stage"])

    def on_buffer(pad, info, stage):
        buf = info.get_buffer()
        wall_ns = time.time_ns()
        pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else -1
        writer.writerow([wall_ns, pts, stage])
        csv_file.flush()
        return Gst.PadProbeReturn.OK

    # Probe 1: right after UDP receive (pre-decode)
    depay = pipeline.get_by_name("depay")
    depay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_depay")

    # Probe 2: after decoding (pre-display)
    decoder = pipeline.get_by_name("decoder")
    decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, "post_decode")

    # Start pipeline
    pipeline.set_state(Gst.State.PLAYING)
    print(f"Receiver running. Logging timestamps to {LOG_FILE}")
    print("Press Ctrl+C to stop.")

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)
        csv_file.close()
        print(f"Timestamps saved to {LOG_FILE}")

if __name__ == "__main__":
    main()
