import re
from typing import Dict, List, Tuple

from .utils import normalize_text


AI_ML_KEYWORDS = [
    "ai engineer",
    "ml engineer",
    "data scientist",
    "data science",
    "gen ai engineer",
    "data analyst",
    "ai model",
    "genai",
    "generative ai",
    "llm",
    "machine learning",
    "python",
    "rag",
]

TECH_TITLE_HINTS = {
    "ai",
    "analyst",
    "architect",
    "data",
    "developer",
    "engineer",
    "llm",
    "machine learning",
    "ml",
    "mlops",
    "nlp",
    "scientist",
    "software",
}

AI_TITLE_TOKENS = (
    "ai",
    "artificial intelligence",
    "genai",
    "llm",
    "machine learning",
    "ml",
    "nlp",
    "rag",
)

CONTRACT_TOKENS = (
    "contract",
    "c2c",
    "corp to corp",
    "corp-to-corp",
    "1099",
)

SENIORITY_TOKENS = (
    "senior",
    "staff",
    "principal",
    "manager",
    "director",
)

NON_TARGET_TITLE_TOKENS = (
    "grafana",
    "observability",
    "trainer",
    "annotator",
    "field service",
    "audio engineer",
    "industrial engineer",
    "data engineer",
)

BLOCKED_RESTRICTION_TOKENS = (
    "us citizen only",
    "usc only",
    "usc / gc",
    "usc/gc",
    "usc gc",
    "usc gc only",
    "usc or gc",
    "citizens only",
    "citizen only",
    "gc only",
    "green card only",
    "permanent resident only",
    "security clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "public trust",
    "clearance required",
    "no h1b",
    "h1b not accepted",
    "h-1b not accepted",
    "h1 transfer only",
    "no c2c",
    "no corp to corp",
    "no corp-to-corp",
    "no third party",
    "no third-party",
    "no 1099",
    # NOTE: "w2 only/role/candidate" removed — user wants to apply to W2 jobs
    # unless they explicitly say "no C2C" or "no third party"
)

FULL_TIME_MARKERS = (
    "type full time",
    "commitment full time",
    "employment type full time",
)


def ai_ml_match(title: str, description: str) -> Tuple[bool, List[str]]:
    normalized_title = normalize_text(title)
    normalized_description = normalize_text(description)

    # Multi-word keyword match in title (e.g. "ai engineer", "machine learning")
    title_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_title]
    if title_matches:
        return True, title_matches

    # Single AI/ML token in title + a tech role hint (e.g. "AI Cloud Engineer",
    # "ML Ops Developer", "LLM Architect") — these are clearly AI/ML roles even
    # though no multi-word phrase matches.
    has_tech_hint = any(hint in normalized_title for hint in TECH_TITLE_HINTS)
    if has_tech_hint:
        token_matches = [tok for tok in AI_TITLE_TOKENS if tok in normalized_title]
        if token_matches:
            return True, token_matches

    if not has_tech_hint:
        return False, []

    description_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_description]
    return bool(description_matches), description_matches


def analyze_job_fit(
    title: str,
    description: str,
    require_contract: bool = True,
    max_experience_years: int = 4,
    ai_check: bool = True,
) -> Dict[str, object]:
    normalized_title = normalize_text(title)
    normalized_description = normalize_text(description)
    combined = normalize_text(f"{title} {description}")
    normalized_contract_tokens = tuple(normalize_text(token) for token in CONTRACT_TOKENS)
    normalized_full_time_markers = tuple(normalize_text(token) for token in FULL_TIME_MARKERS)
    normalized_blocked_tokens = tuple(normalize_text(token) for token in BLOCKED_RESTRICTION_TOKENS)

    matched_ai_ml, matched_keywords = ai_ml_match(title, description)
    reasons: List[str] = []

    normalized_non_target = tuple(normalize_text(token) for token in NON_TARGET_TITLE_TOKENS)
    if any(token in normalized_title for token in normalized_non_target):
        reasons.append("Non-target role title")

    if not matched_ai_ml:
        reasons.append("Not AI/ML aligned")

    if any(token in combined for token in normalized_blocked_tokens):
        reasons.append("Citizenship, clearance, or employment-mode restriction")

    if max_experience_years >= 0 and experience_exceeds_limit(combined, max_experience_years):
        reasons.append(f"Requires more than {max_experience_years} years")

    # AI deep-read: only run when heuristics pass to avoid wasting tokens on
    # obviously ineligible jobs. Falls back gracefully if API is unavailable.
    if not reasons and ai_check:
        import os as _os
        if _os.getenv("ANTHROPIC_API_KEY"):
            from .ai_assistant import assess_eligibility
            eligible, ai_reason = assess_eligibility(title, description)
            if not eligible:
                reasons.append(f"AI review: {ai_reason}")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "ai_ml_match": matched_ai_ml,
        "matched_keywords": matched_keywords,
    }


def experience_exceeds_limit(text: str, max_experience_years: int) -> bool:
    if max_experience_years < 0:
        return False

    normalized = normalize_text(text)
    for lower, upper in _experience_ranges(normalized):
        if upper > max_experience_years:
            return True
        if lower > max_experience_years:
            return True
    return False


def _experience_ranges(text: str) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    patterns = (
        r"(\d+)\s*(?:to|-)\s*(\d+)\s+years",
        r"(\d+)\+?\s+years",
        r"(\d+)\s*(?:to|-)\s*(\d+)\s+yrs",
        r"(\d+)\+?\s+yrs",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            values = [int(group) for group in match.groups() if group]
            if not values:
                continue
            if len(values) == 1:
                ranges.append((values[0], values[0]))
            else:
                ranges.append((min(values), max(values)))
    return ranges
