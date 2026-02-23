#!/bin/bash

gst-launch-1.0 \
v4l2src device=/dev/video0 do-timestamp=true ! \
videoconvert ! \
x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 bitrate=4000 bframes=0 ! \
rtph264pay pt=96 ! \
udpsink host=10.194.81.249 port=5000 sync=false async=false