#!/usr/bin/env python3
"""
Lead Fixer Service  (fix_leads.py)  — COST-OPTIMIZED
=====================================================
Audits and fixes every lead in all_leads.json.

COST OPTIMIZATION STRATEGY (target: $5 per 1K leads):
  1. Maximize local rule-based fixes (FREE) — country, socials, employee_count, etc.
  2. Skip Claude entirely for leads that pass full local validation
  3. Batch 15 leads per API call (amortizes system prompt cost)
  4. Default model: claude-haiku-4-5 ($0.80/M in, $4/M out) instead of Opus ($15/$75)
  5. Support free providers: Groq (Llama), Gemini free tier, Grok (xAI)

Estimated cost per 1K leads:
  - ~40-60% pass local validation → 0 API calls
  - ~400-600 leads need AI → ~30-40 batched calls
  - Haiku: ~$0.30-0.80  |  Groq/Gemini: $0  |  Opus: ~$5-8

What it fixes per lead:
  • Deduplicates by email (keeps first occurrence only)
  • Garbage business names  (single-word, abbreviations, generic)
  • Fake full_names          ("United States", "Closest Match", nav text)
  • Generic / role-based emails (hi@, info@, advertise@, partners@)
  • Wrong / missing country   ("Dubai UAE" → "United Arab Emirates")
  • Garbage city values       ("Screen Share Chicago", "Rapides High School")
  • Wrong state values        ("Dubai" as state for US company)
  • Missing / empty role
  • Wrong industry classification
  • Junk socials              (GitHub showing newrelic, Facebook /tr pixel)

Usage:
    # FREE — use Groq (Llama 3.3 70B, free tier):
    export GROQ_API_KEY="gsk_..."
    python fix_leads.py --provider groq

    # FREE — use Google Gemini free tier:
    export GEMINI_API_KEY="..."
    python fix_leads.py --provider gemini

    # CHEAP — use Haiku (default, ~$0.50/1K leads):
    export ANTHROPIC_API_KEY="sk-ant-..."
    python fix_leads.py

    # EXPENSIVE — use Opus (if you need max quality):
    python fix_leads.py --model claude-opus-4-5
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Load .env FIRST so API keys are available when running standalone ─────────
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

# ─────────────────────────────────────────────────────────────────────────────
# GATEWAY VALIDATION CONSTANTS  (mirrors what the real gateway checks)
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
}

GARBAGE_NAMES = {
    "united states", "closest match", "go back login", "advertising advertise",
    "survey companies construction planning", "transportation submit",
    "pennsylvania ave", "enquiry type", "buy reports newsletters",
    "my folder", "responsible disclosure program", "crescent hotels",
    "fort lauderdale office", "texas texas", "glenwood ave unit",
    "additional information tell", "shapiro deputy", "orange county",
    "cohen claudine",
}

_COUNTRY_NORMALISE = {
    "dubai uae": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "dubai": "United Arab Emirates",
    "uk": "United Kingdom",
    "usa": "United States",
    "us": "United States",
    "india": "India",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

VALID_EMPLOYEE_RANGES = {
    "0-1", "2-10", "11-50", "51-200", "201-500",
    "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+",
}

JUNK_SOCIALS = {
    "github.com/newrelic",
    "github.com/jonsuh",
    "github.com/js-cookie",
    "facebook.com/tr",
    "facebook.com/2008/fbml",
    "youtube.com/@user",
    "youtube.com/@embed",
    "youtube.com/@iframe_api",
    "youtube.com/@watch",
    "youtube.com/@playlist",
    "youtube.com/@shorts",
    "instagram.com/yourpage",
    "facebook.com/yourpage",
}

# Patterns that indicate garbage city values
GARBAGE_CITY_PATTERNS = [
    r"^screen share",
    r"^information ",
    r"^floor ",
    r"high school",
    r"^s for ",
    r"ails? ai governance",
    r"ceo$",
    r"president$",
    r"founder$",
    r"^\d+\s",  # starts with numbers like "123 Main St"
    r"despite",
    r"privacy concern",
    r"^ries$",
    # Business parks / addresses, not city names
    r"digital park",
    r"free zone",
    r"business park",
    r"tech park",
    r"industrial",
    r"tower",
    r"plaza",
    r"center\b",
    r"centre\b",
    r"building",
    r"suite\b",
    r"\bave\b",
    r"\bblvd\b",
    r"\bstreet\b",
    r"\broad\b",
    r"\bdrive\b",
    # Scraped text that isn't a city
    r"lives in",
    r"americas$",
    r"^africa$",
    r"^asia$",
    r"^europe$",
    r"^middle east$",
]
_GARBAGE_CITY_RE = re.compile("|".join(GARBAGE_CITY_PATTERNS), re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# SERPER.DEV WEB SEARCH  (reuses your existing SERPER_API_KEY from .env)
# ─────────────────────────────────────────────────────────────────────────────

def _search_serper(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Google via Serper.dev — your already-configured search API."""
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
    except Exception as e:
        logger.debug(f"Serper search failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# FREE APIs — GeoNames, Wikidata, RDAP (no API key needed except GeoNames username)
# ─────────────────────────────────────────────────────────────────────────────

_GEONAMES_USER = os.environ.get("GEONAMES_USERNAME", "demo")


def _geonames_lookup(city: str) -> Optional[Dict]:
    """
    GeoNames: city → country, state/region, lat/lng.
    FREE — register at geonames.org for your own username.
    Returns {"country": "United States", "state": "CA", "city": "San Jose"} or None.
    """
    if not city or len(city) < 2:
        return None
    try:
        url = f"http://api.geonames.org/searchJSON?q={urllib.request.quote(city)}&maxRows=1&username={_GEONAMES_USER}&style=MEDIUM"
        req = urllib.request.Request(url, headers={"User-Agent": "LeadFixer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("geonames", [])
        if not results:
            return None
        r = results[0]
        return {
            "city": r.get("name", ""),
            "country": r.get("countryName", ""),
            "state": r.get("adminCode1", ""),  # 2-letter state for US
            "region": r.get("adminName1", ""),  # full state/region name
        }
    except Exception:
        return None


def _wikidata_company_lookup(company_name: str) -> Optional[Dict]:
    """
    Wikidata: company → HQ city, country, industry, employee count.
    FREE, no auth. Works great for well-known companies.
    Returns {"hq_city": "...", "hq_country": "...", "industry": "...", "employee_count": "..."} or None.
    """
    if not company_name or len(company_name) < 3:
        return None
    try:
        # Step 1: Search for entity
        search_url = f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={urllib.request.quote(company_name)}&language=en&format=json&limit=1&type=item"
        req = urllib.request.Request(search_url, headers={"User-Agent": "LeadFixer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            search_data = json.loads(resp.read().decode())
        results = search_data.get("search", [])
        if not results:
            return None
        entity_id = results[0]["id"]

        # Step 2: Get entity details via SPARQL-lite (simpler: just get claims)
        entity_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={entity_id}&props=claims|labels&languages=en&format=json"
        req = urllib.request.Request(entity_url, headers={"User-Agent": "LeadFixer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            entity_data = json.loads(resp.read().decode())

        entity = entity_data.get("entities", {}).get(entity_id, {})
        claims = entity.get("claims", {})

        info: Dict[str, str] = {}

        # P159 = headquarters location (points to another entity)
        hq_claims = claims.get("P159", [])
        if hq_claims:
            hq_id = hq_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            if hq_id:
                # Resolve HQ city name
                hq_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={hq_id}&props=labels&languages=en&format=json"
                req = urllib.request.Request(hq_url, headers={"User-Agent": "LeadFixer/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    hq_data = json.loads(resp.read().decode())
                hq_entity = hq_data.get("entities", {}).get(hq_id, {})
                hq_label = hq_entity.get("labels", {}).get("en", {}).get("value", "")
                if hq_label:
                    info["hq_city"] = hq_label

        # P17 = country (of the company entity itself)
        country_claims = claims.get("P17", [])
        if country_claims:
            country_id = country_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            if country_id:
                c_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={country_id}&props=labels&languages=en&format=json"
                req = urllib.request.Request(c_url, headers={"User-Agent": "LeadFixer/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    c_data = json.loads(resp.read().decode())
                c_label = c_data.get("entities", {}).get(country_id, {}).get("labels", {}).get("en", {}).get("value", "")
                if c_label:
                    info["hq_country"] = c_label

        # P452 = industry
        ind_claims = claims.get("P452", [])
        if ind_claims:
            ind_id = ind_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            if ind_id:
                i_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={ind_id}&props=labels&languages=en&format=json"
                req = urllib.request.Request(i_url, headers={"User-Agent": "LeadFixer/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    i_data = json.loads(resp.read().decode())
                i_label = i_data.get("entities", {}).get(ind_id, {}).get("labels", {}).get("en", {}).get("value", "")
                if i_label:
                    info["industry"] = i_label

        return info if info else None
    except Exception:
        return None


def _verify_domain_rdap(domain: str) -> bool:
    """
    RDAP: verify domain exists and is registered.
    FREE, no auth needed.
    """
    if not domain:
        return False
    try:
        url = f"https://rdap.org/domain/{domain}"
        req = urllib.request.Request(url, headers={"User-Agent": "LeadFixer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        # If we get a response with a handle/name, domain exists
        return bool(data.get("handle") or data.get("ldhName"))
    except Exception:
        return False


def _validate_city_country(city: str, country: str) -> Optional[Dict]:
    """
    Use GeoNames to verify city belongs to the claimed country.
    Returns corrected {"city", "country", "state"} or None if can't verify.
    """
    geo = _geonames_lookup(city)
    if not geo:
        return None

    geo_country = geo.get("country", "")
    if not geo_country:
        return None

    # Check if city matches claimed country
    if geo_country.lower() != country.lower():
        return {
            "city": geo.get("city", city),
            "country": geo_country,
            "state": geo.get("state", ""),
        }
    return None  # matches, no correction needed


def _enrich_lead_via_search(lead: Dict, issues: str) -> Dict:
    """
    Use Serper.dev to fill missing fields (role, city, business name, etc.).
    Returns enriched lead dict. Only searches when specific fields are missing.
    FREE with Serper free tier (2,500 queries/month).
    """
    try:
        return _enrich_lead_via_search_inner(lead, issues)
    except Exception as e:
        print(f"      Enrichment error for {lead.get('business', '?')}: {e}")
        return lead


def _enrich_lead_via_search_inner(lead: Dict, issues: str) -> Dict:
    website = lead.get("website", "")
    domain = ""
    if website:
        try:
            domain = urlparse(website).netloc.replace("www.", "")
        except Exception:
            pass

    biz = lead.get("business", "").strip()
    name = lead.get("full_name", "").strip()
    searches_done = 0

    # Search for person's role if missing
    if "missing role" in issues and name and (biz or domain):
        query = f"{name} {biz or domain} role title"
        results = _search_serper(query, max_results=3)
        searches_done += 1
        for r in results:
            text = f"{r.get('title', '')} {r.get('body', '')}".lower()
            # Look for common role patterns in search snippets
            for pattern in [
                r'\b(ceo|cto|cfo|coo|cmo|cpo|cio)\b',
                r'\b(founder|co-founder|cofounder)\b',
                r'\b(president|vice president|vp)\b',
                r'\b(director|managing director)\b',
                r'\b(partner|managing partner|senior partner)\b',
                r'\b(head of \w+)\b',
                r'\b(chief \w+ officer)\b',
                r'\b(senior|sr\.?) (manager|engineer|consultant|analyst|advisor)\b',
                r'\b(manager|engineer|consultant|analyst|advisor)\b',
                r'\b(general manager|regional manager)\b',
                r'\b(editor[- ]in[- ]chief|editor)\b',
                r'\b(principal|owner)\b',
            ]:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    role = m.group(0).strip().title()
                    lead["role"] = role
                    break
            if lead.get("role"):
                break

    # Search for company HQ / city if missing or garbage
    if not lead.get("city", "").strip() or "garbage city" in issues:
        search_name = biz if biz and len(biz) > 2 else domain
        if search_name:
            query = f'"{search_name}" headquarters location city'
            results = _search_serper(query, max_results=5)
            searches_done += 1
            for r in results:
                text = f"{r.get('title', '')} {r.get('body', '')}"
                # Pattern 1: "headquartered in City, ST"
                loc_match = re.search(
                    r'(?:headquartered|based|located|hq|headquarters)\s+(?:in\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z]{2})\b',
                    text
                )
                if loc_match:
                    lead["city"] = loc_match.group(1)
                    lead["state"] = loc_match.group(2)
                    break
                # Pattern 2: "City, State" or "City, Country" anywhere in snippet
                loc_match2 = re.search(
                    r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
                    text
                )
                if loc_match2:
                    candidate_city = loc_match2.group(1)
                    candidate_region = loc_match2.group(2)
                    # Validate it looks like a real city (not a person name or company)
                    skip_words = {"and", "the", "inc", "llc", "ltd", "corp", "group", "partners"}
                    if candidate_city.lower() not in skip_words and len(candidate_city) > 2:
                        lead["city"] = candidate_city
                        # If region is a 2-letter state equivalent
                        if candidate_region.upper() in US_STATES:
                            lead["state"] = candidate_region.upper()
                        break

    # Search for real business name if suspicious (single word, abbreviation)
    if "bad business" in issues or "suspicious business" in issues:
        if domain:
            query = f"site:{domain} company about"
            results = _search_serper(query, max_results=3)
            searches_done += 1
            for r in results:
                title = r.get("title", "").strip()
                # Extract company name from page title (usually "Company Name - About" or "About | Company Name")
                for sep in [" - ", " | ", " — ", " · "]:
                    if sep in title:
                        parts = title.split(sep)
                        candidate = parts[0].strip()
                        # Pick the part that looks like a company name (not "About", "Home", etc.)
                        if candidate.lower() in {"about", "home", "contact", "about us"}:
                            candidate = parts[-1].strip()
                        if len(candidate) > 2 and candidate.lower() not in {"about", "home", "contact", "about us"}:
                            lead["business"] = candidate
                            break
                if lead.get("business", "") != biz:
                    break

    # Search for personal LinkedIn URL if missing
    if not lead.get("linkedin") and name and (biz or domain):
        query = f'"{name}" "{biz or domain}" site:linkedin.com/in/'
        results = _search_serper(query, max_results=3)
        searches_done += 1
        for r in results:
            href = r.get("href", "")
            if "linkedin.com/in/" in href:
                lead["linkedin"] = href
                break

    # Search for company LinkedIn URL if missing
    if not lead.get("company_linkedin") and (biz or domain):
        query = f'"{biz or domain}" site:linkedin.com/company/'
        results = _search_serper(query, max_results=3)
        searches_done += 1
        for r in results:
            href = r.get("href", "")
            if "linkedin.com/company/" in href:
                lead["company_linkedin"] = href
                break

    # ── FREE API ENRICHMENT ──────────────────────────────────────────────────

    # GeoNames: verify city/country consistency
    city = lead.get("city", "").strip()
    country = lead.get("country", "").strip()
    if city and country:
        correction = _validate_city_country(city, country)
        if correction:
            # City belongs to a different country — fix it
            corrected_country = correction.get("country", "")
            # Only apply if the corrected country is US or UAE (our allowed countries)
            ALLOWED = {"United States", "United Arab Emirates"}
            if corrected_country in ALLOWED and corrected_country != country:
                lead["country"] = corrected_country
                lead["hq_country"] = corrected_country
                if correction.get("state"):
                    lead["state"] = correction["state"]
                    lead["hq_state"] = correction["state"]

    # Wikidata: look up company HQ for well-known companies (fills hq_city, hq_country)
    current_biz = lead.get("business", "").strip()
    if current_biz and (not lead.get("hq_city") or not lead.get("city")):
        wiki = _wikidata_company_lookup(current_biz)
        if wiki:
            if wiki.get("hq_city") and not lead.get("hq_city"):
                lead["hq_city"] = wiki["hq_city"]
            if wiki.get("hq_city") and not lead.get("city"):
                lead["city"] = wiki["hq_city"]
            if wiki.get("hq_country"):
                lead["hq_country"] = wiki["hq_country"]

    if searches_done > 0:
        logger.debug(f"  Enrichment: {searches_done} Serper searches for {current_biz or domain}")

    return lead


# ─────────────────────────────────────────────────────────────────────────────
# NAME-FROM-EMAIL INFERENCE  (rescue leads with garbage names but personal emails)
# ─────────────────────────────────────────────────────────────────────────────

def _infer_name_from_email(email: str) -> Optional[Tuple[str, str]]:
    """
    Try to extract a real name from a personal email address.
    e.g., frank.banda@company.com → ("Frank", "Banda")
          jsmith@company.com → None (ambiguous)
          dhernandez@company.com → None (can't split reliably)
    Returns (first, last) or None.
    """
    if not email or "@" not in email:
        return None
    local = email.split("@")[0].lower()
    if local in GENERIC_EMAIL_PREFIXES:
        return None

    # Pattern: first.last or first_last or first-last
    parts = re.split(r'[._\-]', local)
    if len(parts) == 2:
        first, last = parts
        # Both parts should be alphabetic and reasonable length
        if first.isalpha() and last.isalpha() and len(first) >= 2 and len(last) >= 2:
            return (first.capitalize(), last.capitalize())

    # Pattern: firstlast (e.g., frankbanda) — too ambiguous, skip
    return None


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL (FREE) FIXES  — applied before any API call
# ─────────────────────────────────────────────────────────────────────────────

def _is_generic_email(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local = email.split("@")[0].lower()
    return local in GENERIC_EMAIL_PREFIXES


def _is_garbage_name(name: str) -> bool:
    return name.strip().lower() in GARBAGE_NAMES


def _clean_socials(socials: Dict[str, str]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for platform, url in (socials or {}).items():
        if not url:
            continue
        url_lower = url.lower()
        if any(junk in url_lower for junk in JUNK_SOCIALS):
            continue
        clean[platform] = url
    return clean


def _fix_country(country: str) -> str:
    if not country:
        return ""
    normalised = _COUNTRY_NORMALISE.get(country.strip().lower())
    return normalised if normalised else country.strip()


def _fix_state(state: str, country: str) -> str:
    if not state:
        return ""
    s = state.strip().upper()
    if country in ("United States",) and s in US_STATES:
        return s
    if country not in ("United States",):
        return ""
    return state.strip()


def _fix_employee_count(ec: str) -> str:
    if not ec:
        return ""
    ec = ec.strip()
    return ec if ec in VALID_EMPLOYEE_RANGES else ""


def _is_garbage_city(city: str) -> bool:
    if not city:
        return False
    return bool(_GARBAGE_CITY_RE.search(city.strip()))


def _clean_city(city: str, country: str) -> str:
    """Extract real city from garbage city values."""
    if not city:
        return ""
    city = city.strip()

    # "Sean lives in Hershey" → "Hershey"
    m = re.search(r'lives?\s+in\s+(\w+(?:\s\w+)?)', city, re.I)
    if m:
        city = m.group(1).strip()

    # Reject continent/region names
    _NOT_CITIES = {"africa", "asia", "europe", "middle east", "americas",
                   "north america", "south america", "latin america"}
    if city.lower().strip() in _NOT_CITIES:
        return ""

    # Reject garbage patterns (addresses, business parks, etc.)
    if _is_garbage_city(city):
        # UAE: try to extract real city from compound value (e.g. "Dubai Digital Park" → "Dubai")
        if country == "United Arab Emirates":
            uae_cities = ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah", "Fujairah", "Al Ain"]
            for c in uae_cities:
                if c.lower() in city.lower():
                    return c
        return ""

    # UAE: also normalize clean values with compound names
    if country == "United Arab Emirates":
        uae_cities = ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah", "Fujairah", "Al Ain"]
        for c in uae_cities:
            if c.lower() in city.lower() and city != c:
                return c

    return city


def _local_fix_lead(lead: Dict) -> Dict:
    """Apply all rule-based fixes. Returns modified lead dict."""
    lead = deepcopy(lead)

    # Fix socials
    lead["socials"] = _clean_socials(lead.get("socials", {}))

    # Fix country
    lead["country"] = _fix_country(lead.get("country", ""))
    lead["hq_country"] = _fix_country(lead.get("hq_country", ""))

    # Fix state
    lead["state"] = _fix_state(lead.get("state", ""), lead["country"])
    lead["hq_state"] = _fix_state(lead.get("hq_state", ""), lead["hq_country"])

    # Fix employee_count
    lead["employee_count"] = _fix_employee_count(lead.get("employee_count", ""))

    # Fix garbage city — try to extract real city first, then clear if still garbage
    lead["city"] = _clean_city(lead.get("city", ""), lead["country"])
    lead["hq_city"] = _clean_city(lead.get("hq_city", ""), lead["hq_country"])

    # Fix city = company name (scraper bug)
    biz = lead.get("business", "").strip()
    if lead.get("city", "").strip().lower() == biz.lower() and biz:
        lead["city"] = ""

    # Fix business name: strip "Careers at", "Jobs at" etc.
    if biz:
        m = re.match(r'^(?:careers?\s+at|jobs?\s+at|hiring\s+at)\s+(.+)$', biz, re.I)
        if m:
            lead["business"] = m.group(1).strip()

    # Fix garbage descriptions (newlines, Enter Enter, too short)
    desc = lead.get("description", "")
    if desc:
        desc = re.sub(r'\\n|\n', ' ', desc).strip()
        desc = re.sub(r'\s*Enter\s+Enter\s*', ' ', desc).strip()
        desc = re.sub(r'\s{2,}', ' ', desc).strip()
        lead["description"] = desc

    # Infer name from email if name is garbage/missing
    name = lead.get("full_name", "").strip()
    if not name or _is_garbage_name(name) or len(name.split()) < 2:
        inferred = _infer_name_from_email(lead.get("email", ""))
        if inferred:
            first, last = inferred
            lead["full_name"] = f"{first} {last}"
            lead["first"] = first
            lead["last"] = last

    return lead


def _needs_ai_review(lead: Dict) -> Tuple[bool, str]:
    """
    After local fixes, check if lead still needs AI review.
    Returns (needs_review, reason).
    Leads that are CLEAN after local fixes skip the API entirely (FREE).
    """
    issues = []

    email = lead.get("email", "")
    if not email or "@" not in email:
        issues.append("missing email")
    elif _is_generic_email(email):
        issues.append("generic email")

    name = lead.get("full_name", "").strip()
    if not name or _is_garbage_name(name):
        issues.append(f"garbage name: {name!r}")
    elif len(name.split()) < 2:
        issues.append(f"name not First Last: {name!r}")

    biz = lead.get("business", "").strip()
    if not biz or len(biz) <= 2:
        issues.append(f"bad business: {biz!r}")
    elif len(biz.split()) == 1 and len(biz) <= 5:
        issues.append(f"suspicious business: {biz!r}")

    if not lead.get("country", "").strip():
        issues.append("missing country")

    if not lead.get("role", "").strip():
        issues.append("missing role")

    city = lead.get("city", "").strip()
    if city and _is_garbage_city(city):
        issues.append(f"garbage city: {city!r}")

    # LinkedIn URLs needed for validator Stage 4
    if not lead.get("linkedin", "").strip():
        issues.append("missing linkedin")
    if not lead.get("company_linkedin", "").strip():
        issues.append("missing company_linkedin")

    # City required for all leads
    if not lead.get("city", "").strip():
        issues.append("missing city")

    # Employee count required
    if not lead.get("employee_count", "").strip():
        issues.append("missing employee_count")

    # US leads MUST have state (Leadpoet gateway instant reject without it)
    country = lead.get("country", "").strip()
    if country == "United States" and not lead.get("state", "").strip():
        issues.append("missing state (US required)")
    if country == "United States" and not lead.get("hq_state", "").strip():
        issues.append("missing hq_state (US company)")

    # Garbage business names (page titles / nav text / careers pages)
    if biz:
        biz_lower = biz.lower()
        garbage_biz_prefixes = ["about ", "our ", "what we", "who we", "shadow it",
                                "public company", "careers at ", "careers ", "jobs at ",
                                "hiring at "]
        for prefix in garbage_biz_prefixes:
            if biz_lower.startswith(prefix) or biz_lower == prefix:
                issues.append(f"garbage business name: {biz!r}")
                break

    return bool(issues), "; ".join(issues)


# ─────────────────────────────────────────────────────────────────────────────
# POST-FIX LOCAL VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _post_validate(lead: Dict) -> Tuple[bool, str]:
    issues = []

    email = lead.get("email", "")
    if not email or "@" not in email:
        issues.append("missing email")
    elif _is_generic_email(email):
        issues.append(f"still generic email: {email}")

    name = lead.get("full_name", "").strip()
    if not name or _is_garbage_name(name):
        issues.append(f"garbage name: {name!r}")
    parts = name.split()
    if len(parts) < 2:
        issues.append(f"name not 'First Last': {name!r}")

    biz = lead.get("business", "").strip()
    if not biz or len(biz) <= 2:
        issues.append(f"bad business name: {biz!r}")

    country = lead.get("country", "").strip()
    if not country:
        issues.append("missing country")
    elif country.lower() in {"dubai uae", "uae", "uk", "us", "usa"}:
        issues.append(f"country not normalised: {country!r}")

    ec = lead.get("employee_count", "").strip()
    if ec and ec not in VALID_EMPLOYEE_RANGES:
        issues.append(f"invalid employee_count: {ec!r}")

    return (len(issues) == 0), "; ".join(issues)


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def deduplicate(leads: List[Dict]) -> Tuple[List[Dict], int]:
    seen: set = set()
    unique: List[Dict] = []
    dup_count = 0
    for lead in leads:
        key = (lead.get("email", "") or "").lower().strip()
        if key and key in seen:
            dup_count += 1
            continue
        if key:
            seen.add(key)
        unique.append(lead)
    return unique, dup_count


# ─────────────────────────────────────────────────────────────────────────────
# BATCHED SYSTEM PROMPT (shared across all providers)
# ─────────────────────────────────────────────────────────────────────────────

BATCH_SYSTEM_PROMPT = """You are a B2B lead data quality auditor for a Bittensor lead generation network. You receive a JSON array of leads and return a JSON array of results.

For EACH lead, decide: "keep" (good as-is), "fix" (fixable), or "discard" (unfixable).

## MANDATORY FIELDS — lead is REJECTED if ANY of these are empty:
business, full_name, first, last, email, role, website, industry, sub_industry,
country, city, linkedin, company_linkedin, source_url, description, employee_count, hq_country

## US STATE RULE (CRITICAL — leads get rejected without this):
- If country = "United States" → state MUST be a 2-letter code (CA, NY, TX, etc.)
- If country = "United States" → hq_state MUST also be filled for US-headquartered companies
- If you set country to "United States", you MUST also set state and hq_state
- If you cannot determine the US state, check the company website/domain to find HQ location
- NEVER leave state empty for a US lead — it will be instantly rejected

## COUNTRY ACCURACY (CRITICAL):
- country must reflect where the CONTACT is located, based on company office/HQ
- hq_country must reflect the company's ACTUAL global headquarters
- Do NOT blindly keep the scraped country. Verify using your knowledge:
  * Partners Group (partnersgroup.com, city=Baar) → Switzerland, NOT UAE
  * McKinsey (mckinsey.com) → hq_country="United States", hq_city="New York", hq_state="NY"
  * Careem (careem.com) → hq_country="United Arab Emirates", NOT India
  * MedTech World → hq_country="Malta", NOT United States
  * Consultancy ME (consultancy-me.com) → country="United Arab Emirates"
- If a company was found via "Dubai UAE" search but is actually HQ'd in US/Europe, fix the country

## OTHER RULES:
1. email: personal only (first.last@company.com). Generic prefixes (info@, hello@, support@, etc.) = DISCARD
2. full_name: real "First Last". Page titles, nav text, company names as names = DISCARD
3. business: REAL company name. Fix scraped page titles/nav text to actual company name
4. city: REQUIRED for ALL leads. Use your knowledge of company locations
5. employee_count: REQUIRED. Valid ranges: "0-1","2-10","11-50","51-200","201-500","501-1,000","1,001-5,000","5,001-10,000","10,001+"
6. industry/sub_industry: must match the ACTUAL company, not scraped page text
7. hq_country + hq_city: must match company's REAL headquarters

Return COMPLETE lead objects with ALL fields filled. NEVER leave state empty for US leads.

OUTPUT: JSON array, same order. Each element:
- keep/fix: COMPLETE lead + "__action__" + "__reason__"
- discard: {"__action__":"discard","__reason__":"..."}

ONLY valid JSON array. No markdown."""


def _build_batch_user_message(leads_with_issues: List[Tuple[Dict, str]]) -> str:
    batch = []
    for i, (lead, issues) in enumerate(leads_with_issues):
        batch.append({
            "index": i,
            "issues": issues or "full audit needed",
            "lead": lead,
        })
    return json.dumps(batch, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER ABSTRACTION  — Anthropic, Groq, Gemini, Grok
# ─────────────────────────────────────────────────────────────────────────────

def _create_provider(provider: str, model: str, api_key: str):
    """Returns a callable: provider_call(system, user_msg) -> str"""

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        def call(system: str, user_msg: str) -> str:
            resp = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            return resp.content[0].text.strip()
        return call

    elif provider == "groq":
        # Groq free tier: Llama 3.3 70B — FREE
        # pip install groq
        from groq import Groq
        client = Groq(api_key=api_key)
        groq_model = model or "llama-3.3-70b-versatile"

        def call(system: str, user_msg: str) -> str:
            resp = client.chat.completions.create(
                model=groq_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4096,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()
        return call

    elif provider == "gemini":
        # Google Gemini free tier — FREE
        # pip install google-genai
        from google import genai
        client = genai.Client(api_key=api_key)
        gemini_model = model or "gemini-2.0-flash"

        def call(system: str, user_msg: str) -> str:
            resp = client.models.generate_content(
                model=gemini_model,
                contents=user_msg,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=4096,
                    temperature=0.1,
                ),
            )
            return resp.text.strip()
        return call

    elif provider == "grok":
        # xAI Grok — free tier available
        # Uses OpenAI-compatible API
        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
        )
        grok_model = model or "grok-3-mini-fast"

        def call(system: str, user_msg: str) -> str:
            resp = client.chat.completions.create(
                model=grok_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4096,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()
        return call

    else:
        raise ValueError(f"Unknown provider: {provider}. Use: anthropic, groq, gemini, grok")


def _parse_llm_response(content: str) -> List[Dict]:
    """Parse LLM response with aggressive recovery for truncated/malformed JSON."""
    content = content.strip()

    # Strip markdown fences
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Attempt 1: direct parse
    try:
        result = json.loads(content)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        pass

    # Attempt 2: find JSON array in response
    match = re.search(r'\[.*\]', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Attempt 3: recover individual JSON objects from truncated response
    # (Haiku sometimes truncates the closing ] or adds trailing garbage)
    objects = []
    depth = 0
    start = None
    for i, ch in enumerate(content):
        if ch == '{' and depth == 0:
            start = i
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(content[start:i + 1])
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None

    if objects:
        return objects

    raise json.JSONDecodeError("Could not recover any JSON objects", content, 0)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FIXER
# ─────────────────────────────────────────────────────────────────────────────

def fix_leads(
    input_path: str,
    output_path: str,
    provider: str = "anthropic",
    model: str = "",
    api_key: Optional[str] = None,
    dry_run: bool = False,
    max_leads: Optional[int] = None,
    batch_size: int = 15,
    delay_between_calls: float = 0.3,
    enrich: bool = True,
) -> None:
    # ── Default model per provider ───────────────────────────────────────────
    if not model:
        model = {
            "anthropic": "claude-haiku-4-5-20251001",
            "groq": "llama-3.3-70b-versatile",
            "gemini": "gemini-2.0-flash",
            "grok": "grok-3-mini-fast",
        }.get(provider, "claude-haiku-4-5-20251001")

    # ── Load input ───────────────────────────────────────────────────────────
    print(f"\n Loading {input_path} ...")
    with open(input_path, "r", encoding="utf-8") as f:
        master = json.load(f)

    all_leads: List[Dict] = master.get("all_leads", [])
    print(f"   Loaded {len(all_leads)} raw leads")

    # ── Deduplication ────────────────────────────────────────────────────────
    all_leads, dup_count = deduplicate(all_leads)
    print(f"   After dedup: {len(all_leads)} unique  ({dup_count} duplicates removed)")

    if max_leads:
        all_leads = all_leads[:max_leads]
        print(f"   max_leads={max_leads}: processing first {max_leads} only")

    # ── Phase 1: Local fixes (FREE) ─────────────────────────────────────────
    print(f"\n--- Phase 1: Local rule-based fixes (FREE) ---")
    locally_fixed: List[Dict] = []
    needs_ai: List[Tuple[Dict, str]] = []  # (lead, issues)
    local_discarded: List[Dict] = []

    # Only US and UAE leads accepted (per subnet team)
    ALLOWED_COUNTRIES = {"United States", "United Arab Emirates"}

    for lead in all_leads:
        fixed = _local_fix_lead(lead)

        # Filter: only US and UAE leads
        lead_country = fixed.get("country", "").strip()
        if lead_country and lead_country not in ALLOWED_COUNTRIES:
            local_discarded.append({"original": lead, "reason": f"non-US/UAE country: {lead_country}"})
            continue

        needs_review, issues = _needs_ai_review(fixed)

        if not needs_review:
            locally_fixed.append(fixed)
        elif _is_generic_email(fixed.get("email", "")) and _is_garbage_name(fixed.get("full_name", "")):
            local_discarded.append({"original": lead, "reason": f"unfixable locally: {issues}"})
        else:
            needs_ai.append((fixed, issues))

    print(f"   Passed local validation (no API needed): {len(locally_fixed)}")
    print(f"   Discarded locally (unfixable):           {len(local_discarded)}")
    print(f"   Need AI review:                          {len(needs_ai)}")

    # ── Phase 1.5: Serper.dev web search enrichment (FREE with free tier) ────
    serper_searches = 0
    if needs_ai and enrich and os.environ.get("SERPER_API_KEY"):
        print(f"\n--- Phase 1.5: Serper.dev web search enrichment ---")
        enriched_ai: List[Tuple[Dict, str]] = []
        newly_passed: List[Dict] = []

        for idx, (lead, issues) in enumerate(needs_ai, 1):
            try:
                enriched = _enrich_lead_via_search(lead, issues)
                serper_searches += 1
                still_needs, new_issues = _needs_ai_review(enriched)
                if not still_needs:
                    newly_passed.append(enriched)
                else:
                    enriched_ai.append((enriched, new_issues))
            except Exception as e:
                print(f"      [{idx}] enrichment error: {e}")
                enriched_ai.append((lead, issues))
            if idx % 20 == 0:
                print(f"      ... enriched {idx}/{len(needs_ai)}")
            time.sleep(0.15)

        print(f"   Serper searches made:          {serper_searches}")
        print(f"   Fixed by enrichment (no AI):   {len(newly_passed)}")
        print(f"   Still need AI after enrichment: {len(enriched_ai)}")

        locally_fixed.extend(newly_passed)
        needs_ai = enriched_ai
    elif needs_ai and enrich and not os.environ.get("SERPER_API_KEY"):
        print(f"\n   Skipping Serper enrichment (SERPER_API_KEY not set)")

    # ── Phase 2: Batched AI audit ────────────────────────────────────────────
    kept = list(locally_fixed)
    fixed_list: List[Dict] = []
    discarded = list(local_discarded)
    errors: List[Dict] = []

    if needs_ai and not dry_run:
        # Resolve API key
        env_key_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "grok": "GROK_API_KEY",
        }
        key = api_key or os.environ.get(env_key_map.get(provider, ""), "")
        if not key:
            print(f"   {env_key_map.get(provider, 'API_KEY')} not set.")
            sys.exit(1)

        try:
            provider_call = _create_provider(provider, model, key)
        except ImportError as e:
            print(f"   Missing package for {provider}: {e}")
            sys.exit(1)

        print(f"\n--- Phase 2: AI audit ({provider}/{model}, batch_size={batch_size}) ---")

        def _process_batch(batch, label_offset=0):
            """Process a batch of leads. Returns list of failed (lead, issues) for retry."""
            user_msg = _build_batch_user_message(batch)
            failed = []

            try:
                raw_response = provider_call(BATCH_SYSTEM_PROMPT, user_msg)
                results = _parse_llm_response(raw_response)
            except json.JSONDecodeError as e:
                print(f"      JSON parse failed: {e}")
                return batch  # return all for retry
            except Exception as e:
                print(f"      API error: {e}")
                time.sleep(2)
                return batch  # return all for retry

            for i, (lead, _) in enumerate(batch):
                if i >= len(results):
                    failed.append(batch[i])
                    continue

                result = results[i]
                action = result.get("__action__", "keep")
                reason = result.get("__reason__", "")

                if action == "discard":
                    print(f"      [{label_offset + i + 1}] DISCARD: {reason}")
                    discarded.append({"original": lead, "reason": reason})
                else:
                    result.pop("__action__", None)
                    result.pop("__reason__", None)

                    # Merge: keep original fields where LLM returned empty
                    merged = deepcopy(lead)
                    for k, v in result.items():
                        if v is not None and v != "" and v != [] and v != {}:
                            merged[k] = v
                    merged["socials"] = _clean_socials(merged.get("socials", {}))

                    ok, post_issues = _post_validate(merged)
                    if not ok:
                        print(f"      [{label_offset + i + 1}] post-fix issues: {post_issues}")

                    if action == "fix":
                        print(f"      [{label_offset + i + 1}] FIXED: {reason}")
                        fixed_list.append(merged)
                    else:
                        kept.append(merged)

            return failed

        # Process in batches with retry on failure (smaller sub-batches)
        total_batches = (len(needs_ai) + batch_size - 1) // batch_size
        for batch_idx in range(0, len(needs_ai), batch_size):
            batch = needs_ai[batch_idx:batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1
            print(f"\n   Batch {batch_num}/{total_batches}  ({len(batch)} leads)")

            failed = _process_batch(batch, label_offset=batch_idx)

            # Retry failed leads in smaller sub-batches (5 at a time)
            if failed:
                retry_size = max(3, batch_size // 3)
                print(f"      Retrying {len(failed)} failed leads in sub-batches of {retry_size}...")
                time.sleep(0.5)
                still_failed = []
                for sub_idx in range(0, len(failed), retry_size):
                    sub_batch = failed[sub_idx:sub_idx + retry_size]
                    sub_failed = _process_batch(sub_batch, label_offset=batch_idx + sub_idx)
                    still_failed.extend(sub_failed)
                    if delay_between_calls > 0:
                        time.sleep(delay_between_calls)

                # Anything still failing after retry goes to errors
                for lead, issues in still_failed:
                    errors.append({"lead": lead, "error": f"failed after retry: {issues}"})

            if delay_between_calls > 0:
                time.sleep(delay_between_calls)

    elif needs_ai and dry_run:
        print(f"\n--- Phase 2: SKIPPED (dry-run) ---")
        for lead, issues in needs_ai:
            ok, reason = _post_validate(lead)
            if ok:
                kept.append(lead)
            else:
                discarded.append({"original": lead, "reason": f"dry-run: {reason}"})

    # ── Summary ──────────────────────────────────────────────────────────────
    good_leads = kept + fixed_list
    print(f"\n{'='*60}")
    print(f"  RESULTS  (provider={provider}, model={model})")
    print(f"{'='*60}")
    print(f"  Total unique leads processed : {len(all_leads)}")
    print(f"  Passed local (no API call)   : {len(locally_fixed)}")
    print(f"  Kept (after AI)              : {len(kept) - len(locally_fixed)}")
    print(f"  Fixed by AI                  : {len(fixed_list)}")
    print(f"  Discarded                    : {len(discarded)}")
    print(f"  Errors                       : {len(errors)}")
    print(f"  Good leads (kept + fixed)    : {len(good_leads)}")
    api_calls = 0 if dry_run else ((len(needs_ai) + batch_size - 1) // batch_size if needs_ai else 0)
    print(f"  API calls made               : {api_calls}")
    print(f"  Serper searches              : {serper_searches}")
    print(f"{'='*60}\n")

    # ── Write output ─────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    out_master = {
        "sessions": master.get("sessions", []),
        "all_leads": good_leads,
        "total_leads_ever": len(good_leads),
        "last_updated": now.isoformat(),
        "fix_stats": {
            "run_at": now.isoformat(),
            "provider": provider,
            "model": model,
            "dry_run": dry_run,
            "input_leads": len(all_leads),
            "duplicates_removed": dup_count,
            "locally_passed": len(locally_fixed),
            "ai_reviewed": len(needs_ai),
            "api_calls": api_calls,
            "serper_searches": serper_searches,
            "kept": len(kept),
            "fixed": len(fixed_list),
            "discarded": len(discarded),
            "errors": len(errors),
        },
        "discarded": discarded,
        "errors": [e["error"] for e in errors],
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out_master, f, indent=2, ensure_ascii=False)
    print(f"Fixed leads saved -> {output_path}")

    csv_path = output_path.replace(".json", ".csv")
    _write_csv(good_leads, csv_path)
    print(f"CSV export      -> {csv_path}")


def _write_csv(leads: List[Dict], path: str) -> None:
    import csv
    if not leads:
        return
    fieldnames = [
        "business", "full_name", "first", "last", "email", "role",
        "linkedin", "website", "company_linkedin", "industry", "sub_industry",
        "country", "state", "city", "hq_country", "hq_state", "hq_city",
        "employee_count", "phone_numbers", "description", "source_url", "source_type",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        rows = []
        for lead in leads:
            row = dict(lead)
            row["phone_numbers"] = "; ".join(lead.get("phone_numbers") or [])
            rows.append(row)
        writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix/audit leads — cost-optimized with free & cheap provider support"
    )
    parser.add_argument(
        "--input", "-i",
        default=os.path.join(os.path.dirname(__file__), "leads_output", "all_leads.json"),
    )
    parser.add_argument(
        "--output", "-o",
        default=os.path.join(os.path.dirname(__file__), "leads_output", "all_leads_fixed.json"),
    )
    parser.add_argument(
        "--provider", "-p",
        default="anthropic",
        choices=["anthropic", "groq", "gemini", "grok"],
        help="LLM provider (default: anthropic). groq & gemini have FREE tiers.",
    )
    parser.add_argument(
        "--model", "-m",
        default="",
        help="Model override. Defaults: anthropic=haiku, groq=llama-3.3-70b, gemini=gemini-2.0-flash, grok=grok-3-mini-fast",
    )
    parser.add_argument(
        "--api-key", "-k",
        default="",
        help="API key (or set env var: ANTHROPIC_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, GROK_API_KEY)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip AI calls, only apply local fixes",
    )
    parser.add_argument(
        "--max-leads", "-n",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=15,
        help="Leads per API call (default: 15). Higher = cheaper but may hit token limits.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds between API calls (default: 0.3)",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip Serper.dev web search enrichment step",
    )

    args = parser.parse_args()

    fix_leads(
        input_path=args.input,
        output_path=args.output,
        provider=args.provider,
        model=args.model,
        api_key=args.api_key or None,
        dry_run=args.dry_run,
        max_leads=args.max_leads,
        batch_size=args.batch_size,
        delay_between_calls=args.delay,
        enrich=not args.no_enrich,
    )
