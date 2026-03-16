"""Database connection and operations for payment proofs."""

import logging
import os
import ssl
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, text

from db.models import (
    Base,
    WorkerPayment,
    EpochPayment,
    PaymentDispute,
    RegisteredWorker,
    BandwidthProofRecord,
    BandwidthTaskRecord,
    ReceiverCodeRecord,
    SCHEMA_NAME,
)

logger = logging.getLogger(__name__)


def _fix_database_url(database_url: str) -> tuple[str, dict, str | None]:
    """
    Fix database URL for asyncpg compatibility.

    PostgreSQL with SSL requires special handling - asyncpg doesn't use 'sslmode' query param.
    Instead, we need to pass ssl context to create_async_engine.

    Environment Variables:
        DB_SSL_VERIFY: Set to 'false' to disable SSL certificate verification
                       (for development only). Default is 'true' (secure).

    Returns:
        tuple: (cleaned_url, connect_args, schema_name)
    """
    parsed = urlparse(database_url)
    query_params = parse_qs(parsed.query)

    # Check if SSL is required
    ssl_mode = query_params.pop('sslmode', [None])[0]
    query_params.pop('channel_binding', None)  # Remove unsupported param

    # Extract schema from options parameter (e.g., options=-c%20search_path=orchestrator)
    schema_name = None
    options = query_params.pop('options', [None])[0]
    if options:
        # Parse options like "-c search_path=orchestrator"
        if 'search_path=' in options:
            schema_name = options.split('search_path=')[-1].split(',')[0].strip()

    # Ensure we use asyncpg driver
    scheme = parsed.scheme
    if scheme == 'postgresql':
        scheme = 'postgresql+asyncpg'
    elif scheme == 'postgres':
        scheme = 'postgresql+asyncpg'

    # Rebuild URL without sslmode and options
    new_query = urlencode({k: v[0] for k, v in query_params.items()}, doseq=False)
    cleaned_url = urlunparse((
        scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))

    # Set up SSL context if needed
    connect_args = {}
    if ssl_mode in ('require', 'verify-ca', 'verify-full'):
        # Create SSL context for asyncpg
        ssl_context = ssl.create_default_context()

        # Check if SSL verification should be disabled (DEVELOPMENT ONLY)
        disable_ssl_verify = os.environ.get('DB_SSL_VERIFY', 'true').lower() in ('false', '0', 'no')

        if disable_ssl_verify:
            # WARNING: Only use in development environments
            logger.warning(
                "SSL certificate verification DISABLED (DB_SSL_VERIFY=false). "
                "This is insecure and should only be used in development!"
            )
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        else:
            # Production: Enable full certificate verification
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED

            # For verify-full mode, we need hostname checking
            # For verify-ca mode, we only verify the CA but not hostname
            if ssl_mode == 'verify-ca':
                ssl_context.check_hostname = False

        connect_args['ssl'] = ssl_context

    return cleaned_url, connect_args, schema_name


class Database:
    """Async database connection manager."""

    def __init__(self, database_url: str):
        """
        Initialize database connection.

        Args:
            database_url: PostgreSQL connection string
                         Format: postgresql+asyncpg://user:pass@host:port/dbname
        """
        self.database_url = database_url
        self.engine = None
        self.session_factory = None
        self.schema_name = None

    async def connect(self) -> None:
        """Create database connection and tables."""
        logger.info(f"Connecting to database...")

        # Fix URL for asyncpg compatibility
        cleaned_url, connect_args, url_schema = _fix_database_url(self.database_url)

        # Use schema from URL if provided, otherwise fall back to default
        self.schema_name = url_schema or SCHEMA_NAME

        self.engine = create_async_engine(
            cleaned_url,
            echo=False,  # Set to True for SQL debugging
            pool_size=10,
            max_overflow=20,
            connect_args=connect_args,
        )

        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Create schema if it doesn't exist, set search_path, then create tables
        async with self.engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {self.schema_name}"))
            await conn.execute(text(f"SET search_path TO {self.schema_name}"))
            await conn.run_sync(Base.metadata.create_all)

        logger.info(f"Database connected and tables created in schema '{self.schema_name}'")

    async def disconnect(self) -> None:
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database disconnected")

    @asynccontextmanager
    async def get_session(self):
        """Get a new database session with schema set."""
        async with self.session_factory() as session:
            # Set search_path for this session
            await session.execute(text(f"SET search_path TO {self.schema_name}"))
            yield session

    # =========================================================================
    # Worker Payments
    # =========================================================================

    async def save_worker_payment(
        self,
        epoch: int,
        worker_id: str,
        worker_hotkey: str,
        bytes_relayed: int,
        tasks_completed: int,
        amount_earned: int,
        merkle_proof: Optional[str] = None,
        leaf_index: Optional[int] = None,
    ) -> WorkerPayment:
        """Save a worker payment record."""
        async with self.get_session() as session:
            payment = WorkerPayment(
                epoch=epoch,
                worker_id=worker_id,
                worker_hotkey=worker_hotkey,
                bytes_relayed=bytes_relayed,
                tasks_completed=tasks_completed,
                amount_earned=amount_earned,
                merkle_proof=merkle_proof,
                leaf_index=leaf_index,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return payment

    async def save_worker_payments_batch(
        self,
        payments: List[Dict[str, Any]],
    ) -> List[WorkerPayment]:
        """Save multiple worker payments in a batch."""
        async with self.get_session() as session:
            payment_records = [
                WorkerPayment(**payment) for payment in payments
            ]
            session.add_all(payment_records)
            await session.commit()
            return payment_records

    async def get_worker_payments(
        self,
        worker_id: Optional[str] = None,
        worker_hotkey: Optional[str] = None,
        epoch: Optional[int] = None,
        limit: int = 100,
    ) -> List[WorkerPayment]:
        """Get worker payments with optional filters."""
        async with self.get_session() as session:
            query = select(WorkerPayment)

            conditions = []
            if worker_id:
                conditions.append(WorkerPayment.worker_id == worker_id)
            if worker_hotkey:
                conditions.append(WorkerPayment.worker_hotkey == worker_hotkey)
            if epoch is not None:
                conditions.append(WorkerPayment.epoch == epoch)

            if conditions:
                query = query.where(and_(*conditions))

            query = query.order_by(WorkerPayment.epoch.desc()).limit(limit)

            result = await session.execute(query)
            return result.scalars().all()

    async def get_worker_total_earnings(
        self,
        worker_id: str,
    ) -> Dict[str, Any]:
        """Get total earnings for a worker across all epochs."""
        payments = await self.get_worker_payments(worker_id=worker_id, limit=1000)

        total_earned = sum(p.amount_earned for p in payments)
        total_bytes = sum(p.bytes_relayed for p in payments)
        total_tasks = sum(p.tasks_completed for p in payments)

        return {
            "worker_id": worker_id,
            "total_earned": total_earned,
            "total_bytes_relayed": total_bytes,
            "total_tasks_completed": total_tasks,
            "epoch_count": len(payments),
            "payments": [
                {
                    "epoch": p.epoch,
                    "amount_earned": p.amount_earned,
                    "bytes_relayed": p.bytes_relayed,
                    "created_at": p.created_at.isoformat(),
                }
                for p in payments
            ],
        }

    # =========================================================================
    # Epoch Payments
    # =========================================================================

    async def save_epoch_payment(
        self,
        epoch: int,
        orchestrator_hotkey: str,
        orchestrator_uid: Optional[int],
        total_distributed: int,
        worker_count: int,
        total_bytes_relayed: int,
        merkle_root: str,
    ) -> EpochPayment:
        """Save an epoch payment summary."""
        async with self.get_session() as session:
            epoch_payment = EpochPayment(
                epoch=epoch,
                orchestrator_hotkey=orchestrator_hotkey,
                orchestrator_uid=orchestrator_uid,
                total_distributed=total_distributed,
                worker_count=worker_count,
                total_bytes_relayed=total_bytes_relayed,
                merkle_root=merkle_root,
            )
            session.add(epoch_payment)
            await session.commit()
            await session.refresh(epoch_payment)
            return epoch_payment

    async def get_epoch_payment(self, epoch: int) -> Optional[EpochPayment]:
        """Get epoch payment by epoch number."""
        async with self.get_session() as session:
            query = select(EpochPayment).where(EpochPayment.epoch == epoch)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_recent_epoch_payments(self, limit: int = 10) -> List[EpochPayment]:
        """Get recent epoch payments."""
        async with self.get_session() as session:
            query = (
                select(EpochPayment)
                .order_by(EpochPayment.epoch.desc())
                .limit(limit)
            )
            result = await session.execute(query)
            return result.scalars().all()

    async def mark_epoch_submitted(
        self,
        epoch: int,
    ) -> None:
        """Mark an epoch as submitted to validators."""
        async with self.get_session() as session:
            query = select(EpochPayment).where(EpochPayment.epoch == epoch)
            result = await session.execute(query)
            epoch_payment = result.scalar_one_or_none()

            if epoch_payment:
                epoch_payment.submitted_to_validators = True
                epoch_payment.submission_time = datetime.utcnow()
                await session.commit()

    async def mark_epoch_verified(
        self,
        epoch: int,
        verified: bool = True,
    ) -> None:
        """Mark an epoch as verified by validators."""
        async with self.get_session() as session:
            query = select(EpochPayment).where(EpochPayment.epoch == epoch)
            result = await session.execute(query)
            epoch_payment = result.scalar_one_or_none()

            if epoch_payment:
                epoch_payment.verified = verified
                epoch_payment.verified_at = datetime.utcnow()
                await session.commit()

    # =========================================================================
    # Disputes
    # =========================================================================

    async def create_dispute(
        self,
        epoch: int,
        worker_id: str,
        worker_hotkey: str,
        claimed_bytes_relayed: int,
        claimed_amount: int,
        recorded_bytes_relayed: Optional[int] = None,
        recorded_amount: Optional[int] = None,
    ) -> PaymentDispute:
        """Create a payment dispute."""
        async with self.get_session() as session:
            dispute = PaymentDispute(
                epoch=epoch,
                worker_id=worker_id,
                worker_hotkey=worker_hotkey,
                claimed_bytes_relayed=claimed_bytes_relayed,
                claimed_amount=claimed_amount,
                recorded_bytes_relayed=recorded_bytes_relayed,
                recorded_amount=recorded_amount,
            )
            session.add(dispute)
            await session.commit()
            await session.refresh(dispute)
            return dispute

    async def get_pending_disputes(self) -> List[PaymentDispute]:
        """Get all pending disputes."""
        async with self.get_session() as session:
            query = (
                select(PaymentDispute)
                .where(PaymentDispute.status == "pending")
                .order_by(PaymentDispute.created_at.asc())
            )
            result = await session.execute(query)
            return result.scalars().all()

    # =========================================================================
    # Registered Workers (Persistence for restart recovery)
    # =========================================================================

    async def save_worker(self, worker_data: Dict[str, Any]) -> RegisteredWorker:
        """Save or update a registered worker."""
        async with self.get_session() as session:
            # Check if worker already exists
            query = select(RegisteredWorker).where(
                RegisteredWorker.worker_id == worker_data["worker_id"]
            )
            result = await session.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing worker
                for key, value in worker_data.items():
                    if hasattr(existing, key) and key != "worker_id":
                        setattr(existing, key, value)
                existing.last_seen = datetime.utcnow()
                await session.commit()
                await session.refresh(existing)
                return existing
            else:
                # Create new worker
                worker = RegisteredWorker(**worker_data)
                session.add(worker)
                await session.commit()
                await session.refresh(worker)
                return worker

    async def get_worker(self, worker_id: str) -> Optional[RegisteredWorker]:
        """Get a worker by ID."""
        async with self.get_session() as session:
            query = select(RegisteredWorker).where(
                RegisteredWorker.worker_id == worker_id
            )
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_worker_by_hotkey(self, hotkey: str) -> Optional[RegisteredWorker]:
        """Get a worker by hotkey."""
        async with self.get_session() as session:
            query = select(RegisteredWorker).where(
                RegisteredWorker.hotkey == hotkey
            )
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_all_workers(
        self,
        status: Optional[str] = None,
        region: Optional[str] = None,
    ) -> List[RegisteredWorker]:
        """Get all registered workers with optional filters."""
        async with self.get_session() as session:
            query = select(RegisteredWorker)

            conditions = []
            if status:
                conditions.append(RegisteredWorker.status == status)
            if region:
                conditions.append(RegisteredWorker.region == region)

            if conditions:
                query = query.where(and_(*conditions))

            query = query.order_by(RegisteredWorker.registered_at.desc())
            result = await session.execute(query)
            return result.scalars().all()

    async def get_active_workers(self) -> List[RegisteredWorker]:
        """Get all active workers."""
        return await self.get_all_workers(status="active")

    async def update_worker_status(
        self,
        worker_id: str,
        status: str,
    ) -> None:
        """Update worker status."""
        async with self.get_session() as session:
            query = select(RegisteredWorker).where(
                RegisteredWorker.worker_id == worker_id
            )
            result = await session.execute(query)
            worker = result.scalar_one_or_none()

            if worker:
                worker.status = status
                worker.last_seen = datetime.utcnow()
                await session.commit()

    async def update_worker_metrics(
        self,
        worker_id: str,
        bandwidth_mbps: Optional[float] = None,
        latency_ms: Optional[float] = None,
        success_rate: Optional[float] = None,
        bytes_relayed: Optional[int] = None,
        tasks_completed: Optional[int] = None,
        tasks_failed: Optional[int] = None,
    ) -> None:
        """Update worker performance metrics."""
        async with self.get_session() as session:
            query = select(RegisteredWorker).where(
                RegisteredWorker.worker_id == worker_id
            )
            result = await session.execute(query)
            worker = result.scalar_one_or_none()

            if worker:
                if bandwidth_mbps is not None:
                    worker.bandwidth_mbps = bandwidth_mbps
                if latency_ms is not None:
                    worker.latency_ms = latency_ms
                if success_rate is not None:
                    worker.success_rate = success_rate
                if bytes_relayed is not None:
                    worker.bytes_relayed_total += bytes_relayed
                    worker.bytes_relayed_epoch += bytes_relayed
                if tasks_completed is not None:
                    worker.total_tasks += tasks_completed
                    worker.successful_tasks += tasks_completed
                if tasks_failed is not None:
                    worker.total_tasks += tasks_failed
                    worker.failed_tasks += tasks_failed

                worker.last_seen = datetime.utcnow()
                await session.commit()

    async def reset_epoch_stats(self) -> None:
        """Reset epoch-specific stats for all workers (called at epoch end)."""
        async with self.get_session() as session:
            workers = await session.execute(select(RegisteredWorker))
            for worker in workers.scalars().all():
                worker.bytes_relayed_epoch = 0
                worker.rewards_earned_epoch = 0
            await session.commit()

    async def delete_worker(self, worker_id: str) -> bool:
        """Delete a worker record."""
        async with self.get_session() as session:
            query = select(RegisteredWorker).where(
                RegisteredWorker.worker_id == worker_id
            )
            result = await session.execute(query)
            worker = result.scalar_one_or_none()

            if worker:
                await session.delete(worker)
                await session.commit()
                return True
            return False

    # =========================================================================
    # Bandwidth Proofs (PoB records from workers)
    # =========================================================================

    async def save_bandwidth_proof(
        self,
        proof_data: Dict[str, Any],
    ) -> BandwidthProofRecord:
        """Save a bandwidth proof from a worker."""
        async with self.get_session() as session:
            proof = BandwidthProofRecord(**proof_data)
            session.add(proof)
            await session.commit()
            await session.refresh(proof)
            return proof

    async def save_bandwidth_proofs_batch(
        self,
        proofs: List[Dict[str, Any]],
    ) -> int:
        """Save multiple bandwidth proofs in batch. Returns count saved."""
        async with self.get_session() as session:
            records = [BandwidthProofRecord(**proof) for proof in proofs]
            session.add_all(records)
            await session.commit()
            return len(records)

    async def get_bandwidth_proof(
        self,
        task_id: str,
    ) -> Optional[BandwidthProofRecord]:
        """Get a specific bandwidth proof by task ID."""
        async with self.get_session() as session:
            query = select(BandwidthProofRecord).where(
                BandwidthProofRecord.task_id == task_id
            )
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_bandwidth_proofs(
        self,
        worker_id: Optional[str] = None,
        epoch: Optional[int] = None,
        limit: int = 100,
    ) -> List[BandwidthProofRecord]:
        """Get bandwidth proofs with optional filters."""
        async with self.get_session() as session:
            query = select(BandwidthProofRecord)

            conditions = []
            if worker_id:
                conditions.append(BandwidthProofRecord.worker_id == worker_id)
            if epoch is not None:
                conditions.append(BandwidthProofRecord.epoch == epoch)

            if conditions:
                query = query.where(and_(*conditions))

            query = query.order_by(BandwidthProofRecord.created_at.desc()).limit(limit)
            result = await session.execute(query)
            return result.scalars().all()

    async def get_random_proofs_for_epoch(
        self,
        epoch: int,
        count: int = 10,
    ) -> List[BandwidthProofRecord]:
        """Get random bandwidth proofs for an epoch (for spot-checking)."""
        async with self.get_session() as session:
            # Use SQL RANDOM() for random sampling
            query = (
                select(BandwidthProofRecord)
                .where(BandwidthProofRecord.epoch == epoch)
                .order_by(text("RANDOM()"))
                .limit(count)
            )
            result = await session.execute(query)
            return result.scalars().all()

    async def get_epoch_proof_summary(
        self,
        epoch: int,
    ) -> Dict[str, Any]:
        """Get summary statistics for an epoch's proofs."""
        proofs = await self.get_bandwidth_proofs(epoch=epoch, limit=10000)

        if not proofs:
            return {"epoch": epoch, "total_proofs": 0}

        total_bytes = sum(p.bytes_relayed for p in proofs)
        unique_workers = set(p.worker_id for p in proofs)
        avg_bandwidth = sum(p.bandwidth_mbps for p in proofs) / len(proofs)

        return {
            "epoch": epoch,
            "total_proofs": len(proofs),
            "total_bytes_relayed": total_bytes,
            "unique_workers": len(unique_workers),
            "average_bandwidth_mbps": avg_bandwidth,
        }

    # =========================================================================
    # Bandwidth Tasks (Task assignments)
    # =========================================================================

    async def save_task(self, task_data: Dict[str, Any]) -> BandwidthTaskRecord:
        """Save a task assignment."""
        async with self.get_session() as session:
            task = BandwidthTaskRecord(**task_data)
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    async def get_task(self, task_id: str) -> Optional[BandwidthTaskRecord]:
        """Get a task by ID."""
        async with self.get_session() as session:
            query = select(BandwidthTaskRecord).where(
                BandwidthTaskRecord.task_id == task_id
            )
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_worker_tasks(
        self,
        worker_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[BandwidthTaskRecord]:
        """Get tasks for a worker."""
        async with self.get_session() as session:
            query = select(BandwidthTaskRecord).where(
                BandwidthTaskRecord.worker_id == worker_id
            )

            if status:
                query = query.where(BandwidthTaskRecord.status == status)

            query = query.order_by(BandwidthTaskRecord.created_at.desc()).limit(limit)
            result = await session.execute(query)
            return result.scalars().all()

    async def get_pending_tasks(self) -> List[BandwidthTaskRecord]:
        """Get all pending tasks."""
        async with self.get_session() as session:
            query = (
                select(BandwidthTaskRecord)
                .where(BandwidthTaskRecord.status.in_(["pending", "in_progress"]))
                .order_by(BandwidthTaskRecord.created_at.asc())
            )
            result = await session.execute(query)
            return result.scalars().all()

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        bytes_relayed: Optional[int] = None,
        bandwidth_mbps: Optional[float] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        """Update task status and results."""
        async with self.get_session() as session:
            query = select(BandwidthTaskRecord).where(
                BandwidthTaskRecord.task_id == task_id
            )
            result = await session.execute(query)
            task = result.scalar_one_or_none()

            if task:
                task.status = status
                if status == "in_progress":
                    task.started_at = datetime.utcnow()
                elif status in ("completed", "failed", "timeout"):
                    task.completed_at = datetime.utcnow()

                if bytes_relayed is not None:
                    task.bytes_relayed = bytes_relayed
                if bandwidth_mbps is not None:
                    task.bandwidth_mbps = bandwidth_mbps
                if latency_ms is not None:
                    task.latency_ms = latency_ms

                await session.commit()

    async def cleanup_stale_tasks(self, max_age_seconds: int = 3600) -> int:
        """Mark stale tasks as timeout. Returns count of tasks updated."""
        async with self.get_session() as session:
            cutoff = datetime.utcnow()
            query = select(BandwidthTaskRecord).where(
                and_(
                    BandwidthTaskRecord.status.in_(["pending", "in_progress"]),
                    BandwidthTaskRecord.created_at < cutoff,
                )
            )
            result = await session.execute(query)
            stale_tasks = result.scalars().all()

            count = 0
            for task in stale_tasks:
                # Check if deadline has passed (deadline_us is in microseconds)
                deadline_seconds = task.deadline_us / 1_000_000
                age_seconds = (cutoff - task.created_at).total_seconds()
                if age_seconds > max_age_seconds or age_seconds > deadline_seconds:
                    task.status = "timeout"
                    task.completed_at = cutoff
                    count += 1

            if count > 0:
                await session.commit()
            return count

    # =========================================================================
    # Receiver Codes (P2P transfer coordination)
    # =========================================================================

    async def save_receiver_code(
        self,
        receiver_code: str,
        worker_id: str,
        hotkey: str,
        ip: str,
        port: int,
        expires_at: Optional[datetime] = None,
    ) -> ReceiverCodeRecord:
        """Save a receiver code for P2P transfers."""
        async with self.get_session() as session:
            record = ReceiverCodeRecord(
                receiver_code=receiver_code,
                worker_id=worker_id,
                hotkey=hotkey,
                ip=ip,
                port=port,
                expires_at=expires_at,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get_receiver_code(
        self,
        receiver_code: str,
    ) -> Optional[ReceiverCodeRecord]:
        """Get receiver info by code."""
        async with self.get_session() as session:
            query = select(ReceiverCodeRecord).where(
                ReceiverCodeRecord.receiver_code == receiver_code
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            # Check if expired
            if record and record.expires_at and record.expires_at < datetime.utcnow():
                await session.delete(record)
                await session.commit()
                return None

            return record

    async def delete_receiver_code(self, receiver_code: str) -> bool:
        """Delete a receiver code."""
        async with self.get_session() as session:
            query = select(ReceiverCodeRecord).where(
                ReceiverCodeRecord.receiver_code == receiver_code
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if record:
                await session.delete(record)
                await session.commit()
                return True
            return False

    async def cleanup_expired_receiver_codes(self) -> int:
        """Remove expired receiver codes. Returns count deleted."""
        async with self.get_session() as session:
            now = datetime.utcnow()
            query = select(ReceiverCodeRecord).where(
                and_(
                    ReceiverCodeRecord.expires_at.isnot(None),
                    ReceiverCodeRecord.expires_at < now,
                )
            )
            result = await session.execute(query)
            expired = result.scalars().all()

            count = len(expired)
            for record in expired:
                await session.delete(record)

            if count > 0:
                await session.commit()
            return count


# Global database instance
_database: Optional[Database] = None


def get_database() -> Database:
    """Get the global database instance."""
    global _database
    if _database is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _database


async def init_database(database_url: str) -> Database:
    """Initialize the global database instance."""
    global _database
    _database = Database(database_url)
    await _database.connect()
    return _database


async def close_database() -> None:
    """Close the global database connection."""
    global _database
    if _database:
        await _database.disconnect()
        _database = None


# =============================================================================
# Subnet PoB Registry Publisher
# =============================================================================


class SubnetPoBPublisher:
    """
    Publishes PoB records to the subnet-level registry.

    This is a separate class that uses the beam.db module to write
    to the 'subnet' schema, distinct from the orchestrator's own schema.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._initialized = False
        self._session_factory = None

    async def initialize(self) -> None:
        """Initialize connection to subnet database."""
        if self._initialized:
            return

        try:
            from beam.db.base import get_engine, get_session_factory, init_db

            # Initialize subnet schema tables
            await init_db(self.database_url, echo=False)
            self._initialized = True
            logger.info("Subnet PoB publisher initialized")
        except ImportError:
            logger.warning("beam.db not available - subnet publishing disabled")
        except Exception as e:
            logger.error(f"Failed to initialize subnet publisher: {e}")

    async def publish_proof(
        self,
        task_id: str,
        epoch: int,
        orchestrator_hotkey: str,
        orchestrator_uid: int,
        worker_id: str,
        worker_hotkey: str,
        start_time_us: int,
        end_time_us: int,
        bytes_relayed: int,
        bandwidth_mbps: float,
        chunk_hash: str,
        worker_signature: str,
        orchestrator_signature: str,
        canary_proof: Optional[str] = None,
        source_region: Optional[str] = None,
        dest_region: Optional[str] = None,
    ) -> bool:
        """
        Publish a PoB to the subnet registry.

        Returns True if successful, False otherwise.
        """
        if not self._initialized:
            logger.debug("Subnet publisher not initialized, skipping publish")
            return False

        try:
            from beam.db.base import get_session
            from beam.db.subnet_pob_repository import SubnetPoBRepository

            async with get_session() as session:
                repo = SubnetPoBRepository(session)
                await repo.publish_proof(
                    task_id=task_id,
                    epoch=epoch,
                    orchestrator_hotkey=orchestrator_hotkey,
                    orchestrator_uid=orchestrator_uid,
                    worker_id=worker_id,
                    worker_hotkey=worker_hotkey,
                    start_time_us=start_time_us,
                    end_time_us=end_time_us,
                    bytes_relayed=bytes_relayed,
                    bandwidth_mbps=bandwidth_mbps,
                    chunk_hash=chunk_hash,
                    worker_signature=worker_signature,
                    orchestrator_signature=orchestrator_signature,
                    canary_proof=canary_proof,
                    source_region=source_region,
                    dest_region=dest_region,
                )
                logger.debug(f"Published PoB {task_id} to subnet registry")
                return True

        except Exception as e:
            logger.error(f"Failed to publish PoB to subnet: {e}")
            return False


# Global subnet publisher instance
_subnet_publisher: Optional[SubnetPoBPublisher] = None


def get_subnet_publisher() -> Optional[SubnetPoBPublisher]:
    """Get the subnet publisher instance."""
    return _subnet_publisher


async def init_subnet_publisher(database_url: str) -> Optional[SubnetPoBPublisher]:
    """Initialize the subnet publisher."""
    global _subnet_publisher
    _subnet_publisher = SubnetPoBPublisher(database_url)
    await _subnet_publisher.initialize()
    return _subnet_publisher
