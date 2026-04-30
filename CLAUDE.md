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

**SLA affiliation (preferred, no worker restart needed):**
```
POST /sla/orchestrators/{uid}/workers/join
Body: { worker_id, hotkey, ip, port, signature }
Signature: worker signs "{worker_id}:{orchestrator_hotkey}" with worker's hotkey
```

## Payment Pipeline (Critical)

Payment must end with `⛓ confirmed` (`pop_verified=true`) to build compliance. The verified flow:

```
1. Worker completes task → task_result received via WS
2. GET /orchestrators/tasks/{id}/payment-address → get worker_id + amount_owed
3. GET /orchestrators/workers/{worker_id}/coldkey → get ACTUAL coldkey
4. GET /pob/{task_id}/validate-transfer → get transfer_id
5. Publish PoB WITH worker_coldkey field (critical — without it verification fails)
6. On-chain: batch_all(
     remark_with_event("{transfer_id}:{task_id}"),
     SubtensorModule.transfer_stake(destination_coldkey=worker_coldkey, alpha_amount=500_000_000)
   )
7. POST /pob/{task_id}/payment { tx_hash, amount_rao }
8. BeamCore verifies: coldkey in PoB matches on-chain dest → ⛓ confirmed
```

**Critical gotchas:**
- `payment-address` API returns a DERIVED address — do NOT use for `transfer_stake` destination (causes `Recipient mismatch`)
- PoBSubmission MUST include `worker_coldkey` or verifier returns "Missing worker_coldkey"
- `transfer_stake` minimum is ~0.5 ALPHA (500M RAO)
- Memo MUST be exactly `{transfer_id}:{task_id}` — wrong memo = validator compliance penalty
- `pop_verified: null` means async pending (not failure); `false` means wrong destination
- Worker coldkey balance (TAO) needed for tx fees, not just staked ALPHA

**Compliance formula:** `traffic_compliance = paid_verified_tasks / total_assigned_tasks`. Zero compliance → zero `combined_weight` → no traffic → stuck. Need verified payments to break out.

## Worker Assignment Strategy

`_assign_chunks_to_workers` in `orchestrator.py`:
1. Separate workers into `affiliated` (is_affiliated=True) vs `global_pool`
2. Sort each group by score: 60% success_rate + 25% bandwidth + 15% trust
3. Build list: all affiliated first, then top-N global pool workers
4. Round-robin chunk assignment across selected workers
5. Only assign to workers with heartbeat < 60s ago

Workers must **accept tasks from ALL orchestrators** (not just affiliated ones) to avoid "stuck tasks" disconnections from BeamCore. `BEAM_ORCHESTRATOR_HOTKEYS` only controls affiliation, not task filtering.

## Deployment Notes

**PM2 ecosystem configs**: `neurons/orchestrator/ecosystem.*.config.js` per orchestrator, `neurons/worker/ecosystem.*.config.js` for workers. Each config sets `BT_WALLET_PASSWORD`, `DATABASE_URL`, `ALPHA_PER_CHUNK=0.5`, `READY=true`.

**Coldkey password handling**: If `BT_WALLET_PASSWORD` env var doesn't decrypt the coldkey (encryption encoding mismatch), re-encrypt using:
```python
import bittensor_wallet
kp = bittensor_wallet.Keypair.create_from_mnemonic(mnemonic)
kf = bittensor_wallet.Keyfile(path="/root/.bittensor/wallets/{name}/coldkey")
kf.set_keypair(kp, encrypt=True, overwrite=True, password="{name}")
```

**Worker registration cap**: BeamCore caps at 250 active workers globally. Use `BEAM_WORKER_STAGGER` env to jitter registration and avoid rate-limit storms.
