"""SLA-join all 10 local workers to UID 164 without restart."""
import asyncio, uuid, time, httpx, bittensor as bt


ORCH_UID = 164
ORCH_HOTKEY = "5FhoJNyfya2r2Fk9inwfoG9EmvYdkC3vb9kvrVMFUB1ijsP4"
ORCH_API_KEY = "b1m_b36dbd9ee498c0b7197cb9ee5d163149e3270eb8fe0d5454"
ORCH_WALLET = "beam-06"
ORCH_HOTKEY_NAME = "miner-06"
ORCH_WALLET_PASSWORD = "beam-06"
BEAMCORE = "https://beamcore.b1m.ai"

WORKERS = [("beam-01", f"worker-{i:02}") for i in range(1, 11)]


async def orch_sig_headers(orch_wallet_bundle, action="write"):
    hk = orch_wallet_bundle.hotkey.ss58_address
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:{action}:{nonce}"
    sig = orch_wallet_bundle.hotkey.sign(msg.encode()).hex()
    return {
        "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk,
        "X-Orchestrator-Uid": str(ORCH_UID),
        "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": sig, "X-Orchestrator-Action": action,
        "X-Api-Key": ORCH_API_KEY, "Content-Type": "application/json",
    }


async def main():
    # Load orch wallet (hotkey only — no need for coldkey since no on-chain call)
    orch_w = bt.Wallet(name=ORCH_WALLET, hotkey=ORCH_HOTKEY_NAME)
    _ = orch_w.hotkey
    class _O: hotkey = orch_w.hotkey
    orch_bundle = _O()
    assert orch_w.hotkey.ss58_address == ORCH_HOTKEY, "orch hotkey mismatch"

    async with httpx.AsyncClient(timeout=30) as c:
        ok, fail = 0, 0
        for wname, hname in WORKERS:
            try:
                w = bt.Wallet(name=wname, hotkey=hname)
                _ = w.hotkey
                worker_hotkey = w.hotkey.ss58_address
            except Exception as e:
                print(f"[SKIP] {wname}/{hname}: wallet load failed: {e}")
                fail += 1
                continue

            # Lookup SLA membership (SLA uses worker hotkey as worker_id)
            r = await c.get(f"{BEAMCORE}/sla/workers/{worker_hotkey}/membership",
                            headers={"X-Api-Key": ORCH_API_KEY})
            if r.status_code != 200:
                print(f"[SKIP] {hname} hk={worker_hotkey[:16]}: membership lookup {r.status_code} {r.text[:150]}")
                fail += 1
                continue
            mem = r.json()
            worker_id = mem.get("worker_id") or worker_hotkey
            cur_aff = mem.get("orchestrator_id")
            if cur_aff and cur_aff != ORCH_UID:
                print(f"[NOTE] {hname} currently SLA-affiliated with UID {cur_aff}; will switch")

            ip, port = "0.0.0.0", 0  # placeholders — SLA-join record

            # Sign affiliation
            affil_msg = f"{worker_id}:{ORCH_HOTKEY}"
            signature = w.hotkey.sign(affil_msg.encode()).hex()

            # POST SLA join
            H = await orch_sig_headers(orch_bundle, "write")
            body = {
                "worker_id": worker_id,
                "hotkey": worker_hotkey,
                "ip": ip,
                "port": port,
                "signature": signature,
            }
            r = await c.post(f"{BEAMCORE}/sla/orchestrators/{ORCH_UID}/workers/join",
                             headers=H, json=body)
            status = r.status_code
            txt = r.text[:300]
            if status in (200, 201):
                print(f"[OK]   {hname} → worker {worker_id[:18]} joined (ip={ip}:{port})")
                ok += 1
            else:
                print(f"[FAIL] {hname} → worker {worker_id[:18]}: {status} {txt}")
                fail += 1

        print(f"\nSummary: {ok} joined, {fail} failed (of {len(WORKERS)})")


asyncio.run(main())
