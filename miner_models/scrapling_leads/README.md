# 🕷️ Scrapling Lead Engine

A **FREE, zero-API-key** lead generation engine for the Leadpoet miner (Bittensor Subnet 71).

## Why This Exists

The default `lead_sorcerer_main` model requires **3 paid APIs**:

| Service       | Cost       | Purpose                     |
|---------------|------------|-----------------------------|
| GSE API       | ~$5/1000   | Google search for companies |
| Firecrawl     | ~$15/month | Website scraping            |
| OpenRouter    | ~$0.01/req | LLM scoring                 |

**This engine replaces all of them with FREE alternatives:**

| Service           | Cost  | Purpose                     |
|-------------------|-------|-----------------------------|
| DuckDuckGo Search | FREE  | Company discovery           |
| Scrapling Fetcher | FREE  | Website scraping            |
| Regex + heuristics| FREE  | Contact/email extraction    |

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│  1. SEARCH   DuckDuckGo → find company URLs             │
│  2. SCRAPE   Scrapling Fetcher → homepage + sub-pages   │
│  3. EXTRACT  Regex → emails, names, roles, LinkedIn     │
│  4. ASSEMBLE Format into Leadpoet-compliant JSON        │
└─────────────────────────────────────────────────────────┘
```

### Step 1 — Company Discovery
- Builds targeted queries: `"Software startup CEO founder contact email United States"`
- Uses DuckDuckGo (no API key, no rate limit issues)
- Filters out aggregator sites (LinkedIn, Crunchbase, Forbes, etc.)

### Step 2 — Website Scraping (Scrapling)
- Fetches the company homepage with stealth headers
- Also checks: `/about`, `/team`, `/contact`, `/leadership`, `/people`
- Uses HTTP-based `Fetcher` (fast, no browser needed)

### Step 3 — Contact Extraction
- **Emails**: regex on all visible text, filtered for business-only (no Gmail/Yahoo)
- **Person names**: pattern matching near role keywords (CEO, Founder, Director...)
- **LinkedIn URLs**: both personal (`/in/`) and company (`/company/`)
- **Location**: regex for `City, ST` patterns + country detection
- **Employee count**: mapped to valid Leadpoet ranges

### Step 4 — Lead Assembly
- Maps to the exact Leadpoet JSON schema
- Validates required fields (email + business name)
- Maps industry to valid taxonomy values

---

## Usage

### As a drop-in for `get_leads()`

```python
# Option A: Use directly
from miner_models.scrapling_leads import get_leads
import asyncio

leads = asyncio.run(get_leads(5, industry="Software", region="United States"))
```

### Replace main_leads.py in the miner

In `neurons/miner.py`, change:
```python
# OLD (requires paid API keys):
from miner_models.lead_sorcerer_main.main_leads import get_leads

# NEW (completely free):
from miner_models.scrapling_leads import get_leads
```

### Test it directly

```bash
cd /path/to/leadpoet
python -m miner_models.scrapling_leads.scrapling_lead_engine
```

---

## Output Format

Each lead matches the Leadpoet miner schema:

```json
{
  "business": "Acme Corp",
  "full_name": "John Doe",
  "first": "John",
  "last": "Doe",
  "email": "john.doe@acmecorp.com",
  "role": "CEO",
  "linkedin": "https://www.linkedin.com/in/johndoe",
  "website": "https://acmecorp.com",
  "company_linkedin": "https://www.linkedin.com/company/acme-corp",
  "description": "Acme Corp builds enterprise software...",
  "employee_count": "51-200",
  "industry": "Software",
  "sub_industry": "Enterprise Software",
  "country": "United States",
  "state": "California",
  "city": "San Francisco",
  "hq_country": "United States",
  "hq_state": "California",
  "hq_city": "San Francisco",
  "source_url": "https://acmecorp.com",
  "source_type": "company_site"
}
```

---

## Configuration

No configuration needed. No API keys. No `.env` file required.

**Optional environment variables** (none required):
- None — this engine is fully self-contained.

---

## Performance

| Metric         | Value            |
|----------------|------------------|
| Cost           | $0.00            |
| API keys needed| 0                |
| Leads per run  | Configurable     |
| Time per lead  | ~5-15 seconds    |
| Email quality  | Business-only    |

---

## Limitations & Tips

1. **Email quality**: The engine finds emails that are publicly visible on websites.
   Some companies hide emails — those leads will be skipped.

2. **Throughput**: Each company takes 5-15s to scrape (polite delays included).
   For 10 leads, expect ~2-5 minutes.

3. **Anti-bot**: Scrapling uses stealth headers automatically. Most sites work fine.
   For heavily protected sites, the engine gracefully skips them.

4. **Validation**: The Leadpoet gateway still validates emails via TrueList.
   This engine does basic business-email filtering, not deliverability checking.

---

## Files

```
miner_models/scrapling_leads/
├── __init__.py                  # Exports get_leads()
├── scrapling_lead_engine.py     # Main engine (all logic here)
└── README.md                    # This file
```
