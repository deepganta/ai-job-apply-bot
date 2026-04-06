"""
AI-powered helpers using Claude Haiku.

Two jobs:
  1. assess_eligibility()  — deep-read a job description to confirm eligibility
                             (runs after the fast heuristic pass, only for jobs
                              that already passed keyword/regex checks)
  2. select_answer()       — pick the best answer for an unknown screening question
                             from a list of options
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Tuple

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

# ── Candidate context (static, matches config/candidate_profile.json) ────────

_CANDIDATE_CONTEXT = """
Candidate profile:
- Visa: OPT (F-1) — fully authorized to work in the US, zero sponsorship needed
- Employment type: C2C contract only (corp-to-corp)
- Experience: ~4 years in AI/ML/GenAI/NLP/LLM production systems
- Skills: Python, LLMs, RAG, NLP, ML pipelines, GenAI applications
- Location: Remote preferred
""".strip()

# ── Eligibility assessment ────────────────────────────────────────────────────

_ELIGIBILITY_SYSTEM = f"""You are a job eligibility screener. Your job is to decide if the
following job posting is suitable for this specific candidate:

{_CANDIDATE_CONTEXT}

DISQUALIFY if ANY of these are clearly present:
- Requires US citizenship or green card (words like "USC only", "GC only", "must be a US citizen",
  "permanent resident required")
- Requires any security clearance (secret, top secret, TS/SCI, public trust)
- Explicitly says "No C2C", "W2 only", "no corp to corp", "no contractors", "no third party"
- Role is NOT AI/ML (e.g. pure Data Engineer without ML component, DevOps, frontend, QA,
  network engineering, audio engineering, field service)
- Core experience requirement is explicitly MORE THAN 5 years
- Explicitly targets H1B transfer only or requires green card sponsorship from candidate

QUALIFY if:
- C2C is explicitly allowed, OR employment type is not mentioned
- Role involves AI, ML, GenAI, LLM, NLP, or ML-adjacent engineering
- Work authorization language is compatible with OPT (e.g. "authorized to work" without
  citizenship requirement)
- Experience requirement is 5 years or less, or not specified

Respond ONLY with a JSON object (no extra text):
{{"eligible": true, "reason": "brief reason"}}
or
{{"eligible": false, "reason": "brief reason"}}"""


def assess_eligibility(title: str, description: str) -> Tuple[bool, str]:
    """
    Use Claude Haiku to deep-read a job posting and determine eligibility.
    Called only after the fast heuristic pass has already confirmed the job
    looks like an AI/ML role without obvious blockers.

    Returns (eligible, reason). Falls back to (True, ...) on any API error
    so a network hiccup never silently drops a valid job.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return True, "AI check skipped (ANTHROPIC_API_KEY not set)"

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        # Truncate description to keep costs low — 2500 chars is enough for
        # the restrictions and requirements to appear.
        job_text = f"Title: {title}\n\nJob description:\n{description[:2500]}"

        response = client.messages.create(
            model=_MODEL,
            max_tokens=120,
            system=_ELIGIBILITY_SYSTEM,
            messages=[{"role": "user", "content": job_text}],
        )

        raw = response.content[0].text.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            eligible = bool(data.get("eligible", True))
            reason = str(data.get("reason", "")).strip()
            return eligible, reason

        log.warning("AI eligibility: unparseable response — %s", raw[:120])
        return True, "AI response unparseable"

    except Exception as exc:
        log.warning("AI eligibility check failed (%s) — defaulting to eligible", exc)
        return True, f"AI check error: {exc}"


# ── Answer selection ──────────────────────────────────────────────────────────

_ANSWER_SYSTEM = f"""You are filling a job application form for this candidate:

{_CANDIDATE_CONTEXT}

Rules:
- Work authorization / legally authorized to work: always YES (OPT is full work auth)
- Require sponsorship: always NO
- US citizen: NO
- Willing to relocate: NO
- C2C / corp-to-corp: YES
- Veteran: NO
- Disability: NO
- Ethnicity / race: Asian Indian
- Gender: prefer not to say

For SELECT or RADIO questions you will receive a list of available options.
You MUST return EXACTLY one of those options, spelled exactly as shown in the list.
Do not invent a new answer — pick the closest match from the provided options.

Respond ONLY with a JSON object: {{"answer": "your answer here"}}"""


def select_answer(question: str, options: List[str], context: str = "") -> str:
    """
    Use Claude Haiku to select the best answer for an unknown screening question.

    Args:
        question: The question / field label
        options:  List of available answer choices (empty for free-text fields)
        context:  Optional extra context (e.g. question description text)

    Returns the chosen answer string, or "" if the call fails.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        parts = [f"Question: {question}"]
        if context:
            parts.append(f"Context: {context}")
        if options:
            parts.append("Available options:\n" + "\n".join(f"  - {o}" for o in options))
        else:
            parts.append("(Free text field — provide a short, appropriate answer)")

        response = client.messages.create(
            model=_MODEL,
            max_tokens=80,
            system=_ANSWER_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )

        raw = response.content[0].text.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            answer = str(data.get("answer", "")).strip()
            # Validate against options if provided
            if options and answer:
                opts_lower = {o.lower(): o for o in options}
                exact = opts_lower.get(answer.lower())
                if exact:
                    return exact
                # Partial match fallback
                for o_lower, o_orig in opts_lower.items():
                    if answer.lower() in o_lower or o_lower in answer.lower():
                        return o_orig
            return answer

    except Exception as exc:
        log.warning("AI answer selection failed (%s)", exc)

    return ""
