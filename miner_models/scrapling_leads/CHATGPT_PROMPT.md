# ChatGPT Architecture Consultation Prompt
## End-to-End Context: B2B Lead Scraping Engine That Must Pass Automated Gateway Validation

---

## 🧠 WHAT I AM BUILDING

I am building a **B2B lead generation miner** for a decentralized AI network (Bittensor Subnet). 

**The flow is:**
1. A "validator" sends my miner a JSON request called an **ICP (Ideal Customer Profile)**
2. My miner must **autonomously scrape the internet** and produce **B2B business leads**
3. The leads are submitted to a **trustless gateway** that runs **automated validation checks**
4. If a lead passes validation, I earn rewards. If it fails, I get zero.

**The constraint:** I am building a **FREE** scraping-based solution (no paid APIs like Apollo, Hunter.io, etc.) using:
- **Scrapling** (Python web scraping library — stealth HTTP requests + CSS/regex parsing)
- **DuckDuckGo Search** (free, no API key)
- **Python regex + CSS selectors** for extraction

---

## 📥 INPUT: What My Miner Receives (ICP Request)

The validator sends my miner a prompt like this:

```json
{
  "industry": "Software",
  "sub_industry": "Enterprise Software",
  "country": "United States",
  "target_roles": ["CEO", "Founder", "Co-Founder", "CTO", "President"],
  "target_seniority": "C-Suite",
  "employee_count_min": 10,
  "employee_count_max": 500,
  "description": "Find B2B software company founders and CEOs in the United States with valid business email addresses."
}
```

---

## 📤 OUTPUT: What My Miner Must Return (Lead Schema)

Each lead must match this **exact schema**:

```json
{
  "business": "Acme Corp",
  "full_name": "John Smith",
  "first": "John",
  "last": "Smith",
  "email": "john.smith@acmecorp.com",
  "role": "CEO",
  "linkedin": "https://www.linkedin.com/in/johnsmith",
  "website": "https://acmecorp.com",
  "company_linkedin": "https://www.linkedin.com/company/acme-corp",
  "description": "Acme Corp builds enterprise SaaS for logistics companies.",
  "employee_count": "51-200",
  "industry": "Software",
  "sub_industry": "Enterprise Software",
  "country": "United States",
  "state": "CA",
  "city": "San Francisco",
  "hq_country": "United States",
  "hq_state": "CA",
  "hq_city": "San Francisco",
  "source_url": "https://acmecorp.com",
  "source_type": "company_site",
  "phone_numbers": [],
  "socials": {}
}
```

**Valid `employee_count` values:** `"0-1"`, `"2-10"`, `"11-50"`, `"51-200"`, `"201-500"`, `"501-1,000"`, `"1,001-5,000"`, `"5,001-10,000"`, `"10,001+"`

---

## 🔒 GATEWAY VALIDATION RULES (Exact Code — These Are the Rules I Must Pass)

The gateway runs these checks. **A lead gets ZERO score if ANY check fails:**

### Check 1: Hard Time Limit
```python
if run_time_seconds > 30:
    return FAIL  # instant zero
```

### Check 2: Industry Fuzzy Match (80% threshold)
```python
score = fuzz.ratio(lead.industry.lower(), icp.industry.lower())
if score < 80:
    return FAIL  # "Software" must match "Software" at 80%+
```

### Check 3: Sub-Industry Fuzzy Match (70% threshold)
```python
score = fuzz.ratio(lead.sub_industry.lower(), icp.sub_industry.lower())
if score < 70:
    return FAIL
```

### Check 4: Role Fuzzy Match (60% threshold)
```python
# Lead role must match ONE of the ICP's target_roles
# Uses partial_ratio — "Chief Executive Officer" can match "CEO"
best_score = max(fuzz.partial_ratio(lead.role, target) for target in icp.target_roles)
if best_score < 60:
    return FAIL  # "Executive" alone would FAIL this check
```

### Check 5: Country Match
```python
if normalize_country(lead.country) != normalize_country(icp.country):
    return FAIL  # "UK" fails if ICP says "United States"
```

### Check 6: Seniority Match (within 1 level)
```python
# Hierarchy: C-Suite(0) > VP(1) > Director(2) > Manager(3) > IC(4)
# Lead must be within 1 level of ICP target
if lead_level > icp_level + 1:
    return FAIL
```

### Check 7: Email Validation
```python
# Format check: must match regex ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$
# Disposable domain check: no mailinator, tempmail, etc.
# Generic prefix check: "noreply", "no-reply", "donotreply", "admin", "info" → FAIL
if email.split('@')[0] in ['noreply', 'no-reply', 'donotreply', 'nobody', 'admin']:
    return FAIL
```

### Check 8: Data Quality
```python
# No placeholder text: test, asdf, lorem, ipsum, foo, bar, demo, temp, null, undefined
# No suspicious chars: < > { } | \ ^ ~ ` [ ]
# No all-numeric business name
# No generic company names: "Company", "Corp", "LLC", "Business"
# No generic roles: "Employee", "Worker", "Staff", "Person"
```

### Check 9: No Duplicate Companies
```python
# First lead per company per evaluation wins
# Second lead from same company → automatic FAIL
if company.lower() in seen_companies:
    return FAIL
```

---

## ❌ CURRENT FAILURES (Real Examples from My Scraper)

### Failed Lead #1
```json
{
  "business": "Toptal",
  "full_name": "Toptal Marketing",
  "first": "Toptal",
  "last": "Marketing",
  "email": "partners@toptal.com",
  "role": "Partner",
  "linkedin": "",
  "city": "",
  "state": "",
  "country": "United States"
}
```

**Why it fails:**
- `full_name = "Toptal Marketing"` → NOT a real person (I split the email prefix "partners" wrong, then fell back to the company name + "Marketing")
- `email = "partners@toptal.com"` → Generic prefix "partners" — not a named person's email
- `role = "Partner"` → May fail 60% fuzzy match against `["CEO", "Founder", "CTO"]`
- `linkedin = ""` → Missing (reduces score even if it passes)
- `city = ""`, `state = ""` → Missing location fields

**Root cause:** The email `partners@toptal.com` has prefix "partners" → I tried to parse it as a name → got "Toptal Marketing" which is nonsense.

---

### Failed Lead #2
```json
{
  "business": "Prospect Wallet: B2B Mailing & Email lists",
  "full_name": "Mohamed Sameer",
  "first": "Mohamed",
  "last": "Sameer",
  "email": "mohamed.sameer@prospectwallet.com",
  "role": "Executive",
  "linkedin": "",
  "city": "USA",
  "state": "UK",
  "country": "United States"
}
```

**Why it fails:**
- `role = "Executive"` → Too generic, will fail 60% partial_ratio match against `["CEO", "Founder", "CTO"]`
- `city = "USA"` → "USA" is a country, not a city — my location regex extracted the wrong data
- `state = "UK"` → "UK" is a country, not a US state — completely wrong
- `linkedin = ""` → Missing
- The company itself is a B2B data broker — not a real "Software" company ICP would want

---

## 🔧 MY CURRENT SCRAPING APPROACH (Summary)

```python
# Step 1: Search DuckDuckGo
queries = ['"software company" "our team" CEO founder email']
results = ddgs.text(query, max_results=20)

# Step 2: Filter URLs (skip LinkedIn, Wikipedia, aggregators)
company_urls = [url for url in results if not in skip_domains]

# Step 3: Scrape each company homepage + contact/about pages
for url in company_urls:
    page = Fetcher.get(url)  # Scrapling HTTP fetch
    emails = EMAIL_REGEX.findall(page.text)
    
    # Try to find person cards in text
    for line in page.text.split('\n'):
        if role_keyword in line:  # CEO, Founder, etc.
            name = extract_name_from_context(line)
            email = match_email_to_name(name, emails)
            leads.append(build_lead(name, email, role))

# Step 4: Save to JSON/CSV
save_leads_to_json(leads)
```

---

## 🎯 WHAT I NEED

I need a **robust end-to-end algorithmic architecture** to achieve **high gateway pass rates**. Specifically I need help with:

### Problem 1: Finding Real Named-Person Emails
- Generic emails like `partners@`, `info@`, `contact@`, `team@` are easy to find but fail validation
- Personal emails like `john.smith@company.com` are what I need
- **How can I reliably find/infer personal emails from a company website using free tools?**

### Problem 2: Extracting Real Person Names + Roles
- Company "About" and "Team" pages have person cards (h3 name + p role) but the HTML varies wildly
- My current regex-on-text approach picks up garbage
- **What's the best CSS selector + HTML parsing strategy for team pages?**

### Problem 3: Location Extraction
- My regex extracts "USA" as city and "UK" as state — completely wrong
- I need to extract `city` (e.g. "San Francisco"), `state` (e.g. "CA"), `country` (e.g. "United States") separately
- **What's a robust free approach to extract city/state from a webpage?**

### Problem 4: Role Matching
- I'm extracting "Executive" as a catch-all role
- The gateway requires 60% fuzzy match against `["CEO", "Founder", "CTO", "President"]`
- **How to reliably extract the exact role title from a team page?**

### Problem 5: LinkedIn Profile Discovery
- I have the person's name + company, but no LinkedIn URL
- LinkedIn blocks direct scraping
- **What free approach can I use to find LinkedIn /in/ profile URLs for a named person?**

### Problem 6: Pre-Validation Before Submission
- I want to run the same checks the gateway runs (above) on MY side before submitting
- **What's the best architecture to self-validate and score a lead before it ever reaches the gateway?**

---

## ⚙️ TECHNICAL CONSTRAINTS

- **Language:** Python 3.10
- **Free tools only:** No Hunter.io, Apollo, ZoomInfo, Clearbit, etc.
- **Scrapling:** A Python library that does HTTP-based stealth fetching (like requests with browser headers) + CSS/XPath parsing
- **DuckDuckGo:** Free search, ~20 results per query, no API key needed
- **No browser automation** (no Playwright/Selenium) — too slow for per-lead < 30s time limit
- **Time budget:** < 30 seconds per lead (gateway hard limit)
- **Must be deterministic** — same input, same behavior

---

## 📊 SUCCESS CRITERIA

A lead is considered **perfect** if it has ALL of these:
- ✅ `email` = personal named email (firstname.last@company.com)
- ✅ `full_name` = real first + last name
- ✅ `role` = matches ICP target_roles with 60%+ fuzzy score
- ✅ `linkedin` = valid `/in/` profile URL
- ✅ `city`, `state`, `country` = real location data
- ✅ `industry` + `sub_industry` = matches ICP at 80%/70%
- ✅ `business` = real company name (not "Company" or "Corp")
- ✅ `employee_count` = valid range string

---

## 💬 MY QUESTION TO YOU (ChatGPT)

**Please propose a robust, step-by-step algorithmic architecture** for a Python scraper (using Scrapling + DuckDuckGo) that reliably produces leads that pass ALL the gateway validation checks above. 

Focus specifically on:
1. The **search query strategy** to find real company team pages (not lead-gen aggregators)
2. The **HTML parsing strategy** to extract real person name + role from team pages
3. The **email discovery strategy** (from page text, footer, contact page, or email pattern inference)
4. The **location extraction strategy** (properly separating city vs state vs country)
5. The **self-validation layer** (run gateway checks on my side before submitting)
6. The **LinkedIn discovery strategy** (free, no scraping LinkedIn directly)

Please give concrete Python pseudocode or algorithms, not just general advice.
