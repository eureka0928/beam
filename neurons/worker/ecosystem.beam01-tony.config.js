// 20 tony workers affiliated with lord orchestrator (UID 51)
const LORD_HOTKEY = '5Dr8M4jutm9Lpfqoz2Ymmzj2c96oqnJMozwMJkZwZv5VRmNB';

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
      BEAM_ORCHESTRATOR_HOTKEYS: LORD_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_27e915c94fb5d9a96f5c667359848aa8e377cdbc238e39f1',
      CONNECTION_MODE: 'auto',
    }
  });
}

module.exports = { apps };
