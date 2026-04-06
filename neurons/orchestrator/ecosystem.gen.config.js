const fs = require('fs');
const path = require('path');

// Parse .env.gen manually
const envFile = fs.readFileSync(path.join(__dirname, '.env.gen'), 'utf8');
const env = {};
envFile.split('\n').forEach(line => {
  line = line.trim();
  if (line && !line.startsWith('#')) {
    const [key, ...val] = line.split('=');
    if (key) env[key.trim()] = val.join('=').trim();
  }
});

module.exports = {
  apps: [{
    name: 'beam-gen-01',
    script: '/root/beam/neurons/orchestrator/main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: env
  }]
};
