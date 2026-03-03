#!/bin/bash

INTERFACE="enx00e04c681e86"
LOCAL_IP="192.168.1.10"
RTP_PORT="5000"

echo "Configuring receiver network..."

sudo ip addr flush dev $INTERFACE
sudo ip addr add $LOCAL_IP/24 dev $INTERFACE
sudo ip link set $INTERFACE up

echo "Starting RTP receiver..."

gst-launch-1.0 \
udpsrc port=$RTP_PORT caps="application/x-rtp, media=video, encoding-name=H264, payload=96" ! \
rtph264depay ! \
h264parse ! \
avdec_h264 ! \
autovideosink sync=false