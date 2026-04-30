// 5 tony workers affiliated with UID 104 (beam-05)
const COMMON_ENV = {
  SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
  BEAM_WORKER_REGION: 'US',
  CONNECTION_MODE: 'auto',
  BEAM_WORKER_STAGGER: '5',
  BEAM_ORCHESTRATOR_HOTKEYS: '5Eh2KCFgfic6gS5XQ4zBwhfstC1s8cduwkka1Qe3ot91mt55',  // UID 104
  BEAM_ORCHESTRATOR_API_KEY: 'b1m_edf6088af5a7869b243421ae96c742075f59785e2024ae24',
};

const COMMON = {
  interpreter: '/root/beam/.venv/bin/python',
  cwd: '/root/beam/neurons/worker',
};

module.exports = {
  apps: [
    {
      name: 'tony-w002',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-002 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9102' },
    },
    {
      name: 'tony-w003',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-003 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9103' },
    },
    {
      name: 'tony-w004',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-004 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9104' },
    },
    {
      name: 'tony-w005',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-005 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9105' },
    },
    {
      name: 'tony-w006',
      script: 'worker.py',
      args: '--wallet.name tony --wallet.hotkey tony-006 --subtensor.network finney',
      ...COMMON,
      env: { ...COMMON_ENV, BEAM_WORKER_PORT: '9106' },
    },
  ]
};
