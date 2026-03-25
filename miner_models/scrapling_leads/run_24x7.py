#!/usr/bin/env python3
"""
24/7 Lead Generation Runner  (INCREMENTAL v2)
=============================================
Rotates through all industries endlessly, generating leads non-stop.
After EVERY batch: ONLY the NEW leads are fixed → validated → pushed.

Pipeline flow (per batch — INCREMENTAL):
  all_leads.json   ─► (extract NEW only)  ─► pending_leads.json
  pending_leads.json ─► fix_leads.py       ─► pending_fixed.json
  pending_fixed.json ─► validate_leads.py  ─► pending_validated.json
  pending_validated  ─► APPEND             ─► all_validated.json
  all_validated.json ─► PUSH               ─► data/leads.json  (miner pool)

The "already processed" set is the union of emails in all_validated.json.
This means each lead is NEVER re-processed, regardless of how large
all_leads.json grows.

Usage:
    pm2 start run_24x7.py --name lead-engine --interpreter python3
    pm2 logs lead-engine
"""

import asyncio
import json
import os
import sys
import time
import random
import subprocess
import threading
from datetime import datetime, timezone


# ── Load .env FIRST — before any other imports ────────────────────────────────
def _load_env():
    """
    Load .env file into os.environ.
    Strips surrounding quotes from values (handles ANTHROPIC_API_KEY="sk-..." format).
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)


_load_env()

# ── Add project root to path ──────────────────────────────────────────────────
PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
)
sys.path.insert(0, PROJECT_ROOT)

from miner_models.scrapling_leads.scrapling_lead_engine import (
    get_leads, save_leads_to_json, print_lead_summary, LEADS_OUTPUT_DIR
)

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Scraper writes every new lead here immediately (append-only master log)
ALL_LEADS_PATH      = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")

# Incremental pipeline staging files (per-batch temp files)
PENDING_PATH        = os.path.join(LEADS_OUTPUT_DIR, "pending_leads.json")
PENDING_FIXED_PATH  = os.path.join(LEADS_OUTPUT_DIR, "pending_fixed.json")
PENDING_VAL_PATH    = os.path.join(LEADS_OUTPUT_DIR, "pending_validated.json")

# Cumulative store of ALL leads that have passed fix+validate
ALL_VALIDATED_PATH  = os.path.join(LEADS_OUTPUT_DIR, "all_validated.json")

# Miner pool — where pool.py / miner reads leads from
MINER_POOL_DIR  = os.path.join(PROJECT_ROOT, "data")
MINER_POOL_FILE = os.path.join(MINER_POOL_DIR, "leads.json")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

LEADS_PER_BATCH = 25

# Max leads to process through fix+validate per pipeline run.
# Keeps each run fast (a few minutes) even when there is a large backlog.
# The backlog will drain over multiple cycles automatically.
PIPELINE_BATCH_SIZE = 60

UAE_REGIONS = [
    "Dubai UAE",   # Gateway only accepts Dubai for UAE leads
]

US_REGIONS = [
    "United States",
    "San Francisco California",
    "New York City",
    "Austin Texas",
    "Seattle Washington",
    "Boston Massachusetts",
    "Chicago Illinois",
    "Los Angeles California",
    "Miami Florida",
    "Denver Colorado",
    "Atlanta Georgia",
    "Dallas Texas",
]

REGIONS = UAE_REGIONS + US_REGIONS

INDUSTRIES = [
    "Software", "SaaS", "Technology", "Artificial Intelligence",
    "Financial Services", "Health Care", "Consulting", "Real Estate",
    "Marketing", "Education", "Manufacturing", "E-Commerce",
    "Biotechnology", "Advertising", "Cybersecurity", "Energy",
    "Media", "Logistics", "Construction", "Legal", "Insurance",
    "Hospitality", "Food and Beverage", "Accounting", "Architecture",
    "Staffing", "Telecommunications", "Automotive", "Agriculture", "Gaming",
]

QUERY_MODIFIERS = [
    "",
    "startup founded 2023",
    "small business team",
    "growing company hiring",
    "agency firm boutique",
    "enterprise platform",
    "founded 2024 new",
    "inc llc corp",
    "award winning top rated",
    "series funding venture",
]

MIN_DELAY_BETWEEN_BATCHES = 2
MAX_DELAY_BETWEEN_BATCHES = 5

# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────

stats = {
    "started_at": "",
    "total_leads": 0,
    "total_batches": 0,
    "total_errors": 0,
    "leads_by_industry": {},
    "leads_by_region": {},
    "current_cycle": 0,
    "leads_in_pool": 0,
    "pipeline_runs": 0,
}

STATS_PATH = os.path.join(LEADS_OUTPUT_DIR, "runner_stats.json")


def _save_stats():
    os.makedirs(LEADS_OUTPUT_DIR, exist_ok=True)
    with open(STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _run_streamed(cmd: list, cwd: str, env: dict, timeout: int = 900) -> int:
    """
    Run a subprocess, stream stdout+stderr line-by-line to _log().
    Returns exit code.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
    )

    def _reader():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log(f"    {line}")

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        t.join(timeout=timeout)
        if t.is_alive():
            _log("  [pipeline] TIMEOUT — killing subprocess")
            proc.kill()
            t.join(5)
            return 1
        proc.wait(timeout=10)
    except Exception as exc:
        _log(f"  [pipeline] subprocess wait error: {exc}")
        proc.kill()
        return 1

    return proc.returncode


# ─────────────────────────────────────────────────────────────────────────────
# INCREMENTAL PIPELINE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_processed_emails() -> set:
    """
    Return the set of emails already processed through fix+validate.
    Source: all_validated.json  (cumulative store of all validated leads).
    This set grows over time — it's the "cursor" for incremental processing.
    """
    if not os.path.exists(ALL_VALIDATED_PATH):
        return set()
    try:
        with open(ALL_VALIDATED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        leads = data.get("all_leads", [])
        return {(l.get("email") or "").lower().strip() for l in leads if l.get("email")}
    except Exception:
        return set()


def _extract_new_leads(already_processed: set) -> list:
    """
    Read all_leads.json and return ONLY the leads whose email
    has NOT been processed yet.  O(N) scan — fast even for 10K+ leads.
    """
    if not os.path.exists(ALL_LEADS_PATH):
        return []
    try:
        with open(ALL_LEADS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        all_leads = data.get("all_leads", [])
    except Exception:
        return []

    new_leads = []
    seen_in_batch: set = set()   # dedup within this extraction too
    for lead in all_leads:
        email = (lead.get("email") or "").lower().strip()
        if not email:
            continue
        if email in already_processed or email in seen_in_batch:
            continue
        seen_in_batch.add(email)
        new_leads.append(lead)

    return new_leads


def _write_pending(leads: list) -> bool:
    """Write pending leads to pending_leads.json (fix_leads.py input)."""
    os.makedirs(LEADS_OUTPUT_DIR, exist_ok=True)
    try:
        payload = {
            "all_leads": leads,
            "total_leads_ever": len(leads),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return True
    except Exception as exc:
        _log(f"  [pipeline] Failed to write pending_leads.json: {exc}")
        return False


def _append_to_all_validated(new_validated: list) -> int:
    """
    Append newly-validated leads to all_validated.json (cumulative store).
    Deduplicates by email.  Returns number actually appended.
    """
    os.makedirs(LEADS_OUTPUT_DIR, exist_ok=True)
    existing: list = []
    if os.path.exists(ALL_VALIDATED_PATH):
        try:
            with open(ALL_VALIDATED_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f).get("all_leads", [])
        except Exception:
            existing = []

    existing_emails = {(l.get("email") or "").lower().strip() for l in existing}
    added = []
    for lead in new_validated:
        email = (lead.get("email") or "").lower().strip()
        if email and email not in existing_emails:
            existing.append(lead)
            existing_emails.add(email)
            added.append(lead)

    payload = {
        "all_leads": existing,
        "total_leads": len(existing),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(ALL_VALIDATED_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(added)


def _push_to_miner_pool(new_validated: list) -> int:
    """
    Push newly-validated leads to data/leads.json (miner pool).
    Deduplicates by email.  Returns number of NEW leads added.
    """
    os.makedirs(MINER_POOL_DIR, exist_ok=True)
    existing_pool: list = []
    if os.path.exists(MINER_POOL_FILE):
        try:
            with open(MINER_POOL_FILE, "r", encoding="utf-8") as f:
                existing_pool = json.load(f)
            if not isinstance(existing_pool, list):
                existing_pool = []
        except Exception:
            existing_pool = []

    existing_emails = {(l.get("email") or "").lower() for l in existing_pool}
    added = []
    for lead in new_validated:
        email = (lead.get("email") or "").lower()
        if email and email not in existing_emails:
            existing_pool.append(lead)
            existing_emails.add(email)
            added.append(lead)

    with open(MINER_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(existing_pool, f, indent=2, ensure_ascii=False)
    return len(added)


def _cleanup_temp_files():
    """Remove per-batch temp files after pipeline completes."""
    for path in [PENDING_PATH, PENDING_FIXED_PATH, PENDING_VAL_PATH]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# INCREMENTAL FIX → VALIDATE → PUSH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _run_fix_validate_pipeline() -> int:
    """
    INCREMENTAL pipeline — only processes leads NOT yet in all_validated.json.

    Step 0  detect new leads  — diff(all_leads.json, all_validated.json)
    Step 1  fix_leads.py       — pending_leads.json → pending_fixed.json
    Step 2  validate_leads.py  — pending_fixed.json → pending_validated.json
    Step 3  append + push      — pending_validated.json
                                 → all_validated.json  (cumulative)
                                 → data/leads.json     (miner pool)

    Returns: number of NEW leads pushed to the miner pool.
    API keys are read from os.environ (populated by _load_env() at startup).
    """
    # ── Step 0: Find leads not yet processed ─────────────────────────────────
    already_processed = _load_processed_emails()
    new_leads = _extract_new_leads(already_processed)

    if not new_leads:
        _log("  [pipeline] No new leads to process — all already validated")
        return 0

    # Cap to PIPELINE_BATCH_SIZE so each run is fast; backlog drains over cycles
    total_pending = len(new_leads)
    if total_pending > PIPELINE_BATCH_SIZE:
        new_leads = new_leads[:PIPELINE_BATCH_SIZE]
        _log(f"  [pipeline] Step 0/3 — processing {len(new_leads)} of "
             f"{total_pending} pending lead(s) (batch cap={PIPELINE_BATCH_SIZE}) "
             f"| {total_pending - len(new_leads)} will be processed next cycle")
    else:
        _log(f"  [pipeline] Step 0/3 — {len(new_leads)} new lead(s) to process "
             f"(skipping {len(already_processed)} already validated)")

    if not _write_pending(new_leads):
        return 0

    env = os.environ.copy()

    # ── Step 1: fix_leads.py on pending_leads.json only ──────────────────────
    _log(f"  [pipeline] Step 1/3 — fix_leads.py  "
         f"({len(new_leads)} leads → pending_fixed.json)")
    fix_rc = _run_streamed(
        [
            sys.executable,
            os.path.join(SCRIPT_DIR, "fix_leads.py"),
            "--input",  PENDING_PATH,
            "--output", PENDING_FIXED_PATH,
        ],
        cwd=SCRIPT_DIR,
        env=env,
        timeout=900,
    )
    if fix_rc != 0:
        _log(f"  [pipeline] fix_leads.py FAILED (exit {fix_rc})")
        _cleanup_temp_files()
        return 0

    if not os.path.exists(PENDING_FIXED_PATH):
        _log("  [pipeline] pending_fixed.json not created — aborting")
        _cleanup_temp_files()
        return 0

    # ── Step 2: validate_leads.py on pending_fixed.json ──────────────────────
    _log(f"  [pipeline] Step 2/3 — validate_leads.py  "
         f"(pending_fixed.json → pending_validated.json)")
    val_rc = _run_streamed(
        [
            sys.executable,
            os.path.join(SCRIPT_DIR, "validate_leads.py"),
            "--input", PENDING_FIXED_PATH,
            "--output", PENDING_VAL_PATH,
        ],
        cwd=SCRIPT_DIR,
        env=env,
        timeout=900,
    )
    if val_rc != 0:
        _log(f"  [pipeline] validate_leads.py FAILED (exit {val_rc})")
        _cleanup_temp_files()
        return 0

    if not os.path.exists(PENDING_VAL_PATH):
        _log("  [pipeline] pending_validated.json not produced — aborting")
        _cleanup_temp_files()
        return 0

    # ── Step 3: append to all_validated.json + push to miner pool ────────────
    _log("  [pipeline] Step 3/3 — appending validated leads → "
         "all_validated.json + data/leads.json")
    try:
        with open(PENDING_VAL_PATH, "r", encoding="utf-8") as f:
            val_data = json.load(f)
        validated_leads = val_data.get("all_leads", [])

        if not validated_leads:
            _log("  [pipeline] No leads passed validation — nothing to push")
            _cleanup_temp_files()
            return 0

        # Append to cumulative store
        appended = _append_to_all_validated(validated_leads)

        # Push to miner pool
        pushed = _push_to_miner_pool(validated_leads)

        _log(f"  [pipeline] ✅ {pushed} new leads in miner pool  "
             f"| all_validated.json total: {appended + len(_load_processed_emails()) - len(validated_leads) + appended}"
             f"  | Path: {MINER_POOL_FILE}")

        _cleanup_temp_files()
        return pushed

    except Exception as exc:
        _log(f"  [pipeline] Step 3 failed: {exc}")
        _cleanup_temp_files()
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP: process existing all_leads.json on first startup
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap_existing_leads():
    """
    On first startup (or after a crash), check if there are leads in
    all_leads.json that haven't been validated yet and process them.
    This ensures the 1578 existing leads get incrementally validated
    over the first few cycles rather than all at once.
    """
    already = _load_processed_emails()
    if not os.path.exists(ALL_LEADS_PATH):
        return
    try:
        with open(ALL_LEADS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        total = len(data.get("all_leads", []))
    except Exception:
        return

    pending = total - len(already)
    if pending > 0:
        _log(f"  [bootstrap] {pending} unprocessed leads found in all_leads.json "
             f"({len(already)} already validated) — "
             f"pipeline will process them incrementally in batches")
    else:
        _log(f"  [bootstrap] All {total} leads already validated ✅")


# ─────────────────────────────────────────────────────────────────────────────
# BATCH RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def run_batch(industry: str, region: str, cycle: int = 1) -> int:
    """
    Run one scraping batch, then immediately run the INCREMENTAL
    fix → validate → push pipeline on ONLY the new leads.
    """
    modifier = QUERY_MODIFIERS[(cycle - 1) % len(QUERY_MODIFIERS)]
    _log(f"{'='*60}")
    _log(f"BATCH  industry={industry}  region={region}  "
         f"target={LEADS_PER_BATCH}  modifier='{modifier}'")
    _log(f"{'='*60}")

    effective_industry = f"{industry} {modifier}".strip() if modifier else industry

    t0 = time.time()
    try:
        leads = await get_leads(
            num_leads=LEADS_PER_BATCH,
            industry=effective_industry,
            region=region,
        )
    except Exception as exc:
        _log(f"ERROR scraping batch: {exc}")
        stats["total_errors"] += 1
        _save_stats()
        return 0

    elapsed = time.time() - t0
    count   = len(leads)

    if leads:
        save_leads_to_json(leads, {
            "industry": industry,
            "region":   region,
            "elapsed":  round(elapsed, 2),
        })
        print_lead_summary(leads, elapsed)

    stats["total_leads"] += count
    stats["total_batches"] += 1
    stats["leads_by_industry"][industry] = (
        stats["leads_by_industry"].get(industry, 0) + count
    )
    stats["leads_by_region"][region] = (
        stats["leads_by_region"].get(region, 0) + count
    )
    _save_stats()

    _log(f"BATCH DONE: {count} new leads in {elapsed:.0f}s  "
         f"| Total scraped: {stats['total_leads']}")

    # ── Push validated leads directly to miner pool ──────────────────────
    # The scraper now validates leads inline (source URL, email, LinkedIn,
    # city, role, industry, sub_industry, LLM fix). External fix→validate
    # pipeline is no longer needed — leads in all_leads.json are pre-validated.
    if leads:
        pushed = _push_to_miner_pool(leads)
        stats["leads_in_pool"] = stats.get("leads_in_pool", 0) + pushed
        stats["pipeline_runs"] = stats.get("pipeline_runs", 0) + 1
        _save_stats()
        _log(f"  PUSHED: {pushed} new leads to miner pool  "
             f"| Pool total: {stats['leads_in_pool']}  "
             f"| Path: {MINER_POOL_FILE}")

    return count


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    stats["started_at"] = datetime.now(timezone.utc).isoformat()
    _save_stats()

    serper_keys = os.environ.get("SERPER_API_KEYS", "")
    serper_count = len([k for k in serper_keys.split(",") if k.strip()]) if serper_keys else 0
    if not serper_count:
        serper_count = 1 if os.environ.get("SERPER_API_KEY") else 0

    _log("=" * 60)
    _log("  LEAD ENGINE 24/7 RUNNER  (v3 — inline validation)")
    _log(f"  Industries   : {len(INDUSTRIES)}")
    _log(f"  Regions      : {len(REGIONS)}")
    _log(f"  Leads/batch  : {LEADS_PER_BATCH}")
    _log(f"  Pipeline     : DIRECT (scraper validates inline, push to pool)")
    _log(f"  API keys:")
    _log(f"    SERPER_API_KEYS  : "
         f"{'✅ ' + str(serper_count) + ' key(s)' if serper_count else '❌ MISSING'}")
    _log(f"    ANTHROPIC_API_KEY: "
         f"{'✅ SET (LLM fix enabled)' if os.environ.get('ANTHROPIC_API_KEY') else '⚠️  MISSING (LLM fix disabled)'}")
    _log(f"  Source leads : {ALL_LEADS_PATH}")
    _log(f"  Miner pool   : {MINER_POOL_FILE}")
    _log("=" * 60)

    # Push any existing leads from all_leads.json to miner pool at startup
    _log("\n  [startup] Syncing existing leads to miner pool...")
    if os.path.exists(ALL_LEADS_PATH):
        try:
            with open(ALL_LEADS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f).get("all_leads", [])
            if existing:
                pushed = _push_to_miner_pool(existing)
                _log(f"  [startup] Pushed {pushed} existing leads to pool")
        except Exception as exc:
            _log(f"  [startup] Error syncing existing leads: {exc}")

    cycle = 0
    while True:
        cycle += 1
        stats["current_cycle"] = cycle
        _log(f"\n{'#'*60}")
        _log(f"  CYCLE {cycle}  —  "
             f"{len(INDUSTRIES)} industries × {len(REGIONS)} regions")
        _log(f"{'#'*60}")

        industries_shuffled = list(INDUSTRIES)
        random.shuffle(industries_shuffled)

        for industry in industries_shuffled:
            region = REGIONS[
                (cycle + INDUSTRIES.index(industry)) % len(REGIONS)
            ]

            try:
                await run_batch(industry, region, cycle=cycle)
            except KeyboardInterrupt:
                _log("Ctrl+C — shutting down gracefully")
                _save_stats()
                _log(f"FINAL  scraped={stats['total_leads']}  "
                     f"batches={stats['total_batches']}  "
                     f"pool={stats['leads_in_pool']}")
                return
            except Exception as exc:
                _log(f"UNEXPECTED ERROR: {exc}")
                stats["total_errors"] += 1
                _save_stats()

            delay = random.uniform(MIN_DELAY_BETWEEN_BATCHES, MAX_DELAY_BETWEEN_BATCHES)
            _log(f"Sleeping {delay:.0f}s before next batch...")
            await asyncio.sleep(delay)

        _log(f"\nCYCLE {cycle} COMPLETE")
        _log(f"  Scraped : {stats['total_leads']} leads total")
        _log(f"  Pool    : {stats['leads_in_pool']} validated leads")
        _log(f"  Errors  : {stats['total_errors']}")
        _log(f"Starting cycle {cycle + 1} in 3s...")
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("Shutdown requested")
        _save_stats()
