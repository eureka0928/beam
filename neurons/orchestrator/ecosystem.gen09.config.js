const fs = require('fs');
const path = require('path');

const envFile = fs.readFileSync(path.join(__dirname, '.env.gen09'), 'utf8');
const env = {};
envFile.split('\n').forEach(line => {
  line = line.trim();
  if (line && !line.startsWith('#')) {
    const [key, ...vals] = line.split('=');
    env[key.trim()] = vals.join('=').trim();
  }
});

module.exports = {
  apps: [{
    name: 'beam-gen-09',
    script: 'main.py',
    interpreter: '/root/beam/.venv/bin/python',
    cwd: '/root/beam/neurons/orchestrator',
    env: env,
  }]
};
