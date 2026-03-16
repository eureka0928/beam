# BEAM

An open coordination layer for bandwidth and machine-to-machine data transfer.

## Why BEAM Exists

**The modern internet has developed large-scale markets for compute and storage, but not for bandwidth.**

Cloud platforms provide on-demand compute. Storage networks allow data to be stored and retrieved globally. However, the movement of data — bandwidth — remains controlled by centralized providers: cloud platforms, CDNs, and telecom operators.

As a result:

- Bandwidth pricing is opaque and expensive
- Unused global network capacity cannot participate in data delivery
- Routing decisions are controlled by centralized infrastructure

Despite the enormous scale of global networking infrastructure, there is no open coordination layer for bandwidth.

**BEAM is designed to address this gap.**

## The Missing Internet Layer

Compute has markets.
Storage has markets.
Bandwidth markets exist, but they are largely closed, centralized, and inaccessible to independent participants.

BEAM introduces an open coordination layer where bandwidth can be contributed, measured, and rewarded based on performance.

## What BEAM Does

BEAM aggregates distributed bandwidth and coordinates data transfers across a network of participants.

- **Nodes contribute bandwidth** and participate in executing transfers
- **Proof-of-Bandwidth** measures and validates bandwidth performance during transfers, allowing validators to assess delivery quality
- **Rewards are based on measurable performance**, not promises

## Transfer Patterns

BEAM supports flexible source-to-destination configurations:

| Pattern             | Description                                              |
| ------------------- | -------------------------------------------------------- |
| **Single → Single** | One source to one destination                            |
| **Single → Multi**  | One source replicated to multiple destinations (fan-out) |
| **Multi → Single**  | Multiple sources aggregated to one destination           |
| **Multi → Multi**   | Multiple sources to multiple destinations (mesh)         |

```bash
# Single to single
beam transfer gcs://src/data.bin s3://dst/data.bin

# Single to multi (fan-out)
beam transfer gcs://src/data.bin \
  -d s3://dst-1/data.bin \
  -d r2://dst-2/data.bin

# Multi to single (aggregation)
beam transfer gcs://src-1/part1.bin gcs://src-2/part2.bin \
  -d s3://dst/combined.bin

# Multi to multi (mesh)
beam transfer gcs://src-1/data.bin gcs://src-2/data.bin \
  -d s3://dst-1/data.bin \
  -d r2://dst-2/data.bin
```

Each transfer is chunked and distributed across workers in parallel.

## Capabilities

### Universal Storage Connectivity

Connect any storage provider to any destination — S3, GCS, Azure Blob, R2, IPFS, HTTP endpoints, and more.

```bash
beam transfer gcs://source-bucket/data.bin s3://dest-bucket/data.bin
```

### Multi-Destination Fan-Out

Replicate data to multiple destinations in a single transfer.

```bash
beam transfer s3://source/model.tar \
  -d gcs://replica-us/model.tar \
  -d azure://replica-eu/model.tar \
  -d r2://replica-asia/model.tar
```

### Parallel Transfers

Large files are split into chunks and transferred in parallel across the network, maximizing throughput.

### Webhook Callbacks

Get notified when transfers complete.

```bash
beam transfer s3://source/file.bin gcs://dest/file.bin \
  --callback https://your-service.com/webhook
```

### MCP Integration

BEAM supports the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), allowing AI agents to invoke BEAM as a tool for data movement.

```json
{
  "tool": "beam_transfer",
  "source": "gcs://dataset/training-data.tar",
  "destinations": ["s3://gpu-cluster-1/data.tar", "r2://gpu-cluster-2/data.tar"]
}
```

## Built for the Machine Internet

Machine-to-machine communication is rapidly increasing:

- **AI-to-AI** — Autonomous agents exchanging data with AI services
- **Agentic workflows** — AI systems coordinating tasks and sharing outputs
- **AI training pipelines** — Datasets moving to compute, models distributing to inference
- **Data center synchronization** — Replication across clusters and regions
- **AI-to-IoT** — Sensor data flowing to ML systems

These workflows involve terabytes or petabytes moving across regions and independent systems. BEAM provides a decentralized coordination layer for this data movement.

## How It Works

```
┌──────────┐                                                    ┌─────────────┐
│  Client  │ ── transfer ──▶  ┌──────────────┐  ── tasks ──▶    │   Workers   │
└──────────┘                  │   BEAMCORE   │                  └──────┬──────┘
                              │ (Coordinator)│                         │
┌──────────────┐              └──────┬───────┘                         │ delivery
│ Orchestrators│ ◀── assignments ────┘                                 ▼
│   (Miners)   │ ── proofs ──────────▶                          ┌─────────────┐
└──────────────┘                                                │ Destinations│
                              ┌──────────────┐                  └─────────────┘
                              │  Validators  │ ◀── read proofs ──┘
                              └──────────────┘
```

1. Clients create transfers via BeamCore API
2. BeamCore assigns chunks to orchestrators by stake-weighted allocation
3. Orchestrators poll BeamCore for assignments and create tasks
4. BeamCore pushes tasks to workers via WebSocket
5. Workers fetch data from source and deliver to destinations
6. Workers submit Proof-of-Bandwidth to BeamCore
7. Validators read proofs from BeamCore and set weights on chain

> **Note:** BeamCore is the central coordination layer. Data flows directly between sources, workers, and destinations.

## The Vision

BEAM's goal is to become the open bandwidth layer for the machine internet.

By coordinating distributed bandwidth and measuring performance through Proof-of-Bandwidth, BEAM enables data to move efficiently across a network that is:

- **Open to participation** — Contributors join via the Bittensor network
- **Performance-based** — Rewards tied to measured delivery metrics
- **Decentralized** — No single point of control

## Get Started

### Use BEAM (SDK)

```bash
pip install beam-sdk
```

```python
from beam_sdk import Beam

beam = Beam(api_key="...")
transfer = await beam.transfers.create(
    sources=[{"type": "gcs", "bucket": "src", "key": "data.bin", ...}],
    destinations=[
        {"type": "s3", "bucket": "dst-1", "key": "data.bin", ...},
        {"type": "r2", "bucket": "dst-2", "key": "data.bin", ...},
    ],
)
```

### Run a Node

- [Orchestrator Guide](docs/orchestrator.md)
- [Validator Guide](docs/validator.md)

## Links

- [Documentation](docs/)
- [Beam SDK](https://github.com/Beam-Network/beam-sdk)
- [Bittensor](https://bittensor.com)

## License

MIT License — see [LICENSE](LICENSE)
