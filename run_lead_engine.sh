#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_lead_engine.sh  —  PM2 launcher for the 24/7 lead engine
# ─────────────────────────────────────────────────────────────────────────────
# PURPOSE:
#   PM2 inherits its daemon environment from the shell it was first started in.
#   If that shell had PYTHONHOME set (e.g. from a Python 3.10 bittensor env),
#   running the Python 3.12 venv binary will crash with:
#       Fatal Python error: No module named 'encodings'
#   because Python 3.12 looks for its stdlib under the old 3.10 prefix.
#
#   This wrapper CLEARS the offending vars before exec-ing Python,
#   regardless of what PM2 or the parent shell had set.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR='/root/scraping-leads'
VENV_PYTHON="${REPO_DIR}/venv/bin/python3"
SCRIPT="${REPO_DIR}/miner_models/scrapling_leads/run_24x7.py"

# ── Nuke any env vars that break venv Python ──────────────────────────────
unset PYTHONHOME   || true
unset PYTHONPATH   || true

# ── Make the venv first on PATH (activates venv site-packages) ───────────
export PATH="${REPO_DIR}/venv/bin:${PATH}"
export VIRTUAL_ENV="${REPO_DIR}/venv"

# ── Real-time log output ──────────────────────────────────────────────────
export PYTHONUNBUFFERED=1

# ── Change to project root so relative imports work ──────────────────────
cd "${REPO_DIR}"

# ── Run (exec replaces this shell process — clean process tree for PM2) ──
exec "${VENV_PYTHON}" "${SCRIPT}"
