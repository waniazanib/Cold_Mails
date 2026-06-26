"""
agents.py
CrewAI agent and task definitions for the job discovery and outreach
pipeline. LLM backend is Groq, via crewai's own LLM class (litellm
under the hood), not langchain_groq, see the note below on why.

Single-track decision: every lead gets the same AI Engineer resume.
The Python Developer track has been dropped. resume_python.txt and the
category-routing logic described in earlier AppFlow.md drafts are no
longer used here. The `category` column in leads.db still technically
allows 'Python Developer' (the CHECK constraint in db.py was never
changed), it just won't be written anymore, every insert from this
pipeline should pass category="AI Engineer". Say the word if you want
that constraint tightened to a single allowed value.

Why crewai.LLM instead of langchain_groq.ChatGroq:
Agent.llm in the installed crewai version only accepts a string model
id or a crewai.llms.base_llm.BaseLLM instance, not an arbitrary
LangChain chat model. Passing a raw ChatGroq object fails pydantic
validation. crewai's own LLM class wraps litellm and reaches Groq via
the "groq/<model>" string convention, that's what's used here.
LangChain still does the tool-wrapping job described in AppFlow.md,
search_tech_companies in search_tool.py is a LangChain tool, adapted
into a crewai-native tool with Tool.from_langchain() below, since
crewai.Agent.tools doesn't accept a raw LangChain tool object either.

Dependencies: crewai, litellm (required for crewai's Groq provider
support, "pip install litellm" or "pip install crewai[litellm]"),
python-dotenv
Expected location: src/agents.py
"""

import logging
import os
from pathlib import Path
from typing import Optional

# Groq does not support litellm's cache_control / cache_breakpoint fields,
# which litellm injects into system messages when it thinks the provider
# supports prompt caching (it does for Anthropic, not for Groq). This env
# var must be set before litellm is imported or used, so it goes here at
# the top of the module before the crewai import pulls litellm in.
os.environ["LITELLM_CACHE"] = "False"

from crewai import LLM, Agent, Task
from crewai.tools.base_tool import Tool
from dotenv import load_dotenv

from search_tool import search_tech_companies

load_dotenv()

logger = logging.getLogger("agents")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

RESUMES_DIR = Path(__file__).resolve().parent.parent / "resumes"
RESUME_AI_PATH = RESUMES_DIR / "resume_ai.txt"
BACKGROUND_PATH = RESUMES_DIR / "my_background.txt"
_llm_instance = None

def get_llm() -> LLM:
    global _llm_instance
    if _llm_instance is None:
        model = os.getenv("OLLAMA_MODEL", "llama3.2")
        _llm_instance = LLM(
            model=f"ollama/{model}",
            base_url="http://localhost:11434",
            temperature=0.4,
        )
    return _llm_instance


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------


def load_text_file(path: Path) -> str:
    """Read a resume/background text file, with a clear error if missing."""
    if not path.exists():
        raise FileNotFoundError(
            f"Expected file not found: {path}. Create it before running "
            f"the copywriter crew."
        )
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Agent: Tech Scout (Phase 1)
# ---------------------------------------------------------------------------

_tech_scout_instance: Optional[Agent] = None


def get_tech_scout() -> Agent:
    """Build (once) and return the Tech Scout agent. Calls get_llm()."""
    global _tech_scout_instance
    if _tech_scout_instance is None:
        _tech_scout_instance = Agent(
            role="Tech Scout",
            goal=(
                "Find real, individual technology company websites (not "
                "directories or aggregators) in a given city/area, returning "
                "each company's name, domain, and any contact email visible "
                "in the search snippet."
            ),
            backstory=(
                "A meticulous researcher who knows the difference between an "
                "actual software house's homepage and a LinkedIn or Glassdoor "
                "listing about one. Never invents a domain or email that "
                "wasn't in the search results, if it isn't there, it reports "
                "that nothing was found."
            ),
            tools=[Tool.from_langchain(search_tech_companies)],
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
        )
    return _tech_scout_instance


def build_scout_task(city: str, area: Optional[str] = None) -> Task:
    """
    Task for a single scout run. Note: the actual DB insertion in
    crew_scout.py should call search_tool.search_companies() directly
    rather than parsing this agent's text output, see the design note
    in search_tool.py about keeping the LLM out of the data path for
    domains and emails. This task is for the agent's own exploration
    and a human-readable summary, not the system of record.
    """
    location = f"{area}, {city}" if area else city
    return Task(
        description=(
            f"Search for technology companies and software houses in "
            f"{location}. Use the search tool with a query like 'software "
            f"houses tech companies in {location}'. Summarize what you "
            f"found: company names, domains, and whether an email was "
            f"visible for each. Do not fabricate any detail not present "
            f"in the tool's output."
        ),
        expected_output=(
            "A short list of companies found, each with its domain and "
            "email if one was present in the search results."
        ),
        agent=get_tech_scout(),
    )


# ---------------------------------------------------------------------------
# Agent: Outreach Copywriter (Phase 4)
# ---------------------------------------------------------------------------

_outreach_copywriter_instance: Optional[Agent] = None


def get_outreach_copywriter() -> Agent:
    """Build (once) and return the Outreach Copywriter agent. Calls get_llm()."""
    global _outreach_copywriter_instance
    if _outreach_copywriter_instance is None:
        _outreach_copywriter_instance = Agent(
            role="Outreach Copywriter",
            goal=(
                "Write a short, specific cold email to a software house that "
                "gets Wania's AI/ML background in front of the right person, "
                "written in her voice, never generic, never longer than it "
                "needs to be."
            ),
            backstory=(
                "A writer who has read Wania's resume and background notes "
                "closely enough to never pad an email with filler, and who "
                "treats every recipient's company as worth one real, specific "
                "sentence rather than a copy-pasted compliment."
            ),
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
        )
    return _outreach_copywriter_instance


def build_draft_task(company_name: str, domain: str) -> Task:
    """
    Task for drafting one cold email. resume_ai.txt and my_background.txt
    are read here and embedded directly in the task description, since
    a CrewAI agent works from its task prompt, not from arbitrary file
    access at run time, there's no file-reading tool involved.
    """
    resume_text = load_text_file(RESUME_AI_PATH)
    background_text = load_text_file(BACKGROUND_PATH)

    return Task(
        description=(
            f"Write a cold outreach email to {company_name} ({domain}), "
            f"applying for an ML Engineer / AI Developer Intern role.\n\n"
            f"--- Candidate resume ---\n{resume_text}\n\n"
            f"--- Candidate voice and tone guidelines ---\n{background_text}\n\n"
            f"Requirements: under 150 words, no generic 'I came across your "
            f"company' filler, reference something plausible about what a "
            f"company like {company_name} likely builds, match the tone "
            f"guidelines exactly, never use an em dash anywhere in the "
            f"output, end with a plain signature and do not repeat contact "
            f"details that would already be in the email header."
        ),
        expected_output=(
            "A complete cold email body, ready to paste into Gmail, "
            "addressed generically without a 'Dear Hiring Manager' cliche, "
            "under 150 words."
        ),
        agent=get_outreach_copywriter(),
    )


# ---------------------------------------------------------------------------
# Manual smoke test (does not call the LLM, construction only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("agents.py imported and parsed without errors.")
    logger.info("Run get_tech_scout() / get_outreach_copywriter() to verify with a real GROQ_API_KEY set.")