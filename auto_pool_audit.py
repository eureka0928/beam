#!/usr/bin/env python3
"""
Pool-hygiene audit sidecar (read-only).

Polls /orchestrators/workers per UID every 5 min and reports workers that
cross scorer hard-exclude or soft-quarantine policy thresholds. Does NOT
call DELETE (BeamCore restricts affiliation removal to worker hotkeys only).

Purpose: visibility. We want to see WHICH workers BeamCore keeps listing in
our pool despite being unusable. The in-process scorer already excludes
them from picks; this sidecar surfaces the list so operators can decide
whether to ping the worker owner, escalate to BeamCore, or raise thresholds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import bittensor as bt
import httpx

BEAMCORE = "https://beamcore.b1m.ai"

# Policy thresholds mirror worker_scorer.py
HASH_MISMATCH_THRESHOLD = 1
ZERO_BYTES_THRESHOLD = 3
REJECTED_THRESHOLD = 20
MIN_DELIVERY_RATIO = 0.3
MIN_REPUTATION = 0.2
MIN_TASKS_FOR_DR = 20
POLL_INTERVAL_S = 300


@dataclass
class OrchCfg:
    uid: int
    wallet_name: str
    hotkey_name: str
    password: str
    api_key: str


TARGETS: List[OrchCfg] = [
    OrchCfg(uid=69,  wallet_name="beam-02", hotkey_name="miner-02", password="beam-02",
            api_key="b1m_562d862c929dfdc397abf35aa83f417c5476f2118dd7a550"),
    OrchCfg(uid=102, wallet_name="beam-04", hotkey_name="miner-04", password="beam-04",
            api_key="b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1"),
    OrchCfg(uid=104, wallet_name="beam-05", hotkey_name="miner-05", password="beam-05",
            api_key="b1m_b13ec9065306b61fabac753cef947ee6eaf2a95f73585043"),
    # 2026-04-23: UID 102 restored with 10α.
]

log = logging.getLogger("pool-audit")


class Orch:
    def __init__(self, cfg: OrchCfg) -> None:
        os.environ["BT_WALLET_PASSWORD"] = cfg.password
        self.cfg = cfg
        self.wallet = bt.Wallet(name=cfg.wallet_name, hotkey=cfg.hotkey_name)
        _ = self.wallet.hotkey
        self.hotkey_ss58 = self.wallet.hotkey.ss58_address

    def sig_headers(self) -> Dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex[:8]
        action = "request"
        msg = f"orchestrator_auth:{self.hotkey_ss58}:{ts}:{action}:{nonce}"
        return {
            "X-Hotkey": self.hotkey_ss58,
            "X-Orchestrator-Hotkey": self.hotkey_ss58,
            "X-Orchestrator-Uid": str(self.cfg.uid),
            "X-Orchestrator-Timestamp": ts,
            "X-Orchestrator-Nonce": nonce,
            "X-Orchestrator-Signature": self.wallet.hotkey.sign(msg.encode()).hex(),
            "X-Orchestrator-Action": action,
            "X-Api-Key": self.cfg.api_key,
        }


def classify(w: Dict[str, Any]) -> Optional[str]:
    fr = w.get("failure_reasons") or {}
    hm = int(fr.get("hash_mismatch", 0) or 0)
    zb = int(fr.get("zero_bytes", 0) or 0)
    rj = int(fr.get("rejected", 0) or 0)
    tm = int(fr.get("timeout", 0) or 0)
    we = int(fr.get("worker_error", 0) or 0)
    if hm >= HASH_MISMATCH_THRESHOLD:
        return f"hash_mismatch={hm}"
    if zb >= ZERO_BYTES_THRESHOLD:
        return f"zero_bytes={zb}"
    if rj >= REJECTED_THRESHOLD:
        return f"rejected={rj}"
    rep = float(w.get("reputation_score", 1.0) or 1.0)
    if rep < MIN_REPUTATION:
        return f"reputation={rep:.2f}"
    total = int(w.get("total_tasks", 0) or 0)
    dr = float(w.get("delivery_ratio", 1.0) or 1.0)
    if total >= MIN_TASKS_FOR_DR and dr < MIN_DELIVERY_RATIO:
        return f"delivery_ratio={dr:.2f} (total={total})"
    if tm + we >= 30:
        return f"soft_heavy (timeout={tm} worker_error={we})"
    return None


async def audit_uid(client: httpx.AsyncClient, orch: Orch) -> None:
    uid = orch.cfg.uid
    try:
        r = await client.get(
            f"{BEAMCORE}/orchestrators/workers",
            params={"limit": 200, "status": "active"},
            headers=orch.sig_headers(),
        )
    except Exception as e:
        log.warning(f"UID {uid}: list workers error: {e}")
        return
    if r.status_code != 200:
        log.warning(f"UID {uid}: list workers HTTP {r.status_code}: {r.text[:120]}")
        return
    workers = r.json().get("workers", []) or []
    bad: List[Tuple[str, str, Dict[str, Any]]] = []
    for w in workers:
        wid = w.get("worker_id")
        if not wid:
            continue
        reason = classify(w)
        if reason:
            bad.append((wid, reason, w))
    if not bad:
        log.info(f"UID {uid}: pool={len(workers)} bad=0")
        return
    log.info(f"UID {uid}: pool={len(workers)} bad={len(bad)}")
    for wid, reason, w in sorted(bad, key=lambda x: -sum((x[2].get('failure_reasons') or {}).values())):
        rep = float(w.get('reputation_score', 1.0) or 1.0)
        dr = float(w.get('delivery_ratio', 1.0) or 1.0)
        total = int(w.get('total_tasks', 0) or 0)
        log.info(f"  - {wid[:20]} rep={rep:.2f} dr={dr:.2f} total={total} | {reason}")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    orchs = [Orch(cfg) for cfg in TARGETS]
    log.info(f"pool-audit started; {len(orchs)} UIDs, poll={POLL_INTERVAL_S}s (read-only)")
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                await asyncio.gather(
                    *[audit_uid(client, o) for o in orchs],
                    return_exceptions=True,
                )
            except Exception as e:
                log.error(f"cycle error: {e}")
            await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
