// 5 workers (beam-w016..w020) using tony-017..021 hotkeys.
// BEAM_ORCHESTRATOR_HOTKEYS set to UID 102 for initial affiliation; SLA-join to other 5
// orchestrators happens via sla_join_all.py without restart.
const ORCH_HOTKEY = '5HZELQTGxn92kZcupRfrpn45qxeu83S1pmsiwoLKGyESSeH3';
const ORCH_API_KEY = 'b1m_ce5ad0028c626900876ed5a23b9ea8cfa9e74265dd2f28c1';

const apps = [];
for (let i = 16; i <= 20; i++) {
  const wnum = String(i).padStart(3, '0');       // w016
  const hnum = String(i + 1).padStart(3, '0');   // tony-017 (offset to skip 011-016 already used)
  apps.push({
    name: `beam-w${wnum}`,
    script: '/root/beam/neurons/worker/worker.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/worker',
    args: `--wallet.name tony --wallet.hotkey tony-${hnum} --subtensor.network finney`,
    env: {
      SUBNET_CORE_URL: 'https://beamcore.b1m.ai',
      BEAM_ORCHESTRATOR_HOTKEYS: ORCH_HOTKEY,
      BEAM_WORKER_REGION: 'US',
      BEAM_ORCHESTRATOR_API_KEY: ORCH_API_KEY,
      BEAM_WORKER_STAGGER: String((i - 15) * 30),  // 30s, 60s, 90s, 120s, 150s
      CONNECTION_MODE: 'auto',
    }
  });
}
module.exports = { apps };
