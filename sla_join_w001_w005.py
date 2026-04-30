"""SLA-join beam-w001..w005 (beam-01/worker-01..05) to UIDs 164, 104, 189 only."""
import asyncio, time, uuid, httpx, bittensor as bt
import sys
sys.path.insert(0, "/root/beam")
from sla_join_all import ORCHS, orch_sig_headers, BEAMCORE


TARGET_UIDS = {164, 104, 189}
WORKERS = [("beam-01", f"worker-{i:02}") for i in range(1, 6)]  # worker-01..05


async def main():
    selected = [o for o in ORCHS if o["uid"] in TARGET_UIDS]
    orch_wallets = {}
    for o in selected:
        w = bt.Wallet(name=o["wallet"], hotkey=o["hotkey_name"])
        _ = w.hotkey
        orch_wallets[o["uid"]] = w

    worker_wallets = {}
    for wname, hname in WORKERS:
        w = bt.Wallet(name=wname, hotkey=hname)
        _ = w.hotkey
        worker_wallets[hname] = w

    ok = fail = skip = 0
    async with httpx.AsyncClient(timeout=30) as c:
        for hname, ww in worker_wallets.items():
            worker_hk = ww.hotkey.ss58_address
            for orch in selected:
                ow = orch_wallets[orch["uid"]]
                r = await c.get(
                    f"{BEAMCORE}/sla/workers/{worker_hk}/membership",
                    headers={"X-Api-Key": orch["api_key"]},
                )
                if r.status_code != 200:
                    print(f"[SKIP] {hname} → UID {orch['uid']}: membership {r.status_code} {r.text[:100]}")
                    skip += 1
                    continue
                worker_id = r.json().get("worker_id") or worker_hk
                affil_msg = f"{worker_id}:{orch['hk']}"
                signature = ww.hotkey.sign(affil_msg.encode()).hex()
                H = orch_sig_headers(ow, orch, "write")
                r = await c.post(
                    f"{BEAMCORE}/sla/orchestrators/{orch['uid']}/workers/join",
                    headers=H,
                    json={"worker_id": worker_id, "hotkey": worker_hk, "ip": "0.0.0.0", "port": 0, "signature": signature},
                )
                if r.status_code in (200, 201):
                    print(f"[OK]   {hname} ({worker_id[:12]}) → UID {orch['uid']}")
                    ok += 1
                else:
                    print(f"[FAIL] {hname} → UID {orch['uid']}: {r.status_code} {r.text[:200]}")
                    fail += 1
    print(f"\nSummary: {ok} joined, {fail} failed, {skip} skipped (of {len(WORKERS)*len(selected)})")


asyncio.run(main())
