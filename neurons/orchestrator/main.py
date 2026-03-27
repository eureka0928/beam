"""
BEAM Orchestrator Entry Point

Run with: python -m neurons.orchestrator.main

The Orchestrator coordinates bandwidth tasks with BeamCore:
- Registers with BeamCore on startup
- Sends periodic heartbeats with status
- Polls BeamCore for transfer assignments
- Manages local worker pool
- Submits proof-of-bandwidth to BeamCore

Architecture:
┌────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR                               │
│                                                                    │
│  BeamCore ◀────── Register/Heartbeat ──────▶ Transfer Assignments  │
│      │                                              │              │
│      ▼                                              ▼              │
│  PoB Submission ◀──── Task Results ◀──── Worker Pool               │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

All coordination flows through BeamCore. Workers connect to BeamCore,
not directly to orchestrators.
"""

import asyncio
import logging
import os
import random
import signal
import socket
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from core.orchestrator import Orchestrator, get_orchestrator
from core.config import get_settings
# Cluster mode removed - running in standalone mode only
# from core.cluster import (
#     ClusterConfig,
#     ClusterState,
#     ClusterCoordinator,
#     ClusterNode,
#     Region,
# )
from routes import health, orchestrators
from middleware.rate_limiting import RateLimitMiddleware, get_rate_limiter, RateLimitConfig
from middleware.metrics import MetricsMiddleware, get_metrics_collector, get_metrics_response

# ---------------------------------------------------------------------------
# Core API WebSocket-based registration and heartbeat
# ---------------------------------------------------------------------------
import httpx
import json
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

_core_api_ws_task: Optional[asyncio.Task] = None
_core_api_ws: Optional[websockets.WebSocketClientProtocol] = None

# Pending worker-list requests keyed by transfer_id.
_worker_list_futures: dict[str, asyncio.Future] = {}


def _coerce_worker_metric(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_worker_list(workers: list[dict[str, Any]], transfer_id: str) -> list[dict[str, Any]]:
    normalized_workers: list[dict[str, Any]] = []
    skipped_workers = 0

    for worker in workers:
        worker_id = worker.get("worker_id") or worker.get("workerId")
        if not worker_id:
            skipped_workers += 1
            continue

        normalized_workers.append({
            **worker,
            "worker_id": worker_id,
            "trust_score": _coerce_worker_metric(
                worker.get("trust_score", worker.get("trustScore")),
                0.5,
            ),
            "bandwidth_mbps": _coerce_worker_metric(
                worker.get("bandwidth_mbps", worker.get("bandwidthMbps")),
                100.0,
            ),
        })

    if skipped_workers:
        logging.getLogger(__name__).warning(
            "Skipped %s malformed worker entries for transfer %s",
            skipped_workers,
            transfer_id,
        )

    return normalized_workers


def _sign_message(wallet, message: str) -> str:
    """Sign a message with the wallet's hotkey."""
    try:
        signature = wallet.hotkey.sign(message.encode())
        return signature.hex()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to sign message: {e}")
        return ""


async def _get_api_key(settings, wallet) -> Optional[str]:
    """
    Get API key from BeamCore using challenge/verify flow.

    Returns API key (b1m_xxx format) or None if auth fails.
    """
    import httpx
    logger = logging.getLogger(__name__)
    hotkey = wallet.hotkey.ss58_address
    base_url = settings.subnet_core_url

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Request challenge
            challenge_resp = await client.post(
                f"{base_url}/auth/challenge",
                json={"hotkey": hotkey, "role": "orchestrator"},
            )
            if challenge_resp.status_code != 200:
                logger.error(f"Failed to get auth challenge: {challenge_resp.status_code}")
                return None

            challenge_data = challenge_resp.json()
            challenge_id = challenge_data["challenge_id"]
            message = challenge_data["message"]

            # Step 2: Sign the challenge
            signature = _sign_message(wallet, message)
            if not signature:
                logger.error("Failed to sign challenge")
                return None

            # Step 3: Verify and get API key
            verify_resp = await client.post(
                f"{base_url}/auth/verify",
                json={
                    "challenge_id": challenge_id,
                    "hotkey": hotkey,
                    "signature": "0x" + signature if not signature.startswith("0x") else signature,
                    "key_name": "Orchestrator Main WebSocket Key",
                },
            )
            if verify_resp.status_code != 200:
                logger.error(f"Failed to verify signature: {verify_resp.status_code}")
                return None

            verify_data = verify_resp.json()
            if verify_data.get("success") and verify_data.get("api_key"):
                logger.info(f"Obtained API key for main WebSocket: {hotkey[:16]}...")
                return verify_data["api_key"]

            logger.error(f"Auth verify failed: {verify_data.get('message', 'Unknown')}")
            return None

    except Exception as e:
        logger.error(f"Failed to get API key: {e}")
        return None


async def _handle_transfer_assigned(
    ws: websockets.WebSocketClientProtocol,
    data: dict,
) -> None:
    log = logging.getLogger(__name__)
    assignment_id = data.get("assignment_id")
    transfer_id = data.get("transfer_id")
    chunk_start = int(data.get("chunk_start", 0))
    chunk_end = int(data.get("chunk_end", 0))

    log.info(f"transfer_assigned: transfer={transfer_id} chunks={chunk_start}-{chunk_end}")

    try:
        # Request affiliated worker pool over WS.
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _worker_list_futures[transfer_id] = fut
        try:
            await ws.send(json.dumps({"type": "list_workers", "transfer_id": transfer_id}))
            workers = await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            log.error(f"Timed out waiting for worker_list for transfer {transfer_id}")
            _worker_list_futures.pop(transfer_id, None)
            return
        except Exception as e:
            log.error(f"Failed to get worker list for transfer {transfer_id}: {e}")
            _worker_list_futures.pop(transfer_id, None)
            return

        normalized_workers = _normalize_worker_list(workers, transfer_id)
        if not normalized_workers:
            log.warning(f"No compatible workers available for assignment {assignment_id}")
            return

        def sla_score(worker: dict[str, Any]) -> float:
            trust = worker["trust_score"]
            bandwidth = worker["bandwidth_mbps"]
            return trust * min(2.0, bandwidth / 100.0)

        sorted_workers = sorted(normalized_workers, key=sla_score, reverse=True)
        worker_ids = [worker["worker_id"] for worker in sorted_workers]

        assignments = [
            {"chunk_index": i, "worker_id": worker_ids[i % len(worker_ids)]}
            for i in range(chunk_start, chunk_end + 1)
        ]

        await ws.send(json.dumps({
            "type": "chunk_assignments",
            "assignment_id": assignment_id,
            "assignments": assignments,
        }))
        log.info(f"Sent {len(assignments)} chunk_assignments for assignment {assignment_id}")
    except Exception:
        log.exception(
            "Failed to process transfer_assigned for transfer %s assignment %s",
            transfer_id,
            assignment_id,
        )


# Grep-friendly prefix for BeamCore orchestrator registration / register_result lines
BEAMCORE_REGISTER_LOG = "[BEAMCORE_REGISTER]"


async def _connect_and_register_ws(settings, wallet, get_worker_count, get_balance_info=None, get_uid=None):
    """
    Connect to BeamCore via WebSocket and register/send heartbeats.

    This replaces the HTTP-based registration and heartbeat with a single
    persistent WebSocket connection.
    """
    global _core_api_ws
    logger = logging.getLogger(__name__)
    hotkey = wallet.hotkey.ss58_address

    # Get API key for WebSocket auth
    api_key = await _get_api_key(settings, wallet)
    if not api_key:
        logger.error("Failed to obtain API key - WebSocket connection will fail")
        return

    # Build WebSocket URL (convert https to wss, http to ws)
    base_url = settings.subnet_core_url
    if base_url.startswith("https://"):
        ws_url = base_url.replace("https://", "wss://")
    elif base_url.startswith("http://"):
        ws_url = base_url.replace("http://", "ws://")
    else:
        ws_url = f"wss://{base_url}"

    ws_endpoint = f"{ws_url}/ws/orchestrators/{hotkey}"

    heartbeat_interval = 60  # seconds
    retry_count = 0

    while True:
        try:
            # Generate auth headers
            timestamp = str(int(time.time()))
            auth_message = f"{hotkey}:{timestamp}"
            signature = _sign_message(wallet, auth_message)

            headers = {
                "x-signature": signature,
                "x-timestamp": timestamp,
            }
            if api_key:
                headers["x-api-key"] = api_key

            logger.info(f"Connecting to BeamCore WebSocket: {ws_endpoint}")

            async with websockets.connect(
                ws_endpoint,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                _core_api_ws = ws
                logger.info("WebSocket connected to BeamCore")

                # Wait for connected message
                try:
                    connected_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    connected_data = json.loads(connected_msg)
                    logger.info(
                        f"{BEAMCORE_REGISTER_LOG} first_frame type={connected_data.get('type')!r} "
                        f"payload={connected_data}"
                    )
                    if connected_data.get("type") == "connected":
                        logger.info(
                            f"{BEAMCORE_REGISTER_LOG} connected: buffer_id={connected_data.get('buffer_id')!r}"
                        )
                except asyncio.TimeoutError:
                    logger.warning(f"{BEAMCORE_REGISTER_LOG} timeout waiting for first server frame after connect")
                except json.JSONDecodeError as e:
                    logger.warning(f"{BEAMCORE_REGISTER_LOG} invalid JSON in first frame: {e}")

                # Send registration message
                local_ip = settings.external_ip or _get_local_ip()
                orch_url = f"http://{local_ip}:{settings.api_port}"

                # Sign registration data: "{hotkey}:{url}:{region}"
                reg_message = f"{hotkey}:{orch_url}:{settings.region}"
                reg_signature = _sign_message(wallet, reg_message)

                # Get UID: prefer env var, fallback to metagraph detection
                uid = settings.uid  # From ORCHESTRATOR_UID env var
                if uid is None and get_uid is not None:
                    uid = get_uid()  # From metagraph detection

                register_msg = {
                    "type": "register",
                    "url": orch_url,
                    "region": settings.region,
                    "max_workers": settings.max_workers,
                    "uid": uid,
                    "fee_percentage": settings.fee_percentage,
                    "signature": reg_signature,
                }

                await ws.send(json.dumps(register_msg))
                logger.info(
                    f"{BEAMCORE_REGISTER_LOG} sent_register: region={settings.region} "
                    f"fee={settings.fee_percentage}% uid={uid} url={orch_url} "
                    f"(signature omitted)"
                )

                # BeamCore may send register_ack first and register_result (with slot_number) second — read until done
                loop_time = asyncio.get_event_loop().time
                reg_deadline = loop_time() + 25.0
                reg_terminal = False
                saw_register_ack = False
                saw_register_result = False
                reg_inbound_count = 0
                while loop_time() < reg_deadline and not reg_terminal:
                    try:
                        remaining = max(0.1, reg_deadline - loop_time())
                        reg_response = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10.0))
                    except asyncio.TimeoutError:
                        continue
                    reg_inbound_count += 1
                    try:
                        reg_data = json.loads(reg_response)
                    except json.JSONDecodeError as e:
                        raw = reg_response[:500] if isinstance(reg_response, str) else repr(reg_response)[:500]
                        logger.warning(
                            f"{BEAMCORE_REGISTER_LOG} non_json_frame n={reg_inbound_count} err={e} raw_prefix={raw!r}"
                        )
                        continue
                    msg_type = reg_data.get("type")
                    logger.info(
                        f"{BEAMCORE_REGISTER_LOG} inbound_ws n={reg_inbound_count} type={msg_type!r} "
                        f"payload={reg_data}"
                    )
                    if msg_type == "register_ack":
                        saw_register_ack = True
                        logger.info(
                            f"{BEAMCORE_REGISTER_LOG} register_ack status={reg_data.get('status')!r}"
                        )
                    elif msg_type == "register_error":
                        logger.error(
                            f"{BEAMCORE_REGISTER_LOG} register_error: {reg_data.get('error')} full={reg_data}"
                        )
                        reg_terminal = True
                    elif msg_type == "register_result":
                        saw_register_result = True
                        status = reg_data.get("status")
                        slot = reg_data.get("slot_number")
                        logger.info(
                            f"{BEAMCORE_REGISTER_LOG} register_result: status={status!r} "
                            f"slot_number={slot!r} full={reg_data}"
                        )
                        reg_terminal = True
                    else:
                        logger.info(
                            f"{BEAMCORE_REGISTER_LOG} inbound_non_terminal (still waiting for "
                            f"register_result/register_error): type={msg_type!r}"
                        )
                logger.info(
                    f"{BEAMCORE_REGISTER_LOG} registration_phase_summary: "
                    f"frames_seen={reg_inbound_count} "
                    f"register_ack={saw_register_ack} "
                    f"register_result={saw_register_result} "
                    f"terminal={reg_terminal}"
                )
                if not reg_terminal:
                    if saw_register_ack:
                        logger.info(
                            f"{BEAMCORE_REGISTER_LOG} no register_result within 25s after register_ack — "
                            f"BeamCore may omit slot_number; watch for a later {BEAMCORE_REGISTER_LOG} register_result line"
                        )
                    else:
                        logger.warning(
                            f"{BEAMCORE_REGISTER_LOG} no register_ack/register_result/error in registration window — "
                            f"check auth, on-chain registration, EXTERNAL_IP, and orchestrator URL reachability"
                        )

                # Connected successfully — reset retry counter
                retry_count = 0

                # Heartbeat loop
                loop = asyncio.get_event_loop()
                last_heartbeat = time.time()

                while True:
                    try:
                        # Check if it's time to send heartbeat
                        now = time.time()
                        if now - last_heartbeat >= heartbeat_interval:
                            # Gather stats
                            worker_count = get_worker_count() if callable(get_worker_count) else 0
                            balance_tao = -1.0
                            coldkey_balance_tao = -1.0
                            pending_payments = 0

                            if callable(get_balance_info):
                                try:
                                    balance_tao, coldkey_balance_tao, pending_payments = await asyncio.wait_for(
                                        loop.run_in_executor(None, get_balance_info),
                                        timeout=15.0,
                                    )
                                except asyncio.TimeoutError:
                                    logger.warning("Balance fetch timed out")
                                except Exception as e:
                                    logger.debug(f"Balance fetch failed: {e}")

                            heartbeat_msg = {
                                "type": "heartbeat",
                                "worker_count": worker_count,
                                "avg_bandwidth_mbps": 0.0,
                                "total_bytes_relayed": 0,
                                "fee_percentage": settings.fee_percentage,
                                "balance_tao": balance_tao,
                                "coldkey_balance_tao": coldkey_balance_tao,
                                "pending_payments": pending_payments,
                            }

                            await ws.send(json.dumps(heartbeat_msg))
                            last_heartbeat = now
                            logger.debug(f"Heartbeat sent: workers={worker_count}")

                        # Receive messages (non-blocking with timeout)
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            data = json.loads(msg)
                            msg_type = data.get("type")

                            if msg_type == "heartbeat_ack":
                                pass  # Expected
                            elif msg_type == "transfer_assigned":
                                asyncio.create_task(_handle_transfer_assigned(ws, data))
                            elif msg_type == "worker_list":
                                tid = data.get("transfer_id")
                                fut = _worker_list_futures.pop(tid, None)
                                if fut and not fut.done():
                                    fut.set_result(data.get("workers", []))
                            elif msg_type == "chunks_queued":
                                logger.info(f"Chunks queued: assignment={data.get('assignment_id')} count={data.get('task_count')}")
                            elif msg_type == "register_result":
                                logger.info(
                                    f"{BEAMCORE_REGISTER_LOG} register_result (post_handshake): "
                                    f"status={data.get('status')!r} slot_number={data.get('slot_number')!r} "
                                    f"full={data}"
                                )
                            elif msg_type == "error":
                                logger.warning(f"BeamCore error: {data.get('message')}")
                            else:
                                logger.debug(f"Received message: {msg_type}")

                        except asyncio.TimeoutError:
                            pass  # No message, continue heartbeat loop

                    except ConnectionClosed as e:
                        logger.warning(f"WebSocket connection closed: {e}")
                        break

        except WebSocketException as e:
            logger.warning(f"WebSocket error: {e}")
        except Exception as e:
            logger.error(f"BeamCore WebSocket connection failed: {e}")

        _core_api_ws = None
        reconnect_delay = min(5 * (1.5 ** retry_count) + random.uniform(0, 2), 60)
        retry_count += 1
        logger.info(f"Reconnecting to BeamCore in {reconnect_delay:.1f}s (attempt {retry_count})...")
        await asyncio.sleep(reconnect_delay)


# Legacy HTTP functions (kept for fallback, but deprecated)
_core_api_heartbeat_task: Optional[asyncio.Task] = None


async def _register_with_core_api(settings, hotkey: str, uid: int = None, api_key: str = None) -> bool:
    """Register this orchestrator with BeamCore to get a slot."""
    url = f"{settings.subnet_core_url}/orchestrators/register"
    local_ip = settings.external_ip or _get_local_ip()
    orch_url = f"http://{local_ip}:{settings.api_port}"

    payload = {
        "hotkey": hotkey,
        "url": orch_url,
        "ip": local_ip,
        "port": settings.api_port,
        "region": settings.region,
        "max_workers": settings.max_workers,
        "uid": uid,
        "fee_percentage": settings.fee_percentage,
        "signature": "local",
    }

    headers = {
        "X-Hotkey": hotkey,  # Required for rate limiting
    }
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                logging.getLogger(__name__).info(f"Registered with BeamCore: {data}")
                return True
            else:
                logging.getLogger(__name__).warning(
                    f"Registration failed: {resp.status_code} - {resp.text[:200]}"
                )
                return False
    except Exception as e:
        logging.getLogger(__name__).warning(f"Registration error: {e}")
        return False


# Configure logging - both console and file
LOG_DIR = os.environ.get("LOG_DIR", "/tmp/beam_logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
log_datefmt = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    datefmt=log_datefmt,
)

# Add file handler for log viewer
file_handler = logging.FileHandler(f"{LOG_DIR}/orchestrator.log")
file_handler.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger(__name__)

# Global instances
orchestrator: Orchestrator = None
# Cluster mode removed - standalone only
# cluster_coordinator: Optional[ClusterCoordinator] = None
# cluster_state: Optional[ClusterState] = None


def _get_local_ip() -> str:
    """Get the local IP address for cluster communication."""
    try:
        # Create a socket to determine the outbound IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _parse_cluster_uids(uids_str: str) -> list:
    """Parse comma-separated UID string into list of integers."""
    if not uids_str:
        return []
    return [int(uid.strip()) for uid in uids_str.split(",") if uid.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global orchestrator

    settings = get_settings()

    # Configure logging level
    logging.getLogger().setLevel(settings.log_level)

    # Initialize rate limiter
    rate_limiter = get_rate_limiter()
    await rate_limiter.start_cleanup()

    # Rate limit configs for legacy endpoints removed
    # All worker/transfer coordination now handled by BeamCore

    # Initialize metrics collector
    metrics_collector = get_metrics_collector()

    # Initialize orchestrator
    orchestrator = get_orchestrator()
    await orchestrator.initialize()

    # ==========================================================================
    # Initialize Subnet Schema (shared beam tables)
    # ==========================================================================
    if settings.database_url:
        try:
            from beam.db.base import init_db as init_subnet_db
            logger.info("Initializing subnet database schema...")
            await init_subnet_db(settings.database_url)
            logger.info("Subnet database schema initialized")
        except ImportError:
            logger.warning("beam.db not available, skipping subnet schema")
        except Exception as e:
            logger.warning(f"Failed to initialize subnet schema: {e}")

    # Client authentication removed - auth handled by BeamCore

    # Link metrics collector to orchestrator
    metrics_collector.set_orchestrator(orchestrator)
    await metrics_collector.start()

    # Start orchestrator background tasks
    await orchestrator.start()

    # Cluster mode removed - standalone only
    # Destination handlers removed - handled by BeamCore
    # Gateway registry removed - handled by BeamCore

    logger.info("=" * 60)
    logger.info("BEAM Orchestrator started")
    logger.info("=" * 60)
    logger.info(f"Hotkey: {orchestrator.hotkey}")
    logger.info(f"Network: {settings.subtensor_network}")
    logger.info(f"Subnet: {settings.netuid}")
    logger.info(f"API: http://{settings.api_host}:{settings.api_port}")
    logger.info("=" * 60)

    # ======================================================================
    # Connect to BeamCore via WebSocket (registration + heartbeat)
    # ======================================================================
    global _core_api_ws_task

    def _get_worker_count():
        try:
            return len(orchestrator.workers) if hasattr(orchestrator, 'workers') else 0
        except Exception:
            return 0

    def _get_balance_info():
        balance = -1.0
        coldkey_balance = -1.0
        pending = 0
        if orchestrator.subtensor and orchestrator.wallet:
            try:
                bal = orchestrator.subtensor.get_balance(orchestrator.wallet.hotkey.ss58_address)
                balance = float(bal)
            except Exception as e:
                logger.debug(f"Failed to fetch hotkey balance: {e}")
            try:
                ck_bal = orchestrator.subtensor.get_balance(orchestrator.wallet.coldkeypub.ss58_address)
                coldkey_balance = float(ck_bal)
            except Exception as e:
                logger.debug(f"Failed to fetch coldkey balance: {e}")
        try:
            if hasattr(orchestrator, '_reward_mgr'):
                pending = len(orchestrator._reward_mgr._payment_retry_queue)
        except Exception:
            pass
        return balance, coldkey_balance, pending

    def _get_uid():
        """Get UID from metagraph detection."""
        return orchestrator.our_uid

    # Register with BeamCore to get a slot
    api_key = None
    if orchestrator.subnet_core_client:
        api_key = orchestrator.subnet_core_client._api_key
        logger.info(f"Got API key for registration: {api_key[:20] if api_key else 'None'}...")
    else:
        logger.warning("No subnet_core_client available for registration")
    registered = await _register_with_core_api(settings, orchestrator.hotkey, orchestrator.our_uid, api_key)
    if registered:
        logger.info("Successfully registered with BeamCore - slot assigned")
    else:
        logger.warning("Failed to register with BeamCore - may not have a slot")

    # NOTE: WebSocket connection is handled by SubnetCoreClient
    logger.info("WebSocket connection handled by SubnetCoreClient")

    # Signal readiness to receive transfers (controlled by READY env var / config)
    if settings.ready and orchestrator.subnet_core_client:
        try:
            await orchestrator.subnet_core_client.set_ready(True)
            logger.info("Signalled ready=True to BeamCore — orchestrator will receive transfers")
        except Exception as e:
            logger.warning(f"Failed to set ready=True on BeamCore: {e}")
    else:
        logger.info("ready=False (default) — orchestrator will NOT receive transfers until READY=true is set")

    yield

    # Cleanup
    logger.info("Shutting down BEAM Orchestrator...")

    # Signal not-ready before stopping so BeamCore stops routing traffic immediately
    if orchestrator.subnet_core_client:
        try:
            await orchestrator.subnet_core_client.set_ready(False)
            logger.info("Signalled ready=False to BeamCore — orchestrator removed from routing")
        except Exception as e:
            logger.warning(f"Failed to set ready=False on BeamCore during shutdown: {e}")

    await orchestrator.stop()
    await metrics_collector.stop()
    await rate_limiter.stop_cleanup()

    logger.info("BEAM Orchestrator stopped")


# Create FastAPI app
app = FastAPI(
    title="BEAM Orchestrator",
    description="""
BEAM Orchestrator - Decentralized bandwidth mining coordinator.

The Orchestrator connects to BeamCore and:
- Registers with BeamCore on startup
- Sends periodic heartbeats with status updates
- Receives transfer assignments from BeamCore
- Manages local worker pools and task distribution
- Submits proof-of-bandwidth to BeamCore

All worker registration, transfer coordination, and validator communication
is handled centrally by BeamCore.

## Endpoints

### Health
Monitor the Orchestrator's health and view metrics.

### Orchestrators
Registration and heartbeat endpoints for BeamCore communication.
    """,
    version="0.1.0",
    lifespan=lifespan,
)

# Add middleware (order matters - first added = last to process request)
app.add_middleware(MetricsMiddleware, metrics_collector=get_metrics_collector())
app.add_middleware(RateLimitMiddleware, rate_limiter=get_rate_limiter())

# Add CORS middleware if configured
_cors_settings = get_settings()
_cors_origins = _cors_settings.get_cors_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=_cors_settings.cors_allow_credentials,
        allow_methods=_cors_settings.get_cors_methods(),
        allow_headers=_cors_settings.get_cors_headers(),
    )
    logger.info(f"CORS enabled for origins: {_cors_origins}")

# Mount route modules
app.include_router(health.router)
app.include_router(orchestrators.router)


# =============================================================================
# Additional API Routes
# =============================================================================

@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "service": "BEAM Orchestrator",
        "version": "0.1.0",
        "description": "Central coordinator for decentralized bandwidth mining",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/state")
async def get_state():
    """Get full Orchestrator state."""
    if orchestrator:
        return orchestrator.get_state()
    return {"error": "Orchestrator not initialized"}


@app.get("/workers/stats")
async def get_worker_stats():
    """Get detailed worker statistics."""
    if orchestrator:
        return orchestrator.get_worker_stats()
    return {"error": "Orchestrator not initialized"}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    from fastapi.responses import Response

    content, content_type = get_metrics_response()
    return Response(content=content, media_type=content_type)


@app.get("/metrics/json")
async def metrics_json():
    """JSON metrics endpoint for non-Prometheus consumers."""
    metrics_collector = get_metrics_collector()
    rate_limiter = get_rate_limiter()

    return {
        "uptime_seconds": time.time() - metrics_collector._start_time,
        "orchestrator": orchestrator.get_state() if orchestrator else {},
        "rate_limiter": rate_limiter.get_stats(),
    }


@app.get("/weights/estimate")
async def weight_estimate():
    """Estimate current weight formula components for emission monitoring."""
    if orchestrator:
        return orchestrator.get_weight_estimate()
    return {"error": "Orchestrator not initialized"}


# Cluster endpoints removed - standalone mode only


# =============================================================================
# Main
# =============================================================================

def main():
    """Main entry point."""
    settings = get_settings()

    # Handle signals
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Print banner
    cluster_mode = "STANDALONE"  # Cluster mode removed
    print(f"""
╔═══════════════════════════════════════════════════╗
║                                                   ║
║        ██████╗ ███████╗ █████╗ ███╗   ███╗        ║
║        ██╔══██╗██╔════╝██╔══██╗████╗ ████║        ║
║        ██████╔╝█████╗  ███████║██╔████╔██║        ║
║        ██╔══██╗██╔══╝  ██╔══██║██║╚██╔╝██║        ║
║        ██████╔╝███████╗██║  ██║██║ ╚═╝ ██║        ║
║        ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝        ║
║                                                   ║
║                   ORCHESTRATOR                    ║
║    Decentralized Bandwidth Mining Coordinator     ║
║                                                   ║
║                 Mode: {cluster_mode:^12}          ║
║                                                   ║
╚═══════════════════════════════════════════════════╝
    """)

    # Auto-open log viewer in browser (disabled by default, set OPEN_LOG_VIEWER=true to enable)
    if os.environ.get("OPEN_LOG_VIEWER", "").lower() in ("true", "1", "yes"):
        import webbrowser
        import threading
        log_viewer_url = os.environ.get("LOG_VIEWER_URL", "http://localhost:8080/logs/")
        def open_logs():
            time.sleep(1.5)  # Wait for server to start
            webbrowser.open(log_viewer_url)
        threading.Thread(target=open_logs, daemon=True).start()

    # Run server
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
