module.exports = {
  apps: [{
    name: 'beam-lord',
    script: '/root/beam/neurons/orchestrator/main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      REGISTRY_URL: 'https://beamcore.b1m.ai',
      SUBTENSOR_NETWORK: 'finney',
      NETUID: '105',
      WALLET_NAME: 'lord',
      WALLET_HOTKEY: '8100',
      BT_WALLET_PASSWORD: 'wkdrjwjd409',
      ORCHESTRATOR_HOST: '0.0.0.0',
      API_PORT: '8051',
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
      BEAMCORE_API_KEY: 'b1m_27e915c94fb5d9a96f5c667359848aa8e377cdbc238e39f1',
    }
  }]
};
