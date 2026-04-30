// 5 more reliable workers affiliated with UIDs 45, 69, 77, 189
const COMMON_ENV = {
  SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
  BEAM_WORKER_REGION: 'US',
  CONNECTION_MODE: 'auto',
  BT_WALLET_PASSWORD: 'beam-01',
  BEAM_WORKER_STAGGER: '5',
  BEAM_ORCHESTRATOR_HOTKEYS: [
    '5GZUs7uztXt1WPwL6KQhsVznrC1V3yoFtYLaLFEUVT9xdjLC',  // UID 69
    '5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3',  // UID 102
  ].join(','),
  BEAM_ORCHESTRATOR_API_KEY: 'b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1',
};

const COMMON = {
  interpreter: '/root/beam/.venv/bin/python',
  cwd: '/root/beam/neurons/worker',
};

module.exports = {
  apps: [
    {
      name: 'beam-w006',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-06 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9006' },
    },
    {
      name: 'beam-w007',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-07 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9007' },
    },
    {
      name: 'beam-w008',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-08 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9008' },
    },
    {
      name: 'beam-w009',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-09 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9009' },
    },
    {
      name: 'beam-w010',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-10 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9010' },
    },
  ]
};
