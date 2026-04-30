#!/bin/bash
LOG="/root/beam/monitor_payment.log"
echo "$(date '+%Y-%m-%d %H:%M:%S') | === Payment monitor started (PID $$) ===" >> "$LOG"

while true; do
    /root/beam/.venv/bin/python /root/beam/monitor_payment_check.py >> "$LOG" 2>&1
    sleep 300
done
