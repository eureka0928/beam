"""
Cross-Verification Service for Validators

Manages the cross-orchestrator verification process:
1. Receives verification assignments from Proof Registry
2. Verifies assigned proofs (re-evaluates claims)
3. Submits commit-reveal for verification scores
4. Aggregates results and calculates penalties
5. Applies penalty multipliers to orchestrator weights

This service enables trustless verification where orchestrators
anonymously verify each other's worker proof evaluations.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Import stubs for removed beam.cross_verification (logic now in BeamCore)
from core._beam_stubs import (
    # Models
    ProofSubmission,
    AnonymizedProof,
    VerificationCommitment,
    VerificationReveal,
    ProofVerificationResult,
    VerificationVerdict,
    AggregatedVerification,
    AggregationStatus,
    OrchestratorPenalty,
    VerifierPenalty,
    CrossVerificationConfig,
    DEFAULT_CONFIG,
    # Registry
    ProofRegistry,
    get_proof_registry,
    create_proof_registry,
    # Randomness
    get_epoch_random_seed,
    generate_epoch_assignments,
    VerifierAssignments,
    # Commit-Reveal
    CommitRevealManager,
    get_commit_reveal_manager,
    create_commit_reveal_manager,
    VerificationPhase,
    EpochPhaseInfo,
    # Verification
    verify_proof,
    verify_proofs_batch,
    aggregate_verification_results,
    aggregate_epoch_results,
    summarize_orchestrator_results,
    VerificationContext,
    OrchestratorVerificationSummary,
    # Penalties
    calculate_orchestrator_penalty,
    calculate_orchestrator_penalties,
    calculate_verifier_penalty,
    summarize_epoch_penalties,
    PenaltyHistory,
    get_penalty_history,
    EpochPenaltySummary,
)

logger = logging.getLogger(__name__)


@dataclass
class CrossVerificationState:
    """Tracks cross-verification state for current epoch"""
    epoch: int
    phase: VerificationPhase = VerificationPhase.WORK

    # Assignments for this validator (as verifier)
    assignments: Optional[VerifierAssignments] = None
    assigned_proofs: Dict[str, List[AnonymizedProof]] = field(default_factory=dict)

    # Verification results we computed
    our_results: List[ProofVerificationResult] = field(default_factory=list)

    # Commit-reveal state
    commitment: Optional[VerificationCommitment] = None
    commitment_salt: Optional[bytes] = None
    reveal_submitted: bool = False

    # Aggregated results (after reveal phase)
    aggregated: Dict[bytes, AggregatedVerification] = field(default_factory=dict)

    # Penalties calculated
    orchestrator_penalties: Dict[str, OrchestratorPenalty] = field(default_factory=dict)
    verifier_penalties: Dict[str, VerifierPenalty] = field(default_factory=dict)


class CrossVerificationService:
    """
    Service for managing cross-verification on validator nodes.

    Coordinates the full verification lifecycle:
    1. WORK phase: Collect proofs, receive assignments
    2. COMMIT phase: Verify proofs, submit commitment hash
    3. REVEAL phase: Reveal actual scores
    4. SETTLEMENT phase: Aggregate results, calculate penalties
    """

    def __init__(
        self,
        validator_hotkey: str,
        config: CrossVerificationConfig = DEFAULT_CONFIG,
        use_database: bool = False,
        proof_registry_url: Optional[str] = None,
    ):
        """
        Initialize cross-verification service.

        Args:
            validator_hotkey: This validator's hotkey (also used as verifier)
            config: Cross-verification configuration
            use_database: Whether to use database persistence
            proof_registry_url: URL of external Proof Registry (if not local)
        """
        self.validator_hotkey = validator_hotkey
        self.config = config
        self.use_database = use_database
        self.proof_registry_url = proof_registry_url

        # Services
        self._registry: Optional[ProofRegistry] = None
        self._commit_reveal: Optional[CommitRevealManager] = None
        self._penalty_history: Optional[PenaltyHistory] = None

        # State tracking per epoch
        self._epoch_states: Dict[int, CrossVerificationState] = {}
        self._current_epoch: int = 0

        # Lock for thread safety
        self._lock = asyncio.Lock()

        # Background task handle
        self._phase_monitor_task: Optional[asyncio.Task] = None
        self._running: bool = False

        logger.info(f"CrossVerificationService initialized for validator {validator_hotkey[:16]}...")

    async def initialize(self) -> None:
        """Initialize services and connections"""
        logger.info("Initializing CrossVerificationService...")

        # Initialize proof registry
        if self.proof_registry_url:
            # Use remote registry via HTTP
            logger.info(f"Using remote Proof Registry at {self.proof_registry_url}")
            # For remote registry, we'll use HTTP client in methods
            self._registry = None
        else:
            # Use local registry
            self._registry = create_proof_registry(
                config=self.config,
                use_database=self.use_database,
            )

        # Initialize commit-reveal manager
        self._commit_reveal = create_commit_reveal_manager(
            config=self.config,
            use_database=self.use_database,
        )

        # Initialize penalty history
        self._penalty_history = get_penalty_history()

        logger.info("CrossVerificationService initialized")

    async def start(self) -> None:
        """Start the cross-verification service"""
        if self._running:
            return

        self._running = True

        # Start phase monitoring task
        self._phase_monitor_task = asyncio.create_task(self._phase_monitor_loop())

        logger.info("CrossVerificationService started")

    async def stop(self) -> None:
        """Stop the cross-verification service"""
        self._running = False

        if self._phase_monitor_task:
            self._phase_monitor_task.cancel()
            try:
                await self._phase_monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("CrossVerificationService stopped")

    # =========================================================================
    # Registry Access
    # =========================================================================

    def get_registry(self) -> ProofRegistry:
        """Get the proof registry (local or create new)"""
        if self._registry is None:
            self._registry = get_proof_registry()
        return self._registry

    # =========================================================================
    # Epoch & Phase Management
    # =========================================================================

    def set_current_epoch(self, epoch: int, start_block: int) -> None:
        """
        Set current epoch and initialize state.

        Called by validator when new epoch starts.
        """
        if epoch > self._current_epoch:
            self._current_epoch = epoch

            # Initialize state for new epoch
            self._epoch_states[epoch] = CrossVerificationState(epoch=epoch)

            # Set epoch in commit-reveal manager
            if self._commit_reveal:
                self._commit_reveal.set_epoch_start_block(epoch, start_block)

            # Cleanup old epochs
            self._cleanup_old_epochs()

            logger.info(f"CrossVerification epoch updated: {epoch}")

    def get_current_phase(self, current_block: int) -> EpochPhaseInfo:
        """Get current verification phase"""
        if self._commit_reveal:
            return self._commit_reveal.get_phase_info(self._current_epoch, current_block)

        # Fallback
        return EpochPhaseInfo(
            epoch=self._current_epoch,
            phase=VerificationPhase.WORK,
            phase_start_block=0,
            phase_end_block=0,
            current_block=current_block,
        )

    def _cleanup_old_epochs(self, keep: int = 5) -> None:
        """Remove old epoch states"""
        if len(self._epoch_states) > keep:
            epochs_to_remove = sorted(self._epoch_states.keys())[:-keep]
            for epoch in epochs_to_remove:
                del self._epoch_states[epoch]
            logger.debug(f"_cleanup_old_epochs: removed {len(epochs_to_remove)} old epochs, keeping {len(self._epoch_states)}")

    # =========================================================================
    # Phase Monitor
    # =========================================================================

    async def _phase_monitor_loop(self) -> None:
        """Background task that monitors phase transitions"""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds

                # Get current block (would come from subtensor in production)
                # For now, this is called externally via process_phase_transition

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in phase monitor: {e}")

    async def process_phase_transition(
        self,
        current_block: int,
        block_hash: bytes,
        orchestrators: List[str],
    ) -> None:
        """
        Process phase transition for current epoch.

        Called by validator main loop when block changes.

        Args:
            current_block: Current block number
            block_hash: Current block hash (for randomness)
            orchestrators: List of active orchestrator hotkeys
        """
        phase_info = self.get_current_phase(current_block)
        epoch = phase_info.epoch

        state = self._epoch_states.get(epoch)
        if not state:
            return

        # Check if phase changed
        if phase_info.phase != state.phase:
            old_phase = state.phase
            state.phase = phase_info.phase

            logger.info(f"Phase transition: {old_phase.value} -> {phase_info.phase.value}")

            # Handle phase-specific actions
            if phase_info.phase == VerificationPhase.COMMIT:
                await self._on_commit_phase_start(epoch, current_block, block_hash, orchestrators)

            elif phase_info.phase == VerificationPhase.REVEAL:
                await self._on_reveal_phase_start(epoch, current_block)

            elif phase_info.phase == VerificationPhase.SETTLEMENT:
                await self._on_settlement_phase_start(epoch, current_block)

    # =========================================================================
    # Commit Phase
    # =========================================================================

    async def _on_commit_phase_start(
        self,
        epoch: int,
        current_block: int,
        block_hash: bytes,
        orchestrators: List[str],
    ) -> None:
        """Handle start of commit phase"""
        logger.info(f"Commit phase started for epoch {epoch}")

        state = self._epoch_states.get(epoch)
        if not state:
            return

        # Get assignments for this validator
        assignments = await self._get_verification_assignments(
            epoch, block_hash, orchestrators
        )
        state.assignments = assignments

        if not assignments:
            logger.info("No verification assignments for this epoch")
            return

        # Fetch and verify assigned proofs
        results = await self._verify_assigned_proofs(epoch, assignments)
        state.our_results = results

        if not results:
            logger.info("No proofs verified")
            return

        # Create and submit commitment
        commitment, salt = self._commit_reveal.create_commitment(
            epoch=epoch,
            verifier_hotkey=self.validator_hotkey,
            verification_results=results,
            current_block=current_block,
        )

        # Store salt for reveal phase
        self._commit_reveal.store_local_salt(epoch, self.validator_hotkey, salt)
        state.commitment_salt = salt

        # Submit commitment
        success, message = await self._commit_reveal.submit_commitment(commitment, current_block)

        if success:
            state.commitment = commitment
            logger.info(f"Commitment submitted for epoch {epoch}: {len(results)} results")
        else:
            logger.error(f"Failed to submit commitment: {message}")

    async def _get_verification_assignments(
        self,
        epoch: int,
        block_hash: bytes,
        orchestrators: List[str],
    ) -> Optional[VerifierAssignments]:
        """Get verification assignments for this validator"""
        registry = self.get_registry()

        # Get proof IDs by orchestrator
        proof_ids_by_orch = {}
        for orch in orchestrators:
            proof_ids = await registry.get_proof_ids_for_orchestrator(epoch, orch)
            if proof_ids:
                proof_ids_by_orch[orch] = proof_ids
                logger.debug(f"_get_verification_assignments: {orch[:16]}... has {len(proof_ids)} proofs")

        if not proof_ids_by_orch:
            logger.info(f"_get_verification_assignments: no proofs found for any of {len(orchestrators)} orchestrators in epoch {epoch}")
            return None

        total_proofs = sum(len(ids) for ids in proof_ids_by_orch.values())
        logger.info(
            f"_get_verification_assignments: epoch={epoch} "
            f"{len(proof_ids_by_orch)} orchestrators with {total_proofs} total proofs"
        )

        # Generate assignments using on-chain randomness
        assignments = await generate_epoch_assignments(
            epoch=epoch,
            block_hash=block_hash,
            orchestrators=orchestrators,
            proof_ids_by_orchestrator=proof_ids_by_orch,
        )

        if assignments:
            our_tasks = assignments.verifier_tasks.get(self.validator_hotkey, [])
            logger.info(
                f"_get_verification_assignments: we got {len(our_tasks)} verification tasks for epoch {epoch}"
            )

        return assignments

    async def _verify_assigned_proofs(
        self,
        epoch: int,
        assignments: VerifierAssignments,
    ) -> List[ProofVerificationResult]:
        """Verify all proofs assigned to this validator"""
        registry = self.get_registry()
        state = self._epoch_states.get(epoch)

        # Get tasks assigned to us
        our_tasks = assignments.verifier_tasks.get(self.validator_hotkey, [])

        if not our_tasks:
            logger.info(f"_verify_assigned_proofs: no tasks assigned to us for epoch {epoch}")
            return []

        logger.info(f"_verify_assigned_proofs: verifying {len(our_tasks)} orchestrator assignments for epoch {epoch}")

        all_results = []

        for target_orch, proof_ids in our_tasks:
            logger.info(f"_verify_assigned_proofs: fetching {len(proof_ids)} proofs from {target_orch[:16]}...")

            # Get anonymized proofs
            anon_proofs = await registry.get_anonymized_proofs_for_verification(
                epoch=epoch,
                target_orchestrator_hotkey=target_orch,
                proof_ids=proof_ids,
            )

            if state:
                state.assigned_proofs[target_orch] = anon_proofs

            if len(anon_proofs) != len(proof_ids):
                logger.warning(
                    f"_verify_assigned_proofs: requested {len(proof_ids)} proofs from {target_orch[:16]}... "
                    f"but got {len(anon_proofs)}"
                )

            # Verify each proof
            pass_count = 0
            fail_count = 0
            for proof in anon_proofs:
                result = await verify_proof(
                    proof=proof,
                    claimed_bandwidth=proof.claimed_bandwidth_mbps,
                    claimed_reward=proof.claimed_reward_dtao,
                )
                result.verifier_hotkey = self.validator_hotkey
                result.epoch = epoch
                all_results.append(result)

                if result.verdict == VerificationVerdict.VALID:
                    pass_count += 1
                else:
                    fail_count += 1
                    logger.debug(
                        f"_verify_assigned_proofs: proof from {target_orch[:16]}... "
                        f"verdict={result.verdict.value} reason={getattr(result, 'reason', 'N/A')}"
                    )

            logger.info(
                f"_verify_assigned_proofs: {target_orch[:16]}... "
                f"{pass_count} passed, {fail_count} failed out of {len(anon_proofs)} proofs"
            )

        logger.info(f"_verify_assigned_proofs: completed {len(all_results)} proof verifications for epoch {epoch}")
        return all_results

    # =========================================================================
    # Reveal Phase
    # =========================================================================

    async def _on_reveal_phase_start(self, epoch: int, current_block: int) -> None:
        """Handle start of reveal phase"""
        logger.info(f"Reveal phase started for epoch {epoch}")

        state = self._epoch_states.get(epoch)
        if not state or not state.commitment:
            logger.info("No commitment to reveal")
            return

        # Get stored salt
        salt = state.commitment_salt
        if not salt:
            salt = self._commit_reveal.get_local_salt(epoch, self.validator_hotkey)

        if not salt:
            logger.error("Cannot reveal: salt not found")
            return

        # Create reveal
        reveal = self._commit_reveal.create_reveal(
            epoch=epoch,
            verifier_hotkey=self.validator_hotkey,
            verification_results=state.our_results,
            salt=salt,
            current_block=current_block,
        )

        # Submit reveal
        success, message = await self._commit_reveal.submit_reveal(reveal, current_block)

        if success:
            state.reveal_submitted = True
            logger.info(f"Reveal submitted for epoch {epoch}")
        else:
            logger.error(f"Failed to submit reveal: {message}")

    # =========================================================================
    # Settlement Phase
    # =========================================================================

    async def _on_settlement_phase_start(self, epoch: int, current_block: int) -> None:
        """Handle start of settlement phase - aggregate and calculate penalties"""
        logger.info(f"Settlement phase started for epoch {epoch}")

        state = self._epoch_states.get(epoch)
        if not state:
            return

        # Get all verification results
        results_by_proof = await self._commit_reveal.get_all_verification_results(epoch)

        if not results_by_proof:
            logger.info("No verification results to aggregate")
            return

        # Aggregate results
        aggregated = aggregate_epoch_results(results_by_proof, self.config)
        state.aggregated = {a.proof_id: a for a in aggregated}

        logger.info(f"Aggregated {len(aggregated)} verification results")

        # Summarize by orchestrator
        registry = self.get_registry()
        orchestrators = await registry.get_orchestrators_for_epoch(epoch)

        summaries = []
        for orch in orchestrators:
            orch_aggregated = [
                a for a in aggregated
                if a.orchestrator_hotkey == orch
            ]
            if orch_aggregated:
                summary = summarize_orchestrator_results(
                    orchestrator_hotkey=orch,
                    epoch=epoch,
                    aggregated_results=orch_aggregated,
                )
                summaries.append(summary)

        # Calculate orchestrator penalties
        orch_penalties = calculate_orchestrator_penalties(
            summaries=summaries,
            history=self._penalty_history,
            config=self.config,
        )
        state.orchestrator_penalties = orch_penalties

        # Calculate verifier penalties
        reveals = await self._commit_reveal.get_epoch_reveals(epoch)
        missing = await self._commit_reveal.get_missing_reveals(epoch)

        for verifier, reveal in reveals.items():
            verifier_results = [r.to_dict() for r in reveal.results]
            commitment = await self._commit_reveal.get_commitment(epoch, verifier)

            penalty = calculate_verifier_penalty(
                verifier_hotkey=verifier,
                epoch=epoch,
                verifier_results=verifier_results,
                aggregated_results=aggregated,
                commitment_status=commitment.status if commitment else None,
                is_late=False,  # Would check reveal timing
                history=self._penalty_history,
                config=self.config,
            )
            state.verifier_penalties[verifier] = penalty

        # Log summary
        penalized_orch = sum(1 for p in orch_penalties.values() if p.penalty_multiplier < 1.0)
        penalized_ver = sum(1 for p in state.verifier_penalties.values() if p.penalty_multiplier < 1.0)

        logger.info(
            f"Settlement complete: {penalized_orch}/{len(orch_penalties)} orchestrators penalized, "
            f"{penalized_ver}/{len(state.verifier_penalties)} verifiers penalized"
        )

    # =========================================================================
    # Penalty Access
    # =========================================================================

    def get_orchestrator_penalty(
        self,
        epoch: int,
        orchestrator_hotkey: str,
    ) -> Optional[OrchestratorPenalty]:
        """Get penalty for an orchestrator"""
        state = self._epoch_states.get(epoch)
        if not state:
            return None
        return state.orchestrator_penalties.get(orchestrator_hotkey)

    def get_penalty_multiplier(
        self,
        epoch: int,
        orchestrator_hotkey: str,
    ) -> float:
        """
        Get penalty multiplier for an orchestrator.

        Returns 1.0 if no penalty (full rewards).
        Returns < 1.0 if penalized.
        """
        penalty = self.get_orchestrator_penalty(epoch, orchestrator_hotkey)
        if penalty:
            return penalty.penalty_multiplier
        return 1.0

    def get_all_penalty_multipliers(self, epoch: int) -> Dict[str, float]:
        """Get all penalty multipliers for an epoch"""
        state = self._epoch_states.get(epoch)
        if not state:
            return {}

        return {
            orch: p.penalty_multiplier
            for orch, p in state.orchestrator_penalties.items()
        }

    def get_epoch_penalty_summary(self, epoch: int) -> Optional[EpochPenaltySummary]:
        """Get penalty summary for an epoch"""
        state = self._epoch_states.get(epoch)
        if not state:
            return None

        return summarize_epoch_penalties(
            epoch=epoch,
            orchestrator_penalties=state.orchestrator_penalties,
            verifier_penalties=state.verifier_penalties,
        )

    # =========================================================================
    # Proof Submission (for workers submitting to registry)
    # =========================================================================

    async def submit_proof(self, proof: ProofSubmission) -> Tuple[bool, str]:
        """
        Submit a proof to the registry.

        Workers call this after completing bandwidth tasks.
        """
        registry = self.get_registry()
        return await registry.submit_proof(proof)

    async def register_orchestrator_claim(
        self,
        epoch: int,
        orchestrator_hotkey: str,
        proof_id: bytes,
        claimed_bandwidth: float,
        claimed_reward: float,
    ) -> None:
        """
        Register an orchestrator's claimed score for a proof.

        Orchestrators call this to record what they claim a worker deserves.
        """
        registry = self.get_registry()
        await registry.register_orchestrator_claim(
            epoch=epoch,
            orchestrator_hotkey=orchestrator_hotkey,
            proof_id=proof_id,
            claimed_bandwidth=claimed_bandwidth,
            claimed_reward=claimed_reward,
        )

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get service statistics"""
        stats = {
            "current_epoch": self._current_epoch,
            "validator_hotkey": self.validator_hotkey[:16] + "...",
            "use_database": self.use_database,
            "epochs_tracked": len(self._epoch_states),
        }

        # Add current epoch state
        state = self._epoch_states.get(self._current_epoch)
        if state:
            stats["current_state"] = {
                "phase": state.phase.value,
                "proofs_verified": len(state.our_results),
                "has_commitment": state.commitment is not None,
                "reveal_submitted": state.reveal_submitted,
                "orchestrator_penalties": len(state.orchestrator_penalties),
                "verifier_penalties": len(state.verifier_penalties),
            }

        return stats


# =============================================================================
# Factory Functions
# =============================================================================

_service: Optional[CrossVerificationService] = None


def get_cross_verification_service() -> Optional[CrossVerificationService]:
    """Get the global cross-verification service instance"""
    return _service


def create_cross_verification_service(
    validator_hotkey: str,
    config: CrossVerificationConfig = DEFAULT_CONFIG,
    use_database: bool = False,
    proof_registry_url: Optional[str] = None,
) -> CrossVerificationService:
    """
    Create and set the global cross-verification service.

    Args:
        validator_hotkey: This validator's hotkey
        config: Cross-verification configuration
        use_database: Whether to use database persistence
        proof_registry_url: URL of external Proof Registry

    Returns:
        The new service instance
    """
    global _service
    _service = CrossVerificationService(
        validator_hotkey=validator_hotkey,
        config=config,
        use_database=use_database,
        proof_registry_url=proof_registry_url,
    )
    return _service
