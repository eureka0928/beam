"""
Auto-pay unpaid tasks across our UIDs.

Polls BeamCore every POLL_INTERVAL seconds:
1. /dashboard/api/traffic-allocation → log compliance_score + combined_weight per UID.
2. /orchestrators/tasks?status=completed minus /payments/paid-task-ids → unpaid set.
3. For each unpaid task: resolve worker coldkey from the task record (NOT from
   payment-address which has returned stale worker_ids causing Recipient mismatch),
   validate-transfer, on-chain transfer_stake with {transfer_id}:{task_id} memo,
   record payment. Dedup locally so restarts don't double-pay.

Run under pm2:
    pm2 start /root/beam/.venv/bin/python --name beam-auto-pay -- /root/beam/auto_pay_unpaid.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Set

import bittensor as bt
import httpx


BEAMCORE_URL = "https://beamcore.b1m.ai"
NETUID = 105
ALPHA_AMOUNT_RAO = 500_000_000   # 0.5 ALPHA minimum transfer_stake
POLL_INTERVAL = 600              # 10 min
API_KEY_TTL = 23 * 3600          # refresh key before 24h


@dataclass
class OrchCfg:
    uid: int
    wallet_name: str
    hotkey_name: str
    password: str
    api_key: str


# API keys MUST match the ones set as BEAMCORE_API_KEY in the orchestrator's
# pm2 ecosystem config — sharing the same key prevents /auth/verify from
# invalidating the running orchestrator's session.
TARGETS = [
    OrchCfg(uid=69,  wallet_name="beam-02", hotkey_name="miner-02", password="beam-02",
            api_key="b1m_562d862c929dfdc397abf35aa83f417c5476f2118dd7a550"),
    OrchCfg(uid=102, wallet_name="beam-04", hotkey_name="miner-04", password="beam-04",
            api_key="b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1"),
    OrchCfg(uid=104, wallet_name="beam-05", hotkey_name="miner-05", password="beam-05",
            api_key="b1m_b13ec9065306b61fabac753cef947ee6eaf2a95f73585043"),
    OrchCfg(uid=226, wallet_name="beam-09", hotkey_name="miner-09", password="beam-09",
            api_key="b1m_056e5f94fbf9a4e8769f49c3bbf778b9e4a35971a9b03e6e"),
    # 2026-04-23: UID 102 restored after fresh α top-up (10α on miner-04).
    # 2026-04-26: UID 226 (beam-09) registered fresh, hoping for top-tier emission.
    # UID 77 removed 2026-04-20: coldkey dry.
    # UID 164 (beam-06) stopped 2026-04-23: 0 own α.
    # UID 189 deregistered 2026-04-22.
]

# Tasks we've already paid but BeamCore hasn't surfaced on paid-task-ids yet.
# Persist across process restarts via this sidecar file.
ALREADY_PAID_FILE = "/root/beam/auto_pay_already_paid.txt"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("/root/beam/auto_pay.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("auto-pay")


def _load_already_paid() -> Set[str]:
    if not os.path.exists(ALREADY_PAID_FILE):
        return set()
    with open(ALREADY_PAID_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def _append_already_paid(task_id: str) -> None:
    with open(ALREADY_PAID_FILE, "a") as f:
        f.write(task_id + "\n")


class OrchState:
    def __init__(self, cfg: OrchCfg) -> None:
        self.cfg = cfg
        self.wallet = bt.Wallet(name=cfg.wallet_name, hotkey=cfg.hotkey_name)
        _ = self.wallet.hotkey                                       # load hotkey
        # NaCl-encrypted coldkeys only decrypt via BT_WALLET_PASSWORD env at read time.
        # Use a subprocess per-wallet to isolate env and decryption state.
        self.coldkey = None  # Loaded lazily via subprocess when paying
        self.coldkey_ss58 = None
        try:
            import subprocess, json as _json
            result = subprocess.run(
                [sys.executable, "-c", f"""
import os, bittensor as bt, json
os.environ['BT_WALLET_PASSWORD'] = {cfg.password!r}
w = bt.Wallet(name={cfg.wallet_name!r}, hotkey={cfg.hotkey_name!r})
print(json.dumps({{'coldkey_ss58': w.coldkey.ss58_address}}))
"""],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-300:])
            self.coldkey_ss58 = _json.loads(result.stdout.strip().splitlines()[-1])["coldkey_ss58"]
            log.info(f"UID {cfg.uid}: coldkey verified {self.coldkey_ss58[:16]}...")
        except Exception as e:
            log.error(f"UID {cfg.uid}: coldkey decrypt failed: {e}")
            self.coldkey_ss58 = None  # will skip payments for this UID
        self.hotkey_ss58 = self.wallet.hotkey.ss58_address
        self.api_key: str = cfg.api_key     # shared with the running orchestrator
        self.seen_unpaid: Set[str] = set()                           # local dedup (attempted this run)

    async def api_key_ensure(self, client: httpx.AsyncClient) -> str:
        """No-op: API key is shared with the running orchestrator via env var."""
        return self.api_key

    def sig_headers(self, action: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex[:8]
        msg = f"orchestrator_auth:{self.hotkey_ss58}:{ts}:{action}:{nonce}"
        return {
            "X-Hotkey": self.hotkey_ss58,
            "X-Orchestrator-Hotkey": self.hotkey_ss58,
            "X-Orchestrator-Uid": str(self.cfg.uid),
            "X-Orchestrator-Timestamp": ts,
            "X-Orchestrator-Nonce": nonce,
            "X-Orchestrator-Signature": self.wallet.hotkey.sign(msg.encode()).hex(),
            "X-Orchestrator-Action": action,
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }


async def log_compliance() -> None:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{BEAMCORE_URL}/dashboard/api/traffic-allocation")
            allocs = {a["uid"]: a for a in r.json().get("allocations", [])}
        for cfg in TARGETS:
            a = allocs.get(cfg.uid)
            if not a:
                log.info(f"UID {cfg.uid}: not in allocation list")
                continue
            log.info(
                f"UID {cfg.uid} | compliance={a['compliance_score']:.3f} "
                f"sla={a['sla_score']:.2f} combined_w={a['combined_weight']:.3f} "
                f"traffic={a['traffic_pct']:.2f}% bytes24h={a['bytes_relayed_24h']:,} "
                f"workers={a['active_workers']}"
            )
    except Exception as e:
        log.warning(f"compliance poll failed: {e}")


# Owner (2026-04-23): unpaid tasks accumulate in BeamCore's dashboard and
# block emission. Previous 4h cap created a massive backlog. Cap extended
# to 3 days — long enough to drain backlog and pay current work, short
# enough to skip truly stale tasks that validate-transfer would reject.
PAY_RECENCY_SECONDS = 3 * 24 * 3600  # 3 days

def _task_age_seconds(t: dict) -> float:
    """Best-effort age in seconds from the task's completed/updated/created ts.

    BeamCore returns naive ISO timestamps (e.g. '2026-04-20T03:53:32.305034')
    without Z or offset. Treat those as UTC rather than failing.
    """
    from datetime import datetime, timezone
    for key in ("completed_at", "updated_at", "created_at"):
        ts = t.get(key)
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            continue
    return float("inf")  # unknown age → treat as too old, skip


async def find_unpaid(state: OrchState, client: httpx.AsyncClient,
                      locally_paid: Set[str]) -> list[dict]:
    headers = state.sig_headers("request")
    r = await client.get(f"{BEAMCORE_URL}/orchestrators/tasks?status=completed&limit=200",
                         headers=headers)
    r.raise_for_status()
    tasks = r.json().get("tasks") or []
    headers = state.sig_headers("request")
    r = await client.get(f"{BEAMCORE_URL}/orchestrators/payments/paid-task-ids",
                         headers=headers)
    r.raise_for_status()
    paid_ids = set(r.json().get("task_ids") or []) | locally_paid
    unpaid = [t for t in tasks
              if t.get("task_id") not in paid_ids
              and t.get("has_pob")
              and (t.get("bytes_relayed") or 0) > 0
              and _task_age_seconds(t) <= PAY_RECENCY_SECONDS]
    return unpaid


async def pay_task(state: OrchState, client: httpx.AsyncClient, task: dict) -> bool:
    task_id = task["task_id"]
    worker_id = task.get("worker_id")
    if not worker_id:
        log.warning(f"UID {state.cfg.uid} task {task_id[:16]}: no worker_id in task record, skip")
        return False

    # worker coldkey from the TASK record's worker_id (ground truth, not payment-address)
    headers = state.sig_headers("request")
    r = await client.get(f"{BEAMCORE_URL}/orchestrators/workers/{worker_id}/coldkey",
                         headers=headers)
    if r.status_code == 404:
        # Worker unregistered after completing. No way to pay — mark paid locally so we
        # don't keep hammering the endpoint every 10 min.
        log.info(f"UID {state.cfg.uid} task {task_id[:16]}: worker {worker_id[:18]} no longer "
                 f"registered (404); skipping permanently")
        _append_already_paid(task_id)
        return False
    if r.status_code != 200:
        log.warning(f"UID {state.cfg.uid} task {task_id[:16]}: coldkey lookup {r.status_code} {r.text[:200]}")
        return False
    worker_coldkey = r.json().get("coldkey")
    if not worker_coldkey:
        log.warning(f"UID {state.cfg.uid} task {task_id[:16]}: worker has no coldkey")
        _append_already_paid(task_id)
        return False

    headers = state.sig_headers("request")
    r = await client.get(f"{BEAMCORE_URL}/pob/{task_id}/validate-transfer", headers=headers)
    if r.status_code != 200:
        log.warning(f"UID {state.cfg.uid} task {task_id[:16]}: validate-transfer {r.status_code} {r.text[:200]}")
        return False
    vt = r.json()
    if not vt.get("valid") or not vt.get("transfer_id"):
        err = vt.get("error") or ""
        log.info(f"UID {state.cfg.uid} task {task_id[:16]}: validate-transfer invalid ({err}) — skip")
        if "already paid" in err and "chain-verified" in err:
            _append_already_paid(task_id)
        return False
    transfer_id = vt["transfer_id"]

    if not state.coldkey_ss58:
        log.warning(f"UID {state.cfg.uid} task {task_id[:16]}: coldkey unavailable — skip payment")
        return False

    memo = f"{transfer_id}:{task_id}"
    log.info(f"UID {state.cfg.uid} task {task_id[:16]}: paying worker={worker_id[:18]} "
             f"dest={worker_coldkey[:16]}... memo={memo}")

    # Submit on-chain transfer via subprocess (isolates BT_WALLET_PASSWORD per-wallet)
    import subprocess
    script = f"""
import os, bittensor as bt, json, sys
os.environ['BT_WALLET_PASSWORD'] = {state.cfg.password!r}
w = bt.Wallet(name={state.cfg.wallet_name!r}, hotkey={state.cfg.hotkey_name!r})
sub = bt.Subtensor(network='finney')
try:
    s = sub.substrate
    remark = s.compose_call('System', 'remark_with_event', {{'remark': {memo!r}.encode()}})
    transfer = s.compose_call('SubtensorModule', 'transfer_stake', {{
        'destination_coldkey': {worker_coldkey!r},
        'hotkey': {state.hotkey_ss58!r},
        'origin_netuid': {NETUID},
        'destination_netuid': {NETUID},
        'alpha_amount': {ALPHA_AMOUNT_RAO},
    }})
    batch = s.compose_call('Utility', 'batch_all', {{'calls': [remark, transfer]}})
    ext = s.create_signed_extrinsic(call=batch, keypair=w.coldkey)
    r = s.submit_extrinsic(ext, wait_for_inclusion=True, wait_for_finalization=False)
    if r.is_success:
        print(json.dumps({{'ok': True, 'tx_hash': f'{{r.extrinsic_hash}}:{{r.block_hash}}'}}))
    else:
        print(json.dumps({{'ok': False, 'error': str(getattr(r, 'error_message', '?'))}}))
finally:
    try: sub.close()
    except: pass
"""
    try:
        res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            log.error(f"UID {state.cfg.uid} task {task_id[:16]}: subprocess FAIL: {res.stderr[-300:]}")
            return False
        import json as _json
        out = _json.loads(res.stdout.strip().splitlines()[-1])
        if not out.get("ok"):
            log.error(f"UID {state.cfg.uid} task {task_id[:16]}: on-chain FAIL: {out.get('error')}")
            return False
        tx_hash = out["tx_hash"]
    except Exception as e:
        log.error(f"UID {state.cfg.uid} task {task_id[:16]}: payment subprocess error: {e}")
        return False

    headers = state.sig_headers("write")
    r = await client.post(f"{BEAMCORE_URL}/pob/{task_id}/payment", headers=headers,
                          json={"tx_hash": tx_hash, "amount_rao": ALPHA_AMOUNT_RAO})
    body = r.text[:400]
    log.info(f"UID {state.cfg.uid} task {task_id[:16]}: record-payment {r.status_code} {body}")
    ok = r.status_code == 200
    if ok:
        _append_already_paid(task_id)
    return ok


async def sweep_one(state: OrchState, locally_paid: Set[str]) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await state.api_key_ensure(client)
            unpaid = await find_unpaid(state, client, locally_paid)
        except Exception as e:
            log.warning(f"UID {state.cfg.uid}: find_unpaid failed: {e}")
            return
        if not unpaid:
            return
        new_ids = [t["task_id"] for t in unpaid if t["task_id"] not in state.seen_unpaid]
        log.info(f"UID {state.cfg.uid}: {len(unpaid)} unpaid tasks ({len(new_ids)} new this run)")
        for task in unpaid:
            tid = task["task_id"]
            if tid in state.seen_unpaid:
                continue
            state.seen_unpaid.add(tid)
            try:
                await pay_task(state, client, task)
            except Exception as e:
                log.error(f"UID {state.cfg.uid} task {tid[:16]}: unhandled: {e}")


async def main() -> None:
    log.info(f"auto-pay starting: {len(TARGETS)} UIDs, interval={POLL_INTERVAL}s")
    try:
        states = [OrchState(cfg) for cfg in TARGETS]
    except SystemExit as e:
        log.error(str(e))
        raise
    while True:
        locally_paid = _load_already_paid()
        await log_compliance()
        for st in states:
            await sweep_one(st, locally_paid)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
