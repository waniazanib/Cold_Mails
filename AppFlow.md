# App Flow: Multi-Agent Job Discovery & Outreach Drafting Pipeline 
### Framework Stack: Python, CrewAI, LangChain, Groq API, DuckDuckGo Search, BeautifulSoup (Local, Free, & Secure)

---

## 1. System Core Components & Design Choices

* **Orchestration Framework:** **CrewAI** handles agent role-play and task execution. **LangChain** wraps tools and model connections. Agent 1 (Scout) and Agent 2 (Copywriter) run as **two separate CLI invocations**, not one continuous crew, since the manual override step sits between them. SQLite is the state handoff, no `Flow` API needed.
* **LLM Backend:** **Groq API** via `langchain_groq.ChatGroq`. Default model `llama-3.3-70b-versatile`, swappable via `.env` since Groq's catalog changes (check `console.groq.com/docs/models` for current options, faster/cheaper models like the GPT-OSS line are also viable if quality holds up for your use case). Free tier, fast inference, no Anthropic/OpenAI billing.
* **Discovery Engine:** `DuckDuckGoSearchRun`, wrapped in `tenacity` retry with exponential backoff.
* **Contact Page Crawler (new):** Plain `requests` + `BeautifulSoup`, no heavyweight crawling framework. For each domain still missing an email after Scout, hits a small fixed set of likely pages (`/`, `/contact`, `/about`, `/team`, `/careers`) and regex-extracts emails, prioritizing role-based addresses (`hr@`, `careers@`, `jobs@`) over generic ones (`info@`, `contact@`).
* **No Gmail API.** Sending and reply tracking stay fully manual. You send the drafted email yourself, you check your own inbox, you update status by hand with the `track` command.
* **The Relational Database Ledger:** Local **SQLite (`leads.db`)**. Domain normalized to root domain before insert, plus a `rapidfuzz` fuzzy match on `company_name` to catch near-duplicates the URL check misses.
* **Isolated Resume Tracks:** `resume_ai.txt`, `resume_python.txt`, `my_background.txt`, routed by the `category` column.
* **CLI Layer:** `Typer`-based entrypoint, five commands: `scout`, `enrich`, `draft`, `track`, `stats`.
* **Logging:** Rotating file handler via `logging`, no print-statement debugging.

---

## 2. Structural Application Flow

```
             [ User Inputs Location (City, Area) ]
                              │
                              ▼
                 [ Agent 1: Tech Scout ]
        (Queries DuckDuckGo, retry+backoff on failure)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│             The Central SQLite Ledger (leads.db)            │
│        Saves leads, status defaults to 'Pending Email'      │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
              [ Contact Page Crawler (`enrich`) ]
        (requests + BeautifulSoup hit /contact, /about, /team)
        Found email? → status = 'Ready for Draft'
        Not found?   → stays 'Pending Email'
                               │
                               ▼
        [ Manual Override: VS Code SQLite Viewer ]
      (For crawler misses, or manually-sourced LinkedIn leads)
                               │
                               ▼
        (Filters rows with status = 'Ready for Draft')
             [ Agent 2: Outreach Copywriter ]
┌─────────────────────────────────────────────────────────────┐
│   ├─► 'AI Engineer'      ──► Loads resume_ai.txt            │
│ Ingests tone guidelines from my_background.txt              │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
             [ Local 'cold_email_drafts.md' File ]
          (Appends formatted copy blocks, status → 'Drafted')
                               │
                               ▼
        [ Manual: You send it, you check your inbox ]
                               │
                               ▼
        [ 'track' CLI command updates outcome by hand ]
           (Sent → Replied / No Response / Interview)
                               │
                               ▼
            [ 'stats' CLI command: funnel report ]
```

---

## 3. Database Specification (SQLite Schema)

```sql
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    domain TEXT UNIQUE NOT NULL,
    category TEXT CHECK(category IN ('AI Engineer', 'Python Developer')) NOT NULL,
    contact_email TEXT DEFAULT NULL,
    email_source TEXT CHECK(email_source IN ('ddg_snippet', 'crawler', 'manual')) DEFAULT NULL,
    processing_mode TEXT CHECK(processing_mode IN ('Automatic', 'Manual')) NOT NULL,
    status TEXT CHECK(status IN (
        'Pending Email', 'Ready for Draft', 'Drafted',
        'Sent', 'Replied', 'No Response', 'Interview'
    )) NOT NULL DEFAULT 'Pending Email',
    source_query TEXT DEFAULT NULL,
    date_added TEXT DEFAULT CURRENT_TIMESTAMP,
    date_sent TEXT DEFAULT NULL,
    notes TEXT DEFAULT NULL
);
```

### State-Machine Status Guide:

* `Pending Email`: No address found yet, by snippet or crawler. Waiting on manual lookup.
* `Ready for Draft`: Valid email present (any source), category confirmed, primed for Agent 2. Email format regex-validated before this status is allowed.
* `Drafted`: Copy generated and appended to `cold_email_drafts.md`. Never reprocessed.
* `Sent` / `Replied` / `No Response` / `Interview`: Set manually by you via `track`, based on what's actually in your inbox.

---

## 4. Operational Pipeline Checklist

### Phase 1: Interactive Target Discovery (`scout`)

1. Prompts for **City**, optional **Area**.
2. Builds and logs the search string to `source_query`.
3. Tech Scout agent queries DuckDuckGo with retry+backoff, filters directories, normalizes root domains, parses snippets for emails where present, inserts into `leads.db`.

### Phase 2: Automated Contact Page Crawl (`enrich`, new)

1. Pulls every `Pending Email` row.
2. For each domain, requests a small fixed page list (`/`, `/contact`, `/contact-us`, `/about`, `/about-us`, `/team`, `/careers`) with a realistic User-Agent and 5-8s timeout, stops on first email hit.
3. Parses with `BeautifulSoup`, regex-extracts emails, prioritizes `hr@`/`careers@`/`jobs@`/`recruiting@` over generic `info@`/`contact@`.
4. On a hit: writes the email, sets `email_source = 'crawler'`, `status = 'Ready for Draft'`.
5. On a miss: row stays `Pending Email`, falls through to manual lookup.
6. 2-second delay between domains. Catches `ConnectionError`/`Timeout`/`SSLError` per-domain so one dead site doesn't kill the batch.

### Phase 3: Manual Enrichment Override (VS Code SQLite Viewer)

* For rows the crawler couldn't resolve, or companies you found independently on LinkedIn, open the SQLite Viewer extension, paste the email, confirm category, set status to `Ready for Draft`. Regex validation blocks malformed emails at this step.

### Phase 4: Segmented Copy Generation (`draft`)

1. Outreach Copywriter agent queries `Ready for Draft` rows.
2. Routes by category to the matching resume file, reads `my_background.txt` for tone.
3. Generates the email via Groq, appends to `cold_email_drafts.md`, sets status to `Drafted`.

### Phase 5: Manual Outreach Tracking (`track`)

* You send the email yourself. After sending, run `track` to mark `Sent` and stamp `date_sent`. Update to `Replied`, `No Response`, or `Interview` as your inbox changes, based on what you actually see, nothing automated reads your email.

### Phase 6: Funnel Reporting (`stats`)

* Prints counts across every status, scraped → enriched → ready → drafted → sent → replied → interview.

---

## 5. System Parameters & Code Safeguards

1. **DuckDuckGo Retry Logic:** `tenacity` exponential backoff, base 3s, max 3 attempts, failures logged not swallowed.
2. **Contact Crawler Etiquette:** 2s delay between domains, realistic User-Agent, per-domain timeout, graceful skip on connection failure rather than crash.
3. **Domain Deduplication:** Root-domain normalization plus `rapidfuzz` fuzzy match on `company_name`.
4. **Email Validation Guardrail:** Regex check enforced at the DB layer before any row reaches `Ready for Draft`, regardless of source.
5. **Local Workspace Isolation:** Drafts write to local files only. No outbound mail, no inbox access, sending and tracking are entirely in your hands.
6. **Logging:** Rotating file handler for all agent and crawler runs.
7. **Testing:** `pytest` suite for `db.py` using in-memory SQLite, independent of any LLM or network calls.

---

## 6. Project Structure (VS Code)

```
job-outreach-agent/
├── .env                      # GROQ_API_KEY
├── .gitignore
├── requirements.txt
├── data/
│   └── leads.db
├── resumes/
│   ├── resume_ai.txt
│   ├── resume_python.txt
│   └── my_background.txt
├── output/
│   └── cold_email_drafts.md
├── src/
│   ├── db.py
│   ├── search_tool.py        # DDG wrapper w/ retry
│   ├── crawler.py            # requests + BeautifulSoup contact-page lookup
│   ├── agents.py              # CrewAI agent + task definitions (Groq-backed)
│   ├── crew_scout.py
│   ├── crew_copywriter.py
│   └── cli.py                  # Typer entrypoint: scout / enrich / draft / track / stats
└── tests/
    └── test_db.py
```