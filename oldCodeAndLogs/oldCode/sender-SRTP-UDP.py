#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ── Config ────────────────────────────────────────────────────────────────────
CAM_IPs     = ["192.168.0.100", "192.168.1.101", "192.168.2.102"]
USER        = "admin"
PASS        = "NilsNils"
RTSP_PORT   = "554"
RECEIVER_IP = "100.92.97.93"
RTP_PORTS   = ["5000", "5002", "5004"]
SRTP_KEY    = "4142434445464748494A4B4C4D4E4F505152535455565758595A31323334"
SRTP_SSRC   = [1111111111, 2222222222, 3333333333]
# ─────────────────────────────────────────────────────────────────────────────

def build_pipeline():
    parts = []
    for i, cam_ip in enumerate(CAM_IPs):
        parts.append(
            f'rtspsrc location="rtsp://{USER}:{PASS}@{cam_ip}:{RTSP_PORT}/Streaming/Channels/101" '
            f'protocols=tcp latency=0 ! '
            f'rtph264depay ! '
            f'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'rtph264pay config-interval=1 pt=96 ! '
            f'application/x-rtp,payload=(int)96,ssrc=(uint){SRTP_SSRC[i]} ! '
            f'srtpenc key="{SRTP_KEY}" rtp-cipher=aes-128-icm rtp-auth=hmac-sha1-80 '
            f'rtcp-cipher=aes-128-icm rtcp-auth=hmac-sha1-80 ! '
            f'udpsink host={RECEIVER_IP} port={RTP_PORTS[i]} sync=false async=false'
        )
    return " ".join(parts)

def main():
    Gst.init(None)
    pipeline = Gst.parse_launch(build_pipeline())
    pipeline.set_state(Gst.State.PLAYING)
    print("Sender running. Press Ctrl+C to stop.")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()
