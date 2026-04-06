import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error, Page, sync_playwright

from .apply import LinkedInBridgeDriver
from .browser import open_browser_session, prepare_work_page
from .chrome_mcp_client import ChromeMcpClient, ChromeMcpError
from .config import Settings
from .eligibility import analyze_job_fit
from .linkedin import LINKEDIN_LISTINGS_SCRIPT, build_linkedin_job_url, build_linkedin_search_url
from .models import JobRecord
from .utils import normalize_text, sanitize_filename, utc_now


FORM_FIELDS_SCRIPT = """
() => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };

  const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();

  const labelText = (element) => {
    const chunks = [];
    if (element.labels) {
      for (const label of element.labels) {
        chunks.push(label.innerText || label.textContent || '');
      }
    }
    const labelledBy = (element.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      for (const id of labelledBy.split(/\\s+/)) {
        const node = document.getElementById(id);
        if (node) chunks.push(node.innerText || node.textContent || '');
      }
    }
    const aria = element.getAttribute('aria-label');
    if (aria) chunks.push(aria);
    const placeholder = element.getAttribute('placeholder');
    if (placeholder) chunks.push(placeholder);

    let wrapper = element.closest('label, fieldset, .application-question, .question, .form-group, .input-wrapper, .jobs-apply-form__field, .postings-btn-wrapper, li, div');
    for (let depth = 0; wrapper && depth < 5; depth += 1) {
      const legend = wrapper.querySelector('legend');
      if (legend) chunks.push(legend.innerText || legend.textContent || '');
      const heading = wrapper.querySelector('h1, h2, h3, h4, strong');
      if (heading) chunks.push(heading.innerText || heading.textContent || '');
      const wrapperLabel = wrapper.querySelector('label');
      if (wrapperLabel) chunks.push(wrapperLabel.innerText || wrapperLabel.textContent || '');
      wrapper = wrapper.parentElement;
    }

    return normalize(chunks.join(' '));
  };

  const optionLabel = (element) => {
    const container = element.closest('label, li, div, span');
    return container ? normalize(container.innerText || container.textContent || '') : '';
  };

  let index = 0;
  const fields = [];
  const dialogCandidates = Array.from(document.querySelectorAll("[role='dialog'][aria-modal='true'], dialog[open], [role='dialog']"));
  const scope = dialogCandidates.find((element) => isVisible(element)) || document;
  for (const element of scope.querySelectorAll('input, textarea, select')) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    const role = (element.getAttribute('role') || '').toLowerCase();
    const isFile = type === 'file';
    if ((!isVisible(element) && !isFile) || element.disabled) continue;
    if (type === 'hidden') continue;
    index += 1;
    const marker = `job-bot-field-${index}`;
    element.setAttribute('data-job-bot-field', marker);
    const label = labelText(element);
    const name = element.getAttribute('name') || '';
    const placeholder = element.getAttribute('placeholder') || '';
    const aria = element.getAttribute('aria-label') || '';
    if (!label && !name && !placeholder && !aria && !isFile && role != 'combobox') continue;
    fields.push({
      id: marker,
      nativeId: element.id || '',
      tag,
      type,
      role,
      name,
      label,
      required: !!element.required || element.getAttribute('aria-required') === 'true' || /\\*/.test(label),
      accept: element.getAttribute('accept') || '',
      multiple: !!element.multiple,
      optionLabel: optionLabel(element),
      options: tag === 'select'
        ? Array.from(element.options).map((option) => ({
            value: option.value,
            text: normalize(option.textContent || ''),
          }))
        : [],
    });
  }
  return fields;
}
"""

APPLY_CONTROLS_SCRIPT = """
() => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  let index = 0;
  return Array.from(document.querySelectorAll('a, button, input[type="button"], input[type="submit"]'))
    .map((element) => {
      index += 1;
      const marker = `job-bot-action-${index}`;
      element.setAttribute('data-job-bot-action', marker);
      const text = (element.innerText || element.textContent || element.value || '').replace(/\\s+/g, ' ').trim();
      return {
        id: marker,
        text,
        href: element.tagName.toLowerCase() === 'a' ? element.href : '',
        visible: isVisible(element),
      };
    })
    .filter((item) => item.visible && /apply( now| today)?/i.test(item.text));
}
"""

OVERLAY_CONTROLS_SCRIPT = """
() => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  let index = 0;
  return Array.from(document.querySelectorAll('button, a'))
    .map((element) => {
      index += 1;
      const marker = `job-bot-overlay-${index}`;
      element.setAttribute('data-job-bot-overlay', marker);
      const text = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
      return {
        id: marker,
        text,
        visible: isVisible(element),
      };
    })
    .filter(
      (item) =>
        item.visible &&
        /accept|agree|allow|close|dismiss|got it|ok/i.test(item.text) &&
        !/save and close|save application|submit|review|apply|continue|preview/i.test(item.text)
    )
    .slice(0, 10);
}
"""


class JobApplicationService:
    def __init__(self, settings: Settings, profile: Dict[str, str], answers: Dict[str, Dict[str, str]]) -> None:
        self.settings = settings
        self.profile = profile
        self.first_name, self.last_name = self._split_name(profile.get("full_name", ""))
        self.max_experience_years = int(profile.get("max_target_experience_years", 4) or 4)
        self.current_provider = ""
        self.exact_answers = {normalize_text(key): value for key, value in answers.get("exact", {}).items()}
        self.contains_answers = {normalize_text(key): value for key, value in answers.get("contains", {}).items()}
        self._artifact_counters: Dict[str, int] = {}

    def apply_jobs(self, jobs: List[JobRecord], submit_mode: Optional[str] = None, on_job_complete: Optional[Callable[[JobRecord], None]] = None) -> List[JobRecord]:
        if not jobs:
            return jobs

        effective_mode = (submit_mode or self.settings.submit_mode or "review").lower()
        updated_jobs: List[JobRecord] = []

        with sync_playwright() as playwright:
            session = open_browser_session(playwright, self.settings)
            try:
                context = session.context
                page = prepare_work_page(context)
                page.set_default_timeout(self.settings.timeout_ms)
                for job in jobs:
                    self._artifact_counters.pop(job.job_id, None)
                    updated = self._apply_single_job(page, job, effective_mode)
                    updated_jobs.append(updated)
                    if on_job_complete:
                        on_job_complete(updated)
                    time.sleep(self.settings.delay_ms / 1000.0)
            finally:
                session.close()

        return updated_jobs

    def _apply_single_job(self, page: Page, job: JobRecord, submit_mode: str) -> JobRecord:
        self.current_provider = job.provider
        try:
            if job.provider == "linkedin":
                return self._apply_linkedin_job(page, job, submit_mode)

            page.goto(job.apply_url or job.source_url, wait_until="domcontentloaded")
            if job.provider == "indeed":
                return self._apply_indeed_job(page, job, submit_mode)
            return self._apply_generic_job(page, job, submit_mode)
        except Error as exc:
            return self._finalize(page, job, "failed", f"Playwright error: {exc}", submit_mode)

    def _apply_generic_job(self, page: Page, job: JobRecord, submit_mode: str) -> JobRecord:
        self._wait_for_idle(page)
        page.evaluate("window.scrollTo(0, 0)")
        self._dismiss_overlays(page)
        self._open_application_form(page)
        fields = self._extract_fields(page)
        if not fields:
            return self._finalize(page, job, "failed", "No visible application form found", submit_mode)

        unresolved_required = self._fill_fields(page, fields)
        if unresolved_required:
            if submit_mode == "auto":
                return self._finalize(
                    page,
                    job,
                    "review_required",
                    f"Missing answers for required fields: {', '.join(unresolved_required[:5])}",
                    submit_mode,
                )
            return self._finalize(
                page,
                job,
                "review_required",
                f"Filled known fields. Manual answers still needed: {', '.join(unresolved_required[:5])}",
                submit_mode,
            )

        if submit_mode != "auto":
            return self._finalize(page, job, "ready_to_submit", "Filled all known fields in review mode", submit_mode)

        submitted = self._submit_form(page)
        if submitted and self._wait_for_submission_confirmation(page):
            return self._finalize(
                page,
                job,
                "submitted",
                "Application submitted",
                submit_mode,
                include_timestamp=True,
                verified_submission=True,
            )

        return self._finalize(page, job, "review_required", "Submit confirmation was not detected", submit_mode)

    def _apply_indeed_job(self, page: Page, job: JobRecord, submit_mode: str) -> JobRecord:
        self._wait_for_idle(page)
        self._dismiss_overlays(page)
        self._capture_progress_screenshot(page, job, "indeed-open")
        self._ensure_indeed_detail_page(page, job)

        if not self._wait_until_actionable(
            page,
            ["h1", "#jobDescriptionText"] + self._indeed_apply_selectors() + self._indeed_external_apply_selectors(),
        ):
            return self._finalize(page, job, "review_required", "Indeed sign-in or verification is required", submit_mode)

        if self._indeed_job_closed(page):
            return self._finalize(
                page,
                job,
                "review_required",
                "Indeed listing is closed or no longer accepting applications",
                submit_mode,
            )

        if self._body_has_any(page, ["already applied", "you already applied", "application submitted earlier"]):
            return self._finalize(page, job, "review_required", "Indeed already shows this job as applied", submit_mode)

        self._select_indeed_listing(page, job)
        self._ensure_indeed_detail_page(page, job)

        if not self._click_visible_control(page, self._indeed_apply_selectors()):
            external_page = self._open_indeed_external_apply(page)
            if external_page is None:
                if self._indeed_job_closed(page):
                    return self._finalize(
                        page,
                        job,
                        "review_required",
                        "Indeed listing is closed or no longer accepting applications",
                        submit_mode,
                    )
                return self._finalize(page, job, "review_required", "Indeed Apply button was not found", submit_mode)
            try:
                return self._apply_generic_job(external_page, job, submit_mode)
            finally:
                if external_page is not page:
                    try:
                        external_page.close()
                    except Error:
                        pass

        if not self._wait_until_actionable(
            page,
            [
                "input",
                "textarea",
                "select",
                "button:has-text('Continue')",
                "button:has-text('Review')",
                "button:has-text('Submit')",
            ],
        ):
            return self._finalize(page, job, "review_required", "Indeed application dialog did not become ready", submit_mode)

        for step in range(1, 9):
            self._wait_for_idle(page)
            self._dismiss_overlays(page)
            self._capture_progress_screenshot(page, job, f"indeed-step-{step}")

            if self._indeed_submission_succeeded(page):
                return self._finalize(
                    page,
                    job,
                    "submitted",
                    "Indeed application submitted",
                    submit_mode,
                    include_timestamp=True,
                    verified_submission=True,
                )

            fields = self._extract_fields(page)
            if fields:
                unresolved_required = self._fill_fields(page, fields)
                if unresolved_required:
                    return self._finalize(
                        page,
                        job,
                        "review_required",
                        f"Missing answers for required Indeed fields: {', '.join(unresolved_required[:5])}",
                        submit_mode,
                    )

            if self._has_any_selector(page, self._indeed_submit_selectors()):
                if submit_mode != "auto":
                    return self._finalize(page, job, "ready_to_submit", "Filled Indeed application in review mode", submit_mode)
                if not self._click_visible_control(page, self._indeed_submit_selectors()):
                    return self._finalize(page, job, "review_required", "Indeed submit button was present but not clickable", submit_mode)
                continue

            if self._has_any_selector(page, self._indeed_review_selectors()):
                if submit_mode != "auto":
                    return self._finalize(page, job, "ready_to_submit", "Filled Indeed application in review mode", submit_mode)
                if not self._click_visible_control(page, self._indeed_review_selectors()):
                    return self._finalize(page, job, "review_required", "Indeed review step could not be opened", submit_mode)
                continue

            if self._has_any_selector(page, self._indeed_continue_selectors()):
                if not self._click_visible_control(page, self._indeed_continue_selectors()):
                    return self._finalize(page, job, "review_required", "Indeed continue button was present but not clickable", submit_mode)
                continue

            if self._indeed_submission_succeeded(page):
                return self._finalize(
                    page,
                    job,
                    "submitted",
                    "Indeed application submitted",
                    submit_mode,
                    include_timestamp=True,
                    verified_submission=True,
                )

            if not fields:
                return self._finalize(page, job, "review_required", "No visible Indeed fields were found on the current step", submit_mode)

            return self._finalize(page, job, "review_required", "Could not determine the next Indeed application action", submit_mode)

        return self._finalize(page, job, "review_required", "Indeed application exceeded the supported step limit", submit_mode)

    def _apply_linkedin_job(self, page: Page, job: JobRecord, submit_mode: str) -> JobRecord:
        # --- Chrome MCP bridge: fast path using the existing logged-in Chrome ---
        # Runs first because the bridge reads the live DOM directly and is not
        # affected by LinkedIn's hidden-duplicate Easy Apply button that trips
        # Playwright's is_visible() check.
        bridge_result = self._apply_linkedin_job_with_bridge(job, submit_mode)
        if bridge_result is not None:
            return bridge_result

        # --- Vision applier: LLM-guided Playwright fallback ---
        vision_result = self._apply_linkedin_job_with_vision(page, job, submit_mode)
        if vision_result is not None:
            return vision_result

        # Navigate directly to the job URL instead of searching for it in results
        job_url = job.apply_url or job.source_url
        if not job_url:
            return self._finalize(page, job, "review_required", "No job URL available", submit_mode)
        working_page = page.context.new_page()
        working_page.set_default_timeout(min(self.settings.timeout_ms, 25000))
        try:
            working_page.goto(job_url, wait_until="domcontentloaded")
        except Exception:
            pass
        try:
            self._wait_for_idle(working_page)
            self._dismiss_overlays(working_page)
            self._capture_progress_screenshot(working_page, job, "linkedin-result-selected")

            if not self._wait_until_actionable(
                working_page,
                [
                    "h1",
                    "button.jobs-apply-button",
                    "button:has-text('Easy Apply')",
                    ".jobs-description-content__text",
                ],
            ):
                return self._finalize(working_page, job, "review_required", "LinkedIn sign-in or verification is required", submit_mode)

            if self._linkedin_job_page_marked_applied(working_page):
                return self._finalize(
                    working_page,
                    job,
                    "submitted",
                    "LinkedIn already shows this job as applied",
                    submit_mode,
                    include_timestamp=True,
                    verified_submission=True,
                )

            self._scroll_linkedin_description(working_page)
            self._expand_linkedin_description(working_page)
            self._scroll_linkedin_description(working_page)
            self._capture_progress_screenshot(working_page, job, "linkedin-description")

            title = self._page_text(
                working_page,
                [
                    "h1",
                    ".jobs-unified-top-card__job-title",
                    ".job-details-jobs-unified-top-card__job-title",
                ],
            )
            description = self._page_text(
                working_page,
                [
                    ".jobs-description-content__text",
                    ".jobs-description__content",
                    ".jobs-box__html-content",
                    ".jobs-description__container",
                    "#job-details",
                ],
            )
            top_card_text = self._page_text(
                working_page,
                [
                    ".job-details-jobs-unified-top-card__container--two-pane",
                    ".job-details-jobs-unified-top-card",
                    ".jobs-unified-top-card",
                ],
            )
            description = " ".join(part for part in (top_card_text, description or job.description) if part)
            fit = analyze_job_fit(
                title=title or job.title,
                description=description,
                require_contract=False,  # LinkedIn URL already filters by contract job type (f_JT=C)
                max_experience_years=self.max_experience_years,
            )
            if not fit["eligible"]:
                reason = ", ".join(str(item) for item in fit["reasons"][:2])
                return self._finalize(working_page, job, "review_required", f"Skipped by criteria: {reason}", submit_mode)

            working_page.evaluate("window.scrollTo(0, 0)")
            working_page.wait_for_timeout(300)

            if not self._click_visible_control(working_page, self._linkedin_apply_selectors()):
                return self._finalize(working_page, job, "review_required", "LinkedIn Easy Apply button was not found", submit_mode)

            if not self._wait_until_actionable(
                working_page,
                [
                    "input",
                    "textarea",
                    "select",
                    "button:has-text('Next')",
                    "button:has-text('Review')",
                    "button:has-text('Submit')",
                    "button:has-text('Submit application')",
                ],
            ):
                return self._finalize(working_page, job, "review_required", "LinkedIn application dialog did not become ready", submit_mode)

            for step in range(1, 11):
                self._wait_for_idle(working_page)
                self._dismiss_overlays(working_page)
                self._capture_progress_screenshot(working_page, job, f"linkedin-dialog-step-{step}")

                if self._linkedin_submission_screen_visible(working_page):
                    return self._complete_linkedin_submission(working_page, job, submit_mode)

                if self._linkedin_job_page_marked_applied(working_page):
                    return self._finalize(
                        working_page,
                        job,
                        "submitted",
                        "LinkedIn shows this job as applied",
                        submit_mode,
                        include_timestamp=True,
                        verified_submission=True,
                    )

                fields = self._extract_fields(working_page)
                if fields:
                    unresolved_required = self._fill_fields(working_page, fields)
                    if unresolved_required:
                        return self._finalize(
                            working_page,
                            job,
                            "review_required",
                            f"Missing answers for required LinkedIn fields: {', '.join(unresolved_required[:5])}",
                            submit_mode,
                        )

                if self._has_any_selector(working_page, self._linkedin_submit_selectors()):
                    if submit_mode != "auto":
                        return self._finalize(working_page, job, "ready_to_submit", "Filled LinkedIn application in review mode", submit_mode)
                    if not self._click_visible_control(working_page, self._linkedin_submit_selectors()):
                        return self._finalize(working_page, job, "review_required", "LinkedIn submit button was present but not clickable", submit_mode)
                    working_page.wait_for_timeout(1000)
                    continue

                if self._has_any_selector(working_page, self._linkedin_review_selectors()):
                    if submit_mode != "auto":
                        return self._finalize(working_page, job, "ready_to_submit", "Filled LinkedIn application in review mode", submit_mode)
                    if not self._click_visible_control(working_page, self._linkedin_review_selectors()):
                        return self._finalize(working_page, job, "review_required", "LinkedIn review step could not be opened", submit_mode)
                    working_page.wait_for_timeout(700)
                    continue

                if self._has_any_selector(working_page, self._linkedin_continue_selectors()):
                    if not self._click_visible_control(working_page, self._linkedin_continue_selectors()):
                        return self._finalize(working_page, job, "review_required", "LinkedIn continue button was present but not clickable", submit_mode)
                    working_page.wait_for_timeout(700)
                    continue

                if self._linkedin_submission_screen_visible(working_page):
                    return self._complete_linkedin_submission(working_page, job, submit_mode)

                if self._linkedin_job_page_marked_applied(working_page):
                    return self._finalize(
                        working_page,
                        job,
                        "submitted",
                        "LinkedIn shows this job as applied",
                        submit_mode,
                        include_timestamp=True,
                        verified_submission=True,
                    )

                if not fields:
                    return self._finalize(working_page, job, "review_required", "No visible LinkedIn fields were found on the current step", submit_mode)

                return self._finalize(working_page, job, "review_required", "Could not determine the next LinkedIn application action", submit_mode)

            return self._finalize(working_page, job, "review_required", "LinkedIn application exceeded the supported step limit", submit_mode)
        finally:
            if working_page != page:
                try:
                    working_page.close()
                except Error:
                    pass
                try:
                    page.bring_to_front()
                except Error:
                    pass

    def _apply_linkedin_job_with_vision(self, page: Page, job: JobRecord, submit_mode: str) -> Optional[JobRecord]:
        """
        Primary apply path — uses VisionApplier (real Playwright mouse/keyboard +
        LLM for answering unknown form questions). Same approach as Claude computer use.
        """
        from .apply.vision_applier import VisionApplier
        import os

        job_url = job.apply_url or job.source_url
        if not job_url:
            return None

        profile_for_vision = {**self.profile, "_resume_filename": Path(str(self.settings.resume_path)).name}
        applier = VisionApplier(profile_for_vision, custom_answers=self.contains_answers)

        working_page = page.context.new_page()
        working_page.set_default_timeout(min(self.settings.timeout_ms, 30_000))
        try:
            result = applier.apply(
                working_page,
                job_url,
                auto_submit=(submit_mode == "auto"),
            )

            status_map = {
                "submitted": "submitted",
                "already_applied": "submitted",
                "ready_to_submit": "ready_to_submit",
                "review_required": "review_required",
                "error": "review_required",
            }
            final_status = status_map.get(result.status, "review_required")
            return self._finalize(
                working_page,
                job,
                final_status,
                result.message,
                submit_mode,
                include_timestamp=(final_status == "submitted"),
                verified_submission=(result.status in ("submitted", "already_applied")),
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("VisionApplier failed for %s: %s", job.title, exc)
            return None
        finally:
            try:
                working_page.close()
            except Exception:
                pass
            try:
                page.bring_to_front()
            except Exception:
                pass

    def _apply_linkedin_job_with_bridge(self, job: JobRecord, submit_mode: str) -> Optional[JobRecord]:
        try:
            with ChromeMcpClient(self._chrome_mcp_ws_url()) as client:
                driver = LinkedInBridgeDriver(client)
                tab = driver.locate_active_jobs_tab()
                if tab is None:
                    return None

                job_id = self._linkedin_job_id(job)
                target_url = build_linkedin_job_url(job_id) if job_id else (job.source_url or job.apply_url or "")
                if not target_url:
                    return None

                wait_for_url = job_id or "/jobs/view/"
                page_snapshot = driver.go_to_url(tab.id, target_url, wait_for_url_contains=wait_for_url)
                page_snapshot = driver.read_current_state(tab.id, filter_mode="all", scope="page", limit=140)

                if driver.detect_success_state(page_snapshot):
                    return self._bridge_finalize(
                        job,
                        "submitted",
                        "LinkedIn already shows this job as applied",
                        submit_mode,
                        driver=driver,
                        snapshot=page_snapshot,
                        include_timestamp=True,
                        verified_submission=True,
                    )

                title = job.title or page_snapshot.title
                description = " ".join(part for part in (page_snapshot.visible_text_excerpt, job.description) if part)
                fit = analyze_job_fit(
                    title=title,
                    description=description,
                    require_contract=False,  # LinkedIn URL already filters by contract job type
                    max_experience_years=self.max_experience_years,
                )
                if not fit["eligible"]:
                    reason = ", ".join(str(item) for item in fit["reasons"][:2])
                    return self._bridge_finalize(
                        job,
                        "review_required",
                        f"Skipped by criteria: {reason}",
                        submit_mode,
                        driver=driver,
                        snapshot=page_snapshot,
                    )

                dialog_snapshot = driver.open_easy_apply(tab.id)
                if driver.detect_success_state(dialog_snapshot):
                    return self._bridge_finalize(
                        job,
                        "submitted",
                        "LinkedIn already shows this job as applied",
                        submit_mode,
                        driver=driver,
                        snapshot=dialog_snapshot,
                        include_timestamp=True,
                        verified_submission=True,
                    )

                if not driver._dialog_ready(dialog_snapshot):
                    return self._bridge_finalize(
                        job,
                        "review_required",
                        "LinkedIn Easy Apply dialog did not open through the Chrome bridge",
                        submit_mode,
                        driver=driver,
                        snapshot=dialog_snapshot,
                    )

                _last_action_ids: list = []
                _stuck_count = 0
                # Carry the snapshot from open_easy_apply into the first advance_application call
                # so it doesn't re-read during a React transition and get 0 elements.
                _carry_snapshot: Optional[Any] = dialog_snapshot
                for _ in range(8):
                    if _carry_snapshot is None:
                        # Wait for the next step to fully load before reading snapshot.
                        # Uses the same stability logic as open_easy_apply to avoid
                        # reading mid-React-transition (0 elements or spinner-only state).
                        dialog_snapshot = driver._wait_for_dialog_content(tab.id, timeout_seconds=8.0)
                    values_by_label = self._bridge_values_by_label(dialog_snapshot)
                    action_result = driver.advance_application(
                        tab.id,
                        values_by_label=values_by_label or None,
                        auto_submit=submit_mode == "auto",
                        max_steps=1,
                        refresh_limit=140,
                        initial_snapshot=_carry_snapshot,
                    )
                    _carry_snapshot = None  # only use carried snapshot once
                    current_scope = "page" if action_result.submitted else "dialog-interactive"
                    current_filter = "all" if action_result.submitted else "interactive"
                    current_snapshot = driver.read_current_state(tab.id, filter_mode=current_filter, scope=current_scope, limit=140)

                    if action_result.submitted or driver.detect_success_state(current_snapshot):
                        return self._bridge_finalize(
                            job,
                            "submitted",
                            "LinkedIn application submitted",
                            submit_mode,
                            driver=driver,
                            snapshot=current_snapshot,
                            include_timestamp=True,
                            verified_submission=True,
                        )

                    if action_result.status == "ready_to_submit":
                        return self._bridge_finalize(
                            job,
                            "ready_to_submit",
                            "Filled LinkedIn application in review mode",
                            submit_mode,
                            driver=driver,
                            snapshot=current_snapshot,
                        )

                    if action_result.status == "review_required" and not action_result.actions:
                        break

                    # Detect stuck loop: same control clicked with no progress → fall back to Playwright
                    if action_result.actions and action_result.actions == _last_action_ids:
                        _stuck_count += 1
                        if _stuck_count >= 2:
                            # Bridge can't advance this form (likely trusted-event restriction)
                            # Fall through to Playwright CDP path which uses real browser clicks
                            return None
                    else:
                        _stuck_count = 0
                    _last_action_ids = list(action_result.actions)

                    import time as _time
                    _time.sleep(0.5)  # brief pause before wait-for-dialog on next iteration

                # Bridge exhausted its step budget without completing.
                # Return None so the Playwright/VisionApplier path gets a chance
                # to handle the form (it can interact with selects / unknown fields
                # that the bridge cannot reliably fill via synthetic events).
                import logging as _log_bridge
                _log_bridge.getLogger(__name__).info("Bridge exhausted step budget — falling through to VisionApplier")
                return None
        except ChromeMcpError:
            return None

    def _chrome_mcp_ws_url(self) -> str:
        return f"ws://{self.settings.chrome_mcp_host}:{self.settings.chrome_mcp_port}"

    def _bridge_values_by_label(self, snapshot) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for control in snapshot.interactive_elements:
            tag = control.tag_name.lower()
            role = (control.role or "").lower()
            if tag not in {"input", "textarea", "select"} and role not in {"combobox", "listbox"}:
                continue
            if control.disabled:
                continue
            field = self._bridge_field_to_dict(control)
            label = self._best_label(field)
            if not label:
                continue
            answer = self._resolve_answer(field)
            if answer is None:
                answer = self._smart_fallback_answer(field)
            if answer is None:
                continue
            if self._bridge_control_has_value(control):
                continue
            values.setdefault(label, self._normalized_fill_value(field, answer))
        return values

    def _bridge_field_to_dict(self, control) -> Dict[str, object]:
        role = (control.role or "").lower()
        tag = control.tag_name.lower()
        # Treat combobox/listbox elements as selects so downstream logic handles them
        effective_tag = "select" if role in {"combobox", "listbox"} and tag not in {"input", "textarea", "select"} else tag
        return {
            "id": control.id,
            "tag": effective_tag,
            "type": control.type,
            "role": role,
            "label": control.label or control.aria_label or control.placeholder or control.name or control.text,
            "name": control.name,
            "required": bool(control.required),
            "nativeId": "",
            "optionLabel": control.text,
            "options": [],
        }

    def _bridge_control_has_value(self, control) -> bool:
        value = normalize_text(control.value)
        if control.tag_name.lower() == "select":
            return bool(value and value not in {"select an option"})
        if control.type.lower() in {"checkbox", "radio"}:
            return bool(control.checked)
        return bool(value)

    def _bridge_finalize(
        self,
        job: JobRecord,
        status: str,
        reason: str,
        submit_mode: str,
        *,
        driver: LinkedInBridgeDriver,
        snapshot,
        include_timestamp: bool = False,
        verified_submission: bool = False,
    ) -> JobRecord:
        artifact_path = self._capture_bridge_artifacts(job, status, {"timing": driver.timing_summary(), "snapshot": snapshot.to_dict()})
        job.status = status
        job.reason = reason
        job.screenshot_path = artifact_path
        job.submission_verified = status == "submitted" and verified_submission
        if include_timestamp or status == "submitted":
            job.submitted_at = utc_now().replace(microsecond=0).isoformat()
        elif submit_mode != "auto" and not job.submitted_at:
            job.submitted_at = ""
        return job

    def _capture_bridge_artifacts(self, job: JobRecord, label: str, payload: Dict[str, object]) -> str:
        next_index = self._artifact_counters.get(job.job_id, 0) + 1
        self._artifact_counters[job.job_id] = next_index
        stem = sanitize_filename(f"{job.company}-{job.title}-{next_index:02d}-{label}")
        screenshot_path = self.settings.output_dir / f"{stem}.png"
        json_path = self.settings.output_dir / f"{stem}.json"
        try:
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            )
            time.sleep(0.6)
            subprocess.run(
                ["screencapture", "-x", str(screenshot_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=6,
            )
        except OSError:
            pass

        if screenshot_path.exists():
            return str(screenshot_path)
        if json_path.exists():
            return str(json_path)
        return ""

    def _open_linkedin_results_for_job(self, page: Page, job: JobRecord) -> Optional[Page]:
        search = self.settings.load_linkedin_search()
        query = str(search.get("query", "") or "").strip()
        location = str(search.get("location", "United States") or "United States").strip()
        max_pages = max(1, int(search.get("max_pages", 2) or 2))
        recency_hours = max(1, int(search.get("recency_hours", 168) or 168))
        easy_apply_only = bool(search.get("easy_apply_only", True))
        contract_only = bool(search.get("contract_only", True))
        remote_only = bool(search.get("remote_only", False))
        experience_levels = [str(item).strip() for item in search.get("experience_levels", ["2", "3"]) if str(item).strip()]

        if not query:
            query = job.title or "AI/ML Engineer"

        for page_index in range(max_pages):
            results_url = build_linkedin_search_url(
                query=query,
                location=location,
                recency_hours=recency_hours,
                start=page_index * 25,
                easy_apply_only=easy_apply_only,
                contract_only=contract_only,
                remote_only=remote_only,
                experience_levels=experience_levels,
            )
            page.goto(results_url, wait_until="domcontentloaded")
            self._wait_for_idle(page)
            self._dismiss_overlays(page)
            self._ensure_linkedin_search_filters(
                page,
                easy_apply_only=easy_apply_only,
                contract_only=contract_only,
                remote_only=remote_only,
                experience_levels=experience_levels,
            )
            self._capture_progress_screenshot(page, job, f"linkedin-results-page-{page_index + 1}")
            job_id = self._linkedin_job_id(job)
            if not job_id or not self._linkedin_results_include_job(page, job_id):
                continue

            detail_page = page.context.new_page()
            detail_page.set_default_timeout(min(self.settings.timeout_ms, 25000))
            try:
                detail_page.goto(build_linkedin_job_url(job_id), wait_until="domcontentloaded")
                self._wait_for_idle(detail_page)
                self._dismiss_overlays(detail_page)
                return detail_page
            except Error:
                try:
                    detail_page.close()
                except Error:
                    pass
                continue
        return None

    def _linkedin_results_include_job(self, page: Page, job_id: str) -> bool:
        selectors = [
            f"[data-job-id='{job_id}']",
            f"[data-occludable-job-id='{job_id}']",
            f"a[href*='/jobs/view/{job_id}']",
        ]
        if self._has_any_selector(page, selectors):
            return True
        try:
            payload = page.evaluate(LINKEDIN_LISTINGS_SCRIPT)
        except Error:
            return False
        if not isinstance(payload, list):
            return False
        return any(str(item.get("jobId", "") or "").strip() == job_id for item in payload if isinstance(item, dict))

    def _ensure_linkedin_search_filters(
        self,
        page: Page,
        easy_apply_only: bool,
        contract_only: bool,
        remote_only: bool,
        experience_levels: List[str],
    ) -> None:
        self._wait_for_idle(page)
        if not self._open_linkedin_all_filters(page):
            return

        desired_levels = {str(item).strip() for item in experience_levels if str(item).strip()}
        self._set_linkedin_filter_choice(page, "Past week", True)
        self._set_linkedin_filter_choice(page, "Entry level", "2" in desired_levels)
        self._set_linkedin_filter_choice(page, "Associate", "3" in desired_levels)
        self._set_linkedin_filter_choice(page, "Internship", False)
        self._set_linkedin_filter_choice(page, "Mid-Senior level", False)
        self._set_linkedin_filter_choice(page, "Director", False)
        self._set_linkedin_filter_choice(page, "Executive", False)
        self._set_linkedin_filter_choice(page, "Contract", contract_only)
        self._set_linkedin_filter_choice(page, "Remote", remote_only)
        self._set_linkedin_filter_choice(page, "Toggle Easy Apply filter", easy_apply_only)
        self._click_linkedin_filter_apply(page)
        self._wait_for_idle(page)

    def _open_linkedin_all_filters(self, page: Page) -> bool:
        selectors = [
            "button:has-text('All filters')",
            "button[aria-label*='All filters']",
        ]
        return self._click_visible_control(page, selectors)

    def _click_linkedin_filter_apply(self, page: Page) -> bool:
        selectors = [
            "button:has-text('Show results')",
            "button[aria-label*='Show results']",
        ]
        return self._click_visible_control(page, selectors)

    def _set_linkedin_filter_choice(self, page: Page, label_text: str, desired: bool) -> None:
        script = """
({ targetText, desiredState }) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const target = normalize(targetText);
  const labels = Array.from(document.querySelectorAll('label'));
  for (const label of labels) {
    const text = normalize(label.innerText || label.textContent || '');
    if (!text || !text.startsWith(target)) continue;
    let input = label.querySelector('input');
    const forId = label.getAttribute('for');
    if (!input && forId) input = document.getElementById(forId);
    if (!input) continue;
    const current = !!input.checked;
    if (current === desiredState) return true;
    label.click();
    return true;
  }
  return false;
}
"""
        try:
            page.evaluate(script, {"targetText": label_text, "desiredState": desired})
            page.wait_for_timeout(250)
        except Error:
            return

    def _select_linkedin_listing(self, page: Page, job: JobRecord) -> bool:
        job_id = self._linkedin_job_id(job)
        if not job_id:
            return False

        selectors = [
            f"[data-job-id='{job_id}'] a[href*='/jobs/view/{job_id}']",
            f"[data-occludable-job-id='{job_id}'] a[href*='/jobs/view/{job_id}']",
            f"a[href*='/jobs/view/{job_id}']",
        ]
        expected_title = normalize_text(job.title)
        previous_title = normalize_text(
            self._page_text(
                page,
                [
                    "h1",
                    ".jobs-unified-top-card__job-title",
                    ".job-details-jobs-unified-top-card__job-title",
                ],
            )
        )

        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 3)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    candidate.click(force=True)
                    page.wait_for_timeout(1200)
                except Error:
                    continue
                self._wait_for_idle(page)
                current_title = normalize_text(
                    self._page_text(
                        page,
                        [
                            "h1",
                            ".jobs-unified-top-card__job-title",
                            ".job-details-jobs-unified-top-card__job-title",
                        ],
                    )
                )
                if expected_title and current_title and (expected_title in current_title or current_title in expected_title):
                    return True
                if current_title and current_title != previous_title:
                    return True
        return False

    def _linkedin_job_id(self, job: JobRecord) -> str:
        for value in (job.apply_url, job.source_url):
            match = re.search(r"/jobs/view/(\\d+)", value or "")
            if match:
                return match.group(1)
        return ""

    def _scroll_linkedin_description(self, page: Page) -> None:
        script = """
() => {
  const selectors = [
    '.jobs-description__container',
    '.jobs-description-content__text',
    '.jobs-description__content',
    '#job-details',
  ];
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (!node) continue;
    node.scrollIntoView({ block: 'center', behavior: 'instant' });
    if (node.scrollTop !== undefined) {
      node.scrollTop = Math.min(node.scrollHeight, 800);
    }
    return true;
  }
  window.scrollTo(0, 600);
  return false;
}
"""
        try:
            page.evaluate(script)
            page.wait_for_timeout(350)
        except Error:
            return

    def _expand_linkedin_description(self, page: Page) -> None:
        selectors = [
            "button[aria-label*='see more description']",
            "button[aria-label*='See more description']",
            ".jobs-description__footer-button",
            "button:has-text('See more')",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 3)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    candidate.click(force=True)
                    page.wait_for_timeout(300)
                    return
                except Error:
                    continue

    def _wait_for_idle(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.settings.timeout_ms, 7000))
        except Error:
            return

    def _wait_until_actionable(self, page: Page, selectors: List[str]) -> bool:
        deadline = time.time() + max(self.settings.manual_gate_timeout_ms, 1000) / 1000.0
        while time.time() < deadline:
            if self._has_any_selector(page, selectors):
                return True
            if not self._body_has_any(
                page,
                [
                    "just a moment",
                    "enable javascript and cookies to continue",
                    "verify you are human",
                    "sign in",
                    "continue with google",
                    "continue with email",
                ],
            ):
                self._wait_for_idle(page)
            time.sleep(1.0)
        return self._has_any_selector(page, selectors)

    def _open_application_form(self, page: Page) -> None:
        if self._has_form(page):
            return

        candidates = page.evaluate(APPLY_CONTROLS_SCRIPT)

        for candidate in candidates:
            href = (candidate.get("href") or "").strip()
            if href and href != page.url:
                page.goto(href, wait_until="domcontentloaded")
                self._wait_for_idle(page)
                self._dismiss_overlays(page)
                if self._has_form(page):
                    return
            else:
                try:
                    page.locator(f"[data-job-bot-action='{candidate['id']}']").click(force=True)
                    self._wait_for_idle(page)
                    self._dismiss_overlays(page)
                except Error:
                    continue
                if self._has_form(page):
                    return

    def _has_form(self, page: Page) -> bool:
        try:
            fields = self._extract_fields(page)
            meaningful = [field for field in fields if self._is_meaningful_application_field(field)]
            return len(meaningful) >= 3 or any(str(field.get("type", "")).lower() == "file" for field in meaningful)
        except Error:
            return False

    def _extract_fields(self, page: Page) -> List[Dict[str, object]]:
        return page.evaluate(FORM_FIELDS_SCRIPT)

    def _dismiss_overlays(self, page: Page) -> None:
        if self.current_provider == "linkedin" and self._linkedin_application_dialog_open(page):
            return
        try:
            candidates = page.evaluate(OVERLAY_CONTROLS_SCRIPT)
        except Error:
            return

        for candidate in candidates:
            try:
                page.locator(f"[data-job-bot-overlay='{candidate['id']}']").click(force=True)
                time.sleep(0.2)
            except Error:
                continue

    def _fill_fields(self, page: Page, fields: List[Dict[str, object]]) -> List[str]:
        unresolved_required: List[str] = []
        handled_groups = set()

        for field in fields:
            field_type = str(field.get("type", "")).lower()
            if field_type not in {"radio", "checkbox"}:
                label = self._fill_single_field(page, field)
                if label:
                    tag = str(field.get("tag", ""))
                    role = str(field.get("role", ""))
                    detail = f"{label} [type={field_type or tag}, role={role}]" if (field_type or tag or role) else label
                    import logging as _logging
                    _logging.getLogger(__name__).warning("Unknown required field left for manual review: %s", detail)
                    unresolved_required.append(detail)

        for field in fields:
            field_type = str(field.get("type", "")).lower()
            if field_type not in {"radio", "checkbox"}:
                continue
            group_key = normalize_text(str(field.get("name", "")).strip()) or normalize_text(str(field.get("label", "")).strip())
            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            group = [
                candidate
                for candidate in fields
                if (normalize_text(str(candidate.get("name", "")).strip()) or normalize_text(str(candidate.get("label", "")).strip())) == group_key
            ]
            label = self._fill_choice_group(page, group)
            if label:
                detail = f"{label} [type=radio/checkbox]"
                import logging as _logging
                _logging.getLogger(__name__).warning("Unknown required field left for manual review: %s", detail)
                unresolved_required.append(detail)

        return unresolved_required

    def _fill_single_field(self, page: Page, field: Dict[str, object]) -> str:
        selector = f"[data-job-bot-field='{field['id']}']"
        locator = page.locator(selector)
        label = self._best_label(field)
        normalized_label = normalize_text(label)
        answer = self._resolve_answer(field)
        if answer is None:
            answer = self._smart_fallback_answer(field)
        field_role = str(field.get("role", "")).lower()

        if answer is None:
            if self._field_has_value(locator, field):
                return ""
            return label if field.get("required") else ""

        try:
            if field.get("tag") == "select":
                if self._field_has_value(locator, field):
                    return ""
                if "ethnicity" in normalized_label or "race" in normalized_label:
                    try:
                        locator.select_option(label="Asian (Not Hispanic or Latino)")
                        return ""
                    except Error:
                        pass
                if not self._select_option(locator, field, answer):
                    return label if field.get("required") else ""
            elif field_role == "combobox":
                if not self._fill_combobox_field(page, locator, field, answer):
                    return label if field.get("required") else ""
            elif str(field.get("type", "")).lower() == "file":
                locator.set_input_files(str(self.settings.resume_path))
            else:
                locator.fill(self._normalized_fill_value(field, answer))
        except Error:
            if self._field_has_value(locator, field):
                return ""
            return label if field.get("required") else ""

        return ""

    def _fill_choice_group(self, page: Page, group: List[Dict[str, object]]) -> str:
        label = self._best_label(group[0])
        answer = self._resolve_answer(group[0])
        if answer is None:
            answer = self._smart_fallback_answer(group[0])
        if answer is None:
            if self._choice_group_has_selection(page, group):
                return ""
            return label if any(item.get("required") for item in group) else ""

        normalized_answer = normalize_text(answer)
        for field in group:
            option_text = normalize_text(f"{field.get('optionLabel', '')} {field.get('label', '')}")
            if self._choice_matches(normalized_answer, option_text):
                try:
                    page.locator(f"[data-job-bot-field='{field['id']}']").check(force=True)
                    return ""
                except Error:
                    return label if any(item.get("required") for item in group) else ""

        return label if any(item.get("required") for item in group) else ""

    def _field_has_value(self, locator, field: Dict[str, object]) -> bool:
        try:
            value = normalize_text(locator.input_value())
        except Error:
            value = ""
        if value and value not in {"select an option"}:
            return True

        if str(field.get("tag", "")).lower() == "select":
            try:
                selected = normalize_text(
                    locator.locator("option:checked").first.inner_text()
                )
            except Error:
                selected = ""
            return bool(selected and selected not in {"select an option"})

        return False

    def _choice_group_has_selection(self, page: Page, group: List[Dict[str, object]]) -> bool:
        for field in group:
            try:
                if page.locator(f"[data-job-bot-field='{field['id']}']").is_checked():
                    return True
            except Error:
                continue
        return False

    def _resolve_answer(self, field: Dict[str, object]) -> Optional[str]:
        label = normalize_text(self._best_label(field))
        field_type = str(field.get("type", "")).lower()
        field_name = normalize_text(str(field.get("name", "")))
        placeholder = normalize_text(str(field.get("placeholder", "")))
        indeed_hint = f"{field_name} {placeholder}".strip()

        # Indeed forms occasionally expose noisy labels that repeat nearby field
        # labels; prefer explicit input name/placeholder hints when available.
        if self.current_provider == "indeed" and indeed_hint:
            if any(token in indeed_hint for token in ("first name", "firstname", "given name", "givenname", "fname")) and "last" not in indeed_hint:
                return self.first_name
            if any(token in indeed_hint for token in ("last name", "lastname", "surname", "family name", "lname")):
                return self.last_name
            if "email" in indeed_hint:
                return self.profile.get("email", "")
            if "phone" in indeed_hint or "mobile" in indeed_hint:
                return self.profile.get("phone", "")
            if "zip" in indeed_hint or "postal" in indeed_hint:
                return self._profile_value("zip_code")
            if "city" in indeed_hint:
                return self._profile_value("city") or self._profile_value("location")
            if "state" in indeed_hint or "province" in indeed_hint or "region" in indeed_hint:
                return self._profile_value("state")

        if "resume" in label and field_type in {"radio", "checkbox"}:
            return Path(self.settings.resume_path).name
        if field_type == "file" or any(token in label for token in ("resume", "cv")):
            return str(self.settings.resume_path)
        if "first name" in label:
            return self.first_name
        if "last name" in label or "surname" in label:
            return self.last_name
        if any(token in label for token in ("full name", "legal name")) or label == "name":
            return self.profile.get("full_name", "")
        if "email" in label:
            return self.profile.get("email", "")
        if "phone country code" in label:
            return "United States (+1)"
        if "phone" in label or "mobile" in label:
            return self.profile.get("phone", "")
        if "linkedin" in label:
            return self.profile.get("linkedin_url", "")
        if "github" in label:
            return self.profile.get("github_url", "")
        if any(token in label for token in ("portfolio", "personal site", "website")):
            return self.profile.get("portfolio_url", "")
        if any(token in label for token in ("current company", "company name", "employer")):
            return self.profile.get("current_company", "")
        if any(token in label for token in ("current role", "job title", "headline", "title")):
            return self.profile.get("current_role") or self.profile.get("title", "")
        if "where are you located" in label:
            city = self._profile_value("city")
            state = self._profile_value("state")
            if city and state:
                return f"{city}, {state}"
            return self._profile_value("location")
        if "city" in label:
            return self._profile_value("city") or self._profile_value("location")
        if "state" in label or "province" in label or "region" in label:
            return self._profile_value("state")
        if "zip" in label or "postal code" in label or "postcode" in label:
            return self._profile_value("zip_code")
        if "location" in label:
            return self._profile_value("location")
        if "country" in label and "citizen" not in label and "work authorization" not in label:
            return self._profile_value("country")
        if "how did you hear" in label:
            default_source = "LinkedIn" if self.current_provider == "linkedin" else "Indeed"
            return self._lookup_custom_answer(label) or default_source
        if any(token in label for token in ("cover letter", "why are you interested", "tell us about yourself", "summary")):
            return self._lookup_custom_answer(label) or self.profile.get("short_pitch") or self.profile.get("summary", "")
        if any(token in label for token in ("us citizen", "u s citizen", "citizenship", "citizen status")):
            return self._profile_value("us_citizen")
        if "authorized to work" in label or "legally authorized" in label or "work authorization" in label:
            if field.get("tag") == "select" and self._profile_value("work_authorization") == "Yes":
                if self._profile_value("requires_visa_sponsorship") == "No":
                    return "I am authorized to work in this country for any employer"
            return self._profile_value("work_authorization")
        if "type of sponsorship" in label or "what type of sponsorship" in label:
            return self._profile_value("sponsorship_type")
        if "visa status" in label or "explain your visa status" in label:
            return self._profile_value("visa_status")
        if "sponsorship" in label:
            return self._profile_value("requires_visa_sponsorship")
        if "veteran" in label:
            return self._profile_value("veteran_status")
        if "disability" in label:
            if field.get("tag") == "select":
                return "No-Disability"
            return self._profile_value("disability_status")
        if any(token in label for token in ("ethnicity", "race")):
            if field.get("tag") == "select":
                return "Asian (Not Hispanic or Latino)"
            return self._profile_value("ethnicity")
        if "gender" in label:
            if field.get("tag") == "select":
                return "Male"
            return self._profile_value("gender")
        if "sexual orientation" in label:
            return self._profile_value("sexual_orientation")
        if any(token in label for token in ("enrolled", "graduate of a correlation one program", "member of our expert network")):
            return self._lookup_custom_answer(label) or "No"
        if "confirm you understand" in label and "contract position" in label:
            return self._lookup_custom_answer(label) or "Yes"
        if "salary expectation" in label or "salary expectations" in label or "monthly salary" in label:
            custom = self._lookup_custom_answer(label)
            return custom or None
        if (
            "privacy policy" in label
            or "data processing" in label
            or ("agree" in label and any(token in label for token in ("policy", "terms", "consent")))
        ):
            return "Yes"
        degree_completion_answer = self._resolve_degree_completion_answer(label)
        if degree_completion_answer is not None:
            return degree_completion_answer
        if label == "education education" or label.startswith("education "):
            return self._highest_education_option()
        education_answer = self._resolve_education_answer(label)
        if education_answer is not None:
            return education_answer

        custom = self._lookup_custom_answer(label)
        if custom:
            return custom

        if field.get("tag") == "select":
            return custom

        return None

    def _smart_fallback_answer(self, field: Dict[str, object]) -> Optional[str]:
        """Best-effort answer derived from the candidate profile when no explicit answer is known."""
        label = normalize_text(self._best_label(field))
        skills_lower = [s.lower() for s in (self.profile.get("skills") or [])]
        experience_years = int(self.profile.get("experience_years") or 0)

        # Years of experience questions
        if any(token in label for token in ("years of experience", "how many years", "years experience")):
            return str(experience_years)

        # Numeric questions generally about experience
        field_type = str(field.get("type", "")).lower()
        if field_type == "number" and "experience" in label:
            return str(experience_years)

        # Start date / availability
        if any(token in label for token in ("start date", "available to start", "earliest start", "when can you start")):
            return "2 weeks"
        if "availability" in label and "interview" not in label:
            return "Immediately"

        # Willing/open to questions
        if any(token in label for token in ("willing to", "open to", "comfortable with", "able to")):
            return "Yes"

        # Yes/No questions about specific skills the candidate has
        for skill in skills_lower:
            if skill in label:
                return "Yes"

        # Generic yes/no skill/experience questions
        tech_keywords = {
            "python", "machine learning", "deep learning", "nlp", "generative ai", "rag",
            "langchain", "langgraph", "azure", "aws", "docker", "kubernetes", "mlflow",
            "airflow", "pytorch", "scikit", "flask", "sql", "llm", "ai",
        }
        if any(kw in label for kw in tech_keywords):
            return "Yes"

        # Last-resort fallback for select/radio: ask Claude Haiku to pick the
        # right option rather than blindly choosing the first one.
        field_type = str(field.get("type", "")).lower()
        options = field.get("options") or []
        if field_type in {"select", "radio"} or (field.get("tag") == "select"):
            non_placeholder = [o for o in options if o and "select" not in o.lower() and o.strip()]
            if non_placeholder:
                try:
                    from .ai_assistant import select_answer as _ai_select
                    ai_ans = _ai_select(self._best_label(field), non_placeholder)
                    if ai_ans:
                        return ai_ans
                except Exception:
                    pass
                return non_placeholder[0]

        return None

    def _lookup_custom_answer(self, normalized_label: str) -> str:
        if normalized_label in self.exact_answers:
            return self.exact_answers[normalized_label]
        for key, value in self.contains_answers.items():
            if key and key in normalized_label:
                return value
        return ""

    def _best_label(self, field: Dict[str, object]) -> str:
        label = (field.get("label") or "").strip()
        if label:
            return label
        return str(field.get("name", "")).strip() or "unlabeled field"

    def _is_meaningful_application_field(self, field: Dict[str, object]) -> bool:
        label = normalize_text(self._best_label(field))
        field_type = str(field.get("type", "")).lower()
        if field_type == "hidden":
            return False
        if field_type == "file":
            return True
        keywords = (
            "name",
            "email",
            "phone",
            "resume",
            "cv",
            "cover letter",
            "linkedin",
            "github",
            "portfolio",
            "location",
            "city",
            "school",
            "degree",
            "gpa",
            "visa",
            "sponsorship",
        )
        return any(keyword in label for keyword in keywords)

    def _profile_value(self, key: str) -> Optional[str]:
        value = self.profile.get(key, "")
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return str(value)

    def _resolve_education_answer(self, label: str) -> Optional[str]:
        education = self.profile.get("education", []) or []
        if not education:
            return None

        target = self._pick_education_entry(label, education)
        if not target:
            return None

        if any(token in label for token in ("university", "college", "school", "institution")):
            return target.get("school") or None
        if any(token in label for token in ("field of study", "major", "specialization")):
            return target.get("field_of_study") or None
        if any(token in label for token in ("degree", "education level")):
            return target.get("degree") or None
        if "gpa" in label:
            return target.get("gpa") or None
        if any(token in label for token in ("graduation", "graduated", "end date", "to date", "completion")):
            return target.get("to") or None
        if any(token in label for token in ("start date", "from date", "attended from")):
            return target.get("from") or None

        return None

    def _resolve_degree_completion_answer(self, label: str) -> Optional[str]:
        normalized = normalize_text(label)
        if "have you completed" not in normalized:
            return None

        education = self.profile.get("education", []) or []
        degrees = [normalize_text(item.get("degree", "")) for item in education]
        if not degrees:
            return None

        checks = {
            "master": any("master" in degree or "phd" in degree for degree in degrees),
            "bachelor": any(
                any(token in degree for token in ("bachelor", "master", "phd"))
                for degree in degrees
            ),
            "associate": any(
                any(token in degree for token in ("associate", "bachelor", "master", "phd"))
                for degree in degrees
            ),
            "high school": True,
        }

        for token, completed in checks.items():
            if token in normalized:
                return "Yes" if completed else "No"
        return None

    def _pick_education_entry(self, label: str, education: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        normalized = normalize_text(label)
        if any(token in normalized for token in ("master", "graduate", "postgraduate")):
            for item in education:
                degree = normalize_text(item.get("degree", ""))
                if "master" in degree:
                    return item
        if any(token in normalized for token in ("bachelor", "undergraduate")):
            for item in education:
                degree = normalize_text(item.get("degree", ""))
                if "bachelor" in degree:
                    return item
        return education[0]

    def _highest_education_option(self) -> Optional[str]:
        education = self.profile.get("education", []) or []
        for item in education:
            degree = normalize_text(item.get("degree", ""))
            if "master" in degree:
                return "Masters/MBA"
        for item in education:
            degree = normalize_text(item.get("degree", ""))
            if "bachelor" in degree:
                return "Bachelor of Art/Science"
        return None

    def _normalized_fill_value(self, field: Dict[str, object], answer: str) -> str:
        field_type = str(field.get("type", "")).lower()
        if field_type == "number":
            digits = "".join(ch for ch in answer if ch.isdigit())
            return digits
        return answer

    def _fill_combobox_field(self, page: Page, locator, field: Dict[str, object], answer: str) -> bool:
        candidates = self._combobox_candidates(field, answer)
        if not candidates:
            return False

        try:
            locator.click(force=True)
        except Error:
            return False

        try:
            locator.fill("")
        except Error:
            pass

        for candidate_answer in candidates:
            try:
                locator.fill("")
            except Error:
                pass

            try:
                locator.type(candidate_answer, delay=30)
            except Error:
                try:
                    locator.fill(candidate_answer)
                except Error:
                    continue

            time.sleep(0.4)
            if self._select_combobox_option(page, locator, field, candidate_answer):
                return True

            for key in ("ArrowDown", "Enter"):
                try:
                    locator.press(key)
                    time.sleep(0.2)
                except Error:
                    break
            if self._combobox_selected(locator, normalize_text(candidate_answer)):
                return True

        try:
            locator.press("Escape")
        except Error:
            pass
        return False

    def _combobox_candidates(self, field: Dict[str, object], answer: str) -> List[str]:
        candidates: List[str] = []

        def append(value: Optional[str]) -> None:
            if not value:
                return
            normalized = normalize_text(value)
            if not normalized:
                return
            if any(normalize_text(item) == normalized for item in candidates):
                return
            candidates.append(value)

        label = normalize_text(self._best_label(field))
        append(answer)

        if "where are you located" in label or "location" in label:
            city = self._profile_value("city")
            state = self._profile_value("state")
            country = self._profile_value("country")
            if city and state:
                append(f"{city}, {state}")
            append(city)
            append(state)
            append(country)

        if "how did you hear" in label:
            append("Indeed")
            append("Other")

        normalized_answer = normalize_text(answer)
        if normalized_answer == "yes":
            append("Yes")
        if normalized_answer == "no":
            append("No")

        return candidates

    def _select_combobox_option(self, page: Page, locator, field: Dict[str, object], answer: str) -> bool:
        normalized_answer = normalize_text(answer)
        control_id = str(field.get("nativeId", "")).strip()
        try:
            aria_controls = (locator.get_attribute("aria-controls") or "").strip()
        except Error:
            aria_controls = ""

        prefixes: List[str] = []
        if aria_controls.endswith("-listbox"):
            prefixes.append(aria_controls[: -len("-listbox")])
        if control_id:
            prefixes.append(f"react-select-{control_id}")

        selectors: List[str] = []
        for prefix in prefixes:
            selectors.append(f"[id^='{prefix}-option-']")
        selectors.extend(["[role='option']", ".select__option"])

        for selector in selectors:
            option_locator = page.locator(selector)
            try:
                count = min(option_locator.count(), 20)
            except Error:
                continue
            for index in range(count):
                candidate = option_locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    option_text = normalize_text(candidate.inner_text())
                except Error:
                    continue
                if not self._choice_matches(normalized_answer, option_text):
                    continue
                try:
                    candidate.click(force=True)
                    time.sleep(0.3)
                    return True
                except Error:
                    continue
        return False

    def _combobox_selected(self, locator, normalized_answer: str) -> bool:
        try:
            invalid = locator.get_attribute("aria-invalid")
            if invalid == "true":
                return False
        except Error:
            pass

        try:
            value = normalize_text(locator.input_value())
            if self._choice_matches(normalized_answer, value):
                return True
        except Error:
            pass

        try:
            invalid = locator.get_attribute("aria-invalid")
            if invalid == "false":
                return True
        except Error:
            pass
        return False

    def _select_option(self, locator, field: Dict[str, object], answer: str) -> bool:
        normalized_answer = normalize_text(answer)
        options = field.get("options", []) or []

        def _try_select(option: Dict[str, object]) -> bool:
            try:
                locator.select_option(value=option.get("value", ""))
                return True
            except Error:
                try:
                    locator.select_option(label=option.get("text", ""))
                    return True
                except Error:
                    return False

        # Pass 1: exact match on text or value
        for option in options:
            option_text = normalize_text(option.get("text", ""))
            option_value = normalize_text(option.get("value", ""))
            if normalized_answer == option_text or normalized_answer == option_value:
                if _try_select(option):
                    return True

        # Pass 2: option text/value starts with answer (e.g. answer="Yes" → "Yes - I am authorized")
        for option in options:
            option_text = normalize_text(option.get("text", ""))
            option_value = normalize_text(option.get("value", ""))
            if option_text.startswith(normalized_answer) or option_value.startswith(normalized_answer):
                if _try_select(option):
                    return True

        # Pass 3: answer contained in option
        for option in options:
            option_text = normalize_text(option.get("text", ""))
            option_value = normalize_text(option.get("value", ""))
            if normalized_answer and (normalized_answer in option_text or normalized_answer in option_value):
                if _try_select(option):
                    return True

        # Pass 4: option contained in answer
        for option in options:
            option_text = normalize_text(option.get("text", ""))
            option_value = normalize_text(option.get("value", ""))
            if option_text and option_text in normalized_answer:
                if _try_select(option):
                    return True
            if option_value and option_value in normalized_answer:
                if _try_select(option):
                    return True

        # Pass 5: full _choice_matches (covers yes/no synonyms, etc.)
        for option in options:
            option_text = normalize_text(option.get("text", ""))
            option_value = normalize_text(option.get("value", ""))
            if self._choice_matches(normalized_answer, option_text) or self._choice_matches(normalized_answer, option_value):
                if _try_select(option):
                    return True

        return False

    def _choice_matches(self, normalized_answer: str, option_text: str) -> bool:
        if not normalized_answer or not option_text:
            return False
        if normalized_answer == option_text:
            return True
        if normalized_answer in option_text or option_text in normalized_answer:
            return True
        yes_values = {"yes", "y", "authorized", "true"}
        no_values = {"no", "n", "false", "not authorized", "decline", "prefer not to say"}
        if normalized_answer in yes_values and option_text.startswith("yes"):
            return True
        if normalized_answer in no_values and (option_text.startswith("no") or "decline" in option_text or "prefer not" in option_text):
            return True
        synonym_map = {
            "no i am not a veteran": {"not a protected veteran", "i am not a protected veteran", "no", "not veteran"},
            "heterosexual straight": {"straight", "heterosexual"},
            "male": {"man"},
            "asian": {"asian indian", "asian non hispanic", "asian alone"},
        }
        for canonical, aliases in synonym_map.items():
            if normalized_answer == canonical and any(alias in option_text for alias in aliases):
                return True
        return False

    def _has_any_selector(self, page: Page, selectors: List[str]) -> bool:
        attempts = 4 if self.current_provider == "linkedin" else 1
        max_candidates = 12 if self.current_provider == "indeed" else 4
        for attempt in range(attempts):
            for selector in selectors:
                locator = page.locator(selector)
                try:
                    count = min(locator.count(), max_candidates)
                except Error:
                    continue
                for index in range(count):
                    try:
                        candidate = locator.nth(index)
                        try:
                            candidate.scroll_into_view_if_needed(timeout=500)
                        except Error:
                            pass
                        if candidate.is_visible():
                            return True
                    except Error:
                        continue
            if self.current_provider == "linkedin":
                self._scroll_linkedin_control_into_view(page, selectors, attempt)
        return False

    def _click_visible_control(self, page: Page, selectors: List[str]) -> bool:
        attempts = 4 if self.current_provider == "linkedin" else 1
        for attempt in range(attempts):
            if self._try_click_visible_control(page, selectors):
                return True
            if self.current_provider == "linkedin":
                self._scroll_linkedin_control_into_view(page, selectors, attempt)
        return False

    def _try_click_visible_control(self, page: Page, selectors: List[str]) -> bool:
        max_candidates = 12 if self.current_provider == "indeed" else 4
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), max_candidates)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    if hasattr(candidate, "is_enabled") and not candidate.is_enabled():
                        continue
                    try:
                        candidate.scroll_into_view_if_needed(timeout=1000)
                    except Error:
                        pass
                    if self._click_locator(page, candidate):
                        self._wait_for_idle(page)
                        return True
                except Error:
                    continue
        return False

    def _click_locator(self, page: Page, locator) -> bool:
        if self.current_provider == "linkedin":
            try:
                box = locator.bounding_box()
            except Error:
                box = None
            if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                try:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2
                    page.mouse.move(x, y)
                    page.wait_for_timeout(150)
                    page.mouse.click(x, y)
                    return True
                except Error:
                    pass

        for force in (False, True):
            try:
                locator.click(force=force, timeout=1500)
                return True
            except Error:
                continue

        return False

    def _scroll_linkedin_control_into_view(self, page: Page, selectors: List[str], attempt: int) -> None:
        action_selectors = set(
            self._linkedin_continue_selectors()
            + self._linkedin_review_selectors()
            + self._linkedin_submit_selectors()
            + self._linkedin_done_selectors()
        )
        scroll_to_bottom = any(selector in action_selectors for selector in selectors)
        amount = 420 + (attempt * 180)
        script = """
([amount, scrollToBottom]) => {
  const isVisible = (node) => {
    if (!node) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };

  const dialog = [
    ...document.querySelectorAll("[role='dialog'][aria-modal='true'], .jobs-easy-apply-modal, .artdeco-modal[role='dialog']")
  ].find(isVisible);

  if (dialog) {
    const scrollables = [dialog, ...dialog.querySelectorAll('*')]
      .filter((node) => node.scrollHeight > node.clientHeight + 24);
    const target = scrollables.sort((a, b) => b.clientHeight - a.clientHeight)[0] || dialog;
    target.scrollIntoView({ block: 'center', behavior: 'instant' });
    if (target.scrollTop !== undefined) {
      target.scrollTop = scrollToBottom
        ? target.scrollHeight
        : Math.min(target.scrollHeight, target.scrollTop + amount);
    }
    return true;
  }

  window.scrollBy(0, amount);
  return false;
}
"""
        try:
            page.evaluate(script, [amount, scroll_to_bottom])
            page.wait_for_timeout(350)
        except Error:
            return

    def _body_has_any(self, page: Page, phrases: List[str]) -> bool:
        try:
            body_text = normalize_text(page.locator("body").inner_text())
        except Error:
            return False
        return any(normalize_text(phrase) in body_text for phrase in phrases)

    def _page_text(self, page: Page, selectors: List[str]) -> str:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 4)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    value = candidate.inner_text().strip()
                except Error:
                    continue
                if value:
                    return value
        return ""

    def _indeed_submission_succeeded(self, page: Page) -> bool:
        success_markers = [
            "application submitted",
            "your application has been submitted",
            "you have successfully applied",
            "thank you for applying",
        ]
        return self._body_has_any(page, success_markers) or self._submission_succeeded(page)

    def _linkedin_submission_succeeded(self, page: Page) -> bool:
        return self._linkedin_submission_screen_visible(page) or self._linkedin_job_page_marked_applied(page)

    def _linkedin_submission_screen_visible(self, page: Page) -> bool:
        success_markers = [
            "application submitted",
            "your application was sent",
            "your application has been submitted",
            "application sent",
        ]
        if not self._body_has_any(page, success_markers):
            return False
        return self._has_any_selector(page, self._linkedin_done_selectors()) or self._linkedin_application_dialog_open(page)

    def _linkedin_application_dialog_open(self, page: Page) -> bool:
        dialog_selectors = [
            "[role='dialog'][aria-modal='true']",
            ".jobs-easy-apply-modal",
            ".artdeco-modal[role='dialog']",
        ]
        action_selectors = (
            self._linkedin_continue_selectors()
            + self._linkedin_review_selectors()
            + self._linkedin_submit_selectors()
        )
        if self._has_any_selector(page, action_selectors):
            return True
        return self._has_any_selector(page, dialog_selectors)

    def _linkedin_job_page_marked_applied(self, page: Page) -> bool:
        try:
            main_text = normalize_text(page.locator("main").inner_text(timeout=3000))
        except Error:
            try:
                main_text = normalize_text(page.locator("body").inner_text(timeout=3000))
            except Error:
                return False

        top_text = main_text[:1600]
        concrete_markers = [
            "resume downloaded",
            "application submitted",
            "your application was sent",
            "your application has been submitted",
        ]
        if any(marker in top_text for marker in concrete_markers):
            return True

        if " applied " not in f" {top_text} ":
            return False

        return not self._has_any_selector(page, self._linkedin_apply_selectors())

    def _complete_linkedin_submission(self, page: Page, job: JobRecord, submit_mode: str) -> JobRecord:
        self._capture_progress_screenshot(page, job, "linkedin-application-sent")
        self._click_visible_control(page, self._linkedin_done_selectors())
        deadline = time.time() + 12.0
        while time.time() < deadline:
            self._wait_for_idle(page)
            if self._linkedin_application_dialog_open(page):
                self._click_visible_control(page, self._linkedin_done_selectors())
            if self._linkedin_job_page_marked_applied(page):
                return self._finalize(
                    page,
                    job,
                    "submitted",
                    "LinkedIn application submitted",
                    submit_mode,
                    include_timestamp=True,
                    verified_submission=True,
                )
            time.sleep(0.5)

        return self._finalize(
            page,
            job,
            "review_required",
            "LinkedIn showed a success screen but the job page did not switch to Applied",
            submit_mode,
        )

    def _indeed_apply_selectors(self) -> List[str]:
        return [
            "button#indeedApplyButton",
            "button[data-testid='indeedApplyButton-test']",
            "button[aria-label='Apply with Indeed']",
            "button:has-text('Apply with Indeed')",
            "button:has-text('Apply now')",
            "button:has-text('Easily apply')",
            "button:has-text('Apply')",
            "button:has-text('Continue to application')",
            "a:has-text('Apply now')",
            "a:has-text('Easily apply')",
            "a:has-text('Apply')",
        ]

    def _linkedin_apply_selectors(self) -> List[str]:
        return [
            "button.jobs-apply-button",
            "button:has-text('Easy Apply')",
            "button[aria-label*='Easy Apply']",
        ]

    def _indeed_external_apply_selectors(self) -> List[str]:
        return [
            "button:has-text('Apply on company site')",
            "a:has-text('Apply on company site')",
            "button:has-text('Continue to application')",
            "a:has-text('Continue to application')",
            "button:has-text('Apply externally')",
            "a:has-text('Apply externally')",
        ]

    def _indeed_continue_selectors(self) -> List[str]:
        return [
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('Save and continue')",
            "button:has-text('Continue to next step')",
        ]

    def _linkedin_continue_selectors(self) -> List[str]:
        return [
            "button[aria-label='Continue to next step']",
            "button:has-text('Continue to next step')",
            "button:has-text('Next')",
            "button:has-text('Continue')",
        ]

    def _indeed_review_selectors(self) -> List[str]:
        return [
            "button:has-text('Review your application')",
            "button:has-text('Review application')",
        ]

    def _linkedin_review_selectors(self) -> List[str]:
        return [
            "button[aria-label='Review your application']",
            "button:has-text('Review your application')",
            "button:has-text('Review')",
        ]

    def _indeed_submit_selectors(self) -> List[str]:
        return [
            "button:has-text('Submit application')",
            "button:has-text('Submit your application')",
            "button:has-text('Submit')",
        ]

    def _linkedin_submit_selectors(self) -> List[str]:
        return [
            "button[aria-label='Submit application']",
            "button:has-text('Submit application')",
            "button:has-text('Send application')",
            "button:has-text('Submit')",
        ]

    def _linkedin_done_selectors(self) -> List[str]:
        return [
            "button[aria-label='Done']",
            "button:has-text('Done')",
            "button[aria-label*='Dismiss']",
            "button:has-text('Close')",
        ]

    def _select_indeed_listing(self, page: Page, job: JobRecord) -> None:
        jk = self._indeed_job_key(job)
        if not jk:
            return

        selectors = [
            f"[data-jk='{jk}'] a[href*='/viewjob?jk=']",
            f"[data-jk='{jk}'] a[aria-label^='full details of']",
            f"[data-jk='{jk}'] h2 a",
            f"a[data-jk='{jk}']",
            f"a[href*='jk={jk}']",
            f"[data-jk='{jk}']",
        ]
        self._click_visible_control(page, selectors)
        try:
            page.wait_for_timeout(1500)
        except Error:
            return

    def _indeed_job_key(self, job: JobRecord) -> str:
        for raw_url in (job.apply_url, job.source_url):
            parsed = urlparse(raw_url or "")
            query = parse_qs(parsed.query)
            for key in ("jk", "vjk"):
                value = (query.get(key) or [""])[0].strip()
                if value:
                    return value
        return ""

    def _ensure_indeed_detail_page(self, page: Page, job: JobRecord) -> None:
        jk = self._indeed_job_key(job)
        if not jk:
            return

        try:
            current_url = page.url
        except Error:
            current_url = ""

        current_lower = current_url.lower()
        if "/viewjob" in current_lower and f"jk={jk.lower()}" in current_lower:
            return

        if self._has_any_selector(page, self._indeed_apply_selectors() + self._indeed_external_apply_selectors()):
            return

        target_url = f"https://www.indeed.com/viewjob?jk={jk}"
        try:
            page.goto(target_url, wait_until="domcontentloaded")
            self._wait_for_idle(page)
        except Error:
            return

    def _indeed_job_closed(self, page: Page) -> bool:
        markers = [
            "this job has expired",
            "job has expired",
            "is not accepting applications",
            "no longer accepting applications",
            "no longer available",
        ]
        return self._body_has_any(page, markers)

    def _open_indeed_external_apply(self, page: Page):
        before_pages = list(page.context.pages)
        if not self._click_visible_control(page, self._indeed_external_apply_selectors()):
            return None

        try:
            page.wait_for_timeout(2000)
        except Error:
            pass

        after_pages = list(page.context.pages)
        new_pages = [candidate for candidate in after_pages if candidate not in before_pages]
        target_page = new_pages[-1] if new_pages else page
        try:
            self._wait_for_idle(target_page)
        except Error:
            return target_page
        return target_page

    def _submit_form(self, page: Page) -> bool:
        selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Apply')",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() > 0:
                    locator.first.click()
                    self._wait_for_idle(page)
                    return True
            except Error:
                continue
        return False

    def _submission_succeeded(self, page: Page) -> bool:
        try:
            body_text = normalize_text(page.locator("body").inner_text())
        except Error:
            body_text = ""
        success_markers = [
            "thank you",
            "application submitted",
            "we have received your application",
            "your application has been submitted",
            "application has been received",
            "thank you for applying",
        ]
        if any(marker in body_text for marker in success_markers):
            return True
        try:
            current_url = normalize_text(page.url)
        except Error:
            current_url = ""
        return "confirmation" in current_url

    def _wait_for_submission_confirmation(self, page: Page) -> bool:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            self._wait_for_idle(page)
            if self._submission_succeeded(page):
                return True
            time.sleep(0.5)
        return self._submission_succeeded(page)

    def _finalize(
        self,
        page: Page,
        job: JobRecord,
        status: str,
        reason: str,
        submit_mode: str,
        include_timestamp: bool = False,
        verified_submission: bool = False,
    ) -> JobRecord:
        job.status = status
        job.reason = reason
        job.screenshot_path = self._capture_progress_screenshot(page, job, status)
        job.submission_verified = status == "submitted" and verified_submission
        if include_timestamp or status == "submitted":
            job.submitted_at = utc_now().replace(microsecond=0).isoformat()
        elif submit_mode != "auto" and not job.submitted_at:
            job.submitted_at = ""
        return job

    def _capture_progress_screenshot(self, page: Page, job: JobRecord, label: str) -> str:
        next_index = self._artifact_counters.get(job.job_id, 0) + 1
        self._artifact_counters[job.job_id] = next_index
        screenshot_name = sanitize_filename(f"{job.company}-{job.title}-{next_index:02d}-{label}") + ".png"
        screenshot_path = self.settings.output_dir / screenshot_name
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Error:
            return ""
        return str(screenshot_path)

    def _split_name(self, full_name: str) -> Tuple[str, str]:
        parts = [part for part in full_name.split() if part]
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], " ".join(parts[1:])
