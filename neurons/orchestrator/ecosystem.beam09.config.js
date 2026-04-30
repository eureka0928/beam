module.exports = {
  apps: [{
    name: 'beam-miner-09',
    script: '/root/beam/neurons/orchestrator/main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      REGISTRY_URL: 'https://beamcore.b1m.ai',
      SUBTENSOR_NETWORK: 'finney',
      NETUID: '105',
      WALLET_NAME: 'beam-09',
      WALLET_HOTKEY: 'miner-09',
      BT_WALLET_PASSWORD: 'beam-09',
      ORCHESTRATOR_HOST: '0.0.0.0',
      API_PORT: '8226',
      LOG_LEVEL: 'INFO',
      EXTERNAL_IP: '116.202.53.114',
      REGION: 'US',
      LOCAL_MODE: 'false',
      FEE_PERCENTAGE: '0.0',
      MAX_WORKERS: '10000',
      WORKER_TIMEOUT: '300',
      MIN_WORKER_BANDWIDTH: '10.0',
      WORKER_HEARTBEAT_INTERVAL: '60',
      WORKER_SYNC_INTERVAL: '30',
      MAX_CONCURRENT_TASKS: '1000',
      TASK_TIMEOUT: '120',
      ALPHA_PER_CHUNK: '0.5',
      READY: 'true',
      DATABASE_URL: 'postgresql+asyncpg://beam:beam123@localhost:5432/beam_orchestrator',
      BEAMCORE_API_KEY: 'b1m_056e5f94fbf9a4e8769f49c3bbf778b9e4a35971a9b03e6e',
      // Spearhead UID — extra-strict worker scorer. Block any worker with 3+ tasks AND dr<0.6.
      BEAM_SCORER_MIN_DR_SAMPLE: '3',
      BEAM_SCORER_MIN_DR: '0.6',
    }
  }]
};
