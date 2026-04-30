"""
UID 164 unpaid task — payment retry to the CORRECT worker coldkey.
Task: task-d32c5eb8789541e0
Correct worker: worker_9e0eb93301e8 → coldkey 5CXSKaZV2rbj5KCs6Va5hTYD59dUwErggEfZvZD9nebbFF87
(First attempt used worker_57bbf16a16e0 per buggy payment-address response; got Recipient mismatch.)
"""
import asyncio
import time
import uuid
import httpx
import bittensor as bt


TASK_ID = "task-d32c5eb8789541e0"
WORKER_COLDKEY = "5CXSKaZV2rbj5KCs6Va5hTYD59dUwErggEfZvZD9nebbFF87"
UID = 164
WALLET_NAME = "beam-06"
WALLET_HOTKEY = "miner-06"
NETUID = 105
BEAMCORE_URL = "https://beamcore.b1m.ai"
ALPHA_AMOUNT_RAO = 500_000_000   # 0.5 ALPHA minimum


def _sig_headers(wb, action: str, api_key: str) -> dict:
    hk = wb.hotkey.ss58_address
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    msg = f"orchestrator_auth:{hk}:{ts}:{action}:{nonce}"
    return {
        "X-Hotkey": hk,
        "X-Orchestrator-Hotkey": hk,
        "X-Orchestrator-Uid": str(UID),
        "X-Orchestrator-Timestamp": ts,
        "X-Orchestrator-Nonce": nonce,
        "X-Orchestrator-Signature": wb.hotkey.sign(msg.encode()).hex(),
        "X-Orchestrator-Action": action,
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }


async def main() -> None:
    wallet = bt.Wallet(name=WALLET_NAME, hotkey=WALLET_HOTKEY)
    _ = wallet.hotkey
    ck = wallet.coldkey_file.get_keypair(password=WALLET_NAME)

    class _W:
        coldkey = ck
        hotkey = wallet.hotkey
    wb = _W()

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{BEAMCORE_URL}/auth/challenge",
                         json={"hotkey": wb.hotkey.ss58_address, "role": "orchestrator"})
        d = r.json()
        sig = "0x" + wb.hotkey.sign(d["message"].encode()).hex()
        r = await c.post(f"{BEAMCORE_URL}/auth/verify",
                         json={"challenge_id": d["challenge_id"],
                               "hotkey": wb.hotkey.ss58_address,
                               "signature": sig,
                               "key_name": f"repay-{uuid.uuid4().hex[:6]}"})
        api_key = r.json()["api_key"]
        print(f"[0] api_key acquired {api_key[:16]}...")

        r = await c.get(f"{BEAMCORE_URL}/pob/{TASK_ID}/validate-transfer",
                        headers=_sig_headers(wb, "request", api_key))
        print(f"[1] validate-transfer: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
        vt = r.json()
        if not vt.get("valid") or not vt.get("transfer_id"):
            raise SystemExit("validate-transfer failed")
        transfer_id = vt["transfer_id"]

    memo = f"{transfer_id}:{TASK_ID}"
    print(f"[2] on-chain transfer_stake → {WORKER_COLDKEY[:16]}...  memo={memo}  amount={ALPHA_AMOUNT_RAO}")

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
            "destination_coldkey": WORKER_COLDKEY,
            "hotkey": wb.hotkey.ss58_address,
            "origin_netuid": NETUID,
            "destination_netuid": NETUID,
            "alpha_amount": ALPHA_AMOUNT_RAO,
        },
    )
    batch = substrate.compose_call(
        call_module="Utility",
        call_function="batch_all",
        call_params={"calls": [remark, transfer]},
    )
    ext = substrate.create_signed_extrinsic(call=batch, keypair=wb.coldkey)
    receipt = substrate.submit_extrinsic(ext, wait_for_inclusion=True, wait_for_finalization=False)
    if not receipt.is_success:
        raise SystemExit(f"on-chain fail: {getattr(receipt, 'error_message', '?')}")
    tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
    print(f"    tx = {tx_hash}")

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{BEAMCORE_URL}/pob/{TASK_ID}/payment",
            headers=_sig_headers(wb, "write", api_key),
            json={"tx_hash": tx_hash, "amount_rao": ALPHA_AMOUNT_RAO},
        )
        print(f"[3] record payment: {r.status_code} {r.text[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
