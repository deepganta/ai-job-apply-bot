"""
LinkedIn Posts Scanner
Searches LinkedIn feed posts (not job listings) for AI/ML C2C job opportunities.
Looks for posts where recruiters/staffing firms share open contract roles.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from .chrome_mcp_client import ChromeMcpClient
from .eligibility import (
    BLOCKED_RESTRICTION_TOKENS,
    experience_exceeds_limit,
    normalize_text,
)

# ── Search configuration ──────────────────────────────────────────────────────

DEFAULT_SEARCH_QUERIES = [
    "AI C2C",
    "ML C2C",
    "machine learning C2C",
    "generative AI C2C contract",
]

MAX_HOURS = 2          # only keep posts newer than this
MAX_EXPERIENCE = 5     # skip posts requiring more than this many years
SCROLL_PASSES = 4      # how many times to scroll down per query

# ── Post classification tokens ────────────────────────────────────────────────

# If ANY of these appear → likely a candidate-availability post → skip
CANDIDATE_TOKENS = (
    "i am looking",
    "i'm looking",
    "i am seeking",
    "i'm seeking",
    "i am available",
    "i'm available",
    "available for opportunities",
    "available immediately",
    "open to work",
    "open to new opportunities",
    "seeking new opportunities",
    "looking for new opportunities",
    "please refer me",
    "recently laid off",
    "recently let go",
    "i have been",
    "i've been",
    "actively seeking",
    "my profile",
    "my resume",
    "my background includes",
    "i possess",
    "i bring",
)

# At least one of these → likely a job-posting post → keep for further checks
JOB_TOKENS = (
    "#hiring",
    "#jobalert",
    "#opportunity",
    "#contractjob",
    "#c2c",
    "we are hiring",
    "we're hiring",
    "we are looking",
    "we're looking",
    "looking for a",
    "looking for an",
    "urgent requirement",
    "immediate requirement",
    "immediate opening",
    "open position",
    "open role",
    "job opening",
    "job opportunity",
    "role available",
    "position available",
    "opportunity available",
    "client is looking",
    "client looking",
    "my client",
    "client requirement",
    "end client",
    "direct client",
    "reach out",
    "dm me",
    "share your resume",
    "send your resume",
    "send resume",
    "interested candidates",
    "job title:",
    "role:",
    "title:",
    "position:",
    "location:",
    "experience required",
    "years of experience",
    "requirement for",
    "hiring for",
)

# If ANY of these appear → no C2C → skip
NO_C2C_TOKENS = (
    "no c2c",
    "no corp to corp",
    "no corp-to-corp",
    "w2 only",
    "w-2 only",
    "w2 candidates only",
    "full time only",
    "fulltime only",
    "no contractors",
    "no contract",
    "no 1099",
    "no third party",
    "no third-party",
)

# Time patterns: "2h ago", "30m", "45 minutes ago", "1 day ago"
_TIME_RE = re.compile(
    r"(\d+)\s*"
    r"(m(?:in(?:utes?)?)?|h(?:(?:ou)?rs?)?|d(?:ays?)?)"
    r"(?:\s*ago)?",
    re.IGNORECASE,
)

# LinkedIn post separator in page text
_FEED_POST_SEP = "Feed post"
_POST_END_TOKENS = ("Like Comment Repost Send", "Like\nComment\nRepost\nSend")

# Pattern: "Author Name • 2nd+ Job Title at Company\n3m •\nFollow\n{post text}"
_AUTHOR_TIME_RE = re.compile(
    r"^(.+?)\s*•\s*(?:1st|2nd|3rd|1st\+|2nd\+|3rd\+)\b.{0,120}?\b(\d+[mhd])\s*•",
    re.DOTALL,
)
_TIME_ONLY_RE = re.compile(r"\b(\d+[mhd])\s*•")

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LinkedInPostLead:
    text: str
    author: str = ""
    timestamp: str = ""
    url: str = ""
    age_minutes: int = 9999
    # Classification
    is_job_post: bool = False
    is_candidate_post: bool = False
    # Parsed fields
    title: str = ""
    location: str = ""
    # Contact
    email: str = ""
    # Eligibility
    eligible: bool = False
    skip_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Helper functions ──────────────────────────────────────────────────────────

def _parse_age_minutes(text: str) -> int:
    """Parse '2h', '30m ago', '1 day ago' → minutes. Returns 9999 if unknown."""
    m = _TIME_RE.search(text or "")
    if not m:
        return 9999
    value, unit = int(m.group(1)), m.group(2)[0].lower()
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    return 9999


def _norm(text: str) -> str:
    return normalize_text(text)


def _any_token(text: str, tokens: Tuple[str, ...]) -> bool:
    t = _norm(text)
    return any(tok in t for tok in tokens)


def _extract_title(text: str) -> str:
    """Best-effort job title extraction from free-form post text."""
    patterns = [
        r"(?:job\s+title|role|title|position)[\s:–-]+([A-Za-z /,&.()\-]+?)(?:\n|location|experience|skills|$)",
        r"(?:hiring|looking for|seeking)\s+(?:a|an)\s+([A-Za-z /,&.()\-]+?)(?:\n|\.|at |for |with |$)",
        r"(?:opening|opportunity|requirement)\s+(?:for|of)\s+(?:a|an)?\s+([A-Za-z /,&.()\-]+?)(?:\n|\.|at |$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:100]
    return ""


def _extract_location(text: str) -> str:
    patterns = [
        r"location[\s:–-]+([A-Za-z ,/\-()]+?)(?:\n|experience|duration|start|$)",
        r"\b(remote|onsite|on-site|hybrid|work from home|wfh)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            loc = m.group(1).strip() if m.lastindex else m.group(0).strip()
            return loc[:80]
    return ""


def _analyze_post(raw: Dict[str, Any], max_hours: int = MAX_HOURS) -> LinkedInPostLead:
    text = raw.get("text", "")
    lead = LinkedInPostLead(
        text=text,
        author=raw.get("author", ""),
        timestamp=raw.get("timestamp", ""),
        url=raw.get("url", ""),
        age_minutes=_parse_age_minutes(raw.get("timestamp", "")),
    )

    # Classify
    lead.is_candidate_post = _any_token(text, CANDIDATE_TOKENS)
    lead.is_job_post = _any_token(text, JOB_TOKENS)

    if lead.is_candidate_post and not lead.is_job_post:
        lead.skip_reason = "Candidate availability post"
        return lead

    if not lead.is_job_post:
        lead.skip_reason = "Not a job posting"
        return lead

    # Recency check
    if lead.age_minutes > max_hours * 60:
        lead.skip_reason = f"Too old ({lead.age_minutes}m, max {max_hours * 60}m)"
        return lead

    # C2C check
    if _any_token(text, NO_C2C_TOKENS):
        lead.skip_reason = "No C2C"
        return lead

    # Citizenship / clearance block
    blocked = tuple(_norm(t) for t in BLOCKED_RESTRICTION_TOKENS)
    if any(tok in _norm(text) for tok in blocked):
        lead.skip_reason = "Citizenship or clearance restriction"
        return lead

    # Experience limit
    if experience_exceeds_limit(text, max_experience_years=MAX_EXPERIENCE):
        lead.skip_reason = f"Requires more than {MAX_EXPERIENCE} years"
        return lead

    # AI/ML keyword check — at least one AI/ML keyword must appear in text
    from .eligibility import AI_ML_KEYWORDS
    if not any(_norm(kw) in _norm(text) for kw in AI_ML_KEYWORDS):
        lead.skip_reason = "No AI/ML keywords found"
        return lead

    lead.title = _extract_title(text)
    lead.location = _extract_location(text)
    lead.email = _extract_email(text)
    lead.eligible = True
    return lead


# ── Scanner ───────────────────────────────────────────────────────────────────

def _search_url(query: str) -> str:
    return f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(query)}&sortBy=date_posted"


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _extract_email(text: str) -> str:
    """Extract the first email address found in post text."""
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else ""


def _expand_all_posts(client: ChromeMcpClient, tab_id: str) -> int:
    """
    Click every 'see more' / '…see more' button on the current page so that
    all post bodies are fully expanded before we read the text.
    Returns the number of buttons clicked.
    """
    clicked = 0
    for query in ("… more", "...more", "see more", "show more"):
        try:
            elements = client.find_elements(tab_id, query, exact=False, limit=30)
            for el in elements:
                label = (el.label or el.text or el.aria_label or "").strip()
                if label in ("… more", "...more") or "see more" in label.lower() or "show more" in label.lower():
                    try:
                        client.perform_action(tab_id, {"kind": "click", "targetId": el.id})
                        clicked += 1
                        time.sleep(0.3)
                    except Exception:
                        pass
        except Exception:
            pass
    if clicked:
        time.sleep(1.0)  # let expansions render
    return clicked


def _scroll_down(client: ChromeMcpClient, tab_id: str, pixels: int = 2000) -> None:
    try:
        client.perform_action(tab_id, {"kind": "scroll", "deltaY": pixels, "x": 700, "y": 500})
    except Exception:
        pass
    time.sleep(2.5)


def _get_page_text(client: ChromeMcpClient, tab_id: str, limit: int = 40000) -> str:
    """Fetch full visible page text via page.text with a high limit."""
    try:
        response = client.request("page.text", tabId=tab_id, limit=limit)
        payload = response.get("payload", {})
        return payload.get("text", "") or payload.get("visibleText", "") or ""
    except Exception:
        # Fallback: use collect_page with high text limit
        payload = client.collect_page(tab_id, text_limit=limit)
        return payload.get("visibleTextExcerpt", "")


def _parse_posts_from_text(page_text: str, page_url: str) -> List[Dict[str, str]]:
    """
    Split the LinkedIn search results page text into individual posts.
    Each post block starts with 'Feed post' in the text.
    """
    posts = []
    chunks = page_text.split(_FEED_POST_SEP)

    for chunk in chunks[1:]:  # skip everything before first "Feed post"
        chunk = chunk.strip()
        if len(chunk) < 40:
            continue

        # Extract timestamp from beginning of chunk (e.g. "3m •", "1h •")
        timestamp = ""
        tm = _TIME_ONLY_RE.search(chunk[:200])
        if tm:
            timestamp = tm.group(1)

        # Extract author: text before first "•" in the chunk header
        author = ""
        header = chunk[:200]
        if "•" in header:
            candidate = header.split("•")[0].strip().split("\n")[-1].strip()
            if 2 < len(candidate) < 60:
                author = candidate

        # Extract post body: everything after "Follow\n" or after the timestamp line
        body = chunk
        if "Follow\n" in body:
            body = body.split("Follow\n", 1)[1]
        elif "Follow " in body:
            body = body.split("Follow ", 1)[1]

        # Trim trailing interaction buttons
        for end_token in ("Like Comment Repost Send", "… more Like", "Like\nComment"):
            if end_token in body:
                body = body.split(end_token)[0]

        body = body.strip()
        if len(body) < 30:
            continue

        posts.append({
            "text": body,
            "author": author,
            "timestamp": timestamp,
            "url": page_url,
        })

    return posts


def scan_linkedin_posts(
    queries: Optional[List[str]] = None,
    max_hours: int = MAX_HOURS,
    scroll_passes: int = SCROLL_PASSES,
    ws_url: str = "ws://127.0.0.1:8765",
) -> List[LinkedInPostLead]:
    """
    Scan LinkedIn feed posts for AI/ML C2C job opportunities.
    Returns a list of LinkedInPostLead objects, both eligible and skipped.
    """
    if queries is None:
        queries = DEFAULT_SEARCH_QUERIES

    all_leads: List[LinkedInPostLead] = []
    seen_keys: set = set()

    for query in queries:
        print(f"\nSearching posts for: {query!r}")
        url = _search_url(query)

        # Each query gets its own connection to avoid WebSocket keepalive timeouts
        try:
            with ChromeMcpClient(ws_url) as client:
                tabs = client.list_tabs()
                linkedin_tab = next(
                    (t for t in tabs if "linkedin.com" in t.url),
                    tabs[0] if tabs else None,
                )
                if linkedin_tab is None:
                    print("  ERROR: No Chrome tab found.")
                    continue

                tab_id = linkedin_tab.id
                client.navigate(tab_id, url)
                time.sleep(4.0)  # wait for page load + React render

                post_count_before = len(all_leads)
                stop_early = False

                for pass_num in range(scroll_passes + 1):
                    # Expand all truncated posts before reading
                    expanded = _expand_all_posts(client, tab_id)
                    if expanded:
                        print(f"  Expanded {expanded} 'see more' buttons")
                    page_text = _get_page_text(client, tab_id)
                    raw_posts = _parse_posts_from_text(page_text, url)

                    new_this_pass = 0
                    oldest_age = 0
                    for raw in raw_posts:
                        key = raw.get("text", "")[:120]
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        lead = _analyze_post(raw, max_hours=max_hours)
                        all_leads.append(lead)
                        new_this_pass += 1
                        if lead.age_minutes < 9999:
                            oldest_age = max(oldest_age, lead.age_minutes)

                    age_label = f"{oldest_age}m" if oldest_age else "unknown"
                    print(f"  Pass {pass_num}: {new_this_pass} new posts | oldest {age_label}")

                    if oldest_age > max_hours * 60 and pass_num > 0:
                        print(f"  Reached posts older than {max_hours}h — stopping")
                        stop_early = True
                        break

                    if pass_num < scroll_passes and not stop_early:
                        _scroll_down(client, tab_id)

                added = len(all_leads) - post_count_before
                print(f"  Query complete — {added} posts collected")
        except Exception as exc:
            print(f"  Query failed: {exc}")

    eligible = [l for l in all_leads if l.eligible]
    print(f"\nTotal posts scanned: {len(all_leads)}")
    print(f"Eligible job leads: {len(eligible)}")
    return all_leads
