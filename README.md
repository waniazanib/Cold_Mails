<div align="center">

# Cold Mails

**Autonomous job discovery and cold outreach drafting pipeline for AI Engineer roles in Pakistan.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![CrewAI](https://img.shields.io/badge/CrewAI-1.14.7-6366F1?style=flat-square)](https://crewai.com)
[![Ollama](https://img.shields.io/badge/Ollama-Local_LLM-black?style=flat-square&logo=ollama)](https://ollama.com)
[![SQLite](https://img.shields.io/badge/SQLite-Database-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://sqlite.org)
[![Gmail API](https://img.shields.io/badge/Gmail_API-Drafts_Only-EA4335?style=flat-square&logo=gmail&logoColor=white)](https://developers.google.com/gmail)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=flat-square)](LICENSE)

</div>

---

## Overview

Cold Mails is a local, multi-agent pipeline that discovers technology companies in a target city, crawls their contact pages for HR emails, and generates personalized cold outreach emails using a local LLM. Drafts are saved directly to your Gmail Drafts folder with your resume attached. You send them manually.

No cloud LLM billing. No automated sending. No LinkedIn scraping.

```
Scout (DuckDuckGo)  →  Enrich (BeautifulSoup)  →  Draft (CrewAI + Ollama)  →  Gmail Drafts
```

---

## Features

- **Automated discovery** of software houses via DuckDuckGo with retry/backoff and directory filtering
- **Contact page crawler** that tries `/contact`, `/about`, `/team`, `/careers` per domain with robots.txt compliance
- **Fuzzy deduplication** using `rapidfuzz` so the same company under different domains is never double-inserted
- **Local LLM drafting** via Ollama, voice-matched to your background file, under 150 words per email
- **Gmail Drafts integration** with resume attachment and LinkedIn/GitHub footer, nothing is sent automatically
- **SQLite state machine** tracks every lead from `Pending Email` through `Interview`
- **CLI interface** with five commands covering the full pipeline

---

## Tech Stack

| Layer | Technology |
|---|---|
| Orchestration | CrewAI 1.14.7 |
| LLM | Ollama (llama3.2 / llama3.1:8b) |
| Tool wrapping | LangChain Core |
| Discovery | DuckDuckGo Search (ddgs) |
| Crawler | requests + BeautifulSoup4 |
| Retry logic | Tenacity |
| Deduplication | RapidFuzz |
| Database | SQLite (leads.db) |
| Email | Gmail API (compose scope only) |
| CLI | Typer |
| Config | python-dotenv |

---

## Installation

**Prerequisites:** Python 3.11+, [Ollama](https://ollama.com) installed and running

```bash
# 1. Clone the repo
git clone https://github.com/waniazanib/cold-mails.git
cd cold-mails

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Pull a local model
ollama pull llama3.2

# 5. Initialize the database
python src/db.py
```

---

## Environment Variables

Create a `.env` file in the project root:

```env
# Local LLM
OLLAMA_MODEL=llama3.2

# Your profile links (appended to every draft)
LINKEDIN_URL=https://linkedin.com/in/your-handle
GITHUB_URL=https://github.com/your-handle

# Path to your resume PDF (relative to project root)
RESUME_PATH=resumes/Your_Resume.pdf
```

**Gmail setup (one-time):**
1. Enable the Gmail API in [Google Cloud Console](https://console.cloud.google.com)
2. Create an OAuth 2.0 Desktop credential, download as `credentials.json`
3. Place `credentials.json` in the project root
4. Run `python src/gmail_draft.py` once to authorize, a `token.json` is saved and auto-refreshes

---

## Usage

All commands run from the project root:

```bash
# Phase 1: discover companies in a city (prompts for city and area)
python src/cli.py scout

# Phase 2: crawl contact pages for missing emails
python src/cli.py enrich

# View all leads (add --status "Pending Email" to filter)
python src/cli.py leads

# Phase 4: generate drafts and save to Gmail
python src/cli.py draft

# Record an outcome after checking your inbox
python src/cli.py track <lead_id> Sent
python src/cli.py track <lead_id> Replied --notes "Asked for portfolio"
python src/cli.py track <lead_id> Interview

# Funnel stats
python src/cli.py stats
```

**Phase 3 (manual):** For leads the crawler could not resolve, open `data/leads.db` in the VS Code SQLite Viewer extension, paste the contact email, and set status to `Ready for Draft`.

### Pipeline status flow

```
Pending Email  →  Ready for Draft  →  Drafted  →  Sent  →  Replied / No Response / Interview
```

---

## Folder Structure

```
cold-mails/
├── .env
├── credentials.json          # Gmail OAuth (not committed)
├── token.json                # Gmail token, auto-generated (not committed)
├── requirements.txt
├── data/
│   └── leads.db              # SQLite database
├── resumes/
│   ├── resume_ai.txt         # Resume text injected into LLM prompt
│   ├── my_background.txt     # Voice and tone guidelines for the agent
│   └── Your_Resume.pdf       # Attached to every Gmail draft
├── output/
│   └── cold_email_drafts.md  # Local backup of every generated draft
└── src/
    ├── db.py                 # Schema, insert, dedup, status transitions
    ├── search_tool.py        # DuckDuckGo wrapper with retry and filtering
    ├── crawler.py            # Contact page crawler
    ├── agents.py             # CrewAI agent and task definitions
    ├── crew_scout.py         # Phase 1 orchestration
    ├── crew_copywriter.py    # Phase 4 orchestration
    ├── gmail_draft.py        # Gmail Drafts API integration
    └── cli.py                # Typer CLI entrypoint
tests/
    └── test_db.py            # pytest suite for db.py (38 tests, in-memory SQLite)
```

---

## Future Improvements

- **Reviewer agent** that scores each draft before accepting it, auto-regenerates if quality is low
- **Weekly digest** of funnel stats exported to markdown for portfolio tracking
- **Query tuning feedback loop** tracking which `source_query` values produced the most `Ready for Draft` conversions
- **HTTP fallback** in the crawler for sites with broken or missing SSL certificates

---
