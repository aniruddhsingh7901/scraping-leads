// ─────────────────────────────────────────────────────────────────────────────
// PM2 ecosystem config — Lead Engine 24/7
// ─────────────────────────────────────────────────────────────────────────────
//
// WHY we use a bash wrapper (run_lead_engine.sh) instead of calling python directly:
//
//   PM2 inherits its daemon environment from the shell it was first started in.
//   If that shell had PYTHONHOME set (e.g. from bittensor's Python 3.10 venv),
//   the Python 3.12 binary crashes immediately with:
//       Fatal Python error: No module named 'encodings'
//   because Python tries to find its stdlib at the OLD 3.10 prefix paths.
//
//   The shell wrapper (run_lead_engine.sh) calls `unset PYTHONHOME PYTHONPATH`
//   BEFORE exec-ing the venv Python, so it works regardless of PM2's inherited env.
//
// Usage:
//   pm2 start ecosystem.config.js
//   pm2 logs lead-engine
//   pm2 logs miner
//   pm2 stop lead-engine
//   pm2 stop miner
// ─────────────────────────────────────────────────────────────────────────────

module.exports = {
  apps: [
  {
    name: 'lead-engine',
    script: '/root/scraping-leads/run_lead_engine.sh',
    interpreter: 'none',
    cwd: '/root/scraping-leads',
    autorestart: true,
    max_restarts: 50,
    restart_delay: 30000,
    max_memory_restart: '2G',
    error_file: '/root/scraping-leads/miner_models/scrapling_leads/leads_output/pm2-error.log',
    out_file:   '/root/scraping-leads/miner_models/scrapling_leads/leads_output/pm2-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env: {
      PYTHONUNBUFFERED: '1',
      GATEWAY_URL: 'http://52.91.135.79:8000',
    },
  },
  {
    name: 'lead-fixer',
    script: '/root/scraping-leads/venv/bin/python3',
    args: '/root/scraping-leads/miner_models/scrapling_leads/fix_rejected_leads.py --watch',
    interpreter: 'none',
    cwd: '/root/scraping-leads',
    autorestart: true,
    max_restarts: 20,
    restart_delay: 30000,
    max_memory_restart: '1G',
    error_file: '/root/scraping-leads/logs/lead-fixer-error.log',
    out_file:   '/root/scraping-leads/logs/lead-fixer-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env: {
      PYTHONUNBUFFERED: '1',
    },
  },
  {
    name: 'miner',
    script: '/root/scraping-leads/run_miner.sh',
    interpreter: 'none',
    cwd: '/root/scraping-leads',
    autorestart: true,
    max_restarts: 20,
    restart_delay: 60000,   // 60 s between restarts (subtensor reconnect needs time)
    max_memory_restart: '4G',
    error_file: '/root/scraping-leads/logs/miner-error.log',
    out_file:   '/root/scraping-leads/logs/miner-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env: {
      PYTHONUNBUFFERED: '1',
      GATEWAY_URL: 'http://52.91.135.79:8000',
    },
  }]
};
