from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..chrome_mcp_client import BridgeElement, BridgePageSnapshot, BridgeTab, ChromeMcpClient
from ..utils import normalize_text


SUCCESS_MARKERS = (
    "application sent",
    "your application was sent",
    "your application has been submitted",
    "application submitted",
    "application submitted now",
    "submitted resume",
)

REVIEW_MARKERS = (
    "review your application",
    "application review",
)

REVIEW_QUERIES = (
    "Review your application",
    "Review",
)

CONTINUE_QUERIES = (
    "Continue to next step",
    "Next",
    "Continue",
)

SUBMIT_QUERIES = (
    "Submit application",
    "Send application",
    "Submit",
)

DONE_QUERIES = (
    "Done",
    "Close",
    "Dismiss",
)


@dataclass
class LinkedInFieldFillResult:
    matched: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    updated_controls: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def filled_count(self) -> int:
        return len(self.updated_controls)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": list(self.matched),
            "skipped": list(self.skipped),
            "updated_controls": list(self.updated_controls),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class LinkedInActionResult:
    status: str
    submitted: bool
    tab: BridgeTab
    steps: int = 0
    message: str = ""
    actions: List[str] = field(default_factory=list)
    filled_fields: List[str] = field(default_factory=list)
    matched_controls: List[str] = field(default_factory=list)
    timing: Dict[str, Any] = field(default_factory=dict)
    final_state: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "submitted": self.submitted,
            "tab": self.tab.to_dict(),
            "steps": self.steps,
            "message": self.message,
            "actions": list(self.actions),
            "filled_fields": list(self.filled_fields),
            "matched_controls": list(self.matched_controls),
            "timing": dict(self.timing),
            "final_state": dict(self.final_state),
        }


class LinkedInBridgeDriver:
    def __init__(self, client: ChromeMcpClient) -> None:
        self.client = client
        self._snapshots: Dict[str, BridgePageSnapshot] = {}
        self._events: List[Dict[str, Any]] = []
        self._created_at = time.perf_counter()

    def locate_active_jobs_tab(
        self,
        *,
        url_contains: str = "linkedin.com/jobs",
        title_contains: str = "",
    ) -> Optional[BridgeTab]:
        tabs = self.client.list_tabs()
        # 1. Prefer active tab with /jobs/ URL
        found = self.client.find_tab(url_contains=url_contains, title_contains=title_contains, active=True, tabs=tabs)
        if found:
            return found
        # 2. Any tab with /jobs/ URL
        found = self.client.find_tab(url_contains=url_contains, title_contains=title_contains, tabs=tabs)
        if found:
            return found
        # 3. Any active LinkedIn tab (e.g. campaign page after apply)
        found = self.client.find_tab(url_contains="linkedin.com", active=True, tabs=tabs)
        if found:
            return found
        # 4. Any LinkedIn tab at all
        found = self.client.find_tab(url_contains="linkedin.com", tabs=tabs)
        if found:
            return found
        # 5. Return first available tab (we'll navigate it to the job URL)
        return tabs[0] if tabs else None

    def read_current_state(
        self,
        tab_id: Any,
        *,
        filter_mode: str = "interactive",
        limit: int = 120,
        scope: str = "",
    ) -> BridgePageSnapshot:
        started = time.perf_counter()
        effective_scope = scope or ("dialog-interactive" if filter_mode == "interactive" else "page")
        snapshot = self.client.read_page(tab_id, filter_mode=filter_mode, limit=limit, scope=effective_scope)
        self._snapshots[self._snapshot_key(tab_id, filter_mode, effective_scope)] = snapshot
        self._events.append(
            {
                "event": "read_current_state",
                "tab_id": str(tab_id),
                "seconds": round(time.perf_counter() - started, 3),
                "interactive": len(snapshot.interactive_elements),
                "scope": effective_scope,
            }
        )
        return snapshot

    def current_snapshot(
        self,
        tab_id: Any,
        *,
        refresh: bool = False,
        filter_mode: str = "interactive",
        scope: str = "",
        limit: int = 120,
    ) -> BridgePageSnapshot:
        effective_scope = scope or ("dialog-interactive" if filter_mode == "interactive" else "page")
        key = self._snapshot_key(tab_id, filter_mode, effective_scope)
        if refresh or key not in self._snapshots:
            return self.read_current_state(tab_id, filter_mode=filter_mode, scope=effective_scope, limit=limit)
        return self._snapshots[key]

    def find_control(
        self,
        tab_id: Any,
        query: str,
        *,
        exact: bool = True,
        refresh: bool = False,
        limit: int = 10,
        scope: str = "dialog",
    ) -> Optional[BridgeElement]:
        snapshot = self.current_snapshot(tab_id, refresh=refresh, scope=scope)
        matches = snapshot.find_controls(query, exact=exact, limit=limit)
        if matches:
            return matches[0]
        fallback = self.client.find_elements(tab_id, query, limit=limit, scope=scope, exact=exact)
        for element in fallback:
            if exact and element.matches_exact(query):
                return element
            if not exact and element.matches_contains(query):
                return element
        return None

    def find_controls(
        self,
        tab_id: Any,
        query: str,
        *,
        exact: bool = True,
        refresh: bool = False,
        limit: int = 10,
        scope: str = "dialog",
    ) -> List[BridgeElement]:
        snapshot = self.current_snapshot(tab_id, refresh=refresh, scope=scope)
        matches = snapshot.find_controls(query, exact=exact, limit=limit)
        if matches:
            return matches
        fallback = self.client.find_elements(tab_id, query, limit=limit, scope=scope, exact=exact)
        if exact:
            return [element for element in fallback if element.matches_exact(query)]
        return [element for element in fallback if element.matches_contains(query)]

    def click_control(self, tab_id: Any, control: Any) -> Dict[str, Any]:
        action: Dict[str, Any] = {"kind": "click"}
        target_id = ""
        if hasattr(control, "id"):
            target_id = control.id
            action["targetId"] = target_id
        else:
            query = str(control)
            action["query"] = query
            action["exact"] = True
            action["scope"] = "dialog"
        started = time.perf_counter()
        result = self.client.perform_action(tab_id, action)
        self._invalidate(tab_id)
        self._events.append(
            {
                "event": "click_control",
                "tab_id": str(tab_id),
                "target_id": target_id or action.get("query", ""),
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
        return result

    def set_value(self, tab_id: Any, control: Any, value: str) -> Dict[str, Any]:
        action: Dict[str, Any] = {"kind": "setValue", "value": value}
        target_id = ""
        if hasattr(control, "id"):
            target_id = control.id
            action["targetId"] = target_id
        else:
            query = str(control)
            action["query"] = query
            action["exact"] = True
            action["scope"] = "dialog"
        started = time.perf_counter()
        result = self.client.perform_action(tab_id, action)
        self._invalidate(tab_id)
        self._events.append(
            {
                "event": "set_value",
                "tab_id": str(tab_id),
                "target_id": target_id or action.get("query", ""),
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
        return result

    def scroll(self, tab_id: Any, *, target: Any = None, delta_y: Optional[int] = None, delta_x: Optional[int] = None) -> Dict[str, Any]:
        target_id = ""
        if target is not None:
            target_id = target.id if hasattr(target, "id") else str(target)
        started = time.perf_counter()
        payload: Dict[str, Any] = {"kind": "scroll"}
        if target_id:
            payload["targetId"] = target_id
        if delta_y is not None:
            payload["deltaY"] = delta_y
        if delta_x is not None:
            payload["deltaX"] = delta_x
        result = self.client.perform_action(tab_id, payload)
        self._invalidate(tab_id)
        self._events.append(
            {
                "event": "scroll",
                "tab_id": str(tab_id),
                "target_id": target_id,
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
        return result

    def go_to_url(self, tab_id: Any, url: str, *, wait_for_url_contains: str = "", timeout_seconds: float = 8.0) -> BridgePageSnapshot:
        self.client.navigate(tab_id, url)
        self._invalidate(tab_id)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            # Re-discover the tab by URL in case tab_id changed after cross-origin navigation
            effective_tab_id = self._resolve_tab_id(tab_id, url_hint=wait_for_url_contains or url)
            snapshot = self.read_current_state(effective_tab_id, filter_mode="all", scope="page", limit=120)
            if not wait_for_url_contains or wait_for_url_contains in snapshot.url:
                return snapshot
            time.sleep(0.4)
        effective_tab_id = self._resolve_tab_id(tab_id, url_hint=wait_for_url_contains or url)
        return self.read_current_state(effective_tab_id, filter_mode="all", scope="page", limit=120)

    def _resolve_tab_id(self, tab_id: Any, url_hint: str = "") -> Any:
        """Return tab_id if still valid, otherwise find a matching tab by URL hint."""
        tabs = self.client.list_tabs()
        # Check if original tab_id is still valid
        for tab in tabs:
            if tab.id == str(tab_id):
                return tab_id
        # Tab ID changed (cross-origin navigation) — find by URL hint
        if url_hint:
            for tab in tabs:
                if url_hint in (tab.url or ""):
                    return tab.id
            # Partial hint match (e.g. "linkedin.com" in URL)
            for tab in tabs:
                if "linkedin.com" in (tab.url or ""):
                    return tab.id
        # Fallback: return first tab
        if tabs:
            return tabs[0].id
        return tab_id

    def _has_dismiss_button(self, snapshot: BridgePageSnapshot) -> bool:
        """Return True if a Dismiss button is visible — a reliable indicator that the
        Easy Apply dialog shell is open (Dismiss is present in every dialog state)."""
        return bool(snapshot.find_controls("Dismiss", exact=True) or snapshot.find_controls("Dismiss"))

    def _dialog_ready(self, snapshot: BridgePageSnapshot) -> bool:
        """Return True if the Easy Apply dialog is open with form content loaded.

        Checks (in order):
        1. Review / Submit / Success markers in page text
        2. activeDialog reported by the extension (may be None — known gap)
        3. Dismiss button present (dialog shell open) AND at least one form field loaded
        """
        if self.detect_review_step(snapshot) or self.detect_submit_step(snapshot) or self.detect_success_state(snapshot):
            return True
        if snapshot.raw.get("activeDialog"):
            return True
        # Extension may not set activeDialog; use Dismiss button + a form input as proxy
        if not self._has_dismiss_button(snapshot):
            return False
        return any(
            e.tag_name.lower() in {"input", "select", "textarea"}
            or (e.role or "").lower() in {"combobox", "listbox", "radio", "checkbox", "spinbutton"}
            for e in snapshot.interactive_elements
        )

    def open_easy_apply(self, tab_id: Any, *, timeout_seconds: float = 12.0) -> BridgePageSnapshot:
        snapshot = self.read_current_state(tab_id, filter_mode="all", scope="page", limit=120)
        for query in ("Easy Apply to this job", "Easy Apply"):
            control = self.find_control(tab_id, query, exact=query == "Easy Apply to this job", refresh=True, scope="page")
            if not control:
                continue
            if control.href and "/apply/" in control.href:
                snapshot = self.go_to_url(tab_id, control.href, wait_for_url_contains="/apply/", timeout_seconds=timeout_seconds)
            else:
                self.click_control(tab_id, control)
            snapshot = self._wait_for_dialog_content(tab_id, timeout_seconds=timeout_seconds)
            if self._dialog_ready(snapshot):
                return snapshot
            fallback_apply_url = self._fallback_apply_url(control.href or snapshot.url)
            if fallback_apply_url:
                snapshot = self.go_to_url(tab_id, fallback_apply_url, wait_for_url_contains="/apply/", timeout_seconds=timeout_seconds)
                snapshot = self._wait_for_dialog_content(tab_id, timeout_seconds=timeout_seconds)
                if self._dialog_ready(snapshot):
                    return snapshot
        return snapshot

    def _wait_for_dialog_content(self, tab_id: Any, *, timeout_seconds: float = 12.0) -> BridgePageSnapshot:
        """Wait until the Easy Apply dialog is open and stable.

        LinkedIn renders the dialog shell immediately (Dismiss + Next, interactiveCount=2)
        then loads form body asynchronously. We need to wait for the form to load, but
        some first steps genuinely have no extra fields (just a Next to confirm).

        Strategy:
        - If interactiveCount > 2 or a form input is present → form loaded, return.
        - If interactiveCount == 2 has been STABLE for ≥1 s → first step with no fields, return.
        - If interactiveCount == 0 → spinner still loading, keep waiting.
        """
        MIN_STABLE_SECS = 1.0  # stable-2 threshold before declaring "no-field first step"
        deadline = time.time() + timeout_seconds
        last_count = -1
        stable_since: float = 0.0
        snapshot = self.read_current_state(tab_id, filter_mode="interactive", scope="dialog-interactive", limit=120)
        while time.time() < deadline:
            if self.detect_review_step(snapshot) or self.detect_submit_step(snapshot) or self.detect_success_state(snapshot):
                return snapshot
            active = snapshot.raw.get("activeDialog")
            interactive_count = 0
            if isinstance(active, dict):
                interactive_count = active.get("interactiveCount", 0)
            # Also check form inputs directly (extension may not set activeDialog)
            has_dismiss = self._has_dismiss_button(snapshot)
            form_element_count = sum(
                1 for e in snapshot.interactive_elements
                if e.tag_name.lower() in {"input", "select", "textarea"}
                or (e.role or "").lower() in {"combobox", "listbox", "radio", "checkbox", "spinbutton"}
            )
            # Form body loaded — more than just the Dismiss+Next shell
            if interactive_count > 2 or (has_dismiss and form_element_count > 0):
                return snapshot
            # Track stability of interactiveCount
            if interactive_count != last_count:
                last_count = interactive_count
                stable_since = time.time()
            # interactiveCount == 2 and stable for MIN_STABLE_SECS → no-field first step
            if interactive_count >= 2 and (time.time() - stable_since) >= MIN_STABLE_SECS:
                return snapshot
            time.sleep(0.4)
            snapshot = self.read_current_state(tab_id, filter_mode="interactive", scope="dialog-interactive", limit=120)
        return snapshot

    def detect_success_state(self, snapshot: BridgePageSnapshot) -> bool:
        blob = normalize_text(" ".join((snapshot.title, snapshot.visible_text_excerpt, snapshot.url)))
        if "/post-apply/" in snapshot.url:
            return True
        return any(normalize_text(marker) in blob for marker in SUCCESS_MARKERS)

    def detect_review_step(self, snapshot: BridgePageSnapshot) -> bool:
        blob = normalize_text(snapshot.visible_text_excerpt)
        if any(normalize_text(marker) in blob for marker in REVIEW_MARKERS):
            return True
        return bool(snapshot.find_controls("Submit application", exact=True))

    def detect_submit_step(self, snapshot: BridgePageSnapshot) -> bool:
        return bool(snapshot.find_controls("Submit application", exact=True) or snapshot.find_controls("Submit", exact=True))

    def fill_combobox_control(self, tab_id: Any, control: Any, value: str) -> bool:
        """Fill a combobox/listbox element, using setValue first then selectCustomOption fallback."""
        result = self.set_value(tab_id, control, value)
        if result.get("ok"):
            return True
        action: Dict[str, Any] = {"kind": "selectCustomOption", "value": value}
        if hasattr(control, "id"):
            action["targetId"] = control.id
        result = self.client.perform_action(tab_id, action)
        self._invalidate(tab_id)
        return bool(result.get("ok", False))

    def fill_fields(
        self,
        tab_id: Any,
        values_by_label: Mapping[str, str],
        *,
        refresh: bool = False,
    ) -> LinkedInFieldFillResult:
        started = time.perf_counter()
        snapshot = self.current_snapshot(tab_id, refresh=refresh, scope="dialog-interactive")
        normalized_values = self._normalize_values(values_by_label)
        result = LinkedInFieldFillResult()
        seen_labels: set[str] = set()

        for control in snapshot.interactive_elements:
            tag = control.tag_name.lower()
            role = (control.role or "").lower()
            is_combobox = role in {"combobox", "listbox"}
            if tag not in {"input", "textarea", "select"} and not is_combobox:
                continue
            if control.disabled:
                continue
            label = self._choose_field_label(control, normalized_values)
            if not label:
                continue
            seen_labels.add(label)
            desired_value = values_by_label[label]
            if control.value == desired_value:
                continue
            if is_combobox and tag not in {"input", "textarea", "select"}:
                self.fill_combobox_control(tab_id, control, desired_value)
            else:
                self.set_value(tab_id, control, desired_value)
            result.updated_controls.append(control.id)
            result.matched.append(label)

        for label in values_by_label:
            if label not in seen_labels:
                result.skipped.append(label)

        result.elapsed_seconds = time.perf_counter() - started
        return result

    def advance_application(
        self,
        tab_id: Any,
        *,
        values_by_label: Optional[Mapping[str, str]] = None,
        auto_submit: bool = True,
        max_steps: int = 8,
        refresh_limit: int = 120,
        initial_snapshot: Optional["BridgePageSnapshot"] = None,
    ) -> LinkedInActionResult:
        tab = self._tab_for_id(tab_id)
        actions: List[str] = []
        matched_controls: List[str] = []
        filled_fields: List[str] = []
        # Use caller-supplied snapshot when available (avoids re-reading during a
        # React transition and getting 0 elements immediately after dialog open).
        if initial_snapshot is not None:
            snapshot = initial_snapshot
        else:
            snapshot = self.read_current_state(tab_id, limit=refresh_limit, scope="dialog-interactive")

        if self.detect_success_state(snapshot):
            return LinkedInActionResult(
                status="submitted",
                submitted=True,
                tab=tab,
                message="LinkedIn already shows this job as submitted",
                timing=self.timing_summary(),
                final_state=snapshot.to_dict(),
            )

        for step in range(max_steps):
            if values_by_label:
                fill_result = self.fill_fields(tab_id, values_by_label)
                filled_fields.extend(fill_result.matched)
                if fill_result.updated_controls:
                    actions.append(f"fill:{fill_result.filled_count}")
                    snapshot = self.read_current_state(tab_id, limit=refresh_limit, scope="dialog-interactive")
                    if self.detect_success_state(snapshot):
                        return self._done(
                            tab=tab,
                            status="submitted",
                            submitted=True,
                            message="LinkedIn showed a submitted state after filling",
                            steps=step + 1,
                            actions=actions,
                            filled_fields=filled_fields,
                            matched_controls=matched_controls,
                            final_state=snapshot,
                        )

            submit = self._first_control(snapshot, SUBMIT_QUERIES)
            if submit:
                matched_controls.append(submit.id)
                if auto_submit:
                    self.click_control(tab_id, submit)
                    actions.append(f"click:{submit.id}")
                    time.sleep(2.0)  # wait for React transition after submit
                    snapshot = self.read_current_state(tab_id, limit=refresh_limit, scope="dialog-interactive")
                    if self.detect_success_state(snapshot):
                        return self._done(
                            tab=tab,
                            status="submitted",
                            submitted=True,
                            message="LinkedIn confirmed the application was submitted",
                            steps=step + 1,
                            actions=actions,
                            filled_fields=filled_fields,
                            matched_controls=matched_controls,
                            final_state=snapshot,
                        )
                    continue
                return self._done(
                    tab=tab,
                    status="ready_to_submit",
                    submitted=False,
                    message="Submit button is visible",
                    steps=step + 1,
                    actions=actions,
                    filled_fields=filled_fields,
                    matched_controls=matched_controls,
                    final_state=snapshot,
                )

            review = self._first_control(snapshot, REVIEW_QUERIES)
            if review:
                matched_controls.append(review.id)
                self.click_control(tab_id, review)
                actions.append(f"click:{review.id}")
                time.sleep(1.5)  # wait for React transition after review click
                snapshot = self.read_current_state(tab_id, limit=refresh_limit, scope="dialog-interactive")
                if self.detect_success_state(snapshot):
                    return self._done(
                        tab=tab,
                        status="submitted",
                        submitted=True,
                        message="LinkedIn confirmed the application was submitted",
                        steps=step + 1,
                        actions=actions,
                        filled_fields=filled_fields,
                        matched_controls=matched_controls,
                        final_state=snapshot,
                    )
                continue

            continue_control = self._first_control(snapshot, CONTINUE_QUERIES)
            if continue_control:
                matched_controls.append(continue_control.id)
                self.click_control(tab_id, continue_control)
                actions.append(f"click:{continue_control.id}")
                time.sleep(1.5)  # wait for React transition after continue/next click
                snapshot = self.read_current_state(tab_id, limit=refresh_limit, scope="dialog-interactive")
                if self.detect_success_state(snapshot):
                    return self._done(
                        tab=tab,
                        status="submitted",
                        submitted=True,
                        message="LinkedIn confirmed the application was submitted",
                        steps=step + 1,
                        actions=actions,
                        filled_fields=filled_fields,
                        matched_controls=matched_controls,
                        final_state=snapshot,
                    )
                continue

            done_control = self._first_control(snapshot, DONE_QUERIES)
            if done_control and self.detect_success_state(snapshot):
                matched_controls.append(done_control.id)
                return self._done(
                    tab=tab,
                    status="submitted",
                    submitted=True,
                    message="LinkedIn success screen is visible",
                    steps=step + 1,
                    actions=actions,
                    filled_fields=filled_fields,
                    matched_controls=matched_controls,
                    final_state=snapshot,
                )

            if self.detect_success_state(snapshot):
                return self._done(
                    tab=tab,
                    status="submitted",
                    submitted=True,
                    message="LinkedIn success state detected",
                    steps=step + 1,
                    actions=actions,
                    filled_fields=filled_fields,
                    matched_controls=matched_controls,
                    final_state=snapshot,
                )

            return self._done(
                tab=tab,
                status="review_required",
                submitted=False,
                message="No recognized LinkedIn action was visible",
                steps=step + 1,
                actions=actions,
                filled_fields=filled_fields,
                matched_controls=matched_controls,
                final_state=snapshot,
            )

        return self._done(
            tab=tab,
            status="review_required",
            submitted=False,
            message="LinkedIn application exceeded the supported step limit",
            steps=max_steps,
            actions=actions,
            filled_fields=filled_fields,
            matched_controls=matched_controls,
            final_state=snapshot,
        )

    def timing_summary(self) -> Dict[str, Any]:
        elapsed = time.perf_counter() - self._created_at
        return {
            "elapsed_seconds": round(elapsed, 3),
            "client": self.client.timing_summary(),
            "events": list(self._events),
        }

    def _invalidate(self, tab_id: Any) -> None:
        tab_key = f"{tab_id}:"
        for key in [candidate for candidate in self._snapshots if candidate.startswith(tab_key)]:
            self._snapshots.pop(key, None)

    def _snapshot_key(self, tab_id: Any, filter_mode: str, scope: str) -> str:
        return f"{tab_id}:{filter_mode}:{scope}"

    def _fallback_apply_url(self, current_url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)/", current_url or "")
        if not match:
            return ""
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}/apply/"

    def _tab_for_id(self, tab_id: Any) -> BridgeTab:
        tabs = self.client.list_tabs()
        for tab in tabs:
            if tab.id == str(tab_id):
                return tab
        # Tab ID may have changed after navigation — return the most relevant tab
        for tab in tabs:
            if "linkedin.com" in (tab.url or ""):
                return tab
        if tabs:
            return tabs[0]
        raise ValueError(f"Unknown LinkedIn tab id: {tab_id!r}")

    def _done(
        self,
        *,
        tab: BridgeTab,
        status: str,
        submitted: bool,
        message: str,
        steps: int,
        actions: List[str],
        filled_fields: List[str],
        matched_controls: List[str],
        final_state: BridgePageSnapshot,
    ) -> LinkedInActionResult:
        return LinkedInActionResult(
            status=status,
            submitted=submitted,
            tab=tab,
            steps=steps,
            message=message,
            actions=list(actions),
            filled_fields=list(dict.fromkeys(filled_fields)),
            matched_controls=list(dict.fromkeys(matched_controls)),
            timing=self.timing_summary(),
            final_state=final_state.to_dict(),
        )

    def _normalize_values(self, values_by_label: Mapping[str, str]) -> Dict[str, str]:
        return {str(key): "" if value is None else str(value) for key, value in values_by_label.items() if str(key).strip()}

    def _choose_field_label(
        self,
        control: BridgeElement,
        normalized_values: Mapping[str, str],
    ) -> Optional[str]:
        candidates = [
            control.label,
            control.aria_label,
            control.placeholder,
            control.name,
            control.text,
            control.href,
        ]
        exact_candidates = {normalize_text(candidate): candidate for candidate in candidates if normalize_text(candidate)}

        for label in normalized_values:
            normalized_label = normalize_text(label)
            if normalized_label in exact_candidates:
                return label

        for label in normalized_values:
            normalized_label = normalize_text(label)
            if not normalized_label:
                continue
            if any(normalized_label in normalize_text(candidate) for candidate in candidates if candidate):
                return label

        return None

    def _first_control(self, snapshot: BridgePageSnapshot, queries: Sequence[str]) -> Optional[BridgeElement]:
        for query in queries:
            match = snapshot.find_first_control(query, exact=True)
            if match:
                return match
        return None
