#!/bin/bash

INTERFACE=wlp0s20f3

sudo tshark -i $INTERFACE -f "udp port 5000" \
-T fields \
-e frame.time_epoch \
-e rtp.seq \
-e rtp.timestamp \
-E header=y \
-E separator=, \
> logs/sender_rtp.csv