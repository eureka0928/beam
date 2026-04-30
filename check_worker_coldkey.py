"""Check worker coldkey via BeamCore. Usage: python check_worker_coldkey.py <worker_id>"""
import asyncio, sys, time, uuid, httpx, bittensor as bt


async def main(worker_id):
    wallet = bt.Wallet(name="beam-06", hotkey="miner-06")
    _ = wallet.hotkey

    hk = wallet.hotkey.ss58_address
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://beamcore.b1m.ai/auth/challenge",
                         json={"hotkey": hk, "role": "orchestrator"})
        d = r.json()
        sig = "0x" + wallet.hotkey.sign(d["message"].encode()).hex()
        r = await c.post("https://beamcore.b1m.ai/auth/verify",
                         json={"challenge_id": d["challenge_id"], "hotkey": hk,
                               "signature": sig, "key_name": f"probe-{uuid.uuid4().hex[:6]}"})
        api_key = r.json()["api_key"]

        ts = str(int(time.time())); nonce = uuid.uuid4().hex[:8]
        msg = f"orchestrator_auth:{hk}:{ts}:request:{nonce}"
        s = wallet.hotkey.sign(msg.encode()).hex()
        H = {"X-Hotkey": hk, "X-Orchestrator-Hotkey": hk, "X-Orchestrator-Uid": "164",
             "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
             "X-Orchestrator-Signature": s, "X-Orchestrator-Action": "request",
             "X-Api-Key": api_key}

        r = await c.get(f"https://beamcore.b1m.ai/orchestrators/workers/{worker_id}/coldkey", headers=H)
        print(f"coldkey: {r.status_code} {r.text}")
        r = await c.get(f"https://beamcore.b1m.ai/orchestrators/workers/{worker_id}/hotkey", headers=H)
        print(f"hotkey:  {r.status_code} {r.text}")

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "worker_9e0eb93301e8"))
