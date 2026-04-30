"""
SLA-join each of 5 workers (tony-012..016) to 6 orchestrators (UIDs 69, 77, 102, 104, 164, 189).
No worker restart required.
"""
import asyncio
import time
import uuid
import httpx
import bittensor as bt


BEAMCORE = "https://beamcore.b1m.ai"

ORCHS = [
    {"uid": 69,  "wallet": "beam-02", "hotkey_name": "miner-02",
     "hk": "5GZUs7uztXt1WPwL6KQhsVznrC1V3yoFtYLaLFEUVT9xdjLC",
     "api_key": "b1m_562d862c929dfdc397abf35aa83f417c5476f2118dd7a550"},
    {"uid": 77,  "wallet": "beam-03", "hotkey_name": "miner-03",
     "hk": "5D82k8jUe87KhhmZdH9z3X8bBQQM6jjRcixhNiNCjnFS1DsP",
     "api_key": "b1m_26a9b6411fc1295c18ec83f1c4237834bf47004b5fbb0d52"},
    {"uid": 102, "wallet": "beam-04", "hotkey_name": "miner-04",
     "hk": "5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3",
     "api_key": "b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1"},
    {"uid": 104, "wallet": "beam-05", "hotkey_name": "miner-05",
     "hk": "5Eh2KCFgfic6gS5XQ4zBwhfstC1s8cduwkka1Qe3ot91mt55",
     "api_key": "b1m_b13ec9065306b61fabac753cef947ee6eaf2a95f73585043"},
    {"uid": 164, "wallet": "beam-06", "hotkey_name": "miner-06",
     "hk": "5FhoJNyfya2r2Fk9inwfoG9EmvYdkC3vb9kvrVMFUB1ijsP4",
     "api_key": "b1m_b36dbd9ee498c0b7197cb9ee5d163149e3270eb8fe0d5454"},
    {"uid": 189, "wallet": "gen",     "hotkey_name": "gen-09",
     "hk": "5DNtwiMd3ZWYCiWYq3kB6bGkaFkH5BNqrPAY92yi6tXfcgPW",
     "api_key": "b1m_6ead031d46336f69541afaa9d91dc85efaf311bbe644f8cc"},
]

WORKERS = [("tony", f"tony-{i:03}") for i in range(12, 17)]   # tony-012..016


def orch_sig_headers(orch_w, orch, action="write"):
    hk = orch_w.hotkey.ss58_address
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:{action}:{nonce}"
    return {
        "X-Hotkey": hk, "X-Orchestrator-Hotkey": hk,
        "X-Orchestrator-Uid": str(orch["uid"]),
        "X-Orchestrator-Timestamp": ts, "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": orch_w.hotkey.sign(msg.encode()).hex(),
        "X-Orchestrator-Action": action,
        "X-Api-Key": orch["api_key"],
        "Content-Type": "application/json",
    }


async def main():
    orch_wallets = {}
    for o in ORCHS:
        w = bt.Wallet(name=o["wallet"], hotkey=o["hotkey_name"])
        _ = w.hotkey
        assert w.hotkey.ss58_address == o["hk"], f"orch hotkey mismatch UID {o['uid']}"
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
            for orch in ORCHS:
                ow = orch_wallets[orch["uid"]]
                # Resolve SLA worker_id via membership endpoint (uses worker hotkey as worker_id)
                r = await c.get(
                    f"{BEAMCORE}/sla/workers/{worker_hk}/membership",
                    headers={"X-Api-Key": orch["api_key"]},
                )
                if r.status_code != 200:
                    print(f"[SKIP] {hname} hk={worker_hk[:12]} → UID {orch['uid']}: membership {r.status_code}")
                    skip += 1
                    continue
                mem = r.json()
                worker_id = mem.get("worker_id") or worker_hk

                affil_msg = f"{worker_id}:{orch['hk']}"
                signature = ww.hotkey.sign(affil_msg.encode()).hex()

                body = {
                    "worker_id": worker_id,
                    "hotkey": worker_hk,
                    "ip": "0.0.0.0",
                    "port": 0,
                    "signature": signature,
                }
                H = orch_sig_headers(ow, orch, "write")
                r = await c.post(
                    f"{BEAMCORE}/sla/orchestrators/{orch['uid']}/workers/join",
                    headers=H, json=body,
                )
                if r.status_code in (200, 201):
                    print(f"[OK]   {hname} ({worker_id[:12]}) → UID {orch['uid']}")
                    ok += 1
                else:
                    print(f"[FAIL] {hname} → UID {orch['uid']}: {r.status_code} {r.text[:200]}")
                    fail += 1

    total = len(WORKERS) * len(ORCHS)
    print(f"\nSummary: {ok} joined, {fail} failed, {skip} skipped (of {total})")


asyncio.run(main())
