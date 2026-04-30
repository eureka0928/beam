#!/bin/bash
# Monitor BeamCore worker registration limit and auto-launch workers when unblocked.
# Checks every 30 minutes. Launches 20 tony workers for UID 69 with stagger.
# Usage: nohup bash /root/beam/monitor_and_register.sh &

LOG="/root/beam/monitor_register.log"
CONFIG="/root/beam/neurons/worker/ecosystem.beam02-tony.config.js"
WORKERS_LAUNCHED=false

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG"
}

test_registration() {
    # Use a real worker wallet to test registration
    RESP=$(/root/beam/.venv/bin/python -c "
import httpx, hashlib, asyncio, bittensor as bt

async def test():
    w = bt.Wallet(name='tony', hotkey='tony-001')
    hk = w.hotkey.ss58_address
    ip = '116.202.53.114'
    port = 9000
    msg = f'{hk}:{ip}:{port}'
    sig = w.hotkey.sign(msg.encode()).hex()
    ppk = hashlib.sha256(f'payment:{hk}'.encode()).hexdigest()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post('https://beamcore.b1m.ai/workers/register', json={
            'hotkey': hk, 'ip': ip, 'port': port, 'initial_stake': 0,
            'claimed_bandwidth_mbps': 100, 'region': 'US',
            'coldkey': hk, 'payment_pubkey': ppk, 'signature': sig,
        })
        print(r.text[:300])
asyncio.run(test())
" 2>&1)

    if echo "$RESP" | grep -q "registration limit"; then
        log "Probe: Still blocked - $RESP"
        return 1
    elif echo "$RESP" | grep -q "worker_id"; then
        log "Probe: Registration SUCCESS - $RESP"
        return 0
    else
        log "Probe: Different response - $RESP"
        return 0  # Limit likely lifted, try launching
    fi
}

launch_workers() {
    log "Launching 20 workers for UID 69 with 15s stagger..."
    cd /root/beam/neurons/worker

    # Delete any stopped workers first
    pm2 delete beam-tw001 beam-tw002 beam-tw003 beam-tw004 beam-tw005 \
        beam-tw006 beam-tw007 beam-tw008 beam-tw009 beam-tw010 \
        beam-tw011 beam-tw012 beam-tw013 beam-tw014 beam-tw015 \
        beam-tw016 beam-tw017 beam-tw018 beam-tw019 beam-tw020 2>/dev/null

    pm2 start "$CONFIG" 2>&1 | tee -a "$LOG"
    WORKERS_LAUNCHED=true

    # Wait for staggered registration (~5.5 min for 20 workers at 15s each + buffer)
    log "Waiting 6 minutes for staggered registration..."
    sleep 360

    # Check results
    REGISTERED=$(pm2 logs --nostream --lines 50 2>&1 | grep -c "Registered: worker_")
    ERRORS=$(pm2 logs --nostream --lines 50 2>&1 | grep -c "registration limit")
    log "Results: $REGISTERED registered, $ERRORS blocked"
    pm2 list 2>&1 | grep "beam-tw" >> "$LOG"
}

# Main loop
log "=== Monitor started (PID $$) ==="

while true; do
    if [ "$WORKERS_LAUNCHED" = true ]; then
        ONLINE=$(pm2 list 2>&1 | grep "beam-tw" | grep -c "online")
        STOPPED=$(pm2 list 2>&1 | grep "beam-tw" | grep -c "stopped")
        log "Health: $ONLINE online, $STOPPED stopped"
        if [ "$ONLINE" -ge 15 ]; then
            log "Workers stable. Monitor complete."
            exit 0
        fi
        sleep 1800
        continue
    fi

    test_registration
    if [ $? -eq 0 ]; then
        launch_workers
    else
        sleep 1800
    fi
done
