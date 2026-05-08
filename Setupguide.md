Receiver Setup Guide

GStreamer H.264 multi-stream receiver — Ubuntu installation & troubleshooting


Requirements
CategoryComponentsRuntimePython 3, GStreamer 1.0, Python bindings (gi, Gst, GLib)GStreamer pluginsudpsrc, rtph264depay, h264parse, avdec_h264, autovideosink, rtspsrc, rtph264pay, udpsinkPython stdlibos, time, csv, struct, threading, collections, datetime, subprocess, sysSystem toolssudo, ip, ping, nc (netcat)NetworkingTailscale

Installation

Tip: To paste into a Linux terminal: Ctrl + Shift + V

1. Update the OS
Start with a fresh Ubuntu install (latest LTS recommended), then update:
bashsudo apt update && sudo apt upgrade -y
2. Install Python
bashsudo apt install -y python3 python3-pip
3. Install curl
Required for the Tailscale installation later:
bashsudo apt install -y curl
4. Install GStreamer
Install all plugins in one block — includes codecs, network elements, and hardware support:
bashsudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  nvidia-cuda-toolkit
5. Install GStreamer Python bindings
Must be installed after step 4, as they depend on those packages:
bashsudo apt install -y \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
6. Install network tools
Usually included with Ubuntu, but install to be safe:
bashsudo apt install -y \
  iproute2 \
  iputils-ping \
  netcat-openbsd
7. Open firewall ports
bashsudo ufw allow 5000/udp
sudo ufw allow 5002/udp
sudo ufw allow 5004/udp
8. Install and configure Tailscale
Tailscale gives each machine a stable IP and lets them communicate as if on the same local network, even over the internet.
bashcurl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
Open the link in your browser (Ctrl + click) and log in. Repeat on both PCs using the same Tailscale account. Then find the receiver's Tailscale IP (e.g. 100.92.97.93) and set it as RECEIVER_IP in sender.py.
9. Verify the installation
bashgst-inspect-1.0 | grep -E "udpsrc|udpsink|rtph264|h264parse|avdec_h264|autovideosink|rtspsrc"
All listed elements should appear in the output. If any are missing, re-run step 4.

Troubleshooting
Tailscale — switch account or re-authenticate
bashsudo tailscale logout
sudo tailscale up
NVIDIA GPU not detected
bashnvidia-smi
If not found, install the driver and reboot:
bashsudo apt install -y nvidia-utils-595
sudo ubuntu-drivers install
sudo reboot -i
Check available hardware-accelerated decoders
bashgst-inspect-1.0 | grep -E "vaapi|va|vulkan" | grep -i "264"
vainfo

Note: The pipeline uses avdec_h264 (software decoding) by default. At 3 streams this typically uses ~20% CPU — plenty of headroom. GPU decoding is only worth pursuing if you plan to scale beyond 3 streams or run other heavy workloads simultaneously.

Vulkan decoding — avoid for latency-sensitive use
Vulkan H.264 decoding (vulkanh264dec) is available on NVIDIA GPUs but adds ~0.5 seconds of latency per frame. Do not use it in this pipeline. Use avdec_h264 or VA-API (vah264dec) instead.
Test a single stream from the terminal
bashgst-launch-1.0 udpsrc port=5000 \
  caps="application/x-rtp, media=video, encoding-name=H264, payload=96" ! \
  rtph264depay ! h264parse ! avdec_h264 ! autovideosink sync=false
Reboot blocked by session inhibitor
bashsudo reboot -i
The -i flag forces the reboot by ignoring session inhibitors.