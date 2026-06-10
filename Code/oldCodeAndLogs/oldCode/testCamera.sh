#!/bin/bash

CAM_IP="192.168.1.100"
USER="admin"
PASS="NilsNils"
RTSP_PORT="554"
INTERFACE="enx00e04c681e86"

echo "Configuring network..."

sudo ip addr flush dev $INTERFACE
sudo ip addr add 192.168.1.10/24 dev $INTERFACE
sudo ip link set $INTERFACE up

echo "Checking camera reachability..."
ping -c 2 $CAM_IP > /dev/null
if [ $? -ne 0 ]; then
    echo "Camera not reachable."
    exit 1
fi

echo "Checking RTSP port..."
nc -z -w 3 $CAM_IP $RTSP_PORT
if [ $? -ne 0 ]; then
    echo "RTSP port closed or not reachable."
    exit 1
fi

echo "Starting video stream..."

gst-launch-1.0 rtspsrc location="rtsp://$USER:$PASS@$CAM_IP:$RTSP_PORT/Streaming/Channels/101" \
protocols=tcp latency=0 ! \
rtph264depay ! decodebin ! autovideosink sync=false