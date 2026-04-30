"""Probe BeamCore for unpaid tasks on a UID."""
import asyncio, sys, time, uuid, httpx, bittensor as bt


UIDS = {
    "beam-06:miner-06:164": "beam-06",
    "beam-05:miner-05:104": "beam-05",
    "beam-04:miner-04:102": "beam-04",
    "beam-02:miner-02:69":  "beam-02",
}


async def probe(wallet_name, hotkey_name, uid, pw):
    w = bt.Wallet(name=wallet_name, hotkey=hotkey_name)
    _ = w.hotkey
    hk = w.hotkey.ss58_address

    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://beamcore.b1m.ai/auth/challenge",
                         json={"hotkey": hk, "role": "orchestrator"})
        d = r.json()
        sig = "0x" + w.hotkey.sign(d["message"].encode()).hex()
        r = await c.post("https://beamcore.b1m.ai/auth/verify",
                         json={"challenge_id": d["challenge_id"], "hotkey": hk,
                               "signature": sig, "key_name": f"probe-{uuid.uuid4().hex[:6]}"})
        if r.status_code != 200:
            print(f"UID {uid}: auth failed {r.status_code} {r.text[:100]}")
            return
        api_key = r.json().get("api_key")
        if not api_key:
            print(f"UID {uid}: no api_key in verify response: {r.json()}")
            return

        ts = str(int(time.time())); nonce = uuid.uuid4().hex[:8]
        msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
        H = {"X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": str(uid),
             "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
             "X-Orchestrator-Signature": w.hotkey.sign(msg.encode()).hex(),
             "X-Orchestrator-Action": "request", "X-Api-Key": api_key}

        r = await c.get("https://beamcore.b1m.ai/orchestrators/tasks/pending-results?limit=50", headers=H)
        pending = r.json() if r.status_code == 200 else None
        r = await c.get("https://beamcore.b1m.ai/orchestrators/payments/paid-task-ids", headers=H)
        paid = r.json() if r.status_code == 200 else None

        print(f"\n=== UID {uid} ({hk[:16]}...) ===")
        print(f"pending-results: type={type(pending).__name__}")
        if isinstance(pending, dict):
            print(f"  keys: {list(pending.keys())}")
            items = pending.get("tasks") or pending.get("results") or pending.get("items") or []
            print(f"  items count: {len(items) if isinstance(items, list) else 'n/a'}")
            if items and isinstance(items, list):
                print(f"  sample: {items[0]}")
        elif isinstance(pending, list):
            print(f"  count: {len(pending)}")
            if pending: print(f"  sample: {pending[0]}")

        print(f"paid-task-ids: type={type(paid).__name__}")
        if isinstance(paid, dict):
            print(f"  keys: {list(paid.keys())}")
            ids = paid.get("task_ids") or paid.get("ids") or []
            print(f"  count: {len(ids) if isinstance(ids, list) else 'n/a'}")
        elif isinstance(paid, list):
            print(f"  count: {len(paid)}")


async def main():
    for key, pw in UIDS.items():
        wname, hname, uid = key.split(":")
        try:
            await probe(wname, hname, int(uid), pw)
        except Exception as e:
            print(f"UID {uid}: error {e}")


asyncio.run(main())
