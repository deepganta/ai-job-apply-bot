"""
Vision-based LinkedIn job applier.

Replicates the Claude computer-use approach:
  screenshot → read form accessibility tree → LLM decides values → real Playwright clicks

Works with ANY LLM — configure via LLM_PROVIDER env var:
  LLM_PROVIDER=anthropic   (default, uses ANTHROPIC_API_KEY)
  LLM_PROVIDER=openai      (uses OPENAI_API_KEY)
  LLM_PROVIDER=groq        (uses GROQ_API_KEY)
  LLM_PROVIDER=ollama      (local, uses OLLAMA_BASE_URL, no key needed)
  LLM_PROVIDER=none        (heuristic-only, no LLM)
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM provider abstraction — swap out any provider without touching apply logic
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract LLM backend. Only needs to answer: given fields + profile → values."""

    @abstractmethod
    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        """Return {field_label: answer} for the given form fields."""


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        from anthropic import Anthropic
        self._client = Anthropic(api_key=api_key)
        self._model = model

    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        prompt = _build_prompt(fields_json, profile_json, experience_years)
        response = self._client.messages.create(
            model=self._model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json_response(response.content[0].text)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        prompt = _build_prompt(fields_json, profile_json, experience_years)
        response = self._client.chat.completions.create(
            model=self._model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json_response(response.choices[0].message.content or "")


class GroqProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "llama3-8b-8192"):
        from groq import Groq
        self._client = Groq(api_key=api_key)
        self._model = model

    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        prompt = _build_prompt(fields_json, profile_json, experience_years)
        response = self._client.chat.completions.create(
            model=self._model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json_response(response.choices[0].message.content or "")


class OllamaProvider(LLMProvider):
    """Local Ollama — no API key needed."""
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self._base_url = base_url.rstrip("/")
        self._model = model

    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        import urllib.request
        prompt = _build_prompt(fields_json, profile_json, experience_years)
        payload = json.dumps({"model": self._model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(f"{self._base_url}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return _parse_json_response(data.get("response", ""))


class HeuristicProvider(LLMProvider):
    """No LLM — pure rule-based answers from profile data."""
    def answer_fields(self, fields_json: str, profile_json: str, experience_years: int) -> Dict[str, str]:
        return {}  # handled separately by VisionApplier._heuristic_answers


def _build_prompt(fields_json: str, profile_json: str, experience_years: int) -> str:
    return f"""You are filling a job application form for this candidate.

CANDIDATE PROFILE:
{profile_json}

CANDIDATE FACTS (always apply these, they override the profile JSON if there is any conflict):
- Visa status: OPT (F-1) — fully authorized to work in the US without any employer sponsorship
- Employment type: C2C contract only
- Experience: {experience_years} years total in AI/ML/GenAI/NLP
- Willing to relocate: No
- US citizen: No
- Require sponsorship: No (OPT does not require sponsorship for contract work)
- Veteran: No
- Disability: No
- Ethnicity / race: Asian Indian

FORM FIELDS (need answers):
{fields_json}

CRITICAL RULES FOR OPTION FIELDS (select, radio, combobox):
- You MUST return EXACTLY one of the strings listed in the "options" array for that field
- Copy the option text verbatim — do not paraphrase, shorten, or rephrase it
- If the best match is ambiguous, pick the option that most closely means the same thing

GENERAL RULES:
- "Years of experience" with any technology or skill: answer with {experience_years}
- "Legally authorized to work" / "Work authorization": Yes
- "Require sponsorship": No
- "US citizen" / "Are you a citizen": No
- "Willing to relocate": No
- For free-text fields not in the profile: give a brief, honest, reasonable answer
- Do NOT leave required fields blank

Respond ONLY with a JSON object mapping field label to answer:
{{"Field label": "answer", ...}}"""


def _parse_json_response(text: str) -> Dict[str, str]:
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {}


def make_llm_provider(provider: str = "", api_key: str = "", model: str = "") -> LLMProvider:
    """
    Factory. Reads from env vars if not provided:
      LLM_PROVIDER, ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, OLLAMA_BASE_URL
    """
    p = (provider or os.getenv("LLM_PROVIDER", "anthropic")).lower()

    if p == "openai":
        key = api_key or os.getenv("OPENAI_API_KEY", "")
        if key:
            return OpenAIProvider(key, model or "gpt-4o-mini")

    if p == "groq":
        key = api_key or os.getenv("GROQ_API_KEY", "")
        if key:
            return GroqProvider(key, model or "llama3-8b-8192")

    if p == "ollama":
        base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaProvider(base, model or "llama3")

    if p in ("none", "heuristic"):
        return HeuristicProvider()

    # Default: Anthropic
    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return AnthropicProvider(key, model or "claude-haiku-4-5-20251001")

    # No key found — fall back to heuristic
    log.warning("No LLM API key found. Using heuristic field answering only.")
    return HeuristicProvider()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FormField:
    label: str
    kind: str          # "text", "number", "select", "radio", "checkbox", "textarea"
    options: List[str] = field(default_factory=list)
    current_value: str = ""
    required: bool = False


@dataclass
class ApplyResult:
    status: str          # "submitted" | "ready_to_submit" | "review_required" | "already_applied"
    message: str = ""
    steps: int = 0
    fields_filled: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core applier
# ---------------------------------------------------------------------------

class VisionApplier:
    """
    Applies to a LinkedIn Easy Apply job using real Playwright interactions
    guided by a pluggable LLM provider.

    Usage:
        applier = VisionApplier(profile)           # auto-detects LLM from env
        result  = applier.apply(page, job_url)
    """

    MAX_STEPS = 15
    
    LINKEDIN_FORM_SCRIPT = """
    (dialog) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const getLabel = (el) => {
        let label = el.getAttribute('aria-label') || '';
        if (label) return normalize(label);
        let id = el.getAttribute('aria-labelledby') || '';
        if (id) {
          const l = document.getElementById(id.split(' ')[0]);
          if (l) return normalize(l.innerText);
        }
        let eid = el.id;
        if (eid) {
          const l = document.querySelector(`label[for="${eid}"]`);
          if (l) return normalize(l.innerText);
        }
        return normalize(el.placeholder || '');
      };

      const fields = [];
      const inputs = dialog.querySelectorAll('input:not([type="hidden"]), textarea, select, [role="combobox"], [aria-haspopup="listbox"]');
      
      inputs.forEach(el => {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        
        let kind = 'text';
        const type = el.getAttribute('type');
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role');
        const hasPopup = el.getAttribute('aria-haspopup');

        if (tag === 'textarea') kind = 'textarea';
        else if (tag === 'select' || role === 'combobox' || hasPopup === 'listbox') kind = 'select';
        else if (type === 'radio') kind = 'radio';
        else if (type === 'number') kind = 'number';
        
        const label = getLabel(el);
        if (!label && type !== 'radio') return;

        let options = [];
        if (tag === 'select') {
          options = Array.from(el.options).map(o => normalize(o.text));
        } else if (type === 'radio') {
          const name = el.name;
          const legend = el.closest('fieldset')?.querySelector('legend')?.innerText || name;
          kind = 'radio';
          const rl = getLabel(el);
          fields.push({ label: normalize(legend), kind: 'radio', option: rl, name: name });
          return;
        }

        fields.push({
          label,
          kind,
          current_value: el.value || el.innerText || '',
          required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
          options
        });
      });
      return fields;
    }
    """

    def __init__(self, profile: Dict[str, Any], llm: Optional[LLMProvider] = None,
                 custom_answers: Optional[Dict[str, str]] = None):
        self.profile = profile
        self._llm = llm or make_llm_provider()
        # Merged {normalized_key: answer} from question_answers.json (contains section)
        self._custom_answers: Dict[str, str] = custom_answers or {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def apply(self, page: Page, job_url: str, *, auto_submit: bool = True) -> ApplyResult:
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(1500)

        if self._is_already_applied(page):
            return ApplyResult("already_applied", "LinkedIn shows this job as already applied")

        if not self._click_easy_apply(page):
            return ApplyResult("review_required", "Easy Apply button not found")

        page.wait_for_timeout(1500)
        return self._run_form_loop(page, auto_submit=auto_submit)

    # ------------------------------------------------------------------
    # Form loop
    # ------------------------------------------------------------------

    def _run_form_loop(self, page: Page, *, auto_submit: bool) -> ApplyResult:
        steps = 0
        fields_filled: List[str] = []
        prev_fields_hash: str = ""
        same_step_count: int = 0

        for step in range(self.MAX_STEPS):
            steps = step + 1
            page.wait_for_timeout(800)

            dialog = self._get_dialog(page)
            if dialog is None:
                if self._is_already_applied(page):
                    return ApplyResult("submitted", "Application submitted", steps, fields_filled)
                return ApplyResult("review_required", "Dialog closed unexpectedly", steps, fields_filled)

            fields = self._read_form_fields(dialog)
            step_title = self._get_step_title(dialog)
            log.info("Step %d — %s (%d fields)", steps, step_title, len(fields))

            # Stuck detection: hash the current field labels+values.
            # Only declare stuck when the SAME fields appear unchanged for 3
            # consecutive iterations (meaning our fill attempts aren't working).
            # NOTE: do NOT use step_title — LinkedIn reuses the same modal title
            # ("Apply to X") on every step, so title-based detection would fire
            # prematurely and prevent reaching the Submit button.
            import hashlib as _hl
            _fields_key = "|".join(sorted(f"{f.label}:{f.current_value}" for f in fields))
            fields_hash = _hl.md5(_fields_key.encode()).hexdigest()
            if fields_hash and fields_hash == prev_fields_hash:
                same_step_count += 1
                if same_step_count >= 3:
                    return ApplyResult("review_required", f"Stuck on step '{step_title}' — unanswerable required field", steps, fields_filled)
            else:
                same_step_count = 0
            prev_fields_hash = fields_hash

            if fields:
                filled = self._fill_fields(page, dialog, fields)
                fields_filled.extend(filled)
                page.wait_for_timeout(400)

            # Navigate
            prev_title = step_title
            if self._has_button(dialog, ["Submit application", "Send application"]):
                if auto_submit:
                    self._click_button(dialog, ["Submit application", "Send application"])
                    page.wait_for_timeout(2500)
                    if self._is_already_applied(page) or "post-apply" in page.url:
                        return ApplyResult("submitted", "Application submitted successfully", steps, fields_filled)
                    return ApplyResult("submitted", "Submit clicked", steps, fields_filled)
                return ApplyResult("ready_to_submit", "Ready — auto_submit=False", steps, fields_filled)

            elif self._has_button(dialog, ["Review your application", "Review"]):
                self._click_button(dialog, ["Review your application", "Review"])
                # Give the review page enough time to fully render, then
                # explicitly wait for the Submit button so the next loop
                # iteration is guaranteed to find it.
                page.wait_for_timeout(2000)
                try:
                    dialog.get_by_role("button", name="Submit application", exact=False).first.wait_for(state="visible", timeout=6000)
                except Exception:
                    pass

            elif self._has_button(dialog, ["Continue to next step", "Next", "Continue"]):
                self._click_button(dialog, ["Continue to next step", "Next", "Continue"])
                page.wait_for_timeout(2000)
                # Check for errors after clicking Next/Continue
                if self._check_for_errors(dialog):
                    log.warning("Step %d — errors detected after navigation, attempting fix", steps)
                    error_fields = self._read_form_fields(dialog)
                    if error_fields:
                        self._fill_fields(page, dialog, error_fields, fix_mode=True)
                        page.wait_for_timeout(500)
                        # Try clicking again and wait longer for form to advance
                        self._click_button(dialog, ["Continue to next step", "Next", "Continue"])
                        page.wait_for_timeout(2000)

            else:
                return ApplyResult("review_required", "No navigation button found", steps, fields_filled)

        return ApplyResult("review_required", "Exceeded max steps", steps, fields_filled)

    # ------------------------------------------------------------------
    # Field reading
    # ------------------------------------------------------------------

    def _read_form_fields(self, dialog) -> List[FormField]:
        try:
            # Use the "Deep DOM Feature" for 100% precision
            raw_fields = dialog.evaluate(self.LINKEDIN_FORM_SCRIPT)
            fields: List[FormField] = []
            
            # Map JS objects to FormField models
            for rf in raw_fields:
                if rf['kind'] == 'radio':
                   # Merge radio options with the same legend/name
                   existing = next((f for f in fields if f.label == rf['label'] and f.kind == 'radio'), None)
                   if existing:
                       if rf['option'] not in existing.options:
                           existing.options.append(rf['option'])
                       continue
                   fields.append(FormField(label=rf['label'], kind='radio', options=[rf['option']]))
                else:
                   fields.append(FormField(
                       label=rf['label'],
                       kind=rf['kind'],
                       current_value=rf['current_value'],
                       required=rf['required'],
                       options=rf.get('options', [])
                   ))
            return fields
        except Exception as exc:
            log.warning("Deep DOM Scan failed: %s — falling back to standard locators", exc)
            # Fallback to standard locators if injection fails (legacy safety)
            fields: List[FormField] = []
            for inp in dialog.locator("input[type='text'], input[type='number'], input:not([type])").all():
                try:
                    if not inp.is_visible(): continue
                    label = self._get_label(inp, dialog)
                    if not label: continue
                    fields.append(FormField(label=label, kind="number" if inp.get_attribute("type") == "number" else "text", current_value=inp.input_value() or "", required=inp.get_attribute("required") is not None))
                except Exception: continue
            return fields

    def _get_label(self, element, dialog) -> str:
        try:
            v = element.get_attribute("aria-label") or ""
            if v.strip():
                return v.strip()
            lid = element.get_attribute("aria-labelledby") or ""
            if lid:
                for part in lid.split():
                    try:
                        text = dialog.locator(f"#{part}").first.inner_text()
                        if text.strip():
                            return text.strip()
                    except Exception:
                        pass
            eid = element.get_attribute("id") or ""
            if eid:
                try:
                    text = dialog.locator(f"label[for='{eid}']").first.inner_text()
                    if text.strip():
                        return text.strip()
                except Exception:
                    pass
            ph = element.get_attribute("placeholder") or ""
            if ph.strip():
                return ph.strip()
        except Exception:
            pass
        return ""

    def _get_group_label(self, name: str, dialog) -> str:
        try:
            radio = dialog.locator(f"input[name='{name}']").first
            legend = radio.locator("xpath=ancestor::fieldset//legend").first
            text = legend.inner_text()
            if text.strip():
                return text.strip()
        except Exception:
            pass
        return name

    def _get_step_title(self, dialog) -> str:
        try:
            return dialog.locator("h3, h2").first.inner_text().strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # LLM-powered field filling
    # ------------------------------------------------------------------

    _PLACEHOLDER_VALUES = {
        "select an option", "please select", "select...", "-- select --",
        "select one", "choose an option", "choose one", "none", "--",
    }

    def _needs_filling(self, f: "FormField") -> bool:
        """True when the field is blank or only contains a placeholder."""
        v = f.current_value.strip().lower()
        return not v or v in self._PLACEHOLDER_VALUES

    def _fill_fields(self, page: Page, dialog, fields: List[FormField], fix_mode: bool = False) -> List[str]:
        empty = [f for f in fields if self._needs_filling(f) or fix_mode]
        if not empty:
            return []

        answers = self._get_answers(empty)
        filled = []

        for f in empty:
            answer = answers.get(f.label, "").strip()
            if not answer or (fix_mode and f.kind == "number"):
                answer = self._heuristic_answer(f, fix_mode=fix_mode)
            if not answer:
                continue
            try:
                if self._fill_single_field(page, dialog, f, answer):
                    filled.append(f.label)
                    log.info("  ✓ '%s' = '%s'", f.label, answer)
            except Exception as exc:
                log.warning("  ✗ '%s': %s", f.label, exc)

        return filled

    def _get_answers(self, fields: List[FormField]) -> Dict[str, str]:
        fields_json = json.dumps([
            {"label": f.label, "kind": f.kind, "options": f.options, "required": f.required}
            for f in fields
        ], indent=2)
        profile_json = json.dumps({
            k: self.profile.get(k) for k in [
                "full_name", "title", "email", "phone", "location",
                "experience_years", "skills", "education",
                "work_authorization", "requires_visa_sponsorship",
                "visa_status", "us_citizen", "veteran_status",
                "disability_status", "gender", "ethnicity",
            ]
        }, indent=2)
        exp = int(self.profile.get("experience_years", 3))
        try:
            return self._llm.answer_fields(fields_json, profile_json, exp)
        except Exception as exc:
            log.warning("LLM failed: %s — using heuristics", exc)
            return {}

    def _lookup_custom(self, label_lower: str) -> str:
        """Check question_answers.json (contains matching) for a known answer."""
        for key, value in self._custom_answers.items():
            if key and key in label_lower:
                return value
        return ""

    def _heuristic_answer(self, f: FormField, fix_mode: bool = False) -> str:
        """Rule-based fallback for common LinkedIn form questions."""
        exp = str(self.profile.get("experience_years", 3))
        # In fix_mode for numbers, if the previous value failed, we'll try "0" or "1" as a safe minimum
        if fix_mode and f.kind == "number":
             return "0"

        label_l = f.label.lower()

        # Check custom question_answers.json first
        custom = self._lookup_custom(label_l)
        if custom:
            # For select fields, validate the answer is actually one of the options
            if f.kind == "select" and f.options:
                opts_lower = {o.lower(): o for o in f.options}
                matched = opts_lower.get(custom.lower())
                if matched:
                    return matched
            else:
                return custom

        if "year" in label_l and any(w in label_l for w in ("experience", "ml", "ai", "machine", "python", "data")):
            return exp
        if "sponsor" in label_l:
            return "No"
        # Citizenship: not a US citizen
        if any(t in label_l for t in ("us citizen", "u.s. citizen", "u s citizen", "citizenship status", "are you a citizen")):
            return self.profile.get("us_citizen", "No")
        # Work authorization: yes (H1B = authorized to work)
        if "authorized to work" in label_l or "legally authorized" in label_l or "work authorization" in label_l or "work auth" in label_l:
            return self.profile.get("work_authorization", "Yes")
        # Generic "authorized" without citizenship context → work authorized = Yes
        if "authorized" in label_l and "citizen" not in label_l:
            return "Yes"
        if "relocat" in label_l:
            return "No"
        if "veteran" in label_l:
            return self.profile.get("veteran_status", "No")
        if "disability" in label_l:
            return self.profile.get("disability_status", "No")
        if "gender" in label_l:
            return self.profile.get("gender", "")
        if "ethnicity" in label_l or "race" in label_l:
            return self.profile.get("ethnicity", "")
        if f.kind == "radio" and f.options:
            # For resume selection radio groups, pick the option that matches
            # the configured resume filename (e.g. "Deep resume.pdf").
            if any(t in label_l for t in ("resume", "cv", "upload")):
                resume_name = self.profile.get("_resume_filename", "").lower()
                if resume_name:
                    stem = resume_name.replace(".pdf", "").lower()
                    for opt in f.options:
                        ol = opt.lower()
                        if resume_name in ol or stem in ol:
                            return opt
            yes_opts = [o for o in f.options if "yes" in o.lower()]
            if yes_opts:
                return yes_opts[0]
            # Unknown radio — ask AI to pick the right option
            ai_answer = self._ai_select(f.label, f.options)
            return ai_answer if ai_answer else f.options[0]

        if f.kind == "select" and f.options:
            non_placeholder = [o for o in f.options if o.strip() and "select" not in o.lower()]
            if not non_placeholder:
                return ""
            # Unknown select — ask AI to pick the right option
            ai_answer = self._ai_select(f.label, f.options)
            return ai_answer if ai_answer else non_placeholder[0]

        return ""

    def _ai_select(self, question: str, options: List[str]) -> str:
        """Ask Claude Haiku to pick the best option for an unknown question."""
        try:
            from ..ai_assistant import select_answer
            return select_answer(question, options)
        except Exception as exc:
            log.warning("AI select_answer unavailable: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Field filling mechanics
    # ------------------------------------------------------------------

    def _fill_single_field(self, page: Page, dialog, f: FormField, value: str) -> bool:
        el = self._locate_by_label(dialog, f.label)
        if not el or not el.is_visible():
            return False

        # Apply "The DOM Feature": Native Value Injection
        if f.kind in ("text", "number", "textarea"):
            try:
                if f.kind == "number":
                    value = "".join(filter(str.isdigit, value))

                # Detect LinkedIn typeahead/autocomplete fields (e.g. Location city).
                # These need real typing to trigger the suggestion dropdown, then a
                # click on the first matching suggestion.
                is_typeahead = el.evaluate(
                    "e => e.id.includes('typeahead') || e.getAttribute('role') === 'combobox' || "
                    "e.getAttribute('aria-autocomplete') === 'list'"
                )
                if is_typeahead:
                    el.triple_click()
                    el.type(value[:6], delay=50)   # type first 6 chars to trigger suggestions
                    page.wait_for_timeout(1200)
                    # Click the first suggestion that contains our value
                    for opt in page.locator("[role='option']").all():
                        try:
                            if opt.is_visible(timeout=500):
                                text = opt.inner_text().strip()
                                if value.split(",")[0].lower() in text.lower():
                                    opt.click()
                                    return True
                        except Exception:
                            continue
                    # No suggestion matched — clear and type full value
                    el.triple_click()
                    page.keyboard.type(value)
                    return True

                # Set value directly and trigger input/change events
                el.evaluate("(e, val) => { e.value = val; e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true })); }", value)
                return True
            except Exception:
                # Fallback to standard typing
                el.triple_click()
                page.keyboard.type(value)
                return True

        elif f.kind == "select":
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    el.select_option(label=value)
                    return True
                else:
                    # Custom ARIA-style selects (Turing fix)
                    el.click()
                    page.wait_for_timeout(800)
                    for opt in page.locator("[role='option']").all():
                        try:
                            text = opt.inner_text().strip()
                            if value.lower() in text.lower() or text.lower() in value.lower():
                                opt.click()
                                return True
                        except Exception: continue
            except Exception: return False

        elif f.kind == "radio":
            try:
               # Find specifically the radio within the legend group
               for radio in dialog.locator("input[type='radio']").all():
                   rl = self._get_label(radio, dialog)
                   if value.lower() in rl.lower() or rl.lower() in value.lower():
                       radio.evaluate("e => { e.checked = true; e.dispatchEvent(new Event('change', { bubbles: true })); e.click(); }")
                       return True
            except Exception: return False

        return False

    def _locate_by_label(self, dialog, label: str):
        # LinkedIn sometimes doubles label text (visible span + aria span).
        # Deduplicate: if the string is exactly doubled, use the first half.
        clean_label = label.strip()
        half = len(clean_label) // 2
        if half > 8 and clean_label[:half].strip() == clean_label[half:].strip():
            clean_label = clean_label[:half].strip()

        for lbl in [clean_label, label]:
            try:
                loc = dialog.get_by_label(lbl, exact=False).first
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    def _click_easy_apply(self, page: Page) -> bool:
        for selector in [
            "button:has-text('Easy Apply')",
            "button[aria-label*='Easy Apply']",
            ".jobs-apply-button",
            "button:has-text('Apply now')",
            "button[aria-label*='Apply now']",
        ]:
            try:
                # Iterate all matches — LinkedIn renders a hidden duplicate button
                # as the first DOM element; we need the actually-visible one.
                for btn in page.locator(selector).all():
                    try:
                        if btn.is_visible(timeout=500):
                            btn.click()
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _check_for_errors(self, dialog) -> bool:
        """Check if the dialog shows any validation errors (e.g. required field missing or format error)."""
        try:
             # Common LinkedIn error message containers
             error_selectors = [
                 ".artdeco-inline-feedback--error",
                 ".fb-dash-error-message",
                 "[role='alert']",
                 "text=/please enter|required/i"
             ]
             for selector in error_selectors:
                 if dialog.locator(selector).first.is_visible(timeout=500):
                     return True
        except Exception:
            pass
        return False

    def _get_dialog(self, page: Page):
        try:
            dialog = page.locator("[role='dialog']").first
            if dialog.is_visible(timeout=2000):
                return dialog
        except Exception:
            pass
        return None

    def _has_button(self, dialog, labels: List[str]) -> bool:
        for label in labels:
            try:
                if dialog.get_by_role("button", name=label, exact=False).first.is_visible(timeout=2000):
                    return True
            except Exception:
                continue
        return False

    def _click_button(self, dialog, labels: List[str]) -> bool:
        for label in labels:
            try:
                btn = dialog.get_by_role("button", name=label, exact=False).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    return True
            except Exception:
                continue
        return False

    def _is_already_applied(self, page: Page) -> bool:
        try:
            if "post-apply" in page.url:
                return True
            if page.locator("text=Applied").first.is_visible(timeout=500):
                return True
        except Exception:
            pass
        return False
