#!/bin/bash

RTP_PORT="5000"

echo "Configuring receiver network..."
echo "Starting RTP receiver..."

GST_TRACERS="latency" GST_DEBUG="GST_TRACER:7" gst-launch-1.0 \
udpsrc port=$RTP_PORT caps="application/x-rtp, media=video, encoding-name=H264, payload=96" ! \
rtph264depay ! \
h264parse ! \
avdec_h264 ! \
autovideosink sync=false 2> logs/receiverLog.txt