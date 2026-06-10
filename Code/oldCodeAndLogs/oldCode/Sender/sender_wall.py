#!/usr/bin/env python3
"""
sender_wall.py
--------------
Sender using the system wall clock (time.time_ns()) for timestamps.
Run this when no VBOX is available.
Note: wall clock is not synchronized across machines, so latency
      measurements will include clock offset between sender and receiver.
"""

import time
import Code.oldCode.Sender.sender_gst as sender_gst


def main():
    print("Using wall clock timestamps (time.time_ns())")
    sender_gst.run(get_time_ns=time.time_ns)


if __name__ == "__main__":
    main()
