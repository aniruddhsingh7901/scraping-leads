#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_miner.sh  —  PM2 launcher for the LeadPoet miner (subnet 71)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR='/root/scraping-leads'
VENV_PYTHON="${REPO_DIR}/venv/bin/python3"

# Nuke any env vars that break venv Python
unset PYTHONHOME  || true
unset PYTHONPATH  || true

export PATH="${REPO_DIR}/venv/bin:${PATH}"
export VIRTUAL_ENV="${REPO_DIR}/venv"
export PYTHONUNBUFFERED=1

cd "${REPO_DIR}"

# Answer "N" to the optional qualification model prompt (non-interactive PM2 mode)
echo "N" | exec "${VENV_PYTHON}" neurons/miner.py \
    --wallet_name Ani \
    --wallet_hotkey Ani-1 \
    --netuid 71 \
    --subtensor_network finney
