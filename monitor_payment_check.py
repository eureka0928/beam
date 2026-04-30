import asyncio
import bittensor as bt
import httpx
import time
import uuid
import os

# Focus on UIDs 69 and 104
ORCHESTRATORS = [
    {"uid": 69, "wallet": "beam-02", "hotkey_name": "miner-02", "password": "beam-02",
     "api_key": "b1m_43ce5d13d0440015593bbcc990f124beddeed57b3df92a7e"},
    {"uid": 104, "wallet": "beam-05", "hotkey_name": "miner-05", "password": "beam-05",
     "api_key": "b1m_8efb72a79f9036716e91218b3d7bb9f130cfc9c641a1651d"},
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

async def check_and_pay(orch):
    uid = orch["uid"]
    os.environ["BT_WALLET_PASSWORD"] = orch["password"]
    w = bt.Wallet(name=orch["wallet"], hotkey=orch["hotkey_name"])
    hk = w.hotkey.ss58_address
    api_key = orch["api_key"]

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get("https://beamcore.b1m.ai/orchestrators/tasks?limit=50",
                headers=make_auth(w, hk, uid, api_key))
        except Exception as e:
            print(f"  UID {uid}: API error: {e}")
            return

        if r.status_code == 403:
            print(f"  UID {uid}: API key expired (403)")
            return
        if r.status_code != 200:
            print(f"  UID {uid}: Tasks API {r.status_code}")
            return

        tasks = r.json() if isinstance(r.json(), list) else r.json().get("tasks", [])
        unpaid = [t for t in tasks if t.get("status") == "completed" and t.get("has_pob")]

        if not unpaid:
            print(f"  UID {uid}: 0 unpaid")
            return

        print(f"  UID {uid}: {len(unpaid)} UNPAID!")

        for t in unpaid:
            tid = t["task_id"]

            r2 = await client.get(f"https://beamcore.b1m.ai/pob/{tid}/validate-transfer",
                headers=make_auth(w, hk, uid, api_key))
            vt = r2.json()
            if not vt.get("valid") or not vt.get("transfer_id"):
                print(f"    {tid}: skip ({vt.get('error','')})")
                continue

            transfer_id = vt["transfer_id"]

            r3 = await client.get(f"https://beamcore.b1m.ai/orchestrators/tasks/{tid}/payment-address",
                headers=make_auth(w, hk, uid, api_key))
            dest = r3.json().get("address")
            if not dest:
                print(f"    {tid}: no payment address")
                continue

            memo = f"{transfer_id}:{tid}"
            print(f"    PAYING {tid}: dest={dest[:20]}... memo={memo[:50]}")

            try:
                sub = bt.Subtensor(network="finney")
                substrate = sub.substrate
                ck = w.coldkey

                remark = substrate.compose_call("System", "remark_with_event", {"remark": memo.encode()})
                transfer = substrate.compose_call("SubtensorModule", "transfer_stake", {
                    "destination_coldkey": dest, "hotkey": hk,
                    "origin_netuid": 105, "destination_netuid": 105,
                    "alpha_amount": 500_000_000,
                })
                batch = substrate.compose_call("Utility", "batch_all", {"calls": [remark, transfer]})
                ext = substrate.create_signed_extrinsic(call=batch, keypair=ck)
                receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True)

                if not receipt.is_success:
                    print(f"    {tid}: TX FAILED: {receipt.error_message}")
                    continue

                tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
                print(f"    {tid}: TX OK {tx_hash[:40]}...")

                r4 = await client.post(f"https://beamcore.b1m.ai/pob/{tid}/payment",
                    headers=make_auth(w, hk, uid, api_key),
                    json={"tx_hash": tx_hash, "amount_rao": 500_000_000})
                print(f"    {tid}: PoB {r4.status_code} {r4.text[:120]}")

            except Exception as e:
                print(f"    {tid}: ERROR: {e}")

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking UIDs 69, 104...")
    for orch in ORCHESTRATORS:
        await check_and_pay(orch)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Done")

asyncio.run(main())
