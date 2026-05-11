#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

CAM_IP0   = "192.168.0.100"
CAM_IP1   = "192.168.1.101"
CAM_IP2   = "192.168.2.102"
CAM_IPs   = [CAM_IP0, CAM_IP1, CAM_IP2]
USER      = "admin"
PASS      = "NilsNils"
RTSP_PORT = "554"
RTP_PORTS = ["5000", "5002", "5004"]

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 name=src{i} ! '
            f'rtph264depay name=depay{i} ! '
            f'rtph264pay config-interval=1 pt=96 name=pay{i} ! '
            f'rtpstreampay ! '
            f'tcpserversink host=0.0.0.0 port={RTP_PORTS[i]} sync=false async=false name=udpsink{i}'
        )
    return " ".join(parts)

def main():
    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running on ports 5000, 5002, 5004")
    print("Press Ctrl+C to stop.")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()