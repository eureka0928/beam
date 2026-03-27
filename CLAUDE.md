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
