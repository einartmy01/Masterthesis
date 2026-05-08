# Setup Guide — GStreamer RTP Video Pipeline
### Sender PC & Receiver PC (Ubuntu 24.04 LTS)

---

## Overview

These scripts stream H.264 video from IP cameras over RTP/UDP using GStreamer, with latency measurements logged to CSV files.

**Dependencies used by the scripts:**
- Python 3 — standard library modules: `os`, `time`, `csv`, `struct`, `threading`, `collections`, `datetime`, `subprocess`, `sys`
- GStreamer 1.0 with Python bindings (`gi`, `Gst`, `GLib`)
- GStreamer elements: `udpsrc`, `udpsink`, `rtph264depay`, `rtph264pay`, `h264parse`, `avdec_h264`, `autovideosink`, `rtspsrc`
- System tools: `sudo`, `ip`, `ping`, `nc` (netcat)
- Tailscale — for a stable IP over the internet (acts like a local network)

> **Tip:** To paste into a Linux terminal: `Ctrl+Shift+V`

---

## Step 1 — Update the system

Start with a full system update on a fresh install:

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Step 2 — Install Python 3 and curl

Ubuntu 24.04 ships with Python 3, but confirm it is present and install pip and curl:

```bash
sudo apt install -y python3 python3-pip curl
```

`curl` is needed for the Tailscale install later.

---

## Step 3 — Install GStreamer 1.0 core and plugins

```bash
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  nvidia-cuda-toolkit
```

| Package | Elements / purpose |
|---|---|
| `gstreamer1.0-tools` | `gst-launch-1.0`, `gst-inspect-1.0` (debugging) |
| `gstreamer1.0-plugins-base` | `videotestsrc`, base infrastructure |
| `gstreamer1.0-plugins-good` | `udpsrc`, `udpsink`, `rtph264depay`, `rtph264pay`, `autovideosink` |
| `gstreamer1.0-plugins-bad` | `h264parse`, `rtspsrc` |
| `gstreamer1.0-plugins-ugly` | Required for certain codec licensing chains |
| `gstreamer1.0-libav` | `avdec_h264` — FFmpeg-based H.264 software decoder |
| `nvidia-cuda-toolkit` | GPU acceleration support |

---

## Step 4 — Install GStreamer Python bindings

These must be installed **after** the GStreamer packages above:

```bash
sudo apt install -y \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
```

Verify the bindings work:

```bash
python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; print('GStreamer OK:', Gst.version_string())"
```

Expected output: `GStreamer OK: GStreamer 1.24.x`

---

## Step 5 — Install network tools

```bash
sudo apt install -y iproute2 iputils-ping netcat-openbsd
```

| Tool | Used by | Purpose |
|---|---|---|
| `ip` (iproute2) | Sender | Assigns static IPs to network interfaces |
| `ping` | Sender | Checks camera reachability before starting |
| `nc` (netcat) | Sender | Verifies RTSP port is open on each camera |

---

## Step 6 — Install and configure Tailscale

Tailscale gives both machines a stable IP address over the internet, making them behave as if they are on the same local network.

**Install:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

**Connect:**

```bash
sudo tailscale up
```

Open the link that appears (`Ctrl+Click`) and log in. **Repeat on both PCs — use the same Tailscale account.**

Once both are connected, find the Receiver PC's Tailscale IP (format: `100.x.x.x`) in the Tailscale dashboard, then update `RECEIVER_IP` at the top of `sender.py` with that address.

---

## Step 7 — (Receiver only) Open firewall ports

The receiver listens on UDP ports `5000`, `5002`, and `5004`:

```bash
sudo ufw allow 5000/udp
sudo ufw allow 5002/udp
sudo ufw allow 5004/udp
```

Check firewall status with `sudo ufw status`. If ufw is inactive, skip this step.

---

## Step 8 — (Sender only) Verify interface and camera names

The sender script has network interface names (`eth0`, `eth1`, etc.) and camera IPs hardcoded at the top of the file. Check what your sender PC actually has:

```bash
ip link show
```

If your interface names differ (e.g. `enp3s0` instead of `eth0`), update the `INTERFACES` and `LOCAL_IPS` lists in `sender.py` to match before running.

---

## Step 9 — Verify all GStreamer elements are present

```bash
gst-inspect-1.0 | grep -E "udpsrc|udpsink|rtph264|h264parse|avdec_h264|autovideosink|rtspsrc"
```

The output should list all elements. If any are missing, re-check that the corresponding plugin package from Step 3 installed correctly.

---

## Step 10 — Run the scripts

**Receiver:**

```bash
python3 receiverV11.py
```

Starts listening on ports 5000, 5002, and 5004 and creates CSV log files under `logs/`.

**Sender:**

```bash
sudo python3 senderV20.py
```

> The sender requires `sudo` because it calls `sudo ip addr` internally to configure network interfaces.

---

## Quick-reference — All commands in order

```bash
# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Python and curl
sudo apt install -y python3 python3-pip curl

# 3. GStreamer plugins
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  nvidia-cuda-toolkit

# 4. Python GStreamer bindings (install after GStreamer packages)
sudo apt install -y \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0

# 5. Network tools
sudo apt install -y iproute2 iputils-ping netcat-openbsd

# 6. Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# → Open the link, log in on BOTH PCs with the same account
# → Set RECEIVER_IP in sender.py to the receiver's Tailscale IP (100.x.x.x)

# 7. Open firewall ports (receiver only)
sudo ufw allow 5000/udp && sudo ufw allow 5002/udp && sudo ufw allow 5004/udp

# 8. Verify GStreamer elements
gst-inspect-1.0 | grep -E "udpsrc|udpsink|rtph264|h264parse|avdec_h264|autovideosink|rtspsrc"
```

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'gi'`**
→ Re-run Step 4. Make sure you are using the system Python 3, not a virtualenv.

**`No such element or plugin 'avdec_h264'`**
→ Run `sudo apt install -y gstreamer1.0-libav` and try again.

**`No such element or plugin 'rtspsrc'`**
→ Run `sudo apt install -y gstreamer1.0-plugins-bad`.

**`Permission denied` when sender runs `ip addr`**
→ Run the sender script with `sudo`.

**Video windows do not appear on the receiver**
→ `autovideosink` requires a graphical desktop session (X11 or Wayland). If running over SSH, use `ssh -X` or switch to `fakesink` for headless testing.

**Ports already in use on the receiver**
→ Check with `sudo ss -ulnp | grep -E '5000|5002|5004'` and kill any conflicting process.

**Switching Tailscale accounts**
→ Log out and reconnect with the new account:
```bash
sudo tailscale logout
sudo tailscale up
```
Then open the new login link and authenticate.
