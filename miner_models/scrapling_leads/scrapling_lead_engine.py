"""
Scrapling-Powered Lead Generation Engine  (FIXED v2)
======================================================

A FREE drop-in replacement for the expensive Firecrawl + Google Search API.

  OLD (paid)  →  GSE API ($) + Firecrawl ($) + OpenRouter ($)
  NEW (free)  →  DuckDuckGo (free) + Scrapling (free) + regex

Install deps:
    pip install scrapling duckduckgo-search

Usage:
    from scrapling_leads import get_leads
    leads = asyncio.run(get_leads(5, industry="Software", region="United States"))

FIX LOG (v2):
  BUG-01  Fetcher.get() → Fetcher().get()  (class vs instance method)
  BUG-02  `from ddgs import DDGS` → `from duckduckgo_search import DDGS`
  BUG-03  page.html_content → str(page.html)  (correct Scrapling attr)
  BUG-04  _build_email_candidates() defined but never called → wired into
          _find_email_for_person() as fallback guessing
  BUG-05  _seen_companies blocked ALL contacts from same company → removed;
          now only emails are deduped (1 email per run)
  BUG-06  _extract_person_cards: `page` arg accepted but CSS never used →
          now uses page.css() for structured card extraction + text regex fallback
  BUG-07  phone_numbers always [] → _scrape_company_info now extracts phones
          and _assemble_lead wires them in
  BUG-08  socials always {} → Facebook/Twitter/Instagram/YouTube extracted
          alongside LinkedIn and passed through _assemble_lead
  BUG-09  _normalize_employee_count: "501-1,000".split("-")[1] → "1,000" OK
          but "1,001-5,000".split("-") gives 3 parts → fixed with rsplit
  BUG-10  US state hard-fail removed → warning only; state="" is allowed
  BUG-11  asyncio.sleep inside executor-run sync function → moved delay to
          async layer; sync functions are now fully blocking-safe
  BUG-12  _extract_location_from_text returned plain city/state/country but
          _scrape_company_info expected hq_ keys too → now mirrors correctly
  BUG-13  save_lead_immediately called before dedup check → moved AFTER dedup
  BUG-14  DDGS API returns dicts in newer versions → normalised with .get()
  BUG-15  stealthy_headers not in all Scrapling builds → wrapped in try/except
  BUG-16  page.css("title")[0].text → safe with guard + .text attribute
  BUG-17  meta description: .attrib vs .attributes → unified via _safe_attrib()
  BUG-18  _find_email_for_person never guessed patterns → now calls
          _build_email_candidates() and returns best guess
  BUG-19  Sub-pages fetched even on 404 → status check added
  BUG-20  asyncio.get_event_loop() deprecated in 3.10+ → asyncio.get_running_loop()
"""

import asyncio
import csv
import json
import logging
import os
import re
import socket
import sys
import time
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Ensure local Scrapling package is importable ─────────────────────────────
# The repo ships Scrapling as a subdirectory: <project_root>/Scrapling/scrapling/
# We need <project_root>/Scrapling on sys.path so `import scrapling` works.
_SCRAPLING_PKG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'Scrapling'
)
_SCRAPLING_PKG_DIR = os.path.normpath(_SCRAPLING_PKG_DIR)
if _SCRAPLING_PKG_DIR not in sys.path:
    sys.path.insert(0, _SCRAPLING_PKG_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
# US/CA format + international format with country code
PHONE_REGEX = re.compile(
    r'(\+?1?\s?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})'   # US: (512) 823-0256
    r'|(\+\d{1,3}[\s.\-]?\d{1,4}[\s.\-]?\d{3,4}[\s.\-]?\d{3,4})'  # Intl: +61 7 3000 1234
)

LINKEDIN_PROFILE_RE  = re.compile(r'https?://(?:www\.)?linkedin\.com/in/([\w\-_%]+)/?')
LINKEDIN_COMPANY_RE  = re.compile(r'https?://(?:www\.)?linkedin\.com/company/([\w\-_%]+)/?')
FACEBOOK_RE          = re.compile(r'https?://(?:www\.)?facebook\.com/([\w.\-/]+)/?')
TWITTER_RE           = re.compile(r'https?://(?:www\.)?(?:twitter|x)\.com/([\w\-]+)/?')
INSTAGRAM_RE         = re.compile(r'https?://(?:www\.)?instagram\.com/([\w.\-]+)/?')
YOUTUBE_RE           = re.compile(r'https?://(?:www\.)?youtube\.com/(?:c/|@|channel/)?([\w\-]+)/?')
GITHUB_RE            = re.compile(r'https?://(?:www\.)?github\.com/([\w\-]+)/?')
TIKTOK_RE            = re.compile(r'https?://(?:www\.)?tiktok\.com/@([\w.\-]+)/?')

SKIP_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "proton.me", "mailinator.com",
    "temp-mail.org", "guerrillamail.com", "10minutemail.com", "yopmail.com",
    "sharklasers.com", "throwaway.email",
}

GENERIC_PREFIXES = {
    "info", "hello", "contact", "support", "admin", "team", "sales",
    "marketing", "noreply", "no-reply", "help", "service", "office",
    "mail", "webmaster", "enquiries", "enquiry", "general", "press",
    "media", "hr", "jobs", "careers", "billing", "invoice", "legal",
    "privacy", "security", "abuse", "postmaster", "bounce",
    # Dotted department prefixes
    "accounts.receivable", "accounts.payable", "accounts",
    "customer.service", "customer.support", "tech.support",
}

# Also check if any part of a dotted local matches a generic prefix
GENERIC_PREFIX_PARTS = {
    "noreply", "no-reply", "postmaster", "webmaster", "bounce",
    "accounts", "billing", "invoice", "abuse",
}

VALID_EMPLOYEE_RANGES = [
    "0-1", "2-10", "11-50", "51-200", "201-500",
    "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+"
]

EMPLOYEE_PATTERNS = [
    re.compile(r'\b(\d{1,3}(?:,\d{3})*)\+?\s*(?:employees?|staff|people|team members?|workers?)\b', re.I),
    re.compile(r'\b(?:employees?|staff|team):\s*(\d[\d,\s\-\+k]+)', re.I),
    re.compile(r'\bteam of (\d[\d,\s\-\+k]+)', re.I),
]

# Tiered page list — fetch in priority order, stop when we have what we need
TIER1_PATHS = ["/contact", "/contact-us", "/about", "/about-us"]
TIER2_PATHS = ["/team", "/our-team", "/team-members", "/leadership", "/people",
               "/management", "/staff", "/meet-the-team", "/company", "/who-we-are",
               "/founders", "/executives", "/board", "/our-story"]
# Tier 3 (/blog, /pricing, /docs, /careers) — never fetched

NAV_TITLE_WORDS = {
    "home", "welcome", "index", "homepage", "main", "front page",
    "page", "untitled", "default", "new", "start",
    # Page titles that get scraped as business names
    "about", "about us", "what we do", "who we are", "our story",
    "careers", "contact us", "contact", "overview", "leadership",
    "public company", "official site", "official website",
    # Navigation / section titles
    "team", "our team", "meet the team", "meet our team", "the team",
    "staff", "our staff", "management", "management team",
    "services", "our services", "products", "our products",
    "blog", "news", "press", "press releases", "media",
    "careers", "jobs", "open positions", "work with us", "join us",
    "privacy policy", "terms of service", "terms", "terms of use",
    "faq", "help", "support", "help center", "support center",
    "resources", "downloads", "documentation", "docs",
    "login", "sign in", "sign up", "register", "dashboard",
    "loading", "please wait", "redirecting", "error", "not found",
    "404", "403", "500",
    # Role/section words that appear as page titles but aren't company names
    "executive", "executive team", "executive leadership",
    "founders", "advisors", "board", "board of directors",
    "investors", "portfolio", "clients", "testimonials",
}

NAV_WORDS = {
    "home", "about", "contact", "team", "staff", "people", "crew",
    "services", "solutions", "products", "portfolio", "work", "projects",
    "case", "studies", "blog", "news", "insights", "resources",
    "careers", "jobs", "leadership", "management", "overview",
    "our", "the", "meet", "join", "search", "view", "see", "read",
    "learn", "click", "here", "more", "info", "get", "find", "now",
    "welcome", "main", "menu", "navigation", "partners", "clients",
    "privacy", "policy", "terms", "legal", "sitemap", "copyright",
    "roles", "subsectors", "functions", "professionals", "executive",
    "up", "for", "and", "or", "of", "in", "on", "by", "to", "a",
    "upcoming", "see", "all", "none", "select", "expand", "collapse",
}

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

ROLE_NORMALIZE = {
    "ceo": "CEO", "chief executive officer": "CEO",
    "cto": "CTO", "chief technology officer": "CTO",
    "cfo": "CFO", "chief financial officer": "CFO",
    "coo": "COO", "chief operating officer": "COO",
    "cmo": "CMO", "chief marketing officer": "CMO",
    "cpo": "CPO", "chief product officer": "CPO",
    "president": "President",
    "founder": "Founder",
    "co-founder": "Co-Founder", "cofounder": "Co-Founder",
    "co founder": "Co-Founder",
    "director": "Director",
    "vp": "VP", "vice president": "Vice President",
    "svp": "SVP", "senior vice president": "SVP",
    "evp": "EVP", "executive vice president": "EVP",
    "head of": "Head of",
    "partner": "Partner", "managing partner": "Managing Partner",
    "general partner": "General Partner",
    "principal": "Principal",
    "owner": "Owner", "co-owner": "Co-Owner",
    "manager": "Manager",
    "managing director": "Managing Director",
}

INDUSTRY_QUERIES: Dict[str, List[str]] = {
    "Software":           ['software company "about us" CEO founder',
                           'software development firm "our team" leadership',
                           'custom software company founder contact'],
    "SaaS":               ['SaaS company "about us" CEO founder',
                           'SaaS startup "our team" leadership',
                           'cloud SaaS platform founder CEO'],
    "Technology":         ['technology company "about us" CEO founder',
                           'tech firm "our team" leadership contact',
                           'IT company founder CEO team'],
    "Financial Services": ['financial services firm "about us" CEO founder',
                           'fintech company "our team" leadership',
                           'investment firm founder managing director'],
    "Health Care":        ['healthcare company "about us" CEO founder',
                           'health tech startup "our team" leadership',
                           'medical device company founder CEO'],
    "Consulting":         ['consulting firm "about us" partner founder',
                           'management consulting "our team" leadership',
                           'strategy consulting firm founder managing partner'],
    "Real Estate":        ['real estate company "about us" CEO owner',
                           'commercial real estate firm "our team" founder',
                           'property management company CEO team'],
    "Marketing":          ['marketing agency "about us" CEO founder',
                           'digital marketing firm "our team" leadership',
                           'creative agency founder CEO team'],
    "Education":          ['education company "about us" CEO founder',
                           'edtech startup "our team" leadership',
                           'online learning platform founder CEO'],
    "Manufacturing":      ['manufacturing company "about us" CEO president',
                           'industrial company "our team" founder leadership',
                           'factory automation company CEO team'],
    "Advertising":        ['advertising agency "about us" CEO founder',
                           'creative ad agency "our team" leadership',
                           'media buying agency founder CEO'],
    "E-Commerce":         ['ecommerce company "about us" CEO founder',
                           'online retail startup "our team" leadership',
                           'DTC brand founder CEO team'],
    "Biotechnology":      ['biotech company "about us" CEO founder',
                           'life sciences startup "our team" leadership',
                           'pharmaceutical company founder CEO'],
    "Artificial Intelligence": ['AI company "about us" CEO founder',
                                'machine learning startup "our team" leadership',
                                'AI platform company founder CEO'],
    "Cybersecurity":      ['cybersecurity company "about us" CEO founder',
                           'infosec firm "our team" leadership',
                           'security platform startup founder CEO'],
    "Energy":             ['energy company "about us" CEO founder',
                           'renewable energy startup "our team" leadership',
                           'cleantech company founder CEO'],
    "Legal":              ['law firm "about us" managing partner founder',
                           'legal services firm "our team" leadership',
                           'legal tech company founder CEO'],
    "Insurance":          ['insurance company "about us" CEO founder',
                           'insurtech startup "our team" leadership',
                           'insurance agency founder CEO'],
    "Hospitality":        ['hospitality company "about us" CEO founder',
                           'hotel management company "our team" leadership',
                           'restaurant group founder CEO'],
    "Logistics":          ['logistics company "about us" CEO founder',
                           'supply chain company "our team" leadership',
                           'freight company founder CEO'],
    "Construction":       ['construction company "about us" CEO founder',
                           'general contractor "our team" leadership',
                           'building company founder president'],
    "Staffing":           ['staffing agency "about us" CEO founder',
                           'recruiting firm "our team" leadership',
                           'talent agency founder managing director'],
    "Telecommunications": ['telecom company "about us" CEO founder',
                           'communications company "our team" leadership',
                           'ISP company founder CEO'],
    "Automotive":         ['automotive company "about us" CEO founder',
                           'auto tech startup "our team" leadership',
                           'car dealership group founder CEO'],
    "Agriculture":        ['agriculture company "about us" CEO founder',
                           'agtech startup "our team" leadership',
                           'farming technology company founder CEO'],
    "Gaming":             ['gaming company "about us" CEO founder',
                           'game studio "our team" leadership',
                           'esports company founder CEO'],
    "Food and Beverage":  ['food company "about us" CEO founder',
                           'beverage brand "our team" leadership',
                           'food tech startup founder CEO'],
    "Accounting":         ['accounting firm "about us" managing partner',
                           'CPA firm "our team" leadership',
                           'bookkeeping company founder CEO'],
    "Architecture":       ['architecture firm "about us" principal founder',
                           'design firm "our team" leadership',
                           'architecture studio founder director'],
    "Media":              ['media company "about us" CEO founder',
                           'digital media startup "our team" leadership',
                           'publishing company founder CEO'],
}

DEFAULT_QUERIES = [
    '"{industry} company" "about us" CEO founder',
    '"{industry} firm" "our team" leadership founder',
    '"{industry}" startup founder CEO team',
]

INDUSTRY_MAP: Dict[str, Tuple[str, str]] = {
    # ── Software & Technology ───────────────────────────────────────────
    "software":                ("Software",                        "Enterprise Software"),
    "saas":                    ("Software",                        "SaaS"),
    "technology":              ("Information Technology",          "Information Technology"),
    "tech":                    ("Information Technology",          "Information Technology"),
    "it":                      ("Information Technology",          "Information Services"),
    "ai":                      ("Artificial Intelligence",         "Artificial Intelligence"),
    "artificial intelligence": ("Artificial Intelligence",         "Machine Learning"),
    "machine learning":        ("Artificial Intelligence",         "Machine Learning"),
    "cloud":                   ("Software",                        "Cloud Computing"),
    "data":                    ("Data and Analytics",              "Analytics"),
    "analytics":               ("Data and Analytics",              "Analytics"),
    "cybersecurity":           ("Privacy and Security",            "Cyber Security"),
    "security":                ("Privacy and Security",            "Security"),
    "blockchain":              ("Blockchain and Cryptocurrency",   "Blockchain"),
    "crypto":                  ("Blockchain and Cryptocurrency",   "Cryptocurrency"),
    # ── Finance & Insurance ─────────────────────────────────────────────
    "fintech":                 ("Financial Services",              "FinTech"),
    "finance":                 ("Financial Services",              "Finance"),
    "financial":               ("Financial Services",              "Financial Services"),
    "financial services":      ("Financial Services",              "Financial Services"),
    "insurance":               ("Financial Services",              "Insurance"),
    "accounting":              ("Financial Services",              "Accounting"),
    # ── Health & Biotech ────────────────────────────────────────────────
    "healthcare":              ("Health Care",                     "Health Care"),
    "health":                  ("Health Care",                     "Health Care"),
    "health care":             ("Health Care",                     "Health Care"),
    "biotech":                 ("Biotechnology",                   "Biotechnology"),
    "biotechnology":           ("Biotechnology",                   "Biotechnology"),
    # ── Professional Services ───────────────────────────────────────────
    "consulting":              ("Professional Services",           "Consulting"),
    "legal":                   ("Professional Services",           "Legal"),
    "law":                     ("Professional Services",           "Legal"),
    "staffing":                ("Professional Services",           "Recruiting"),
    "recruiting":              ("Professional Services",           "Recruiting"),
    # ── Real Estate & Construction ──────────────────────────────────────
    "real estate":             ("Real Estate",                     "Commercial Real Estate"),
    "construction":            ("Real Estate",                     "Construction"),
    "architecture":            ("Real Estate",                     "Architecture"),
    # ── Sales & Marketing ───────────────────────────────────────────────
    "marketing":               ("Sales and Marketing",             "Marketing"),
    "advertising":             ("Advertising",                     "Advertising"),
    # ── Education ───────────────────────────────────────────────────────
    "education":               ("Education",                       "Education"),
    "edtech":                  ("Education",                       "EdTech"),
    # ── Commerce ────────────────────────────────────────────────────────
    "ecommerce":               ("Commerce and Shopping",           "E-Commerce"),
    "e-commerce":              ("Commerce and Shopping",           "E-Commerce"),
    # ── Manufacturing & Industrial ──────────────────────────────────────
    "manufacturing":           ("Manufacturing",                   "Industrial Manufacturing"),
    # ── Transportation & Logistics ──────────────────────────────────────
    "logistics":               ("Transportation",                  "Logistics"),
    "transportation":          ("Transportation",                  "Automotive"),
    "automotive":              ("Transportation",                  "Automotive"),
    # ── Energy & Sustainability ─────────────────────────────────────────
    "energy":                  ("Energy",                          "Energy"),
    # ── Media & Entertainment ───────────────────────────────────────────
    "media":                   ("Media and Entertainment",         "Media and Entertainment"),
    "gaming":                  ("Gaming",                          "Gaming"),
    # ── Travel & Hospitality ────────────────────────────────────────────
    "hospitality":             ("Travel and Tourism",              "Hospitality"),
    "hotel":                   ("Travel and Tourism",              "Hotel"),
    # ── Food & Beverage ─────────────────────────────────────────────────
    "food":                    ("Food and Beverage",               "Food and Beverage"),
    "food and beverage":       ("Food and Beverage",               "Food and Beverage"),
    "restaurant":              ("Food and Beverage",               "Restaurants"),
    # ── Agriculture ─────────────────────────────────────────────────────
    "agriculture":             ("Agriculture and Farming",         "Agriculture"),
    "farming":                 ("Agriculture and Farming",         "Farming"),
    "agtech":                  ("Agriculture and Farming",         "AgTech"),
    # ── Telecom ─────────────────────────────────────────────────────────
    "telecommunications":      ("Messaging and Telecommunications", "Telecommunications"),
    "telecom":                 ("Messaging and Telecommunications", "Telecommunications"),
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_get_domain(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lstrip("www.")
    except Exception:
        return ""


# FIX-17: unified attribute accessor for Scrapling elements
def _safe_attrib(el: Any, key: str, default: str = "") -> str:
    """Get an HTML attribute safely from a Scrapling element."""
    try:
        # Scrapling ≥ 0.2 uses .attrib (dict-like)
        if hasattr(el, "attrib"):
            return el.attrib.get(key, default)
        # Older builds use .attributes
        if hasattr(el, "attributes"):
            return el.attributes.get(key, default)
    except Exception:
        pass
    return default


def _is_business_email(email: str) -> bool:
    email = email.lower().strip()
    parts = email.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if domain in SKIP_EMAIL_DOMAINS:
        return False
    if local in GENERIC_PREFIXES:
        return False
    # Check dotted parts (e.g. "accounts.receivable" → "accounts" is generic)
    local_parts = re.split(r'[._\-]', local)
    if any(p in GENERIC_PREFIX_PARTS for p in local_parts):
        return False
    if len(local) < 2 or local.isdigit():
        return False
    return True


def _extract_emails(text: str) -> List[str]:
    return [e for e in EMAIL_REGEX.findall(text) if _is_business_email(e)]


# FIX-07: extract phones from text and normalise to E.164
def _normalize_phone(phone: str) -> str:
    """Normalize a phone number to E.164 format."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"          # US/CA without country code
    elif len(digits) == 11 and digits[0] == "1":
        return f"+{digits}"           # US/CA with country code
    elif len(digits) >= 7 and phone.strip().startswith("+"):
        return f"+{digits}"           # International with +
    elif len(digits) >= 10 and len(digits) <= 15:
        return f"+{digits}"           # Long enough to be international
    return ""


def _extract_phones(text: str) -> List[str]:
    raw = PHONE_REGEX.findall(text)
    seen: set = set()
    result: List[str] = []
    for match in raw:
        # PHONE_REGEX has 2 groups — pick whichever matched
        p = match[0] if isinstance(match, tuple) and match[0] else match
        if isinstance(match, tuple) and not p:
            p = match[1] if len(match) > 1 else ""
        if not p:
            continue
        normalised = _normalize_phone(p)
        if normalised and normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    return result


# FIX-08: extract all social links from HTML text
def _extract_socials(html: str) -> Dict[str, str]:
    socials: Dict[str, str] = {}
    co_matches = LINKEDIN_COMPANY_RE.findall(html)
    if co_matches:
        socials["linkedin"] = f"https://www.linkedin.com/company/{co_matches[0]}"
    fb = FACEBOOK_RE.findall(html)
    if fb:
        socials["facebook"] = f"https://www.facebook.com/{fb[0]}"
    tw = TWITTER_RE.findall(html)
    if tw:
        socials["twitter"] = f"https://x.com/{tw[0]}"
    ig = INSTAGRAM_RE.findall(html)
    if ig:
        socials["instagram"] = f"https://www.instagram.com/{ig[0]}"
    yt = YOUTUBE_RE.findall(html)
    if yt:
        _yt_skip = {"watch", "embed", "iframe_api", "playlist", "shorts", "channel",
                     "user", "results", "feed", "premium", "music", "kids", "tv"}
        valid_yt = [y for y in yt if y.lower() not in _yt_skip and len(y) > 2]
        if valid_yt:
            socials["youtube"] = f"https://www.youtube.com/@{valid_yt[0]}"
    gh = GITHUB_RE.findall(html)
    if gh:
        _gh_skip = {"topics", "explore", "features", "pricing", "login", "signup",
                     "marketplace", "sponsors", "settings", "notifications", "new",
                     "orgs", "collections", "trending", "events", "about", "security"}
        valid_gh = [g for g in gh if g.lower() not in _gh_skip and len(g) > 1]
        if valid_gh:
            socials["github"] = f"https://github.com/{valid_gh[0]}"
    tt = TIKTOK_RE.findall(html)
    if tt:
        socials["tiktok"] = f"https://www.tiktok.com/@{tt[0]}"
    return socials


# FIX-09: robust employee range normaliser
def _normalize_employee_count(raw: str) -> str:
    """Map a raw employee count string to a valid range.

    Handles: "51-200", "200", "1,001-5,000", "5k", "~300 employees"
    FIX: use rsplit("-", 1) so "1,001-5,000" splits into ["1,001", "5,000"]
    """
    try:
        lo_raw = raw.rsplit("-", 1)[0]
        lo_raw = lo_raw.replace(",", "").replace("+", "").replace("k", "000").replace("K", "000").strip()
        n = int(re.sub(r"[^\d]", "", lo_raw))
    except Exception:
        return ""
    for rng in VALID_EMPLOYEE_RANGES:
        if "+" in rng:
            lo = int(rng.replace("+", "").replace(",", ""))
            if n >= lo:
                return rng
        else:
            lo_s, hi_s = rng.split("-")
            lo = int(lo_s.replace(",", ""))
            hi = int(hi_s.replace(",", ""))
            if lo <= n <= hi:
                return rng
    return ""


def _guess_employee_count_from_text(text: str) -> str:
    for pat in EMPLOYEE_PATTERNS:
        m = pat.search(text)
        if m:
            val = _normalize_employee_count(m.group(1))
            if val:
                return val
    return ""


def _scrapling_linkedin_company(company_linkedin_url: str) -> Dict[str, Any]:
    """
    Scrape LinkedIn public company page using Scrapling Fetcher (no login needed).
    Returns employee_count, city, state, country, description.
    This avoids DDG queries for employee count + HQ location.
    """
    result: Dict[str, Any] = {}
    if not company_linkedin_url:
        return result
    try:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(company_linkedin_url, impersonate='chrome',
                           stealthy_headers=True, follow_redirects=True, timeout=15)
        if not page:
            return result

        text = page.get_all_text(separator='\n') if page else ""
        if len(text) < 200:
            return result

        # Employee count: "201-500 employees" or "View all 363 employees"
        emp_m = re.search(r'(\d{1,3}(?:,\d{3})*(?:\s*[-–]\s*\d{1,3}(?:,\d{3})*)?)\s*employees?', text, re.I)
        if emp_m:
            result["employee_count"] = _normalize_employee_count(emp_m.group(1))

        # HQ location: "Headquarters\nSouth Brisbane, Queensland"
        hq_m = re.search(r'Headquarters?\s*\n\s*([A-Z][^\n]{3,50})', text)
        if hq_m:
            hq_text = hq_m.group(1).strip()
            parts = [p.strip() for p in hq_text.split(",")]
            if len(parts) >= 2:
                result["city"] = parts[0]
                if len(parts) >= 3:
                    result["state"] = parts[1]
                    result["country"] = parts[2] if len(parts) > 3 else parts[2]
                else:
                    result["state"] = parts[1]

        # Company size range directly: "Company size\n201-500 employees"
        size_m = re.search(r'Company size\s*\n\s*(\d[\d,\-–\s]*\d)\s*employees?', text, re.I)
        if size_m and not result.get("employee_count"):
            result["employee_count"] = _normalize_employee_count(size_m.group(1))

        # Industry: "Industry\nComputer Software" or "Industry\nInformation Technology"
        industry_m = re.search(r'Industr(?:y|ies)\s*\n\s*([A-Z][^\n]{3,60})', text)
        if industry_m:
            li_industry = industry_m.group(1).strip()
            # Filter out garbage (navigation text, etc.)
            if len(li_industry) <= 50 and not li_industry.startswith(('Sign', 'Log', 'Join')):
                result["industry"] = li_industry

        # Founded year
        founded_m = re.search(r'[Ff]ounded\s+(?:in\s+)?(\d{4})', text)
        if founded_m:
            result["founded"] = founded_m.group(1)

        # Description
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        for line in lines:
            if len(line) > 100 and not line.startswith(('Sign', 'Log', 'Join', 'Skip', 'Cookie')):
                result["description"] = line[:500]
                break

        logger.info(f"LinkedIn company scrape: {result}")
    except Exception as e:
        logger.debug(f"LinkedIn company scrape failed: {e}")

    return result


def _search_employee_count_ddg(business: str, domain: str) -> str:
    """
    Step 8: Search DDG for employee count from LinkedIn/Crunchbase/ZoomInfo.
    Returns normalized range like "201-500" or "".
    """
    employee_re = re.compile(
        r'(\d{1,3}(?:,\d{3})*(?:\s*-\s*\d{1,3}(?:,\d{3})*)?)\s*'
        r'(?:employees?|staff|people|team members?|workers?)',
        re.I
    )
    # LinkedIn "Company size" format: "201-500 employees"
    size_re = re.compile(
        r'(?:company size|size|employees?)[:\s]*'
        r'(\d{1,3}(?:,\d{3})*(?:\s*[-–]\s*\d{1,3}(?:,\d{3})*)?(?:\+)?)',
        re.I
    )

    queries = [
        f'"{business}" employees company size site:linkedin.com',
        f'"{business}" "{domain}" employees number',
    ]

    for query in queries:
        results = _search_ddg(query, max_results=3, retries=1)
        for r in results:
            combined = f"{r.get('title', '')} {r.get('body', '') or ''}"

            # Try LinkedIn size format first
            sm = size_re.search(combined)
            if sm:
                val = _normalize_employee_count(sm.group(1))
                if val:
                    return val

            # Try general employee count
            em = employee_re.search(combined)
            if em:
                val = _normalize_employee_count(em.group(1))
                if val:
                    return val

        time.sleep(random.uniform(0.5, 1.0))

    return ""


def _split_name(full_name: str) -> Tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


_NON_PERSON_WORDS = {
    # Business/marketing jargon that gets picked up as "names"
    "company", "data", "software", "business", "digital", "creative",
    "global", "group", "enterprise", "solutions", "technology", "consulting",
    "capital", "ventures", "labs", "studio", "agency", "network", "systems",
    "insightful", "prospect", "overcoming", "challenges", "faster", "higher",
    "better", "premium", "ultimate", "leading", "innovative", "advanced",
    "custom", "smart", "cloud", "cyber", "open", "free", "full", "best",
    "high", "low", "new", "big", "top", "pro", "super", "mega", "core",
    "trusted", "verified", "provider", "platform", "market", "growth",
    "revenue", "sales", "success", "impact", "strategy", "vision", "power",
    "mobile", "web", "online", "virtual", "modern", "future", "next",
    "with", "your", "from", "that", "this", "have", "they", "will",
    "email", "phone", "chat", "call", "list", "sign", "free", "demo",
    # Words that appear in department names / generic roles
    "inquiries", "inquiry", "relations", "department", "division",
    "coordinator", "specialist", "representative", "associate",
    "general", "corporate", "international", "regional", "national",
    # Institution / entity words — prevent "Clayton State University",
    # "National Foundation", "Inc Corp" etc from being treated as person names
    "university", "college", "school", "institute", "academy",
    "foundation", "association", "organization", "corporation",
    "incorporated", "limited", "llc", "inc", "ltd", "gmbh", "pvt",
    "department", "ministry", "bureau", "authority", "commission",
    "committee", "council", "board", "trust", "charity", "hospital",
    "clinic", "church", "parish", "mosque", "temple", "synagogue",
    # HTML/image artifact words scraped as names
    "logo", "icon", "image", "photo", "avatar", "banner", "thumbnail",
    "header", "footer", "sidebar", "widget", "button", "link", "menu",
    # Page section headings scraped alongside names
    "funding", "investors", "backed", "portfolio", "series",
    "pricing", "features", "testimonials", "reviews", "download",
    "subscribe", "newsletter", "blog", "article", "podcast",
    "event", "webinar", "conference", "summit", "award",
}


def _is_valid_person_name(name: str) -> bool:
    parts = name.strip().split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    for word in parts:
        if not (2 <= len(word) <= 30):
            return False
        if not word[0].isupper():
            return False
        if any(c.isdigit() for c in word):
            return False
        if word.lower() in NAV_WORDS:
            return False
        if word.lower() in _NON_PERSON_WORDS:
            return False
    return True


def _normalize_role(role: str) -> str:
    if not role:
        return role
    key = role.strip().lower()
    if key in ROLE_NORMALIZE:
        return ROLE_NORMALIZE[key]
    # Replace each known role keyword with its proper form
    result = role.strip()
    for k, v in ROLE_NORMALIZE.items():
        pattern = re.compile(r'\b' + re.escape(k) + r'\b', re.IGNORECASE)
        result = pattern.sub(v, result)
    # Clean up "And" → "&" for compound roles
    result = re.sub(r'\s+And\s+', ' & ', result)
    result = re.sub(r'\s+and\s+', ' & ', result)
    return result


def _build_email_candidates(first: str, last: str, domain: str) -> List[str]:
    """Generate common business email patterns for a name + domain."""
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f or not l:
        return []
    return [
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f[0]}.{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f}@{domain}",
        f"{l}.{f}@{domain}",
        f"{l}@{domain}",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — EMAIL PATTERN VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def _verify_mx(domain: str) -> bool:
    """Check if domain has valid MX records (i.e. can receive email)."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except ImportError:
        # dnspython not installed — fall back to socket
        try:
            socket.getaddrinfo(f"mail.{domain}", 25)
            return True
        except socket.gaierror:
            # Try the domain itself (some use A record for mail)
            try:
                socket.getaddrinfo(domain, 25)
                return True
            except socket.gaierror:
                return False
    except Exception:
        return False


# In-memory cache: domain → (is_catchall: bool, mx_host: str)
_smtp_cache: Dict[str, Tuple[bool, str]] = {}


def _get_mx_host(domain: str) -> str:
    """Return the primary MX host for a domain, or "" if none found."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        if answers:
            # Pick lowest-priority (highest preference) MX
            best = min(answers, key=lambda r: r.preference)
            return str(best.exchange).rstrip(".")
        return ""
    except Exception:
        return ""


def _smtp_verify_email(email: str, timeout: int = 5) -> Tuple[bool, str]:
    """
    Free SMTP-level email verification.

    Connects to the domain's MX server and checks if the mailbox exists
    using RCPT TO probe. No email is actually sent.

    Returns (is_valid, reason):
      (True, "mailbox exists")         — RCPT TO accepted (250)
      (True, "catch-all server")       — server accepts everything, can't verify
      (False, "mailbox not found")     — RCPT TO rejected (550/551/553)
      (False, "mx not found")          — no MX records
      (False, "smtp error: ...")       — connection/timeout/etc
    """
    if not email or "@" not in email:
        return False, "invalid email"

    domain = email.split("@")[1].lower()

    # Check cache for MX host + catch-all status
    if domain in _smtp_cache:
        is_catchall, mx_host = _smtp_cache[domain]
        if is_catchall:
            return True, "catch-all server"
        if not mx_host:
            return False, "mx not found (cached)"
    else:
        mx_host = _get_mx_host(domain)
        if not mx_host:
            # Fallback: try the domain itself as MX
            mx_host = domain
        _smtp_cache[domain] = (False, mx_host)

    try:
        import smtplib
        smtp = smtplib.SMTP(timeout=timeout)
        smtp.connect(mx_host, 25)
        smtp.helo("verify.leadpoet.com")

        # Some servers require MAIL FROM before RCPT TO
        smtp.mail("verify@leadpoet.com")

        # Step 1: Check if server is catch-all (accepts random addresses)
        import uuid
        random_email = f"xyztest{uuid.uuid4().hex[:8]}@{domain}"
        catchall_code, _ = smtp.rcpt(random_email)
        if catchall_code == 250:
            # Server accepts everything — can't distinguish real from fake
            _smtp_cache[domain] = (True, mx_host)
            smtp.quit()
            return True, "catch-all server"

        # Step 2: Check the actual email
        code, message = smtp.rcpt(email)
        smtp.quit()

        if code == 250:
            return True, "mailbox exists"
        elif code in (550, 551, 552, 553, 554):
            return False, f"mailbox not found (SMTP {code})"
        else:
            # Ambiguous response (e.g. 450 greylisting) — give benefit of doubt
            return True, f"smtp {code} (ambiguous, allowing)"

    except smtplib.SMTPServerDisconnected:
        return True, "server disconnected (allowing)"
    except smtplib.SMTPConnectError:
        return True, "smtp connect failed (allowing)"
    except socket.timeout:
        return True, "smtp timeout (allowing)"
    except Exception as e:
        # On any error, don't reject — SMTP probing is best-effort
        return True, f"smtp error: {e}"


def _detect_pattern_from_existing(existing_emails: List[str], domain: str) -> str:
    """
    Cross-check existing emails found on the site to detect the company's
    email pattern. Returns the pattern name or "" if can't determine.

    Pattern names:
      "first.last"   → john.doe@domain
      "firstlast"    → johndoe@domain
      "f.last"       → j.doe@domain
      "flast"        → jdoe@domain
      "first"        → john@domain
      "last.first"   → doe.john@domain
    """
    domain_emails = [e for e in existing_emails if e.split("@")[1].lower() == domain.lower()]
    if not domain_emails:
        return ""

    for email in domain_emails:
        local = email.split("@")[0].lower()
        if "." in local:
            parts = local.split(".")
            if len(parts) == 2:
                a, b = parts
                if len(a) == 1:
                    return "f.last"
                elif len(a) > 1 and len(b) > 1:
                    return "first.last"
        elif len(local) > 3:
            # Could be "firstlast" or "flast"
            return "firstlast"

    return ""


def _query_hunter_pattern(domain: str) -> str:
    """
    Query Hunter.io free API for the domain's email pattern.
    Free tier: 25 searches/month (no API key needed for pattern lookup).
    Returns pattern like "first.last" or "" on failure.
    """
    try:
        import urllib.request
        # Hunter.io domain-search — returns the email pattern for a domain
        auto_url = f"https://api.hunter.io/v2/domain-search?domain={domain}"
        req = urllib.request.Request(auto_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            pattern = data.get("data", {}).get("pattern", "")
            if pattern:
                # Hunter returns patterns like "{first}.{last}" → normalize
                return pattern.replace("{", "").replace("}", "")
    except Exception:
        pass
    return ""


def _verify_email_pattern(
    first: str, last: str, domain: str, existing_emails: List[str]
) -> str:
    """
    Step 4: Verify and pick the best email for a person.

    1. Check MX records (domain can receive mail?)
    2. Cross-check existing emails on site → detect pattern
    3. Try Hunter.io free API → get domain pattern
    4. Apply detected pattern to generate the email

    Returns the best-guess email or "" if domain has no MX.
    """
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f or not l:
        return ""

    # Step 4.1: MX record check — if no mail server, skip entirely
    if not _verify_mx(domain):
        logger.info(f"No MX records for {domain} — skipping email generation")
        return ""

    # Step 4.2: Cross-check existing emails found on the site
    detected = _detect_pattern_from_existing(existing_emails, domain)

    # Step 4.3: Hunter.io pattern lookup (if cross-check didn't work)
    if not detected:
        detected = _query_hunter_pattern(domain)

    # Step 4.4: Apply the detected pattern (or default to first.last)
    pattern_map = {
        "first.last":  f"{f}.{l}@{domain}",
        "firstlast":   f"{f}{l}@{domain}",
        "f.last":      f"{f[0]}.{l}@{domain}",
        "flast":       f"{f[0]}{l}@{domain}",
        "first":       f"{f}@{domain}",
        "last.first":  f"{l}.{f}@{domain}",
        "last":        f"{l}@{domain}",
    }

    if detected in pattern_map:
        email = pattern_map[detected]
        logger.info(f"Email pattern '{detected}' → {email}")
        return email

    # Default: first.last (most common business pattern)
    email = f"{f}.{l}@{domain}"
    logger.info(f"Email pattern default → {email}")
    return email


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — INDUSTRY CORRECTION
# ─────────────────────────────────────────────────────────────────────────────

# Keywords in h1/h2/meta that signal a specific industry
_INDUSTRY_SIGNALS: Dict[str, List[str]] = {
    "software":              ["software", "saas", "devops", "developer tools", "api platform",
                              "continuous delivery", "continuous deployment", "ci/cd",
                              "deploy", "developer experience", "sdk"],
    "artificial intelligence": ["artificial intelligence", "machine learning", "ai platform",
                               "deep learning", "nlp", "computer vision", "generative ai"],
    "fintech":               ["fintech", "payments", "banking", "financial technology",
                              "lending", "neobank", "insurtech", "regtech"],
    "healthcare":            ["healthcare", "health tech", "healthtech", "medical device",
                              "clinical", "patient", "telehealth", "digital health"],
    "cybersecurity":         ["cybersecurity", "cyber security", "infosec",
                              "threat detection", "security platform", "zero trust"],
    "ecommerce":             ["ecommerce", "e-commerce", "online store", "marketplace",
                              "shopify", "retail tech", "dtc"],
    "edtech":                ["edtech", "education technology", "learning platform",
                              "online learning", "lms", "courseware"],
    "real estate":           ["real estate", "property", "proptech", "realty",
                              "brokerage", "mortgage"],
    "marketing":             ["marketing", "advertising", "ad tech", "martech",
                              "seo", "digital marketing", "content marketing"],
    "consulting":            ["consulting", "advisory", "professional services",
                              "management consulting", "strategy consulting"],
    "manufacturing":         ["manufacturing", "industrial", "factory", "production",
                              "supply chain", "logistics"],
    "biotech":               ["biotech", "biotechnology", "pharmaceutical", "pharma",
                              "drug discovery", "genomics", "life sciences"],
    "data":                  ["data analytics", "big data", "data platform",
                              "business intelligence", "data science", "analytics"],
    "cloud":                 ["cloud computing", "cloud infrastructure", "iaas",
                              "paas", "cloud native", "serverless"],
    "blockchain":            ["blockchain", "cryptocurrency", "crypto", "web3",
                              "defi", "nft", "decentralized"],
    "media":                 ["media", "entertainment", "streaming", "content platform",
                              "publishing", "podcast", "video platform"],
    "energy":                ["energy", "renewable", "solar", "cleantech", "climate tech",
                              "sustainability", "green energy"],
}


def _detect_industry_from_website(description: str, page_headings: List[str]) -> str:
    """
    Scan website description + h1/h2 headings for industry signals.
    Returns the best-matching industry key or "".
    """
    text = (description + " " + " ".join(page_headings)).lower()
    best_key = ""
    best_score = 0
    for industry_key, signals in _INDUSTRY_SIGNALS.items():
        score = sum(1 for s in signals if s in text)
        if score > best_score:
            best_score = score
            best_key = industry_key
    return best_key if best_score > 0 else ""


def _detect_industry_from_ddg(business: str, domain: str) -> str:
    """
    Search DDG for industry info from LinkedIn/Crunchbase.
    Priority: LinkedIn company page > Crunchbase categories.
    Returns industry key or "".
    """
    queries = [
        f'"{business}" site:linkedin.com/company',
        f'"{business}" "{domain}" industry OR category site:crunchbase.com',
    ]

    for query in queries:
        results = _search_ddg(query, max_results=3)
        if not results:
            time.sleep(0.3)
            continue

        for r in results:
            combined = f"{r.get('title', '')} {r.get('body', '') or ''}".lower()
            # Check all industry signals against the result text
            best_key = ""
            best_score = 0
            for industry_key, signals in _INDUSTRY_SIGNALS.items():
                score = sum(1 for s in signals if s in combined)
                if score > best_score:
                    best_score = score
                    best_key = industry_key
            if best_key:
                return best_key

        time.sleep(0.3)

    return ""


# LinkedIn uses its own industry names. Map them to our gateway taxonomy.
# Source: LinkedIn's ~150 industry categories → our 50 parent + 725 sub-industries
_LINKEDIN_INDUSTRY_MAP: Dict[str, Tuple[str, str]] = {
    # Software & Tech
    "computer software":                      ("Software",                  "Enterprise Software"),
    "information technology and services":     ("Information Technology",    "Information Services"),
    "information technology & services":       ("Information Technology",    "Information Services"),
    "information services":                    ("Information Technology",    "Information Services"),
    "internet":                               ("Software",                  "SaaS"),
    "computer & network security":            ("Privacy and Security",      "Cyber Security"),
    "computer networking":                    ("Information Technology",     "Network Security"),
    "semiconductors":                         ("Hardware",                   "Semiconductor"),
    "telecommunications":                     ("Hardware",                   "Telecommunications"),
    # Finance
    "financial services":                     ("Financial Services",         "Financial Services"),
    "banking":                                ("Financial Services",         "Finance"),
    "insurance":                              ("Financial Services",         "Insurance"),
    "investment management":                  ("Financial Services",         "Asset Management"),
    "venture capital & private equity":       ("Financial Services",         "Venture Capital"),
    "accounting":                             ("Financial Services",         "Accounting"),
    # Health
    "hospital & health care":                 ("Health Care",                "Hospital"),
    "health, wellness and fitness":           ("Health Care",                "Health Care"),
    "medical devices":                        ("Health Care",                "Medical Device"),
    "biotechnology":                          ("Biotechnology",              "Biotechnology"),
    "pharmaceuticals":                        ("Biotechnology",              "Pharmaceutical"),
    # Professional Services
    "management consulting":                  ("Professional Services",      "Consulting"),
    "legal services":                         ("Professional Services",      "Legal"),
    "human resources":                        ("Professional Services",      "Human Resources"),
    "staffing and recruiting":                ("Professional Services",      "Recruiting"),
    # Marketing & Advertising
    "marketing and advertising":              ("Sales and Marketing",        "Marketing"),
    "marketing & advertising":                ("Sales and Marketing",        "Marketing"),
    "public relations and communications":    ("Sales and Marketing",        "Marketing"),
    # Education
    "education management":                   ("Education",                  "Education"),
    "e-learning":                             ("Education",                  "EdTech"),
    "higher education":                       ("Education",                  "Higher Education"),
    # Real Estate & Construction
    "real estate":                            ("Real Estate",                "Commercial Real Estate"),
    "construction":                           ("Real Estate",                "Construction"),
    # Manufacturing
    "manufacturing":                          ("Manufacturing",              "Manufacturing"),
    "automotive":                             ("Transportation",             "Automotive"),
    "machinery":                              ("Manufacturing",              "Industrial Manufacturing"),
    # Energy
    "oil & energy":                           ("Energy",                     "Oil and Gas"),
    "renewables & environment":               ("Energy",                     "Renewable Energy"),
    "utilities":                              ("Energy",                     "Energy"),
    # Media & Entertainment
    "media production":                       ("Media and Entertainment",    "Media and Entertainment"),
    "entertainment":                          ("Media and Entertainment",    "Media and Entertainment"),
    "online media":                           ("Media and Entertainment",    "Digital Media"),
    # Food & Hospitality
    "food & beverages":                       ("Food and Beverage",          "Food and Beverage"),
    "restaurants":                            ("Food and Beverage",          "Restaurants"),
    "hospitality":                            ("Travel and Tourism",         "Hospitality"),
    # Retail & Commerce
    "retail":                                 ("Commerce and Shopping",      "E-Commerce"),
    "consumer goods":                         ("Consumer Goods",             "Consumer Goods"),
    # Government & Nonprofits
    "government administration":              ("Government and Military",    "Government"),
    "nonprofit organization management":      ("Social Impact",              "Non Profit"),
    # Other
    "design":                                 ("Design",                     "Design"),
    "architecture & planning":                ("Real Estate",                "Architecture"),
    "logistics and supply chain":             ("Transportation",             "Logistics"),
    "transportation/trucking/railroad":       ("Transportation",             "Logistics"),
}


def _map_linkedin_industry(li_industry: str) -> Optional[Tuple[str, str]]:
    """
    Map a LinkedIn industry string to our gateway taxonomy (industry, sub_industry).
    Returns None if no mapping found.
    """
    if not li_industry:
        return None
    key = li_industry.strip().lower()
    # Normalize "&" → "and" for matching (LinkedIn uses both)
    key_normalized = key.replace(" & ", " and ")
    # Exact match
    if key in _LINKEDIN_INDUSTRY_MAP:
        return _LINKEDIN_INDUSTRY_MAP[key]
    if key_normalized in _LINKEDIN_INDUSTRY_MAP:
        return _LINKEDIN_INDUSTRY_MAP[key_normalized]
    # Fuzzy match: check if any map key is a substring
    for map_key, pair in _LINKEDIN_INDUSTRY_MAP.items():
        if map_key in key or key in map_key:
            return pair
        mk_norm = map_key.replace(" & ", " and ")
        if mk_norm in key_normalized or key_normalized in mk_norm:
            return pair
    # Try our existing INDUSTRY_MAP as fallback
    if key in INDUSTRY_MAP:
        return INDUSTRY_MAP[key]
    return None


def _correct_industry(
    company_info: Dict[str, Any],
    requested_industry: Optional[str],
) -> Tuple[str, str]:
    """
    Step 5: Correct the industry using multiple sources.
    Priority: LinkedIn company page > Website keyword scan > DDG search > User-requested.

    LinkedIn data is authoritative because the validator (Stage 5) also uses it.
    If the LinkedIn page says "Computer Software", we should claim "Software" too.

    Returns (industry, sub_industry) tuple.
    """
    description = company_info.get("description", "")
    business = company_info.get("business", "")
    domain = company_info.get("domain", "")

    # Source 0 (BEST): LinkedIn company page industry — same data the validator uses
    # This was extracted by _scrapling_linkedin_company() and stored in company_info
    li_industry = company_info.get("linkedin_industry", "")
    if li_industry:
        mapped = _map_linkedin_industry(li_industry)
        if mapped:
            logger.info(f"Industry from LinkedIn: '{li_industry}' → {mapped}")
            return mapped

    # Source 1: Website keyword scan (h1/h2 + description)
    headings = company_info.get("headings", [])
    site_industry = _detect_industry_from_website(description, headings)

    # Source 2: DDG search (LinkedIn/Crunchbase) — only if website has no clear signal
    ddg_industry = ""
    if not site_industry:
        ddg_industry = _detect_industry_from_ddg(business, domain)

    # Pick the best source (site scan > DDG > user-requested)
    detected = site_industry or ddg_industry or ""

    if detected:
        # Map to proper (industry, sub_industry) pair
        if detected in INDUSTRY_MAP:
            result = INDUSTRY_MAP[detected]
            logger.info(f"Industry corrected to: {result[0]} / {result[1]} (source: {'ddg' if ddg_industry else 'site'})")
            return result

    # Fall back to user-requested industry
    return _get_industry_pair(requested_industry)


def _get_industry_pair(industry: Optional[str]) -> Tuple[str, str]:
    if not industry:
        return ("Software", "Enterprise Software")
    key = industry.lower().strip()
    if key in INDUSTRY_MAP:
        return INDUSTRY_MAP[key]
    for k, v in INDUSTRY_MAP.items():
        if k in key or key in k:
            return v
    return ("Software", "Enterprise Software")


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH  —  DuckDuckGo (FIX-02: correct import)
# ─────────────────────────────────────────────────────────────────────────────

# City/region variations per canonical region — randomly sampled each call for query diversity
REGION_VARIATIONS: Dict[str, List[str]] = {
    "United States": [
        "United States", "San Francisco California", "New York City NY",
        "Austin Texas", "Seattle Washington", "Boston Massachusetts",
        "Chicago Illinois", "Los Angeles California", "Miami Florida",
        "Denver Colorado", "Atlanta Georgia", "Dallas Texas",
    ],
    "Dubai UAE": [
        "Dubai UAE", "Dubai", "Abu Dhabi UAE",
        "Sharjah UAE", "United Arab Emirates", "MENA region",
    ],
}

# Generic extra query templates mixed into every industry search
_EXTRA_QUERY_TEMPLATES: List[str] = [
    'inurl:team "{industry}" CEO OR founder',
    '"{industry} company" "meet the team" OR "our leadership" CEO founder',
    '"{industry}" startup "our team" founder CEO email contact',
]



# Negative site filters appended to every query so Serper skips aggregator
# domains and returns actual company websites instead.
_NEGATIVE_SITE_FILTERS = (
    " -site:linkedin.com -site:crunchbase.com -site:glassdoor.com"
    " -site:indeed.com -site:yelp.com -site:facebook.com -site:twitter.com"
    " -site:instagram.com -site:youtube.com -site:reddit.com"
    " -site:wikipedia.org -site:bloomberg.com -site:forbes.com"
    " -site:techcrunch.com -site:g2.com -site:capterra.com"
    " -site:zoominfo.com -site:apollo.io -site:clutch.co"
)


def _build_queries(
    industry: Optional[str],
    region: Optional[str],
    max_queries: int = 12,
) -> List[str]:
    """Build diverse search queries mixing industry templates + city region variations."""
    industry_str = industry or "business"
    # Industry-specific templates (or fallback DEFAULT_QUERIES)
    templates = list(INDUSTRY_QUERIES.get(industry or "", DEFAULT_QUERIES))
    # Append generic extra templates (formatted with industry)
    for t in _EXTRA_QUERY_TEMPLATES:
        rendered = t.format(industry=industry_str)
        if rendered not in templates:
            templates.append(rendered)

    # Resolve city variations for the given region
    region_list: List[str] = [region] if region else [""]
    if region:
        for key, variations in REGION_VARIATIONS.items():
            if key.lower() in region.lower() or region.lower() in key.lower():
                region_list = variations
                break
    # Sample up to 3 distinct city variants for diversity on each call
    selected_regions = random.sample(region_list, min(3, len(region_list)))

    queries: List[str] = []
    for t in templates:
        for reg in selected_regions:
            q = t.format(industry=industry_str)
            if reg:
                q += f" {reg}"
            # Append negative site filters — keeps aggregators out of results
            q += _NEGATIVE_SITE_FILTERS
            if q not in queries:
                queries.append(q)
            if len(queries) >= max_queries:
                return queries
    return queries


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
                # Strip whitespace then strip surrounding double/single quotes
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)

_load_env()


# ── Serper.dev multi-key state ────────────────────────────────────────────────
# Keys are loaded once from SERPER_API_KEYS (comma-separated) or SERPER_API_KEY.
# _serper_key_index tracks which key is currently active. When a key hits its
# quota (HTTP 429 / "credits" exhausted), we advance the index and retry with
# the next key automatically — zero manual intervention needed.

def _load_serper_keys() -> List[str]:
    """Return a deduplicated list of Serper API keys from env vars."""
    # Prefer the comma-separated multi-key var
    multi = os.environ.get("SERPER_API_KEYS", "").strip()
    if multi:
        keys = [k.strip() for k in multi.split(",") if k.strip()]
        if keys:
            return keys
    # Fall back to the single-key var
    single = os.environ.get("SERPER_API_KEY", "").strip()
    return [single] if single else []

_serper_keys: List[str] = []          # populated lazily on first call
_serper_key_index: int  = 0           # index into _serper_keys (module-level)
_exhausted_serper_keys: set = set()   # keys confirmed out-of-credits


def _get_serper_key() -> str:
    """Return the currently active Serper API key (or "" if none available)."""
    global _serper_keys, _serper_key_index
    if not _serper_keys:
        _serper_keys = _load_serper_keys()
    if not _serper_keys:
        return ""
    _serper_key_index = _serper_key_index % len(_serper_keys)
    return _serper_keys[_serper_key_index]


def _rotate_serper_key(exhausted_key: str, mark_exhausted: bool = False) -> str:
    """
    Mark the current key as quota-exhausted and rotate to the next one.
    Returns the new key, or "" if all keys are exhausted.
    """
    global _serper_keys, _serper_key_index, _exhausted_serper_keys
    if not _serper_keys:
        _serper_keys = _load_serper_keys()
    if mark_exhausted and exhausted_key:
        _exhausted_serper_keys.add(exhausted_key)
    if not _serper_keys:
        return ""
    current_idx = _serper_key_index % len(_serper_keys)
    # Only rotate if the exhausted key is still the active one
    if _serper_keys[current_idx] == exhausted_key:
        _serper_key_index = (current_idx + 1) % len(_serper_keys)
        logger.warning(
            f"Serper key ...{exhausted_key[-6:]} quota exceeded — "
            f"rotating to key {_serper_key_index + 1}/{len(_serper_keys)}"
        )
    new_key = _serper_keys[_serper_key_index % len(_serper_keys)]
    # If we've wrapped all the way around to the same key, all are exhausted
    if new_key == exhausted_key and len(_serper_keys) > 1:
        logger.error("All Serper API keys exhausted — falling back to DDG")
        return ""
    return new_key


def _search_serper(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Search Google via Serper.dev API (primary engine — no rate limits, no IP bans).
    Supports multiple API keys: if the active key hits its monthly quota (HTTP 429
    or a credits-exhausted error), the next key in SERPER_API_KEYS is used
    automatically. Returns list of {href, title, body}.
    """
    import urllib.request
    import urllib.error

    api_key = _get_serper_key()
    if not api_key:
        return []

    # Try each available key at most once per call
    keys_tried: set = set()

    while api_key and api_key not in keys_tried:
        keys_tried.add(api_key)
        try:
            payload = json.dumps({"q": query, "num": max_results})
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=payload.encode("utf-8"),
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            results: List[Dict[str, Any]] = []
            for item in data.get("organic", []):
                results.append({
                    "href":  item.get("link", ""),
                    "title": item.get("title", ""),
                    "body":  item.get("snippet", ""),
                })
            if results:
                logger.debug(f"Serper key ...{api_key[-6:]} → {len(results)} results")
            return results[:max_results]

        except urllib.error.HTTPError as e:
            # 429 = quota exceeded; 403 = invalid/suspended key
            if e.code in (429, 403):
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                logger.warning(
                    f"Serper key ...{api_key[-6:]} HTTP {e.code}: {body[:120]}"
                )
                # Rotate to next key and retry
                api_key = _rotate_serper_key(api_key)
                continue
            elif e.code == 400:
                # 400 can mean "Not enough credits" — treat as quota-exhausted
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if "credits" in body.lower() or "quota" in body.lower():
                    logger.warning(
                        f"Serper key ...{api_key[-6:]} HTTP 400 credits exhausted: {body[:120]}"
                    )
                    api_key = _rotate_serper_key(api_key, mark_exhausted=True)
                    continue
                else:
                    logger.debug(f"Serper.dev HTTP 400 for query: {query[:60]}")
                    return []
            else:
                logger.debug(f"Serper.dev HTTP {e.code} for query: {query[:60]}")
                return []

        except Exception as e:
            logger.debug(f"Serper.dev search failed: {e}")
            return []

    # All keys tried and exhausted
    return []


# ── Google 429 global backoff state ──────────────────────────────────────────
# When Google returns a 429 (rate-limit / CAPTCHA), we back off for an
# exponentially growing cooldown period rather than hammering it further.
_google_backoff_until: float = 0.0          # epoch seconds; 0 = no backoff
_google_backoff_seconds: float = 30.0       # initial backoff duration
_GOOGLE_BACKOFF_MAX: float = 300.0          # never back off more than 5 minutes


def _google_is_backed_off() -> bool:
    """Return True if we are still in the Google backoff window."""
    return time.monotonic() < _google_backoff_until


def _trigger_google_backoff() -> None:
    """Record a 429 hit and extend the Google backoff window exponentially."""
    global _google_backoff_until, _google_backoff_seconds
    _google_backoff_seconds = min(_google_backoff_seconds * 2, _GOOGLE_BACKOFF_MAX)
    _google_backoff_until = time.monotonic() + _google_backoff_seconds
    logger.warning(
        f"Google 429 — backing off for {_google_backoff_seconds:.0f}s "
        f"(until +{_google_backoff_seconds:.0f}s from now)"
    )


def _reset_google_backoff() -> None:
    """Successful Google response — reset backoff to initial value."""
    global _google_backoff_seconds
    _google_backoff_seconds = 30.0


def _scrapling_search_google(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Search Google using Scrapling Fetcher with Chrome impersonation.
    Parses the HTML SERP page to extract URLs, titles, and snippets.

    429 / sorry-page handling:
      - Detects Google's rate-limit redirect (/sorry/index) by URL and HTTP status.
      - On 429: triggers exponential backoff (30 s → 60 s → … → 300 s).
      - While backed off: returns [] immediately without making a request.
      - On success: resets backoff timer.
    """
    from urllib.parse import quote_plus

    # Fast-path: if we're still in the backoff window, skip Google entirely
    if _google_is_backed_off():
        remaining = _google_backoff_until - time.monotonic()
        logger.debug(f"Google backoff active — skipping ({remaining:.0f}s left)")
        return []

    try:
        from scrapling.fetchers import Fetcher
        url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}&hl=en"
        page = Fetcher.get(url, impersonate='chrome', stealthy_headers=True,
                           follow_redirects=True, timeout=15)
        if not page:
            return []

        # ── Detect 429 / sorry page ────────────────────────────────────────
        page_status = getattr(page, "status", 200)
        final_url   = str(getattr(page, "url", "") or "")
        if page_status == 429 or "google.com/sorry" in final_url:
            _trigger_google_backoff()
            return []

        html = str(page.html) if hasattr(page, 'html') else ""
        # Secondary check: sorry page sometimes returns 200 but has distinctive content
        if 'sorry/index' in html or 'www.google.com/sorry' in html:
            _trigger_google_backoff()
            return []

        text = page.get_all_text(separator='\n') if page else ""

        # Parse Google SERP: extract href links + surrounding text
        results: List[Dict[str, Any]] = []
        link_re  = re.compile(r'href="(https?://(?!www\.google\.com)[^"]+)"')
        title_re = re.compile(r'<h3[^>]*>([^<]+)</h3>')

        titles = title_re.findall(html)

        seen_urls: set = set()
        for m in link_re.finditer(html):
            href = m.group(1)
            if 'google.com' in href or 'googleapis.com' in href or 'gstatic.com' in href:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            title = ""
            if titles:
                title = titles.pop(0)

            body = ""
            url_domain = _safe_get_domain(href)
            if url_domain:
                for line in text.split('\n'):
                    if url_domain in line.lower() and len(line) > 30:
                        body = line.strip()[:200]
                        break

            results.append({"href": href, "title": title, "body": body})
            if len(results) >= max_results:
                break

        if results:
            _reset_google_backoff()   # Successful SERP — reset backoff timer

        return results
    except Exception as e:
        logger.debug(f"Scrapling Google search failed: {e}")
        return []


def _scrapling_search_ddg(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Search DuckDuckGo HTML version using Scrapling Fetcher with Chrome impersonation.
    """
    from urllib.parse import quote_plus, unquote
    try:
        from scrapling.fetchers import Fetcher
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        page = Fetcher.get(url, impersonate='chrome', stealthy_headers=True, follow_redirects=True, timeout=15)
        if not page:
            return []

        html = str(page.html) if hasattr(page, 'html') else ""

        # DDG HTML results: class="result__a" links with uddg= redirect
        results: List[Dict[str, Any]] = []
        # Parse result links
        result_re = re.compile(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
        )
        snippet_re = re.compile(
            r'class="result__snippet"[^>]*>([^<]+)'
        )

        links = result_re.findall(html)
        snippets = snippet_re.findall(html)

        for i, (href, title) in enumerate(links):
            # Unwrap DDG redirect
            if 'uddg=' in href:
                href = unquote(href.split('uddg=')[1].split('&')[0])
            body = snippets[i].strip() if i < len(snippets) else ""
            results.append({"href": href, "title": title.strip(), "body": body})
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        logger.debug(f"Scrapling DDG search failed: {e}")
        return []


def _search_ddg_raw(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """Search DuckDuckGo via ddgs library (legacy fallback)."""
    try:
        from ddgs import DDGS
        results: List[Dict[str, Any]] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                if isinstance(r, dict):
                    url = r.get("href") or r.get("url") or r.get("link", "")
                    results.append({"href": url, "title": r.get("title", ""),
                                    "body": r.get("body", "") or r.get("snippet", "")})
                else:
                    results.append({"href": getattr(r, "href", ""),
                                    "title": getattr(r, "title", ""),
                                    "body": getattr(r, "body", "")})
        return results
    except Exception as e:
        logger.debug(f"DDG library search failed: {e}")
        return []


def _has_serper_keys() -> bool:
    """Return True if at least one Serper API key is configured."""
    global _serper_keys
    if not _serper_keys:
        _serper_keys = _load_serper_keys()
    return bool(_serper_keys)


def _has_working_serper_keys() -> bool:
    """Return True if at least one Serper key still has credits (not exhausted)."""
    global _serper_keys, _exhausted_serper_keys
    if not _serper_keys:
        _serper_keys = _load_serper_keys()
    working = [k for k in _serper_keys if k not in _exhausted_serper_keys]
    return bool(working)


def _search_ddg(query: str, max_results: int = 15, retries: int = 2) -> List[Dict[str, Any]]:
    """
    Multi-engine search — priority chain:
      1. Serper.dev API (primary — no rate limits, clean JSON, titles+snippets)
      2. DDG library (ddgs) — free fallback
      3. Scrapling StealthyFetcher → Google HTML — last resort
         (skipped entirely when Serper keys are configured, to avoid 429s)
    """
    # Engine 1: Serper.dev API (most reliable, no bans)
    results = _search_serper(query, max_results=max_results)
    if results:
        return results

    # Engine 2: DDG library (free fallback)
    results = _search_ddg_raw(query, max_results=max_results)
    if results:
        return results

    # Engine 3: Scrapling StealthyFetcher → Google HTML (last resort)
    # ⚠️  Always skip direct Google scraping — it triggers 429 CAPTCHA blocks.
    # DDG library (engine 2) is the safe free fallback when Serper credits run out.
    if True:  # permanently disabled to prevent Google 429s
        logger.debug(f"Skipping direct Google scrape (429 risk): {query[:60]}")
        return []

    time.sleep(random.uniform(0.3, 0.6))
    results = _scrapling_search_google(query, max_results=max_results)
    if results:
        return results

    return []


def _filter_urls(results: List[Dict[str, Any]]) -> List[str]:
    skip_domains = {
        "linkedin.com", "facebook.com", "twitter.com", "x.com",
        "instagram.com", "youtube.com", "reddit.com", "wikipedia.org",
        "crunchbase.com", "glassdoor.com", "indeed.com", "yelp.com",
        "amazon.com", "google.com", "bing.com", "yahoo.com",
        "capterra.com", "g2.com", "trustpilot.com", "clutch.co",
        "github.com", "medium.com", "forbes.com", "techcrunch.com",
        "bloomberg.com", "cnbc.com", "zoominfo.com", "apollo.io",
        "rocketreach.co", "hunter.io",
        # Mega-corporations — not realistic B2B leads, waste scraping time
        "meta.com", "apple.com", "microsoft.com", "oracle.com",
        "salesforce.com", "ibm.com", "intel.com", "cisco.com",
        "adobe.com", "nvidia.com", "tesla.com", "uber.com",
        "airbnb.com", "netflix.com", "spotify.com", "snap.com",
        "walmart.com", "target.com", "costco.com",
        # Data vendors / lead list sellers — not real company sites
        "datacaptive.com", "iinfotanks.com", "leadsdatasets.com",
        "readycontacts.com", "mailingdatasolutions.com", "360marco.com",
        "contactout.com", "ceoemail.com", "solutionsuggest.com",
        "ellfound.com", "orkatastartup.com", "orkato.com",
        # News / aggregator / magazine sites
        "beststartup.us", "eu-startups.com", "indianstartuptimes.com",
        "newsweek.com", "theguardian.com", "theregister.com",
        "financialexpress.com", "saastr.com", "startupgrind.com",
        "dermascope.com", "shoutoutatlanta.com", "shoutoutmiami.com",
        "frontlines.io", "qsrmagazine.com", "designrush.com",
        # Template / demo sites
        "demos.famethemes.com", "landio.uicore.co",
        "uicore.co", "famethemes.com",
    }
    seen: set = set()
    urls: List[str] = []
    for r in results:
        url = r.get("href", "")
        if not url:
            continue
        domain = _safe_get_domain(url)
        if any(sd in domain for sd in skip_domains):
            continue
        root = f"https://{domain}"
        if root not in seen:
            seen.add(root)
            urls.append(root)
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING  —  Scrapling Fetcher  (FIX-01, FIX-03, FIX-15, FIX-16, FIX-17, FIX-19)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_page(url: str, timeout: int = 15) -> Optional[Any]:
    """Fetch a page. FIX-01: instantiate Fetcher(). FIX-15: guard stealthy_headers."""
    try:
        from scrapling.fetchers import Fetcher
        fetcher = Fetcher()                         # FIX-01: instance, not class call
        try:
            page = fetcher.get(url, timeout=timeout, stealthy_headers=True)
        except TypeError:
            # FIX-15: older Scrapling builds don't support stealthy_headers
            page = fetcher.get(url, timeout=timeout)
        if page and getattr(page, "status", 200) == 200:
            return page
    except Exception as e:
        logger.debug(f"Fetch failed for {url}: {e}")
    return None


def _page_html(page: Any) -> str:
    """Return raw HTML string from a Scrapling page. FIX-03."""
    # Scrapling exposes HTML via .html (lxml element) — str() serialises it
    # or .content is bytes in some builds
    for attr in ("html", "content", "html_content"):
        val = getattr(page, attr, None)
        if val is None:
            continue
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return str(val)
    return ""


def _page_text(page: Any) -> str:
    """Return all visible text. Falls back to regex-strip if get_all_text unavailable."""
    try:
        return page.get_all_text(separator=" ")
    except Exception:
        raw = _page_html(page)
        return re.sub(r"<[^>]+>", " ", raw)


# Known country names — used to reject country names appearing as "city"
_COUNTRY_NAMES = {
    "united states", "usa", "united kingdom", "uk", "canada", "australia",
    "germany", "france", "india", "singapore", "netherlands", "israel",
    "japan", "china", "brazil", "mexico", "spain", "italy", "sweden",
    "norway", "denmark", "finland", "switzerland", "austria", "belgium",
    "ireland", "new zealand", "south korea", "south africa", "poland",
    "portugal", "czech republic", "romania", "hungary", "turkey",
    "russia", "ukraine", "thailand", "vietnam", "indonesia", "malaysia",
    "philippines", "taiwan", "hong kong", "uae", "saudi arabia",
    "argentina", "chile", "colombia", "peru", "egypt", "nigeria", "kenya",
}

# US state full names → codes for validation
_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


# ─────────────────────────────────────────────────────────────────────────────
# GATEWAY GEO DATABASE  (loaded from gateway/utils/geo_lookup_fast.json)
# ─────────────────────────────────────────────────────────────────────────────
# The gateway validates city+state against this database.  We load the SAME
# data so the scraper can pre-validate before submission.
#
# Structure:
#   _GEO_US_CITIES_BY_STATE = {"california": {"los angeles", "san francisco", ...}, ...}
#   _GEO_CITIES_BY_COUNTRY  = {"united arab emirates": {"dubai", "abu dhabi", ...}, ...}

_GEO_US_CITIES_BY_STATE: Dict[str, set] = {}
_GEO_CITIES_BY_COUNTRY: Dict[str, set] = {}
_GEO_STATE_ABBR: Dict[str, str] = {}    # "ca" → "california"


def _load_geo_database() -> None:
    """Load geo_lookup_fast.json from the gateway directory."""
    global _GEO_US_CITIES_BY_STATE, _GEO_CITIES_BY_COUNTRY, _GEO_STATE_ABBR
    for rel in [
        os.path.join('..', '..', 'gateway', 'utils', 'geo_lookup_fast.json'),
        os.path.join('gateway', 'utils', 'geo_lookup_fast.json'),
    ]:
        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), rel))
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                _GEO_US_CITIES_BY_STATE = {
                    state: set(cities) for state, cities in data.get('us_states', {}).items()
                }
                _GEO_CITIES_BY_COUNTRY = {
                    country: set(cities) for country, cities in data.get('cities', {}).items()
                }
                _GEO_STATE_ABBR = data.get('state_abbr', {})
                total_cities = sum(len(c) for c in _GEO_US_CITIES_BY_STATE.values())
                total_intl = sum(len(c) for c in _GEO_CITIES_BY_COUNTRY.values())
                logger.info(f"Loaded gateway geo database: {total_cities} US cities, "
                            f"{total_intl} international cities from {path}")
                return
            except Exception as e:
                logger.warning(f"Failed to load geo database from {path}: {e}")
    logger.warning("Gateway geo database not found — city validation will use heuristics only")


_load_geo_database()


# Gateway city aliases (from gateway/utils/geo_normalize.py)
_US_CITY_ALIASES = {
    'new york': 'new york city', 'nyc': 'new york city', 'la': 'los angeles',
    'sf': 'san francisco', 'dc': 'washington', 'washington dc': 'washington',
    'philly': 'philadelphia', 'vegas': 'las vegas', 'salt lake': 'salt lake city',
}


def _gateway_city_exists(city: str, state: str, country: str) -> bool:
    """
    Check if city exists in the gateway's geo database.
    Mirrors gateway/utils/geo_normalize.py:validate_location().
    Returns True if valid, False if not (or if geo DB not loaded).
    """
    if not city or not _GEO_US_CITIES_BY_STATE:
        return True  # can't validate, allow through

    city_lower = city.lower().strip().replace('.', '')
    country_lower = country.lower().strip() if country else ""

    if country_lower in ("united states", "usa", "us", "america") or not country_lower:
        # Apply US city aliases
        city_lookup = _US_CITY_ALIASES.get(city_lower, city_lower)
        # Resolve state abbreviation → full name
        state_lower = state.lower().strip().replace('.', '') if state else ""
        state_full = _GEO_STATE_ABBR.get(state_lower, state_lower)
        if not state_full:
            return True  # no state to check against
        state_cities = _GEO_US_CITIES_BY_STATE.get(state_full, set())
        if not state_cities:
            return True  # state not in DB
        return city_lookup in state_cities
    else:
        country_cities = _GEO_CITIES_BY_COUNTRY.get(country_lower, set())
        if not country_cities:
            return True  # country not in DB
        return city_lower in country_cities


# Garbage prefixes to strip from scraped city values.
# These appear when regex captures surrounding text along with the city name.
_GARBAGE_CITY_PREFIXES = re.compile(
    r'^(?:'
    r'(?:th|rd|nd|st)\s+floor\s+'     # "th Floor New York"
    r'|founded\s+'                      # "Founded Salt Lake City"
    r'|locations?\s+'                   # "Locations Boston"
    r'|corporations?\s+'                # "Corporation Boston"
    r'|headquartered\s+(?:in\s+)?'     # "Headquartered in Austin"
    r'|(?:head)?office\s+(?:in\s+)?'   # "Office in Dallas"
    r'|based\s+(?:in\s+)?'             # "Based in Chicago"
    r'|located\s+(?:in\s+)?'           # "Located in Miami"
    r'|offices?\s+(?:in\s+)?'          # "Offices in Seattle"
    r'|main\s+office\s+'               # "Main Office Denver"
    r'|our\s+'                          # "Our Denver"
    r'|the\s+'                          # "The Boston" (only at start)
    r')',
    re.IGNORECASE,
)

# Street/address keywords — if the city value contains these, it's an address not a city
_ADDRESS_KEYWORDS = re.compile(
    r'\b(?:avenue|ave|street|st|road|rd|boulevard|blvd|lane|ln|drive|dr|'
    r'suite|ste|unit|floor|highway|hwy|parkway|pkwy|place|pl|court|ct|'
    r'way|circle|cir|terrace|trail|path|crossing|loop)\b',
    re.IGNORECASE,
)

# Compound city separator: "Seattle in Bellevue" → take the part after "in"
_COMPOUND_CITY_RE = re.compile(r'^(.+?)\s+in\s+(.+)$', re.IGNORECASE)

_GARBAGE_CITY_WORDS = re.compile(
    r"digital park|free zone|business park|tech park|industrial|tower|plaza|"
    r"center\b|centre\b|building|suite\b|"
    r"lives?\s+in|americas$|^africa$|^asia$|^europe$|^middle east$|"
    r"despite|privacy concern|high school|north metro",
    re.IGNORECASE,
)


def _validate_city(city: str, state: str = "", country: str = "") -> str:
    """
    Clean and validate a city value.

    Steps:
      1. Strip whitespace, newlines, punctuation
      2. Strip garbage prefixes (Founded, Corporation, Floor, etc.)
      3. Handle compound patterns ("Seattle in Bellevue" → "Bellevue")
      4. Reject address fragments (contains avenue, street, etc.)
      5. Reject country names, state names, nav words
      6. Reject garbage words (business park, tower, etc.)
      7. Validate against gateway geo database
      8. Return cleaned title-case city or "" if invalid
    """
    if not city:
        return ""

    # Step 1: basic cleaning
    city = re.sub(r'\s*\n\s*', ' ', city).strip()
    city = city.strip(" ,.\-–—:;/")
    if '  ' in city:
        parts = [p.strip() for p in city.split('  ') if p.strip()]
        city = parts[-1] if parts else city

    if not city or len(city) <= 2:
        return ""

    # Step 2: strip garbage prefixes
    city = _GARBAGE_CITY_PREFIXES.sub('', city).strip()
    if not city or len(city) <= 2:
        return ""

    # Step 3: handle compound patterns
    m = _COMPOUND_CITY_RE.match(city)
    if m:
        # "Seattle in Bellevue" → try "Bellevue" first
        candidate = m.group(2).strip()
        if len(candidate) > 2:
            city = candidate

    # Step 4: reject address fragments
    if _ADDRESS_KEYWORDS.search(city):
        # Try to extract city after the address part
        # "Delaware Avenue Pennington" → try words after address keyword
        parts = _ADDRESS_KEYWORDS.split(city)
        if len(parts) > 1:
            # Take the last non-empty part
            tail = parts[-1].strip()
            if tail and len(tail) > 2 and not _ADDRESS_KEYWORDS.search(tail):
                city = tail
            else:
                return ""
        else:
            return ""

    # Step 5: reject country names, state names, nav words
    city_lower = city.lower().strip()
    if city_lower in _COUNTRY_NAMES:
        return ""
    if city_lower in _US_STATE_NAMES:
        return ""
    if city_lower in NAV_WORDS or city_lower in _NON_PERSON_WORDS:
        return ""

    # Step 6: reject business parks, towers, etc.
    if _GARBAGE_CITY_WORDS.search(city):
        _UAE_CITIES = {"dubai", "abu dhabi", "sharjah", "ajman", "al ain", "fujairah"}
        for uc in _UAE_CITIES:
            if uc in city_lower:
                return uc.title()
        return ""

    # Step 7: reject if it contains digits (address fragment that slipped through)
    if re.search(r'\d', city):
        return ""

    # Step 8: reject person names that got scraped as city
    if city_lower in {"robert andtbacka", "ion"}:
        return ""

    # Step 9: title-case and validate against gateway geo database
    city = city.strip().title()

    if state or country:
        if not _gateway_city_exists(city, state, country):
            logger.debug(f"City '{city}' not in gateway geo DB for state={state}, country={country}")
            return ""

    return city


def _validate_state(state: str, country: str) -> str:
    """Validate state: must be 2-letter US state code if country is US."""
    if not state:
        return ""
    state = state.strip()
    # Strip newlines
    state = re.sub(r'\s*\n\s*', ' ', state).strip()
    state_upper = state.upper()
    if country == "United States" or not country:
        # Already a valid 2-letter code
        if state_upper in US_STATES:
            return state_upper
        # Try full name → code (case-insensitive)
        code = _US_STATE_NAMES.get(state.lower(), "")
        if code:
            return code
        # Reject invalid US states
        if country == "United States":
            return ""
    # For non-US, accept as-is but clean up
    return state.title() if len(state) > 2 else state_upper


def _extract_location_from_text(text: str) -> Dict[str, str]:
    """
    Step 6: Structured location extraction with validation.
    Handles US addresses, international addresses, and validates garbage.
    """
    result = {
        "city": "", "state": "", "country": "",
        "hq_city": "", "hq_state": "", "hq_country": "",
    }

    # ── Pattern 1: US format — "City, ST" or "City, ST 12345" ──────────────
    us_pat = re.compile(r'\b([A-Z][a-zA-Z\s]{2,25}),\s*([A-Z]{2})\s*(?:\d{5})?\b')
    for m in us_pat.finditer(text):
        city  = m.group(1).strip()
        state = m.group(2).strip()
        if state in US_STATES:
            city = _validate_city(city)
            if city:
                result.update({"city": city, "state": state, "country": "United States",
                               "hq_city": city, "hq_state": state, "hq_country": "United States"})
                return result

    # ── Pattern 2: "City, State/Province, Country" or "City, Country" ──────
    intl_pat = re.compile(
        r'\b([A-Z][a-zA-Z\s]{2,25}),\s*([A-Z][a-zA-Z\s]{2,25})\b'
    )
    country_lookup = {
        "united states": "United States", "usa": "United States", "us": "United States",
        "united kingdom": "United Kingdom", "uk": "United Kingdom",
        "england": "United Kingdom", "scotland": "United Kingdom",
        "canada": "Canada", "australia": "Australia", "germany": "Germany",
        "france": "France", "india": "India", "singapore": "Singapore",
        "netherlands": "Netherlands", "israel": "Israel", "japan": "Japan",
        "brazil": "Brazil", "spain": "Spain", "italy": "Italy",
        "sweden": "Sweden", "switzerland": "Switzerland", "ireland": "Ireland",
        "new zealand": "New Zealand", "south korea": "South Korea",
    }
    for m in intl_pat.finditer(text):
        part1 = m.group(1).strip()
        part2 = m.group(2).strip()
        p2_lower = part2.lower()
        if p2_lower in country_lookup:
            city = _validate_city(part1)
            if city:
                country = country_lookup[p2_lower]
                result.update({"city": city, "country": country,
                               "hq_city": city, "hq_country": country})
                return result

    # ── Pattern 3: Structured address lines (street + city + state + zip) ──
    addr_pat = re.compile(
        r'\d{1,5}\s+[A-Za-z\s]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|'
        r'Drive|Dr|Lane|Ln|Way|Place|Pl|Court|Ct)[.,]?\s*'
        r'(?:Suite|Ste|Floor|Fl|Unit|#)?\s*\d*[.,]?\s*'
        r'([A-Z][a-zA-Z\s]{2,25}),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?',
        re.I
    )
    m = addr_pat.search(text)
    if m:
        city = _validate_city(m.group(1).strip())
        state = m.group(2).strip().upper()
        if city and state in US_STATES:
            result.update({"city": city, "state": state, "country": "United States",
                           "hq_city": city, "hq_state": state, "hq_country": "United States"})
            return result

    # ── Pattern 4: Country-only detection ──────────────────────────────────
    country_patterns = [
        (re.compile(r'\b(United States|USA|U\.S\.A\.?|U\.S\.)\b', re.I), "United States"),
        (re.compile(r'\b(United Kingdom|UK|England|Scotland|Wales)\b', re.I), "United Kingdom"),
        (re.compile(r'\b(Canada)\b', re.I),     "Canada"),
        (re.compile(r'\b(Australia)\b', re.I),  "Australia"),
        (re.compile(r'\b(Germany|Deutschland)\b', re.I), "Germany"),
        (re.compile(r'\b(France)\b', re.I),     "France"),
        (re.compile(r'\b(India)\b', re.I),      "India"),
        (re.compile(r'\b(Singapore)\b', re.I),  "Singapore"),
        (re.compile(r'\b(Netherlands|Holland)\b', re.I), "Netherlands"),
        (re.compile(r'\b(Israel)\b', re.I),     "Israel"),
    ]
    for pat, country_name in country_patterns:
        if pat.search(text):
            result.update({"country": country_name, "hq_country": country_name})
            break
    return result


def _search_hq_location_ddg(business: str, domain: str) -> Dict[str, str]:
    """
    Step 6 fallback: search DDG for company HQ location.
    Sources: LinkedIn company page, Crunchbase, general results.
    """
    result = {"city": "", "state": "", "country": "",
              "hq_city": "", "hq_state": "", "hq_country": ""}

    queries = [
        f'"{business}" headquarters location',
        f'"{business}" "{domain}" office location city',
    ]

    # Regex patterns for location in search results
    # "headquartered in Austin, TX" or "based in San Francisco, California"
    hq_pat = re.compile(
        r'(?:headquartered|based|located|offices?)\s+in\s+'
        r'([A-Z][a-zA-Z\s]{2,25}),\s*([A-Za-z\s]{2,25})', re.I
    )

    for query in queries:
        results = _search_ddg(query, max_results=3, retries=1)
        for r in results:
            combined = f"{r.get('title', '')} {r.get('body', '') or ''}"
            m = hq_pat.search(combined)
            if m:
                city_raw = m.group(1).strip()
                state_or_country = m.group(2).strip()

                city = _validate_city(city_raw)
                if not city:
                    continue

                # Check if part2 is a US state
                state_upper = state_or_country.upper()
                state_lower = state_or_country.lower()
                if state_upper in US_STATES:
                    result.update({"city": city, "state": state_upper,
                                   "country": "United States",
                                   "hq_city": city, "hq_state": state_upper,
                                   "hq_country": "United States"})
                    return result
                elif state_lower in _US_STATE_NAMES:
                    code = _US_STATE_NAMES[state_lower]
                    result.update({"city": city, "state": code,
                                   "country": "United States",
                                   "hq_city": city, "hq_state": code,
                                   "hq_country": "United States"})
                    return result
                else:
                    # International — state_or_country might be country name
                    country_lookup = {
                        "australia": "Australia", "canada": "Canada",
                        "united kingdom": "United Kingdom", "uk": "United Kingdom",
                        "germany": "Germany", "france": "France",
                        "india": "India", "singapore": "Singapore",
                        "israel": "Israel", "japan": "Japan",
                    }
                    country = country_lookup.get(state_lower, state_or_country)
                    result.update({"city": city, "country": country,
                                   "hq_city": city, "hq_country": country})
                    return result

        time.sleep(random.uniform(0.5, 1.0))

    return result


def _extract_with_ai(page_text: str, domain: str) -> List[Dict[str, Any]]:
    """
    Haiku AI fallback: extract C-suite contacts from page text when CSS/regex finds nothing.
    Only called when page has substantial content (>500 chars) but zero contacts found.
    Cost: ~$0.0002 per call (≈500 input + 200 output tokens @ Haiku pricing).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    "Extract people with C-Suite or Founder roles from this text.\n"
                    "Return a JSON array ONLY (no explanation, no markdown):\n"
                    '[{"name":"First Last","role":"CEO","email":""}]\n'
                    "Only include: CEO, Founder, Co-Founder, CTO, President, COO, MD, Director.\n"
                    "For email: only include if it appears EXPLICITLY in the text, else leave empty.\n"
                    f"Company domain (for context): {domain}\n\n"
                    f"Text:\n{page_text[:3000]}"
                ),
            }],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        people = json.loads(raw)
        contacts: List[Dict[str, Any]] = []
        for p in people:
            name  = (p.get("name") or "").strip()
            role  = (p.get("role") or "").strip()
            email = (p.get("email") or "").strip()
            if not _is_valid_person_name(name):
                continue
            if email and not _is_business_email(email):
                email = ""
            first, last = _split_name(name)
            contacts.append({
                "full_name": name,
                "first":     first,
                "last":      last,
                "role":      _normalize_role(role) if role else "",
                "email":     email,
                "linkedin":  "",
            })
        logger.info(f"AI extraction found {len(contacts)} contact(s) for {domain}")
        return contacts
    except Exception as e:
        logger.debug(f"AI extraction failed for {domain}: {e}")
        return []


def _extract_person_linkedin_urls(html: str) -> Dict[str, str]:
    """
    Extract personal LinkedIn /in/ URLs from page HTML.
    Returns {slug: full_url} for each unique profile found.

    Many /team and /about pages link employee names to their LinkedIn profiles:
      <a href="https://linkedin.com/in/john-doe">John Doe</a>
    """
    urls: Dict[str, str] = {}
    for m in LINKEDIN_PROFILE_RE.finditer(html):
        slug = m.group(1).lower().rstrip("/")
        if slug and slug not in urls:
            urls[slug] = _normalize_linkedin_url(m.group(0))
    return urls


def _match_person_to_linkedin(
    name: str, person_linkedin_urls: Dict[str, str]
) -> str:
    """
    Find the LinkedIn /in/ URL whose slug best matches a person name.
    Returns the URL or "" if no match.

    Matching: name words must appear in the slug.
      "John Doe" matches "john-doe-283942" (slug contains john + doe)
      "John Doe" matches "jdoe" (slug contains doe)
    """
    if not name or not person_linkedin_urls:
        return ""
    name_parts = [w.lower() for w in name.split() if len(w) >= 2]
    if not name_parts:
        return ""

    best_url = ""
    best_score = 0
    for slug, url in person_linkedin_urls.items():
        slug_parts = slug.split("-")
        slug_words = [w for w in slug_parts if not w.isdigit() and len(w) >= 2]
        slug_flat = slug.replace("-", "")

        # Word-level match: "john" in ["john", "doe"]
        word_hits = sum(1 for w in name_parts if w in slug_words)
        # Substring match: "doe" in "jdoe"
        substr_hits = sum(1 for w in name_parts if len(w) >= 3 and w in slug_flat)

        score = word_hits * 3 + substr_hits * 2
        if score > best_score:
            best_score = score
            best_url = url

    # Require at least one name part to match
    return best_url if best_score >= 2 else ""


def _extract_person_cards(page: Any, text: str, domain: str) -> List[Dict[str, Any]]:
    """
    Anchor-based extraction: first extract all emails matching the domain,
    then find person names + roles near those emails. Only return contacts
    where we have a REAL email found on the page belonging to the company domain.
    Also extracts personal LinkedIn /in/ URLs from the page HTML when available.
    """
    contacts: List[Dict[str, Any]] = []
    seen_names: set = set()
    role_re = re.compile(
        r'\b(CEO|CTO|CFO|COO|CMO|CPO|President|Founder|Co-Founder|Director|'
        r'VP|Vice President|Head of|Manager|Partner|Principal|Owner|'
        r'Chief Executive|Chief Technology|Chief Financial|Chief Operating|'
        r'Chief Marketing|Chief Product|Managing Director)\b', re.I
    )
    name_re = re.compile(r'\b([A-Z][a-z]+(?:\s[A-Z][\'a-z-]+)+)\b')

    # Extract all personal LinkedIn /in/ URLs from the page HTML
    page_html = _page_html(page) if page else ""
    person_linkedin_urls = _extract_person_linkedin_urls(page_html)

    # ── Step 1: Anchor — collect all on-domain emails ───────────────────────
    all_emails = _extract_emails(text)
    domain_emails = [e for e in all_emails if e.split("@")[1] == domain]

    # ── Step 2: For each domain email, find the nearest person name + role ──
    lines = [l.strip() for l in text.split('\n')]
    full_flat = "\n".join(lines)

    for email in domain_emails:
        if len(contacts) >= 5:
            break
        # Find where this email appears in the text
        email_idx = full_flat.find(email)
        if email_idx < 0:
            continue
        # Get a window of text around the email (±500 chars)
        window_start = max(0, email_idx - 500)
        window_end = min(len(full_flat), email_idx + 500)
        window = full_flat[window_start:window_end]

        # Find person names in the window
        best_name = ""
        best_role = ""
        best_dist = 9999
        for m in name_re.finditer(window):
            name = m.group(0)
            if not _is_valid_person_name(name) or role_re.search(name):
                continue
            dist = abs(m.start() - (email_idx - window_start))
            if dist < best_dist:
                best_name = name
                best_dist = dist

        # Find role in the window
        rm = role_re.search(window)
        if rm:
            best_role = rm.group(0)

        # Also try matching email local part to a name
        if not best_name:
            local = email.split("@")[0].lower()
            for m in name_re.finditer(full_flat):
                name = m.group(0)
                if not _is_valid_person_name(name):
                    continue
                first_l, last_l = _split_name(name)
                if first_l.lower() in local or last_l.lower() in local:
                    best_name = name
                    # Look for role near this name
                    n_idx = m.start()
                    nearby = full_flat[max(0, n_idx - 200):n_idx + 200]
                    rm2 = role_re.search(nearby)
                    if rm2:
                        best_role = rm2.group(0)
                    break

        if best_name and best_name not in seen_names:
            seen_names.add(best_name)
            first, last = _split_name(best_name)
            contacts.append({
                "full_name": best_name,
                "first":     first,
                "last":      last,
                "role":      _normalize_role(best_role) if best_role else "",
                "email":     email,
                "linkedin":  _match_person_to_linkedin(best_name, person_linkedin_urls),
            })

    # ── Step 3: text-proximity fallback (name on line N, role on line N±1) ──
    # Only for contacts with a matching domain email we may have missed above
    if len(contacts) < 5:
        for i, line in enumerate(lines):
            if not line or len(line) > 120:
                continue
            role_match = role_re.search(line)
            if not role_match:
                continue
            context = "\n".join(lines[max(0, i - 2): i + 3])
            for name in name_re.findall(context):
                if role_re.search(name) or not _is_valid_person_name(name):
                    continue
                if name in seen_names:
                    continue
                first, last = _split_name(name)
                # Only add if we can find a matching email on the page
                email = _find_email_for_person(first, last, domain, text)
                if not email:
                    continue
                seen_names.add(name)
                contacts.append({
                    "full_name": name,
                    "first":     first,
                    "last":      last,
                    "role":      _normalize_role(role_match.group(0)),
                    "email":     email,
                    "linkedin":  _match_person_to_linkedin(name, person_linkedin_urls),
                })
                if len(contacts) >= 5:
                    return contacts

    return contacts


def _find_email_for_person(first: str, last: str, domain: str, text: str) -> str:
    """Find an email on the page that matches the person's name. Never fabricate."""
    first_l = first.lower()
    last_l  = last.lower()

    for email in _extract_emails(text):
        local = email.split("@")[0].lower()
        if first_l in local or last_l in local:
            return email

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — DDG/GOOGLE EXECUTIVE SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def _search_executive_ddg(business: str, domain: str, existing_emails: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Search DDG for executive name + role when the website didn't have person data.
    Collects candidates across all results, merges info for the same person,
    then picks the most complete one (has role + linkedin).
    Uses Step 4 email verification (MX + pattern cross-check + Hunter.io).
    Returns: {"full_name", "first", "last", "role", "linkedin", "email"} or None.
    """
    role_re = re.compile(
        r'\b(CEO|CTO|CFO|COO|CMO|CPO|President|Founder|Co-Founder|'
        r'Chief Executive Officer|Chief Technology Officer|Managing Director)\b', re.I
    )
    # Captures full role string like "Founder & CEO" or "Founder & CEO at Company"
    # IMPORTANT: \b word boundaries to avoid matching "cto" inside "Octopus"
    role_full_re = re.compile(
        r'(\b(?:CEO|CTO|CFO|COO|CMO|CPO|President|Founder|Co-Founder|'
        r'Chief Executive Officer|Chief Technology Officer|Managing Director)\b'
        r'(?:\s*[&,]\s*\b(?:CEO|CTO|CFO|COO|CMO|CPO|President|Founder|Co-Founder)\b)*)',
        re.I
    )
    # LinkedIn title: "Name - Role - Company | LinkedIn"
    linkedin_title_re = re.compile(
        r'[\-–—]\s*([^|\-–—]*\b(?:CEO|CTO|CFO|COO|CMO|CPO|President|Founder|'
        r'Co-Founder|Chief|Director|VP|Head)\b[^|\-–—]*)\s*[\-–—|]', re.I
    )
    name_re = re.compile(r'\b([A-Z][a-z]+(?:\s[A-Z][\'a-z-]+)+)\b')
    linkedin_re = re.compile(r'linkedin\.com/in/([\w\-]+)')

    # Collect candidates: {name: {role, linkedin, mentions, source}}
    candidates: Dict[str, Dict[str, Any]] = {}

    # ── Multi-stage query plan ─────────────────────────────────────────────
    # Stage 1 (strict)  : LinkedIn profiles — slug must match name
    # Stage 2 (relaxed) : Crunchbase, Wikipedia, news articles, general web
    # Rationale: many small companies have NO LinkedIn presence; we must fall
    # back to Wikipedia (founders), news/press (CEO named), general about pages.

    stage1_queries = [
        f'"{business}" site:linkedin.com/in CEO OR founder OR director',
        f'"{business}" "{domain}" CEO OR founder site:linkedin.com/in',
    ]
    stage2_queries = [
        f'"{business}" site:crunchbase.com founder CEO people',
        f'"{business}" founder CEO wikipedia',
        f'"{business}" CEO founder president "about us" OR "meet the team"',
        f'"{business}" "{domain}" CEO OR founder OR president',
    ]

    def _process_results(results, strict_linkedin: bool) -> None:
        for r in results:
            title = r.get("title", "")
            body  = r.get("body", "") or ""
            href  = r.get("href", "")

            is_linkedin_profile = "linkedin.com/in/" in href
            is_crunchbase       = "crunchbase.com" in href
            is_wikipedia        = "wikipedia.org" in href
            is_company_page     = "linkedin.com/company/" in href

            # In strict mode, skip non-LinkedIn results
            if strict_linkedin and not (is_linkedin_profile or is_company_page):
                return

            combined  = f"{title} {body}"
            biz_lower = business.lower()

            # Must mention the business somewhere in the result
            if biz_lower not in combined.lower():
                return

            # For LinkedIn profiles in strict mode: company MUST be in title
            if is_linkedin_profile and strict_linkedin:
                if biz_lower not in title.lower():
                    return

            for m in name_re.finditer(combined):
                name = m.group(0)
                if not _is_valid_person_name(name) or role_re.search(name):
                    continue
                if name.lower() == biz_lower:
                    continue
                # Must have a C-suite/founder role in the same snippet
                if not role_re.search(combined):
                    continue

                # LinkedIn slug must match the name (only for /in/ pages)
                if is_linkedin_profile:
                    lm = linkedin_re.search(href)
                    slug = lm.group(1).lower() if lm else ""
                    first_check, last_check = _split_name(name)
                    if slug and not (first_check.lower() in slug or
                                     last_check.lower() in slug):
                        continue

                if name not in candidates:
                    candidates[name] = {"role": "", "linkedin": "", "mentions": 0,
                                        "source": ""}
                candidates[name]["mentions"] += 1
                if not candidates[name]["source"]:
                    candidates[name]["source"] = (
                        "linkedin" if is_linkedin_profile else
                        "crunchbase" if is_crunchbase else
                        "wikipedia" if is_wikipedia else "web"
                    )

                # Role extraction
                if not candidates[name]["role"]:
                    lrm = linkedin_title_re.search(title)
                    if lrm:
                        candidates[name]["role"] = lrm.group(1).strip()
                    else:
                        frm = role_full_re.search(combined)
                        if frm:
                            candidates[name]["role"] = frm.group(0).strip()

                # LinkedIn profile URL
                if not candidates[name]["linkedin"] and is_linkedin_profile:
                    lm = linkedin_re.search(href)
                    if lm:
                        candidates[name]["linkedin"] = (
                            f"https://www.linkedin.com/in/{lm.group(1)}"
                        )

    # ── Stage 1: LinkedIn (strict) ─────────────────────────────────────────
    for query in stage1_queries:
        results = _search_ddg(query, max_results=5)
        if results:
            _process_results(results, strict_linkedin=True)
        time.sleep(random.uniform(0.5, 1.0))

    # ── Stage 2: Crunchbase / Wikipedia / General (relaxed) ───────────────
    # Only run if Stage 1 found nothing useful
    valid_so_far = {n: c for n, c in candidates.items() if c["role"] or c["linkedin"]}
    if not valid_so_far:
        for query in stage2_queries:
            results = _search_ddg(query, max_results=5)
            if results:
                _process_results(results, strict_linkedin=False)
            time.sleep(random.uniform(0.5, 1.0))

    if not candidates:
        return None

    # Pick the best candidate: mentions across queries is the strongest signal
    # A real CEO/founder appears in multiple search results; random people don't
    def _score(info: Dict[str, Any]) -> int:
        s = info["mentions"] * 5  # each mention = 5 points (cross-query validation)
        if info["role"]:
            s += 3
        if info["linkedin"]:
            s += 2
        return s

    # Filter: must have at least role OR linkedin to be considered real
    valid = {n: c for n, c in candidates.items() if c["role"] or c["linkedin"]}
    if not valid:
        return None

    # Log all candidates for debugging
    for name, c in sorted(valid.items(), key=lambda x: _score(x[1]), reverse=True):
        logger.info(f"  Candidate: {name} (mentions={c['mentions']}, role={c['role']}, linkedin={'✓' if c['linkedin'] else '✗'})")

    best_name = max(valid, key=lambda n: _score(valid[n]))
    best = valid[best_name]

    # Clean role — strip company name appended in various formats
    role = best["role"]
    if role:
        # "CEO at Stripe" → "CEO"
        role = re.split(r'\s+(?:at|@)\s+', role, flags=re.I)[0].strip()
        # "President, Starburst" → "President"
        role = re.split(r'\s*,\s+(?=[A-Z])', role)[0].strip()
        # "CEO - Stripe" → "CEO"
        role = re.split(r'\s+[-–—]\s+(?=[A-Z])', role)[0].strip()
        # Strip trailing punctuation
        role = role.rstrip(" ,.@-–—")

    first, last = _split_name(best_name)
    # Step 4: verify email pattern via MX + cross-check + Hunter.io
    best_email = _verify_email_pattern(first, last, domain, existing_emails or [])

    logger.info(f"DDG exec search found: {best_name} ({role}) for {business}")
    return {
        "full_name": best_name,
        "first":     first,
        "last":      last,
        "role":      _normalize_role(role) if role else "",
        "email":     best_email,
        "linkedin":  best["linkedin"],
        "source":    "ddg_search",
    }


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS NAME ↔ DOMAIN / LINKEDIN CROSS-VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
# The gateway validator (Stage 4) checks that the business name matches the
# LinkedIn company page. We replicate that signal upstream by checking:
#   1. Does the business name relate to the website domain?
#   2. Does the business name match the LinkedIn company slug?
# If neither matches, the name is almost certainly garbage (nav text, h1, etc.)
# and the validator WILL reject it — so we reject early to save rate limit.


def _normalize_for_comparison(text: str) -> str:
    """Lowercase, strip punctuation/whitespace, collapse to a comparable key."""
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)     # remove punctuation
    t = re.sub(r'\s+', ' ', t).strip()     # collapse whitespace
    return t


def _biz_matches_domain(biz_name: str, domain: str) -> bool:
    """
    Check if a business name plausibly relates to the domain.

    Examples that pass:
      "Stripe"         + stripe.com         → True
      "HubSpot"        + hubspot.com        → True
      "Palo Alto"      + palo-alto.com      → True
      "Kim Joyce & Associates" + kimjoyceandassociates.com → True

    Examples that fail:
      "Leadership"     + stripe.com         → False
      "Team"           + hubspot.com        → False
      "Please Wait"    + acme.com           → False
    """
    if not biz_name or not domain:
        return False

    # Extract root from domain: "www.palo-alto.com" → "paloalto"
    root = domain.split(".")[0].lower()
    root_clean = re.sub(r'[^a-z0-9]', '', root)     # "palo-alto" → "paloalto"

    # Normalize business name: "Palo Alto Networks" → "paloaltonetworks"
    biz_clean = re.sub(r'[^a-z0-9]', '', biz_name.lower())

    if not root_clean or not biz_clean:
        return False

    # Check 1: domain root appears in business name (most common)
    #   "hubspot" in "hubspot" ✓   "stripe" in "stripe" ✓
    if root_clean in biz_clean:
        return True

    # Check 2: business name appears in domain root
    #   "scw" in "scw" ✓  (SCW.AI → scw.ai)
    if biz_clean in root_clean:
        return True

    # Check 3: word overlap — at least one significant word matches
    #   "Kim Joyce & Associates" + "kimjoyceandassociates.com"
    biz_words = [w for w in re.split(r'[\s&,.\-]+', biz_name.lower()) if len(w) >= 3]
    for word in biz_words:
        word_clean = re.sub(r'[^a-z0-9]', '', word)
        if word_clean and word_clean in root_clean:
            return True

    return False


def _biz_name_from_linkedin_slug(company_linkedin: str) -> str:
    """
    Extract a human-readable company name from a LinkedIn company URL slug.

    "https://www.linkedin.com/company/palo-alto-networks" → "Palo Alto Networks"
    "https://www.linkedin.com/company/stripe"             → "Stripe"
    "https://www.linkedin.com/company/scw-ai"             → "Scw Ai"
    "https://www.linkedin.com/company/792882"             → "" (numeric ID, not a name)
    """
    if not company_linkedin:
        return ""
    m = re.search(r'linkedin\.com/company/([\w\-]+)', company_linkedin, re.I)
    if not m:
        return ""
    slug = m.group(1)
    # Numeric LinkedIn company IDs (e.g. "792882") are not names
    if slug.isdigit():
        return ""
    # "palo-alto-networks" → "Palo Alto Networks"
    return " ".join(w.capitalize() for w in slug.split("-"))


def _biz_matches_linkedin_slug(biz_name: str, company_linkedin: str) -> bool:
    """
    Check if business name matches the LinkedIn company slug.

    The validator does exactly this check — so we mirror it.

    Examples:
      "Stripe"            + linkedin.com/company/stripe            → True
      "Palo Alto Networks" + linkedin.com/company/palo-alto-networks → True
      "Leadership"        + linkedin.com/company/stripe            → False
    """
    if not biz_name or not company_linkedin:
        return False

    slug_name = _biz_name_from_linkedin_slug(company_linkedin)
    if not slug_name:
        # Numeric LinkedIn ID (e.g. /company/792882) — can't verify name from slug.
        # Don't reject; the validator will check against the actual LinkedIn page.
        return True

    biz_lower = _normalize_for_comparison(biz_name)
    slug_lower = _normalize_for_comparison(slug_name)

    if not biz_lower or not slug_lower:
        return False

    # Exact match after normalization
    if biz_lower == slug_lower:
        return True

    # Substring (handles "Inc.", "LLC", etc. suffixes)
    if biz_lower in slug_lower or slug_lower in biz_lower:
        return True

    # Space-stripped comparison: "lucid software" → "lucidsoftware" matches "lucidsoftware"
    # LinkedIn slugs never have spaces; business names always do for multi-word companies
    biz_nospace = biz_lower.replace(" ", "")
    slug_nospace = slug_lower.replace(" ", "")
    if biz_nospace == slug_nospace:
        return True
    if biz_nospace in slug_nospace or slug_nospace in biz_nospace:
        return True

    # Word overlap: at least one significant word from slug appears in biz name
    slug_words = [w for w in slug_lower.split() if len(w) >= 3]
    biz_words = set(biz_lower.split())
    if slug_words and any(w in biz_words for w in slug_words):
        return True

    # Abbreviation check: slug may be initials/shortened form of business words
    # "NBC Los Angeles" → words = ["nbc", "los", "angeles"]
    # slug "nbcla" = "nbc" + "la" (first letters of "los angeles")
    # Check if slug can be built from word prefixes of the business name
    if biz_nospace and slug_nospace and len(slug_nospace) >= 3:
        biz_word_list = biz_lower.split()
        # Strategy 1: slug starts with first word and rest is abbreviation
        if len(biz_word_list) >= 2:
            first_word = re.sub(r'[^a-z0-9]', '', biz_word_list[0])
            if slug_nospace.startswith(first_word) and len(first_word) >= 2:
                remainder = slug_nospace[len(first_word):]
                remaining_words = biz_word_list[1:]
                # Check if remainder matches initials of remaining words
                initials = "".join(w[0] for w in remaining_words if w)
                if remainder == initials:
                    return True
                # Or first 2 letters of each remaining word
                abbrev2 = "".join(w[:2] for w in remaining_words if len(w) >= 2)
                if remainder == abbrev2:
                    return True

        # Strategy 2: slug is pure initials of all words
        if len(biz_word_list) >= 2:
            initials = "".join(w[0] for w in biz_word_list if w)
            if slug_nospace == initials and len(initials) >= 2:
                return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# COMPANY SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_company_info(homepage_url: str) -> Dict[str, Any]:
    """Scrape a company website. FIX-07/08: phones + socials now extracted."""
    info: Dict[str, Any] = {
        "website": homepage_url,
        "domain":  _safe_get_domain(homepage_url),
        "business": "", "description": "",
        "city": "", "state": "", "country": "",
        "hq_city": "", "hq_state": "", "hq_country": "",
        "company_linkedin": "", "employee_count": "",
        "emails": [], "phones": [], "socials": {}, "contacts": [],
        "headings": [],
    }

    # ── Homepage ─────────────────────────────────────────────────────────────
    page = _fetch_page(homepage_url)
    if not page:
        return info

    full_text = _page_text(page)
    html      = _page_html(page)

    # ── Business name extraction ────────────────────────────────────────────
    # Priority (most reliable first):
    #   1. schema.org Organization/LocalBusiness "name"
    #   2. meta og:site_name
    #   3. meta application-name
    #   4. <title> tag — shortest meaningful part
    #   5. h1 — first heading
    #   6. Domain-derived fallback
    # Every candidate is validated via _is_valid_biz_name() before acceptance.

    biz_name = ""
    domain = info["domain"]

    def _domain_fallback() -> str:
        """Derive a business name from the domain (last resort)."""
        if not domain:
            return ""
        base = domain.split(".")[0]
        return " ".join(w.capitalize() for w in re.split(r'[-_]', base))

    def _is_valid_biz_name(name: str) -> bool:
        """Return True if name looks like a real company name, not nav/article text."""
        if not name or len(name) < 2:
            return False
        n = name.strip()
        nl = n.lower()
        # Reject if it's a known nav/page title
        if nl in NAV_TITLE_WORDS:
            return False
        # Reject if > 8 words (likely a tagline or article title)
        if len(n.split()) > 8:
            return False
        # Reject if > 60 chars
        if len(n) > 60:
            return False
        # Reject question marks (article titles: "What Is a Strategy Map?")
        if "?" in n:
            return False
        # Reject patterns that indicate articles, guides, or non-company text
        _GARBAGE_BIZ_RE = re.compile(
            r'^(?:please\s+wait|loading|redirecting|error|not\s+found'
            r'|what\s+is|how\s+to|why\s+|when\s+|where\s+'
            r'|top\s+\d+|best\s+\d+|\d+\s+best|\d+\s+top'
            r'|about\s+|our\s+|what\s+we|who\s+we|careers?\s+at'
            r'|jobs?\s+at|hiring\s+at|shadow\s+it'
            r'|public\s+company|official\s+site|official\s+website'
            r'|solutions?\s+for|products?\s+&|services?\s+-'
            r'|meet\s+the|join\s+the|welcome\s+to'
            r'|subscribe|sign\s+up|log\s*in|cookie'
            r'|complete\s+guide|ultimate\s+guide|definitive\s+guide'
            r'|everything\s+you|step.by.step|comprehensive\s+guide'
            r'|guide\s+to|introduction\s+to|beginner)',
            re.IGNORECASE,
        )
        if _GARBAGE_BIZ_RE.search(nl):
            return False
        # Reject if every word is a nav word (e.g. "Team Leadership")
        words = nl.split()
        if all(w in NAV_WORDS or w in NAV_TITLE_WORDS for w in words):
            return False
        # Reject taglines disguised as names (e.g. "Braze Customer Engagement Platform")
        # Real company names are typically 1-3 words. 4+ words with marketing keywords = tagline.
        _TAGLINE_WORDS = {
            "platform", "solution", "solutions", "engagement", "customer",
            "experience", "intelligence", "management", "automation",
            "optimization", "integration", "analytics", "transformation",
            "infrastructure", "accelerator", "marketplace", "ecosystem",
        }
        if len(words) >= 4 and sum(1 for w in words if w in _TAGLINE_WORDS) >= 2:
            return False
        return True

    # 1. schema.org Organization / LocalBusiness name (most reliable)
    if page:
        try:
            ld_scripts = page.css('script[type="application/ld+json"]')
            for script_el in (ld_scripts or []):
                try:
                    ld_text = script_el.text or ""
                    if not ld_text.strip():
                        continue
                    ld_data = json.loads(ld_text)
                    # Handle @graph arrays
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    if isinstance(ld_data, dict) and "@graph" in ld_data:
                        items = ld_data["@graph"]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("@type", "")
                        if isinstance(item_type, list):
                            item_type = " ".join(item_type)
                        if any(t in item_type for t in ("Organization", "Corporation",
                                "LocalBusiness", "Company", "WebSite")):
                            schema_name = (item.get("name") or "").strip()
                            if schema_name and _is_valid_biz_name(schema_name):
                                biz_name = schema_name
                                break
                    if biz_name:
                        break
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
        except Exception:
            pass

    # 2. og:site_name
    if not biz_name and page:
        try:
            og_els = page.css('meta[property="og:site_name"]')
            if og_els:
                og_name = _safe_attrib(og_els[0], "content").strip()
                if _is_valid_biz_name(og_name):
                    biz_name = og_name
        except Exception:
            pass

    # 3. meta application-name
    if not biz_name and page:
        try:
            app_els = page.css('meta[name="application-name"]')
            if app_els:
                app_name = _safe_attrib(app_els[0], "content").strip()
                if _is_valid_biz_name(app_name):
                    biz_name = app_name
        except Exception:
            pass

    # 4. <title> tag — shortest meaningful part
    if not biz_name:
        title_els = page.css("title") if page else []
        if title_els:
            try:
                raw_title = title_els[0].text or ""
                parts = [p.strip() for p in re.split(r'[\|\-–—•·:,]', raw_title) if p.strip()]
                candidates = [p for p in parts if _is_valid_biz_name(p)]
                if candidates:
                    biz_name = min(candidates, key=len)
                    if len(biz_name) < 3 and len(candidates) > 1:
                        biz_name = sorted(candidates, key=len)[1]
            except Exception:
                pass

    # 5. h1 fallback
    if not biz_name:
        h1_els = page.css("h1") if page else []
        if h1_els:
            try:
                h1_text = h1_els[0].text.strip()[:80]
                if _is_valid_biz_name(h1_text):
                    biz_name = h1_text
            except Exception:
                pass

    # 6. Domain fallback
    if not biz_name:
        biz_name = _domain_fallback()

    # Final validation — if the candidate still looks bad, use domain
    if not _is_valid_biz_name(biz_name):
        biz_name = _domain_fallback()

    info["business"] = biz_name[:80]

    # ── Description: build from multiple sources, ensure ≥ 70 chars ────
    # Gateway requires 70-2000 chars. We try multiple sources in priority
    # order and combine them if needed to reach the minimum.
    desc_candidates: List[str] = []

    # Source 1: meta description (most concise, usually accurate)
    meta_els = page.css('meta[name="description"]') if page else []
    if meta_els:
        meta_desc = _safe_attrib(meta_els[0], "content").strip()
        if meta_desc and len(meta_desc) >= 20:
            desc_candidates.append(meta_desc)

    # Source 2: og:description (social sharing — often richer)
    if page:
        og_desc_els = page.css('meta[property="og:description"]')
        if og_desc_els:
            og_desc = _safe_attrib(og_desc_els[0], "content").strip()
            if og_desc and len(og_desc) >= 20 and og_desc not in desc_candidates:
                desc_candidates.append(og_desc)

    # Source 3: schema.org Organization description
    if page:
        try:
            for script_el in (page.css('script[type="application/ld+json"]') or []):
                try:
                    ld_text = script_el.text or ""
                    if not ld_text.strip():
                        continue
                    ld_data = json.loads(ld_text)
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    if isinstance(ld_data, dict) and "@graph" in ld_data:
                        items = ld_data["@graph"]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        schema_desc = (item.get("description") or "").strip()
                        if schema_desc and len(schema_desc) >= 30 and schema_desc not in desc_candidates:
                            desc_candidates.append(schema_desc)
                            break
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception:
            pass

    # Source 4: first substantial paragraph from page text
    if full_text:
        for paragraph in full_text.split('\n'):
            p = paragraph.strip()
            if len(p) >= 50 and not p.startswith(('<', '{', 'var ', 'function', '//', '#')):
                # Skip nav-like text (all-caps, very short words, or known patterns)
                words = p.split()
                if len(words) >= 8 and p not in desc_candidates:
                    desc_candidates.append(p)
                    break

    # Pick the best description (longest that's ≥ 70 chars)
    desc = ""
    for candidate in desc_candidates:
        clean = candidate.strip()[:500]
        if len(clean) >= 70:
            desc = clean
            break

    # If no single source reaches 70 chars, combine the top two
    if not desc and desc_candidates:
        combined = ". ".join(c.rstrip(".") for c in desc_candidates[:3])
        desc = combined.strip()[:500]

    # Last resort: raw page text
    if not desc or len(desc) < 70:
        fallback = full_text[:500].strip()
        # Clean up: take the first few sentences instead of raw text
        sentences = re.split(r'[.!?]\s+', fallback)
        if sentences:
            built = ""
            for s in sentences:
                s = s.strip()
                if not s or len(s) < 10:
                    continue
                if built:
                    built += ". " + s
                else:
                    built = s
                if len(built) >= 70:
                    break
            if len(built) >= len(desc):
                desc = built[:500]

    info["description"] = desc.strip()[:500]

    # Extract h1/h2 headings for industry detection (Step 5)
    if page:
        for sel in ("h1", "h2"):
            try:
                for el in (page.css(sel) or []):
                    txt = (el.text or "").strip()
                    if txt and len(txt) <= 100:
                        info["headings"].append(txt)
            except Exception:
                pass

    # Socials (FIX-08)
    info["socials"] = _extract_socials(html)
    if "linkedin" in info["socials"]:
        info["company_linkedin"] = info["socials"]["linkedin"]

    # Emails + phones
    info["emails"].extend(_extract_emails(full_text))
    info["phones"].extend(_extract_phones(full_text))

    # Employee count
    info["employee_count"] = _guess_employee_count_from_text(full_text)

    # ── Tiered sub-page scraping ─────────────────────────────────────────────
    # Tier 1: Always fetch (contact info, company description, basic team)
    # Tier 2: Fetch only if Tier 1 didn't give us contacts with emails
    scraped: set = set()

    def _scrape_subpage(path: str) -> None:
        sub_url = homepage_url.rstrip("/") + path
        if sub_url in scraped:
            return
        scraped.add(sub_url)
        time.sleep(random.uniform(0.2, 0.5))   # reduced delay for throughput

        sub = _fetch_page(sub_url)
        if not sub:
            return

        sub_text = _page_text(sub)
        sub_html = _page_html(sub)

        # Merge emails
        for e in _extract_emails(sub_text):
            if e not in info["emails"]:
                info["emails"].append(e)

        # Merge phones
        for p in _extract_phones(sub_text):
            if p not in info["phones"]:
                info["phones"].append(p)

        # Merge socials
        new_socials = _extract_socials(sub_html)
        for k, v in new_socials.items():
            info["socials"].setdefault(k, v)
        if not info["company_linkedin"] and "linkedin" in new_socials:
            info["company_linkedin"] = new_socials["linkedin"]

        # Person cards
        for c in _extract_person_cards(sub, sub_text, info["domain"]):
            if not any(x["full_name"] == c["full_name"] for x in info["contacts"]):
                info["contacts"].append(c)

        # Location
        if not info["city"]:
            loc = _extract_location_from_text(sub_text)
            info.update({k: v for k, v in loc.items() if v})

    # Tier 1 — always fetch
    for path in TIER1_PATHS:
        _scrape_subpage(path)

    # Tier 2 — only if Tier 1 didn't find contacts with on-domain emails
    has_email_contacts = any(c.get("email") for c in info["contacts"])
    if not has_email_contacts:
        for path in TIER2_PATHS:
            _scrape_subpage(path)
            # Stop early if we found what we need
            if any(c.get("email") for c in info["contacts"]):
                break

    # ── LinkedIn company page scrape (employee count, HQ, description) ─────
    # This uses Scrapling directly — no DDG queries, no rate limiting
    if info.get("company_linkedin"):
        print(f"      🔗 Scraping LinkedIn company page...")
        li_data = _scrapling_linkedin_company(info["company_linkedin"])
        if li_data:
            if li_data.get("employee_count") and not info["employee_count"]:
                info["employee_count"] = li_data["employee_count"]
                print(f"      ✅ Employees: {info['employee_count']}")
            if li_data.get("city") and not info["city"]:
                info["city"] = li_data["city"]
                info["hq_city"] = li_data["city"]
            if li_data.get("state") and not info["state"]:
                info["state"] = li_data["state"]
                info["hq_state"] = li_data["state"]
            if li_data.get("description"):
                li_desc = li_data["description"].strip()
                # Use LinkedIn description if we have none, or if ours is too short
                if not info["description"] or len(info["description"]) < 70:
                    if len(li_desc) >= len(info.get("description", "")):
                        info["description"] = li_desc[:500]
            if li_data.get("industry"):
                info["linkedin_industry"] = li_data["industry"]
                print(f"      🏭 LinkedIn industry: {li_data['industry']}")

    # ── Step 3: Executive search (DDG + Google) ─────────────────────────────
    real_contacts = [c for c in info["contacts"]
                     if c.get("full_name", "").lower() != info["business"].lower()]
    if not real_contacts and info["business"]:
        print(f"      🔍 Searching for executive...")
        exec_info = _search_executive_ddg(info["business"], info["domain"], info["emails"])
        if exec_info:
            info["contacts"] = [exec_info]
            print(f"      ✅ Found: {exec_info['full_name']} ({exec_info['role']})")
        else:
            print(f"      ⚠️  No executive found")

    # ── AI extraction fallback (Haiku) — fires when ALL other methods found nothing ──
    real_contacts = [c for c in info["contacts"]
                     if c.get("full_name", "").lower() != info["business"].lower()]
    if not real_contacts and len(full_text) > 500:
        print(f"      🤖 AI extraction fallback (Haiku)...")
        ai_contacts = _extract_with_ai(full_text, info["domain"])
        if ai_contacts:
            # Wire in email via pattern verification for any AI contact missing email
            for ac in ai_contacts:
                if not ac.get("email") and ac.get("first") and ac.get("last"):
                    ac["email"] = _verify_email_pattern(
                        ac["first"], ac["last"], info["domain"], info["emails"]
                    )
            info["contacts"].extend(ai_contacts)
            print(f"      ✅ AI found {len(ai_contacts)} contact(s)")

    # Fallback location from homepage text
    if not info["city"]:
        loc = _extract_location_from_text(full_text)
        info.update({k: v for k, v in loc.items() if v})

    # Step 8: DDG employee count search ONLY if LinkedIn scrape didn't work
    if not info["employee_count"] and info["business"]:
        print(f"      🔍 Searching DDG for employee count...")
        emp = _search_employee_count_ddg(info["business"], info["domain"])
        if emp:
            info["employee_count"] = emp
            print(f"      ✅ Employees: {emp}")

    # Validate city/state (reject garbage like city="USA")
    info["state"] = _validate_state(info["state"], info.get("country", ""))
    info["hq_state"] = _validate_state(info["hq_state"], info.get("hq_country", ""))
    info["city"] = _validate_city(info["city"], info["state"], info.get("country", ""))
    info["hq_city"] = _validate_city(info["hq_city"], info["hq_state"], info.get("hq_country", ""))

    # Mirror HQ from contact location if blank
    if not info["hq_country"] and info["country"]:
        info["hq_country"] = info["country"]
        info["hq_state"]   = info["state"]
        info["hq_city"]    = info["city"]

    # ── Cross-validate business name against domain + LinkedIn slug ────────
    # The gateway validator (Stage 4) will check business name against LinkedIn.
    # If our extracted name doesn't match either the domain or the LinkedIn slug,
    # it's almost certainly garbage and will be rejected — costing a rate limit slot.
    biz = info.get("business", "")
    domain = info.get("domain", "")
    co_li = info.get("company_linkedin", "")

    if biz and domain:
        domain_match = _biz_matches_domain(biz, domain)
        linkedin_match = _biz_matches_linkedin_slug(biz, co_li) if co_li else False

        if not domain_match and not linkedin_match:
            # Business name doesn't match domain or LinkedIn — likely garbage
            # Try to recover from LinkedIn slug first, then domain fallback
            li_name = _biz_name_from_linkedin_slug(co_li)
            if li_name and _is_valid_biz_name(li_name) and _biz_matches_domain(li_name, domain):
                logger.info(
                    f"Business name '{biz}' doesn't match domain '{domain}' — "
                    f"corrected to '{li_name}' from LinkedIn slug"
                )
                info["business"] = li_name
            else:
                # Fall back to domain-derived name
                base = domain.split(".")[0]
                domain_name = " ".join(w.capitalize() for w in re.split(r'[-_]', base))
                if _is_valid_biz_name(domain_name):
                    logger.info(
                        f"Business name '{biz}' doesn't match domain '{domain}' — "
                        f"corrected to '{domain_name}' from domain"
                    )
                    info["business"] = domain_name

    return info


# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY NORMALIZATION (gateway only accepts US + UAE/Dubai)
# ─────────────────────────────────────────────────────────────────────────────

_UAE_INDICATORS = {
    "united arab emirates", "uae", "u.a.e.", "emirates",
    "dubai", "abu dhabi", "sharjah", "ajman", "fujairah", "al ain",
    "dubai uae", "abu dhabi uae", "sharjah uae", "mena", "mena region",
}

_US_INDICATORS = {
    "united states", "usa", "us", "u.s.", "u.s.a.", "america",
    "united states of america",
}


def _normalize_allowed_country(country: Optional[str], city: Optional[str] = None) -> str:
    """
    Normalize country to one of the two gateway-allowed values.

    Rules:
      - Any US variant → "United States"
      - Any UAE variant OR city is Dubai → "United Arab Emirates"
      - Unknown → "United States" (safe default; most scraped leads are US)

    No network calls. Pure string matching.
    """
    c = (country or "").strip().lower()
    ci = (city or "").strip().lower()

    # Check US explicitly first — takes priority over city-based UAE inference
    # This prevents "Dubai, California" from being mapped to UAE
    if c in _US_INDICATORS:
        return "United States"

    # If country is a US state name (sometimes state gets put in country field)
    if c and c in _US_STATE_NAMES:
        return "United States"

    # Check UAE — country field takes priority
    if c in _UAE_INDICATORS:
        return "United Arab Emirates"

    # City-based UAE inference — only when country is empty or unrecognized
    # Guard: if city contains a US state name alongside "dubai", it's US
    # e.g. "Dubai, California" → ci contains "california" → skip UAE
    if ci and not c:
        _US_STATE_WORDS = {"california", "texas", "florida", "new york", "virginia",
                           "georgia", "ohio", "illinois", "pennsylvania", "michigan",
                           "arizona", "colorado", "washington", "oregon", "nevada",
                           "massachusetts", "maryland", "carolina", "jersey", "connecticut"}
        has_us_state = any(st in ci for st in _US_STATE_WORDS)
        if not has_us_state and ci in _UAE_INDICATORS:
            return "United Arab Emirates"

    # Default: United States (most B2B leads are US-based)
    return "United States"


# ─────────────────────────────────────────────────────────────────────────────
# LINKEDIN URL DISCOVERY  (aggressive multi-query search)
# ─────────────────────────────────────────────────────────────────────────────
# The validator Stage 4 requires both personal and company LinkedIn URLs.
# Leads missing either are rejected. We run multiple search queries using
# the existing Serper/DDG search stack to maximize discovery rate.

# In-memory cache: (person+company key) → (person_li, company_li)
_linkedin_cache: Dict[str, Tuple[str, str]] = {}


def _normalize_linkedin_url(url: str) -> str:
    """
    Strip tracking params and trailing slashes from LinkedIn URLs.
    'linkedin.com/in/john-doe-123?trk=public_profile' → 'linkedin.com/in/john-doe-123'
    """
    if not url:
        return ""
    url = url.split("?")[0].split("#")[0].rstrip("/")
    return url


# Signals that the person no longer works at the company
_FORMER_EMPLOYEE_SIGNALS = re.compile(
    r'\b(?:former|ex-|previously|past|prior|retired|departed|left|was\s+at|'
    r'formerly|ex\s|moved\s+to|joined\s|now\s+at)\b',
    re.IGNORECASE,
)


def _extract_linkedin_person_url(href: str, person_name: str) -> Optional[str]:
    """Return normalized URL if it's a valid /in/ profile."""
    if "linkedin.com/in/" not in href.lower():
        return None
    if "linkedin.com/company/" in href.lower():
        return None
    return _normalize_linkedin_url(href)


def _extract_linkedin_company_url(href: str) -> Optional[str]:
    """Return normalized URL if it's a valid /company/ page."""
    if "linkedin.com/company/" not in href.lower():
        return None
    if "linkedin.com/in/" in href.lower():
        return None
    return _normalize_linkedin_url(href)


def _score_person_linkedin(url: str, person_name: str, business: str,
                            title: str, body: str) -> int:
    """
    Score a LinkedIn person URL result. Higher = better match.

    Scoring:
      +3  person name words appear in /in/ slug
      +2  business name appears in snippet
      +1  base score for being a /in/ URL
      -3  snippet contains "former", "ex-", "previously" (wrong employer)
    """
    score = 1
    snippet = f"{title} {body}".lower()

    # Slug-name match: extract slug words, check against name words
    slug_m = re.search(r'linkedin\.com/in/([\w\-]+)', url, re.I)
    if slug_m:
        slug_raw = slug_m.group(1).lower()
        slug_parts = slug_raw.split("-")
        # Remove trailing numeric IDs (e.g. "john-doe-283942" → ["john","doe"])
        slug_words = [w for w in slug_parts if not w.isdigit() and len(w) >= 2]
        name_words = [w.lower() for w in person_name.split() if len(w) >= 2]
        # Word-level match: "john" in ["john","doe"]
        matching = sum(1 for w in name_words if w in slug_words)
        if matching > 0:
            score += 3
        else:
            # Substring match for concatenated slugs: "jdoe" contains "doe"
            slug_flat = slug_raw.replace("-", "")
            if any(w in slug_flat for w in name_words if len(w) >= 3):
                score += 2

    # Business in snippet
    if business and business.lower() in snippet:
        score += 2

    # Former employee penalty
    if _FORMER_EMPLOYEE_SIGNALS.search(snippet):
        score -= 3

    return score


def _score_company_linkedin(url: str, business: str, domain: str,
                             title: str, body: str) -> int:
    """
    Score a LinkedIn company URL result. Higher = better match.

    Scoring:
      +3  business name matches /company/ slug
      +2  domain appears in snippet
      +1  base score for being a /company/ URL
    """
    score = 1
    snippet = f"{title} {body}".lower()

    slug_m = re.search(r'linkedin\.com/company/([\w\-]+)', url, re.I)
    if slug_m:
        slug = slug_m.group(1).lower().replace("-", "")
        biz_clean = re.sub(r'[^a-z0-9]', '', business.lower())
        if biz_clean and (biz_clean in slug or slug in biz_clean):
            score += 3

    if domain and domain.lower() in snippet:
        score += 2

    return score


def _find_linkedin_urls(
    person_name: str,
    business: str,
    domain: str,
    existing_person_li: str = "",
    existing_company_li: str = "",
) -> Tuple[str, str, List[str]]:
    """
    Aggressively search for personal and company LinkedIn URLs.

    Uses multiple query strategies via the existing _search_ddg() stack
    (Serper API → DDG library fallback). Each query costs ~0.001 Serper credits.

    Returns (person_linkedin_url, company_linkedin_url, person_snippet_evidence).
    The snippet evidence is used by _verify_person_company_match() to check
    that the person actually works at the claimed company — without needing
    to scrape the LinkedIn profile page (which requires authentication).
    """
    person_li = _normalize_linkedin_url(existing_person_li) if existing_person_li else ""
    company_li = _normalize_linkedin_url(existing_company_li) if existing_company_li else ""

    # If we already have both, skip (no snippet evidence for pre-existing URLs)
    if person_li and company_li:
        return person_li, company_li, []

    # Check cache
    cache_key = f"{person_name}|{business}".lower().strip()
    if cache_key in _linkedin_cache:
        cached_person, cached_company = _linkedin_cache[cache_key]
        person_li = person_li or cached_person
        company_li = company_li or cached_company
        if person_li and company_li:
            return person_li, company_li, []

    # Collect snippet evidence for person-company verification
    # (reused by _verify_person_company_match later)
    _person_snippet_evidence: List[str] = []

    # ── Person LinkedIn search ──────────────────────────────────────────
    if not person_li and person_name and business:
        person_queries = [
            f'"{person_name}" "{business}" site:linkedin.com/in',
            f'"{person_name}" "{business}" linkedin',
        ]
        if domain:
            person_queries.append(f'"{person_name}" "{domain}" linkedin')
        # Last resort: name-only + linkedin (filter by snippet later)
        person_queries.append(f'"{person_name}" linkedin')

        for query in person_queries:
            if person_li:
                break
            try:
                results = _search_ddg(query, max_results=5, retries=1)
                best_url = ""
                best_score = 0
                best_snippet = ""
                for r in results:
                    href = r.get("href", "")
                    url = _extract_linkedin_person_url(href, person_name)
                    if not url:
                        continue
                    title_text = r.get("title", "")
                    body_text = r.get("body", "") or ""
                    score = _score_person_linkedin(
                        url, person_name, business, title_text, body_text,
                    )
                    if score <= 0:
                        continue
                    if score > best_score:
                        best_score = score
                        best_url = url
                        best_snippet = f"{title_text} {body_text}"
                    # Collect ALL relevant snippets for later verification
                    if "linkedin.com/in/" in href.lower():
                        _person_snippet_evidence.append(f"{title_text} {body_text}")
                if best_url:
                    person_li = best_url
                    if best_snippet:
                        _person_snippet_evidence.insert(0, best_snippet)
            except Exception:
                continue
            time.sleep(random.uniform(0.3, 0.8))

    # ── Company LinkedIn search ─────────────────────────────────────────
    if not company_li and business:
        company_queries = [
            f'"{business}" site:linkedin.com/company',
            f'"{business}" linkedin company page',
        ]
        if domain:
            company_queries.append(f'"{domain}" site:linkedin.com/company')

        for query in company_queries:
            if company_li:
                break
            try:
                results = _search_ddg(query, max_results=5, retries=1)
                best_url = ""
                best_score = 0
                for r in results:
                    href = r.get("href", "")
                    url = _extract_linkedin_company_url(href)
                    if not url:
                        continue
                    score = _score_company_linkedin(
                        url, business, domain,
                        r.get("title", ""), r.get("body", "") or "",
                    )
                    if score > best_score:
                        best_score = score
                        best_url = url
                if best_url:
                    company_li = best_url
            except Exception:
                continue
            time.sleep(random.uniform(0.3, 0.8))

    # ── Company LinkedIn: domain-derived slug fallback ──────────────────
    # If search found nothing, try constructing the URL from the domain.
    # Many tech companies use: linkedin.com/company/{domain-without-tld}
    # e.g. paloaltonetworks.com → linkedin.com/company/palo-alto-networks
    if not company_li and domain:
        domain_root = domain.split(".")[0].lower()
        # Try the plain root first, then a hyphenated version
        slug_candidates = [domain_root]
        # Insert hyphens between camelCase/concatenated words:
        # "paloaltonetworks" → "palo-alto-networks" (heuristic: split on known words)
        # Simple approach: try the raw slug — LinkedIn will redirect if close
        if len(domain_root) > 10:
            # Try adding hyphens between lowercase word boundaries
            hyphenated = re.sub(r'([a-z])([A-Z])', r'\1-\2', domain_root).lower()
            if hyphenated != domain_root:
                slug_candidates.append(hyphenated)

        for slug in slug_candidates:
            # Verify the URL exists via a quick search
            try:
                verify_results = _search_ddg(
                    f'site:linkedin.com/company/{slug}', max_results=1, retries=1
                )
                for r in verify_results:
                    href = r.get("href", "")
                    if "linkedin.com/company/" in href.lower():
                        company_li = _normalize_linkedin_url(href)
                        break
                if company_li:
                    break
            except Exception:
                continue

    # Cache result
    _linkedin_cache[cache_key] = (person_li, company_li)
    return person_li, company_li, _person_snippet_evidence


# ─────────────────────────────────────────────────────────────────────────────
# PERSON ↔ COMPANY VERIFICATION  (prevents validator Stage 4 mismatch)
# ─────────────────────────────────────────────────────────────────────────────
# LinkedIn personal profiles can't be scraped (login wall). Instead, we verify
# using search engine snippets which contain the person's headline and company.
# Google/Serper results for LinkedIn profiles show:
#   "John Doe - CEO at Stripe | LinkedIn"
#   "John Doe | Founder & CEO | Stripe | San Francisco"
# We parse these to confirm the person currently works at the claimed company.

def _normalize_company_for_match(name: str) -> str:
    """Normalize company name for fuzzy comparison."""
    return re.sub(r'[^a-z0-9]', '', name.lower().strip())


def _verify_person_company_match(
    person_url: str,
    company_url: str,
    business: str,
    person_name: str,
    snippet_evidence: Optional[List[str]] = None,
) -> bool:
    """
    Verify that the person's LinkedIn profile indicates they work at the claimed company.

    Uses 3 signals (accept if at least 2 match):
      1. Search snippet evidence (headline from Google/Serper results)
      2. Company LinkedIn slug ↔ business name
      3. Dedicated verification search (if snippets are inconclusive)

    No LinkedIn scraping — uses search snippets which contain the headline.
    """
    if not person_url or not business:
        return False

    biz_norm = _normalize_company_for_match(business)
    if not biz_norm:
        return False

    # ── Signal 1: Company slug matches business ─────────────────────────
    slug_match = False
    if company_url:
        slug_m = re.search(r'linkedin\.com/company/([\w\-]+)', company_url, re.I)
        if slug_m:
            slug_norm = slug_m.group(1).lower().replace("-", "")
            slug_match = (biz_norm in slug_norm) or (slug_norm in biz_norm)

    # ── Signal 2: Snippet evidence contains business name ───────────────
    snippet_match = False
    all_snippets = snippet_evidence or []

    for snippet in all_snippets:
        sl = snippet.lower()
        # Check business name appears near "at" or in LinkedIn headline format
        # "John Doe - CEO at Stripe | LinkedIn"
        # "Founder & CEO at Stripe"
        if biz_norm in _normalize_company_for_match(sl):
            snippet_match = True
            break
        # Also check for "at {business}" pattern
        biz_words = [w for w in business.lower().split() if len(w) >= 3]
        if biz_words and any(w in sl for w in biz_words):
            # Check it's not "former" context
            if not _FORMER_EMPLOYEE_SIGNALS.search(sl):
                snippet_match = True
                break

    # ── Signal 3: Dedicated verification search ─────────────────────────
    # Only runs if signals 1+2 are split (one match, one not)
    search_match = False
    if slug_match != snippet_match:
        # One signal matches, one doesn't — run a verification query
        person_slug_m = re.search(r'linkedin\.com/in/([\w\-]+)', person_url, re.I)
        if person_slug_m and person_name:
            try:
                verify_query = f'"{person_name}" "{business}" site:linkedin.com'
                results = _search_ddg(verify_query, max_results=3, retries=1)
                for r in results:
                    combined = f"{r.get('title', '')} {r.get('body', '') or ''}".lower()
                    if biz_norm in _normalize_company_for_match(combined):
                        if not _FORMER_EMPLOYEE_SIGNALS.search(combined):
                            search_match = True
                            break
            except Exception:
                pass

    # ── Decision: accept if at least 2 of 3 signals match ──────────────
    matches = sum([slug_match, snippet_match, search_match])

    if matches >= 2:
        return True

    # Relaxed: if slug matches AND we have no counter-evidence, accept
    # (many small companies have no search snippets but valid LinkedIn)
    if slug_match and not all_snippets:
        return True

    # Relaxed: if snippet confirms AND no company_url to check slug, accept
    if snippet_match and not company_url:
        return True

    if matches >= 1:
        # One signal — accept with warning (borderline case)
        logger.debug(
            f"Person-company match borderline: {person_name} at {business} "
            f"(slug={slug_match}, snippet={snippet_match}, search={search_match})"
        )
        return True

    logger.warning(
        f"Person-company MISMATCH: {person_name} does not appear to work at {business} "
        f"(slug={slug_match}, snippet={snippet_match}, search={search_match})"
    )
    return False


# ─────────────────────────────────────────────────────────────────────────────
# ROLE VERIFICATION FROM LINKEDIN SNIPPETS
# ─────────────────────────────────────────────────────────────────────────────
# The scraper may assign the wrong C-suite title to a person when multiple
# executives appear in search results. For example, it finds "Daniel Hellerman"
# (CPO) and assigns "CEO" because that role appeared nearby in the results.
#
# Fix: extract the ACTUAL role from the LinkedIn search snippet for this person.
# Snippets look like: "Daniel Hellerman - Co-Founder & CPO at Saleo | LinkedIn"
# If the snippet says "CPO" but the scraper assigned "CEO", correct the role.

# Role keywords ranked by seniority (for role extraction from snippets)
_ROLE_KEYWORDS_RE = re.compile(
    r'\b(CEO|CTO|CFO|COO|CMO|CPO|CRO|CIO|CISO|'
    r'Chief\s+Executive\s+Officer|Chief\s+Technology\s+Officer|'
    r'Chief\s+Financial\s+Officer|Chief\s+Operating\s+Officer|'
    r'Chief\s+Marketing\s+Officer|Chief\s+Product\s+Officer|'
    r'President|Founder|Co-Founder|Managing\s+Director|'
    r'VP|Vice\s+President|Director|Partner|Principal|Owner)\b',
    re.IGNORECASE,
)


def _extract_role_from_snippets(
    person_name: str, snippets: List[str]
) -> str:
    """
    Extract the role for a specific person from LinkedIn search snippets.

    Snippet formats:
      "Daniel Hellerman - Co-Founder & CPO at Saleo | LinkedIn"
      "Justin McDonald - Co-Founder & CEO at Saleo | LinkedIn"

    Returns the role string or "" if not found.
    """
    if not person_name or not snippets:
        return ""

    name_parts = [w.lower() for w in person_name.split() if len(w) >= 3]
    if not name_parts:
        return ""

    for snippet in snippets:
        sl = snippet.lower()
        # Check if this snippet is about our person (name appears in it)
        if not any(p in sl for p in name_parts):
            continue

        # Extract role from the snippet
        # Typical format: "Name - Role at Company | LinkedIn"
        for sep in [' - ', ' | ', ' – ', ' — ']:
            if sep not in snippet:
                continue
            parts = snippet.split(sep)
            for part in parts[1:]:  # skip the name part (first segment)
                # Look for role keywords in this segment
                roles_found = _ROLE_KEYWORDS_RE.findall(part)
                if roles_found:
                    # Strip company name from role in various formats
                    # "Co-Founder & CPO at Saleo" → "Co-Founder & CPO"
                    role_text = re.split(r'\s+(?:at|@)\s+', part, flags=re.I)[0].strip()
                    # "President, Starburst" → "President"
                    role_text = re.split(r'\s*,\s+(?=[A-Z])', role_text)[0].strip()
                    # "CEO - Stripe" → "CEO"
                    role_text = re.split(r'\s+[-–—]\s+(?=[A-Z])', role_text)[0].strip()
                    role_text = role_text.rstrip(" |–—-,.")
                    if role_text and len(role_text) <= 60:
                        return _normalize_role(role_text)

    return ""


def _verify_and_correct_role(
    full_name: str,
    current_role: str,
    snippets: List[str],
    business: str,
) -> str:
    """
    Cross-check the assigned role against LinkedIn snippet evidence.
    If the snippet shows a different role for this person, use the snippet's role.

    Example:
      Assigned: "CEO" (scraped from page, wrong person)
      Snippet: "Daniel Hellerman - Co-Founder & CPO at Saleo"
      Corrected: "Co-Founder & CPO"

    Returns the corrected role.
    """
    snippet_role = _extract_role_from_snippets(full_name, snippets)

    if not snippet_role:
        return current_role  # no evidence, keep what we have

    # If snippet role matches current role, no correction needed
    current_lower = current_role.lower().strip()
    snippet_lower = snippet_role.lower().strip()
    if current_lower == snippet_lower:
        return current_role

    # Check if they're substantively different (not just formatting)
    # "Co-Founder & CEO" vs "CEO" — close enough
    current_keywords = set(_ROLE_KEYWORDS_RE.findall(current_role))
    snippet_keywords = set(_ROLE_KEYWORDS_RE.findall(snippet_role))
    current_kw_lower = {k.lower() for k in current_keywords}
    snippet_kw_lower = {k.lower() for k in snippet_keywords}

    if current_kw_lower == snippet_kw_lower:
        # Same role keywords, just different formatting — keep snippet (more complete)
        return snippet_role

    # Roles are genuinely different (e.g. CEO vs CPO)
    logger.info(
        f"Role corrected from snippet: '{current_role}' → '{snippet_role}' "
        f"for {full_name} at {business}"
    )
    return snippet_role


# ─────────────────────────────────────────────────────────────────────────────
# NAME CORRECTION FROM LINKEDIN  (fixes garbage names like "Saleo Logo")
# ─────────────────────────────────────────────────────────────────────────────
# When the scraper extracts a garbage name (logo text, section heading, etc.)
# but finds a valid LinkedIn /in/ URL, we can derive the REAL name from:
#   1. The LinkedIn slug: /in/justin-mcdonald-21a3aa6 → "Justin Mcdonald"
#   2. The search snippet: "Justin McDonald - Founder & CEO at Saleo | LinkedIn"
# Then regenerate the email using the corrected name + company domain.

def _name_from_linkedin_slug(linkedin_url: str) -> Tuple[str, str, str]:
    """
    Extract a person name from a LinkedIn /in/ slug.
    Returns (full_name, first, last) or ("", "", "").

    "linkedin.com/in/justin-mcdonald-21a3aa6" → ("Justin Mcdonald", "Justin", "Mcdonald")
    "linkedin.com/in/hesamlamei"              → ("Hesamlamei", "Hesamlamei", "")
    """
    m = re.search(r'linkedin\.com/in/([\w\-]+)', linkedin_url, re.I)
    if not m:
        return "", "", ""
    slug = m.group(1)
    parts = slug.split("-")
    # Filter out trailing IDs: pure digits, hex-like (21a3aa6), or single chars
    name_parts = []
    for p in parts:
        if not p or len(p) < 2:
            continue
        if p.isdigit():
            continue
        # Reject hex-like trailing IDs (e.g. "21a3aa6", "b5a124")
        # Heuristic: if it contains digits AND is the last segment, skip it
        if any(c.isdigit() for c in p) and p == parts[-1]:
            continue
        # Also skip if it looks like a LinkedIn ID (mix of digits and letters, 5+ chars)
        if len(p) >= 5 and sum(c.isdigit() for c in p) >= 2:
            continue
        name_parts.append(p)

    if not name_parts:
        return "", "", ""
    name_parts = [p.capitalize() for p in name_parts]
    full = " ".join(name_parts)
    first_name = name_parts[0]
    last_name = name_parts[-1] if len(name_parts) > 1 else ""
    return full, first_name, last_name


def _name_from_linkedin_snippet(snippets: List[str]) -> Tuple[str, str, str]:
    """
    Extract a person name from LinkedIn search result snippets.

    Typical snippet formats:
      "Justin McDonald - Founder & CEO at Saleo | LinkedIn"
      "Jean Marie Richardson | Founder | iFOLIO"
      "Dr. Sarah O'Brien - CTO at Acme | LinkedIn"

    Returns (full_name, first, last) or ("", "", "").
    """
    # Pattern handles: McDonald, O'Brien, De La Cruz, hyphenated names
    # [A-Z] starts each word, then allows lowercase, apostrophes, uppercase mid-word
    name_re = re.compile(r"^([A-Z][a-zA-Z'.-]+(?:\s[A-Z][a-zA-Z'.-]+)+)")

    for snippet in snippets:
        for sep in [' - ', ' | ', ' – ', ' — ']:
            if sep in snippet:
                candidate = snippet.split(sep)[0].strip()
                # Strip common prefixes: "Dr. ", "Prof. "
                candidate = re.sub(r'^(?:Dr|Prof|Mr|Mrs|Ms|Miss)\.\s*', '', candidate)
                m = name_re.match(candidate)
                if m:
                    name = m.group(1).strip()
                    if _is_valid_person_name(name):
                        first_name, last_name = _split_name(name)
                        return name, first_name, last_name

    return "", "", ""


def _correct_name_from_linkedin(
    current_name: str,
    current_first: str,
    current_last: str,
    current_email: str,
    linkedin_url: str,
    snippets: List[str],
    domain: str,
    business: str,
) -> Optional[Tuple[str, str, str, str]]:
    """
    If the current name is garbage but LinkedIn has real data, correct it.

    Returns (full_name, first, last, email) if corrected, or None if no correction needed.
    """
    # Check if current name is already valid and matches LinkedIn slug
    if _is_valid_person_name(current_name):
        # Check name matches LinkedIn slug
        slug_m = re.search(r'linkedin\.com/in/([\w\-]+)', linkedin_url, re.I)
        if slug_m:
            slug = slug_m.group(1).lower().replace("-", "")
            name_parts = [w.lower() for w in current_name.split() if len(w) >= 3]
            if name_parts and any(p in slug for p in name_parts):
                return None  # Name already matches LinkedIn — no correction needed

    # Current name is garbage OR doesn't match LinkedIn. Try to extract real name.
    # Priority 1: snippet (has proper capitalization)
    real_name, real_first, real_last = _name_from_linkedin_snippet(snippets or [])

    # Priority 2: slug (always available but may lack proper casing)
    if not real_name:
        real_name, real_first, real_last = _name_from_linkedin_slug(linkedin_url)

    if not real_name or not _is_valid_person_name(real_name):
        return None  # Can't extract a valid name

    # Don't "correct" if the slug-derived name is the same person as current
    if current_name and real_name:
        curr_lower = current_name.lower().strip()
        real_lower = real_name.lower().strip()
        if curr_lower == real_lower:
            return None  # Same person, no correction needed

    logger.info(
        f"Name correction: '{current_name}' → '{real_name}' (from LinkedIn)"
    )

    # Regenerate email from the corrected name + company domain
    new_email = ""
    if domain and real_first and real_last:
        new_email = _verify_email_pattern(real_first, real_last, domain, [])
    if not new_email:
        # Fall back to simple first.last pattern
        f = re.sub(r'[^a-z]', '', real_first.lower())
        l = re.sub(r'[^a-z]', '', real_last.lower())
        if f and l and domain:
            new_email = f"{f}.{l}@{domain}"

    return real_name, real_first, real_last, new_email


# ─────────────────────────────────────────────────────────────────────────────
# LEAD ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────

def _assemble_lead(
    company_info: Dict[str, Any],
    contact: Dict[str, Any],
    industry: Optional[str],
    region: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Anchor-validated lead assembly. Requires real email matching company domain."""
    # Step 5: correct industry from LinkedIn/Crunchbase/website signals
    ind, sub_ind = _correct_industry(company_info, industry)

    email     = contact.get("email", "")
    full_name = contact.get("full_name", "")
    first     = contact.get("first", "")
    last      = contact.get("last", "")
    role      = contact.get("role", "")
    linkedin  = contact.get("linkedin", "")
    if linkedin and not linkedin.startswith("http"):
        linkedin = f"https://www.linkedin.com/in/{linkedin}"

    # ── Anchor validation ───────────────────────────────────────────────
    if not email or not company_info.get("business"):
        return None
    # Email domain must relate to the company website domain
    # Allows subdomains (us.stripe.com) and sibling TLDs (hubspot.io vs hubspot.com)
    email_domain = email.split("@")[1].lower() if "@" in email else ""
    site_domain = _strip_www(company_info.get("domain", "").lower())
    if email_domain and site_domain and email_domain != site_domain:
        # Allow subdomain or root overlap
        if not email_domain.endswith("." + site_domain):
            email_root = email_domain.split(".")[0]
            site_root = site_domain.split(".")[0]
            if not (site_root and email_root and
                    (site_root in email_root or email_root in site_root)):
                logger.debug(f"Email domain mismatch: {email_domain} != {site_domain}")
                return None
    # Must have a real person name (not junk, not the company name)
    if not _is_valid_person_name(full_name):
        return None
    if full_name.lower() == company_info.get("business", "").lower():
        return None

    country = company_info.get("country", "")
    state   = company_info.get("state", "")
    city    = company_info.get("city", "")

    # Normalise country to allowed values (US or UAE only)
    # Also consider region as a signal when country is empty
    raw_country = country or (region if region else "")
    country = _normalize_allowed_country(raw_country, city)

    # Step 6: validate city/state in final lead
    state   = _validate_state(state, country)
    city    = _validate_city(city, state, country)

    # Step 7: Aggressive LinkedIn URL discovery + person-company verification
    biz_name = company_info.get("business", "").strip()
    company_linkedin = company_info.get("company_linkedin", "")
    site_domain = company_info.get("domain", "")

    linkedin, company_linkedin, li_snippets = _find_linkedin_urls(
        full_name, biz_name, site_domain,
        existing_person_li=linkedin,
        existing_company_li=company_linkedin,
    )

    # Step 7b: Cross-check name against LinkedIn slug + snippets.
    # If the scraped name is garbage but LinkedIn has the real person,
    # CORRECT the name from LinkedIn data rather than rejecting.
    if linkedin:
        corrected = _correct_name_from_linkedin(
            full_name, first, last, email, linkedin, li_snippets,
            site_domain, biz_name,
        )
        if corrected:
            full_name, first, last, email = corrected
            logger.info(f"Corrected contact from LinkedIn: {full_name} ({email})")

    # Step 7c: Verify person actually works at claimed company
    if linkedin and biz_name:
        if not _verify_person_company_match(
            linkedin, company_linkedin, biz_name, full_name, li_snippets
        ):
            logger.warning(
                f"Rejected: {full_name} does not appear to work at {biz_name}"
            )
            return None

    # Step 7d: Verify and correct role from LinkedIn snippet evidence
    # Fixes: scraper assigns "CEO" to person who is actually "CPO" because
    # both titles appeared in nearby search results
    if li_snippets and full_name and role:
        role = _verify_and_correct_role(full_name, role, li_snippets, biz_name)

    lead = {
        "business":         company_info.get("business", "").strip(),
        "full_name":        full_name.strip(),
        "first":            first.strip(),
        "last":             last.strip(),
        "email":            email.lower().strip(),
        "role":             role.strip(),
        "linkedin":         linkedin.strip(),
        "website":          company_info.get("website", ""),
        "company_linkedin": company_linkedin or company_info.get("company_linkedin", ""),
        "description":      company_info.get("description", "")[:500],
        "employee_count":   company_info.get("employee_count", ""),
        "industry":         ind,
        "sub_industry":     sub_ind,
        "country":          country,
        "state":            state,
        "city":             city,
        "hq_country":       _normalize_allowed_country(
                                company_info.get("hq_country") or country,
                                company_info.get("hq_city") or city,
                            ),
        "hq_state":         _validate_state(company_info.get("hq_state") or state,
                                _normalize_allowed_country(company_info.get("hq_country") or country,
                                                           company_info.get("hq_city") or city)),
        "hq_city":          _validate_city(company_info.get("hq_city") or city,
                                _validate_state(company_info.get("hq_state") or state,
                                    _normalize_allowed_country(company_info.get("hq_country") or country,
                                                               company_info.get("hq_city") or city)),
                                _normalize_allowed_country(company_info.get("hq_country") or country,
                                                           company_info.get("hq_city") or city)),
        "source_url":       company_info.get("website", ""),
        "source_type":      "company_site",
        "phone_numbers":    company_info.get("phones", []),
        "socials":          company_info.get("socials", {}),
    }

    # ── Step 10: Final validation — with LLM fix attempt if it fails ─────
    if _validate_lead(lead):
        return lead

    # Validation failed — try LLM fix before discarding
    fixed = _llm_fix_lead(lead)
    if fixed and _validate_lead(fixed):
        logger.info(f"LLM fixed lead: {fixed.get('business', '?')}")
        return fixed

    # Both raw and LLM-fixed lead failed — log for manual review
    save_rejected_lead(lead, "failed _validate_lead + LLM fix")
    return None


# ── Step 10: Final validation rules ───────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# LLM LEAD FIX  (Claude Haiku — last chance to save a lead before rejection)
# ─────────────────────────────────────────────────────────────────────────────
# When _validate_lead() rejects a lead, we give Haiku one shot to fix it.
# Cost: ~$0.0003 per lead (500 input + 300 output tokens @ Haiku pricing).
# Only runs on leads that FAILED validation — good leads skip this entirely.

_LLM_FIX_PROMPT = """Fix this B2B lead so it passes gateway validation. Return ONLY valid JSON (no markdown).

RULES:
- role: must be a real job title (CEO, CTO, Founder, etc.), min 2 chars. Strip company name from role.
- industry: must be a valid parent category (Software, Financial Services, Health Care, etc.)
- sub_industry: must be a specific taxonomy value (Enterprise Software, SaaS, FinTech, etc.)
- country: must be "United States" or "United Arab Emirates"
- state: required 2-letter code for US leads (CA, NY, TX, etc.)
- city: required for US leads, must be a real city name
- hq_country/hq_state/hq_city: same rules as country/state/city
- description: min 70 characters, must describe the company
- email: must be personal (first.last@company.com), NOT generic (info@, hello@)
- linkedin: must contain linkedin.com/in/
- company_linkedin: must contain linkedin.com/company/

Fix what you can. If a field is unfixable (e.g. no way to determine city), leave it as-is.
Return the COMPLETE lead JSON object with fixes applied."""


def _llm_fix_lead(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Use Claude Haiku to fix a lead that failed validation.
    Returns the fixed lead dict, or None if LLM is unavailable or fix failed.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        lead_json = json.dumps(lead, indent=2, ensure_ascii=False)
        response = client.messages.create(
            model="claude-haiku-4-5-20241022",
            max_tokens=1500,
            system=_LLM_FIX_PROMPT,
            messages=[{"role": "user", "content": lead_json}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown fencing if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        fixed = json.loads(raw)
        if not isinstance(fixed, dict):
            return None

        # Preserve fields LLM shouldn't change (URLs, socials)
        for keep_field in ("website", "source_url", "source_type", "linkedin",
                           "company_linkedin", "socials", "phone_numbers"):
            if keep_field in lead:
                fixed[keep_field] = lead[keep_field]

        logger.info(f"LLM fix attempt for {lead.get('business', '?')}: success")
        return fixed

    except ImportError:
        return None
    except json.JSONDecodeError:
        logger.debug(f"LLM fix returned invalid JSON for {lead.get('business', '?')}")
        return None
    except Exception as e:
        logger.debug(f"LLM fix failed for {lead.get('business', '?')}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE URL VALIDATION  (mirrors gateway validator rules)
# ─────────────────────────────────────────────────────────────────────────────
# The gateway performs 4 checks on every source_url:
#   1. Denylist — domain must not be a data broker / lead database
#   2. Domain age — domain must be ≥ 7 days old (WHOIS)
#   3. Reachability — HEAD then GET; must return an allowed status code
#   4. Redirect safety — final redirect host must match original domain
#
# We replicate them here so leads are pre-filtered BEFORE submission.

_SOURCE_DENYLIST_DOMAINS = {
    # Data brokers / lead databases (gateway rejects these)
    "zoominfo.com", "apollo.io", "rocketreach.co", "crunchbase.com",
    "lusha.com", "hunter.io", "snov.io", "clearbit.com", "leadiq.com",
    "peopledatalabs.com", "contactout.com", "uplead.com", "seamless.ai",
    "adapt.io", "voilanorbert.com", "findthatlead.com", "skrapp.io",
    "anymailfinder.com", "datanyze.com", "slintel.com", "demandbase.com",
    # Aggregators / review sites (not real company sites)
    "glassdoor.com", "indeed.com", "yelp.com", "g2.com", "capterra.com",
    "trustpilot.com", "clutch.co", "goodfirms.co",
    # Social / media platforms
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "reddit.com", "medium.com",
    "wikipedia.org", "github.com",
}

# Status codes the gateway treats as "reachable"
_ALLOWED_HTTP_STATUS = {200, 206, 301, 302, 303, 304, 305, 306, 307, 308, 401, 403}

# In-memory cache:  domain → (is_valid: bool, checked_at: float)
# Avoids re-checking the same domain within a session.
_source_url_cache: Dict[str, Tuple[bool, float]] = {}
_SOURCE_CACHE_TTL = 3600  # 1 hour


def _source_cache_get(domain: str) -> Optional[bool]:
    """Return cached validity or None if not cached / expired."""
    entry = _source_url_cache.get(domain)
    if entry is None:
        return None
    is_valid, checked_at = entry
    if time.time() - checked_at > _SOURCE_CACHE_TTL:
        del _source_url_cache[domain]
        return None
    return is_valid


def _source_cache_set(domain: str, is_valid: bool) -> None:
    _source_url_cache[domain] = (is_valid, time.time())


def _check_source_denylist(domain: str) -> Optional[str]:
    """Return rejection reason if domain is denylisted, else None."""
    domain_lower = domain.lower().strip()
    for denied in _SOURCE_DENYLIST_DOMAINS:
        if domain_lower == denied or domain_lower.endswith("." + denied):
            return f"denylisted domain: {denied}"
    return None


def _check_source_domain_age(domain: str) -> Optional[str]:
    """
    Return rejection reason ONLY if domain is confirmed < 7 days old.
    If the creation date cannot be determined (WHOIS failure, non-.com TLD,
    library missing), allow the domain and log a warning — do NOT reject.
    """
    try:
        import whois
        w = whois.whois(domain)
        creation = w.creation_date
        if creation is None:
            logger.warning(f"[Source Validation] WHOIS returned no creation date for {domain} — allowing")
            return None
        # whois sometimes returns a list of dates
        if isinstance(creation, list):
            creation = creation[0]
        if not hasattr(creation, 'timestamp'):
            logger.warning(f"[Source Validation] Could not parse creation date for {domain} — allowing")
            return None
        age_days = (datetime.now() - creation).days
        if age_days < 7:
            return f"domain age {age_days} days < 7 days minimum"
        return None
    except ImportError:
        # python-whois not installed — skip this check
        logger.debug(f"python-whois not installed, skipping domain age check for {domain}")
        return None
    except Exception as e:
        # WHOIS lookup failed — common for non-.com TLDs (.ae, .co, .global, etc.)
        # Do NOT reject; just warn and allow
        logger.warning(f"[Source Validation] WHOIS lookup failed for {domain}: {e} — allowing")
        return None


def _check_source_reachability(url: str) -> Tuple[Optional[str], str]:
    """
    Check URL reachability using plain HTTP (no Chrome impersonation).
    Returns (rejection_reason_or_None, final_url_after_redirects).

    Uses HEAD first, then GET fallback — matches gateway behavior.
    """
    import urllib.request
    import urllib.error

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.getcode()
                final_url = resp.geturl()
                if status in _ALLOWED_HTTP_STATUS:
                    return None, final_url
                return f"HTTP {status} (not in allowed set)", final_url
        except urllib.error.HTTPError as e:
            if e.code in _ALLOWED_HTTP_STATUS:
                return None, e.geturl() if hasattr(e, 'geturl') else url
            if method == "HEAD":
                continue  # try GET fallback
            return f"HTTP {e.code}", url
        except urllib.error.URLError as e:
            if method == "HEAD":
                continue
            reason = str(e.reason) if hasattr(e, 'reason') else str(e)
            return f"URL error: {reason}", url
        except Exception as e:
            if method == "HEAD":
                continue
            return f"connection error: {e}", url

    return "unreachable (HEAD+GET both failed)", url


def _strip_www(host: str) -> str:
    """Strip leading 'www.' prefix from a hostname (not char-by-char)."""
    if host.startswith("www."):
        return host[4:]
    return host


def _check_source_redirect_safety(original_url: str, final_url: str) -> Optional[str]:
    """Return rejection reason if the redirect went to a different host."""
    if not final_url:
        return None
    try:
        # Strip www prefix AND port number (:443, :80, etc.)
        orig_host = _strip_www(urlparse(original_url).hostname or "")
        final_host = _strip_www(urlparse(final_url).hostname or "")
        if not final_host or not orig_host:
            return None
        # Allow same root domain (e.g. company.com → www.company.com)
        if final_host == orig_host:
            return None
        # Allow subdomains (e.g. company.com → app.company.com)
        if final_host.endswith("." + orig_host) or orig_host.endswith("." + final_host):
            return None
        return f"redirected to different domain: {orig_host} → {final_host}"
    except Exception:
        return None


def validate_source_url(url: str) -> bool:
    """
    Validate a source_url against gateway rules (denylist, domain age,
    reachability, redirect safety).  Returns True if the URL passes.

    Results are cached per-domain so the same site isn't re-checked
    within the same scraping session.
    """
    if not url:
        logger.warning("[Source Validation] Rejecting empty source_url")
        return False

    # Normalise
    if not url.startswith("http"):
        url = f"https://{url}"

    domain = _safe_get_domain(url)
    if not domain:
        logger.warning(f"[Source Validation] Rejecting {url} — could not extract domain")
        return False

    # ── Check cache first ────────────────────────────────────────────────
    cached = _source_cache_get(domain)
    if cached is not None:
        if not cached:
            logger.debug(f"[Source Validation] Cached rejection for {domain}")
        return cached

    # ── 1. Denylist ──────────────────────────────────────────────────────
    reason = _check_source_denylist(domain)
    if reason:
        logger.warning(f"[Source Validation] Rejecting {url} — {reason}")
        _source_cache_set(domain, False)
        return False

    # ── 2. Domain age (WHOIS) ────────────────────────────────────────────
    reason = _check_source_domain_age(domain)
    if reason:
        logger.warning(f"[Source Validation] Rejecting {url} — {reason}")
        _source_cache_set(domain, False)
        return False

    # ── 3. Reachability (HEAD → GET) ─────────────────────────────────────
    reason, final_url = _check_source_reachability(url)
    if reason:
        logger.warning(f"[Source Validation] Rejecting {url} — {reason}")
        _source_cache_set(domain, False)
        return False

    # ── 4. Redirect safety ───────────────────────────────────────────────
    reason = _check_source_redirect_safety(url, final_url)
    if reason:
        logger.warning(f"[Source Validation] Rejecting {url} — {reason}")
        _source_cache_set(domain, False)
        return False

    # ── All checks passed ────────────────────────────────────────────────
    _source_cache_set(domain, True)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# INDUSTRY TAXONOMY VALIDATION  (loaded from gateway at import time)
# ─────────────────────────────────────────────────────────────────────────────
# _VALID_SUB_INDUSTRIES: {sub_industry_name: [valid_parent_industry, ...]}
# Loaded from gateway/utils/industry_taxonomy.py so it stays in sync.
# If the gateway file is unavailable, falls back to an empty dict (validation skipped).

_VALID_SUB_INDUSTRIES: Dict[str, List[str]] = {}

def _load_gateway_taxonomy() -> Dict[str, List[str]]:
    """Load the gateway taxonomy once at import time."""
    # Try direct import first (works if gateway package is on sys.path)
    try:
        from gateway.utils.industry_taxonomy import INDUSTRY_TAXONOMY
        return {sub: info["industries"] for sub, info in INDUSTRY_TAXONOMY.items()}
    except ImportError:
        pass

    # Fallback: read the file directly from known relative paths
    for rel in [
        os.path.join('..', '..', 'gateway', 'utils', 'industry_taxonomy.py'),
        os.path.join('gateway', 'utils', 'industry_taxonomy.py'),
    ]:
        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), rel))
        if os.path.exists(path):
            try:
                import ast as _ast
                with open(path, 'r') as f:
                    tree = _ast.parse(f.read())
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Assign):
                        for target in node.targets:
                            if isinstance(target, _ast.Name) and target.id == 'INDUSTRY_TAXONOMY':
                                taxonomy = eval(compile(
                                    _ast.Expression(body=node.value), path, 'eval'
                                ))
                                result = {sub: info["industries"] for sub, info in taxonomy.items()}
                                logger.info(f"Loaded gateway taxonomy: {len(result)} sub_industries from {path}")
                                return result
            except Exception as e:
                logger.warning(f"Failed to parse gateway taxonomy from {path}: {e}")

    logger.warning("Gateway taxonomy not found — sub_industry validation will be skipped")
    return {}


_VALID_SUB_INDUSTRIES = _load_gateway_taxonomy()

# Case-insensitive lookup: lowercase → canonical name (as it appears in the taxonomy)
_VALID_SUB_LOWER: Dict[str, str] = {k.lower(): k for k in _VALID_SUB_INDUSTRIES}

# Map of known-bad sub_industry values → correct taxonomy values.
# Covers values the scraper historically generated + values seen in gateway rejections.
# Exact-match correction map (stored in canonical Title Case).
# The lookup function below normalises input before checking.
_INVALID_SUB_INDUSTRY_MAP_RAW: Dict[str, str] = {
    # From gateway rejection logs
    "Real Estate Services":              "Commercial Real Estate",
    "Health Care Services":              "Home Health Care",
    "Legal Services":                    "Legal",
    "Law Firm":                          "Legal",
    "Farming & Agriculture":             "Agriculture",
    "Apparel & Textile Manufacturing":   "Textiles",
    # From old _sub_fixes that were invalid
    "IT Services":                       "Information Services",
    "Digital Advertising":               "Advertising Platforms",
    "Education Services":                "Continuing Education",
    "General Construction":              "Construction",
    # Other common mismatches the scraper may produce
    "Enterprise SaaS":                   "SaaS",
    "PropTech":                          "Property Technology",
    "InsurTech":                         "InsurTech",
    "RegTech":                           "RegTech",
    "LegalTech":                         "Legal Tech",
    "CleanTech":                         "Clean Energy",
    "FoodTech":                          "Food Processing",
    "HealthTech":                        "Health Care",
    "HR Tech":                           "Human Resources",
    "Supply Chain":                      "Supply Chain Management",
    "Logistics & Supply Chain":          "Supply Chain Management",
    "Digital Health":                    "Health Care",
    "Mental Health":                     "Mental Health",
    "Renewable Energy":                  "Renewable Energy",
    "Oil & Gas":                         "Oil and Gas",
    "Real Estate Technology":            "Property Technology",
    "Property Management":               "Property Management",
    "Wealth Management":                 "Wealth Management",
}

# Build case-insensitive version: lowercase key → valid taxonomy value
_INVALID_SUB_INDUSTRY_MAP: Dict[str, str] = {
    k.lower(): v for k, v in _INVALID_SUB_INDUSTRY_MAP_RAW.items()
}

# Keyword-based fallback: if the sub_industry text CONTAINS one of these
# keywords (checked in order, longest first), map it to the valid value.
# This catches variations like "real estate consulting", "health care service" (singular), etc.
_SUB_INDUSTRY_KEYWORD_MAP: List[Tuple[str, str]] = [
    # (keyword_lowercase, valid_taxonomy_sub_industry)
    # Order matters: more specific keywords first
    ("real estate",          "Commercial Real Estate"),
    ("health care",          "Health Care"),
    ("healthcare",           "Health Care"),
    ("legal",                "Legal"),
    ("law",                  "Legal"),
    ("construction",         "Construction"),
    ("agriculture",          "Agriculture"),
    ("farming",              "Agriculture"),
    ("manufacturing",        "Industrial Manufacturing"),
    ("insurance",            "Insurance"),
    ("accounting",           "Accounting"),
    ("advertising",          "Advertising"),
    ("marketing",            "Marketing"),
    ("consulting",           "Consulting"),
    ("education",            "Education"),
    ("software",             "Enterprise Software"),
    ("hospitality",          "Hospitality"),
    ("restaurant",           "Restaurants"),
    ("staffing",             "Recruiting"),
    ("recruiting",           "Recruiting"),
    ("logistics",            "Logistics"),
    ("transportation",       "Automotive"),
    ("energy",               "Energy"),
    ("telecom",              "Telecommunications"),
    ("blockchain",           "Blockchain"),
    ("ecommerce",            "E-Commerce"),
    ("e-commerce",           "E-Commerce"),
    ("gaming",               "Gaming"),
    ("biotech",              "Biotechnology"),
    ("cyber",                "Cyber Security"),
    ("security",             "Security"),
    ("fintech",              "FinTech"),
    ("food",                 "Food and Beverage"),
    ("media",                "Digital Media"),
    ("architecture",         "Architecture"),
]


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL VALIDATION  (mirrors validator Stage 0: Hardcoded Checks)
# ─────────────────────────────────────────────────────────────────────────────
# The validator runs 4 email checks in Stage 0 before any API calls:
#   1. check_email_regex — format + no "+" alias
#   2. check_name_email_match — first/last name must appear in local part
#   3. check_general_purpose_email — reject info@, hello@, contact@, etc.
#   4. check_free_email_domain — reject gmail, yahoo, hotmail, etc.
# Plus check_disposable in Stage 0.5 — reject throwaway domains.
# We replicate ALL of these so bad emails never waste a submission slot.

_EMAIL_FORMAT_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Exact match with validator's general_purpose_prefixes list
_GENERAL_PURPOSE_PREFIXES = {
    "info", "hello", "owner", "ceo", "founder", "contact", "support",
    "team", "admin", "office", "mail", "connect", "help", "hi",
    "welcome", "inquiries", "general", "feedback", "ask", "outreach",
    "communications", "crew", "staff", "community", "reachus", "talk",
    "service",
    # Also from our existing GENERIC_PREFIXES that the validator would catch
    "sales", "marketing", "noreply", "no-reply", "webmaster", "enquiries",
    "enquiry", "press", "media", "hr", "jobs", "careers", "billing",
    "invoice", "legal", "privacy", "security", "abuse", "postmaster",
    "bounce",
}

# Superset of validator's free_domains + our existing SKIP_EMAIL_DOMAINS
_FREE_EMAIL_DOMAINS = {
    # Validator's exact list
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "mail.com", "protonmail.com", "proton.me",
    "icloud.com", "me.com", "mac.com",
    "zoho.com", "yandex.com", "gmx.com", "mail.ru",
    # Additional disposable/temp domains
    "mailinator.com", "temp-mail.org", "guerrillamail.com",
    "10minutemail.com", "yopmail.com", "sharklasers.com", "throwaway.email",
    "maildrop.cc", "trashmail.com", "fakeinbox.com", "discard.email",
    "tempail.com", "guerrillamail.info", "grr.la", "mailnesia.com",
}

_BLOCKED_EMAILS = {
    "test@test.com", "example@example.com", "example@domain.com",
    "user@example.com", "admin@example.com", "test@example.com",
    "email@example.com", "name@domain.com", "your@email.com",
    "name@company.com", "first.last@example.com",
}

_MIN_NAME_MATCH_LENGTH = 3


def _validate_email(
    email: str, first: str, last: str, company_domain: str = ""
) -> Tuple[bool, str]:
    """
    Comprehensive email validation mirroring validator Stage 0 checks.

    Returns (True, "") if valid, (False, reason) if rejected.
    """
    # ── 0. Normalize ────────────────────────────────────────────────────
    email = (email or "").strip().lower().strip(" ,.<>()[]")
    if not email:
        return False, "empty email"

    # ── 1. Format check (RFC-5322 simplified) ───────────────────────────
    if not _EMAIL_FORMAT_RE.match(email):
        return False, f"invalid email format: {email}"

    if "@" not in email:
        return False, "missing @ symbol"

    local, domain = email.split("@", 1)

    # ── 2. Reject "+" alias (validator check_email_regex) ───────────────
    if "+" in local:
        return False, f"email contains '+' alias character: {email}"

    # ── 3. Reject placeholder/test emails ───────────────────────────────
    if email in _BLOCKED_EMAILS:
        return False, f"blocked test/placeholder email: {email}"

    # ── 4. Reject invalid/localhost domains ─────────────────────────────
    if "." not in domain:
        return False, f"invalid domain (no TLD): {domain}"
    if domain in {"localhost", "example.com", "test.com", "domain.com", "company.com"}:
        return False, f"blocked domain: {domain}"

    # ── 5. Reject general-purpose prefixes (validator check) ────────────
    if local in _GENERAL_PURPOSE_PREFIXES:
        return False, f"general purpose email prefix: {local}@"

    # Also check dotted sub-parts: accounts.receivable → "accounts" is generic
    local_parts = re.split(r'[._\-]', local)
    _GENERIC_PARTS = {"noreply", "postmaster", "webmaster", "bounce", "accounts",
                      "billing", "invoice", "abuse", "no-reply"}
    if any(p in _GENERIC_PARTS for p in local_parts):
        return False, f"generic email sub-part: {local}@"

    # ── 6. Reject free/consumer email domains (validator check) ─────────
    if domain in _FREE_EMAIL_DOMAINS:
        return False, f"free consumer email domain: {domain}"

    # ── 7. Reject disposable email domains (best-effort without package) ─
    # The validator uses the `disposable_email_domains` pip package.
    # We check it if available, else fall back to our hardcoded list.
    try:
        from disposable_email_domains import blocklist as _disposable_blocklist
        if domain in _disposable_blocklist:
            return False, f"disposable email domain: {domain}"
    except ImportError:
        pass  # hardcoded list in _FREE_EMAIL_DOMAINS covers the most common ones

    # ── 8. Name-email match (validator check_name_email_match) ──────────
    # Mirrors the validator's exact logic including prefix/shortened matching
    first_norm = re.sub(r'[^a-z0-9]', '', (first or "").lower())
    last_norm = re.sub(r'[^a-z0-9]', '', (last or "").lower())
    local_norm = re.sub(r'[^a-z0-9]', '', local)

    if first_norm and last_norm and local_norm:
        name_match = False

        # Strategy 1: pattern matching (john, doe, johndoe, jdoe, doej)
        patterns = []
        if len(first_norm) >= _MIN_NAME_MATCH_LENGTH:
            patterns.append(first_norm)
        if len(last_norm) >= _MIN_NAME_MATCH_LENGTH:
            patterns.append(last_norm)
        patterns.append(f"{first_norm}{last_norm}")
        if first_norm:
            patterns.append(f"{first_norm[0]}{last_norm}")
            patterns.append(f"{last_norm}{first_norm[0]}")
        patterns = [p for p in patterns if p and len(p) >= _MIN_NAME_MATCH_LENGTH]
        name_match = any(p in local_norm for p in patterns)

        # Strategy 2: shortened name prefix (greg@ matches Gregory)
        if not name_match and len(local_norm) >= _MIN_NAME_MATCH_LENGTH:
            if len(first_norm) >= len(local_norm) and first_norm.startswith(local_norm):
                name_match = True
            if not name_match and len(last_norm) >= len(local_norm) and last_norm.startswith(local_norm):
                name_match = True
            # Reverse: name prefix in local part
            if not name_match:
                for name_str in (first_norm, last_norm):
                    for length in range(_MIN_NAME_MATCH_LENGTH, min(len(name_str) + 1, 7)):
                        if name_str[:length] in local_norm:
                            name_match = True
                            break
                    if name_match:
                        break

        if not name_match:
            return False, f"name '{first} {last}' not found in email local part '{local}'"

    # ── 9. Email domain must relate to company website domain ────────────
    # Allows: subdomains (us.stripe.com), sibling TLDs (hubspot.io vs hubspot.com),
    # and extended domains (stripepayments.com vs stripe.com).
    if company_domain:
        cd = _strip_www(company_domain.lower())
        if domain != cd:
            # Check subdomain: us.stripe.com ends with .stripe.com
            if domain.endswith("." + cd):
                pass  # subdomain — allowed
            else:
                # Check root-level similarity: strip TLD and compare roots
                email_root = domain.split(".")[0]
                company_root = cd.split(".")[0]
                if company_root and email_root:
                    if company_root in email_root or email_root in company_root:
                        pass  # root overlap (stripe in stripepayments, or hubspot in hubspot)
                    else:
                        return False, f"email domain '{domain}' doesn't match company domain '{cd}'"
                else:
                    return False, f"email domain '{domain}' doesn't match company domain '{cd}'"

    # ── 10. SMTP mailbox verification (free, best-effort) ────────────────
    # Connects to the MX server and checks if the mailbox exists via RCPT TO.
    # Only rejects when the server explicitly says "mailbox not found" (550).
    # Catch-all servers, timeouts, and errors → allow (benefit of doubt).
    smtp_ok, smtp_reason = _smtp_verify_email(email)
    if not smtp_ok:
        return False, f"SMTP verification failed: {smtp_reason}"

    return True, ""


_FAKE_NAMES = {
    "lets speak", "info team", "admin", "the team", "our team",
    "sales team", "support team", "customer service", "web master",
    "no reply", "do not reply", "test user", "sample user",
    "first last", "john doe", "jane doe", "example user",
    "media inquiries", "press inquiries", "general inquiries",
    "media relations", "press relations", "public relations",
    "customer support", "technical support", "help desk",
    "human resources", "investor relations",
}


def _validate_lead(lead: Dict[str, Any]) -> bool:
    """
    Step 10: Final validation pass to catch bad data.
    Returns False if the lead should be rejected.
    """
    # ❌ source_url must pass gateway validation (denylist, domain age, reachability, redirect)
    source_url = lead.get("source_url") or lead.get("website") or ""
    if not source_url:
        logger.warning(f"Rejected lead: missing source_url — {lead.get('business', '?')}")
        return False
    if not validate_source_url(source_url):
        logger.warning(f"Rejected lead: source_url failed validation — {lead.get('business', '?')} ({source_url})")
        return False

    # ❌ LinkedIn URLs required (validator Stage 4 rejects leads without them)
    li_person = (lead.get("linkedin") or "").strip()
    li_company = (lead.get("company_linkedin") or "").strip()

    if not li_person or "linkedin.com/in/" not in li_person.lower():
        logger.warning(f"Rejected lead: missing/invalid personal LinkedIn — {lead.get('business', '?')}")
        return False

    if not li_company or "linkedin.com/company/" not in li_company.lower():
        logger.warning(f"Rejected lead: missing/invalid company LinkedIn — {lead.get('business', '?')}")
        return False

    # ❌ LinkedIn company slug must be consistent with business name
    # Prevents submitting a company_linkedin that points to a different company
    biz = lead.get("business", "")
    if biz and li_company:
        if not _biz_matches_linkedin_slug(biz, li_company):
            logger.warning(
                f"Rejected lead: company LinkedIn mismatch — business='{biz}' "
                f"vs slug={li_company.split('/')[-1]}"
            )
            return False

    # ❌ Personal LinkedIn must be /in/ not /company/ (wrong field)
    if "linkedin.com/company/" in li_person.lower():
        logger.warning(f"Rejected lead: personal LinkedIn is a company page — {li_person}")
        return False

    # ❌ Role must be non-empty and ≥ 2 chars (gateway min_length=2 in role_patterns.json)
    role = (lead.get("role") or "").strip()
    if len(role) < 2:
        logger.warning(
            f"Rejected lead: role too short or empty ('{role}') — "
            f"{lead.get('business', '?')}"
        )
        return False

    # ❌ Role must not contain company name (gateway rejects role_contains_company_name)
    role_lower = role.lower()
    biz_for_role = (lead.get("business") or "").strip()
    if biz_for_role and len(biz_for_role) >= 3:
        biz_lower_check = biz_for_role.lower()
        if biz_lower_check in role_lower:
            # Strip company name from role: "President, Starburst" → "President"
            role = re.split(r'\s*[,\-–—]\s*' + re.escape(biz_for_role), role, flags=re.I)[0].strip()
            role = re.sub(r'\s+' + re.escape(biz_for_role) + r'\s*$', '', role, flags=re.I).strip()
            role = role.rstrip(" ,.@-–—")
            lead["role"] = role
            if len(role) < 2:
                logger.warning(f"Rejected lead: role empty after stripping company name — {biz_for_role}")
                return False

    # ❌ Description must be ≥ 70 characters (gateway rejects desc_too_short)
    desc = (lead.get("description") or "").strip()
    if len(desc) < 70:
        logger.warning(
            f"Rejected lead: description too short ({len(desc)} chars, need 70+) — "
            f"{lead.get('business', '?')}"
        )
        return False

    # ❌ full_name is a fake/generic name
    name_lower = lead.get("full_name", "").lower().strip()
    if name_lower in _FAKE_NAMES:
        logger.warning(f"Rejected fake name: {name_lower}")
        return False

    # ❌ Full email validation (mirrors validator Stage 0 checks)
    email = (lead.get("email") or "").strip().lower()
    lead["email"] = email  # write back normalized
    first = lead.get("first", "").lower().strip()
    last = lead.get("last", "").lower().strip()
    company_domain = _safe_get_domain(lead.get("website") or lead.get("source_url") or "")

    email_ok, email_reason = _validate_email(email, first, last, company_domain)
    if not email_ok:
        logger.warning(f"Rejected email: {email_reason} — {lead.get('business', '?')} ({email})")
        return False

    # ❌ Business name is a page title / nav text, not a real company name
    biz = lead.get("business", "").strip().lower()
    _bad_biz_starts = ["about ", "our ", "what we", "who we", "careers at ",
                       "shadow it", "public company", "official"]
    if any(biz.startswith(p) for p in _bad_biz_starts):
        logger.warning(f"Rejected garbage business name: {lead.get('business')}")
        return False

    # ── Normalize country + hq_country via _normalize_allowed_country ────────
    lead_city = lead.get("city", "")
    lead["country"] = _normalize_allowed_country(lead.get("country", ""), lead_city)
    lead["hq_country"] = _normalize_allowed_country(
        lead.get("hq_country", "") or lead["country"], lead_city
    )
    lead_country = lead["country"]

    # ❌ UAE leads must be Dubai only (gateway rule)
    if lead_country == "United Arab Emirates":
        lead_city_lower = lead_city.lower().strip()
        if lead_city_lower and "dubai" not in lead_city_lower:
            logger.warning(f"Rejected UAE non-Dubai lead: city={lead_city}")
            return False

    # ❌ US leads require city (gateway rejects invalid_region_format)
    if lead_country == "United States":
        if not lead_city.strip():
            logger.warning(f"Rejected US lead: empty city — {lead.get('business', '?')}")
            return False

    # ❌ US leads require state (gateway rejects invalid_region_format)
    if lead_country == "United States":
        lead_state = lead.get("state", "").strip()
        if not lead_state:
            logger.warning(f"Rejected US lead: empty state — {lead.get('business', '?')}")
            return False

    # ❌ city is a country name (wrong field)
    city = lead.get("city", "").lower()
    if city in _COUNTRY_NAMES:
        lead["city"] = ""
        lead["hq_city"] = ""

    # ❌ state is a country name (wrong field)
    state = lead.get("state", "").lower()
    if state in _COUNTRY_NAMES or state in {"uk", "us", "usa"}:
        lead["state"] = ""
        lead["hq_state"] = ""

    # ❌ industry == sub_industry — too vague, make sub_industry more specific
    # All values here MUST exist in gateway/utils/industry_taxonomy.py
    if lead.get("industry") == lead.get("sub_industry"):
        _sub_fixes = {
            "Software":                        "Enterprise Software",
            "Information Technology":          "Information Services",
            "Energy":                          "Energy Management",
            "Health Care":                     "Hospital",
            "Education":                       "Continuing Education",
            "Real Estate":                     "Commercial Real Estate",
            "Manufacturing":                   "Industrial Manufacturing",
            "Advertising":                     "Advertising Platforms",
            "Media and Entertainment":         "Digital Media",
            "Gaming":                          "Video Games",
            "Transportation":                  "Logistics",
            "Financial Services":              "Finance",
            "Professional Services":           "Consulting",
            "Agriculture and Farming":         "Agriculture",
            "Messaging and Telecommunications": "Telecommunications",
            "Food and Beverage":               "Restaurants",
            "Travel and Tourism":              "Hospitality",
            "Commerce and Shopping":           "E-Commerce",
            "Privacy and Security":            "Cyber Security",
            "Data and Analytics":              "Analytics",
            "Biotechnology":                   "Biotechnology",
        }
        fix = _sub_fixes.get(lead.get("industry", ""))
        if fix:
            lead["sub_industry"] = fix

    # ❌ sub_industry must exist in gateway taxonomy (case-insensitive)
    sub = lead.get("sub_industry", "").strip().strip(" ,.\-–—:;/")
    lead["sub_industry"] = sub  # write back cleaned value
    if sub and _VALID_SUB_INDUSTRIES:
        sub_lower = sub.lower()

        # Step A: already valid (case-insensitive)?
        if sub_lower in _VALID_SUB_LOWER:
            # Fix casing to match taxonomy exactly
            sub = _VALID_SUB_LOWER[sub_lower]
            lead["sub_industry"] = sub

        # Step B: known-bad exact match? (case-insensitive)
        elif sub_lower in _INVALID_SUB_INDUSTRY_MAP:
            corrected = _INVALID_SUB_INDUSTRY_MAP[sub_lower]
            logger.info(f"Corrected sub_industry: '{sub}' → '{corrected}'")
            lead["sub_industry"] = corrected
            sub = corrected

        # Step C: keyword fallback — try to rescue via partial text match
        else:
            matched = False
            for keyword, valid_sub in _SUB_INDUSTRY_KEYWORD_MAP:
                if keyword in sub_lower:
                    logger.info(f"Keyword-matched sub_industry: '{sub}' → '{valid_sub}' (keyword: '{keyword}')")
                    lead["sub_industry"] = valid_sub
                    sub = valid_sub
                    matched = True
                    break

            # Step D: nothing worked — reject
            if not matched:
                logger.warning(
                    f"Rejected lead: sub_industry '{sub}' not in gateway taxonomy — "
                    f"{lead.get('business', '?')}"
                )
                return False

    # ❌ industry must be a valid parent for the sub_industry
    ind = lead.get("industry", "").strip().strip(" ,.\-–—:;/")
    sub = lead.get("sub_industry", "").strip().strip(" ,.\-–—:;/")
    if ind and sub and sub in _VALID_SUB_INDUSTRIES:
        valid_parents = _VALID_SUB_INDUSTRIES[sub]
        if ind not in valid_parents:
            lead["industry"] = valid_parents[0]
            logger.info(
                f"Corrected industry: '{ind}' → '{valid_parents[0]}' "
                f"(valid parent for sub_industry '{sub}')"
            )

    # ❌ phone numbers: normalize all
    phones = lead.get("phone_numbers", [])
    if phones:
        normalized = []
        for p in phones:
            n = _normalize_phone(p) if not p.startswith("+") else p
            if n:
                normalized.append(n)
        lead["phone_numbers"] = list(set(normalized))

    return True


# ─────────────────────────────────────────────────────────────────────────────
# SQLITE URL CACHE  (persist scraped domains across restarts, 30-day TTL)
# ─────────────────────────────────────────────────────────────────────────────

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_output", "_url_cache.db")
_db_conn = None


def _get_db():
    """Return (or create) a persistent SQLite connection for the URL cache."""
    global _db_conn
    if _db_conn is None:
        import sqlite3
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _db_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS company_urls (
                domain      TEXT PRIMARY KEY,
                url         TEXT,
                scraped_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                lead_found  INTEGER DEFAULT 0
            )
        """)
        _db_conn.commit()
    return _db_conn


def _cache_is_stale(domain: str) -> bool:
    """
    Return True if this domain should be scraped:
      - never seen before, OR
      - was scraped but no lead found, OR
      - was scraped > 30 days ago
    Return False (skip) if scraped recently WITH a lead found.
    """
    if not domain:
        return True
    try:
        db = _get_db()
        row = db.execute(
            "SELECT lead_found, julianday('now') - julianday(scraped_at) "
            "FROM company_urls WHERE domain=?",
            (domain,),
        ).fetchone()
        if row is None:
            return True                          # never scraped
        lead_found, age_days = row
        if not lead_found:
            return True                          # scraped before, no lead — retry
        if age_days is not None and age_days > 30:
            return True                          # stale — re-scrape
        return False                             # fresh hit, skip
    except Exception:
        return True                              # on DB error always allow scraping


def _cache_mark(domain: str, url: str, lead_found: bool) -> None:
    """Record that a domain was scraped (and whether a lead was found)."""
    if not domain:
        return
    try:
        db = _get_db()
        db.execute(
            """INSERT INTO company_urls (domain, url, lead_found, scraped_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(domain) DO UPDATE SET
                   url=excluded.url,
                   lead_found=excluded.lead_found,
                   scraped_at=excluded.scraped_at""",
            (domain, url, 1 if lead_found else 0),
        )
        db.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# PARALLEL URL SCRAPING  (5 company sites at once — 5× throughput)
# ─────────────────────────────────────────────────────────────────────────────

SCRAPE_BATCH_SIZE = 10   # URLs scraped in parallel per batch


async def _scrape_batch_parallel(
    urls: List[str],
    loop: asyncio.AbstractEventLoop,
) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    """
    Scrape SCRAPE_BATCH_SIZE company URLs concurrently using a ThreadPoolExecutor.
    Returns list of (url, company_info_or_None) tuples.
    Each call to _scrape_company_info is a sync blocking function — safe to run
    in a thread pool since there is no shared mutable state between calls.
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=SCRAPE_BATCH_SIZE) as executor:
        tasks = [
            loop.run_in_executor(executor, lambda u=url: _scrape_company_info(u))
            for url in urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        (url, r if not isinstance(r, Exception) else None)
        for url, r in zip(urls, results)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def get_leads(
    num_leads: int,
    industry: Optional[str] = None,
    region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate B2B leads using Scrapling + DuckDuckGo (FREE, no API keys)."""
    print(f"\n🕷️  Scrapling Lead Engine starting...")
    print(f"   Target: {num_leads} leads | industry={industry or 'any'} | region={region or 'any'}")

    leads:       List[Dict[str, Any]] = []
    tried_urls:  set = set()
    seen_emails: set = set()   # FIX-05: only dedupe by email, not company
    domain_lead_count: Dict[str, int] = {}  # max 2 leads per domain
    MAX_LEADS_PER_DOMAIN = 2

    queries = _build_queries(industry, region)
    print(f"   Searching with {len(queries)} query(ies) via Serper.dev...")

    # FIX-20: use asyncio.get_running_loop() instead of deprecated get_event_loop()
    loop = asyncio.get_running_loop()

    for query in queries:
        if len(leads) >= num_leads:
            break

        print(f"   🔎 Query: {query[:70]}...")
        results = await loop.run_in_executor(
            None, lambda q=query: _search_ddg(q, max_results=20)
        )
        company_urls = _filter_urls(results)
        print(f"   Found {len(company_urls)} candidate URLs")

        # ── Parallel batch scraping (SCRAPE_BATCH_SIZE URLs at once) ─────────
        # 1. Filter out already-tried URLs
        new_urls = [u for u in company_urls if u not in tried_urls]
        for u in new_urls:
            tried_urls.add(u)

        # 2. Filter out domains cached as recently-found (skip re-scraping)
        new_urls = [u for u in new_urls if _cache_is_stale(_safe_get_domain(u))]
        if not new_urls:
            if not company_urls:
                print("   ⚠️  No company URLs found — all results were aggregator sites")
            else:
                print("   ⏭️  All URLs cached — skipping this query batch")
            continue

        for batch_start in range(0, len(new_urls), SCRAPE_BATCH_SIZE):
            if len(leads) >= num_leads:
                break
            batch = new_urls[batch_start : batch_start + SCRAPE_BATCH_SIZE]
            print(f"   🌐 Scraping {len(batch)} URLs in parallel...")

            scraped = await _scrape_batch_parallel(batch, loop)

            for url, company_info in scraped:
                if len(leads) >= num_leads:
                    break
                if company_info is None:
                    continue

                site_domain = company_info.get("domain", "")

                if not company_info.get("business"):
                    print(f"      ⚠️  {url[:40]} — no business name")
                    _cache_mark(site_domain, url, False)
                    continue

                if domain_lead_count.get(site_domain, 0) >= MAX_LEADS_PER_DOMAIN:
                    print(f"      ⚠️  {site_domain} — domain lead limit reached")
                    continue

                contacts = company_info.get("contacts", [])
                emails   = company_info.get("emails", [])
                lead_found_this_domain = False

                # ── Named contacts ──────────────────────────────────────
                if contacts:
                    for contact in contacts:
                        if len(leads) >= num_leads:
                            break
                        if domain_lead_count.get(site_domain, 0) >= MAX_LEADS_PER_DOMAIN:
                            break
                        if not contact.get("email") and emails:
                            contact["email"] = emails[0]
                        c_email = (contact.get("email") or "").lower()
                        if not c_email or c_email in seen_emails:
                            continue
                        lead = _assemble_lead(company_info, contact, industry, region)
                        if lead:
                            seen_emails.add(c_email)
                            leads.append(lead)
                            domain_lead_count[site_domain] = domain_lead_count.get(site_domain, 0) + 1
                            save_lead_immediately(lead)
                            lead_found_this_domain = True
                            print(f"      ✅ {lead['business']} — {lead['full_name']} ({lead['email']})")

                # Record result in SQLite cache
                _cache_mark(site_domain, url, lead_found_this_domain)

            # Small polite delay between parallel batches
            await asyncio.sleep(random.uniform(0.3, 0.8))

    print(f"\n✅ Done: {len(leads)} leads found")
    return leads[:num_leads]


# ─────────────────────────────────────────────────────────────────────────────
# STORAGE
# ─────────────────────────────────────────────────────────────────────────────

LEADS_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_output")


def _ensure_output_dir() -> None:
    os.makedirs(LEADS_OUTPUT_DIR, exist_ok=True)


def save_rejected_lead(lead: Dict[str, Any], reason: str) -> None:
    """Append a rejected lead + reason to rejected_leads.json for manual review."""
    _ensure_output_dir()
    path = os.path.join(LEADS_OUTPUT_DIR, "rejected_leads.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data if isinstance(data, list) else []
    except Exception:
        entries = []
    entries.append({
        **lead,
        "_rejected_reason": reason,
        "_rejected_at": datetime.now(timezone.utc).isoformat(),
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def save_lead_immediately(lead: Dict[str, Any]) -> None:
    """Append a single lead to all_leads.json right away (Ctrl+C safe). Dedupes by email."""
    _ensure_output_dir()
    master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            master: Dict[str, Any] = json.load(f)
    except Exception:
        master = {"sessions": [], "all_leads": [], "total_leads_ever": 0, "last_updated": ""}

    # Deduplicate by email — never store the same email twice
    existing_emails = {l.get("email", "").lower() for l in master.get("all_leads", [])}
    lead_email = lead.get("email", "").lower()
    if lead_email and lead_email in existing_emails:
        return  # Already exists, skip

    master.setdefault("all_leads", []).append(lead)
    master["total_leads_ever"] = len(master["all_leads"])
    master["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)


def save_leads_to_json(
    leads: List[Dict[str, Any]],
    session_meta: Optional[Dict[str, Any]] = None,
) -> str:
    _ensure_output_dir()
    now       = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")

    session_data = {
        "session": {
            "timestamp":   now.isoformat(),
            "total_leads": len(leads),
            "industry":    (session_meta or {}).get("industry"),
            "region":      (session_meta or {}).get("region"),
            "elapsed_sec": (session_meta or {}).get("elapsed"),
        },
        "leads": leads,
    }
    session_path = os.path.join(LEADS_OUTPUT_DIR, f"leads_{timestamp}.json")
    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Session → {session_path}")

    master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            master: Dict[str, Any] = json.load(f)
    except Exception:
        master = {"sessions": [], "all_leads": [], "total_leads_ever": 0, "last_updated": ""}

    master.setdefault("sessions", []).append(session_data["session"])
    # Dedup: save_lead_immediately() already stored leads during scraping.
    # Only add leads whose email is not already in the master list.
    existing_emails = {l.get("email", "").lower() for l in master.get("all_leads", [])}
    for lead in leads:
        lead_email = lead.get("email", "").lower()
        if lead_email and lead_email not in existing_emails:
            master.setdefault("all_leads", []).append(lead)
            existing_emails.add(lead_email)
    master["total_leads_ever"] = len(master["all_leads"])
    master["last_updated"] = now.isoformat()

    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)
    print(f"📚 Master log → {master_path}  (total ever: {master['total_leads_ever']})")

    csv_path = os.path.join(LEADS_OUTPUT_DIR, f"leads_{timestamp}.csv")
    _save_leads_csv(leads, csv_path)
    print(f"📊 CSV → {csv_path}")
    return session_path


def _save_leads_csv(leads: List[Dict[str, Any]], path: str) -> None:
    if not leads:
        return
    fieldnames = [
        "business", "full_name", "first", "last", "email", "role",
        "linkedin", "website", "company_linkedin", "industry", "sub_industry",
        "country", "state", "city", "hq_country", "hq_state", "hq_city",
        "employee_count", "description", "source_url", "source_type",
        "phone_numbers", "socials",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        # Flatten list/dict fields for CSV
        rows = []
        for lead in leads:
            row = dict(lead)
            row["phone_numbers"] = "; ".join(lead.get("phone_numbers") or [])
            row["socials"]       = json.dumps(lead.get("socials") or {})
            rows.append(row)
        writer.writerows(rows)


def print_lead_summary(leads: List[Dict[str, Any]], elapsed: float) -> None:
    print(f"\n{'='*70}")
    print(f"  📋 LEAD SUMMARY  ({len(leads)} leads in {elapsed:.1f}s)")
    print(f"{'='*70}")
    for i, lead in enumerate(leads, 1):
        ok_email = "✅" if lead.get("email")    else "❌"
        ok_li    = "✅" if lead.get("linkedin")  else "⚠️ "
        ok_loc   = "✅" if lead.get("country")   else "⚠️ "
        ok_ph    = "✅" if lead.get("phone_numbers") else "⚠️ "
        ok_soc   = "✅" if lead.get("socials")   else "⚠️ "
        print(f"\n  {i}. {lead.get('business', '?')}")
        print(f"     {ok_email} Email    : {lead.get('email', 'N/A')}")
        print(f"     👤 Contact  : {lead.get('full_name', 'N/A')} ({lead.get('role', 'N/A')})")
        print(f"     {ok_li} LinkedIn : {lead.get('linkedin', 'N/A')}")
        print(f"     🌐 Website  : {lead.get('website', 'N/A')}")
        print(f"     🏭 Industry : {lead.get('industry', 'N/A')} / {lead.get('sub_industry', 'N/A')}")
        print(f"     {ok_loc} Location : {lead.get('city', '')}, {lead.get('state', '')}, {lead.get('country', 'N/A')}")
        print(f"     👥 Employees: {lead.get('employee_count', 'N/A')}")
        print(f"     {ok_ph} Phones   : {', '.join(lead.get('phone_numbers') or []) or 'N/A'}")
        print(f"     {ok_soc} Socials  : {list((lead.get('socials') or {}).keys()) or 'N/A'}")

    req = ["business", "full_name", "first", "last", "email", "role",
           "website", "industry", "sub_industry", "country"]
    print(f"\n{'─'*70}")
    print(f"  📊 GATEWAY VALIDATION")
    print(f"{'─'*70}")
    for lead in leads:
        biz     = lead.get("business", "?")[:30]
        missing = [f for f in req if not lead.get(f)]
        status  = "✅" if not missing else f"⚠️  missing: {', '.join(missing)}"
        print(f"  {status:60s}  {biz}")
    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import time as _time

    parser = argparse.ArgumentParser(description="Scrapling Lead Engine")
    parser.add_argument("--leads",    type=int, default=5,               help="Number of leads (default: 5)")
    parser.add_argument("--industry", type=str, default="Software",      help="Target industry")
    parser.add_argument("--region",   type=str, default="United States", help="Target region")
    parser.add_argument("--outdir",   type=str, default=None,            help="Output directory")
    args = parser.parse_args()

    if args.outdir:
        LEADS_OUTPUT_DIR = args.outdir

    print("=" * 70)
    print("  🕷️  SCRAPLING LEAD ENGINE  (v2 — fixed)")
    print("=" * 70)
    print(f"  Leads    : {args.leads}")
    print(f"  Industry : {args.industry}")
    print(f"  Region   : {args.region}")
    print(f"  Output   : {LEADS_OUTPUT_DIR}/")
    print("=" * 70)

    _partial: List[Dict[str, Any]] = []
    _t0 = _time.time()

    async def _run() -> List[Dict[str, Any]]:
        found = await get_leads(args.leads, industry=args.industry, region=args.region)
        _partial.extend(found)
        return found

    leads: List[Dict[str, Any]] = []
    interrupted = False
    try:
        leads = asyncio.run(_run())
    except KeyboardInterrupt:
        interrupted = True
        master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")
        if os.path.exists(master_path):
            try:
                with open(master_path, "r", encoding="utf-8") as f:
                    leads = json.load(f).get("all_leads", [])[-args.leads:]
            except Exception:
                leads = _partial
        else:
            leads = _partial
        print(f"\n\n⚠️  Ctrl+C — saving {len(leads)} partial lead(s)...")

    elapsed = _time.time() - _t0
    if leads:
        print_lead_summary(leads, elapsed)
        save_leads_to_json(leads, {
            "industry": args.industry,
            "region":   args.region,
            "elapsed":  round(elapsed, 2),
            "interrupted": interrupted,
        })
        print(f"\n{'⚠️  Partial' if interrupted else '✅ Done'}!")
    else:
        print("\n⚠️  No leads found.")
        print("   Tips: try --industry Marketing | --region California")

    print(f"\n📁 {os.path.abspath(LEADS_OUTPUT_DIR)}/")