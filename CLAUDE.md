# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BEAM is a decentralized bandwidth coordination network built on Bittensor. It aggregates distributed bandwidth and coordinates data transfers via Proof-of-Bandwidth. This repo contains the **orchestrator** (miner) and **validator** node implementations. The central coordinator (**BeamCore**) is a separate private service not in this repo.

## Common Commands

```bash
# Install
pip install -e ".[dev]"              # dev dependencies (pytest, black, ruff, mypy)
pip install -e ".[validator]"        # validator extras (torch, networkx)

# Lint & Format
black --check .                      # check formatting (line-length=100)
black .                              # auto-format
ruff check .                         # lint

# Test
pytest tests/ -v --tb=short          # run all tests (asyncio_mode=auto)
pytest tests/test_foo.py -v          # run a single test file
pytest tests/test_foo.py::test_bar   # run a single test

# Type check
mypy neurons/ --ignore-missing-imports

# Run nodes
cd neurons/orchestrator && python -m neurons.orchestrator.main
cd neurons/validator && python main.py
```

## Architecture

### Data Flow

```
Client -> BeamCore -> Orchestrator -> Workers -> Destinations
                         |                |
                   (heartbeat/WS)    (PoB proofs)
                         |                |
                     BeamCore <-----------+
                         |
                     Validator (reads proofs, sets weights on Bittensor)
```

### Orchestrator (`neurons/orchestrator/`)

Miners that coordinate bandwidth work. Entry point: `main.py` (run with `python -m neurons.orchestrator.main`).

- **`core/orchestrator.py`** - Central facade wiring all managers together
- **`core/worker_manager.py`** - Worker registration, health checks, scoring
- **`core/task_scheduler.py`** - Assigns bandwidth tasks to workers
- **`core/proof_aggregator.py`** - Collects/aggregates Proof-of-Bandwidth
- **`core/epoch_manager.py`** - Epoch lifecycle, payment proof generation (Merkle trees)
- **`core/reward_manager.py`** - Distributes rewards to workers
- **`core/metagraph_sync.py`** - Syncs Bittensor metagraph, discovers validators
- **`clients/subnet_core_client.py`** - WebSocket + HTTP client for BeamCore API
- **`db/`** - SQLAlchemy async models (PostgreSQL/asyncpg), Merkle tree impl
- **`routes/`** - FastAPI health/status endpoints
- **`middleware/`** - Rate limiting, metrics

### Validator (`neurons/validator/`)

Verifies Proof-of-Bandwidth and sets weights on Bittensor. Entry point: `main.py` (run with `python main.py` from the validator dir).

- **`core/validator.py`** - Main validation loop (~20 min cycles): fetch proofs, verify, score, set weights
- **`core/config.py`** - Pydantic settings (env prefix: `BEAM_VALIDATOR_`)
- **`services/weight_calculator.py`** - Exposure-based weight formula: `raw_weight = exposure * quality * confidence * penalty`
- **`services/cross_verification.py`** - Peer verification of proofs across validators
- **`chain/fiber_chain.py`** - Bittensor metagraph integration
- **`chain/tx_verifier.py`** - On-chain transaction verification
- **`clients/subnet_core_client.py`** - BeamCore API client for validators

### Shared (`neurons/shared/`)

- **`merkle.py`** - Merkle tree implementation for batch payment verification

## Configuration

Layered YAML config in `config/`: `defaults.yaml` -> `testnet.yaml` / `mainnet.yaml` / `local.yaml`.

Environment variables override YAML values:
- Orchestrator: no prefix (e.g., `WALLET_NAME`, `DATABASE_URL`)
- Validator: `BEAM_VALIDATOR_` prefix (e.g., `BEAM_VALIDATOR_WALLET_NAME`)
- Shared: `BEAM_` prefix (e.g., `BEAM_NETUID`, `BEAM_SUBTENSOR_NETWORK`)

See `.env.example` for all available variables.

| Network | netuid | Subtensor | BeamCore |
|---------|--------|-----------|----------|
| Testnet | 304 | `test` | beamcore-dev.b1m.ai |
| Mainnet | 105 | `finney` | beamcore.b1m.ai |

## Key Technical Details

- **Python 3.10+**, fully async (asyncio, asyncpg, httpx, websockets)
- **FastAPI** for HTTP endpoints, **WebSocket** for BeamCore registration and real-time task delivery
- **SQLAlchemy 2.0 async** with PostgreSQL; schema namespace: `orchestrator`
- **Pydantic v2** for config and data validation
- **PyNaCl** for cryptographic signing of proofs
- **Black** line-length is 100, **Ruff** rules: E, F, I, W (E501 ignored)
- Validator imports beam library which reads UID config from env vars at import time - must call `_fetch_uid_config_sync()` before importing beam modules

## BeamCore API Reference

Dashboard: `https://beamcore.b1m.ai/dashboard/` (requires auth)
API Docs: `https://beamcore.b1m.ai/redoc` | `https://beamcore.b1m.ai/docs`
Auth: `X-Api-Key` header or hotkey signature (`X-Hotkey`, `X-Signature`, `X-Timestamp`, `X-Nonce`)

### Key Endpoints

#### Routing & Selection (how orchestrators get work)
- `GET /routing/orchestrators` — List registered orchestrators with status, weight
- `GET /routing/weight-distribution` — Per-orchestrator selection_probability
- `GET /routing/select?region=` — Select orchestrator for transfer (weighted random by `combined_weight = sla_score × worker_factor`)
- `POST /routing/plan` — Plan chunk distribution across orchestrators by weight
- `GET /routing/worker-capacity` — Total worker capacity (used to determine chunk count)

#### Orchestrator (our endpoints)
- `GET /orchestrators/pending-transfers` — Poll for transfer assignments (HTTP fallback)
- `POST /orchestrators/assignments` — Claim chunks for a transfer (race — 5s timeout)
- `POST /orchestrators/tasks` / `POST /orchestrators/tasks/batch` — Create tasks
- `POST /orchestrators/tasks/acknowledge` — Acknowledge completions, create PoB
- `POST /orchestrators/tasks/reassign` — Reassign stale task to new worker
- `GET /orchestrators/slots/status` — Slot system status (public)
- `GET /orchestrators/workers` — List workers in our pool

#### Worker Affiliation (CRITICAL)
- `POST /workers/affiliate` — Affiliate worker with orchestrator. Sign `"{worker_id}:{orchestrator_hotkey}"`
- `POST /workers/affiliations` — Same, alternate endpoint
- `POST /sla/orchestrators/{id}/workers/join` — Worker joins orchestrator with SLA commitment
- `DELETE /workers/{worker_id}/affiliate/{orchestrator_hotkey}` — Remove affiliation
- `GET /workers/{worker_id}/affiliations` — Check worker's affiliations

#### Worker
- `POST /workers/register` — Register worker (hotkey + IP + signature)
- `POST /workers/heartbeat` — Worker heartbeat
- `GET /workers/tasks/pending` — Poll for pending tasks
- `POST /workers/tasks/accept` — Accept a task
- `POST /workers/tasks/complete` — Report completion (with chunk_hash, bandwidth)

#### Proof of Bandwidth
- `GET /pob/epoch/{epoch}/summaries` — Epoch summaries (validators use this for weights)
- `GET /pob/latest-epoch` — Latest epoch with data
- `GET /pob/stats/{hotkey}` — Orchestrator PoB stats

#### Dashboard API (admin/monitoring)
- `GET /dashboard/api/traffic-allocation` — How BeamCore distributes traffic
- `GET /dashboard/api/orchestrators` — All orchestrators with status
- `GET /dashboard/api/orchestrators/{hotkey}/pool` — Orchestrator's worker pool
- `GET /dashboard/api/orchestrator-stats?hotkey=` — Orchestrator payment/activity stats
- `GET /dashboard/api/weights` — Current weight distribution with formula components
- `GET /dashboard/api/mass-affiliation` — Worker affiliation management

### Traffic Allocation Formula

BeamCore distributes transfers using:
```
combined_weight = sla_score × worker_factor
worker_factor = log2(active_workers + 1) + 1  (approx)
```

Key factors:
- `active_workers` — Workers with recent heartbeat affiliated with your orchestrator
- `sla_score` — Currently 0.5 for all orchestrators (baseline)
- `compliance_score` — 1.0 for compliant orchestrators
- Each orchestrator gets `traffic_pct = combined_weight / sum(all_weights) * 100`

### Validator Weight Formula

```
raw_weight = exposure × quality × confidence × penalty
```

- **Exposure** = 70% bytes_share + 30% proofs_share (relative to all orchestrators)
- **Quality** = 40% bandwidth + 25% compliance + 20% verification + 15% spot_check. Trust gate: <0.5 compliance or verification → 0.5x penalty
- **Confidence** = sqrt ramp: needs 10 proofs + 1GB bytes for full confidence
- **Penalty** = fraud multiplier (default 1.0)

Validators fetch `/pob/epoch/{epoch}/summaries` from BeamCore. Only orchestrators in summaries get weight. Zero proofs = zero confidence = zero weight.

### Worker-Orchestrator Relationship

Workers register with BeamCore globally. They must be **affiliated** with an orchestrator to count toward that orchestrator's `active_workers` and receive tasks from it. Without affiliation, workers are in a global pool and may be assigned to other orchestrators.

### Current Deployment (UID 115)

- Orchestrator: `beam` (pm2), hotkey `dream-001`, UID 115, slot 30
- Workers: `beam-w5` through `beam-w14` (pm2), hotkeys `dream-005` to `dream-014`
- Server: 95.217.67.193 (Hetzner Finland), registered as region US
- Known issue: BeamCore alpha stake cache shows 0.0 despite 31.60 on-chain
