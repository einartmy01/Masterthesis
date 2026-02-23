#!/bin/bash

awk -F, 'NR==FNR {if(NR>1) send[$2]=$1; next}
NR>1 && $2 in send {
    delay = $1 - send[$2]
    print $2 "," send[$2] "," $1 "," delay
}' logs/sender_rtp.csv logs/receiver_rtp.csv \
> logs/delay_results.csv