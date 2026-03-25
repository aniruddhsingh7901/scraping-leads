#!/usr/bin/env python3
"""
Validator-Style Lead Checker  (validate_leads.py)
==================================================
Simulates the FULL validator pipeline on your fixed leads to predict
how many would survive real validator checks.

Stages (mirrors real validator automated_checks.py):
  Stage 0: Hardcoded checks — email format, name-email match, generic/free/disposable
  Stage 1: DNS — MX record check
  Stage 2: Domain reputation — (skipped, needs DNSBL infra)
  Stage 3: Email deliverability — basic MX check (TrueList not available locally)
  Stage 4: LinkedIn verification — Serper.dev Google search for person + company
  Stage 5: Data quality — role, industry, employee count, location

Usage:
    export SERPER_API_KEY="..."
    python validate_leads.py
    python validate_leads.py --input leads_output/all_leads_fixed.json --skip-serper
"""

import argparse
import os as _os_preload


# ── Load .env FIRST so API keys are available when running standalone ─────────
def _load_env():
    """
    Load .env file into os.environ.
    Strips surrounding quotes from values (handles ANTHROPIC_API_KEY="sk-..." format).
    """
    import os as _os
    env_path = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), '..', '..', '.env'
    )
    if _os.path.exists(env_path):
        with open(env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _v = _line.split('=', 1)
                # Strip whitespace then surrounding quotes
                _v = _v.strip().strip('"').strip("'")
                _os.environ.setdefault(_k.strip(), _v)


_load_env()

import dns.resolver
import json
import os
import re
import socket
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

GENERIC_EMAIL_PREFIXES = {
    "info", "hello", "contact", "support", "admin", "team", "sales",
    "marketing", "noreply", "no-reply", "help", "service", "office",
    "mail", "webmaster", "enquiries", "enquiry", "general", "press",
    "media", "hr", "jobs", "careers", "billing", "invoice", "legal",
    "privacy", "security", "abuse", "postmaster", "bounce",
    "hi", "news", "editor", "advertise", "partners", "outreach",
    "communications", "crew", "staff", "community", "reachus", "talk",
    "welcome", "inquiries", "feedback", "ask", "connect", "owner", "ceo",
    "founder", "information", "thirdpartymanagement", "partnerprogram",
    "mediainquiries", "comms", "resumes", "human", "digital",
    "subs", "pr", "lawyer", "customerservices", "web", "dtec.sales",
    "askalfred",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "proton.me", "live.com", "msn.com",
    "yandex.com", "mail.com", "zoho.com", "gmx.com", "fastmail.com",
}

DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.com", "mailinator.com", "10minutemail.com",
    "guerrillamail.com", "sharklasers.com", "yopmail.com", "maildrop.cc",
    "trashmail.com", "fakeinbox.com", "discard.email", "temp-mail.org",
}

GARBAGE_NAMES = {
    "united states", "closest match", "go back login", "advertising advertise",
    "survey companies construction planning", "transportation submit",
    "pennsylvania ave", "enquiry type", "buy reports newsletters",
    "my folder", "responsible disclosure program", "crescent hotels",
    "fort lauderdale office", "texas texas", "glenwood ave unit",
    "additional information tell", "shapiro deputy", "orange county",
    "cohen claudine", "attorney advertising", "harlow street",
    "middle east insurance review", "uncertain how",
}

VALID_EMPLOYEE_RANGES = {
    "0-1", "2-10", "11-50", "51-200", "201-500",
    "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

GENERIC_ROLES = {
    "employee", "worker", "staff", "person", "member", "user",
    "head of", "n/a", "na", "none", "",
}

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
GARBAGE_CITY_RE = re.compile(
    r"screen share|^information |^floor |high school|^s for |"
    r"ails? ai governance|ceo$|president$|founder$|^\d+\s|"
    r"despite|privacy concern|^ries$",
    re.IGNORECASE,
)
SUSPICIOUS_CHAR_RE = re.compile(r'[<>{}|\\\^~`\[\]]')


# ─────────────────────────────────────────────────────────────────────────────
# SERPER.DEV SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def _search_serper(query: str, max_results: int = 5) -> List[Dict]:
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        return []
    try:
        payload = json.dumps({"q": query, "num": max_results})
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=payload.encode("utf-8"),
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for item in data.get("organic", []):
            results.append({
                "href": item.get("link", ""),
                "title": item.get("title", ""),
                "body": item.get("snippet", ""),
            })
        return results[:max_results]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 0: HARDCODED CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def stage0_hardcoded(lead: Dict) -> List[str]:
    """Instant rejection checks: email, name, required fields."""
    fails = []

    # Required fields
    for field in ["email", "full_name", "business", "country", "role", "industry"]:
        if not (lead.get(field) or "").strip():
            fails.append(f"S0: missing required field: {field}")

    email = (lead.get("email") or "").strip().lower()
    if not email:
        return fails

    # Email format
    if not EMAIL_RE.match(email):
        fails.append(f"S0: invalid email format")
        return fails

    local = email.split("@")[0]
    domain = email.split("@")[1]

    # Generic email
    if local in GENERIC_EMAIL_PREFIXES:
        fails.append(f"S0: generic email prefix: {local}@")

    # Free email domain
    if domain in FREE_EMAIL_DOMAINS:
        fails.append(f"S0: free email domain: {domain}")

    # Disposable
    if domain in DISPOSABLE_DOMAINS:
        fails.append(f"S0: disposable email domain: {domain}")

    # Name-email match (validator checks first/last name appears in email)
    first = (lead.get("first") or "").strip().lower()
    last = (lead.get("last") or "").strip().lower()
    if first and last:
        # Check if first or last name appears in email local part
        name_in_email = (first in local) or (last in local) or (first[0] + last in local)
        if not name_in_email:
            fails.append(f"S0: name not in email ({first} {last} vs {local}@)")

    # Full name checks
    name = (lead.get("full_name") or "").strip()
    if name:
        if name.lower() in GARBAGE_NAMES:
            fails.append(f"S0: garbage name: {name}")
        elif len(name.split()) < 2:
            fails.append(f"S0: name not First Last: {name}")

    # Role check
    role = (lead.get("role") or "").strip()
    if role and role.lower() in GENERIC_ROLES:
        fails.append(f"S0: generic role: {role}")

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1: DNS / MX CHECK
# ─────────────────────────────────────────────────────────────────────────────

def stage1_dns(lead: Dict) -> List[str]:
    """Check MX records exist for email domain."""
    fails = []
    email = (lead.get("email") or "").strip()
    if not email or "@" not in email:
        return fails

    domain = email.split("@")[1]
    try:
        mx = dns.resolver.resolve(domain, 'MX', lifetime=5)
        if not mx:
            fails.append(f"S1: no MX records for {domain}")
    except dns.resolver.NXDOMAIN:
        fails.append(f"S1: domain does not exist: {domain}")
    except dns.resolver.NoAnswer:
        fails.append(f"S1: no MX records for {domain}")
    except dns.resolver.NoNameservers:
        fails.append(f"S1: no nameservers for {domain}")
    except Exception:
        # DNS timeout or other transient — don't fail
        pass

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 4: LINKEDIN VERIFICATION (via Serper.dev Google search)
# ─────────────────────────────────────────────────────────────────────────────

def stage4_linkedin(lead: Dict, use_serper: bool = True) -> List[str]:
    """
    Simulate validator Stage 4: LinkedIn person + company verification.
    Uses Serper.dev to Google search "{name}" "{company}" linkedin
    and checks if results contain matching LinkedIn profile.
    """
    fails = []
    warnings = []

    # Check LinkedIn URL provided
    linkedin = (lead.get("linkedin") or "").strip()
    if not linkedin:
        fails.append(f"S4: missing linkedin URL")

    # Check company LinkedIn URL provided
    company_linkedin = (lead.get("company_linkedin") or "").strip()
    if not company_linkedin:
        fails.append(f"S4: missing company_linkedin URL")

    if not use_serper:
        return fails

    # Google search to verify person exists on LinkedIn
    name = (lead.get("full_name") or "").strip()
    biz = (lead.get("business") or "").strip()

    if not name or not biz:
        return fails

    query = f'"{name}" "{biz}" linkedin'
    results = _search_serper(query, max_results=5)

    if not results:
        warnings.append(f"S4-WARN: serper returned no results for: {query}")
        return fails  # Don't fail on search errors

    # Check if any result contains LinkedIn profile link
    found_linkedin = False
    found_name = False
    found_company = False

    name_lower = name.lower()
    biz_lower = biz.lower()

    for r in results:
        href = (r.get("href") or "").lower()
        title = (r.get("title") or "").lower()
        body = (r.get("body") or "").lower()
        text = f"{title} {body}"

        if "linkedin.com/in/" in href:
            found_linkedin = True

        # Check name in results
        name_parts = name_lower.split()
        if all(part in text for part in name_parts):
            found_name = True

        # Check company in results
        if biz_lower in text:
            found_company = True

    if not found_linkedin:
        fails.append(f"S4: no LinkedIn profile found in search for: {name}")

    if not found_name:
        fails.append(f"S4: name '{name}' not found in search results")

    if not found_company:
        # Softer — company names often differ slightly
        warnings.append(f"S4-WARN: company '{biz}' not confirmed in search results")

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 5: DATA QUALITY
# ─────────────────────────────────────────────────────────────────────────────

def stage5_data_quality(lead: Dict, seen_companies: Set[str]) -> List[str]:
    """Employee count, city, state, industry, duplicate checks."""
    fails = []

    # Employee count format
    ec = (lead.get("employee_count") or "").strip()
    if ec and ec not in VALID_EMPLOYEE_RANGES:
        fails.append(f"S5: invalid employee_count: {ec}")

    # City garbage
    city = (lead.get("city") or "").strip()
    if city and GARBAGE_CITY_RE.search(city):
        fails.append(f"S5: garbage city: {city}")

    # State check for US
    country = (lead.get("country") or "").strip()
    state = (lead.get("state") or "").strip()
    if country == "United States" and state and state.upper() not in US_STATES:
        fails.append(f"S5: invalid US state: {state}")

    # Country normalised
    if country and country.lower() in {"dubai uae", "uae", "uk", "us", "usa"}:
        fails.append(f"S5: country not normalised: {country}")

    # Industry present
    if not (lead.get("industry") or "").strip():
        fails.append(f"S5: missing industry")

    # Suspicious chars
    for field in ["business", "role", "industry", "full_name"]:
        val = (lead.get(field) or "").strip()
        if val and SUSPICIOUS_CHAR_RE.search(val):
            fails.append(f"S5: suspicious chars in {field}: {val}")

    # Duplicate company
    biz = (lead.get("business") or "").strip().lower()
    if biz and biz in seen_companies:
        fails.append(f"S5: duplicate company: {lead.get('business')}")
    elif biz:
        seen_companies.add(biz)

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# FULL PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def validate_lead_full(
    lead: Dict,
    seen_companies: Set[str],
    use_serper: bool = True,
    check_dns: bool = True,
) -> Dict:
    """
    Run full validator pipeline. Returns dict with stage results.
    """
    result = {
        "business": lead.get("business", "?"),
        "email": lead.get("email", "?"),
        "stages": {},
        "all_fails": [],
        "all_warns": [],
        "passed": True,
    }

    # Stage 0
    s0 = stage0_hardcoded(lead)
    result["stages"]["S0_hardcoded"] = "PASS" if not s0 else s0
    result["all_fails"].extend(s0)

    # Stage 1 (DNS)
    if check_dns:
        s1 = stage1_dns(lead)
        result["stages"]["S1_dns"] = "PASS" if not s1 else s1
        result["all_fails"].extend(s1)

    # Stage 4 (LinkedIn)
    s4 = stage4_linkedin(lead, use_serper=use_serper)
    result["stages"]["S4_linkedin"] = "PASS" if not s4 else s4
    result["all_fails"].extend(s4)

    # Stage 5 (Data quality)
    s5 = stage5_data_quality(lead, seen_companies)
    result["stages"]["S5_quality"] = "PASS" if not s5 else s5
    result["all_fails"].extend(s5)

    result["passed"] = len(result["all_fails"]) == 0
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main(input_path: str, output_path: str = "", skip_serper: bool = False, skip_dns: bool = False):
    print(f"\nLoading {input_path} ...")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    leads = data.get("all_leads", [])
    total = len(leads)
    print(f"Total leads: {total}")

    use_serper = not skip_serper and bool(os.environ.get("SERPER_API_KEY"))
    check_dns_flag = not skip_dns

    if use_serper:
        print(f"Serper.dev: ENABLED (will verify LinkedIn via Google search)")
    else:
        print(f"Serper.dev: DISABLED (use --skip-serper or set SERPER_API_KEY)")

    if check_dns_flag:
        print(f"DNS/MX check: ENABLED")
    else:
        print(f"DNS/MX check: DISABLED")

    print(f"\n{'='*80}")
    print(f"{'#':>4}  {'Status':6}  {'Business':30}  {'Failures'}")
    print(f"{'='*80}")

    passed_leads = []
    failed_leads = []
    seen_companies: Set[str] = set()
    stage_fail_counts: Dict[str, int] = {}

    for i, lead in enumerate(leads, 1):
        result = validate_lead_full(lead, seen_companies, use_serper, check_dns_flag)

        if result["passed"]:
            passed_leads.append(lead)
            print(f"  {i:3d}  PASS   {result['business'][:30]:30s}")
        else:
            failed_leads.append(result)
            fail_summary = "; ".join(result["all_fails"][:3])
            if len(result["all_fails"]) > 3:
                fail_summary += f" (+{len(result['all_fails'])-3} more)"
            print(f"  {i:3d}  FAIL   {result['business'][:30]:30s}  {fail_summary}")

            for f in result["all_fails"]:
                stage = f.split(":")[0].strip()
                stage_fail_counts[stage] = stage_fail_counts.get(stage, 0) + 1

        # Rate limit Serper
        if use_serper and i < total:
            time.sleep(0.2)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  VALIDATOR SIMULATION RESULTS")
    print(f"{'='*80}")
    print(f"  Total leads          : {total}")
    print(f"  PASSED all stages    : {len(passed_leads)}  ({100*len(passed_leads)/max(total,1):.1f}%)")
    print(f"  FAILED               : {len(failed_leads)}  ({100*len(failed_leads)/max(total,1):.1f}%)")
    print(f"{'='*80}")

    if stage_fail_counts:
        print(f"\n  Failures by stage:")
        for stage, count in sorted(stage_fail_counts.items()):
            stage_desc = {
                "S0": "Stage 0 - Hardcoded (email/name/fields)",
                "S1": "Stage 1 - DNS/MX records",
                "S4": "Stage 4 - LinkedIn verification",
                "S5": "Stage 5 - Data quality",
            }.get(stage, stage)
            print(f"    {stage_desc:50s} : {count}")

    # Show failed lead details
    if failed_leads:
        print(f"\n  Failed lead details:")
        for r in failed_leads:
            print(f"    {r['business']:30s} {r['email']}")
            for f in r["all_fails"]:
                print(f"      - {f}")

    # ── Save PASSED leads to output_path (or final_leads.json by default) ────
    output_dir = os.path.dirname(os.path.abspath(input_path))
    if output_path:
        final_json = output_path
        final_csv  = output_path.replace(".json", ".csv")
    else:
        final_json = os.path.join(output_dir, "final_leads.json")
        final_csv  = os.path.join(output_dir, "final_leads.csv")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    final_output = {
        "all_leads": passed_leads,
        "total_leads": len(passed_leads),
        "validated_at": now.isoformat(),
        "validation_stats": {
            "input_leads": total,
            "passed": len(passed_leads),
            "failed": len(failed_leads),
            "pass_rate": f"{100*len(passed_leads)/max(total,1):.1f}%",
            "serper_enabled": use_serper,
            "dns_enabled": check_dns_flag,
        },
    }

    with open(final_json, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {len(passed_leads)} PASSED leads -> {final_json}")

    # CSV export
    import csv
    if passed_leads:
        fieldnames = [
            "business", "full_name", "first", "last", "email", "role",
            "linkedin", "website", "company_linkedin", "industry", "sub_industry",
            "country", "state", "city", "hq_country", "hq_state", "hq_city",
            "employee_count", "phone_numbers", "description", "source_url", "source_type",
        ]
        with open(final_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for lead in passed_leads:
                row = dict(lead)
                row["phone_numbers"] = "; ".join(lead.get("phone_numbers") or [])
                writer.writerow(row)
        print(f"  Saved CSV             -> {final_csv}")

    print()
    return len(passed_leads), len(failed_leads)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate validator checks on leads (Serper.dev for LinkedIn verification)"
    )
    parser.add_argument(
        "--input", "-i",
        default=os.path.join(os.path.dirname(__file__), "leads_output", "all_leads_fixed.json"),
        help="Input JSON file (must contain {'all_leads': [...]})",
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        help="Output JSON path (default: final_leads.json next to input file)",
    )
    parser.add_argument(
        "--skip-serper",
        action="store_true",
        help="Skip Serper.dev LinkedIn verification (Stage 4 only checks URL presence)",
    )
    parser.add_argument(
        "--skip-dns",
        action="store_true",
        help="Skip DNS/MX record checks (Stage 1)",
    )
    args = parser.parse_args()
    main(
        args.input,
        output_path=args.output,
        skip_serper=args.skip_serper,
        skip_dns=args.skip_dns,
    )
