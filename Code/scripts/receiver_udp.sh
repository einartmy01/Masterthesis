#!/bin/bash

# Starting a gstreamer pipeline
gst-launch-1.0 \
#f
udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96" ! \
rtph264depay ! \
avdec_h264 ! \
videoconvert ! \
autovideosink sync=false
