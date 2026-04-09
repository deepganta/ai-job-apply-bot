import math
import re
import time
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from playwright.sync_api import Error, Page, sync_playwright

from .browser import open_browser_session
from .config import Settings
from .models import JobRecord
from .utils import as_utc_iso, compact_text, normalize_text, url_slug, utc_now


INDEED_LISTINGS_SCRIPT = """
() => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const visible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const cards = Array.from(document.querySelectorAll('[data-jk], [data-testid="slider_item"], .job_seen_beacon, article'));
  const seen = new Set();
  const jobs = [];

  for (const card of cards) {
    if (!visible(card)) continue;

    const titleNode = card.querySelector("a[aria-label^='full details of'], h2 a, a[id^='sj_']");
    if (!visible(titleNode)) continue;

    const href = titleNode?.href || '';
    const hrefJkMatch = href.match(/[?&]jk=([^&]+)/i);
    const hrefJk = hrefJkMatch ? hrefJkMatch[1] : '';
    const jk = card.getAttribute('data-jk') || card.querySelector('[data-jk]')?.getAttribute('data-jk') || hrefJk || '';
    const title = normalize(titleNode?.innerText || titleNode?.textContent || '');
    if (!jk || !title || title.length < 4) continue;

    const dedupeKey = `${jk}|${title}`;
    if (seen.has(dedupeKey)) continue;

    const company = normalize(
      card?.querySelector("[data-testid='company-name'], [data-testid='inlineHeader-companyName'], .companyName, [data-company-name='true']")?.textContent || ''
    );
    const location = normalize(
      card?.querySelector("[data-testid='text-location'], [data-testid='job-location'], .companyLocation")?.textContent || ''
    );
    const postedText = normalize(
      card?.querySelector("time, [data-testid='myJobsStateDate'], [data-testid='job-age'], .date")?.textContent || ''
    );
    const snippet = normalize(
      card?.querySelector(".job-snippet, [data-testid='job-snippet'], .slider-snippet")?.textContent || ''
    );
    const cardText = normalize(card?.innerText || '');
    const easyApply = /easily apply|apply with indeed/i.test(cardText);

    seen.add(dedupeKey);
    jobs.push({ jk, title, company, location, postedText, snippet, easyApply, cardText });
  }

  return jobs.slice(0, 80);
}
"""


INDEED_DETAIL_SCRIPT = """
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
  const actions = Array.from(document.querySelectorAll('button, a'))
    .filter((element) => visible(element))
    .map((element) => ({
      text: normalize(element.innerText || element.textContent || element.value || ''),
      href: element.tagName.toLowerCase() === 'a' ? element.href : '',
    }))
    .filter((item) => /apply|continue|review|submit|resume/i.test(item.text))
    .slice(0, 30);

  const postedMatch = bodyText.match(/(just posted|today|yesterday|\\d+\\s+(?:minutes?|hours?|days?)\\s+ago|posted\\s+\\d+\\s+(?:minutes?|hours?|days?)\\s+ago)/i);

  return {
    title: text(["h1", "[data-testid='jobsearch-JobInfoHeader-title']", "[data-testid='simpler-jobTitle']"]),
    company: text(["[data-testid='inlineHeader-companyName']", "[data-testid='company-name']", ".jobsearch-CompanyInfoContainer a", ".jobsearch-CompanyInfoContainer div"]),
    location: text(["[data-testid='job-location']", "[data-testid='text-location']", ".jobsearch-CompanyInfoContainer [data-testid='inlineHeader-companyLocation']", ".jobsearch-JobInfoHeader-subtitle div"]),
    description: text(["#jobDescriptionText", "[data-testid='jobsearch-JobComponent-description']", "main"]) || bodyText,
    postedText: postedMatch ? postedMatch[0] : '',
    applyText: (actions.find((item) => /apply now|easily apply|continue to application|continue applying/i.test(item.text)) || {}).text || '',
    applyHref: (actions.find((item) => /apply now|easily apply|continue to application|continue applying/i.test(item.text)) || {}).href || '',
    bodyText,
  };
}
"""


AI_ML_KEYWORDS = [
    "ai engineer",
    "artificial intelligence",
    "applied ai",
    "applied scientist",
    "computer vision",
    "data scientist",
    "genai",
    "generative ai",
    "llm",
    "machine learning",
    "ml engineer",
    "mlops",
    "nlp",
    "prompt engineer",
    "rag",
]

GENERIC_PAGE_TITLES = {"careers", "job search", "jobs", "open positions", "search results"}
TECH_TITLE_HINTS = {"ai", "architect", "data", "developer", "engineer", "llm", "machine learning", "ml", "mlops", "nlp", "scientist", "software"}

GATE_MARKERS = (
    "just a moment",
    "enable javascript and cookies to continue",
    "verify you are human",
    "security check",
    "checking your browser",
)

LOGIN_MARKERS = (
    "sign in",
    "continue with google",
    "continue with email",
    "log in",
)


def build_indeed_search_url(query: str, location: str, recency_hours: int, start: int = 0) -> str:
    params = {
        "q": query.strip(),
        "l": location.strip(),
        "sort": "date",
        "fromage": max(1, math.ceil(max(recency_hours, 1) / 24)),
        "jt": "contract",
        "sc": "0kf:attr(NJXCK);",
    }
    if start > 0:
        params["start"] = start
    return f"https://www.indeed.com/jobs?{urlencode(params)}"


def build_indeed_job_url(query: str, location: str, recency_hours: int, jk: str) -> str:
    _ = query, location, recency_hours
    return f"https://www.indeed.com/viewjob?jk={jk.strip()}"


class IndeedDiscoveryService:
    def __init__(self, settings: Settings, progress_callback: Optional[Callable[..., None]] = None) -> None:
        self.settings = settings
        self.progress_callback = progress_callback

    def discover(
        self,
        query: str,
        location: str,
        max_pages: int = 2,
        max_jobs: int = 20,
        easy_apply_only: bool = True,
        parity_mode: bool = False,
    ) -> List[JobRecord]:
        query = (query or "").strip()
        location = (location or "").strip()
        if not query:
            raise ValueError("Indeed query is required.")

        label = self._label(query, location)
        jobs: Dict[str, JobRecord] = {}

        with sync_playwright() as playwright:
            session = open_browser_session(playwright, self.settings)
            detail_page: Optional[Page] = None
            try:
                listings_page = session.context.new_page()
                listings_page.set_default_timeout(min(self.settings.timeout_ms, 25000))
                try:
                    detail_page = session.context.new_page()
                    detail_page.set_default_timeout(min(self.settings.timeout_ms, 20000))
                except Error:
                    detail_page = None
                self._notify(
                    "scan_started",
                    message=f"Scanning Indeed for {query} in {location or 'all locations'}",
                )
                self._notify("vendor_started", vendor=label, url=build_indeed_search_url(query, location, self.settings.recency_hours))
                page_limit = max(1, max_pages)
                job_limit = max(1, max_jobs)
                if parity_mode:
                    # Ensure parity mode can return all visible cards from scanned pages.
                    job_limit = max(job_limit, page_limit * 80)

                for page_index in range(page_limit):
                    if len(jobs) >= job_limit:
                        break

                    search_url = build_indeed_search_url(
                        query=query,
                        location=location,
                        recency_hours=self.settings.recency_hours,
                        start=page_index * 10,
                    )
                    self._notify(
                        "vendor_url",
                        vendor=label,
                        url=search_url,
                        message=f"Scanning Indeed results page {page_index + 1}",
                    )
                    listings_page.goto(search_url, wait_until="domcontentloaded")
                    if not self._wait_until_ready(
                        listings_page,
                        expected_selectors=["a[href*='/viewjob']", "a[href*='jk=']", "h1"],
                        vendor=label,
                        waiting_message="Waiting for manual Indeed login or verification",
                    ):
                        raise RuntimeError(
                            "Indeed did not become ready. Open a headed browser profile, sign in, and solve any verification prompt."
                        )

                    self._scroll_page_to_bottom(listings_page)
                    candidates = self._extract_listing_candidates(listings_page)
                    if not candidates:
                        if self._looks_like_gate(self._body_text(listings_page)):
                            raise RuntimeError("Indeed is still blocking the search page.")
                        continue

                    for candidate in candidates:
                        if len(jobs) >= job_limit:
                            break
                        job = self._inspect_candidate(
                            candidate=candidate,
                            label=label,
                            query=query,
                            location=location,
                            easy_apply_only=easy_apply_only,
                            parity_mode=parity_mode,
                            detail_page=detail_page,
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
                    message=f"Indeed scan finished with {len(results)} jobs",
                )
                return results
            finally:
                if detail_page is not None:
                    try:
                        detail_page.close()
                    except Error:
                        pass
                session.close()

    def bootstrap_session(self, url: str = "https://www.indeed.com/") -> None:
        if self.settings.headless:
            raise RuntimeError("Indeed login requires a visible browser. Set JOB_BOT_HEADLESS=false first.")

        with sync_playwright() as playwright:
            session = open_browser_session(playwright, self.settings)
            try:
                page = session.context.new_page()
                page.set_default_timeout(min(self.settings.timeout_ms, 25000))
                page.goto(url, wait_until="domcontentloaded")
                print(f"Opened {url} using profile {self.settings.browser_profile_dir}")
                print("Sign in to Indeed in the browser window, then return here.")
                input("Press Enter when you are done with login and any verification prompts...")
            finally:
                session.close()

    def _inspect_candidate(
        self,
        candidate: Dict[str, str],
        label: str,
        query: str,
        location: str,
        easy_apply_only: bool,
        parity_mode: bool = False,
        detail_page: Optional[Page] = None,
    ) -> Optional[JobRecord]:
        jk = str(candidate.get("jk", "") or "").strip()
        title = str(candidate.get("title", "") or "").strip()
        if not jk or not title:
            return None

        easy_apply = bool(candidate.get("easyApply", False))
        if easy_apply_only and not easy_apply and not parity_mode:
            return None

        card_text = str(candidate.get("cardText", "") or "")
        # NOTE: jt=contract is already in the search URL so we don't double-filter here.
        # _matches_contract_constraints is kept as a soft signal but not used to discard.

        description = compact_text(candidate.get("snippet", ""), limit=2000)
        company = str(candidate.get("company", "") or "Indeed")
        location_value = str(candidate.get("location", "") or "")
        url = build_indeed_job_url(query=query, location=location, recency_hours=self.settings.recency_hours, jk=jk)
        if detail_page is not None:
            try:
                detail_page.goto(url, wait_until="domcontentloaded")
                detail_page.wait_for_timeout(1500)
                detail_data = detail_page.evaluate(INDEED_DETAIL_SCRIPT)
                if isinstance(detail_data, dict):
                    full_desc = str(detail_data.get("description", "") or "")
                    if full_desc:
                        description = compact_text(full_desc, limit=4000)
                    if not candidate.get("postedText") and detail_data.get("postedText"):
                        candidate["postedText"] = str(detail_data.get("postedText", "") or "")
            except Error:
                pass
            except Exception:
                pass

        experience_ok = self._matches_experience_constraints(title, description or card_text)
        if not experience_ok and not parity_mode:
            return None

        posted_at = self._parse_posted_at(str(candidate.get("postedText", "") or ""))
        if posted_at is None and easy_apply:
            posted_at = utc_now()

        ai_ml_match, matched_keywords = self._is_ai_ml_job(title, description or title)

        return JobRecord(
            job_id=url_slug(url),
            source_url=url,
            discovered_from=label,
            company=company,
            title=title,
            location=location_value,
            posted_at=as_utc_iso(posted_at),
            description=description,
            provider="indeed",
            apply_url=url,
            easy_apply=easy_apply,
            apply_supported=True,
            trusted=True,
            ai_ml_match=ai_ml_match,
            recency_ok=posted_at is not None and utc_now() - posted_at <= timedelta(hours=self.settings.recency_hours),
            criteria_ok=experience_ok,
            matched_keywords=matched_keywords,
            status="pending",
            reason=self._default_reason(
                ai_ml_match,
                posted_at,
                easy_apply,
                easy_apply_only,
                parity_mode=parity_mode,
                experience_ok=experience_ok,
            ),
        )

    def _extract_listing_candidates(self, page: Page) -> List[Dict[str, str]]:
        try:
            candidates = page.evaluate(INDEED_LISTINGS_SCRIPT)
        except Error:
            return []
        if not isinstance(candidates, list):
            return []
        return [candidate for candidate in candidates if isinstance(candidate, dict)]

    def _scroll_page_to_bottom(self, page: Page) -> None:
        script = """
async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const target = document.scrollingElement || document.documentElement || document.body;
  const viewport = window.innerHeight || 800;
  const stepSize = Math.max(Math.floor(viewport * 0.9), 400);

  let stagnant = 0;
  let previousY = -1;
  let previousHeight = -1;

  for (let i = 0; i < 40; i++) {
    const maxY = Math.max((target.scrollHeight || 0) - viewport, 0);
    const nextY = Math.min((window.scrollY || 0) + stepSize, maxY);
    window.scrollTo(0, nextY);
    await wait(250);

    const currentY = window.scrollY || 0;
    const height = target.scrollHeight || 0;
    const atBottom = currentY + viewport >= height - 8;

    if (currentY === previousY && height === previousHeight && atBottom) {
      stagnant += 1;
    } else {
      stagnant = 0;
    }
    previousY = currentY;
    previousHeight = height;

    if (stagnant >= 4) {
      break;
    }
  }

  window.scrollTo(0, target.scrollHeight || 0);
}
"""
        try:
            page.evaluate(script)
            page.wait_for_timeout(500)
        except Error:
            return

    def _wait_until_ready(
        self,
        page: Page,
        expected_selectors: List[str],
        vendor: str,
        waiting_message: str,
    ) -> bool:
        deadline = time.time() + max(self.settings.manual_gate_timeout_ms, 1000) / 1000.0
        waiting_notified = False
        while time.time() < deadline:
            if self._has_expected_selector(page, expected_selectors):
                return True
            body = self._body_text(page)
            if self._looks_like_gate(body) or self._looks_like_login(body):
                if not waiting_notified:
                    self._notify("vendor_url", vendor=vendor, url=page.url, message=waiting_message)
                    waiting_notified = True
            time.sleep(1.0)
        return self._has_expected_selector(page, expected_selectors)

    def _has_expected_selector(self, page: Page, selectors: List[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 3)
                for index in range(count):
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

    def _looks_like_gate(self, body_text: str) -> bool:
        return any(marker in body_text for marker in GATE_MARKERS)

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

        if "today" in lowered or "just posted" in lowered:
            return now
        if "yesterday" in lowered:
            return now - timedelta(days=1)
        return None

    def _is_easy_apply(self, apply_text: str) -> bool:
        lowered = normalize_text(apply_text)
        return "apply now" in lowered or "easily apply" in lowered

    def _matches_contract_constraints(self, text: str) -> bool:
        lowered = normalize_text(text)
        return any(token in lowered for token in ("contract", "c2c", "corp to corp", "corp-to-corp", "1099"))

    def _matches_experience_constraints(self, title: str, text: str) -> bool:
        lowered_title = normalize_text(title)
        lowered_text = normalize_text(text)
        if any(token in lowered_title for token in ("senior", "staff", "principal", "lead", "manager", "director", "architect")):
            return False

        year_patterns = [
            r"([5-9]|[1-9][0-9])\+?\s+years?",
            r"([5-9]|[1-9][0-9])\s*-\s*([5-9]|[1-9][0-9])\s+years?",
        ]
        for pattern in year_patterns:
            if re.search(pattern, lowered_text):
                return False
        return True

    def _is_ai_ml_job(self, title: str, description: str) -> Tuple[bool, List[str]]:
        normalized_title = normalize_text(title)
        normalized_description = normalize_text(description)
        title_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_title]

        if normalized_title in GENERIC_PAGE_TITLES and not title_matches:
            return False, []
        if title_matches:
            return True, title_matches
        if not any(hint in normalized_title for hint in TECH_TITLE_HINTS):
            return False, []

        description_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_description]
        return bool(description_matches), description_matches

    def _default_reason(
        self,
        ai_ml_match,
        posted_at,
        easy_apply: bool,
        easy_apply_only: bool,
        parity_mode: bool = False,
        experience_ok: bool = True,
    ) -> str:
        if parity_mode:
            reasons: List[str] = []
            if not ai_ml_match:
                reasons.append("Not AI/ML focused")
            if not experience_ok:
                reasons.append("Experience exceeds 4 years")
            if posted_at is None:
                reasons.append("Missing posted date")
            elif utc_now() - posted_at > timedelta(hours=self.settings.recency_hours):
                reasons.append(f"Older than {self.settings.recency_hours} hours")
            if not easy_apply:
                reasons.append("Indeed listing needs manual apply")
            return "; ".join(reasons)

        if not ai_ml_match:
            return "Not AI/ML focused"
        if posted_at is None:
            return "Missing posted date"
        if utc_now() - posted_at > timedelta(hours=self.settings.recency_hours):
            return f"Older than {self.settings.recency_hours} hours"
        if not easy_apply:
            return "Not Indeed Easy Apply" if easy_apply_only else "Indeed listing needs manual apply"
        return ""

    def _label(self, query: str, location: str) -> str:
        if location:
            return f"Indeed: {query} | {location}"
        return f"Indeed: {query}"

    def _notify(self, event: str, **payload: object) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, **payload)
        except Exception:
            return
