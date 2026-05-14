import subprocess
import os

def setup_trb500():
    # Find TRB500 interface by USB ID
    iface = None
    for net_iface in os.listdir('/sys/class/net'):
        try:
            vendor = open(f'/sys/class/net/{net_iface}/device/../idVendor').read().strip()
            product = open(f'/sys/class/net/{net_iface}/device/../idProduct').read().strip()
            if vendor == '0525' and product == 'a4a2':
                iface = net_iface
                break
        except:
            continue
    if not iface:
        print("TRB500 not found!")
        return
    print(f"TRB500 found on: {iface}")
    # Set IP
    subprocess.run(["sudo", "ip", "addr", "replace", "192.168.2.189/24", "dev", iface])
    # Add default route (ignore error if already exists)
    subprocess.run(["sudo", "ip", "route", "add", "default", "via", "192.168.2.1", "dev", iface, "metric", "50"])
    # Fix DNS
    subprocess.run(["sudo", "bash", "-c", "echo 'nameserver 8.8.8.8' | tee /etc/resolv.conf"])    
    print("TRB500 ready!")
setup_trb500()