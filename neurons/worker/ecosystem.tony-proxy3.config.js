const GEN01_HOTKEY = "5Ef43doHxGJEZbGif5h6Je1s5UoVSCpfhfbkUeBG4Vjca6gh";

const workers = [];
for (let i = 111; i <= 125; i++) {
  const id = String(i).padStart(3, '0');
  workers.push({
    name: `tony-w${id}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${id} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: GEN01_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_b720ce026090d5f975821c1d8be8ea3e38dc362553bfcb6d',
      BEAM_WORKER_PROXY: 'socks5://vadanamihai409:im5PBXYYST@95.164.207.146:59101',
    }
  });
}

module.exports = { apps: workers };
