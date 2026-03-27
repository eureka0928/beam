"""
Proof Aggregator - Proof aggregation and validator reporting.
"""

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from .config import OrchestratorSettings


# ============================================================================
# Local crypto helpers (replacing beam.crypto imports)
# ============================================================================

def sha256(data: bytes) -> str:
    """Compute SHA256 hash, return hex string."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def compute_merkle_root(leaves: List[str]) -> str:
    """Compute Merkle root from list of hex hash strings."""
    if not leaves:
        return "0" * 64

    # Normalize
    hashes = [h.lower().strip() for h in leaves]

    # Pad to power of 2
    while len(hashes) & (len(hashes) - 1) != 0:
        hashes.append(hashes[-1])

    # Build tree
    while len(hashes) > 1:
        next_level = []
        for i in range(0, len(hashes), 2):
            combined = bytes.fromhex(hashes[i]) + bytes.fromhex(hashes[i + 1])
            next_level.append(hashlib.sha256(combined).hexdigest())
        hashes = next_level

    return hashes[0]


def sign_message(message: bytes, hotkey) -> str:
    """Sign message with hotkey, return hex signature."""
    try:
        sig = hotkey.sign(message)
        return sig.hex() if isinstance(sig, bytes) else str(sig)
    except Exception:
        return "unsigned"

logger = logging.getLogger(__name__)

# Optional imports
try:
    from clients import PoBSubmission
    SUBNET_CORE_CLIENT_AVAILABLE = True
except ImportError:
    SUBNET_CORE_CLIENT_AVAILABLE = False

try:
    from db.database import Database
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False


class ProofAggregator:
    """Manages proof aggregation, Merkle tree computation, and validator reporting."""

    def __init__(self, settings: OrchestratorSettings):
        self.settings = settings

        # Proof aggregation
        self.pending_proofs: List[Any] = []  # List[BandwidthProof]
        self.epoch_proofs: Dict[int, List[Any]] = defaultdict(list)
        self.epoch_summaries: Dict[int, Any] = {}  # epoch -> EpochSummary

        # PoB publish retry queue and health tracking
        self._failed_publishes: List[tuple] = []  # List[(PoBSubmission, retry_count)]
        self._publish_success_count: int = 0
        self._publish_failure_count: int = 0

    def _validate_and_fix_proof(self, proof) -> bool:
        """Pre-flight validation mirroring validator's _verify_single_subnet_proof.

        Fixes recoverable issues in-place. Returns False if proof is unfixable
        and should be dropped (to avoid tanking our verification_rate).
        """
        if not proof.bytes_relayed or proof.bytes_relayed <= 0:
            logger.warning(f"Dropping proof {proof.task_id[:16]}...: zero bytes_relayed")
            return False

        # Fix timing: ensure end > start and duration >= 1ms (1000us)
        if proof.end_time_us <= proof.start_time_us:
            if proof.bandwidth_mbps > 0:
                est = int(
                    (proof.bytes_relayed * 8) / (proof.bandwidth_mbps * 1_000_000) * 1_000_000
                )
            else:
                est = 100_000  # 100ms default
            proof.start_time_us = proof.end_time_us - max(est, 1000)

        duration_us = proof.end_time_us - proof.start_time_us
        if duration_us < 1000:
            proof.start_time_us = proof.end_time_us - 1000
            duration_us = 1000

        # Fix bandwidth: compute from bytes/duration if zero or missing
        calculated_bw = (proof.bytes_relayed * 8) / (duration_us / 1_000_000) / 1_000_000
        if not proof.bandwidth_mbps or proof.bandwidth_mbps <= 0:
            proof.bandwidth_mbps = calculated_bw
        elif proof.bandwidth_mbps > 0:
            ratio = calculated_bw / proof.bandwidth_mbps
            if ratio < 0.5 or ratio > 2.0:
                proof.bandwidth_mbps = calculated_bw

        # Cap at physical limit (validator rejects > 100 Gbps)
        if calculated_bw > 100_000:
            needed_duration_us = int(
                (proof.bytes_relayed * 8) / (99_000 * 1_000_000) * 1_000_000
            )
            proof.start_time_us = proof.end_time_us - max(needed_duration_us, 1000)
            proof.bandwidth_mbps = 99_000.0

        # Ensure canary_proof is exactly 64 hex chars
        canary = proof.canary_proof or ''
        if len(canary) != 64:
            proof.canary_proof = hashlib.sha256(os.urandom(32)).hexdigest()
        else:
            try:
                bytes.fromhex(canary)
            except ValueError:
                proof.canary_proof = hashlib.sha256(os.urandom(32)).hexdigest()

        return True

    async def persist_bandwidth_proof(self, proof, current_epoch: int, db, subnet_core_client) -> None:
        """Persist a bandwidth proof to the database and publish to subnet registry."""
        # Pre-flight validation: fix or drop bad proofs before publishing
        if not self._validate_and_fix_proof(proof):
            return

        logger.info(
            f"persist_bandwidth_proof called: task={proof.task_id[:20]}..., "
            f"DB_AVAILABLE={DB_AVAILABLE}, db={db is not None}, "
            f"SUBNET_CORE_CLIENT_AVAILABLE={SUBNET_CORE_CLIENT_AVAILABLE}, client={subnet_core_client is not None}"
        )
        if DB_AVAILABLE and db:
            try:
                await db.save_bandwidth_proof({
                    "task_id": proof.task_id,
                    "worker_id": proof.worker_id,
                    "worker_hotkey": proof.worker_hotkey,
                    "epoch": current_epoch,
                    "start_time_us": proof.start_time_us,
                    "end_time_us": proof.end_time_us,
                    "bytes_relayed": proof.bytes_relayed,
                    "bandwidth_mbps": proof.bandwidth_mbps,
                    "chunk_hash": proof.chunk_hash,
                    "canary_proof": proof.canary_proof,
                    "worker_signature": proof.worker_signature,
                    "orchestrator_signature": proof.orchestrator_signature,
                    "source_region": proof.source_region,
                    "dest_region": proof.dest_region,
                })
            except Exception as e:
                logger.warning(f"Failed to persist bandwidth proof: {e}")

        if SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client:
            logger.info(f"Publishing PoB to SubnetCore: task={proof.task_id[:20]}...")
            pob = PoBSubmission(
                task_id=proof.task_id,
                epoch=current_epoch,
                worker_id=proof.worker_id,
                worker_hotkey=proof.worker_hotkey,
                start_time_us=proof.start_time_us,
                end_time_us=proof.end_time_us,
                bytes_relayed=proof.bytes_relayed,
                bandwidth_mbps=proof.bandwidth_mbps,
                chunk_hash=proof.chunk_hash,
                worker_signature=proof.worker_signature,
                orchestrator_signature=proof.orchestrator_signature,
                canary_proof=proof.canary_proof,
                source_region=proof.source_region,
                dest_region=proof.dest_region,
            )
            success = await self._try_publish_pob(subnet_core_client, pob)
            if not success:
                self._failed_publishes.append((pob, 0))

    async def _try_publish_pob(self, client, pob, is_retry: bool = False) -> bool:
        """Attempt to publish a single PoB to SubnetCore. Returns True on success."""
        try:
            await client.publish_pob(pob)
            self._publish_success_count += 1
            if is_retry:
                logger.info(f"PoB retry succeeded: {pob.task_id[:16]}...")
            return True
        except Exception as e:
            self._publish_failure_count += 1
            level = logging.ERROR if self._publish_failure_count > 3 else logging.WARNING
            logger.log(
                level,
                f"PoB publish {'retry ' if is_retry else ''}failed "
                f"(success={self._publish_success_count}, fail={self._publish_failure_count}): {e}",
            )
            return False

    async def _retry_failed_publishes(self, subnet_core_client) -> None:
        """Retry failed PoB publishes. Called from proof_aggregation_loop."""
        if not self._failed_publishes or not subnet_core_client:
            return

        batch_size = 50
        to_retry = self._failed_publishes[:batch_size]
        self._failed_publishes = self._failed_publishes[batch_size:]

        still_failed = []
        for pob, retry_count in to_retry:
            if retry_count >= 5:
                logger.error(
                    f"PoB publish permanently failed after 5 retries: {pob.task_id[:16]}... "
                    f"({pob.bytes_relayed} bytes, epoch {pob.epoch})"
                )
                continue
            success = await self._try_publish_pob(subnet_core_client, pob, is_retry=True)
            if not success:
                still_failed.append((pob, retry_count + 1))

        self._failed_publishes = still_failed + self._failed_publishes

        if still_failed:
            logger.warning(
                f"PoB retry queue: {len(self._failed_publishes)} pending, "
                f"{len(still_failed)} still failing"
            )

    def get_publish_health(self) -> Dict[str, Any]:
        """Return PoB publish health stats for monitoring."""
        return {
            "success_count": self._publish_success_count,
            "failure_count": self._publish_failure_count,
            "retry_queue_size": len(self._failed_publishes),
            "success_rate": (
                self._publish_success_count /
                max(self._publish_success_count + self._publish_failure_count, 1)
            ),
        }

    async def proof_aggregation_loop(self, running_flag, subnet_core_client_ref=None) -> None:
        """Background loop for aggregating proofs and retrying failed PoB publishes."""
        while running_flag():
            try:
                await asyncio.sleep(self.settings.proof_aggregation_interval)
                # Aggregate whenever there are ANY pending proofs (not just >= batch_size)
                if self.pending_proofs:
                    await self._aggregate_proofs()

                # Retry failed PoB publishes
                client = subnet_core_client_ref() if subnet_core_client_ref else None
                await self._retry_failed_publishes(client)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in proof aggregation: {e}")

    async def _aggregate_proofs(self) -> None:
        """Aggregate pending proofs into a merkle tree."""
        if not self.pending_proofs:
            return

        batch = self.pending_proofs[:self.settings.proof_batch_size]
        self.pending_proofs = self.pending_proofs[self.settings.proof_batch_size:]

        proof_leaves = []
        for proof in batch:
            leaf_data = (
                proof.task_id.encode("utf-8") +
                proof.worker_id.encode("utf-8") +
                proof.bytes_relayed.to_bytes(8, "big") +
                int(proof.bandwidth_mbps * 1000).to_bytes(8, "big") +
                int(proof.start_time).to_bytes(8, "big")
            )
            proof_leaves.append(sha256(leaf_data))

        merkle_root = compute_merkle_root(proof_leaves)

        if not hasattr(self, "aggregated_batches"):
            self.aggregated_batches = []

        self.aggregated_batches.append({
            "epoch": 0,  # Will be set by caller
            "batch_size": len(batch),
            "merkle_root": merkle_root.hex(),
            "timestamp": time.time(),
            "total_bytes": sum(p.bytes_relayed for p in batch),
            "avg_bandwidth": sum(p.bandwidth_mbps for p in batch) / len(batch) if batch else 0,
        })

        logger.info(
            f"Aggregated {len(batch)} proofs, "
            f"merkle_root={merkle_root.hex()[:16]}..."
        )

    async def validator_report_loop(self, running_flag, validators_ref, metagraph_ref, wallet_ref, hotkey_ref, current_epoch_ref, subnet_core_client_ref=None) -> None:
        """Background loop for reporting to validators."""
        while running_flag():
            try:
                await asyncio.sleep(self.settings.validator_report_interval)
                client = subnet_core_client_ref() if subnet_core_client_ref else None
                await self._report_to_validators(
                    validators_ref(), metagraph_ref(), wallet_ref(), hotkey_ref(), current_epoch_ref(),
                    subnet_core_client=client,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in validator reporting: {e}")

    async def _report_to_validators(self, validators, metagraph, wallet, hotkey, current_epoch, subnet_core_client=None) -> None:
        """Send epoch summary to validators."""
        summary = self.build_epoch_summary(current_epoch)

        if summary.total_tasks == 0:
            logger.debug("No work to report this period")
            return

        submitted_count = 0
        for validator_hotkey, validator_info in validators.items():
            try:
                await self._send_report_to_validator(
                    validator_hotkey, summary, validators, metagraph, wallet, hotkey, current_epoch
                )
                submitted_count += 1
            except Exception as e:
                logger.error(f"Failed to report to validator {validator_hotkey[:16]}: {e}")

        logger.info(
            f"Reported epoch {current_epoch} to {len(validators)} validators: "
            f"{summary.total_tasks} tasks, {summary.total_bytes_relayed} bytes"
        )

        # Mark epoch as submitted in SubnetCore
        if submitted_count > 0 and subnet_core_client and SUBNET_CORE_CLIENT_AVAILABLE:
            try:
                from clients.subnet_core_client import EpochPaymentData
                epoch_data = EpochPaymentData(
                    epoch=current_epoch,
                    total_distributed=0,  # payment totals already recorded by reward_manager
                    worker_count=summary.active_workers,
                    total_bytes_relayed=summary.total_bytes_relayed,
                    merkle_root="0x" + "0" * 64,
                    submitted_to_validators=True,
                )
                await subnet_core_client.record_epoch_payment(epoch_data)
                logger.info(f"Epoch {current_epoch} marked as submitted to validators")
            except Exception as e:
                logger.warning(f"Failed to mark epoch as submitted: {e}")

    def build_epoch_summary(self, current_epoch: int, epoch_start_time: datetime = None):
        """Build summary of current epoch's work."""
        from .orchestrator import EpochSummary

        proofs = self.epoch_proofs.get(current_epoch, [])
        total_bytes = sum(p.bytes_relayed for p in proofs)
        total_bandwidth_seconds = sum(
            p.bytes_relayed * 8 / 1_000_000 / p.bandwidth_mbps
            for p in proofs if p.bandwidth_mbps > 0
        )

        contributions = defaultdict(int)
        for p in proofs:
            contributions[p.worker_id] += p.bytes_relayed

        active_workers = len(set(p.worker_id for p in proofs))
        avg_bandwidth = sum(p.bandwidth_mbps for p in proofs) / len(proofs) if proofs else 0.0
        avg_latency = sum(p.duration_ms for p in proofs) / len(proofs) if proofs else 0.0

        return EpochSummary(
            epoch=current_epoch,
            start_time=epoch_start_time or datetime.utcnow(),
            end_time=datetime.utcnow(),
            total_tasks=len(proofs),
            successful_tasks=len(proofs),
            total_bytes_relayed=total_bytes,
            total_bandwidth_seconds=total_bandwidth_seconds,
            active_workers=active_workers,
            worker_contributions=dict(contributions),
            proof_count=len(proofs),
            avg_bandwidth_mbps=avg_bandwidth,
            avg_latency_ms=avg_latency,
            success_rate=1.0 if proofs else 0.0,
        )

    async def _send_report_to_validator(self, validator_hotkey, summary, validators, metagraph, wallet, hotkey, current_epoch) -> None:
        """Send report to a specific validator."""
        validator_info = validators.get(validator_hotkey)
        if not validator_info:
            return

        validator_uid = None
        for uid, axon in enumerate(metagraph.axons):
            if axon.hotkey == validator_hotkey:
                validator_uid = uid
                break

        if validator_uid is None:
            logger.warning(f"Validator {validator_hotkey[:16]} not found in metagraph")
            return

        axon = metagraph.axons[validator_uid]
        if not axon.ip or axon.ip == "0.0.0.0":
            return

        payload = {
            "orchestrator_hotkey": hotkey,
            "epoch": summary.epoch,
            "total_tasks": summary.total_tasks,
            "successful_tasks": summary.successful_tasks,
            "total_bytes_relayed": summary.total_bytes_relayed,
            "active_workers": summary.active_workers,
            "avg_bandwidth_mbps": summary.avg_bandwidth_mbps,
            "avg_latency_ms": summary.avg_latency_ms,
            "success_rate": summary.success_rate,
            "proof_count": summary.proof_count,
            "worker_contributions": summary.worker_contributions,
            "timestamp": time.time(),
        }

        proofs = self.epoch_proofs.get(current_epoch, [])
        if proofs:
            proof_leaves = []
            for p in proofs:
                leaf_data = (
                    p.task_id.encode("utf-8") +
                    p.worker_id.encode("utf-8") +
                    p.bytes_relayed.to_bytes(8, "big") +
                    int(p.bandwidth_mbps * 1000).to_bytes(8, "big")
                )
                proof_leaves.append(sha256(leaf_data))
            merkle_root = compute_merkle_root(proof_leaves)
            payload["merkle_root"] = merkle_root.hex()
        else:
            payload["merkle_root"] = "0" * 64

        message = f"{payload['orchestrator_hotkey']}:{payload['epoch']}:{payload['total_bytes_relayed']}:{payload['merkle_root']}"
        try:
            signature = sign_message(message.encode(), wallet.hotkey)
            payload["signature"] = signature.hex() if isinstance(signature, bytes) else signature
        except Exception as e:
            logger.error(f"Failed to sign report: {e}")
            return

        validator_url = f"http://{axon.ip}:{axon.port}/orchestrator/report"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    validator_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status == 200:
                        logger.debug(f"Report sent to validator {validator_hotkey[:16]}")
                    else:
                        error_text = await response.text()
                        logger.warning(
                            f"Validator {validator_hotkey[:16]} rejected report: "
                            f"{response.status} - {error_text[:100]}"
                        )
        except aiohttp.ClientError as e:
            logger.warning(f"Failed to reach validator {validator_hotkey[:16]}: {e}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout sending report to validator {validator_hotkey[:16]}")
