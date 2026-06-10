#!/bin/bash

if [ -z "$1" ]; then
    echo "Usage: $0 <logfile>"
    exit 1
fi

INPUT="$1"
OUTPUT="logs/latency_readable.txt"

# Remove ANSI color codes
# Keep only real latency measurements (numeric time values)
# Extract pipeline time, src element, sink element, latency (ms)

sed -r 's/\x1B\[[0-9;]*[mK]//g' "$INPUT" | \
grep "time=(guint64)[0-9]" | \
grep "src-element=" | \
awk '
{
    pipeline_time=$1

    src=""
    sink=""
    latency_ns=""

    for(i=1;i<=NF;i++){
        if($i ~ /^src-element=/){
            split($i,a,"=")
            gsub(/,/,"",a[2])
            src=a[2]
        }
        if($i ~ /^sink-element=/){
            split($i,b,"=")
            gsub(/,/,"",b[2])
            sink=b[2]
        }
        if($i ~ /^time=/){
            split($i,c,"=")
            gsub(/\(guint64\)/,"",c[2])
            gsub(/,/,"",c[2])
            latency_ns=c[2]
        }
    }

    if(latency_ns != ""){
        latency_ms = latency_ns / 1000000
        printf "%-15s  %-15s -> %-15s  %8.3f ms\n",
               pipeline_time, src, sink, latency_ms
    }
}
' > "$OUTPUT"

echo "Readable latency log saved to $OUTPUT"