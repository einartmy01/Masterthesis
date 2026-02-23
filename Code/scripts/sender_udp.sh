#!/bin/bash

#Purpose:
#	Capture video from a V4L2 camera, encode with H.264 using Intel Quick Sync (VAAPI), 
#	packetize into RTP, and transmit over UDP
#Timing model:
#	Timestamp origin: v4l2src (capture time)
#	No clock synchronization or buffering at sink
#Starts a gstreamer pipeline
#Captures video from , including timestamp from kernel clock
#Converts pixel format (VAAPI prefers NV12)
#Activate hardware H.264 encoder. One I-frame per second at 30fps. Bitrate controls stable quaility.
#IDK
#IDK
#Swap internal host=127.0.0.1 

GST_DEBUG="rtpbasepayload:7,rtph264pay:7" \
gst-launch-1.0 \
v4l2src device=/dev/video0 do-timestamp=true ! \
videoconvert ! \
x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 bitrate=4000 bframes= 0 ! \
rtph264pay pt=96 ! \
udpsink host=10.194.81.249 port=5000 sync=false async=false \
2>&1 | tee logs/sender_log.txt