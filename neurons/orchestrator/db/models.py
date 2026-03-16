"""SQLAlchemy models for payment proof persistence."""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    Boolean,
    DateTime,
    Text,
    Index,
    MetaData,
)
from sqlalchemy.orm import DeclarativeBase

# Use 'orchestrator' schema for all orchestrator tables
SCHEMA_NAME = "orchestrator"
metadata = MetaData(schema=SCHEMA_NAME)


class Base(DeclarativeBase):
    """Base class for all models."""
    metadata = metadata


class WorkerPayment(Base):
    """Individual worker payment record."""
    __tablename__ = "worker_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Epoch info
    epoch = Column(Integer, nullable=False, index=True)

    # Worker info
    worker_id = Column(String(64), nullable=False, index=True)
    worker_hotkey = Column(String(64), nullable=False, index=True)

    # Work done
    bytes_relayed = Column(BigInteger, nullable=False, default=0)
    tasks_completed = Column(Integer, nullable=False, default=0)

    # Payment
    amount_earned = Column(BigInteger, nullable=False, default=0)  # In nano units (1e-9)

    # Merkle proof for this payment
    merkle_proof = Column(Text, nullable=True)  # JSON array of hashes
    leaf_index = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_worker_payments_epoch_worker", "epoch", "worker_id"),
    )


class EpochPayment(Base):
    """Epoch-level payment summary with merkle root."""
    __tablename__ = "epoch_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Epoch info
    epoch = Column(Integer, nullable=False, unique=True, index=True)

    # Orchestrator info
    orchestrator_hotkey = Column(String(64), nullable=False)
    orchestrator_uid = Column(Integer, nullable=True)

    # Payment summary
    total_distributed = Column(BigInteger, nullable=False, default=0)  # In nano units
    worker_count = Column(Integer, nullable=False, default=0)
    total_bytes_relayed = Column(BigInteger, nullable=False, default=0)

    # Merkle root for all payments this epoch
    merkle_root = Column(String(66), nullable=False)  # 0x + 64 hex chars

    # Submission status
    submitted_to_validators = Column(Boolean, default=False)
    submission_time = Column(DateTime, nullable=True)

    # Verification status (updated by validators)
    verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaymentDispute(Base):
    """Worker disputes for missing/incorrect payments."""
    __tablename__ = "payment_disputes"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Dispute info
    epoch = Column(Integer, nullable=False, index=True)
    worker_id = Column(String(64), nullable=False, index=True)
    worker_hotkey = Column(String(64), nullable=False)

    # Claim
    claimed_bytes_relayed = Column(BigInteger, nullable=False)
    claimed_amount = Column(BigInteger, nullable=False)

    # What orchestrator recorded
    recorded_bytes_relayed = Column(BigInteger, nullable=True)
    recorded_amount = Column(BigInteger, nullable=True)

    # Resolution
    status = Column(String(20), default="pending")  # pending, resolved, rejected
    resolution_notes = Column(Text, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# =============================================================================
# New Persistence Tables (for state recovery on restart)
# =============================================================================


class RegisteredWorker(Base):
    """Persistent worker registry - survives orchestrator restart."""
    __tablename__ = "registered_workers"

    worker_id = Column(String(64), primary_key=True)
    hotkey = Column(String(64), nullable=False, index=True)
    ip = Column(String(45), nullable=False)
    port = Column(Integer, nullable=False)
    region = Column(String(32), nullable=False)

    # Payment address derivation (privacy-preserving)
    payment_pubkey = Column(String(128), nullable=True)  # Base pubkey for address derivation

    # Geographic coordinates
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Status
    status = Column(String(20), default="pending")  # pending, active, suspended, offline, banned

    # Performance metrics
    bandwidth_mbps = Column(Float, default=0.0)
    bandwidth_ema = Column(Float, default=0.0)
    latency_ms = Column(Float, default=0.0)
    success_rate = Column(Float, default=1.0)

    # Task tracking
    total_tasks = Column(Integer, default=0)
    successful_tasks = Column(Integer, default=0)
    failed_tasks = Column(Integer, default=0)
    max_concurrent_tasks = Column(Integer, default=10)

    # Bytes relayed
    bytes_relayed_total = Column(BigInteger, default=0)
    bytes_relayed_epoch = Column(BigInteger, default=0)

    # Trust scoring
    trust_score = Column(Float, default=0.5)
    fraud_score = Column(Float, default=0.0)

    # Reward tracking
    rewards_earned_epoch = Column(BigInteger, default=0)
    rewards_earned_total = Column(BigInteger, default=0)

    # Timestamps
    registered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_registered_workers_hotkey", "hotkey"),
        Index("ix_registered_workers_region", "region"),
        Index("ix_registered_workers_status", "status"),
    )


class BandwidthProofRecord(Base):
    """Individual PoB record from worker - persisted for validator queries."""
    __tablename__ = "bandwidth_proofs"

    task_id = Column(String(64), primary_key=True)
    worker_id = Column(String(64), nullable=False, index=True)
    worker_hotkey = Column(String(64), nullable=False, index=True)
    epoch = Column(Integer, nullable=False, index=True)

    # Timing (microseconds)
    start_time_us = Column(BigInteger, nullable=False)
    end_time_us = Column(BigInteger, nullable=False)

    # Metrics
    bytes_relayed = Column(BigInteger, nullable=False, default=0)
    bandwidth_mbps = Column(Float, nullable=False, default=0.0)

    # Verification
    chunk_hash = Column(String(64), nullable=True)
    canary_proof = Column(String(64), nullable=True)
    worker_signature = Column(Text, nullable=True)
    orchestrator_signature = Column(Text, nullable=True)

    # Regions
    source_region = Column(String(32), nullable=True)
    dest_region = Column(String(32), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_bandwidth_proofs_epoch", "epoch"),
        Index("ix_bandwidth_proofs_worker_epoch", "worker_id", "epoch"),
    )


class BandwidthTaskRecord(Base):
    """Task assignment record - for tracking active and completed tasks."""
    __tablename__ = "bandwidth_tasks"

    task_id = Column(String(64), primary_key=True)
    worker_id = Column(String(64), nullable=False, index=True)

    # Task details
    chunk_size = Column(Integer, nullable=False)
    chunk_hash = Column(String(64), nullable=False)
    source_region = Column(String(32), nullable=True)
    dest_region = Column(String(32), nullable=True)

    # Status
    status = Column(String(20), default="pending")  # pending, in_progress, completed, failed, timeout

    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    deadline_us = Column(BigInteger, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Anti-cheat (stored as hex)
    canary_hex = Column(String(64), nullable=True)
    canary_offset = Column(Integer, nullable=True)

    # Results
    bytes_relayed = Column(BigInteger, default=0)
    bandwidth_mbps = Column(Float, default=0.0)
    latency_ms = Column(Float, default=0.0)

    __table_args__ = (
        Index("ix_bandwidth_tasks_status", "status"),
        Index("ix_bandwidth_tasks_worker_status", "worker_id", "status"),
    )


class ReceiverCodeRecord(Base):
    """P2P transfer receiver codes - for direct worker-to-worker transfers."""
    __tablename__ = "receiver_codes"

    receiver_code = Column(String(32), primary_key=True)
    worker_id = Column(String(64), nullable=False, index=True)
    hotkey = Column(String(64), nullable=False)
    ip = Column(String(45), nullable=False)
    port = Column(Integer, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_receiver_codes_worker", "worker_id"),
    )
