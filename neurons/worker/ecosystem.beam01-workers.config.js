// beam-01 dedicated workers (10 workers affiliated with beam-01 orchestrator)
const BEAM01_HOTKEY = '5ES591VqvYbsFL7q1rLaPT225CrwDtMKptV3UUrQBLRttzxC';

const apps = [];

for (let i = 1; i <= 10; i++) {
  const num = String(i).padStart(2, '0');
  apps.push({
    name: `beam-w${num}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name beam-01 --wallet.hotkey worker-${num} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: BEAM01_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_7463add7fa36d7ed61f02693796de9dd06af5517209ad9e9',
      CONNECTION_MODE: 'auto',
    }
  });
}

module.exports = { apps };
