#!/bin/bash

CAM_IP="192.168.1.100"
USER="admin"
PASS="NilsNils"
RTSP_PORT="554"

INTERFACE="enp0s31f6"
LOCAL_IP="192.168.1.20"

RECEIVER_IP="192.168.1.10"
RTP_PORT="5000"

echo "Configuring sender network..."

sudo ip addr flush dev $INTERFACE
sudo ip addr add $LOCAL_IP/24 dev $INTERFACE
sudo ip link set $INTERFACE up

echo "Checking camera reachability..."
ping -c 2 $CAM_IP > /dev/null || { echo "Camera not reachable."; exit 1; }

echo "Checking RTSP port..."
nc -z -w 3 $CAM_IP $RTSP_PORT || { echo "RTSP port not reachable."; exit 1; }

echo "Starting RTSP -> RTP forward stream..."

gst-launch-1.0 \
rtspsrc location="rtsp://$USER:$PASS@$CAM_IP:$RTSP_PORT/Streaming/Channels/101" protocols=tcp latency=0 ! \
rtph264depay ! \
rtph264pay pt=96 config-interval=1 ! \
udpsink host=$RECEIVER_IP port=$RTP_PORT sync=false async=false