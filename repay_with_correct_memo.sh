#!/bin/bash
# Monitor validate-transfer endpoint and re-pay with correct memo once transfer_id is available.
# Usage: nohup bash /root/beam/repay_with_correct_memo.sh &

LOG="/root/beam/repay_memo.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG"
}

log "=== Repay monitor started (PID $$) ==="

while true; do
    RESP=$(/root/beam/.venv/bin/python << 'PYEOF'
import asyncio, bittensor as bt, httpx, time, uuid, json

async def main():
    w = bt.Wallet(name="beam-03", hotkey="miner-03")
    hk = w.hotkey.ss58_address
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
    sig = w.hotkey.sign(msg.encode("utf-8")).hex()
    headers = {
        "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": "77",
        "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": "request",
        "X-Api-Key": "b1m_d5cf6cbcabae6151bb78a40d897c180b99315cdd5c752348",
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://beamcore.b1m.ai/pob/task-0680fa6ea4874d90/validate-transfer", headers=headers)
        print(r.text)
asyncio.run(main())
PYEOF
    )

    VALID=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('valid',False))" 2>/dev/null)
    TRANSFER_ID=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('transfer_id','') or '')" 2>/dev/null)

    log "Probe: valid=$VALID transfer_id=$TRANSFER_ID"

    if [ "$VALID" = "True" ] && [ -n "$TRANSFER_ID" ] && [ "$TRANSFER_ID" != "None" ] && [ "$TRANSFER_ID" != "" ]; then
        log "transfer_id found: $TRANSFER_ID — executing payment..."

        /root/beam/.venv/bin/python << PYEOF
import pexpect, sys

script = '''
import asyncio
import bittensor as bt
import httpx
import time
import uuid

async def main():
    w = bt.Wallet(name="beam-03", hotkey="miner-03")
    ck = w.coldkey
    HOTKEY = w.hotkey.ss58_address
    API_KEY = "b1m_d5cf6cbcabae6151bb78a40d897c180b99315cdd5c752348"
    TRANSFER_ID = "${TRANSFER_ID}"

    sub = bt.Subtensor(network="finney")
    substrate = sub.substrate

    task_id = "task-0680fa6ea4874d90"
    dest = "5ChZ2JF1gagECCQKCCFPprjngA3RsfAic2poAhvLJFG5V4Dz"
    amount = 2042
    memo = f"{TRANSFER_ID}:{task_id}"

    print(f"Paying {task_id}")
    print(f"  To: {dest}")
    print(f"  Amount: {amount} RAO")
    print(f"  Memo: {memo}")

    remark = substrate.compose_call("System", "remark_with_event", {"remark": memo.encode()})
    transfer = substrate.compose_call("Balances", "transfer_keep_alive", {"dest": dest, "value": amount})
    batch = substrate.compose_call("Utility", "batch_all", {"calls": [remark, transfer]})
    ext = substrate.create_signed_extrinsic(call=batch, keypair=ck)
    receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True)

    if not receipt.is_success:
        print(f"  FAILED: {receipt.error_message}")
        return

    tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
    print(f"  ON-CHAIN SUCCESS: {tx_hash[:60]}...")

    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{HOTKEY}:{ts}:request:{nonce}"
    sig = w.hotkey.sign(msg.encode("utf-8")).hex()
    headers = {
        "X-Hotkey": HOTKEY, "X-Orchestrator-Hotkey": HOTKEY, "X-Orchestrator-Uid": "77",
        "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": "request",
        "X-Api-Key": API_KEY, "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://beamcore.b1m.ai/pob/{task_id}/payment",
            headers=headers,
            json={"tx_hash": tx_hash, "amount_rao": amount},
        )
        print(f"  PoB record: {r.status_code} {r.text[:300]}")

asyncio.run(main())
'''

with open("/tmp/repay_exec.py", "w") as f:
    f.write(script)

child = pexpect.spawn("/root/beam/.venv/bin/python /tmp/repay_exec.py", timeout=120)
child.logfile_read = sys.stdout.buffer
try:
    child.expect("password:", timeout=15)
    child.sendline("beam-03")
    child.expect(pexpect.EOF, timeout=120)
except:
    pass
PYEOF

        log "Payment script finished"
        log "=== Repay monitor complete ==="
        exit 0
    fi

    log "Not ready yet, checking again in 5 minutes..."
    sleep 300
done
