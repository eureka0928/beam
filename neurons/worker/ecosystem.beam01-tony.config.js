// 5 tony workers affiliated with beam-03 orchestrator (UID 77)
// Staggered startup: each worker waits ~10s * index before registering
const BEAM03_HOTKEY = '5D82k8jUe87KhhmZdH9z3X8bBQQM6jjRcixhNiNCjnFS1DsP';

const apps = [];

for (let i = 1; i <= 5; i++) {
  const num = String(i).padStart(3, '0');
  apps.push({
    name: `beam-tw${num}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${num} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: BEAM03_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_13993002119673ced9f15c66bdb2be06b38a6e9b95ac98a4',
      BEAM_WORKER_STAGGER: String(i * 10),
      CONNECTION_MODE: 'auto',
    }
  });
}

module.exports = { apps };
