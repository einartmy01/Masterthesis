#!/bin/bash
GST_TRACERS="latency" \
GST_DEBUG="GST_TRACER:7" \
gst-launch-1.0 \
udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96" ! \
rtph264depay ! \
avdec_h264 ! \
videoconvert ! \
autovideosink sync=false \
2>&1 | grep "time=(guint64)" > logs/receiver_log.txt
