#!/usr/bin/env python3
"""
fix_rejected_leads.py — Automated rejected lead fixer using Claude Code CLI
============================================================================
Reads rejected_leads.json, sends each lead to Claude to fix/fill missing fields,
re-validates, then pushes passing leads to all_leads.json + data/leads.json.

Runs in a loop — safe to keep running 24/7 via PM2.
Uses your claude.ai subscription via the `claude --print` CLI — no API key needed.

Usage:
    python3 fix_rejected_leads.py              # run once then exit
    python3 fix_rejected_leads.py --watch      # loop forever, check every 5 min
    pm2 start fix_rejected_leads.py --interpreter python3 --name lead-fixer
"""

import json
import os
import sys
import subprocess
import time
import re
import shutil
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
OUTPUT_DIR   = os.path.join(SCRIPT_DIR, "leads_output")

REJECTED_FILE  = os.path.join(OUTPUT_DIR, "rejected_leads.json")
ALL_LEADS_FILE = os.path.join(OUTPUT_DIR, "all_leads.json")
POOL_FILE      = os.path.join(PROJECT_ROOT, "data", "leads.json")
FIXED_LOG      = os.path.join(OUTPUT_DIR, "fixed_leads_log.json")
STILL_BAD_FILE = os.path.join(OUTPUT_DIR, "still_rejected.json")

# Claude Code binary — auto-detected
_CLAUDE_CANDIDATES = [
    "/root/.vscode-server/extensions/anthropic.claude-code-2.1.83-linux-x64/resources/native-binary/claude",
    shutil.which("claude") or "",
]
CLAUDE_BIN = next((p for p in _CLAUDE_CANDIDATES if p and os.path.isfile(p)), None)

# ── Validation rules (mirrors gateway requirements) ───────────────────────────
REQUIRED_FIELDS = [
    "business", "full_name", "first", "last", "email", "role",
    "linkedin", "website", "company_linkedin", "description",
    "employee_count", "industry", "sub_industry",
    "country", "city", "source_url", "source_type",
]

VALID_EMPLOYEE_COUNTS = [
    "0-1", "2-10", "11-50", "51-200", "201-500",
    "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+",
]

VALID_COUNTRIES = {"United States", "United Arab Emirates"}


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _quick_validate(lead: dict) -> tuple[bool, str]:
    """Fast local checks before spending a Claude call."""
    # Required fields
    for f in REQUIRED_FIELDS:
        if not lead.get(f):
            return False, f"missing field: {f}"

    # Country
    country = lead.get("country", "")
    if country not in VALID_COUNTRIES:
        return False, f"invalid country: {country} (must be United States or United Arab Emirates)"

    # UAE must be Dubai
    if country == "United Arab Emirates" and lead.get("city", "").lower() != "dubai":
        return False, f"UAE lead must have city=Dubai, got: {lead.get('city')}"

    # US leads need state
    if country == "United States" and not lead.get("state"):
        return False, "US lead missing state"

    # City must not contain commas (gateway rejects)
    city = lead.get("city", "")
    if "," in city:
        return False, f"city contains comma (not allowed): {city}"

    # LinkedIn format
    li_person  = lead.get("linkedin", "")
    li_company = lead.get("company_linkedin", "")
    if "linkedin.com/in/" not in li_person.lower():
        return False, f"invalid personal linkedin: {li_person}"
    if "linkedin.com/company/" not in li_company.lower():
        return False, f"invalid company linkedin: {li_company}"

    # Email basic format
    email = lead.get("email", "")
    if "@" not in email or "." not in email.split("@")[-1]:
        return False, f"invalid email format: {email}"

    # Description length
    desc = lead.get("description", "")
    if len(desc) < 70:
        return False, f"description too short: {len(desc)} chars (need 70+)"

    # employee_count
    if lead.get("employee_count") not in VALID_EMPLOYEE_COUNTS:
        return False, f"invalid employee_count: {lead.get('employee_count')}"

    # Role min length
    if len(lead.get("role", "")) < 2:
        return False, "role too short"

    return True, ""


BATCH_SIZE = 5   # leads per Claude call

_FIX_PROMPT = """You are a B2B lead data fixer. Fix the JSON leads below so they pass all gateway validation rules.

VALIDATION RULES:
- Required fields (ALL must be non-empty): business, full_name, first, last, email, role, linkedin, website, company_linkedin, description, employee_count, industry, sub_industry, country, city, source_url, source_type
- country: ONLY "United States" or "United Arab Emirates"
- If country is "United Arab Emirates": city MUST be exactly "Dubai" (the ONLY UAE city the gateway accepts), state MUST be ""
- If country is "United States": state (2-letter code e.g. "CA", "IL", "TX") is REQUIRED

CITY RULES (CRITICAL — gateway validates city against a real geo database):
- city MUST be a real, known city that exists for that state/country
- For UAE: ALWAYS use city = "Dubai" (only accepted UAE city, no exceptions)
- For US: use the company's actual HQ city. If unknown, use the largest/most well-known city in the state
  - CA: San Francisco, Los Angeles, San Diego, San Jose, Sacramento, Palo Alto, Oakland, Irvine
  - TX: Houston, Austin, Dallas, San Antonio, Fort Worth, Plano
  - NY: New York City, Buffalo, Rochester, Albany
  - FL: Miami, Orlando, Tampa, Jacksonville, Fort Lauderdale
  - IL: Chicago, Aurora, Naperville
  - WA: Seattle, Bellevue, Tacoma, Redmond
  - GA: Atlanta, Savannah
  - MA: Boston, Cambridge, Worcester
  - CO: Denver, Boulder, Colorado Springs
  - VA: Richmond, Arlington, Alexandria
  - NC: Charlotte, Raleigh, Durham
  - OH: Columbus, Cleveland, Cincinnati
  - AZ: Phoenix, Scottsdale, Tempe, Mesa
  - MN: Minneapolis, Saint Paul
  - NJ: Newark, Jersey City, Princeton
  - PA: Philadelphia, Pittsburgh
  - MI: Detroit, Grand Rapids, Ann Arbor
  - OR: Portland, Eugene
  - UT: Salt Lake City, Provo
  - NV: Las Vegas, Reno
  - TN: Nashville, Memphis, Knoxville
  - MO: Kansas City, Saint Louis
- NO commas in city field (e.g. "New York City" not "New York, NY")
- Infer city from: website domain, company name, phone area code, LinkedIn URL, description text, or address clues

- linkedin: must contain "linkedin.com/in/" (personal profile)
- company_linkedin: must contain "linkedin.com/company/"
- email: valid business email (first.last@companydomain.com). If domain in email doesn't match website, fix to match website domain.
- description: minimum 70 characters — expand if too short using company name/industry context
- employee_count: must be EXACTLY one of: "0-1", "2-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+"
- role: minimum 2 chars, real job title (CEO, CTO, Founder, President, etc.)
- source_url: same as website is fine
- source_type: use "company_site"
- hq_city, hq_state, hq_country: mirror city/state/country if empty

LEADS TO FIX (JSON array — one entry per lead with its rejection reason):
{leads_json}

IMPORTANT: Return ONLY a valid JSON array (same number of objects as input, in the same order).
No markdown fences, no explanation text — just the raw JSON array.
Every required field MUST be present and non-empty in every object. Use real values inferred from context clues (domain, phone area code, LinkedIn slug, description text, etc.)."""


def _fix_batch_with_claude(batch: list[dict]) -> list[dict | None]:
    """
    Call `claude --print` to fix a batch of leads in one shot.
    Returns a list of fixed dicts (same length as input); entry is None if unfixable.
    """
    if not CLAUDE_BIN:
        _log("  ⚠️  Claude CLI not found — skipping AI fix")
        return [None] * len(batch)

    # Build payload: list of {lead, rejection_reason}
    payload = []
    for lead in batch:
        clean = {k: v for k, v in lead.items() if not k.startswith("_")}
        payload.append({
            "_rejection_reason": lead.get("_rejected_reason", "unknown"),
            **clean,
        })

    prompt = _FIX_PROMPT.format(leads_json=json.dumps(payload, indent=2))

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            _log("  ⚠️  Claude returned empty output for batch")
            return [None] * len(batch)

        # Extract JSON array from output
        arr_match = re.search(r'\[.*\]', output, re.DOTALL)
        if not arr_match:
            _log("  ⚠️  No JSON array found in Claude output — trying object fallback")
            # If only 1 lead and Claude returned a single object, wrap it
            obj_match = re.search(r'\{.*\}', output, re.DOTALL)
            if obj_match and len(batch) == 1:
                try:
                    return [json.loads(obj_match.group())]
                except Exception:
                    pass
            return [None] * len(batch)

        fixed_list = json.loads(arr_match.group())
        if not isinstance(fixed_list, list):
            _log("  ⚠️  Claude returned non-list JSON")
            return [None] * len(batch)

        # Pad or trim to match batch size
        while len(fixed_list) < len(batch):
            fixed_list.append(None)
        return fixed_list[:len(batch)]

    except subprocess.TimeoutExpired:
        _log("  ⚠️  Claude timed out for batch")
        return [None] * len(batch)
    except json.JSONDecodeError as e:
        _log(f"  ⚠️  JSON parse error in batch: {e}")
        return [None] * len(batch)
    except Exception as e:
        _log(f"  ⚠️  Claude error for batch: {e}")
        return [None] * len(batch)


def _load_json_list(path: str) -> list:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, list) else d.get("all_leads", [])
    except Exception:
        return []


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _lead_keys(leads: list) -> tuple[set, set]:
    """Return (emails, linkedins) seen sets for fast dedup."""
    emails    = set()
    linkedins = set()
    for l in leads:
        e = (l.get("email") or "").lower().strip()
        li = (l.get("linkedin") or "").lower().strip().rstrip("/")
        if e:
            emails.add(e)
        if li:
            linkedins.add(li)
    return emails, linkedins


def _is_duplicate(lead: dict, emails: set, linkedins: set) -> bool:
    e  = (lead.get("email") or "").lower().strip()
    li = (lead.get("linkedin") or "").lower().strip().rstrip("/")
    return (e and e in emails) or (li and li in linkedins)


def _push_to_pool(lead: dict):
    """Add lead to data/leads.json (dedup by email + linkedin)."""
    pool = _load_json_list(POOL_FILE)
    emails, linkedins = _lead_keys(pool)
    if not _is_duplicate(lead, emails, linkedins):
        pool.append(lead)
        os.makedirs(os.path.dirname(POOL_FILE), exist_ok=True)
        _save_json(POOL_FILE, pool)
        return True
    return False


def _push_to_all_leads(lead: dict):
    """Add lead to all_leads.json (dedup by email + linkedin)."""
    try:
        with open(ALL_LEADS_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {"all_leads": [], "total_leads_ever": 0}
    leads = data.get("all_leads", [])
    emails, linkedins = _lead_keys(leads)
    if not _is_duplicate(lead, emails, linkedins):
        leads.append(lead)
        data["all_leads"] = leads
        data["total_leads_ever"] = len(leads)
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_json(ALL_LEADS_FILE, data)
        return True
    return False


def process_rejected_leads():
    """Main processing loop — one pass through rejected_leads.json."""
    if not os.path.exists(REJECTED_FILE):
        _log("No rejected_leads.json found — nothing to fix")
        return 0

    rejected = _load_json_list(REJECTED_FILE)
    if not rejected:
        _log("rejected_leads.json is empty")
        return 0

    _log(f"Processing {len(rejected)} rejected lead(s) in batches of {BATCH_SIZE}...")

    fixed_leads   = []   # successfully fixed + validated
    still_bad     = []   # still failing after Claude fix
    needs_fix     = []   # leads that failed local re-validate → queue for Claude
    fixed_log     = _load_json_list(FIXED_LOG) if os.path.exists(FIXED_LOG) else []

    # ── Pass 1: local re-validate (free — no Claude calls) ────────────────────
    for i, lead in enumerate(rejected, 1):
        biz   = lead.get("business", "?")
        email = lead.get("email", "?")
        _log(f"  [{i}/{len(rejected)}] {biz} ({email})")

        clean = {k: v for k, v in lead.items() if not k.startswith("_")}
        ok, val_reason = _quick_validate(clean)
        if ok:
            _log(f"    ✅ Passes validation now (no fix needed)")
            fixed_leads.append(clean)
        else:
            _log(f"    ❌ Still invalid: {val_reason}")
            lead["_rejected_reason"] = val_reason  # refresh reason
            needs_fix.append(lead)

    # ── Pass 2: batch-send to Claude ──────────────────────────────────────────
    if needs_fix:
        _log(f"  🤖 Sending {len(needs_fix)} lead(s) to Claude in batches of {BATCH_SIZE}...")
        for batch_start in range(0, len(needs_fix), BATCH_SIZE):
            batch = needs_fix[batch_start : batch_start + BATCH_SIZE]
            names = ", ".join(l.get("business", "?") for l in batch)
            _log(f"    Batch [{batch_start+1}–{batch_start+len(batch)}]: {names}")

            fixed_list = _fix_batch_with_claude(batch)

            for lead, fixed in zip(batch, fixed_list):
                original_reason = lead.get("_rejected_reason", "unknown")
                biz = lead.get("business", "?")
                if not fixed or not isinstance(fixed, dict):
                    _log(f"      ⚠️  {biz}: Claude returned nothing")
                    still_bad.append(lead)
                    continue

                ok2, reason2 = _quick_validate(fixed)
                if ok2:
                    _log(f"      ✅ {biz}: fixed → pushing to pool")
                    fixed["source_type"] = fixed.get("source_type") or "company_site"
                    fixed_leads.append(fixed)
                    fixed_log.append({
                        **fixed,
                        "_fixed_at": datetime.now(timezone.utc).isoformat(),
                        "_original_reason": original_reason,
                    })
                else:
                    _log(f"      ⚠️  {biz}: still invalid after fix: {reason2}")
                    lead["_rejected_reason"] = reason2
                    still_bad.append(lead)

            # Brief pause between batches
            if batch_start + BATCH_SIZE < len(needs_fix):
                time.sleep(3)

    # ── Dedup fixed_leads within this run (email + linkedin) before pushing ──
    seen_e, seen_li = set(), set()
    deduped = []
    for lead in fixed_leads:
        e  = (lead.get("email") or "").lower().strip()
        li = (lead.get("linkedin") or "").lower().strip().rstrip("/")
        if (e and e in seen_e) or (li and li in seen_li):
            _log(f"  ⚠️  Skipping in-run duplicate: {lead.get('business','?')} ({e})")
            continue
        if e:  seen_e.add(e)
        if li: seen_li.add(li)
        deduped.append(lead)
    fixed_leads = deduped

    # ── Push all fixed leads ──────────────────────────────────────────────────
    pushed_pool = 0
    pushed_all  = 0
    for lead in fixed_leads:
        if _push_to_all_leads(lead):
            pushed_all += 1
        if _push_to_pool(lead):
            pushed_pool += 1

    # ── Save updated files ────────────────────────────────────────────────────
    _save_json(REJECTED_FILE, still_bad)
    if fixed_log:
        _save_json(FIXED_LOG, fixed_log)
    if still_bad:
        _save_json(STILL_BAD_FILE, still_bad)

    _log(f"Done: {len(fixed_leads)} fixed | {pushed_pool} pushed to pool | {len(still_bad)} still rejected")
    return len(fixed_leads)


def main():
    watch_mode = "--watch" in sys.argv

    _log("=" * 55)
    _log("  Lead Fixer — uses Claude Code (your subscription)")
    _log(f"  Claude CLI : {CLAUDE_BIN or 'NOT FOUND'}")
    _log(f"  Mode       : {'watch (loop every 5 min)' if watch_mode else 'single pass'}")
    _log("=" * 55)

    if not CLAUDE_BIN:
        _log("ERROR: claude CLI binary not found. Cannot fix leads.")
        sys.exit(1)

    if watch_mode:
        while True:
            process_rejected_leads()
            _log("Sleeping 5 min before next check...")
            time.sleep(300)
    else:
        process_rejected_leads()


if __name__ == "__main__":
    main()
