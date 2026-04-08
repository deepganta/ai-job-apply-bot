import re
import time
from datetime import timedelta
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from playwright.sync_api import Error, Page, sync_playwright

from .browser import open_browser_session, prepare_work_page
from .config import Settings
from .eligibility import analyze_job_fit
from .models import JobRecord
from .utils import as_utc_iso, compact_text, normalize_text, sanitize_filename, url_slug, utc_now, within_last_hours


LINKEDIN_LISTINGS_SCRIPT = """
() => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const extractJobId = (value) => {
    const match = String(value || '').match(/(\\d{6,})/);
    return match ? match[1] : '';
  };
  const extractText = (value) => {
    if (!value) return '';
    if (typeof value === 'string') return normalize(value);
    if (typeof value.text === 'string') return normalize(value.text);
    if (typeof value.accessibilityText === 'string') return normalize(value.accessibilityText);
    return '';
  };

  const jobs = [];
  const seen = new Set();

  const pushJob = (job) => {
    const jobId = normalize(job.jobId);
    const title = normalize(job.title);
    if (!jobId || !title || seen.has(jobId)) return;
    seen.add(jobId);
    jobs.push({
      jobId,
      href: normalize(job.href) || `https://www.linkedin.com/jobs/view/${jobId}/`,
      title,
      company: normalize(job.company),
      location: normalize(job.location),
      postedText: normalize(job.postedText),
      easyApply: !!job.easyApply,
      cardText: normalize(job.cardText),
    });
  };

  const hydrateFromPayload = (payload) => {
    const root = payload && typeof payload === 'object' ? payload : {};
    const data = root.data && typeof root.data === 'object' ? root.data : {};
    const elements = Array.isArray(data.elements) ? data.elements : [];
    const included = Array.isArray(root.included) ? root.included : [];
    if (!elements.length || !included.length) return;

    const byUrn = new Map();
    for (const item of included) {
      const urn = normalize(item && item.entityUrn);
      if (urn) byUrn.set(urn, item);
    }

    for (const element of elements) {
      const cardUrn = normalize(element?.jobCardUnion?.['*jobPostingCard'] || element?.jobCardUnion?.jobPostingCard || '');
      const card = byUrn.get(cardUrn);
      if (!card) continue;

      const jobId = extractJobId(card.jobPostingUrn || card.entityUrn || cardUrn);
      const title = extractText(card.title) || normalize(card.jobPostingTitle || '');
      const company = extractText(card.primaryDescription);
      const location = extractText(card.secondaryDescription);
      const insightText = normalize(
        (Array.isArray(card.jobInsightsV2) ? card.jobInsightsV2 : [])
          .map((item) => extractText(item))
          .filter(Boolean)
          .join(' | ')
      );

      pushJob({
        jobId,
        href: `https://www.linkedin.com/jobs/view/${jobId}/`,
        title,
        company,
        location,
        postedText: insightText,
        easyApply: false,
        cardText: [title, company, location, insightText].filter(Boolean).join(' | '),
      });
    }
  };

  for (const node of Array.from(document.querySelectorAll('code'))) {
    const raw = (node.textContent || '').trim();
    if (!raw || raw[0] !== '{') continue;
    if (!raw.includes('jobCardUnion') || !raw.includes('fsd_jobPostingCard')) continue;
    try {
      hydrateFromPayload(JSON.parse(raw));
    } catch (_error) {
      continue;
    }
  }

  const anchors = Array.from(document.querySelectorAll("a[href*='/jobs/view/']"));
  for (const anchor of anchors) {
    const href = anchor.href || '';
    const hrefMatch = href.match(/\\/jobs\\/view\\/(\\d+)/);
    const card = anchor.closest("[data-job-id], li, .job-card-container, .jobs-search-results__list-item") || anchor.parentElement;
    const dataJobId = card?.getAttribute('data-job-id') || '';
    const jobId = (hrefMatch && hrefMatch[1]) || dataJobId;
    const title = normalize(
      card?.querySelector('.job-card-list__title, .job-card-container__link, .job-card-container__job-title, strong')?.innerText ||
      anchor.innerText ||
      anchor.textContent ||
      ''
    );
    const cardText = normalize(card?.innerText || '');
    pushJob({
      jobId,
      href,
      title,
      company: normalize(
        card?.querySelector('.artdeco-entity-lockup__subtitle, .job-card-container__company-name, .job-card-container__primary-description, .job-card-container__subtitle')?.innerText ||
        ''
      ),
      location: normalize(
        card?.querySelector('.job-card-container__metadata-item, .job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption')?.innerText ||
        ''
      ),
      postedText: normalize(
        card?.querySelector('time, .job-card-list__footer-wrapper, .job-card-container__footer-wrapper, .job-card-container__listed-time')?.innerText ||
        ''
      ),
      easyApply: /easy apply/i.test(cardText),
      cardText,
    });
  }

  return jobs.slice(0, 80);
}
"""


LINKEDIN_DETAIL_SCRIPT = """
() => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const text = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (!node) continue;
      const value = normalize(node.innerText || node.textContent || '');
      if (value) return value;
    }
    return '';
  };

  const bodyText = normalize(document.body?.innerText || '');
  const topCard = document.querySelector('.job-details-jobs-unified-top-card__container--two-pane, .job-details-jobs-unified-top-card, .jobs-unified-top-card');
  const topCardText = normalize(topCard?.innerText || topCard?.textContent || '');
  const buttons = Array.from(document.querySelectorAll('button, a, .jobs-apply-button'))
    .filter((element) => visible(element))
    .map((element) => ({
      text: normalize(element.innerText || element.textContent || element.value || ''),
      href: element.tagName.toLowerCase() === 'a' ? element.href : '',
      isEasyApply: element.classList.contains('jobs-apply-button') || /easy apply/i.test(element.innerText || element.ariaLabel || ''),
    }))
    .filter((item) => /apply|submit|review|continue|next/i.test(item.text) || item.isEasyApply)
    .slice(0, 40);

  const applyButton = buttons.find((item) => item.isEasyApply) || buttons.find((item) => /apply/i.test(item.text)) || { text: '', href: '' };

  return {
    title: text([
      'h1',
      '.jobs-unified-top-card__job-title',
      '.job-details-jobs-unified-top-card__job-title',
    ]),
    company: text([
      '.jobs-unified-top-card__company-name a',
      '.job-details-jobs-unified-top-card__company-name a',
      '.jobs-unified-top-card__company-name',
    ]),
    location: text([
      '.jobs-unified-top-card__primary-description-container',
      '.job-details-jobs-unified-top-card__primary-description-container',
      '.jobs-unified-top-card__subtitle-primary-grouping',
    ]),
    postedText: text([
      '.jobs-unified-top-card__primary-description-container',
      '.job-details-jobs-unified-top-card__primary-description-container',
      '.jobs-unified-top-card__subtitle-primary-grouping',
      'time',
    ]),
    description: text([
      '.jobs-description-content__text',
      '.jobs-description__content',
      '.jobs-box__html-content',
      '.jobs-description__container',
      '#job-details',
    ]),
    topCardText,
    applyText: applyButton.text || '',
    applyHref: applyButton.href || '',
    bodyText,
  };
}
"""


LOGIN_MARKERS = (
    "agree join linkedin",
    "already on linkedin sign in",
    "sign in",
    "join linkedin",
)

MAX_LINKEDIN_SCAN_PAGES = 3


def build_linkedin_search_url(
    query: str,
    location: str,
    recency_hours: int,
    start: int = 0,
    easy_apply_only: bool = True,
    contract_only: bool = True,
    remote_only: bool = False,
    experience_levels: Optional[List[str]] = None,
) -> str:
    params = {
        "keywords": query.strip(),
        "location": location.strip(),
        "f_TPR": f"r{max(recency_hours, 1) * 3600}",
    }
    if contract_only:
        params["f_JT"] = "C"
    if easy_apply_only:
        params["f_AL"] = "true"
    if remote_only:
        params["f_WT"] = "2"
    normalized_levels = [str(item).strip() for item in (experience_levels or []) if str(item).strip()]
    if normalized_levels:
        params["f_E"] = ",".join(normalized_levels)
    if start > 0:
        params["start"] = start
    return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"


def build_linkedin_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


class LinkedInDiscoveryService:
    def __init__(self, settings: Settings, progress_callback: Optional[Callable[..., None]] = None) -> None:
        self.settings = settings
        self.progress_callback = progress_callback
        self.profile = settings.load_profile()
        self.max_experience_years = int(self.profile.get("max_target_experience_years", 4) or 4)

    def discover(
        self,
        query: str,
        location: str,
        max_pages: int = 2,
        max_jobs: int = 20,
        recency_hours: Optional[int] = None,
        easy_apply_only: bool = True,
        contract_only: bool = True,
        remote_only: bool = False,
        experience_levels: Optional[List[str]] = None,
    ) -> List[JobRecord]:
        query = (query or "").strip()
        location = (location or "").strip()
        if not query:
            raise ValueError("LinkedIn query is required.")

        label = self._label(query, location)
        jobs: Dict[str, JobRecord] = {}
        effective_recency_hours = max(1, int(recency_hours or self.settings.recency_hours or 168))
        page_limit = min(max(1, int(max_pages or 1)), MAX_LINKEDIN_SCAN_PAGES)

        with sync_playwright() as playwright:
            session = open_browser_session(playwright, self.settings)
            try:
                page = prepare_work_page(session.context)
                page.set_default_timeout(min(self.settings.timeout_ms, 25000))
                self._notify("scan_started", message=f"Scanning LinkedIn for {query} in {location or 'all locations'}")
                self._notify(
                    "vendor_started",
                    vendor=label,
                    url=build_linkedin_search_url(
                        query=query,
                        location=location,
                        recency_hours=effective_recency_hours,
                        easy_apply_only=easy_apply_only,
                        contract_only=contract_only,
                        remote_only=remote_only,
                        experience_levels=experience_levels,
                    ),
                )

                for page_index in range(page_limit):
                    if len(jobs) >= max_jobs:
                        break

                    search_url = build_linkedin_search_url(
                        query=query,
                        location=location,
                        recency_hours=effective_recency_hours,
                        start=page_index * 25,
                        easy_apply_only=easy_apply_only,
                        contract_only=contract_only,
                        remote_only=remote_only,
                        experience_levels=experience_levels,
                    )
                    self._notify(
                        "vendor_url",
                        vendor=label,
                        url=search_url,
                        message=f"Scanning LinkedIn results page {page_index + 1}",
                    )
                    page.goto(search_url, wait_until="domcontentloaded")
                    if not self._wait_until_ready(page):
                        raise RuntimeError(
                            "LinkedIn did not become ready. Sign in in the browser window and open the Jobs tab first."
                        )
                    page.set_viewport_size({"width": 1440, "height": 1200})
                    self._ensure_visible_filters(
                        page=page,
                        query=query,
                        page_index=page_index,
                        easy_apply_only=easy_apply_only,
                        contract_only=contract_only,
                        remote_only=remote_only,
                        experience_levels=experience_levels,
                    )
                    self._scroll_results_list_to_bottom(page)

                    candidates = self._extract_listing_candidates(page)
                    if not candidates:
                        if self._looks_like_login(self._body_text(page)):
                            raise RuntimeError("LinkedIn sign-in is required before scanning jobs.")
                        continue

                    for candidate in candidates:
                        if len(jobs) >= max_jobs:
                            break
                        job = self._inspect_candidate(
                            page=page,
                            candidate=candidate,
                            label=label,
                            easy_apply_only=easy_apply_only,
                            contract_only=contract_only,
                            recency_hours=effective_recency_hours,
                        )
                        if job:
                            jobs[job.job_id] = job

                results = list(jobs.values())
                results.sort(key=lambda item: (item.eligible, item.easy_apply, item.posted_at, item.company, item.title), reverse=True)
                self._notify(
                    "vendor_done",
                    vendor=label,
                    jobs_found=len(results),
                    eligible_found=len([job for job in results if job.eligible]),
                    message=f"LinkedIn scan finished with {len(results)} jobs",
                )
                return results
            finally:
                session.close()

    def bootstrap_session(self, url: str = "https://www.linkedin.com/jobs/") -> None:
        if self.settings.headless:
            raise RuntimeError("LinkedIn login requires a visible browser. Set JOB_BOT_HEADLESS=false first.")

        with sync_playwright() as playwright:
            session = open_browser_session(playwright, self.settings)
            try:
                page = prepare_work_page(session.context)
                page.set_default_timeout(min(self.settings.timeout_ms, 25000))
                page.goto(url, wait_until="domcontentloaded")
                print(f"Opened {url}. Sign in to LinkedIn in the browser window, then return here.")
                input("Press Enter when login and any verification prompts are complete...")
            finally:
                session.close()

    def _inspect_candidate(
        self,
        page: Page,
        candidate: Dict[str, str],
        label: str,
        easy_apply_only: bool,
        contract_only: bool,
        recency_hours: int,
    ) -> Optional[JobRecord]:
        job_id = str(candidate.get("jobId", "") or "").strip()
        title = str(candidate.get("title", "") or "").strip()
        if not job_id or not title:
            return None

        detail_page = self._open_candidate_detail_page(page, candidate)
        if detail_page is None:
            return None
        try:
            job_url = build_linkedin_job_url(job_id)
            self._expand_description(detail_page)

            detail = self._extract_detail(detail_page)
            full_title = str(detail.get("title", "") or title).strip()
            company = str(detail.get("company", "") or candidate.get("company", "") or "LinkedIn").strip()
            location_value = str(detail.get("location", "") or candidate.get("location", "") or "").strip()
            top_card_text = str(detail.get("topCardText", "") or "")
            description_only = str(detail.get("description", "") or "")
            description = compact_text(
                " ".join(
                    part
                    for part in (
                        top_card_text,
                        description_only,
                        str(candidate.get("cardText", "") or ""),
                    )
                    if part
                ),
                limit=7000,
            )

            easy_apply = bool(candidate.get("easyApply", False)) or "easy apply" in normalize_text(str(detail.get("applyText", "") or ""))
            if easy_apply_only and not easy_apply:
                return None

            fit = analyze_job_fit(
                title=full_title,
                description=description,
                # When contract_only=True, the LinkedIn search URL already has f_JT=C
                # (Contract job type filter) applied, so any job returned IS already a
                # contract role. Skip the description keyword check to avoid false negatives.
                require_contract=False,
                max_experience_years=self.max_experience_years,
            )
            posted_at = self._parse_posted_at(str(detail.get("postedText", "") or candidate.get("postedText", "") or ""))

            return JobRecord(
                job_id=url_slug(job_url),
                source_url=job_url,
                discovered_from=label,
                company=company,
                title=full_title,
                location=location_value,
                posted_at=as_utc_iso(posted_at),
                description=description,
                provider="linkedin",
                apply_url=job_url,
                easy_apply=easy_apply,
                apply_supported=easy_apply,
                trusted=True,
                ai_ml_match=bool(fit["ai_ml_match"]),
                recency_ok=within_last_hours(posted_at, recency_hours),
                criteria_ok=bool(fit["eligible"]),
                matched_keywords=list(fit["matched_keywords"]),
                status="pending",
                reason=self._default_reason(fit, posted_at, easy_apply),
            )
        finally:
            try:
                detail_page.close()
            except Error:
                pass
            try:
                page.bring_to_front()
            except Error:
                pass

    def _open_candidate_detail_page(self, page: Page, candidate: Dict[str, str]) -> Optional[Page]:
        job_id = str(candidate.get("jobId", "") or "").strip()
        if not job_id:
            return None

        detail_page = page.context.new_page()
        detail_page.set_default_timeout(min(self.settings.timeout_ms, 25000))
        try:
            detail_page.goto(build_linkedin_job_url(job_id), wait_until="domcontentloaded")
            if not self._wait_until_ready(detail_page):
                raise RuntimeError("LinkedIn detail page did not become ready")
            self._wait_for_idle(detail_page)
            return detail_page
        except (Error, RuntimeError):
            try:
                detail_page.close()
            except Error:
                pass
            return None

    def _open_candidate_in_results(self, page: Page, candidate: Dict[str, str]) -> bool:
        job_id = str(candidate.get("jobId", "") or "").strip()
        title = normalize_text(str(candidate.get("title", "") or ""))
        if not job_id:
            return False

        selectors = [
            f"[data-job-id='{job_id}'] a[href*='/jobs/view/{job_id}']",
            f"[data-occludable-job-id='{job_id}'] a[href*='/jobs/view/{job_id}']",
            f"a[href*='/jobs/view/{job_id}']",
        ]
        previous_title = normalize_text(
            self._extract_detail(page).get("title", "") if isinstance(self._extract_detail(page), dict) else ""
        )

        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 3)
            except Error:
                continue
            for index in range(count):
                candidate_link = locator.nth(index)
                try:
                    if not candidate_link.is_visible():
                        continue
                    candidate_link.click(force=True)
                    page.wait_for_timeout(1200)
                except Error:
                    continue
                self._wait_for_idle(page)
                current_title = normalize_text(str(self._extract_detail(page).get("title", "") or ""))
                if title and current_title and title in current_title:
                    return True
                if current_title and current_title != previous_title:
                    return True
        return False

    def _extract_listing_candidates(self, page: Page) -> List[Dict[str, str]]:
        try:
            payload = page.evaluate(LINKEDIN_LISTINGS_SCRIPT)
        except Error:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _scroll_results_list_to_bottom(self, page: Page) -> None:
        # LinkedIn lazily loads cards in the results pane. Scroll to the bottom so
        # extraction sees the full page, not just the initially rendered subset.
        script = """
async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const candidateSelectors = [
    ".jobs-search-results-list",
    ".jobs-search-results-list__list",
    ".scaffold-layout__list-container",
    ".scaffold-layout__list",
    "[data-results-list]",
  ];
  const candidates = candidateSelectors
    .map((selector) => document.querySelector(selector))
    .filter(Boolean);

  let target = null;
  for (const node of candidates) {
    if ((node.scrollHeight || 0) > (node.clientHeight || 0) + 20) {
      target = node;
      break;
    }
  }
  if (!target) {
    target = document.scrollingElement || document.documentElement;
  }

  const isWindowScrollTarget =
    target === document.scrollingElement || target === document.documentElement || target === document.body;
  const viewHeight = isWindowScrollTarget ? (window.innerHeight || 800) : (target.clientHeight || 800);
  const stepSize = Math.max(Math.floor(viewHeight * 0.9), 400);

  let stagnant = 0;
  let previousCount = -1;
  let previousHeight = -1;

  for (let i = 0; i < 40; i++) {
    if (isWindowScrollTarget) {
      const nextY = Math.min((window.scrollY || 0) + stepSize, target.scrollHeight || 0);
      window.scrollTo(0, nextY);
    } else {
      target.scrollTop = Math.min((target.scrollTop || 0) + stepSize, target.scrollHeight || 0);
    }

    await wait(250);

    const count = document.querySelectorAll("a[href*='/jobs/view/']").length;
    const height = target.scrollHeight || 0;
    const top = isWindowScrollTarget ? (window.scrollY || 0) : (target.scrollTop || 0);
    const currentView = isWindowScrollTarget ? (window.innerHeight || 0) : (target.clientHeight || 0);
    const atBottom = top + currentView >= height - 8;

    if (count === previousCount && height === previousHeight && atBottom) {
      stagnant += 1;
    } else {
      stagnant = 0;
    }

    previousCount = count;
    previousHeight = height;
    if (stagnant >= 4) {
      break;
    }
  }

  if (isWindowScrollTarget) {
    window.scrollTo(0, target.scrollHeight || 0);
  } else {
    target.scrollTop = target.scrollHeight || target.scrollTop || 0;
  }
  await wait(300);
}
"""
        try:
            page.evaluate(script)
        except Error:
            return

    def _extract_detail(self, page: Page) -> Dict[str, str]:
        try:
            payload = page.evaluate(LINKEDIN_DETAIL_SCRIPT)
        except Error:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _ensure_visible_filters(
        self,
        page: Page,
        query: str,
        page_index: int,
        easy_apply_only: bool,
        contract_only: bool,
        remote_only: bool,
        experience_levels: Optional[List[str]],
    ) -> None:
        self._wait_for_idle(page)
        if not self._has_any_selector(page, ["button:has-text('All filters')"]):
            return

        if page_index == 0:
            self._capture_search_screenshot(page, query, "linkedin-results-before-filters")
            if self._open_all_filters(page):
                self._set_filter_choice(page, "Past week", True)
                desired_levels = {str(item).strip() for item in (experience_levels or []) if str(item).strip()}
                self._set_filter_choice(page, "Entry level", "2" in desired_levels)
                self._set_filter_choice(page, "Associate", "3" in desired_levels)
                self._set_filter_choice(page, "Internship", False)
                self._set_filter_choice(page, "Mid-Senior level", False)
                self._set_filter_choice(page, "Director", False)
                self._set_filter_choice(page, "Executive", False)
                self._set_filter_choice(page, "Contract", contract_only)
                self._set_filter_choice(page, "Remote", remote_only)
                self._set_filter_choice(page, "Toggle Easy Apply filter", easy_apply_only)
                self._capture_search_screenshot(page, query, "linkedin-results-filter-panel")
                self._click_filter_apply(page)
                self._wait_until_ready(page)

        self._capture_search_screenshot(page, query, f"linkedin-results-page-{page_index + 1}")

    def _open_all_filters(self, page: Page) -> bool:
        for selector in [
            "button:has-text('All filters')",
            "button[aria-label*='All filters']",
        ]:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 2)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    candidate.click(force=True)
                    page.wait_for_timeout(500)
                    return True
                except Error:
                    continue
        return False

    def _click_filter_apply(self, page: Page) -> None:
        for selector in [
            "button:has-text('Show results')",
            "button[aria-label*='Show results']",
        ]:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 2)
            except Error:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    candidate.click(force=True)
                    page.wait_for_timeout(1000)
                    return
                except Error:
                    continue

    def _set_filter_choice(self, page: Page, label_text: str, desired: bool) -> None:
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
            page.wait_for_timeout(200)
        except Error:
            return

    def _capture_search_screenshot(self, page: Page, query: str, label: str) -> str:
        screenshot_name = sanitize_filename(f"linkedin-{query}-{label}") + ".png"
        screenshot_path = self.settings.output_dir / screenshot_name
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Error:
            return ""
        return str(screenshot_path)

    def _expand_description(self, page: Page) -> None:
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

    def _wait_until_ready(self, page: Page) -> bool:
        deadline = time.time() + max(self.settings.manual_gate_timeout_ms, 1000) / 1000.0
        while time.time() < deadline:
            if self._has_any_selector(page, ["a[href*='/jobs/view/']", ".jobs-search-results-list", "button.jobs-apply-button", "button:has-text('Easy Apply')", "h1"]):
                return True
            if self._has_embedded_listing_payload(page):
                return True
            time.sleep(1.0)
        return self._has_any_selector(page, ["a[href*='/jobs/view/']", ".jobs-search-results-list", "button.jobs-apply-button", "button:has-text('Easy Apply')", "h1"]) or self._has_embedded_listing_payload(page)

    def _wait_for_idle(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.settings.timeout_ms, 7000))
        except Error:
            return

    def _has_any_selector(self, page: Page, selectors: List[str]) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 4)
            except Error:
                continue
            for index in range(count):
                try:
                    if locator.nth(index).is_visible():
                        return True
                except Error:
                    continue
        return False

    def _body_text(self, page: Page) -> str:
        try:
            return normalize_text(page.locator("body").inner_text())
        except Error:
            return ""

    def _has_embedded_listing_payload(self, page: Page) -> bool:
        script = """
() => Array.from(document.querySelectorAll('code')).some((node) => {
  const raw = (node.textContent || '').trim();
  return raw.includes('jobCardUnion') && raw.includes('fsd_jobPostingCard');
})
"""
        try:
            return bool(page.evaluate(script))
        except Error:
            return False

    def _looks_like_login(self, body_text: str) -> bool:
        return any(marker in body_text for marker in LOGIN_MARKERS)

    def _parse_posted_at(self, text: str):
        lowered = normalize_text(text)
        if not lowered:
            return None
        now = utc_now()

        minutes_match = re.search(r"(\d+)\s+minutes?\s+ago", lowered)
        if minutes_match:
            return now - timedelta(minutes=int(minutes_match.group(1)))

        hours_match = re.search(r"(\d+)\s+hours?\s+ago", lowered)
        if hours_match:
            return now - timedelta(hours=int(hours_match.group(1)))

        days_match = re.search(r"(\d+)\s+days?\s+ago", lowered)
        if days_match:
            return now - timedelta(days=int(days_match.group(1)))

        weeks_match = re.search(r"(\d+)\s+weeks?\s+ago", lowered)
        if weeks_match:
            return now - timedelta(weeks=int(weeks_match.group(1)))

        if "today" in lowered or "just now" in lowered:
            return now
        if "yesterday" in lowered:
            return now - timedelta(days=1)
        return None

    def _default_reason(self, fit: Dict[str, object], posted_at, easy_apply: bool) -> str:
        reasons = [str(reason) for reason in fit.get("reasons", [])]
        if reasons:
            return reasons[0]
        if posted_at is None:
            return "Missing posted date"
        if not within_last_hours(posted_at, self.settings.recency_hours):
            return f"Older than {self.settings.recency_hours} hours"
        if not easy_apply:
            return "Not LinkedIn Easy Apply"
        return ""

    def _label(self, query: str, location: str) -> str:
        if location:
            return f"LinkedIn: {query} | {location}"
        return f"LinkedIn: {query}"

    def _notify(self, event: str, **payload: object) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, **payload)
        except Exception:
            return
