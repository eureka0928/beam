// 5 tony workers for beam-04 orchestrator (UID 102) with 15s stagger
const BEAM04_HOTKEY = '5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3';

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
      BEAM_ORCHESTRATOR_HOTKEYS: BEAM04_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_8312417dc4fa15aceec74dda78aab097c53ce784df71e39a',
      BEAM_WORKER_STAGGER: String(i * 15),
      CONNECTION_MODE: 'auto',
    }
  });
}
module.exports = { apps };
