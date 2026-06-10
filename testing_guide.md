# Testing Guide — Running Sender/Receiver Pairs

> **Full system setup:** See [setup_guide.md](setup_guide.md) for installation of GStreamer, Python bindings, Tailscale, and firewall configuration.

---

## Prerequisites Checklist

Before running any test, confirm the following on **both** PCs:

- [ ] GStreamer 1.0 and Python bindings installed (see `setup_guide.md`)
- [ ] Both PCs connected to the **same Tailscale account** (`sudo tailscale up`)
- [ ] Receiver firewall open: `sudo ufw allow 5000/udp 5002/udp 5004/udp`
- [ ] Sender script configured with the correct `RECEIVER_IP` (Tailscale IP, format `100.x.x.x`)
- [ ] Sender script configured with correct `INTERFACES` / `LOCAL_IPS` for its network adapters
- [ ] IP cameras reachable at `192.168.0.100`–`192.168.0.102` on RTSP port 554

---

## Sender/Receiver Pairs

Always use a matching sender and receiver — codec, transport, and port layout must align.

| Test scenario | Sender script | Receiver script |
|---|---|---|
| **H.264 (standard)** | `senderV22-H-264.py` | `receiverV14-H-264.py` |
| **H.264 720p** | `senderV22-H-264-720p.py` | `receiverV14-H-264.py` |
| **H.264 GPU** | `senderV22-H-264-GPU.py` | `receiverV14-H-264.py` |
| **H.265 (standard)** | `senderV22-H-265.py` | `receiverV14-H-265.py` |
| **H.265 720p** | `senderV22-H-265-720p.py` | `receiverV14-H-265.py` |
| **H.265 GPU** | `senderV22-H-265-GPU.py` | `receiverV14-H-265.py` |
| **MJPEG** | `senderV22-MJPEG.py` | `receiverV14-MJPEG.py` |
| **UDP/RTP** | `senderV22-UDP-RTP.py` | `receiverV14-UDP-RTP.py` |
| **UDP/RTP bandwidth** | `senderV22-UDP-RTP-Bandwidth.py` | `receiverV14-UDP-RTP.py` |
| **UDP/RTP no transcode** | `senderV22-UDP-RTP-No-Transcode.py` | `receiverV14-UDP-RTP.py` |
| **UDP/SRTP** | `senderV22-UDP-SRTP.py` | `receiverV14-UDP-SRTP.py` |
| **TCP/RTP** | `senderV22-TCP-RTP.py` | `receiverV15-TCP-RTP.py` |
| **TCP/RTP bandwidth** | `senderV22-TCP-RTP-Bandwidth.py` | `receiverV15-TCP-RTP.py` |
| **2 cameras** | `senderV22-H-264-2Cams.py` | `receiverV14-H-264-2Cams.py` |

---

## Running a Test

### Step 1 — Configure the sender script

Open the sender script and update the constants at the top:

```python
RECEIVER_IP = "100.x.x.x"          # Tailscale IP of the receiver PC
INTERFACES  = ["eth0", "eth1"]      # Your network interface names (check: ip link show)
LOCAL_IPS   = ["192.168.0.50/24", "192.168.1.50/24"]
```

If your interfaces don't have an IP yet, assign them manually:

```bash
sudo ip addr flush dev eth0 && sudo ip addr add 192.168.0.50/24 dev eth0
sudo ip addr flush dev eth1 && sudo ip addr add 192.168.1.50/24 dev eth1
```

### Step 2 — Start the receiver (Receiver PC)

Navigate to the `Code/` directory and start the receiver **before** the sender:

```bash
cd ~/Masterthesis/Code
python3 receiverV14-H-264.py
```

The receiver prints its listening ports and waits for the stream. Log files are written under `logs/` as they arrive.

### Step 3 — Start the sender (Sender PC)

In a separate terminal, start the matching sender with `sudo` (required for interface configuration):

```bash
cd ~/Masterthesis/Code
sudo python3 senderV22-H-264.py
```

The sender will:
1. Assign IPs to network interfaces
2. Ping cameras to verify reachability
3. Start GStreamer pipelines and begin streaming to the receiver

### Step 4 — Stop the test

Stop both scripts with `Ctrl+C`. Stop the **sender first**, then the **receiver**, so the receiver can flush any remaining log data.

---

## Minimal/Debug Variants

These stripped-down scripts are useful for verifying connectivity without full logging:

| Script | Purpose |
|---|---|
| `sender-Pure.py` | Sender with no logging — only forwards RTP |
| `receiver-Pure.py` | Receiver with no logging — only displays video |
| `sender-minimal.py` | Camera reachability check only |
| `sender-minimal-logged.py` | Minimal sender with transit latency logging |

Run these the same way as the full variants (receiver first, sender second).

---

## Log Files

All logs are written to `Code/logs/` with a shared timestamp prefix (`YYYYMMDD_HHMMSS`):

| Subdirectory | Files | Written by |
|---|---|---|
| `logs/pipeline/sender/` | `send_pipe_<ts>.csv` | Sender |
| `logs/pipeline/receiver/` | `rec_full_<ts>.csv` | Receiver |
| `logs/transit/` | `send_transit_<ts>.csv`, `rec_transit_<ts>.csv` | Both |
| `logs/quality/` | `rec_quality_<ts>.csv` | Receiver |
| `logs/throughput/` | `sender_throughput_<ts>.csv` | Sender |
| `logs/cpu/` | `sender_cpu_<ts>.log` | Sender |

---

## Analyzing Results

After a test run, use the master analyzer to process all logs for a given timestamp:

```bash
cd ~/Masterthesis/Code/Analyzers

# Auto-discover latest timestamp and run all analyses
python3 run_all_analyses.py

# Or specify a timestamp explicitly
python3 run_all_analyses.py 20240610_143022
```

This runs the following analyzers in order:

| Analyzer | Output |
|---|---|
| `sender_pipeline_analyseV2.py` | Sender-side pipeline latency stats |
| `receiver_pipeline_analyseV2.py` | Receiver-side pipeline latency stats |
| `transit_analyseV5.py` | Network transit time distribution |
| `combine_transitV2.py` | Combined sender+receiver transit view |
| `throughput_analyse.py` | Bandwidth usage over time |
| `cpu_analyseV2.py` | CPU utilization during the run |
| `quality_analyse.py` | BRISQUE video quality scores |

Graphs are saved to `Code/Analyzers/graphs/<timestamp>/`.

**Analyzer Python dependencies** (install once):

```bash
pip3 install pandas matplotlib numpy torch piq
```

---

## Quick Checklist for Each Run

```
[ ] RECEIVER_IP updated in sender script
[ ] Receiver started and listening
[ ] Sender started (sudo)
[ ] Test running — monitor terminal output for errors
[ ] Ctrl+C sender, then receiver
[ ] python3 run_all_analyses.py
[ ] Check graphs/ for results
```
