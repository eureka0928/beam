"""
Orchestrator Management API Routes

Endpoints for orchestrator registration, datastream creation, and SLA management.
These endpoints allow orchestrators to register with the subnet and manage their datastreams.

UID Allocation:
- UID 1: Subnet orchestrator (not registrable)
- UIDs 2-256: Public orchestrators (all equal, rentable via /register endpoint)
"""

import logging
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from core.orchestrator import get_orchestrator, Orchestrator

# Orchestrator UID range constants (was in beam.scoring.sla)
PUBLIC_ORCHESTRATOR_UID_START = 2
PUBLIC_ORCHESTRATOR_UID_END = 256

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrators", tags=["orchestrators"])


# =============================================================================
# Request/Response Models
# =============================================================================

class OrchestratorRegistrationRequest(BaseModel):
    """Request to register a new orchestrator (UIDs 2-256)."""
    uid: int = Field(
        ...,
        ge=PUBLIC_ORCHESTRATOR_UID_START,
        le=PUBLIC_ORCHESTRATOR_UID_END,
        description=f"Orchestrator UID ({PUBLIC_ORCHESTRATOR_UID_START}-{PUBLIC_ORCHESTRATOR_UID_END})"
    )
    hotkey: str = Field(..., min_length=48, description="Bittensor hotkey")
    stake_tao: float = Field(..., ge=100.0, description="Stake amount in TAO (min 100)")
    registration_cost_tao: Optional[float] = Field(
        None,
        ge=0.0,
        description="Hotkey registration cost in TAO (burned to register miner hotkey and receive UID). "
                    "Should be fetched from chain via subtensor.recycle(netuid). Uses default if not provided."
    )
    name: Optional[str] = Field(None, max_length=100, description="Orchestrator name")
    description: Optional[str] = Field(None, max_length=500, description="Description")
    contact: Optional[str] = Field(None, max_length=100, description="Contact info (email/discord)")


class OrchestratorRegistrationResponse(BaseModel):
    """Response from orchestrator registration."""
    success: bool
    uid: Optional[int] = None
    hotkey: Optional[str] = None
    status: Optional[str] = None
    grace_period_ends: Optional[datetime] = None
    stake_tao: Optional[float] = None
    registration_cost_tao: Optional[float] = None
    total_cost_tao: Optional[float] = None
    message: str


class OrchestratorUpdateRequest(BaseModel):
    """Request to update orchestrator info."""
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    contact: Optional[str] = Field(None, max_length=100)


class DatastreamCreateRequest(BaseModel):
    """Request to create a new datastream."""
    datastream_id: str = Field(..., min_length=1, max_length=64, description="Unique datastream ID")
    name: str = Field(..., min_length=1, max_length=100, description="Datastream name")
    stake_tao: float = Field(10.0, ge=10.0, description="Stake amount in TAO (min 10)")
    description: Optional[str] = Field(None, max_length=500, description="Description")


class DatastreamResponse(BaseModel):
    """Datastream information."""
    datastream_id: str
    orchestrator_uid: int
    name: str
    stake_tao: float
    status: str
    created_at: datetime
    total_bytes_transferred: int = 0
    total_transfers: int = 0
    success_rate: float = 0.0


class OrchestratorInfo(BaseModel):
    """Orchestrator information response."""
    uid: int
    hotkey: str
    stake_tao: float
    registration_cost_tao: float = 0.0
    total_cost_tao: float = 0.0
    status: str
    is_subnet_owned: bool
    name: Optional[str] = None
    description: Optional[str] = None
    contact: Optional[str] = None
    registered_at: datetime
    grace_period_ends: Optional[datetime] = None
    worker_count: int = 0
    datastream_count: int = 0
    sla_score: Optional[float] = None
    stake_weight: Optional[float] = None  # Stake-based routing weight multiplier


class OrchestratorSLAResponse(BaseModel):
    """SLA metrics for an orchestrator."""
    uid: int
    uptime_percent: float
    bandwidth_mbps: float
    latency_p95_ms: float
    acceptance_rate_percent: float
    success_rate_percent: float
    combined_multiplier: float
    effective_multiplier: float
    penalty_redirect_percent: float
    in_grace_period: bool
    violations: List[str] = []


class WorkerAffiliationRequest(BaseModel):
    """Request to affiliate a worker with an orchestrator."""
    worker_id: str = Field(..., description="Worker ID")
    worker_hotkey: str = Field(..., description="Worker's hotkey")
    stake_tao: float = Field(0.0, ge=0.0, description="Worker stake in TAO")


class WorkerAffiliationResponse(BaseModel):
    """Response from worker affiliation."""
    success: bool
    worker_id: str
    orchestrator_uid: int
    has_stake_bonus: bool
    message: str


# =============================================================================
# Dependency
# =============================================================================

def get_orchestrator_instance() -> Orchestrator:
    """Get Orchestrator instance."""
    return get_orchestrator()


# =============================================================================
# Orchestrator Registration
# =============================================================================

@router.post("/register", response_model=OrchestratorRegistrationResponse)
async def register_orchestrator(
    request: OrchestratorRegistrationRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """
    Register a new orchestrator with the subnet.

    Requirements:
    - UID must be between 2-256 (valid orchestrator range)
    - Minimum stake of 100 TAO
    - Valid Bittensor hotkey

    Higher stake amounts increase traffic routing priority (alongside SLA scores).
    Stake weight formula: min((stake / 100) ^ 0.5, 3.0)
    - 100 TAO -> 1.0x weight
    - 400 TAO -> 2.0x weight
    - 900+ TAO -> 3.0x weight (capped)

    New orchestrators enter a 24-hour grace period during which
    SLA penalties are not applied.
    """
    try:
        # Check if manager is available
        if not hasattr(orchestrator, 'orch_manager') or orchestrator.orch_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Orchestrator manager not initialized"
            )

        # Check for async method (PersistentOrchestratorManager) vs sync (OrchestratorManager)
        if hasattr(orchestrator.orch_manager, 'register_orchestrator_async'):
            orch = await orchestrator.orch_manager.register_orchestrator_async(
                uid=request.uid,
                hotkey=request.hotkey,
                stake_tao=request.stake_tao,
                name=request.name,
                description=request.description,
                contact=request.contact,
                registration_cost_tao=request.registration_cost_tao,
            )
        else:
            orch = orchestrator.orch_manager.register_orchestrator(
                uid=request.uid,
                hotkey=request.hotkey,
                stake_tao=request.stake_tao,
                name=request.name,
                description=request.description,
                contact=request.contact,
                registration_cost_tao=request.registration_cost_tao,
            )

        return OrchestratorRegistrationResponse(
            success=True,
            uid=orch.uid,
            hotkey=orch.hotkey,
            status=orch.status.value,
            grace_period_ends=orch.grace_period_ends,
            stake_tao=orch.stake_tao,
            registration_cost_tao=orch.registration_cost_tao,
            total_cost_tao=orch.total_cost_tao,
            message=f"Orchestrator UID {orch.uid} registered successfully. "
                    f"Stake: {orch.stake_tao} TAO (refundable), "
                    f"Registration cost: {orch.registration_cost_tao} TAO (burned), "
                    f"Total: {orch.total_cost_tao} TAO. "
                    f"Grace period ends: {orch.grace_period_ends}",
        )

    except ValueError as e:
        return OrchestratorRegistrationResponse(
            success=False,
            message=str(e),
        )
    except Exception as e:
        logger.error(f"Orchestrator registration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{uid}")
async def deregister_orchestrator(
    uid: int,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """
    Deregister an orchestrator from the subnet.

    All workers affiliated with this orchestrator will be
    reassigned to Orchestrator #1 (subnet default).
    """
    if uid == 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot deregister subnet orchestrator #1"
        )

    try:
        if not hasattr(orchestrator, 'orch_manager'):
            raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

        if hasattr(orchestrator.orch_manager, 'deregister_orchestrator_async'):
            success = await orchestrator.orch_manager.deregister_orchestrator_async(uid)
        else:
            success = orchestrator.orch_manager.deregister_orchestrator(uid)

        if success:
            return {"success": True, "message": f"Orchestrator UID {uid} deregistered"}
        else:
            raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Orchestrator deregistration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{uid}", response_model=OrchestratorInfo)
async def update_orchestrator(
    uid: int,
    request: OrchestratorUpdateRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Update orchestrator information."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    # Update fields
    if request.name is not None:
        orch.name = request.name
    if request.description is not None:
        orch.description = request.description
    if request.contact is not None:
        orch.contact = request.contact

    return OrchestratorInfo(
        uid=orch.uid,
        hotkey=orch.hotkey,
        stake_tao=orch.stake_tao,
        registration_cost_tao=getattr(orch, 'registration_cost_tao', 0.0),
        total_cost_tao=getattr(orch, 'total_cost_tao', orch.stake_tao),
        status=orch.status.value,
        is_subnet_owned=orch.is_subnet_owned,
        name=orch.name,
        description=orch.description,
        contact=orch.contact,
        registered_at=orch.registered_at,
        grace_period_ends=orch.grace_period_ends,
        worker_count=len(orch.worker_hotkeys),
        datastream_count=len([d for d in orch.datastreams.values() if d.is_active]),
        sla_score=orch.sla_state.score.effective_multiplier if orch.sla_state and orch.sla_state.score else None,
    )


# =============================================================================
# Orchestrator Queries
# =============================================================================

@router.get("/{uid}", response_model=OrchestratorInfo)
async def get_orchestrator_info(
    uid: int,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Get information about a specific orchestrator."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    return OrchestratorInfo(
        uid=orch.uid,
        hotkey=orch.hotkey,
        stake_tao=orch.stake_tao,
        registration_cost_tao=getattr(orch, 'registration_cost_tao', 0.0),
        total_cost_tao=getattr(orch, 'total_cost_tao', orch.stake_tao),
        status=orch.status.value,
        is_subnet_owned=orch.is_subnet_owned,
        name=orch.name,
        description=orch.description,
        contact=orch.contact,
        registered_at=orch.registered_at,
        grace_period_ends=orch.grace_period_ends,
        worker_count=len(orch.worker_hotkeys),
        datastream_count=len([d for d in orch.datastreams.values() if d.is_active]),
        sla_score=orch.sla_state.score.effective_multiplier if orch.sla_state and orch.sla_state.score else None,
    )


@router.get("/", response_model=List[OrchestratorInfo])
async def list_orchestrators(
    status: Optional[str] = Query(None, description="Filter by status"),
    min_stake: float = Query(0.0, description="Minimum stake TAO"),
    include_subnet: bool = Query(True, description="Include subnet orchestrator #1"),
    limit: int = Query(100, le=256, description="Max results"),
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """List all registered orchestrators."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orchestrators = list(orchestrator.orch_manager.orchestrators.values())

    # Apply filters
    if not include_subnet:
        orchestrators = [o for o in orchestrators if not o.is_subnet_owned]

    if status:
        orchestrators = [o for o in orchestrators if o.status.value == status]

    if min_stake > 0:
        orchestrators = [o for o in orchestrators if o.stake_tao >= min_stake]

    # Sort by UID
    orchestrators.sort(key=lambda o: o.uid)

    # Apply limit
    orchestrators = orchestrators[:limit]

    return [
        OrchestratorInfo(
            uid=o.uid,
            hotkey=o.hotkey,
            stake_tao=o.stake_tao,
            registration_cost_tao=getattr(o, 'registration_cost_tao', 0.0),
            total_cost_tao=getattr(o, 'total_cost_tao', o.stake_tao),
            status=o.status.value,
            is_subnet_owned=o.is_subnet_owned,
            name=o.name,
            description=o.description,
            contact=o.contact,
            registered_at=o.registered_at,
            grace_period_ends=o.grace_period_ends,
            worker_count=len(o.worker_hotkeys),
            datastream_count=len([d for d in o.datastreams.values() if d.is_active]),
            sla_score=o.sla_state.score.effective_multiplier if o.sla_state and o.sla_state.score else None,
        )
        for o in orchestrators
    ]


@router.get("/{uid}/sla", response_model=OrchestratorSLAResponse)
async def get_orchestrator_sla(
    uid: int,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Get SLA metrics for an orchestrator."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    if not orch.sla_state or not orch.sla_state.metrics:
        return OrchestratorSLAResponse(
            uid=uid,
            uptime_percent=0.0,
            bandwidth_mbps=0.0,
            latency_p95_ms=0.0,
            acceptance_rate_percent=0.0,
            success_rate_percent=0.0,
            combined_multiplier=1.0,
            effective_multiplier=1.0,
            penalty_redirect_percent=0.0,
            in_grace_period=orch.in_grace_period,
            violations=[],
        )

    metrics = orch.sla_state.metrics
    score = orch.sla_state.score

    return OrchestratorSLAResponse(
        uid=uid,
        uptime_percent=metrics.uptime_percent,
        bandwidth_mbps=metrics.bandwidth_mbps,
        latency_p95_ms=metrics.latency_p95_ms,
        acceptance_rate_percent=metrics.acceptance_rate_percent,
        success_rate_percent=metrics.success_rate_percent,
        combined_multiplier=score.combined_multiplier if score else 1.0,
        effective_multiplier=score.effective_multiplier if score else 1.0,
        penalty_redirect_percent=score.penalty_redirect_percent if score else 0.0,
        in_grace_period=score.in_grace_period if score else orch.in_grace_period,
        violations=[v.value for v in score.violations] if score else [],
    )


# =============================================================================
# Datastream Management
# =============================================================================

@router.post("/{uid}/datastreams", response_model=DatastreamResponse)
async def create_datastream(
    uid: int,
    request: DatastreamCreateRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """
    Create a new datastream for an orchestrator.

    Requirements:
    - Minimum stake of 10 TAO per datastream
    - Maximum 50 datastreams per orchestrator
    - Unique datastream ID
    """
    try:
        if not hasattr(orchestrator, 'orch_manager'):
            raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

        if hasattr(orchestrator.orch_manager, 'create_datastream_async'):
            ds = await orchestrator.orch_manager.create_datastream_async(
                orchestrator_uid=uid,
                datastream_id=request.datastream_id,
                name=request.name,
                stake_tao=request.stake_tao,
                description=request.description,
            )
        else:
            # Base OrchestratorManager doesn't have description param
            ds = orchestrator.orch_manager.create_datastream(
                orchestrator_uid=uid,
                datastream_id=request.datastream_id,
                name=request.name,
                stake_tao=request.stake_tao,
            )

        return DatastreamResponse(
            datastream_id=ds.datastream_id,
            orchestrator_uid=uid,
            name=ds.name,
            stake_tao=ds.stake_tao,
            status=ds.status.value,
            created_at=ds.created_at,
            total_bytes_transferred=ds.total_bytes_transferred,
            total_transfers=ds.total_transfers,
            success_rate=ds.success_rate,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Datastream creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{uid}/datastreams", response_model=List[DatastreamResponse])
async def list_datastreams(
    uid: int,
    include_terminated: bool = Query(False, description="Include terminated datastreams"),
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """List all datastreams for an orchestrator."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    datastreams = list(orch.datastreams.values())

    if not include_terminated:
        datastreams = [d for d in datastreams if d.is_active]

    return [
        DatastreamResponse(
            datastream_id=d.datastream_id,
            orchestrator_uid=uid,
            name=d.name,
            stake_tao=d.stake_tao,
            status=d.status.value,
            created_at=d.created_at,
            total_bytes_transferred=d.total_bytes_transferred,
            total_transfers=d.total_transfers,
            success_rate=d.success_rate,
        )
        for d in datastreams
    ]


@router.get("/{uid}/datastreams/{datastream_id}", response_model=DatastreamResponse)
async def get_datastream(
    uid: int,
    datastream_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Get information about a specific datastream."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    ds = orch.datastreams.get(datastream_id)
    if not ds:
        raise HTTPException(status_code=404, detail=f"Datastream {datastream_id} not found")

    return DatastreamResponse(
        datastream_id=ds.datastream_id,
        orchestrator_uid=uid,
        name=ds.name,
        stake_tao=ds.stake_tao,
        status=ds.status.value,
        created_at=ds.created_at,
        total_bytes_transferred=ds.total_bytes_transferred,
        total_transfers=ds.total_transfers,
        success_rate=ds.success_rate,
    )


@router.delete("/{uid}/datastreams/{datastream_id}")
async def terminate_datastream(
    uid: int,
    datastream_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Terminate a datastream."""
    try:
        if not hasattr(orchestrator, 'orch_manager'):
            raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

        if hasattr(orchestrator.orch_manager, 'terminate_datastream_async'):
            success = await orchestrator.orch_manager.terminate_datastream_async(uid, datastream_id)
        else:
            success = orchestrator.orch_manager.terminate_datastream(uid, datastream_id)

        if success:
            return {"success": True, "message": f"Datastream {datastream_id} terminated"}
        else:
            raise HTTPException(status_code=404, detail=f"Datastream {datastream_id} not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Datastream termination error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Worker Affiliation
# =============================================================================

@router.post("/{uid}/workers", response_model=WorkerAffiliationResponse)
async def affiliate_worker(
    uid: int,
    request: WorkerAffiliationRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """
    Affiliate a worker with this orchestrator.

    Workers can voluntarily choose an orchestrator. Workers with
    stake >= 1 TAO receive a 1.5x traffic bonus.
    """
    try:
        if not hasattr(orchestrator, 'orch_manager'):
            raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

        if hasattr(orchestrator.orch_manager, 'affiliate_worker_async'):
            success = await orchestrator.orch_manager.affiliate_worker_async(
                worker_id=request.worker_id,
                worker_hotkey=request.worker_hotkey,
                orchestrator_uid=uid,
                stake_tao=request.stake_tao,
            )
        else:
            success = orchestrator.orch_manager.affiliate_worker(
                worker_id=request.worker_id,
                worker_hotkey=request.worker_hotkey,
                orchestrator_uid=uid,
                stake_tao=request.stake_tao,
            )

        if success:
            has_bonus = request.stake_tao >= 1.0
            return WorkerAffiliationResponse(
                success=True,
                worker_id=request.worker_id,
                orchestrator_uid=uid,
                has_stake_bonus=has_bonus,
                message=f"Worker affiliated with orchestrator UID {uid}. "
                        f"Stake bonus: {'Yes (1.5x traffic)' if has_bonus else 'No (stake < 1 TAO)'}",
            )
        else:
            return WorkerAffiliationResponse(
                success=False,
                worker_id=request.worker_id,
                orchestrator_uid=uid,
                has_stake_bonus=False,
                message=f"Failed to affiliate worker with orchestrator UID {uid}",
            )

    except Exception as e:
        logger.error(f"Worker affiliation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{uid}/workers")
async def list_affiliated_workers(
    uid: int,
    limit: int = Query(100, le=1000, description="Max results"),
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """List all workers affiliated with this orchestrator."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orch = orchestrator.orch_manager.get_orchestrator(uid)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestrator UID {uid} not found")

    workers = list(orch.worker_hotkeys)[:limit]

    return {
        "orchestrator_uid": uid,
        "total_workers": len(orch.worker_hotkeys),
        "workers": [{"hotkey": hk[:16] + "..."} for hk in workers],
    }


# =============================================================================
# Statistics
# =============================================================================

@router.get("/stats/summary")
async def get_orchestrator_stats(
    orchestrator: Orchestrator = Depends(get_orchestrator_instance),
):
    """Get aggregate orchestrator statistics."""
    if not hasattr(orchestrator, 'orch_manager'):
        raise HTTPException(status_code=503, detail="Orchestrator manager not initialized")

    orchestrators = list(orchestrator.orch_manager.orchestrators.values())
    non_subnet = [o for o in orchestrators if not o.is_subnet_owned]

    # Status distribution
    status_counts = {}
    for o in orchestrators:
        status = o.status.value
        status_counts[status] = status_counts.get(status, 0) + 1

    # Total stake
    total_stake = sum(o.stake_tao for o in non_subnet)

    # Total workers
    total_workers = sum(len(o.worker_hotkeys) for o in orchestrators)

    # Total datastreams
    total_datastreams = sum(
        len([d for d in o.datastreams.values() if d.is_active])
        for o in orchestrators
    )

    # Average SLA score (excluding grace period)
    sla_scores = [
        o.sla_state.score.effective_multiplier
        for o in non_subnet
        if o.sla_state and o.sla_state.score and not o.in_grace_period
    ]
    avg_sla_score = sum(sla_scores) / len(sla_scores) if sla_scores else 1.0

    return {
        "total_orchestrators": len(orchestrators),
        "non_subnet_orchestrators": len(non_subnet),
        "orchestrators_by_status": status_counts,
        "total_stake_tao": total_stake,
        "total_workers": total_workers,
        "total_datastreams": total_datastreams,
        "avg_sla_score": round(avg_sla_score, 4),
        "subnet_orchestrator": {
            "uid": 1,
            "workers": len(orchestrators[0].worker_hotkeys) if orchestrators else 0,
        } if orchestrators and orchestrators[0].is_subnet_owned else None,
    }
