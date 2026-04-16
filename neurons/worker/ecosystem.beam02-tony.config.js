// 20 tony workers affiliated with beam-02 orchestrator (UID 69)
// Staggered startup: each worker waits ~15s * index before registering
const BEAM02_HOTKEY = '5GZUs7uztXt1WPwL6KQhsVznrC1V3yoFtYLaLFEUVT9xdjLC';

const apps = [];

for (let i = 1; i <= 20; i++) {
  const num = String(i).padStart(3, '0');
  apps.push({
    name: `beam-tw${num}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${num} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: BEAM02_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_43ce5d13d0440015593bbcc990f124beddeed57b3df92a7e',
      BEAM_WORKER_STAGGER: String(i * 15),
      CONNECTION_MODE: 'auto',
    }
  });
}

module.exports = { apps };
