// 5 workers affiliated with UIDs 102, 104, 226 (skipping 69 — sla decayed to 0.01 floor)
const COMMON_ENV = {
  SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
  BEAM_WORKER_REGION: 'US',
  CONNECTION_MODE: 'auto',
  BT_WALLET_PASSWORD: 'beam-01',
  BEAM_WORKER_STAGGER: '5',
  BEAM_ORCHESTRATOR_HOTKEYS: [
    '5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3',  // UID 102
    '5Eh2KCFgfic6gS5XQ4zBwhfstC1s8cduwkka1Qe3ot91mt55',  // UID 104
    '5E6kGMZ4Vewcov7SYfMeZtj3dznfTznKceuZVvYUz8kmK6cn',  // UID 226
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
      name: 'beam-w001',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-01 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9001' },
    },
    {
      name: 'beam-w002',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-02 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9002' },
    },
    {
      name: 'beam-w003',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-03 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9003' },
    },
    {
      name: 'beam-w004',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-04 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9004' },
    },
    {
      name: 'beam-w005',
      script: 'worker.py',
      args: '--wallet.name beam-01 --wallet.hotkey worker-05 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9005' },
    },
  ]
};
