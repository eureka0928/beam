module.exports = {
  apps: [{
    name: 'beam-miner-04',
    script: '/root/beam/neurons/orchestrator/main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      REGISTRY_URL: 'https://beamcore.b1m.ai',
      SUBTENSOR_NETWORK: 'finney',
      NETUID: '105',
      WALLET_NAME: 'beam-04',
      WALLET_HOTKEY: 'miner-04',
      BT_WALLET_PASSWORD: 'beam-04',
      ORCHESTRATOR_HOST: '0.0.0.0',
      API_PORT: '8102',
      LOG_LEVEL: 'INFO',
      EXTERNAL_IP: '116.202.53.114',
      REGION: 'US',
      LOCAL_MODE: 'false',
      FEE_PERCENTAGE: '0.0',
      MAX_WORKERS: '10000',
      WORKER_TIMEOUT: '300',
      MIN_WORKER_BANDWIDTH: '10.0',
      WORKER_HEARTBEAT_INTERVAL: '30',
      WORKER_SYNC_INTERVAL: '30',
      MAX_CONCURRENT_TASKS: '5000',
      TASK_TIMEOUT: '180',
      ALPHA_PER_CHUNK: '0.5',
      READY: 'true',
      DATABASE_URL: 'postgresql+asyncpg://beam:beam123@localhost:5432/beam_orchestrator',
      BEAMCORE_API_KEY: 'b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1',
      // Now in top-tier — every chunk counts. Stricter than default (10/0.5).
      BEAM_SCORER_MIN_DR_SAMPLE: '5',
      BEAM_SCORER_MIN_DR: '0.55',
      // Cold-start hoarding cutoff: 3 pending tasks with 0 completed → exclude.
      BEAM_SCORER_HOARDING_PENDING: '3',
      // Stronger preference for workers we've seen successfully deliver.
      BEAM_PROVEN_MULT: '5.0',
    }
  }]
};
