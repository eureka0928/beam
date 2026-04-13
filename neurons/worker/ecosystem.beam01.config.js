const BEAM01_HOTKEY = "5ES591VqvYbsFL7q1rLaPT225CrwDtMKptV3UUrQBLRttzxC";

const workers = [];
for (let i = 81; i <= 120; i++) {
  const id = String(i).padStart(3, '0');
  workers.push({
    name: `tony-w${id}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${id} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: BEAM01_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_7463add7fa36d7ed61f02693796de9dd06af5517209ad9e9',
      CONNECTION_MODE: 'http',
      BEAM_WORKER_PROXY: 'socks5://vadanamihai409:im5PBXYYST@185.228.195.218:59101',
    }
  });
}

module.exports = { apps: workers };
