const GEN01_HOTKEY = "5Ef43doHxGJEZbGif5h6Je1s5UoVSCpfhfbkUeBG4Vjca6gh";
const GEN02_HOTKEY = "5Ge9qXaQuxqfnVbgUkbiBJnzWgQZBUhpP8ykitYwHXikpXJx";

const workers = [];
for (let i = 1; i <= 20; i++) {
  const id = String(i).padStart(3, '0');
  workers.push({
    name: `tony-w${id}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${id} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: GEN01_HOTKEY + ',' + GEN02_HOTKEY,
      BEAM_WORKER_REGION: 'EU',
      BEAM_ORCHESTRATOR_API_KEY: 'b1m_3ed0e2e5b242f4dc8a4302beede8f6084be115ad789d7eb7',
    }
  });
}

module.exports = { apps: workers };
