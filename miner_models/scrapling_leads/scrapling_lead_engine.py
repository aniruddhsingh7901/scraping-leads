"""
Scrapling-Powered Lead Generation Engine
=========================================

A FREE drop-in replacement for the expensive Firecrawl + Google Search API.

  OLD (paid)  →  GSE API ($) + Firecrawl ($) + OpenRouter ($)
  NEW (free)  →  DuckDuckGo (free) + Scrapling (free) + regex

Drop-in for: miner_models.lead_sorcerer_main.main_leads.get_leads()

Usage:
    from miner_models.scrapling_leads import get_leads
    leads = asyncio.run(get_leads(5, industry="Software", region="United States"))
"""

import asyncio
import logging
import re
import time
import random
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
LINKEDIN_PROFILE_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/([\w\-_%]+)/?')
LINKEDIN_COMPANY_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/company/([\w\-_%]+)/?')

# Domains to skip — not real business emails
SKIP_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "proton.me", "mailinator.com",
    "temp-mail.org", "guerrillamail.com", "10minutemail.com", "yopmail.com",
    "sharklasers.com", "throwaway.email",
}

# Generic prefixes that are NOT person emails
GENERIC_PREFIXES = {
    "info", "hello", "contact", "support", "admin", "team", "sales",
    "marketing", "noreply", "no-reply", "help", "service", "office",
    "mail", "webmaster", "enquiries", "enquiry", "general", "press",
    "media", "hr", "jobs", "careers", "billing", "invoice", "legal",
    "privacy", "security", "abuse", "postmaster", "bounce",
}

# Valid employee count ranges per the Leadpoet schema
VALID_EMPLOYEE_RANGES = [
    "0-1", "2-10", "11-50", "51-200", "201-500",
    "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+"
]

# Regex to pull employee counts from text
EMPLOYEE_PATTERNS = [
    (re.compile(r'\b(\d{1,3}(?:,\d{3})*)\+?\s*(?:employees?|staff|people|team members?|workers?)\b', re.I), None),
    (re.compile(r'\b(?:employees?|staff|team):\s*(\d[\d,\s\-\+k]+)', re.I), None),
]

# Common paths to check for contact / team info
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/team",
                 "/our-team", "/people", "/leadership", "/management", "/staff",
                 "/company", "/who-we-are", "/meet-the-team"]

# ── PRODUCTION VALIDATION CONSTANTS ──────────────────────────────────────────

# Title words that mean "this is NOT the company name" — use the other part
NAV_TITLE_WORDS = {
    "home", "welcome", "index", "homepage", "main", "front page",
    "page", "untitled", "default", "new", "start",
}

# Words found in navigation menus / UI — NOT valid parts of a person name
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

# Valid US 2-letter state codes (prevents garbage like "UK" as state)
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

# Role normalization (extracted text → gateway-acceptable role)
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

# Search query templates by industry
# NOTE: These use "our team" / "meet the team" to find actual company team pages,
#       NOT lead-gen aggregator sites (Apollo, Rocketreach, ContactOut, etc.)
INDUSTRY_QUERIES: Dict[str, List[str]] = {
    "Software":           ['"software company" "our team" "CEO" OR "Founder" email',
                           '"software startup" "meet the team" "about us" email'],
    "SaaS":               ['"SaaS" "our team" CEO founder email company',
                           '"SaaS company" "about us" founder president email'],
    "Technology":         ['"technology company" "our team" CEO founder email',
                           '"tech company" "about us" "founded" email contact'],
    "Financial Services": ['"financial services" "our team" CEO founder email',
                           '"fintech company" "about us" president email contact'],
    "Health Care":        ['"healthcare company" "our team" CEO founder email',
                           '"health startup" "about us" founder president email'],
    "Consulting":         ['"consulting firm" "our team" partner founder email',
                           '"management consulting" "about us" "founded" email contact'],
    "Real Estate":        ['"real estate company" "our team" CEO owner email',
                           '"property company" "about us" founder email contact'],
    "Marketing":          ['"marketing agency" "our team" CEO founder email',
                           '"digital marketing" "about us" "founded" email contact'],
    "Education":          ['"education company" "our team" CEO founder email',
                           '"edtech" "about us" founder president email'],
    "Manufacturing":      ['"manufacturing company" "our team" CEO president email',
                           '"industrial company" "about us" founder email'],
    "Advertising":        ['"advertising agency" "our team" CEO founder email',
                           '"ad agency" "about us" founded email contact'],
    "E-Commerce":         ['"ecommerce company" "our team" CEO founder email',
                           '"online store" "about us" founder president email'],
    "Biotechnology":      ['"biotech company" "our team" CEO founder email',
                           '"life sciences" "about us" founder president email'],
    "Artificial Intelligence": ['"AI company" "our team" CEO founder email',
                                '"machine learning company" "about us" founder email'],
}
DEFAULT_QUERIES = [
    '"{industry} company" "our team" CEO founder email',
    '"{industry}" "about us" founder president email contact',
]

# ─────────────────────────────────────────────────────────────────────────────
# HELPER UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _safe_get_domain(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lstrip("www.")
    except Exception:
        return ""


def _is_business_email(email: str) -> bool:
    """Return True if this looks like a real business email (not generic/disposable)."""
    email = email.lower().strip()
    parts = email.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if domain in SKIP_EMAIL_DOMAINS:
        return False
    if local in GENERIC_PREFIXES:
        return False
    # Must have a name-like local part (at least 2 chars, not all digits)
    if len(local) < 2 or local.isdigit():
        return False
    return True


def _extract_emails(text: str) -> List[str]:
    """Extract all valid business emails from a block of text."""
    found = EMAIL_REGEX.findall(text)
    return [e for e in found if _is_business_email(e)]


def _extract_linkedin_profiles(text: str) -> List[str]:
    return list(dict.fromkeys(LINKEDIN_PROFILE_RE.findall(text)))


def _extract_linkedin_companies(text: str) -> List[str]:
    return list(dict.fromkeys(LINKEDIN_COMPANY_RE.findall(text)))


def _normalize_employee_count(raw: str) -> str:
    """Map a raw employee count string to a valid Leadpoet range."""
    try:
        n = int(re.sub(r"[^\d]", "", raw.split("-")[0].split("+")[0].replace("k", "000").replace("K", "000")))
    except Exception:
        return ""
    for rng in VALID_EMPLOYEE_RANGES:
        lo_str = rng.split("-")[0].replace(",", "").replace("+", "")
        hi_str = rng.split("-")[1].replace(",", "") if "-" in rng else "999999999"
        lo, hi = int(lo_str), int(hi_str)
        if lo <= n <= hi:
            return rng
    if n >= 10001:
        return "10,001+"
    return ""


def _guess_employee_count_from_text(text: str) -> str:
    for pat, _ in EMPLOYEE_PATTERNS:
        m = pat.search(text)
        if m:
            return _normalize_employee_count(m.group(1))
    return ""


def _split_name(full_name: str) -> Tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _is_valid_person_name(name: str) -> bool:
    """
    Return True only if `name` looks like a real "First Last" person name.

    Rules:
      • Exactly 2 words (handles "John Smith", rejects "Capital Futures About")
      • Each word 2-30 chars, starts with a capital letter
      • No digits in any word
      • Neither word is a known navigation/UI word (NAV_WORDS)
    """
    parts = name.strip().split()
    if len(parts) != 2:
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
    return True


def _normalize_role(role: str) -> str:
    """
    Normalize a raw role string to a gateway-acceptable form.

    E.g.  "partner"  →  "Partner"
          "ceo"      →  "CEO"
          "co-founder and cto" → "Co-Founder and CTO"
    """
    if not role:
        return role
    key = role.strip().lower()
    # Exact match in normalization table
    if key in ROLE_NORMALIZE:
        return ROLE_NORMALIZE[key]
    # Partial match — replace known substrings
    result = role.strip()
    for k, v in ROLE_NORMALIZE.items():
        pattern = re.compile(r'\b' + re.escape(k) + r'\b', re.IGNORECASE)
        result = pattern.sub(v, result)
    # Final fallback: title-case anything not matched
    if result == role.strip():
        result = role.strip().title()
    return result


def _build_email_candidates(first: str, last: str, domain: str) -> List[str]:
    """Generate common business email patterns for a name + domain."""
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f or not l:
        return []
    candidates = [
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f[0]}.{l}@{domain}",
        f"{f}@{domain}",
        f"{l}.{f}@{domain}",
        f"{l}{f[0]}@{domain}",
    ]
    return candidates


def _ddg_find_linkedin(first: str, last: str, company_name: str) -> str:
    """
    Use DuckDuckGo to find a person's LinkedIn /in/ URL when the company
    team page doesn't link it directly.

    Query:  "First Last" "Company Name" site:linkedin.com/in
    Returns a full https://www.linkedin.com/in/SLUG URL, or "" if not found.

    Called as a LAST RESORT inside _extract_person_cards — only when neither
    the page HTML nor the tight-window proximity search found a LinkedIn slug.
    A small random delay is added to avoid DDG rate-limiting.
    """
    if not first or not last:
        return ""

    name = f"{first} {last}"
    # With company name → more precise; without → broader but still useful
    if company_name:
        query = f'"{name}" "{company_name}" site:linkedin.com/in'
    else:
        query = f'"{name}" site:linkedin.com/in'

    try:
        import time as _t
        _t.sleep(random.uniform(0.5, 1.2))   # polite delay — DDG rate-limits on bursts
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        first_l = first.lower()
        last_l  = last.lower()

        # Pass 1: prefer slugs that contain the person's first or last name
        # (e.g. "john-smith-abc123" contains "john" and "smith")
        for r in results:
            url = r.get("href") or r.get("url", "")
            if not url:
                continue
            m = LINKEDIN_PROFILE_RE.search(url)
            if not m:
                continue
            slug = m.group(1)
            slug_norm = slug.lower().replace("-", "").replace("_", "")
            if first_l in slug_norm or last_l in slug_norm:
                return f"https://www.linkedin.com/in/{slug}"

        # Pass 2: accept the first linkedin.com/in URL even without name match
        # (company + name query is already specific enough)
        for r in results:
            url = r.get("href") or r.get("url", "")
            if not url:
                continue
            m = LINKEDIN_PROFILE_RE.search(url)
            if m:
                return f"https://www.linkedin.com/in/{m.group(1)}"

    except Exception as e:
        logger.debug(f"DDG LinkedIn search failed for '{name}': {e}")

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH PHASE — Find company URLs using DuckDuckGo
# ─────────────────────────────────────────────────────────────────────────────

def _build_queries(industry: Optional[str], region: Optional[str]) -> List[str]:
    """Build DuckDuckGo search queries from industry and region."""
    templates = INDUSTRY_QUERIES.get(industry or "", DEFAULT_QUERIES)
    queries = []
    for t in templates:
        q = t.format(industry=industry or "business")
        if region:
            q += f" {region}"
        queries.append(q)
    return queries[:3]  # max 3 queries per run


def _search_ddg(query: str, max_results: int = 15) -> List[Dict[str, Any]]:
    """Search DuckDuckGo and return list of results with title, url, body."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results or []
    except Exception as e:
        logger.warning(f"DDG search failed for '{query}': {e}")
        return []


def _filter_urls(results: List[Dict[str, Any]]) -> List[str]:
    """Extract clean company website URLs from DDG results, skip known non-company sites."""
    skip_domains = {
        "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
        "youtube.com", "reddit.com", "wikipedia.org", "crunchbase.com",
        "glassdoor.com", "indeed.com", "yelp.com", "amazon.com",
        "google.com", "bing.com", "yahoo.com", "capterra.com", "g2.com",
        "trustpilot.com", "clutch.co", "github.com", "medium.com",
        "forbes.com", "techcrunch.com", "bloomberg.com", "cnbc.com",
    }
    seen = set()
    urls = []
    for r in results:
        url = r.get("href") or r.get("url", "")
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
# SCRAPING PHASE — Use Scrapling to extract contact info from websites
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_page(url: str, timeout: int = 15) -> Optional[Any]:
    """Fetch a page using Scrapling's Fetcher (fast, HTTP-based)."""
    try:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(url, timeout=timeout, stealthy_headers=True)
        if page and page.status == 200:
            return page
    except Exception as e:
        logger.debug(f"Fetch failed for {url}: {e}")
    return None


def _scrape_company_info(homepage_url: str) -> Dict[str, Any]:
    """
    Scrape a company website to extract:
    - Company name
    - Description
    - Location (city, state, country)
    - LinkedIn company URL
    - Emails
    - Employee count
    - Contact people (name, role, email, linkedin)
    """
    info: Dict[str, Any] = {
        "website": homepage_url,
        "domain": _safe_get_domain(homepage_url),
        "business": "",
        "description": "",
        "city": "",
        "state": "",
        "country": "",
        "hq_city": "",
        "hq_state": "",
        "hq_country": "",
        "company_linkedin": "",
        "employee_count": "",
        "emails": [],
        "contacts": [],   # list of {name, role, email, linkedin}
    }

    # ── Step 1: Scrape the homepage ──────────────────────────────────────────
    page = _fetch_page(homepage_url)
    if not page:
        return info

    full_text = page.get_all_text(separator=" ")
    html_text = str(page.html_content) if hasattr(page, "html_content") else full_text

    # Company name from <title> or <h1>
    title_els = page.css("title")
    if title_els:
        raw_title = title_els[0].text
        # Split on common separators INCLUDING colon (taglines like "Acme: Best SaaS")
        parts = [p.strip() for p in re.split(r'[\|\-–—•·:]', raw_title) if p.strip()]
        name = parts[0] if parts else ""
        # If the first segment is a nav word (e.g. "Home | Company"), use the longest non-nav part
        # Only fall back to a different part if the FIRST part is a known nav word
        # e.g. "Home | Capital Futures" → "Capital Futures"
        # Keep "Capital Futures | Executive Search" → "Capital Futures" (first = company)
        if name.lower() in NAV_TITLE_WORDS and len(parts) > 1:
            non_nav = [p for p in parts if p.lower() not in NAV_TITLE_WORDS]
            name = max(non_nav, key=len) if non_nav else parts[-1]
        info["business"] = name[:80]

    if not info["business"]:
        h1_els = page.css("h1")
        if h1_els:
            info["business"] = h1_els[0].text.strip()[:80]

    # Description from <meta name="description">
    meta_desc = page.css('meta[name="description"]')
    if meta_desc:
        desc = meta_desc[0].attrib.get("content", "")
        info["description"] = desc[:500]

    if not info["description"]:
        # Fallback: first 300 chars of visible text
        info["description"] = full_text[:300].strip()

    # LinkedIn company URL
    company_links = LINKEDIN_COMPANY_RE.findall(html_text)
    if company_links:
        info["company_linkedin"] = f"https://www.linkedin.com/company/{company_links[0]}"

    # Emails on homepage
    info["emails"].extend(_extract_emails(full_text))

    # Employee count from text
    info["employee_count"] = _guess_employee_count_from_text(full_text)

    # ── Step 2: Scrape contact/team/about pages ──────────────────────────────
    scraped_paths = set()
    for path in CONTACT_PATHS[:6]:   # check up to 6 extra pages
        sub_url = homepage_url.rstrip("/") + path
        if sub_url in scraped_paths:
            continue
        scraped_paths.add(sub_url)
        time.sleep(random.uniform(0.5, 1.2))   # polite delay

        sub_page = _fetch_page(sub_url)
        if not sub_page:
            continue

        sub_text = sub_page.get_all_text(separator=" ")
        sub_html = str(sub_page.html_content) if hasattr(sub_page, "html_content") else sub_text

        # Grab any new emails
        new_emails = _extract_emails(sub_text)
        for e in new_emails:
            if e not in info["emails"]:
                info["emails"].append(e)

        # Grab LinkedIn profiles
        profiles = LINKEDIN_PROFILE_RE.findall(sub_html)

        # If no LinkedIn company yet, check here too
        if not info["company_linkedin"]:
            company_links = LINKEDIN_COMPANY_RE.findall(sub_html)
            if company_links:
                info["company_linkedin"] = f"https://www.linkedin.com/company/{company_links[0]}"

        # Try to extract person cards (name + role) using common patterns
        # Pass business name so the DDG LinkedIn fallback can build a precise query
        contacts = _extract_person_cards(sub_page, sub_text, info["domain"], info.get("business", ""))
        for c in contacts:
            if c not in info["contacts"]:
                info["contacts"].append(c)

        # Location clues (city, state, country)
        if not info["city"]:
            loc = _extract_location_from_text(sub_text)
            info.update(loc)

        # If we have enough already, stop early
        if len(info["emails"]) >= 3 and len(info["contacts"]) >= 2:
            break

    # ── Step 3: If still no location, try homepage text ────────────────────
    if not info["city"]:
        loc = _extract_location_from_text(full_text)
        info.update({k: v for k, v in loc.items() if v})

    # ── Step 4: Detect actual industry from description + full text ─────────
    # This overrides the search-query's industry when we can read what the
    # company actually does (e.g. "executive search firm" → Professional Services)
    detect_text = (info["description"] + " " + full_text[:3000])
    info["detected_industry"] = _detect_industry_from_text(detect_text)
    if info["detected_industry"]:
        print(f"      🏭 Detected industry from description: {info['detected_industry']}")

    # ── Step 5: Mirror hq to contact location if blank ─────────────────────
    if not info["hq_country"] and info["country"]:
        info["hq_country"] = info["country"]
        info["hq_state"] = info["state"]
        info["hq_city"] = info["city"]

    return info


def _extract_person_cards(
    page: Any,
    text: str,
    domain: str,
    company_name: str = "",
) -> List[Dict[str, Any]]:
    """
    Try to find person name + role cards on a page.
    Looks for patterns like:
        <h3>John Doe</h3><p>CEO</p>
        John Doe - Chief Executive Officer

    LinkedIn lookup — three strategies in order:
      1. Name-matched slug already present in the page HTML
      2. Tight HTML window (300 chars) around the person's name
      3. DuckDuckGo fallback: "First Last" "Company" site:linkedin.com/in
         (only called when both HTML strategies fail)

    company_name is forwarded to the DDG fallback so the query is specific.
    """
    contacts = []
    html_text = str(page.html_content) if hasattr(page, "html_content") else text

    # Pattern 1: CSS — look for headings near role keywords
    role_keywords = re.compile(
        r'\b(CEO|CTO|CFO|COO|CMO|CPO|President|Founder|Co-Founder|Director|'
        r'VP|Vice President|Head of|Manager|Partner|Principal|Owner|'
        r'Chief Executive|Chief Technology|Chief Financial|Chief Operating|'
        r'Chief Marketing|Chief Product)\b', re.I
    )
    name_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\b')

    # Extract all LinkedIn /in/ profile slugs from the full page HTML once
    # We'll try to associate them with names by proximity in the raw HTML
    all_li_profiles = LINKEDIN_PROFILE_RE.findall(html_text)

    # Search the raw text for lines that have a name + role together
    lines = text.split('\n')
    for i, line in enumerate(lines):
        line = line.strip()
        if not line or len(line) > 120:
            continue

        role_match = role_keywords.search(line)
        if not role_match:
            continue

        # Try to find a name on the same line or adjacent lines
        context = "\n".join(lines[max(0, i-2):i+3])
        name_matches = name_pattern.findall(context)

        for name in name_matches:
            # Skip if the "name" is actually a role title or company name
            if role_keywords.search(name):
                continue
            if not _is_valid_person_name(name):
                continue

            first, last = _split_name(name)
            role = _normalize_role(role_match.group(1))

            # Try to find a matching email for this person
            email = _find_email_for_person(first, last, domain, text)

            # Try to find a LinkedIn profile URL for this person
            # Strategy: look in the HTML near the person's name for a /in/slug
            # whose slug contains their first or last name (case-insensitive)
            linkedin_slug = ""
            first_l = first.lower()
            last_l = last.lower()
            # First try: name-matched slug from page
            for slug in all_li_profiles:
                slug_norm = slug.lower().replace("-", "").replace("_", "")
                if first_l in slug_norm or last_l in slug_norm:
                    linkedin_slug = slug
                    break
            # Second try: look in a tight HTML window around the name
            if not linkedin_slug:
                # Find the name in the HTML and extract nearby href /in/ links
                name_pos = html_text.find(name)
                if name_pos != -1:
                    window = html_text[max(0, name_pos - 300): name_pos + 300]
                    nearby = LINKEDIN_PROFILE_RE.findall(window)
                    if nearby:
                        linkedin_slug = nearby[0]

            # Third try: DuckDuckGo fallback — fire when the page has no /in/
            # links at all (most team pages simply don't link to LinkedIn).
            # Uses:  "First Last" "Company Name" site:linkedin.com/in
            if not linkedin_slug:
                ddg_url = _ddg_find_linkedin(first, last, company_name)
                if ddg_url:
                    m_slug = LINKEDIN_PROFILE_RE.search(ddg_url)
                    if m_slug:
                        linkedin_slug = m_slug.group(1)
                        print(f"         🔗 LinkedIn via DDG: https://www.linkedin.com/in/{linkedin_slug}")

            linkedin_url = (
                f"https://www.linkedin.com/in/{linkedin_slug}"
                if linkedin_slug else ""
            )

            contact = {
                "full_name": name,
                "first": first,
                "last": last,
                "role": role,
                "email": email,
                "linkedin": linkedin_url,
            }
            contacts.append(contact)
            if len(contacts) >= 5:
                return contacts

    return contacts


def _find_email_for_person(first: str, last: str, domain: str, text: str) -> str:
    """
    Try to find a real email for a person by:
    1. Checking if any extracted email matches their name pattern
    2. Returning the first matching email found
    """
    first_l = first.lower()
    last_l = last.lower()

    # Check emails already on the page
    page_emails = _extract_emails(text)
    for email in page_emails:
        local = email.split("@")[0].lower()
        if first_l in local or last_l in local:
            return email

    # Return nothing (email will be empty — that's OK for now)
    return ""


# Words that look like "City, XX" matches but are not real city names
_NOT_A_CITY = {
    "usa", "us", "uk", "united states", "united kingdom", "england",
    "scotland", "wales", "canada", "australia", "germany", "france",
    "india", "singapore", "netherlands", "israel", "europe", "america",
    "worldwide", "global", "international", "online", "remote",
}


def _extract_location_from_text(text: str) -> Dict[str, str]:
    """
    Try to extract city/state/country from raw text.
    Looks for patterns like "San Francisco, CA" or "London, UK".

    Validation:
      • State code must be a real US state abbreviation (US_STATES set)
      • City must not be a known country name / country abbreviation
      • City must be at least 3 characters
    """
    result = {"city": "", "state": "", "country": ""}

    # US city, state pattern — iterate all matches, take first valid one
    us_pattern = re.compile(
        r'\b([A-Z][a-zA-Z\s]{2,25}),\s*([A-Z]{2})\s*(?:\d{5})?\b'
    )
    for m in us_pattern.finditer(text):
        raw_city  = m.group(1).strip()
        raw_state = m.group(2).strip()
        # ── Validate state is a real US state abbreviation ────────────────
        if raw_state not in US_STATES:
            continue
        # ── Validate city is not a country name / abbreviation ────────────
        if raw_city.lower() in _NOT_A_CITY or len(raw_city) < 3:
            continue
        result["city"]    = raw_city
        result["state"]   = raw_state
        result["country"] = "United States"
        return result

    # Country mentions
    country_patterns = [
        (re.compile(r'\b(United States|USA|US)\b', re.I), "United States"),
        (re.compile(r'\b(United Kingdom|UK|England|Scotland|Wales)\b', re.I), "United Kingdom"),
        (re.compile(r'\b(Canada)\b', re.I), "Canada"),
        (re.compile(r'\b(Australia)\b', re.I), "Australia"),
        (re.compile(r'\b(Germany|Deutschland)\b', re.I), "Germany"),
        (re.compile(r'\b(France)\b', re.I), "France"),
        (re.compile(r'\b(India)\b', re.I), "India"),
        (re.compile(r'\b(Singapore)\b', re.I), "Singapore"),
        (re.compile(r'\b(Netherlands|Holland)\b', re.I), "Netherlands"),
        (re.compile(r'\b(Israel)\b', re.I), "Israel"),
    ]
    for pat, country_name in country_patterns:
        if pat.search(text):
            result["country"] = country_name
            break

    return result


# ─────────────────────────────────────────────────────────────────────────────
# INDUSTRY MAPPING
# ─────────────────────────────────────────────────────────────────────────────

# Map broad industry names to (industry, sub_industry) pairs accepted by Leadpoet
INDUSTRY_MAP: Dict[str, Tuple[str, str]] = {
    "software":           ("Software",            "Enterprise Software"),
    "saas":               ("Software",            "SaaS"),
    "technology":         ("Information Technology", "Information Technology"),
    "tech":               ("Information Technology", "Information Technology"),
    "ai":                 ("Artificial Intelligence", "Artificial Intelligence"),
    "artificial intelligence": ("Artificial Intelligence", "Machine Learning"),
    "machine learning":   ("Artificial Intelligence", "Machine Learning"),
    "fintech":            ("Financial Services",   "FinTech"),
    "finance":            ("Financial Services",   "Finance"),
    "financial":          ("Financial Services",   "Financial Services"),
    "healthcare":         ("Health Care",          "Health Care"),
    "health":             ("Health Care",          "Health Care"),
    "health care":        ("Health Care",          "Health Care"),
    "biotech":            ("Biotechnology",        "Biotechnology"),
    "biotechnology":      ("Biotechnology",        "Biotechnology"),
    "consulting":         ("Professional Services", "Consulting"),
    "real estate":        ("Real Estate",          "Real Estate"),
    "marketing":          ("Sales and Marketing",  "Marketing"),
    "advertising":        ("Advertising",          "Advertising"),
    "education":          ("Education",            "Education"),
    "edtech":             ("Education",            "EdTech"),
    "ecommerce":          ("Commerce and Shopping", "E-Commerce"),
    "e-commerce":         ("Commerce and Shopping", "E-Commerce"),
    "manufacturing":      ("Manufacturing",        "Manufacturing"),
    "logistics":          ("Transportation",       "Logistics"),
    "transportation":     ("Transportation",       "Transportation"),
    "energy":             ("Energy",               "Energy"),
    "media":              ("Media and Entertainment", "Media and Entertainment"),
    "gaming":             ("Gaming",               "Gaming"),
    "cybersecurity":      ("Privacy and Security", "Cyber Security"),
    "security":           ("Privacy and Security", "Security"),
    "data":               ("Data and Analytics",   "Analytics"),
    "analytics":          ("Data and Analytics",   "Analytics"),
    "cloud":              ("Software",             "Cloud Computing"),
    "blockchain":         ("Blockchain and Cryptocurrency", "Blockchain"),
    "crypto":             ("Blockchain and Cryptocurrency", "Cryptocurrency"),
    # ── Detected-from-description industries ─────────────────────────────
    "recruiting":         ("Professional Services", "Recruiting"),
    "executive search":   ("Professional Services", "Recruiting"),
    "staffing":           ("Professional Services", "Staffing and Recruiting"),
    "private equity":     ("Financial Services",   "Private Equity"),
    "venture capital":    ("Financial Services",   "Venture Capital"),
    "asset management":   ("Financial Services",   "Asset Management"),
    "investment banking": ("Financial Services",   "Investment Banking"),
    "legal":              ("Professional Services", "Legal"),
    "law firm":           ("Professional Services", "Legal"),
    "accounting":         ("Professional Services", "Accounting"),
    "insurance":          ("Financial Services",   "Insurance"),
    "logistics":          ("Transportation",       "Logistics"),
    "supply chain":       ("Transportation",       "Supply Chain"),
    "construction":       ("Construction",         "Construction"),
    "architecture":       ("Construction",         "Architecture"),
    "hospitality":        ("Travel and Tourism",   "Hospitality"),
    "restaurant":         ("Food and Beverage",    "Restaurants"),
    "food":               ("Food and Beverage",    "Food and Beverage"),
}

# Description-level industry signals — ordered from most specific to least
# (first match wins, so put narrow patterns before broad ones)
DESCRIPTION_INDUSTRY_SIGNALS: List[Tuple[re.Pattern, str]] = [
    # Professional services (must come before software/SaaS)
    (re.compile(r'\bexecutive\s+search\b|\bexec[\s\-]search\b|\bsearch\s+firm\b', re.I), "executive search"),
    (re.compile(r'\bheadhunting\b|\bheadhunter\b|\btalent\s+acquisition\s+firm\b', re.I), "recruiting"),
    (re.compile(r'\brecruiting\s+firm\b|\brecruitment\s+firm\b|\bstaffing\s+firm\b', re.I), "recruiting"),
    (re.compile(r'\bprivate\s+equity\b|\bpe[\s\-]backed\b|\bleveraged\s+buyout\b', re.I), "private equity"),
    (re.compile(r'\bventure\s+capital\b|\bvc\s+firm\b|\bventure\s+fund\b', re.I), "venture capital"),
    (re.compile(r'\blaw\s+firm\b|\battorney\s+at\s+law\b|\blegal\s+services\b', re.I), "law firm"),
    (re.compile(r'\baccounting\s+firm\b|\bcertified\s+public\s+accountant\b|\baudit\s+firm\b', re.I), "accounting"),
    (re.compile(r'\bmanagement\s+consulting\b|\bconsulting\s+firm\b|\bstrategy\s+consulting\b', re.I), "consulting"),
    (re.compile(r'\binsurance\s+(?:company|firm|agency|broker)\b', re.I), "insurance"),
    (re.compile(r'\breal\s+estate\b|\bproperty\s+management\b|\bcommercial\s+property\b', re.I), "real estate"),
    # Technology (broad — after the professional-services signals)
    (re.compile(r'\bfintech\b|\bfinancial\s+technology\b|\bdigital\s+banking\b', re.I), "fintech"),
    (re.compile(r'\bsaas\b|\bsoftware\s+as\s+a\s+service\b', re.I), "saas"),
    (re.compile(r'\bcybersecurity\b|\binformation\s+security\b|\binfosec\b', re.I), "cybersecurity"),
    (re.compile(r'\bartificial\s+intelligence\b|\bmachine\s+learning\b|\bdeep\s+learning\b', re.I), "ai"),
    (re.compile(r'\bhealthcare\b|\bhealth\s+care\b|\bmedical\s+technology\b|\bhealthtech\b', re.I), "healthcare"),
    (re.compile(r'\bbiotech(?:nology)?\b|\blife\s+sciences\b|\bpharmaceutical\b', re.I), "biotech"),
    (re.compile(r'\be[\-\s]commerce\b|\bonline\s+retail\b|\bonline\s+marketplace\b', re.I), "ecommerce"),
    (re.compile(r'\bmarketing\s+agency\b|\bdigital\s+marketing\b|\badvertising\s+agency\b', re.I), "marketing"),
    (re.compile(r'\beducation\s+technology\b|\bedtech\b|\bonline\s+learning\b', re.I), "edtech"),
    (re.compile(r'\bconstruction\s+(?:company|firm)\b|\bgeneral\s+contractor\b', re.I), "construction"),
    (re.compile(r'\brestaurant\b|\bfood\s+and\s+beverage\b|\bfood\s+service\b', re.I), "restaurant"),
    (re.compile(r'\bhospitality\b|\bhotel\b|\bresort\b', re.I), "hospitality"),
]


def _detect_industry_from_text(text: str) -> Optional[str]:
    """
    Detect actual industry from company description/full-page text.

    Returns a key present in INDUSTRY_MAP, or None if not detected.
    This overrides the search-query's industry when the company's actual
    business can be inferred from what they write about themselves.
    """
    if not text:
        return None
    for pattern, key in DESCRIPTION_INDUSTRY_SIGNALS:
        if pattern.search(text):
            return key
    return None


def _get_industry_pair(industry: Optional[str]) -> Tuple[str, str]:
    """Map a free-form industry string to a valid (industry, sub_industry) pair."""
    if not industry:
        return ("Software", "Enterprise Software")
    key = industry.lower().strip()
    if key in INDUSTRY_MAP:
        return INDUSTRY_MAP[key]
    # Partial match
    for k, v in INDUSTRY_MAP.items():
        if k in key or key in k:
            return v
    return ("Software", "Enterprise Software")


# ─────────────────────────────────────────────────────────────────────────────
# LEAD ASSEMBLY — Turn raw scraped data into a Leadpoet-compliant lead dict
# ─────────────────────────────────────────────────────────────────────────────

def _assemble_lead(
    company_info: Dict[str, Any],
    contact: Dict[str, Any],
    industry: Optional[str],
    region: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    Combine company + contact info into the exact JSON structure
    required by the Leadpoet miner.

    Returns None if required fields are missing.
    """
    # Prefer industry detected from the company's own description/text
    # over the broad search-query industry (e.g. "Software" query but site
    # says "executive search firm" → use Professional Services/Recruiting)
    effective_industry = company_info.get("detected_industry") or industry
    ind, sub_ind = _get_industry_pair(effective_industry)

    email = contact.get("email", "")
    full_name = contact.get("full_name", "")
    first = contact.get("first", "")
    last = contact.get("last", "")
    role = contact.get("role", "")
    linkedin = contact.get("linkedin", "")
    if linkedin and not linkedin.startswith("http"):
        linkedin = f"https://www.linkedin.com/in/{linkedin}"

    # Must have both email and business name
    if not email or not company_info.get("business"):
        return None

    # Determine country / state / city (with region fallback)
    country = company_info.get("country", "")
    state   = company_info.get("state", "")
    city    = company_info.get("city", "")

    if not country and region:
        # Try to infer country from region string
        if re.search(r'\bUS\b|United States|America', region, re.I):
            country = "United States"
        elif re.search(r'\bUK\b|United Kingdom|England', region, re.I):
            country = "United Kingdom"
        else:
            country = region  # Use region as country fallback

    if not country:
        country = "United States"   # safe default

    lead = {
        # ── Person ────────────────────────────────────────────────────────
        "business":         company_info.get("business", "").strip(),
        "full_name":        full_name.strip(),
        "first":            first.strip(),
        "last":             last.strip(),
        "email":            email.lower().strip(),
        "role":             role.strip(),
        "linkedin":         linkedin.strip(),

        # ── Company ───────────────────────────────────────────────────────
        "website":          company_info.get("website", ""),
        "company_linkedin": company_info.get("company_linkedin", ""),
        "description":      company_info.get("description", "")[:500],
        "employee_count":   company_info.get("employee_count", ""),

        # ── Industry ──────────────────────────────────────────────────────
        "industry":         ind,
        "sub_industry":     sub_ind,

        # ── Contact Location ──────────────────────────────────────────────
        "country":          country,
        "state":            state,
        "city":             city,

        # ── HQ Location ───────────────────────────────────────────────────
        "hq_country":       company_info.get("hq_country", country),
        "hq_state":         company_info.get("hq_state", state),
        "hq_city":          company_info.get("hq_city", city),

        # ── Provenance ────────────────────────────────────────────────────
        "source_url":       company_info.get("website", ""),
        "source_type":      "company_site",

        # ── Optional fields ───────────────────────────────────────────────
        "phone_numbers":    [],
        "socials":          {},
    }

    return lead


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — get_leads()
# ─────────────────────────────────────────────────────────────────────────────

async def get_leads(
    num_leads: int,
    industry: Optional[str] = None,
    region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Generate B2B leads using Scrapling (FREE, no API keys needed).

    Strategy:
      1. Search DuckDuckGo for companies in the target industry/region
      2. Scrape each company's website with Scrapling Fetcher
      3. Extract emails, names, roles, LinkedIn URLs via CSS + regex
      4. Return leads in the Leadpoet-required JSON format

    Args:
        num_leads:  How many leads to return
        industry:   Target industry (e.g. "Software", "FinTech")
        region:     Target region  (e.g. "United States", "California")

    Returns:
        List of lead dicts matching the Leadpoet miner schema
    """
    logger.info(f"🔍 Scrapling Engine: requesting {num_leads} leads "
                f"(industry={industry}, region={region})")
    print(f"\n🕷️  Scrapling Lead Engine starting...")
    print(f"   Target: {num_leads} leads | industry={industry or 'any'} | region={region or 'any'}")

    leads: List[Dict[str, Any]] = []
    tried_urls: set = set()
    _seen_emails: set = set()      # per-session email dedup (gateway rejects dups)
    _seen_companies: set = set()   # per-session company dedup (gateway allows 1/company)

    # Build search queries
    queries = _build_queries(industry, region)
    print(f"   Searching with {len(queries)} DuckDuckGo query(ies)...")

    for query in queries:
        if len(leads) >= num_leads:
            break

        print(f"   🔎 Query: {query[:70]}...")
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda q=query: _search_ddg(q, max_results=20)
        )
        company_urls = _filter_urls(results)
        print(f"   Found {len(company_urls)} candidate company URLs")

        for url in company_urls:
            if len(leads) >= num_leads:
                break
            if url in tried_urls:
                continue
            tried_urls.add(url)

            print(f"   🌐 Scraping: {url}")
            try:
                # Run blocking scraping in executor to avoid blocking event loop
                company_info = await asyncio.get_event_loop().run_in_executor(
                    None, lambda u=url: _scrape_company_info(u)
                )
            except Exception as e:
                logger.debug(f"Error scraping {url}: {e}")
                continue

            if not company_info.get("business"):
                print(f"      ⚠️  No business name found, skipping")
                continue

            # ── Try to build leads from contacts + emails ──────────────
            contacts = company_info.get("contacts", [])
            emails = company_info.get("emails", [])

            # If we found contacts with emails, use them directly
            if contacts:
                for contact in contacts:
                    if len(leads) >= num_leads:
                        break

                    # ── Fix 1: Only assign an email if it actually matches the
                    # contact's name — never blindly assign emails[0] to any contact
                    if not contact.get("email"):
                        matched = _find_email_for_person(
                            contact.get("first", ""),
                            contact.get("last", ""),
                            company_info["domain"],
                            " ".join(emails),   # search the pool of page emails
                        )
                        if matched:
                            contact["email"] = matched

                    # ── Fix 5: Require a LinkedIn URL — leads without it get
                    # zero points in gateway scoring and waste a submission slot
                    if not contact.get("linkedin"):
                        print(f"      ⚠️  No LinkedIn for {contact.get('full_name', '?')} — skipping")
                        continue

                    # ── Per-email + per-company dedup (gateway auto-zeros dups) ──
                    c_email = (contact.get("email") or "").lower()
                    c_biz   = company_info.get("business", "").lower()
                    if c_email in _seen_emails or c_biz in _seen_companies:
                        continue
                    lead = _assemble_lead(company_info, contact, industry, region)
                    if lead:
                        # ── Require state for US leads (gateway hard fail) ──────
                        if lead.get("country") == "United States" and not lead.get("state"):
                            print(f"      ⚠️  US lead missing state — skipping: {lead['email']}")
                            continue
                        _seen_emails.add(c_email)
                        _seen_companies.add(c_biz)
                        leads.append(lead)
                        save_lead_immediately(lead)
                        print(f"      ✅ Lead: {lead['business']} — {lead['full_name']} ({lead['email']})")

            # If no named contacts but we have emails, create a minimal lead
            elif emails and len(leads) < num_leads:
                email = emails[0]
                local = email.split("@")[0]
                # Try to guess name from email (e.g. john.doe@company.com → John Doe)
                name_guess = " ".join(
                    w.capitalize() for w in re.split(r'[._\-]', local)
                    if len(w) > 1 and not w.isdigit()
                )
                if len(name_guess.split()) >= 2:
                    first, last = _split_name(name_guess)
                    contact = {
                        "full_name": name_guess,
                        "first": first,
                        "last": last,
                        "role": "Executive",
                        "email": email,
                        "linkedin": "",
                    }
                    lead = _assemble_lead(company_info, contact, industry, region)
                    if lead:
                        leads.append(lead)
                        save_lead_immediately(lead)   # ← saved NOW (Ctrl+C safe)
                        print(f"      ✅ Lead (email-only): {lead['business']} ({lead['email']})")

            # Polite delay between companies
            await asyncio.sleep(random.uniform(1.0, 2.5))

    print(f"\n✅ Scrapling Engine complete: found {len(leads)} leads")
    return leads[:num_leads]


# ─────────────────────────────────────────────────────────────────────────────
# STORAGE — Save leads to JSON for analysis, research & gateway verification
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
from datetime import datetime, timezone

LEADS_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "leads_output")


def _ensure_output_dir():
    os.makedirs(LEADS_OUTPUT_DIR, exist_ok=True)


def save_lead_immediately(lead: Dict[str, Any]) -> None:
    """
    Append a SINGLE lead to all_leads.json right away.
    Called as soon as each lead is found — so Ctrl+C never loses data.
    """
    _ensure_output_dir()
    master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")

    if os.path.exists(master_path):
        try:
            with open(master_path, "r", encoding="utf-8") as f:
                master: Dict[str, Any] = json.load(f)
        except Exception:
            master = {"sessions": [], "all_leads": []}
    else:
        master = {"sessions": [], "all_leads": []}

    master["all_leads"].append(lead)
    master["total_leads_ever"] = len(master["all_leads"])
    master["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)


def save_leads_to_json(leads: List[Dict[str, Any]], session_meta: Optional[Dict[str, Any]] = None) -> str:
    """
    Save all scraped leads to a timestamped JSON file in leads_output/.

    Creates two files:
      1. leads_YYYYMMDD_HHMMSS.json  — the raw leads array
      2. all_leads.json              — running master log (all sessions combined)

    Returns the path to the session file.
    """
    _ensure_output_dir()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")

    # ── 1. Session file ──────────────────────────────────────────────────────
    session_data = {
        "session": {
            "timestamp":    now.isoformat(),
            "total_leads":  len(leads),
            "industry":     session_meta.get("industry") if session_meta else None,
            "region":       session_meta.get("region")   if session_meta else None,
            "elapsed_sec":  session_meta.get("elapsed")  if session_meta else None,
        },
        "leads": leads,
    }

    session_path = os.path.join(LEADS_OUTPUT_DIR, f"leads_{timestamp}.json")
    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    print(f"\n💾 Session saved  → {session_path}")

    # ── 2. Master log (append to all_leads.json) ─────────────────────────────
    master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")
    if os.path.exists(master_path):
        try:
            with open(master_path, "r", encoding="utf-8") as f:
                master: Dict[str, Any] = json.load(f)
        except Exception:
            master = {"sessions": [], "all_leads": [], "total_leads_ever": 0, "last_updated": ""}
    else:
        master = {"sessions": [], "all_leads": [], "total_leads_ever": 0, "last_updated": ""}

    master["sessions"].append(session_data["session"])
    all_leads_list: List[Dict[str, Any]] = master.get("all_leads", [])
    all_leads_list.extend(leads)
    master["all_leads"] = all_leads_list
    master["total_leads_ever"] = len(all_leads_list)
    master["last_updated"] = now.isoformat()

    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)

    print(f"📚 Master log     → {master_path}  (total ever: {master['total_leads_ever']})")

    # ── 3. CSV for easy spreadsheet analysis ─────────────────────────────────
    csv_path = os.path.join(LEADS_OUTPUT_DIR, f"leads_{timestamp}.csv")
    _save_leads_csv(leads, csv_path)
    print(f"📊 CSV export     → {csv_path}")

    return session_path


def _save_leads_csv(leads: List[Dict[str, Any]], path: str):
    """Save leads as a CSV for spreadsheet analysis."""
    import csv
    if not leads:
        return
    fieldnames = [
        "business", "full_name", "first", "last", "email", "role",
        "linkedin", "website", "company_linkedin", "industry", "sub_industry",
        "country", "state", "city", "hq_country", "hq_state", "hq_city",
        "employee_count", "description", "source_url", "source_type",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)


def print_lead_summary(leads: List[Dict[str, Any]], elapsed: float):
    """Pretty-print a summary table of all found leads."""
    print(f"\n{'='*70}")
    print(f"  📋 LEAD SUMMARY  ({len(leads)} leads in {elapsed:.1f}s)")
    print(f"{'='*70}")
    for i, lead in enumerate(leads, 1):
        status_email    = "✅" if lead.get("email") else "❌"
        status_linkedin = "✅" if lead.get("linkedin") else "⚠️ "
        status_location = "✅" if lead.get("country") else "⚠️ "
        print(f"\n  {i}. {lead.get('business', 'Unknown Company')}")
        print(f"     {status_email} Email    : {lead.get('email', 'N/A')}")
        print(f"     👤 Contact  : {lead.get('full_name', 'N/A')} ({lead.get('role', 'N/A')})")
        print(f"     {status_linkedin} LinkedIn : {lead.get('linkedin', 'N/A')}")
        print(f"     🌐 Website  : {lead.get('website', 'N/A')}")
        print(f"     🏭 Industry : {lead.get('industry', 'N/A')} / {lead.get('sub_industry', 'N/A')}")
        print(f"     {status_location} Location : {lead.get('city', '')}, {lead.get('state', '')}, {lead.get('country', 'N/A')}")
        print(f"     👥 Employees: {lead.get('employee_count', 'N/A')}")

    # Validation checklist
    print(f"\n{'─'*70}")
    print(f"  📊 GATEWAY VALIDATION CHECKLIST")
    print(f"{'─'*70}")
    req_fields = ["business", "full_name", "first", "last", "email",
                  "role", "website", "industry", "sub_industry", "country",
                  "city", "linkedin", "company_linkedin"]
    for lead in leads:
        biz = lead.get("business", "?")[:30]
        missing = [f for f in req_fields if not lead.get(f)]
        if missing:
            print(f"  ⚠️  {biz:30s} — missing: {', '.join(missing)}")
        else:
            print(f"  ✅  {biz:30s} — all required fields present")
    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT — run directly for research + analysis
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import time as _time

    parser = argparse.ArgumentParser(
        description="Scrapling Lead Engine — scrape B2B leads & save to JSON/CSV"
    )
    parser.add_argument("--leads",    type=int,   default=5,              help="Number of leads to generate (default: 5)")
    parser.add_argument("--industry", type=str,   default="Software",     help="Target industry (default: Software)")
    parser.add_argument("--region",   type=str,   default="United States",help="Target region (default: United States)")
    parser.add_argument("--outdir",   type=str,   default=None,           help="Output directory (default: leads_output/)")
    args = parser.parse_args()

    if args.outdir:
        LEADS_OUTPUT_DIR = args.outdir

    print("=" * 70)
    print("  🕷️  SCRAPLING LEAD ENGINE")
    print("=" * 70)
    print(f"  Leads    : {args.leads}")
    print(f"  Industry : {args.industry}")
    print(f"  Region   : {args.region}")
    print(f"  Output   : {LEADS_OUTPUT_DIR}/")
    print("=" * 70)

    # Shared state so Ctrl+C handler can access partial leads
    # Use a list container (mutable) so the inner async fn can update it
    _partial_leads: List[Dict[str, Any]] = []
    _t0 = _time.time()

    async def _run():
        found = await get_leads(args.leads, industry=args.industry, region=args.region)
        _partial_leads.extend(found)   # extend (not assign) — no nonlocal needed
        return found

    leads: List[Dict[str, Any]] = []
    interrupted = False

    try:
        leads = asyncio.run(_run())
    except KeyboardInterrupt:
        interrupted = True
        # get whatever was saved to all_leads.json already (via save_lead_immediately)
        master_path = os.path.join(LEADS_OUTPUT_DIR, "all_leads.json")
        if os.path.exists(master_path):
            try:
                with open(master_path, "r", encoding="utf-8") as f:
                    master_data = json.load(f)
                leads = master_data.get("all_leads", [])[-args.leads:]
            except Exception:
                leads = _partial_leads
        else:
            leads = _partial_leads
        print(f"\n\n⚠️  Interrupted by Ctrl+C — saving {len(leads)} lead(s) found so far...")

    elapsed = _time.time() - _t0

    # ── Print summary ────────────────────────────────────────────────────
    if leads:
        print_lead_summary(leads, elapsed)
        save_leads_to_json(leads, {
            "industry": args.industry,
            "region":   args.region,
            "elapsed":  round(elapsed, 2),
            "interrupted": interrupted,
        })
        print(f"\n{'⚠️  Partial run saved' if interrupted else '✅ Done'}!")
    else:
        print(f"\n⚠️  No leads found — nothing saved.")
        print("   Tips:")
        print("   • Try a different --industry  e.g. --industry Marketing")
        print("   • Try a different --region    e.g. --region California")
        print("   • Let it run longer (big companies like BCG hide emails)")

    print(f"\n📁 Output folder: {os.path.abspath(LEADS_OUTPUT_DIR)}/")
    print(f"   - leads_*.json   ← this session's leads (structured)")
    print(f"   - leads_*.csv    ← open in Excel / Google Sheets")
    print(f"   - all_leads.json ← full history of every lead ever found")
