#!/bin/bash

#Purpose:
#	Capture video from a V4L2 camera, encode with H.264 using Intel Quick Sync (VAAPI), 
#	packetize into RTP, and transmit over UDP

#Timing model:
#	Timestamp origin: v4l2src (capture time)
#	No clock synchronization or buffering at sink

gst-launch-1.0 \ 					#Starts a gstreamer pipeline
v4l2src do-timestamp=true ! \ 				#Captures video from , including timestamp from kernel clock
videoconvert ! \ 					#Converts pixel format (VAAPI prefers NV12)
vaapih264enc keyframe-period=30 bitrate=4000 ! \ 	#Activate hardware H.264 encoder. One I-frame per second at 30fps. Bitrate controls stable quaility.
rtph264pay config-interval=1 pt=96 ! \			#
udpsink host=127.0.0.1 port=5000 sync=false async=false	#

