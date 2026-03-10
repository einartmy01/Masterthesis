import re
import csv
import sys

def parse_latency_log(input_file, output_file):
    pattern = re.compile(
        r'(\S+)\s+\d+\s+\S+\s+TRACE.*?'
        r'latency,\s+'
        r'src-element-id=\(string\)(\S+),\s+'
        r'src-element=\(string\)(\S+),\s+'
        r'src=\(string\)(\S+),\s+'
        r'sink-element-id=\(string\)(\S+),\s+'
        r'sink-element=\(string\)(\S+),\s+'
        r'sink=\(string\)(\S+),\s+'
        r'time=\(guint64\)(\d+),\s+'
        r'ts=\(guint64\)(\d+)'
    )

    with open(input_file, 'r') as f_in, open(output_file, 'w', newline='') as f_out:
        writer = csv.writer(f_out)
        writer.writerow(['timestamp', 'src_element', 'sink_element', 'latency_ns', 'latency_ms', 'ts_ns'])

        for line in f_in:
            if 'TRACE' not in line or 'latency,' not in line:
                continue
            # Strip ANSI color codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
            m = pattern.search(clean)
            if m:
                ts, _, src_el, _, _, sink_el, _, time_ns, ts_ns = m.groups()
                latency_ns = int(time_ns)
                latency_ms = latency_ns / 1_000_000
                writer.writerow([ts, src_el, sink_el, latency_ns, round(latency_ms, 3), ts_ns])

    print(f"Done! Saved to {output_file}")

if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "logs/receiverLog.txt"
    output_file = sys.argv[2] if len(sys.argv) > 2 else "logs/latency.csv"
    parse_latency_log(input_file, output_file)