// 5 more workers affiliated with UID 102
const COMMON_ENV = {
  SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
  BEAM_WORKER_REGION: 'US',
  CONNECTION_MODE: 'auto',
  BEAM_WORKER_STAGGER: '5',
  BEAM_ORCHESTRATOR_HOTKEYS: '5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3',  // UID 102
  BEAM_ORCHESTRATOR_API_KEY: 'b1m_1a2000f39b2e7b22b0074384206f0c9b2ff0d5bf3fad08fe',
};

const COMMON = {
  interpreter: '/root/beam/.venv/bin/python',
  cwd: '/root/beam/neurons/worker',
};

module.exports = {
  apps: [
    {
      name: 'beam-w011',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-012 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9019' },
    },
    {
      name: 'beam-w012',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-013 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9020' },
    },
    {
      name: 'beam-w013',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-014 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9021' },
    },
    {
      name: 'beam-w014',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-015 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9022' },
    },
    {
      name: 'beam-w015',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-016 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9023' },
    },
  ]
};
