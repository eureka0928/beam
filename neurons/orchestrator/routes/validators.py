"""
Validator API Routes

Endpoints that validators call on orchestrators for proof verification,
spot-checking, and bandwidth challenges.
"""

import base64
import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, Request

from core.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/validators", tags=["validators"])


@router.get("/proof-ids")
async def get_proof_ids(
    x_validator_hotkey: Optional[str] = Header(None),
):
    """
    Return proof task IDs for the current epoch.

    Validators call this to get a list of proof IDs they can spot-check.
    """
    orchestrator = get_orchestrator()
    epoch = orchestrator.current_epoch

    proof_ids = []
    for e in [epoch, epoch - 1]:
        for proof in orchestrator.epoch_proofs.get(e, []):
            task_id = getattr(proof, "task_id", None)
            if task_id:
                proof_ids.append(task_id)

    logger.info(
        f"Validator {(x_validator_hotkey or 'unknown')[:16]}... "
        f"requested proof-ids: {len(proof_ids)} proofs (epoch {epoch})"
    )

    return {"proof_ids": proof_ids, "epoch": epoch}


@router.get("/summary")
async def get_summary(
    x_validator_hotkey: Optional[str] = Header(None),
):
    """
    Return work summary for the current epoch.
    """
    orchestrator = get_orchestrator()
    epoch = orchestrator.current_epoch

    epoch_proof_list = orchestrator.epoch_proofs.get(epoch, [])
    proof_count = len(epoch_proof_list)
    total_bytes = sum(getattr(p, "bytes_relayed", 0) for p in epoch_proof_list)

    # Worker stats
    active_workers = 0
    worker_regions = {}
    for w in orchestrator.workers.values():
        if hasattr(w, "status") and str(w.status) != "WorkerStatus.OFFLINE":
            active_workers += 1
            region = getattr(w, "region", "unknown") or "unknown"
            worker_regions[region] = worker_regions.get(region, 0) + 1

    # Bandwidth stats from workers
    bandwidths = [
        w.bandwidth_ema for w in orchestrator.workers.values()
        if hasattr(w, "bandwidth_ema") and w.bandwidth_ema > 0
    ]
    avg_bandwidth = sum(bandwidths) / len(bandwidths) if bandwidths else 0.0

    # Latency from proofs
    latencies = []
    for p in epoch_proof_list:
        start = getattr(p, "start_time_us", 0)
        end = getattr(p, "end_time_us", 0)
        if start and end and end > start:
            latencies.append((end - start) / 1000.0)  # us to ms
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

    # Success rate
    total_tasks = orchestrator.total_tasks_completed
    failed = getattr(orchestrator, "total_tasks_failed", 0)
    success_rate = total_tasks / (total_tasks + failed) if (total_tasks + failed) > 0 else 0.0

    # Sign the summary
    sig_message = f"{orchestrator.hotkey}:{epoch}:{proof_count}:{total_bytes}"
    orchestrator_signature = ""
    if orchestrator.wallet:
        try:
            sig = orchestrator.wallet.hotkey.sign(sig_message.encode())
            orchestrator_signature = sig.hex() if isinstance(sig, bytes) else str(sig)
        except Exception:
            pass

    summary = {
        "epoch": epoch,
        "orchestrator_hotkey": orchestrator.hotkey,
        "total_tasks": total_tasks,
        "successful_tasks": total_tasks,
        "total_bytes_relayed": orchestrator.total_bytes_relayed,
        "active_workers": active_workers,
        "avg_bandwidth_mbps": round(avg_bandwidth, 2),
        "avg_latency_ms": round(avg_latency, 2),
        "success_rate": round(success_rate, 4),
        "proof_count": proof_count,
        "worker_regions": worker_regions,
        "orchestrator_signature": orchestrator_signature,
    }

    logger.info(
        f"Validator {(x_validator_hotkey or 'unknown')[:16]}... "
        f"requested summary: {proof_count} proofs, {total_bytes} bytes (epoch {epoch})"
    )

    return summary


@router.post("/challenge")
async def accept_challenge(
    request: Request,
    x_validator_hotkey: Optional[str] = Header(None),
):
    """
    Accept a bandwidth challenge from a validator.
    """
    data = await request.json()
    challenge_id = data.get("challenge_id", "unknown")

    logger.info(
        f"Validator {(x_validator_hotkey or 'unknown')[:16]}... "
        f"sent challenge: {challenge_id}, size={data.get('chunk_size', 0)}"
    )

    return {"accepted": True, "challenge_id": challenge_id}


@router.post("/challenge/data")
async def challenge_data(
    request: Request,
    x_validator_hotkey: Optional[str] = Header(None),
):
    """
    Receive challenge data from validator and return relay proof.

    Validator sends base64-encoded chunk_data. We decode it to prove
    we received the full payload and measure bandwidth.
    """
    start_time = time.time()

    data = await request.json()
    challenge_id = data.get("challenge_id", "unknown")
    chunk_data_b64 = data.get("chunk_data", "")

    # Decode to prove we received the full data
    try:
        chunk_bytes = base64.b64decode(chunk_data_b64)
        bytes_relayed = len(chunk_bytes)
    except Exception:
        bytes_relayed = 0

    end_time = time.time()
    duration_s = max(end_time - start_time, 0.001)
    bandwidth_mbps = (bytes_relayed * 8) / (duration_s * 1_000_000)

    logger.info(
        f"Validator {(x_validator_hotkey or 'unknown')[:16]}... "
        f"challenge data: {challenge_id}, {bytes_relayed} bytes, "
        f"{bandwidth_mbps:.1f} Mbps"
    )

    return {
        "success": True,
        "challenge_id": challenge_id,
        "bytes_relayed": bytes_relayed,
        "bandwidth_mbps": round(bandwidth_mbps, 2),
    }
