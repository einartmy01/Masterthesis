#!/bin/bash

if [ $# -ne 2 ]; then
    echo "Usage: $0 <sender_log> <receiver_log>"
    exit 1
fi

SENDER="$1"
RECEIVER="$2"
OUTPUT="logs/rtp_combined.csv"

# Remove ANSI colors first
clean_sender=$(mktemp)
clean_receiver=$(mktemp)

sed -r 's/\x1B\[[0-9;]*[mK]//g' "$SENDER" > "$clean_sender"
sed -r 's/\x1B\[[0-9;]*[mK]//g' "$RECEIVER" > "$clean_receiver"

# Extract sender data
awk '
/Preparing to push packet/ {
    seq=""; rtptime=""
    for(i=1;i<=NF;i++){
        if($i ~ /^seq=/){
            split($i,a,"=")
            gsub(/,/,"",a[2])
            seq=a[2]
        }
        if($i ~ /^rtptime=/){
            split($i,b,"=")
            gsub(/,/,"",b[2])
            rtptime=b[2]
        }
    }
    if(seq != "" && rtptime != "")
        print seq "," rtptime
}
' "$clean_sender" > sender_tmp.csv

# Extract receiver data
awk '
/seqnum/ {
    seq=""; rtptime=""; pts=""
    for(i=1;i<=NF;i++){
        if($i=="seqnum"){
            seq=$(i+1)
            gsub(/,/,"",seq)
        }
        if($i=="rtptime"){
            rtptime=$(i+1)
            gsub(/,/,"",rtptime)
        }
        if($i=="pts"){
            pts=$(i+1)
            gsub(/,/,"",pts)
        }
    }
    if(seq != "" && rtptime != "")
        print seq "," rtptime "," pts
}
' "$clean_receiver" > receiver_tmp.csv

# Combine by sequence number
echo "seq,sender_rtptime,receiver_rtptime,receiver_pts" > "$OUTPUT"

join -t',' -1 1 -2 1 <(sort sender_tmp.csv) <(sort receiver_tmp.csv) >> "$OUTPUT"

# Cleanup
rm sender_tmp.csv receiver_tmp.csv "$clean_sender" "$clean_receiver"

echo "Saved to $OUTPUT"