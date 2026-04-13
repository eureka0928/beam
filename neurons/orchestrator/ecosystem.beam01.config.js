module.exports = {
  apps: [{
    name: 'beam-miner-01',
    script: '/root/beam/neurons/orchestrator/main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      REGISTRY_URL: 'https://beamcore.b1m.ai',
      SUBTENSOR_NETWORK: 'finney',
      NETUID: '105',
      WALLET_NAME: 'beam-01',
      WALLET_HOTKEY: 'miner-01',
      BT_WALLET_PASSWORD: 'beam-01',
      ORCHESTRATOR_HOST: '0.0.0.0',
      API_PORT: '8013',
      LOG_LEVEL: 'INFO',
      EXTERNAL_IP: '116.202.53.114',
      REGION: 'US',
      LOCAL_MODE: 'false',
      FEE_PERCENTAGE: '0.0',
      MAX_WORKERS: '10000',
      WORKER_TIMEOUT: '300',
      MIN_WORKER_BANDWIDTH: '10.0',
      WORKER_HEARTBEAT_INTERVAL: '60',
      WORKER_SYNC_INTERVAL: '60',
      MAX_CONCURRENT_TASKS: '1000',
      TASK_TIMEOUT: '120',
      ALPHA_PER_CHUNK: '0.5',
      READY: 'true',
      BEAMCORE_API_KEY: 'b1m_7463add7fa36d7ed61f02693796de9dd06af5517209ad9e9',
    }
  }]
};
