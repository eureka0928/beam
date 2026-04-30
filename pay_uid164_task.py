"""
One-shot payment for UID 164 unpaid task.
Task: task-d32c5eb8789541e0  Worker: worker_9e0eb93301e8  Size: 100 MB
"""
import asyncio
import os
import time
import uuid
import httpx
import bittensor as bt


TASK_ID = "task-d32c5eb8789541e0"
WORKER_ID = "worker_9e0eb93301e8"
UID = 164
WALLET_NAME = "beam-06"
WALLET_HOTKEY = "miner-06"
NETUID = 105
BEAMCORE_URL = "https://beamcore.b1m.ai"
ALPHA_AMOUNT_RAO = 500_000_000   # 0.5 ALPHA minimum
API_KEY = ""   # acquired at runtime via /auth/challenge → /auth/verify


def auth_headers(wallet_bundle, action: str = "request") -> dict:
    hk = wallet_bundle.hotkey.ss58_address
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:{action}:{nonce}"
    sig = wallet_bundle.hotkey.sign(msg.encode()).hex()
    return {
        "X-Hotkey": hk,
        "X-Orchestrator-Hotkey": hk,
        "X-Orchestrator-Uid": str(UID),
        "X-Orchestrator-Timestamp": ts,
        "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": sig,
        "X-Orchestrator-Action": action,
        "X-Api-Key": API_KEY,
        "Content-Type": "application/json",
    }


async def acquire_api_key(wallet_bundle) -> str:
    """Challenge/verify flow to get a fresh API key for this orchestrator."""
    global API_KEY
    hk = wallet_bundle.hotkey.ss58_address
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{BEAMCORE_URL}/auth/challenge",
                         json={"hotkey": hk, "role": "orchestrator"})
        r.raise_for_status()
        data = r.json()
        cid, msg = data["challenge_id"], data["message"]
        sig = "0x" + wallet_bundle.hotkey.sign(msg.encode()).hex()
        r = await c.post(f"{BEAMCORE_URL}/auth/verify",
                         json={"challenge_id": cid, "hotkey": hk,
                               "signature": sig, "key_name": "payment-script"})
        if r.status_code == 409:
            raise SystemExit("ABORT: API key already exists; extract it from running process env")
        r.raise_for_status()
        data = r.json()
        if not data.get("success") or not data.get("api_key"):
            raise SystemExit(f"ABORT: auth/verify failed: {data}")
        API_KEY = data["api_key"]
        print(f"    acquired API key: {API_KEY[:16]}...")
        return API_KEY


async def main() -> None:
    wallet = bt.Wallet(name=WALLET_NAME, hotkey=WALLET_HOTKEY)
    _ = wallet.hotkey  # unlock hotkey (not encrypted)
    ck = wallet.coldkey_file.get_keypair(password=WALLET_NAME)

    class _W:
        coldkey = ck
        hotkey = wallet.hotkey
    wallet_bundle = _W()

    print(f"[0] acquire API key for {wallet.hotkey.ss58_address[:16]}...")
    await acquire_api_key(wallet_bundle)

    async with httpx.AsyncClient(timeout=30) as c:
        print(f"[1] payment-address for {TASK_ID}")
        r = await c.get(
            f"{BEAMCORE_URL}/orchestrators/tasks/{TASK_ID}/payment-address",
            headers=auth_headers(wallet_bundle),
        )
        print(f"    {r.status_code} {r.text[:300]}")
        r.raise_for_status()
        pa = r.json()
        pa_worker_id = pa.get("worker_id") or WORKER_ID
        amount_owed = pa.get("amount_owed") or ALPHA_AMOUNT_RAO
        if amount_owed < ALPHA_AMOUNT_RAO:
            amount_owed = ALPHA_AMOUNT_RAO

        print(f"[2] coldkey for worker {pa_worker_id}")
        r = await c.get(
            f"{BEAMCORE_URL}/orchestrators/workers/{pa_worker_id}/coldkey",
            headers=auth_headers(wallet_bundle),
        )
        print(f"    {r.status_code} {r.text[:300]}")
        r.raise_for_status()
        worker_coldkey = r.json().get("coldkey")
        if not worker_coldkey:
            raise SystemExit("ABORT: worker has no coldkey")

        print(f"[3] validate-transfer for {TASK_ID}")
        r = await c.get(
            f"{BEAMCORE_URL}/pob/{TASK_ID}/validate-transfer",
            headers=auth_headers(wallet_bundle),
        )
        print(f"    {r.status_code} {r.text[:400]}")
        r.raise_for_status()
        vt = r.json()
        transfer_id = vt.get("transfer_id")
        if not vt.get("valid") or not transfer_id:
            raise SystemExit("ABORT: validate-transfer invalid")

    memo = f"{transfer_id}:{TASK_ID}"
    print(f"[4] on-chain transfer_stake  dest={worker_coldkey[:16]}...  memo={memo}  amount_rao={amount_owed}")

    sub = bt.Subtensor(network="finney")
    substrate = sub.substrate

    remark = substrate.compose_call(
        call_module="System",
        call_function="remark_with_event",
        call_params={"remark": memo.encode()},
    )
    transfer = substrate.compose_call(
        call_module="SubtensorModule",
        call_function="transfer_stake",
        call_params={
            "destination_coldkey": worker_coldkey,
            "hotkey": wallet_bundle.hotkey.ss58_address,
            "origin_netuid": NETUID,
            "destination_netuid": NETUID,
            "alpha_amount": int(amount_owed),
        },
    )
    batch = substrate.compose_call(
        call_module="Utility",
        call_function="batch_all",
        call_params={"calls": [remark, transfer]},
    )
    ext = substrate.create_signed_extrinsic(call=batch, keypair=wallet_bundle.coldkey)
    receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True, wait_for_finalization=False)
    if not receipt.is_success:
        raise SystemExit(f"ABORT: on-chain failure: {getattr(receipt, 'error_message', '?')}")

    tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
    print(f"    on-chain tx = {tx_hash}")

    print(f"[5] record PoB payment")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{BEAMCORE_URL}/pob/{TASK_ID}/payment",
            headers=auth_headers(wallet_bundle, action="write"),
            json={"tx_hash": tx_hash, "amount_rao": int(amount_owed)},
        )
        print(f"    {r.status_code} {r.text[:500]}")

    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
