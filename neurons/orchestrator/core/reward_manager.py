"""
Reward Manager - Worker payments, retry queue, and reward distribution.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from .config import OrchestratorSettings

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

import hashlib
import json as _json

# Import Balance for TAO transfers
try:
    from bittensor.utils.balance import Balance
except ImportError:
    Balance = None


def _compute_payment_merkle_root(leaves: list) -> str:
    """Compute merkle root from payment leaf dicts (inline, no external deps)."""
    if not leaves:
        return "0x" + "0" * 64

    # Hash each leaf: deterministic JSON -> SHA-256
    hashes = []
    for leaf in leaves:
        leaf_str = _json.dumps(leaf, sort_keys=True, separators=(',', ':'))
        h = hashlib.sha256(leaf_str.encode()).digest()
        hashes.append(h)

    # Build tree bottom-up
    while len(hashes) > 1:
        next_level = []
        for i in range(0, len(hashes), 2):
            left = hashes[i]
            right = hashes[i + 1] if i + 1 < len(hashes) else hashes[i]
            next_level.append(hashlib.sha256(left + right).digest())
        hashes = next_level

    return "0x" + hashes[0].hex()


class RewardManager:
    """Manages worker payments, retry queue, and reward distribution."""

    def __init__(self, settings: OrchestratorSettings):
        self.settings = settings

        # Payment retry queue
        self._payment_retry_queue: List[dict] = []
        self._max_payment_retries: int = 5

        # Dedup: task IDs that have already been paid (prevents double-payment)
        self._paid_task_ids: Set[str] = set()

        # Coldkey cache: hotkey -> coldkey (avoids repeated metagraph loads)
        self._coldkey_cache: Dict[str, str] = {}
        self._coldkey_cache_ts: float = 0.0
        self._coldkey_cache_ttl: float = 300.0  # Refresh metagraph every 5 min

        # Failed PoB recordings that need retry
        self._pob_recording_queue: List[Dict[str, Any]] = []

        # Reward tracking
        self.total_rewards_distributed: float = 0.0
        self.last_emission_check: float = 0.0
        self.epoch_start_emission: float = 0.0

        # Payment success/failure counters for monitoring
        self._payment_success_count: int = 0
        self._payment_failure_count: int = 0

        # Per-epoch accumulators for epoch summary reporting
        self._epoch_totals: Dict[int, Dict[str, Any]] = {}  # epoch -> {distributed_nano, worker_ids, bytes_relayed}

    async def load_paid_tasks(self, subnet_core_client) -> int:
        """
        Load already-paid task IDs from SubnetCore on startup.
        Returns the number of task IDs loaded.
        """
        if not SUBNET_CORE_CLIENT_AVAILABLE or not subnet_core_client:
            return 0
        try:
            task_ids = await subnet_core_client.get_paid_task_ids()
            self._paid_task_ids.update(task_ids)
            logger.info(f"Loaded {len(task_ids)} paid task IDs into dedup set")
            return len(task_ids)
        except Exception as e:
            logger.warning(f"Failed to load paid task IDs: {e}")
            return 0

    async def pay_worker_immediately(
        self,
        worker,
        proof,
        current_epoch: int,
        get_our_emission,
        total_bytes_relayed: int,
        wallet,
        subtensor,
        hotkey: str,
        db,
        subnet_core_client,
        fee_percentage: float = 12.0,
        netuid: int = 105,
        alpha_per_chunk: float = 0.5,
    ) -> Optional[float]:
        """
        Execute on-chain ALPHA payment for a completed task and record it.

        Flow (matches BeamCore's verification requirements):
        1. Dedup + validate inputs
        2. Parallel fetch: validate-transfer, payment-address
        3. Resolve worker coldkey (cached)
        4. On-chain transfer_stake with {transfer_id}:{task_id} memo
        5. POST /pob/{task_id}/payment with tx_hash

        Returns alpha_per_chunk on success, None on skip/fail.
        """
        task_id = proof.task_id
        worker_id = proof.worker_id
        worker_hotkey = proof.worker_hotkey

        # Early exits
        if not proof.bytes_relayed or proof.bytes_relayed <= 0:
            return None
        if task_id in self._paid_task_ids:
            logger.debug(f"DEDUP: task {task_id[:16]}... already paid")
            return None
        if not (SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client and wallet and subtensor):
            logger.warning(f"Missing dependencies for task {task_id[:16]}... — queuing")
            self._queue_failed_payment(worker, proof, alpha_per_chunk)
            return None

        # Parallel: validate transfer + get payment-address (saves ~500ms)
        try:
            validation, payment_info = await asyncio.gather(
                subnet_core_client.validate_transfer_for_payment(task_id),
                subnet_core_client.get_task_payment_address(task_id),
                return_exceptions=True,
            )
        except Exception as e:
            logger.warning(f"Payment prep failed for {task_id[:16]}...: {e}")
            self._queue_failed_payment(worker, proof, alpha_per_chunk)
            return None

        # Check validation
        if isinstance(validation, Exception) or not validation.get("valid"):
            err = validation.get("error", str(validation)) if isinstance(validation, dict) else str(validation)
            logger.warning(f"Transfer invalid for {task_id[:16]}...: {err}")
            return None
        transfer_id = validation.get("transfer_id")
        if not transfer_id:
            logger.warning(f"No transfer_id for {task_id[:16]}... — skip")
            return None

        # Resolve worker coldkey.
        # The proof's worker_id is the ACTUAL completer; payment-address API has been
        # observed to return a different worker_id for the same task, which causes
        # "Recipient mismatch" on verification. Prefer the proof, use payment-address
        # only as a fallback when proof has no worker_id.
        pa_worker_id = ""
        if isinstance(payment_info, dict):
            pa_worker_id = payment_info.get("worker_id", "")
        if pa_worker_id and worker_id and pa_worker_id != worker_id:
            logger.warning(
                f"payment-address worker_id mismatch for {task_id[:16]}...: "
                f"proof={worker_id} vs payment-address={pa_worker_id} — trusting proof"
            )

        resolve_id = worker_id or pa_worker_id
        payment_dest = None
        # Check cache first
        if resolve_id and resolve_id in self._coldkey_cache:
            cached_ts = self._coldkey_cache_ts
            if time.time() - cached_ts < self._coldkey_cache_ttl:
                payment_dest = self._coldkey_cache.get(resolve_id)

        if not payment_dest:
            try:
                if resolve_id:
                    payment_dest = await subnet_core_client.get_worker_coldkey(resolve_id)
                # Last resort: try payment-address's worker_id if it differed
                if not payment_dest and pa_worker_id and pa_worker_id != resolve_id:
                    payment_dest = await subnet_core_client.get_worker_coldkey(pa_worker_id)
                if payment_dest and resolve_id:
                    self._coldkey_cache[resolve_id] = payment_dest
                    self._coldkey_cache_ts = time.time()
            except Exception as e:
                logger.warning(f"Worker coldkey lookup failed for {task_id[:16]}...: {e}")

        # Last resort: metagraph lookup
        if not payment_dest and worker_hotkey:
            payment_dest = await self._resolve_worker_coldkey(worker_hotkey, subtensor, netuid)

        if not payment_dest:
            logger.error(f"No worker coldkey for {task_id[:16]}... — queuing")
            self._queue_failed_payment(worker, proof, alpha_per_chunk, transfer_id=transfer_id)
            return None

        # ----- On-chain ALPHA payment (transfer_stake in batch_all with memo) -----
        alpha_amount_rao = int(alpha_per_chunk * 1e9)
        payment_memo = f"{transfer_id}:{task_id}"

        try:
            tx_hash = await self.transfer_alpha_with_memo(
                dest_address=payment_dest,
                amount_alpha=alpha_per_chunk,
                transfer_id=payment_memo,
                wallet=wallet,
                subtensor=subtensor,
                netuid=netuid,
            )
        except Exception as e:
            self._payment_failure_count += 1
            logger.error(f"transfer_stake error for {task_id[:16]}...: {e}")
            self._queue_failed_payment(worker, proof, alpha_per_chunk, transfer_id=transfer_id, payment_dest=payment_dest)
            return None

        if not tx_hash:
            self._payment_failure_count += 1
            logger.error(f"transfer_stake returned no tx for {task_id[:16]}... — queuing")
            self._queue_failed_payment(worker, proof, alpha_per_chunk, transfer_id=transfer_id, payment_dest=payment_dest)
            return None

        # ----- Record payment to BeamCore (3 retries, then background queue) -----
        self._paid_task_ids.add(task_id)
        self._payment_success_count += 1
        self.total_rewards_distributed += alpha_per_chunk

        recorded = False
        for attempt in range(3):
            try:
                await subnet_core_client.record_pob_payment(
                    task_id=task_id, tx_hash=tx_hash, amount_rao=alpha_amount_rao,
                )
                recorded = True
                break
            except Exception as e:
                logger.warning(f"Record PoB payment retry {attempt+1}/3 for {task_id[:16]}...: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)

        if not recorded:
            self._pob_recording_queue.append({
                "task_id": task_id, "tx_hash": tx_hash,
                "amount_rao": alpha_amount_rao,
                "attempts": 3, "queued_at": time.time(),
            })
            logger.error(f"PoB record failed for {task_id[:16]}... — queued for background retry")

        logger.info(
            f"✓ PAID {alpha_per_chunk} ALPHA → {payment_dest[:16]}... "
            f"task={task_id[:16]}... memo={payment_memo} tx={tx_hash[:24]}..."
        )

        # Update worker stats
        if worker is not None:
            worker.rewards_earned_epoch += alpha_amount_rao
            worker.rewards_earned_total += alpha_amount_rao

        # Save to DB
        if DB_AVAILABLE and db:
            try:
                await db.save_worker_payment(
                    epoch=current_epoch,
                    worker_id=worker_id,
                    worker_hotkey=worker_hotkey,
                    bytes_relayed=proof.bytes_relayed,
                    tasks_completed=1,
                    amount_earned=alpha_amount_rao,
                    merkle_proof=tx_hash,
                    leaf_index=0,
                )
            except Exception as e:
                logger.warning(f"DB save failed: {e}")

        # Record to SubnetCore (non-critical)
        try:
            from clients.subnet_core_client import WorkerPaymentData
            await subnet_core_client.record_worker_payment(WorkerPaymentData(
                orchestrator_hotkey=hotkey or "",
                epoch=current_epoch,
                worker_id=worker_id,
                worker_hotkey=worker_hotkey,
                bytes_relayed=proof.bytes_relayed,
                tasks_completed=1,
                amount_earned=alpha_amount_rao,
                task_id=task_id,
                tx_hash=tx_hash,
            ))
        except Exception as e:
            logger.debug(f"SubnetCore payment record failed (non-critical): {e}")

        # Epoch tracking
        self._track_epoch_payment(current_epoch, worker_id, worker_hotkey, alpha_amount_rao, proof.bytes_relayed)
        await self._report_epoch_summary(current_epoch, hotkey, subnet_core_client)

        return alpha_per_chunk

    def _track_epoch_payment(self, epoch: int, worker_id: str, worker_hotkey: str, amount_nano: int, bytes_relayed: int):
        """Accumulate payment totals and leaf data for an epoch."""
        if epoch not in self._epoch_totals:
            self._epoch_totals[epoch] = {"distributed_nano": 0, "worker_ids": set(), "bytes_relayed": 0, "leaves": []}
        totals = self._epoch_totals[epoch]
        totals["distributed_nano"] += amount_nano
        totals["worker_ids"].add(worker_id)
        totals["bytes_relayed"] += bytes_relayed
        totals["leaves"].append({
            "worker_id": worker_id,
            "worker_hotkey": worker_hotkey,
            "epoch": epoch,
            "bytes_relayed": bytes_relayed,
            "amount_earned": amount_nano,
        })

    async def _report_epoch_summary(self, epoch: int, hotkey: str, subnet_core_client):
        """Report accumulated epoch totals to SubnetCore so the dashboard can display them."""
        if not SUBNET_CORE_CLIENT_AVAILABLE or not subnet_core_client:
            return
        totals = self._epoch_totals.get(epoch)
        if not totals:
            return
        try:
            from clients.subnet_core_client import EpochPaymentData

            # Compute real merkle root from accumulated payment leaves
            leaves = totals.get("leaves", [])
            merkle_root = _compute_payment_merkle_root(leaves)
            if leaves:
                logger.info(f"[MERKLE] Computed root for epoch {epoch}: {merkle_root} ({len(leaves)} leaves)")

            epoch_data = EpochPaymentData(
                epoch=epoch,
                total_distributed=totals["distributed_nano"],
                worker_count=len(totals["worker_ids"]),
                total_bytes_relayed=totals["bytes_relayed"],
                merkle_root=merkle_root,
                submitted_to_validators=True,
            )
            await subnet_core_client.record_epoch_payment(epoch_data)
            logger.info(f"[MERKLE] Epoch summary sent to SubnetCore: epoch={epoch}, merkle_root={merkle_root}, workers={len(totals['worker_ids'])}")
        except Exception as e:
            logger.error(f"[MERKLE] Failed to report epoch summary: {e}", exc_info=True)

    def _queue_failed_payment(self, worker, proof, reward_tao: float, transfer_id: str = None, payment_dest: str = None):
        """Queue a failed/partial payment for retry when balance is available."""
        # Use proof attributes (always available) since worker may be None
        self._payment_retry_queue.append({
            "worker_hotkey": proof.worker_hotkey,
            "worker_id": proof.worker_id,
            "task_id": proof.task_id,
            "transfer_id": transfer_id,
            "payment_dest": payment_dest,
            "proof": proof,
            "reward_tao": reward_tao,
            "attempts": 0,
            "queued_at": time.time(),
        })
        logger.info(
            f"Queued payment retry: {reward_tao:.9f} TAO for task {proof.task_id[:16]}... "
            f"(queue size: {len(self._payment_retry_queue)})"
        )

    async def process_payment_retry_queue(self, current_epoch: int, wallet, subtensor, hotkey: str, db, subnet_core_client):
        """Process queued payments when balance is available."""
        if not self._payment_retry_queue or not wallet or not subtensor:
            return

        # transfer_stake uses staked ALPHA, not free TAO balance.
        # Check staked alpha on our hotkey for the subnet.
        try:
            stake_info = subtensor.get_stake(
                coldkey_ss58=wallet.coldkey.ss58_address,
                hotkey_ss58=wallet.hotkey.ss58_address,
                netuid=105,
            )
            available = float(stake_info) - 0.1  # Keep 0.1 ALPHA buffer
        except Exception:
            # Fallback: try TAO balance as a rough proxy
            try:
                balance = subtensor.get_balance(wallet.hotkey.ss58_address)
                available = float(balance) - 0.001
            except Exception as e:
                logger.warning(f"Could not check balance for retry queue: {e}")
                return

        if available <= 0:
            logger.debug(
                f"Insufficient staked ALPHA for payment retries ({len(self._payment_retry_queue)} queued)"
            )
            return

        logger.info(
            f"Processing payment retry queue: {len(self._payment_retry_queue)} items, "
            f"{available:.4f} TAO available"
        )

        completed = set()
        for i, item in enumerate(self._payment_retry_queue):
            if available <= 1e-9:
                break

            task_id = item.get("task_id")

            # Skip zero-byte tasks — no work, nothing to pay
            proof = item.get("proof")
            if proof and getattr(proof, "bytes_relayed", 0) <= 0:
                completed.add(i)
                continue

            # Dedup: skip if already paid (e.g. immediate pay succeeded after queuing shortfall)
            if task_id and task_id in self._paid_task_ids:
                logger.info(f"DEDUP: retry task {task_id[:16]}... already paid — removing from queue")
                completed.add(i)
                continue

            # Resolve payment address via SubnetCore
            payment_dest = None
            if SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client and task_id:
                try:
                    payment_info = await subnet_core_client.get_task_payment_address(task_id)
                    payment_dest = payment_info.get("address")
                except Exception as e:
                    item["attempts"] += 1
                    logger.warning(
                        f"Failed to resolve payment address for task {task_id[:16]}...: {e} "
                        f"(attempt {item['attempts']}/{self._max_payment_retries})"
                    )
                    continue
            else:
                item["attempts"] += 1
                logger.warning(
                    f"Cannot resolve payment address: SubnetCore unavailable "
                    f"(attempt {item['attempts']}/{self._max_payment_retries})"
                )
                continue

            if not payment_dest:
                item["attempts"] += 1
                logger.warning(f"Empty payment address for task {task_id[:16]}...")
                continue

            reward = min(item["reward_tao"], available)

            try:
                # Resolve worker's actual coldkey for transfer_stake
                retry_payment_dest = item.get("payment_dest", "")
                if not retry_payment_dest and SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client:
                    try:
                        payment_info = await subnet_core_client.get_task_payment_address(task_id)
                        pa_wid = payment_info.get("worker_id", "")
                        if pa_wid:
                            retry_payment_dest = await subnet_core_client.get_worker_coldkey(pa_wid)
                    except Exception as e:
                        logger.warning(f"Retry: worker coldkey lookup failed for {task_id[:16]}...: {e}")
                if not retry_payment_dest:
                    item["attempts"] += 1
                    logger.warning(f"Retry: cannot resolve worker coldkey for task {task_id[:16]}...")
                    continue

                # ALPHA payment via transfer_stake with on-chain memo
                # Memo format must be {transfer_id}:{task_id} per BeamCore spec
                alpha_per_chunk = self.settings.alpha_per_chunk if hasattr(self.settings, 'alpha_per_chunk') else 0.5
                alpha_amount = alpha_per_chunk
                retry_transfer_id = item.get("transfer_id")
                if not retry_transfer_id and SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client and task_id:
                    try:
                        validation = await subnet_core_client.validate_transfer_for_payment(task_id)
                        retry_transfer_id = validation.get("transfer_id") if validation.get("valid") else None
                    except Exception:
                        pass
                if not retry_transfer_id:
                    item["attempts"] += 1
                    logger.warning(
                        f"Retry: cannot resolve transfer_id for task {task_id[:16]}... "
                        f"(attempt {item['attempts']}/{self._max_payment_retries})"
                    )
                    continue
                payment_memo = f"{retry_transfer_id}:{task_id}"
                tx_hash = await self.transfer_alpha_with_memo(
                    dest_address=retry_payment_dest,
                    amount_alpha=alpha_amount,
                    transfer_id=payment_memo,
                    wallet=wallet,
                    subtensor=subtensor,
                    netuid=105,
                )

                if tx_hash:
                    available -= reward
                    if task_id:
                        self._paid_task_ids.add(task_id)
                    logger.info(
                        f"Retry payment SUCCESS: {alpha_amount} ALPHA to {retry_payment_dest[:16]}... tx={tx_hash[:24]}..."
                    )

                    # Record payment on PoB record via BeamCore
                    # CRITICAL: failure here means BeamCore thinks we didn't pay
                    alpha_amount_rao = int(alpha_amount * 1e9)
                    if SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client:
                        recorded = False
                        for rec_attempt in range(3):
                            try:
                                await subnet_core_client.record_pob_payment(
                                    task_id=task_id,
                                    tx_hash=tx_hash,
                                    amount_rao=alpha_amount_rao,
                                )
                                recorded = True
                                break
                            except Exception as e:
                                logger.warning(f"Failed to record retry PoB payment (attempt {rec_attempt+1}/3): {e}")
                                if rec_attempt < 2:
                                    await asyncio.sleep(2)
                        if not recorded:
                            self._pob_recording_queue.append({
                                "task_id": task_id,
                                "tx_hash": tx_hash,
                                "amount_rao": alpha_amount_rao,
                                "attempts": 3,
                                "queued_at": time.time(),
                            })

                    proof = item["proof"]

                    if DB_AVAILABLE and db:
                        try:
                            await db.save_worker_payment(
                                epoch=current_epoch,
                                worker_id=item["worker_id"],
                                worker_hotkey=item["worker_hotkey"],
                                bytes_relayed=proof.bytes_relayed,
                                tasks_completed=1,
                                amount_earned=alpha_amount_rao,
                                merkle_proof=tx_hash,
                                leaf_index=0,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to save retry payment: {e}")

                    if SUBNET_CORE_CLIENT_AVAILABLE and subnet_core_client:
                        try:
                            from clients.subnet_core_client import WorkerPaymentData
                            payment_data = WorkerPaymentData(
                                orchestrator_hotkey=hotkey or "",
                                epoch=current_epoch,
                                worker_id=item["worker_id"],
                                worker_hotkey=item["worker_hotkey"],
                                bytes_relayed=proof.bytes_relayed,
                                tasks_completed=1,
                                amount_earned=alpha_amount_rao,
                                task_id=proof.task_id,
                                tx_hash=tx_hash,
                            )
                            await subnet_core_client.record_worker_payment(payment_data)
                        except Exception as e:
                            logger.warning(f"Failed to record retry payment via SubnetCore: {e}")

                    self._track_epoch_payment(current_epoch, item["worker_id"], item["worker_hotkey"], alpha_amount_rao, proof.bytes_relayed)
                    self.total_rewards_distributed += alpha_amount
                    completed.add(i)
                else:
                    item["attempts"] += 1
                    logger.warning(
                        f"Retry ALPHA transfer failed for task {task_id[:16]}... "
                        f"(attempt {item['attempts']}/{self._max_payment_retries})"
                    )
            except Exception as e:
                item["attempts"] += 1
                logger.warning(f"Retry payment error: {e}")

        expired = [
            i for i, item in enumerate(self._payment_retry_queue)
            if i not in completed and item["attempts"] >= self._max_payment_retries
        ]
        for i in expired:
            item = self._payment_retry_queue[i]
            logger.error(
                f"Payment retry EXHAUSTED ({self._max_payment_retries} attempts) for "
                f"task {item.get('task_id', 'unknown')[:16]}... — {item['reward_tao']:.9f} ALPHA lost"
            )

        self._payment_retry_queue = [
            item for i, item in enumerate(self._payment_retry_queue)
            if i not in completed and item["attempts"] < self._max_payment_retries
        ]

        if completed:
            await self._report_epoch_summary(current_epoch, hotkey, subnet_core_client)

        if completed or expired:
            logger.info(
                f"Retry queue: {len(completed)} paid, {len(expired)} expired, "
                f"{len(self._payment_retry_queue)} remaining"
            )

    async def process_pob_recording_queue(self, subnet_core_client):
        """Retry failed PoB payment recordings. Called periodically.

        If record_pob_payment fails, BeamCore doesn't know we paid on-chain,
        so the proof appears unpaid → 5% compliance penalty per proof.
        """
        if not self._pob_recording_queue or not SUBNET_CORE_CLIENT_AVAILABLE or not subnet_core_client:
            return

        completed = []
        for i, item in enumerate(self._pob_recording_queue):
            try:
                await subnet_core_client.record_pob_payment(
                    task_id=item["task_id"],
                    tx_hash=item["tx_hash"],
                    amount_rao=item["amount_rao"],
                )
                completed.append(i)
                logger.info(f"PoB recording retry SUCCESS for task {item['task_id'][:16]}...")
            except Exception as e:
                item["attempts"] += 1
                if item["attempts"] >= 10:
                    completed.append(i)
                    logger.error(
                        f"PoB recording ABANDONED after {item['attempts']} attempts "
                        f"for task {item['task_id'][:16]}... — compliance will be penalized"
                    )
                else:
                    logger.debug(f"PoB recording retry failed ({item['attempts']}/10): {e}")

        if completed:
            self._pob_recording_queue = [
                item for i, item in enumerate(self._pob_recording_queue)
                if i not in completed
            ]

    # =========================================================================
    # ALPHA Token Transfer with On-Chain Memo
    # =========================================================================

    async def transfer_alpha_with_memo(
        self,
        dest_address: str,
        amount_alpha: float,
        transfer_id: str,
        wallet,
        subtensor,
        netuid: int,
    ) -> Optional[str]:
        """
        Transfer ALPHA tokens via transfer_stake with on-chain memo.

        Uses utility.batch_all to atomically execute:
        1. system.remark_with_event(memo) - on-chain memo for validator verification
        2. SubtensorModule.transfer_stake(...) - ALPHA transfer

        The dest_address from payment-address API is the worker's coldkey for transfer_stake.

        Args:
            dest_address: Payment address (worker coldkey) from BeamCore payment-address API
            amount_alpha: Amount of ALPHA to transfer (e.g., 0.5 for 0.5 ALPHA)
            transfer_id: Memo string "{transfer_id}:{task_id}" for on-chain remark
            wallet: Bittensor wallet with coldkey for signing
            subtensor: Subtensor connection
            netuid: Network UID (105 for mainnet, 304 for testnet)

        Returns:
            Transaction hash in "extrinsic_hash:block_hash" format, or None if failed
        """
        if not wallet or not subtensor:
            logger.warning("Cannot transfer ALPHA: wallet or subtensor not available")
            return None

        # Convert to RAO (1 ALPHA = 1e9 RAO)
        amount_rao = int(amount_alpha * 1e9)

        # Minimum transfer is 500,000 RAO (0.0005 TAO equivalent)
        MIN_TRANSFER_RAO = 500_000
        if amount_rao < MIN_TRANSFER_RAO:
            logger.debug(f"ALPHA amount {amount_rao} RAO topped up to minimum {MIN_TRANSFER_RAO} RAO")
            amount_rao = MIN_TRANSFER_RAO

        try:
            # Access the underlying substrate interface
            substrate = subtensor.substrate

            # Compose remark call with memo
            remark_call = substrate.compose_call(
                call_module='System',
                call_function='remark_with_event',
                call_params={'remark': transfer_id.encode()}
            )

            # Compose transfer_stake call — BeamCore verifier expects this
            transfer_call = substrate.compose_call(
                call_module='SubtensorModule',
                call_function='transfer_stake',
                call_params={
                    'destination_coldkey': dest_address,
                    'hotkey': wallet.hotkey.ss58_address,
                    'origin_netuid': netuid,
                    'destination_netuid': netuid,
                    'alpha_amount': amount_rao,
                }
            )

            # Batch both calls atomically - if either fails, both are reverted
            batch_call = substrate.compose_call(
                call_module='Utility',
                call_function='batch_all',
                call_params={'calls': [remark_call, transfer_call]}
            )

            # Sign with coldkey
            extrinsic = substrate.create_signed_extrinsic(
                call=batch_call,
                keypair=wallet.coldkey,
            )

            # Submit and wait for inclusion
            receipt = substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )

            if receipt.is_success:
                tx_hash = f"{receipt.extrinsic_hash}:{receipt.block_hash}"
                logger.info(
                    f"ALPHA transfer SUCCESS: {amount_alpha} ALPHA to {dest_address[:16]}... "
                    f"memo={transfer_id} tx={tx_hash[:32]}..."
                )
                return tx_hash
            else:
                error_msg = getattr(receipt, 'error_message', 'Unknown error')
                logger.error(f"ALPHA transfer FAILED: {error_msg}")
                return None

        except Exception as e:
            logger.error(f"Error transferring ALPHA: {e}", exc_info=True)
            return None

    async def _resolve_worker_coldkey(
        self,
        worker_hotkey: str,
        subtensor,
        netuid: int,
    ) -> Optional[str]:
        """
        Resolve a worker's hotkey to their coldkey via cached metagraph.

        Caches the entire hotkey->coldkey mapping to avoid loading metagraph
        on every payment call. Cache refreshes every 5 minutes.
        """
        # Check cache first
        if worker_hotkey in self._coldkey_cache:
            return self._coldkey_cache[worker_hotkey]

        # Refresh cache if stale
        now = time.time()
        if now - self._coldkey_cache_ts > self._coldkey_cache_ttl:
            try:
                metagraph = subtensor.metagraph(netuid)
                self._coldkey_cache = {}
                for neuron in metagraph.neurons:
                    self._coldkey_cache[neuron.hotkey] = neuron.coldkey
                self._coldkey_cache_ts = now
                logger.info(f"Refreshed coldkey cache: {len(self._coldkey_cache)} entries")
            except Exception as e:
                logger.error(f"Failed to refresh coldkey cache: {e}")

        # Lookup after refresh
        coldkey = self._coldkey_cache.get(worker_hotkey)
        if coldkey:
            logger.debug(f"Resolved coldkey for {worker_hotkey[:16]}...: {coldkey[:16]}...")
        else:
            logger.warning(f"Worker hotkey {worker_hotkey[:16]}... not found in metagraph cache")
        return coldkey

    def _calculate_quality_multiplier(self, worker) -> float:
        """
        Calculate quality multiplier for worker rewards.

        Range: 0.5 to 1.5
        """
        # If worker is None (e.g., challenge flow), return neutral multiplier
        if worker is None:
            return 1.0

        multiplier = 1.0
        multiplier += getattr(worker, 'success_rate', 0.0) * 0.25

        latency_ms = getattr(worker, 'latency_ms', 0)
        if latency_ms > 0:
            latency_score = max(0, 1 - (latency_ms / 1000))
            multiplier += latency_score * 0.15

        multiplier += getattr(worker, 'trust_score', 0.5) * 0.10

        return max(0.5, min(1.5, multiplier))

    def _calculate_worker_reward_score(self, worker) -> float:
        """Calculate a worker's reward score based on multiple factors."""
        # If worker is None, return minimal score
        if worker is None:
            return 0.0

        w_bytes = self.settings.reward_weight_bytes
        w_success = self.settings.reward_weight_success_rate
        w_latency = self.settings.reward_weight_latency
        w_trust = self.settings.reward_weight_trust

        bytes_score = float(getattr(worker, 'bytes_relayed_epoch', 0))
        success_score = getattr(worker, 'success_rate', 0.0)

        max_latency_ms = 1000.0
        latency_ms = getattr(worker, 'latency_ms', 0)
        latency_score = max(0.0, 1.0 - (latency_ms / max_latency_ms))
        trust_score = getattr(worker, 'trust_score', 0.5)

        quality_multiplier = (
            w_success * success_score +
            w_latency * latency_score +
            w_trust * trust_score
        ) / (w_success + w_latency + w_trust) if (w_success + w_latency + w_trust) > 0 else 1.0

        final_score = bytes_score * (0.5 + 0.5 * quality_multiplier)
        return final_score

    def distribute_rewards_at_epoch_end(self, workers_values, get_our_emission) -> Dict[str, float]:
        """
        Distribute accumulated epoch emissions to all workers proportionally.

        Returns dict of worker_id -> reward amount in TAO.
        """
        current_emission = get_our_emission()
        epoch_rewards = current_emission - self.epoch_start_emission

        if epoch_rewards <= 0:
            logger.debug("No new emissions this epoch, no rewards to distribute")
            return {}

        contributing_workers = [
            w for w in workers_values
            if w.bytes_relayed_epoch > 0
        ]

        if not contributing_workers:
            logger.debug("No workers contributed this epoch, no rewards to distribute")
            return {}

        worker_scores: Dict[str, float] = {}
        for worker in contributing_workers:
            score = self._calculate_worker_reward_score(worker)
            worker_scores[worker.worker_id] = score

        total_score = sum(worker_scores.values())
        if total_score <= 0:
            logger.warning("Total worker score is 0, distributing equally")
            total_score = len(contributing_workers)
            worker_scores = {w.worker_id: 1.0 for w in contributing_workers}

        rewards_distributed: Dict[str, float] = {}
        for worker in contributing_workers:
            share = worker_scores[worker.worker_id] / total_score
            reward = epoch_rewards * share

            reward_nano = int(reward * 1e9)
            worker.rewards_earned_epoch = reward_nano
            worker.rewards_earned_total += reward_nano

            rewards_distributed[worker.worker_id] = reward

            logger.info(
                f"Worker {worker.worker_id[:8]} ({worker.hotkey[:12]}...) earned {reward:.6f} ध "
                f"({share*100:.2f}% share, {worker.bytes_relayed_epoch:,} bytes, "
                f"success={worker.success_rate:.2f}, latency={worker.latency_ms:.0f}ms)"
            )

        self.total_rewards_distributed += epoch_rewards

        logger.info(
            f"Epoch reward distribution complete: {epoch_rewards:.6f} ध "
            f"to {len(rewards_distributed)} workers "
            f"(total all-time: {self.total_rewards_distributed:.6f} ध)"
        )

        return rewards_distributed

    def distribute_rewards_to_workers(self, get_our_emission) -> Dict[str, float]:
        """
        Legacy method - now just tracks emission changes.

        Actual distribution happens at epoch end via distribute_rewards_at_epoch_end().
        """
        current_emission = get_our_emission()
        if current_emission > self.last_emission_check:
            new_emissions = current_emission - self.last_emission_check
            logger.debug(f"New emissions detected: {new_emissions:.6f} ध (accumulated for epoch end)")
            self.last_emission_check = current_emission

        return {}
