#!/bin/bash
# Monitor all orchestrators for unpaid completed tasks and pay manually.
# Checks every 5 minutes. Uses payment-address API + transfer_stake.
# Usage: nohup bash /root/beam/monitor_payment.sh &

LOG="/root/beam/monitor_payment.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG"
}

log "=== Payment monitor started (PID $$) ==="

while true; do
    /root/beam/.venv/bin/python << 'PYEOF'
import asyncio
import bittensor as bt
import httpx
import time
import uuid
import json
import os

# All our orchestrators
ORCHESTRATORS = [
    {"uid": 69, "wallet": "beam-02", "hotkey_name": "miner-02", "password": "beam-02"},
    {"uid": 77, "wallet": "beam-03", "hotkey_name": "miner-03", "password": "beam-03"},
    {"uid": 102, "wallet": "beam-04", "hotkey_name": "miner-04", "password": "beam-04"},
    {"uid": 104, "wallet": "beam-05", "hotkey_name": "miner-05", "password": "beam-05"},
    {"uid": 189, "wallet": "gen", "hotkey_name": "gen-09", "password": "rhkddid928"},
]

def make_auth(wallet, hk, uid, api_key):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
    sig = wallet.hotkey.sign(msg.encode("utf-8")).hex()
    return {
        "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": str(uid),
        "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": "request",
        "X-Api-Key": api_key, "Content-Type": "application/json",
    }

async def get_api_key(wallet, hk):
    """Get fresh API key via challenge/verify."""
    async with httpx.AsyncClient(timeout=30) as client:
        r1 = await client.post("https://beamcore.b1m.ai/auth/challenge",
            json={"hotkey": hk})
        if r1.status_code != 200:
            return None
        challenge = r1.json().get("challenge", "")
        sig = wallet.hotkey.sign(challenge.encode()).hex()
        r2 = await client.post("https://beamcore.b1m.ai/auth/verify",
            json={"hotkey": hk, "signature": sig, "challenge": challenge})
        if r2.status_code == 200:
            return r2.json().get("api_key")
    return None

async def check_and_pay(orch):
    uid = orch["uid"]
    w = bt.Wallet(name=orch["wallet"], hotkey=orch["hotkey_name"])
    hk = w.hotkey.ss58_address

    # Get API key
    api_key = await get_api_key(w, hk)
    if not api_key:
        print(f"  UID {uid}: Cannot get API key, skipping")
        return

    async with httpx.AsyncClient(timeout=30) as client:
        # Get tasks
        r = await client.get("https://beamcore.b1m.ai/orchestrators/tasks?limit=50",
            headers=make_auth(w, hk, uid, api_key))
        if r.status_code != 200:
            print(f"  UID {uid}: Tasks API failed: {r.status_code}")
            return

        tasks = r.json() if isinstance(r.json(), list) else r.json().get("tasks", [])
        unpaid = [t for t in tasks if t.get("status") == "completed" and t.get("has_pob")]

        if not unpaid:
            print(f"  UID {uid}: No unpaid tasks")
            return

        print(f"  UID {uid}: {len(unpaid)} unpaid task(s) found!")

        for t in unpaid:
            tid = t["task_id"]

            # Validate transfer
            r2 = await client.get(f"https://beamcore.b1m.ai/pob/{tid}/validate-transfer",
                headers=make_auth(w, hk, uid, api_key))
            vt = r2.json()
            if not vt.get("valid") or not vt.get("transfer_id"):
                print(f"    {tid}: validate failed ({vt.get('error','')})")
                continue

            transfer_id = vt["transfer_id"]

            # Get payment address
            r3 = await client.get(f"https://beamcore.b1m.ai/orchestrators/tasks/{tid}/payment-address",
                headers=make_auth(w, hk, uid, api_key))
            pa = r3.json()
            dest = pa.get("address")
            if not dest:
                print(f"    {tid}: no payment address")
                continue

            memo = f"{transfer_id}:{tid}"
            print(f"    {tid}: PAYING dest={dest[:16]}... memo={memo[:40]}...")

            # On-chain payment
            try:
                os.environ["BT_WALLET_PASSWORD"] = orch["password"]
                sub = bt.Subtensor(network="finney")
                substrate = sub.substrate
                ck = w.coldkey

                remark = substrate.compose_call("System", "remark_with_event", {"remark": memo.encode()})
                transfer = substrate.compose_call("SubtensorModule", "transfer_stake", {
                    "destination_coldkey": dest,
                    "hotkey": hk,
                    "origin_netuid": 105,
                    "destination_netuid": 105,
                    "alpha_amount": 500_000_000,
                })
                batch = substrate.compose_call("Utility", "batch_all", {"calls": [remark, transfer]})
                ext = substrate.create_signed_extrinsic(call=batch, keypair=ck)
                receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True)

                if not receipt.is_success:
                    print(f"    {tid}: TX FAILED: {receipt.error_message}")
                    continue

                tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
                print(f"    {tid}: TX SUCCESS {tx_hash[:40]}...")

                # Record PoB
                r4 = await client.post(f"https://beamcore.b1m.ai/pob/{tid}/payment",
                    headers=make_auth(w, hk, uid, api_key),
                    json={"tx_hash": tx_hash, "amount_rao": 500_000_000})
                print(f"    {tid}: PoB {r4.status_code} {r4.text[:100]}")

            except Exception as e:
                print(f"    {tid}: ERROR: {e}")

async def main():
    print(f"[{time.strftime('%H:%M:%S')}] Checking all orchestrators...")
    for orch in ORCHESTRATORS:
        await check_and_pay(orch)
    print(f"[{time.strftime('%H:%M:%S')}] Done")

asyncio.run(main())
PYEOF

    sleep 300  # Check every 5 minutes
done
