#!/bin/bash
# Monitor validate-transfer for UID 69 unpaid tasks.
# Pays with correct {transfer_id}:{task_id} memo once endpoint is fixed.
# Usage: nohup bash /root/beam/repay_uid69.sh &

LOG="/root/beam/repay_uid69.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG"
}

log "=== UID 69 repay monitor started (PID $$) ==="

while true; do
    RESULT=$(/root/beam/.venv/bin/python << 'PYEOF'
import asyncio
import bittensor as bt
import httpx
import time
import uuid
import json

API_KEY = "b1m_43ce5d13d0440015593bbcc990f124beddeed57b3df92a7e"

async def main():
    w = bt.Wallet(name="beam-02", hotkey="miner-02")
    hk = w.hotkey.ss58_address

    def auth():
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex[:8]
        msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
        sig = w.hotkey.sign(msg.encode("utf-8")).hex()
        return {
            "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": "69",
            "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
            "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": "request",
            "X-Api-Key": API_KEY,
        }

    tasks = [
        {"task_id": "task-71885cead0ba4f7f", "dest": "5DP5RFHTZQq4cGHFmQEUGYmmJmpYshi7C9Av7dQWwQdyP8MC", "amount": 100000},
        {"task_id": "task-c56dd27faa7d4120", "dest": "5GbHzTbgAB8JZAKrsEVSkrGMAHtK7wJdmeq8n6yKfkszwr74", "amount": 100000},
        {"task_id": "spec-53a3ebb68c11493b", "dest": "5FEozSPZeqakutvYvpHuJQc6uHJYvZvLuBDLopCtUP47eDsf", "amount": 10000},
    ]

    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for t in tasks:
            r = await client.get(
                f"https://beamcore.b1m.ai/pob/{t['task_id']}/validate-transfer",
                headers=auth(),
            )
            data = r.json()
            valid = data.get("valid", False)
            transfer_id = data.get("transfer_id") or ""
            results.append({"task_id": t["task_id"], "valid": valid, "transfer_id": transfer_id})

    print(json.dumps(results))

asyncio.run(main())
PYEOF
    )

    log "Probe result: $RESULT"

    # Check if any task has valid=true with transfer_id
    HAS_VALID=$(echo "$RESULT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    valid_count = sum(1 for t in data if t.get('valid') and t.get('transfer_id'))
    print(valid_count)
except:
    print(0)
" 2>/dev/null)

    if [ "$HAS_VALID" -gt 0 ]; then
        log "Found $HAS_VALID valid tasks! Executing payment..."

        /root/beam/.venv/bin/python << 'INNEREOF'
import pexpect, sys

script = """
import asyncio
import bittensor as bt
import httpx
import time
import uuid
import json

API_KEY = "b1m_43ce5d13d0440015593bbcc990f124beddeed57b3df92a7e"

async def main():
    w = bt.Wallet(name="beam-02", hotkey="miner-02")
    ck = w.coldkey
    hk = w.hotkey.ss58_address

    sub = bt.Subtensor(network="finney")
    substrate = sub.substrate

    def auth():
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex[:8]
        msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
        sig = w.hotkey.sign(msg.encode("utf-8")).hex()
        return {
            "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": "69",
            "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
            "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": "request",
            "X-Api-Key": API_KEY, "Content-Type": "application/json",
        }

    tasks = [
        {"task_id": "task-71885cead0ba4f7f", "dest": "5DP5RFHTZQq4cGHFmQEUGYmmJmpYshi7C9Av7dQWwQdyP8MC", "amount": 100000},
        {"task_id": "task-c56dd27faa7d4120", "dest": "5GbHzTbgAB8JZAKrsEVSkrGMAHtK7wJdmeq8n6yKfkszwr74", "amount": 100000},
        {"task_id": "spec-53a3ebb68c11493b", "dest": "5FEozSPZeqakutvYvpHuJQc6uHJYvZvLuBDLopCtUP47eDsf", "amount": 10000},
    ]

    async with httpx.AsyncClient(timeout=30) as client:
        for t in tasks:
            tid = t["task_id"]

            # Get transfer_id
            r = await client.get(f"https://beamcore.b1m.ai/pob/{tid}/validate-transfer", headers=auth())
            vdata = r.json()
            if not vdata.get("valid") or not vdata.get("transfer_id"):
                print(f"SKIP {tid}: not valid ({vdata.get('error','')})")
                continue

            transfer_id = vdata["transfer_id"]
            memo = f"{transfer_id}:{tid}"
            print(f"PAY {tid}")
            print(f"  dest={t['dest']}")
            print(f"  amount={t['amount']} RAO")
            print(f"  memo={memo}")

            remark = substrate.compose_call("System", "remark_with_event", {"remark": memo.encode()})
            transfer = substrate.compose_call("Balances", "transfer_keep_alive", {"dest": t["dest"], "value": t["amount"]})
            batch = substrate.compose_call("Utility", "batch_all", {"calls": [remark, transfer]})
            ext = substrate.create_signed_extrinsic(call=batch, keypair=ck)
            receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True)

            if not receipt.is_success:
                print(f"  FAILED: {receipt.error_message}")
                continue

            tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
            print(f"  ON-CHAIN SUCCESS: {tx_hash[:60]}...")

            # Record PoB
            r2 = await client.post(
                f"https://beamcore.b1m.ai/pob/{tid}/payment",
                headers=auth(),
                json={"tx_hash": tx_hash, "amount_rao": t["amount"]},
            )
            print(f"  PoB record: {r2.status_code} {r2.text[:300]}")

asyncio.run(main())
"""

with open("/tmp/repay_uid69_exec.py", "w") as f:
    f.write(script)

child = pexpect.spawn("/root/beam/.venv/bin/python /tmp/repay_uid69_exec.py", timeout=180)
child.logfile_read = sys.stdout.buffer
try:
    child.expect("password:", timeout=15)
    child.sendline("beam-02")
    child.expect(pexpect.EOF, timeout=180)
except:
    pass
INNEREOF

        log "Payment complete"
        log "=== UID 69 repay monitor done ==="
        exit 0
    fi

    log "validate-transfer not ready yet, checking in 5 minutes..."
    sleep 300
done
