"""
AI-powered form answer resolver for Indeed Easy Apply.

Sits between the raw Chrome MCP snapshot and IndeedBridgeDriver.fill_fields().
For every form field in a snapshot it tries, in order:

  1. Profile data    — name, email, phone, location, etc.
  2. Heuristics      — work auth, sponsorship, visa, veteran, disability, skills
  3. question_answers.json lookup (contains-match)
  4. Claude Haiku    — picks the best option from a dropdown / radio list
  5. None            — field is left for manual review

Usage (inside application.py or standalone):

    from job_apply_bot.apply.indeed.form_filler import IndeedFormFiller

    filler   = IndeedFormFiller(profile, custom_answers)
    values   = filler.build_values(snapshot)          # {label: value}
    # pass values to IndeedBridgeDriver.advance_application(values_by_label=values)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils import normalize_text

log = logging.getLogger(__name__)


# Fields whose labels are too generic to safely auto-answer
_SKIP_LABELS = frozenset(
    normalize_text(t)
    for t in (
        "message",
        "additional information",
        "notes",
        "comments",
        "anything else",
        "other",
    )
)

# Normalize placeholder-style select options so we can skip them
_PLACEHOLDER_OPTIONS = frozenset(
    ("select an option", "please select", "select...", "-- select --",
     "select one", "choose an option", "choose one", "none", "--")
)


class IndeedFormFiller:
    """
    Resolves answers for all interactive fields found in a BridgePageSnapshot.

    Parameters
    ----------
    profile        : candidate_profile.json dict
    custom_answers : question_answers.json dict  (raw, un-normalized)
    """

    def __init__(
        self,
        profile: Dict[str, Any],
        custom_answers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.profile = profile
        # Build a lower-case "contains" lookup table
        self._custom: Dict[str, str] = {
            normalize_text(k): v
            for k, v in (custom_answers or {}).items()
            if k
        }

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def build_values(self, snapshot) -> Dict[str, str]:
        """
        Return {label: value} for every answerable field in snapshot.
        Unknown dropdowns / radios are sent to Claude Haiku.
        Fields that cannot be answered are omitted (bridge will skip them).
        """
        values: Dict[str, str] = {}

        for control in snapshot.interactive_elements:
            if control.disabled:
                continue

            tag   = control.tag_name.lower()
            role  = (control.role or "").lower()
            ftype = (control.type  or "").lower()

            # Only care about fillable / choosable controls
            if tag not in {"input", "textarea", "select"} and role not in {
                "combobox", "listbox", "radio", "checkbox",
            }:
                continue
            if ftype == "hidden":
                continue

            field = self._control_to_field(control)
            label = self._label(field)
            if not label:
                continue
            if normalize_text(label) in _SKIP_LABELS:
                continue

            answer = self._resolve(field)
            if answer is None:
                continue

            values.setdefault(label, answer)

        return values

    # ------------------------------------------------------------------ #
    # Resolution pipeline                                                  #
    # ------------------------------------------------------------------ #

    def _resolve(self, field: Dict[str, Any]) -> Optional[str]:
        """Try all answer strategies in priority order."""
        answer = self._from_profile(field)
        if answer is not None:
            return answer

        answer = self._from_heuristics(field)
        if answer is not None:
            return answer

        answer = self._from_custom_answers(field)
        if answer is not None:
            return self._validate_against_options(answer, field)

        # For selects / radios with options: ask AI
        options = self._options(field)
        if options:
            answer = self._ai_select(self._label(field), options)
            if answer:
                return answer
            # Last resort: pick first non-placeholder option
            non_ph = [o for o in options if normalize_text(o) not in _PLACEHOLDER_OPTIONS]
            return non_ph[0] if non_ph else None

        return None

    # ------------------------------------------------------------------ #
    # Strategy 1 — Profile data                                           #
    # ------------------------------------------------------------------ #

    def _from_profile(self, field: Dict[str, Any]) -> Optional[str]:
        label   = normalize_text(self._label(field))
        p       = self.profile
        ftype   = str(field.get("type",  "")).lower()
        fname   = normalize_text(str(field.get("name",        "") or ""))
        ph      = normalize_text(str(field.get("placeholder", "") or ""))
        hint    = f"{fname} {ph}".strip()

        # Name fields — prefer name/placeholder hint for Indeed (avoids label noise)
        if hint:
            if any(t in hint for t in ("firstname", "first name", "given name", "fname")) and "last" not in hint:
                return p.get("first_name") or (p.get("full_name", "").split()[0] if p.get("full_name") else None)
            if any(t in hint for t in ("lastname", "last name", "surname", "lname")):
                return p.get("last_name") or (" ".join(p.get("full_name", "").split()[1:]) or None)
            if "email"  in hint: return p.get("email")
            if "phone"  in hint or "mobile" in hint: return p.get("phone")
            if "zip"    in hint or "postal" in hint: return p.get("zip_code")
            if "city"   in hint: return p.get("city") or p.get("location")
            if "state"  in hint or "province" in hint: return p.get("state")

        # Resume upload
        if ftype == "file" or any(t in label for t in ("resume", "cv")):
            return str(p.get("_resume_path", ""))

        # Standard profile fields by label
        if "first name" in label and "last" not in label:
            return p.get("first_name") or (p.get("full_name", "").split()[0] if p.get("full_name") else None)
        if "last name" in label or "surname" in label:
            return p.get("last_name") or (" ".join(p.get("full_name", "").split()[1:]) or None)
        if any(t in label for t in ("full name", "legal name")) or label == "name":
            return p.get("full_name")
        if "email" in label:    return p.get("email")
        if "phone" in label or "mobile" in label: return p.get("phone")
        if "linkedin" in label: return p.get("linkedin_url")
        if "github"   in label: return p.get("github_url")
        if any(t in label for t in ("portfolio", "personal site", "website")): return p.get("portfolio_url")
        if any(t in label for t in ("current company", "company name", "employer")): return p.get("current_company")
        if any(t in label for t in ("current role", "job title", "headline")) or label == "title":
            return p.get("current_role") or p.get("title")
        if "city" in label:   return p.get("city") or p.get("location")
        if "state" in label or "province" in label: return p.get("state")
        if "zip" in label or "postal code" in label: return p.get("zip_code")
        if "location" in label: return p.get("location")
        if "country" in label and "citizen" not in label: return p.get("country")

        return None

    # ------------------------------------------------------------------ #
    # Strategy 2 — Heuristics                                             #
    # ------------------------------------------------------------------ #

    def _from_heuristics(self, field: Dict[str, Any]) -> Optional[str]:
        label   = normalize_text(self._label(field))
        p       = self.profile
        exp     = str(p.get("experience_years", 4))
        options = self._options(field)

        # Years of experience
        if any(t in label for t in ("years of experience", "how many years", "years experience")):
            return exp
        if str(field.get("type", "")).lower() == "number" and "experience" in label:
            return exp

        # Work authorization
        if any(t in label for t in ("authorized to work", "legally authorized", "work authorization", "work auth")):
            val = p.get("work_authorization", "Yes")
            if options:
                return self._best_option(val, options) or self._best_option("authorized for any employer", options) or val
            return val

        # Sponsorship
        if "sponsorship" in label:
            val = p.get("requires_visa_sponsorship", "No")
            if options:
                return self._best_option(val, options) or val
            return val

        # Citizenship
        if any(t in label for t in ("us citizen", "u.s. citizen", "citizenship status", "are you a citizen")):
            val = p.get("us_citizen", "No")
            if options:
                return self._best_option(val, options) or val
            return val

        # Visa status
        if "visa status" in label or "explain your visa status" in label:
            return p.get("visa_status")

        # Veteran
        if "veteran" in label:
            val = p.get("veteran_status", "I am not a protected veteran")
            if options:
                return self._best_option("not a protected veteran", options) or self._best_option(val, options) or val
            return val

        # Disability
        if "disability" in label:
            val = p.get("disability_status", "No")
            if options:
                return self._best_option("no disability", options) or self._best_option(val, options) or val
            return val

        # Ethnicity / race
        if any(t in label for t in ("ethnicity", "race")):
            val = p.get("ethnicity", "Asian Indian")
            if options:
                return (
                    self._best_option("asian", options)
                    or self._best_option(val, options)
                    or val
                )
            return val

        # Gender
        if "gender" in label:
            val = p.get("gender", "")
            if options and val:
                return self._best_option(val, options) or val
            return val or None

        # Relocate
        if "relocat" in label:
            return self._best_option("no", options) if options else "No"

        # Availability / start date
        if any(t in label for t in ("start date", "available to start", "earliest start", "when can you start")):
            return "2 weeks"
        if "availability" in label and "interview" not in label:
            return "Immediately"

        # Skills we have → yes
        skills_lower = [s.lower() for s in (p.get("skills") or [])]
        for skill in skills_lower:
            if skill in label:
                return self._best_option("yes", options) if options else "Yes"

        tech_kw = {
            "python", "machine learning", "deep learning", "nlp", "generative ai",
            "rag", "langchain", "langgraph", "azure", "aws", "docker", "kubernetes",
            "mlflow", "airflow", "pytorch", "scikit", "flask", "sql", "llm", "ai",
        }
        if any(kw in label for kw in tech_kw):
            return self._best_option("yes", options) if options else "Yes"

        # Generic willing / open to / comfortable with
        if any(t in label for t in ("willing to", "open to", "comfortable with", "able to")):
            return self._best_option("yes", options) if options else "Yes"

        # Privacy / terms / consent
        if any(t in label for t in ("privacy policy", "data processing")) or (
            "agree" in label and any(t in label for t in ("policy", "terms", "consent"))
        ):
            return self._best_option("yes", options) if options else "Yes"

        # How did you hear
        if "how did you hear" in label:
            return "Indeed"

        # Cover letter / pitch
        if any(t in label for t in ("cover letter", "why are you interested", "tell us about yourself", "summary")):
            return p.get("short_pitch") or p.get("summary") or None

        return None

    # ------------------------------------------------------------------ #
    # Strategy 3 — question_answers.json lookup                           #
    # ------------------------------------------------------------------ #

    def _from_custom_answers(self, field: Dict[str, Any]) -> Optional[str]:
        label_norm = normalize_text(self._label(field))
        # Exact match first
        if label_norm in self._custom:
            return self._custom[label_norm]
        # Contains match
        for key, value in self._custom.items():
            if key and key in label_norm:
                return value
        return None

    # ------------------------------------------------------------------ #
    # Strategy 4 — AI                                                     #
    # ------------------------------------------------------------------ #

    def _ai_select(self, question: str, options: List[str]) -> str:
        try:
            from ...ai_assistant import select_answer
            answer = select_answer(question, options)
            if answer:
                log.info("  AI answered '%s' → '%s'", question, answer)
            return answer
        except Exception as exc:
            log.warning("IndeedFormFiller AI select failed: %s", exc)
            return ""

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _control_to_field(self, control) -> Dict[str, Any]:
        role = (control.role or "").lower()
        tag  = control.tag_name.lower()
        raw_options = control.raw.get("options", []) if isinstance(control.raw, dict) else []
        options = [str(o).strip() for o in raw_options if str(o).strip()] if isinstance(raw_options, list) else []
        effective_tag = "select" if role in {"combobox", "listbox"} and tag not in {"input", "textarea", "select"} else tag
        return {
            "id":          control.id,
            "tag":         effective_tag,
            "type":        (control.type or "").lower(),
            "role":        role,
            "label":       control.label or control.aria_label or control.placeholder or control.name or control.text or "",
            "name":        control.name or "",
            "placeholder": control.placeholder or "",
            "required":    bool(control.required),
            "optionLabel": control.text or "",
            "options":     options,
        }

    def _label(self, field: Dict[str, Any]) -> str:
        return (field.get("label") or field.get("name") or "").strip()

    def _options(self, field: Dict[str, Any]) -> List[str]:
        return [o for o in (field.get("options") or []) if o.strip()]

    def _best_option(self, desired: str, options: List[str]) -> str:
        """Return the option that best matches `desired`, or '' if none match."""
        d = normalize_text(desired)
        opts_lower = {normalize_text(o): o for o in options}
        if d in opts_lower:
            return opts_lower[d]
        for o_norm, o_orig in opts_lower.items():
            if d in o_norm or o_norm in d:
                return o_orig
        return ""

    def _validate_against_options(self, answer: str, field: Dict[str, Any]) -> str:
        """If field has options, ensure answer is one of them."""
        options = self._options(field)
        if not options:
            return answer
        best = self._best_option(answer, options)
        return best if best else answer
