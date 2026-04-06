import re
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .config import Settings
from .models import Vendor
from .utils import ensure_url, is_known_ats_host, load_json, normalize_domain, normalize_text, save_json, same_or_parent_domain


CAREER_HINTS = [
    "career",
    "careers",
    "career opportunities",
    "career search",
    "current openings",
    "employment",
    "find a job",
    "find work",
    "job openings",
    "job results",
    "job search",
    "jobs",
    "open positions",
    "open roles",
    "search jobs",
    "search results",
    "view jobs",
]

BLOCKED_HINTS = [
    "about",
    "article",
    "benefits",
    "blog",
    "career resources",
    "category",
    "contact us",
    "eeo notice",
    "faq",
    "fraud",
    "hashtag",
    "insight",
    "internal",
    "join our talent community",
    "job alert",
    "job alerts",
    "job search safety",
    "leadership",
    "locations",
    "newsletter",
    "news",
    "partner",
    "privacy",
    "refer a friend",
    "resource",
    "review",
    "sign up",
    "signup",
    "solution",
    "submit your resume",
    "tag",
    "talent community",
    "uncategorized",
    "upload resume",
    "veterans",
    "webinar",
]

ENTRY_PATH_HINTS = (
    "career",
    "job-search",
    "job-results",
    "jobs",
    "openings",
    "search-results",
    "search-jobs",
    "work-with-us",
)

JOB_DETAIL_HINTS = (
    "jobdetail",
    "/job/",
    "/job-details/",
    "job-description",
    "jobdescription",
    "/details/",
    "#/detail/",
    "requisition",
)

ROLE_HINTS = (
    "analyst",
    "architect",
    "consultant",
    "data",
    "developer",
    "engineer",
    "scientist",
    "software",
)

ACTION_ENTRY_HINTS = (
    "all jobs",
    "careers",
    "current openings",
    "find a job",
    "find jobs",
    "job results",
    "open positions",
    "search jobs",
    "view jobs",
)

REJECT_URL_HINTS = (
    "/blog/",
    "/category/",
    "/feed/",
    "/news/",
    "/tag/",
    "cold-join",
    "eeo-notice",
    "hashtag",
    "job-alert",
    "job-alerts",
    "job-search-safety",
    "linkedin.com",
    "newsletter",
    "refer",
    "respond",
    "review",
    "sign-up",
    "signup",
    "uncategorized",
)


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MANUAL_CAREER_PAGE_OVERRIDES: Dict[str, Dict[str, object]] = {
    "avidtr.com": {
        "career_page": "https://www.avidtr.com/job-search/",
        "score": 100,
        "method": "override",
        "link_text": "Job Search",
    },
    "jobs.vaco.com": {
        "career_page": "https://jobs.vaco.com/en-US/search",
        "score": 100,
        "method": "override",
        "link_text": "Search Jobs",
    },
    "modis.com": {
        "career_page": "https://www.modis.com/en-us/job-seekers/job-search/",
        "score": 100,
        "method": "override",
        "link_text": "MODIS Job Search",
    },
}


def load_career_page_cache(settings: Settings) -> Dict[str, Dict[str, object]]:
    return load_json(settings.career_pages_path, {})


def save_career_page_cache(settings: Settings, payload: Dict[str, Dict[str, object]]) -> None:
    save_json(settings.career_pages_path, payload)


def apply_career_page_cache(vendors: List[Vendor], cache: Dict[str, Dict[str, object]]) -> List[Vendor]:
    rewritten: List[Vendor] = []
    for vendor in vendors:
        record = cache.get(vendor.domain, {})
        resolved = ensure_url(str(record.get("career_page", "") or ""))
        rewritten.append(
            Vendor(
                name=vendor.name,
                website=resolved or vendor.website,
                domain=normalize_domain(resolved or vendor.website),
                aliases=list(vendor.aliases),
            )
        )
    return rewritten


class CareerPageResolver:
    def __init__(self, settings: Settings, progress_callback: Optional[Callable[..., None]] = None) -> None:
        self.settings = settings
        self.progress_callback = progress_callback
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self._playwright = None
        self._browser = None
        self._context = None

    def resolve_all(self, vendors: List[Vendor], use_cache: bool = True) -> Dict[str, Dict[str, object]]:
        cache = load_career_page_cache(self.settings) if use_cache else {}
        self._start_renderer()
        try:
            for vendor in vendors:
                existing = cache.get(vendor.domain)
                if existing and existing.get("career_page"):
                    continue
                try:
                    cache[vendor.domain] = self.resolve_vendor(vendor)
                except Exception as exc:
                    fallback = {
                        "career_page": ensure_url(vendor.website),
                        "score": 0,
                        "method": "fallback",
                    }
                    cache[vendor.domain] = fallback
                    self._notify("vendor_error", vendor=vendor.name, message=f"Career page resolution failed: {exc}")
            save_career_page_cache(self.settings, cache)
            return cache
        finally:
            self._stop_renderer()

    def resolve_vendor(self, vendor: Vendor) -> Dict[str, object]:
        override = MANUAL_CAREER_PAGE_OVERRIDES.get(vendor.domain)
        if override is not None:
            self._notify(
                "vendor_resolved",
                vendor=vendor.name,
                url=str(override["career_page"]),
                career_page=str(override["career_page"]),
                message=f"Resolved career page: {override['career_page']}",
            )
            return dict(override)

        self._notify("vendor_resolving", vendor=vendor.name, url=vendor.website, message=f"Resolving career page for {vendor.name}")

        html, final_url = self._fetch_html(vendor.website)
        allowed_domains = self._allowed_domains(vendor.website, final_url or vendor.website)
        candidate_links = [
            {
                "url": final_url or vendor.website,
                "text": vendor.name,
                "score": self._score_candidate(final_url or vendor.website, vendor.name),
            }
        ]

        if html:
            candidate_links.extend(self._extract_candidate_links(html, final_url or vendor.website, allowed_domains))

        if not self._has_strong_candidate(candidate_links):
            rendered_links = self._fetch_rendered_links(final_url or vendor.website)
            candidate_links.extend(
                self._extract_candidate_links_from_rendered(rendered_links, final_url or vendor.website, allowed_domains)
            )

        best = self._pick_best_candidate(candidate_links, vendor.website)
        self._notify(
            "vendor_resolved",
            vendor=vendor.name,
            url=best["career_page"],
            career_page=best["career_page"],
            message=f"Resolved career page: {best['career_page']}",
        )
        return best

    def _fetch_html(self, url: str) -> tuple[str, str]:
        try:
            response = self.session.get(url, timeout=15)
            if response.status_code >= 400:
                return "", ensure_url(url)
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return "", response.url
            return response.text, response.url
        except requests.RequestException:
            return "", ensure_url(url)

    def _extract_candidate_links(self, html: str, base_url: str, allowed_domains: List[str]) -> List[Dict[str, object]]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: List[Dict[str, object]] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"].strip())
            host = normalize_domain(href)
            if not (self._matches_allowed_domains(host, allowed_domains) or is_known_ats_host(host)):
                continue
            text = anchor.get_text(" ", strip=True)
            score = self._score_candidate(href, text)
            if score > 0:
                candidates.append({"url": href, "text": text, "score": score})
        return candidates

    def _extract_candidate_links_from_rendered(
        self,
        links: List[Dict[str, str]],
        base_url: str,
        allowed_domains: List[str],
    ) -> List[Dict[str, object]]:
        candidates: List[Dict[str, object]] = []
        for link in links:
            href = urljoin(base_url, (link.get("href") or "").strip())
            host = normalize_domain(href)
            if not href or not (self._matches_allowed_domains(host, allowed_domains) or is_known_ats_host(host)):
                continue
            text = link.get("text", "")
            score = self._score_candidate(href, text)
            if score > 0:
                candidates.append({"url": href, "text": text, "score": score})
        return candidates

    def _pick_best_candidate(self, candidates: List[Dict[str, object]], fallback_url: str) -> Dict[str, object]:
        deduped: Dict[str, Dict[str, object]] = {}
        for candidate in candidates:
            url = ensure_url(str(candidate["url"]))
            score = int(candidate["score"])
            current = deduped.get(url)
            if current is None or score > int(current["score"]):
                deduped[url] = {"url": url, "text": str(candidate.get("text", "")), "score": score}

        if not deduped:
            return {
                "career_page": ensure_url(fallback_url),
                "score": 0,
                "method": "fallback",
            }

        ranked = sorted(deduped.values(), key=lambda item: (item["score"], len(item["url"])), reverse=True)
        for candidate in ranked[:3]:
            candidate["score"] = int(candidate["score"]) + self._candidate_page_bonus(candidate["url"])

        best = max(ranked, key=lambda item: (int(item["score"]), len(item["url"])))
        if int(best["score"]) <= 0:
            return {
                "career_page": ensure_url(fallback_url),
                "score": int(best["score"]),
                "method": "fallback",
            }
        return {
            "career_page": best["url"],
            "score": int(best["score"]),
            "method": "resolved",
            "link_text": best["text"],
        }

    def _score_candidate(self, url: str, text: str) -> int:
        normalized_url = normalize_text(url)
        normalized_text_value = normalize_text(text)
        combined = f"{normalized_text_value} {normalized_url}"
        path_segments = [segment for segment in urlparse(url).path.split("/") if segment]
        slug_words = [word for word in (path_segments[-1].split("-") if path_segments else []) if word]
        score = 0

        if any(hint in combined for hint in CAREER_HINTS):
            score += 80
        if any(fragment in url.lower() for fragment in ENTRY_PATH_HINTS):
            score += 50
        if is_known_ats_host(url):
            score += 70
        if any(hint in combined for hint in BLOCKED_HINTS):
            score -= 120
        if any(fragment in url.lower() for fragment in REJECT_URL_HINTS):
            score -= 200
        if any(fragment in url.lower() for fragment in JOB_DETAIL_HINTS):
            score -= 45
        if normalized_text_value in {"apply", "apply now"}:
            score -= 40
        if len(normalized_text_value.split()) > 8 and not any(hint in normalized_text_value for hint in ACTION_ENTRY_HINTS):
            score -= 120
        if len(path_segments) >= 2 and len(normalized_text_value.split()) > 6:
            score -= 80
        if len(slug_words) > 4 and not any(hint in normalized_url for hint in ACTION_ENTRY_HINTS):
            score -= 160
        if text.strip().endswith("?") or normalized_text_value.startswith(("how ", "what ", "why ", "is the ", "5 steps ")):
            score -= 160

        return score

    def _candidate_page_bonus(self, url: str) -> int:
        html, final_url = self._fetch_html(url)
        if not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        headings = normalize_text(" ".join(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"])[:3]))
        text = normalize_text(soup.get_text(" ", strip=True)[:20000])
        combined = f"{title} {headings} {text} {normalize_text(final_url)}"
        bonus = 0

        if any(hint in combined for hint in CAREER_HINTS):
            bonus += 35
        if any(hint in combined for hint in ("search jobs", "job search", "search results", "job results", "current openings", "open positions", "view jobs")):
            bonus += 35
        if any(fragment in final_url.lower() for fragment in ENTRY_PATH_HINTS):
            bonus += 20
        if is_known_ats_host(final_url) and any(hint in combined for hint in ("job", "career", "search results")):
            bonus += 25
        if any(hint in combined for hint in BLOCKED_HINTS):
            bonus -= 120
        if any(fragment in final_url.lower() for fragment in REJECT_URL_HINTS):
            bonus -= 200
        if any(fragment in final_url.lower() for fragment in JOB_DETAIL_HINTS):
            bonus -= 50
        if self._looks_like_single_job_detail(combined):
            bonus -= 45

        return bonus

    def _looks_like_single_job_detail(self, combined: str) -> bool:
        role_hits = sum(1 for hint in ROLE_HINTS if hint in combined)
        return "apply now" in combined and "job id" in combined and role_hits >= 2 and "search jobs" not in combined

    def _has_strong_candidate(self, candidates: List[Dict[str, object]]) -> bool:
        for candidate in candidates:
            url = str(candidate.get("url", ""))
            score = int(candidate.get("score", 0) or 0)
            lowered = url.lower()
            if score >= 120 and not any(fragment in lowered for fragment in JOB_DETAIL_HINTS):
                return True
        return False

    def _allowed_domains(self, *urls: str) -> List[str]:
        allowed: List[str] = []
        for url in urls:
            domain = normalize_domain(url)
            if domain and domain not in allowed:
                allowed.append(domain)
        return allowed

    def _matches_allowed_domains(self, host: str, allowed_domains: List[str]) -> bool:
        return any(same_or_parent_domain(host, domain) for domain in allowed_domains)

    def _fetch_rendered_links(self, url: str) -> List[Dict[str, str]]:
        if self._context is None:
            return []
        page = self._context.new_page()
        page.set_default_timeout(min(self.settings.timeout_ms, 20000))
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightError:
                pass
            return page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]')).map((anchor) => ({
                  text: (anchor.innerText || anchor.textContent || '').replace(/\\s+/g, ' ').trim(),
                  href: anchor.href || '',
                })).filter((item) => item.href).slice(0, 400)
                """
            )
        except PlaywrightError:
            return []
        finally:
            page.close()

    def _start_renderer(self) -> None:
        if self._playwright is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.settings.headless)
        self._context = self._browser.new_context(viewport={"width": 1440, "height": 1200})

    def _stop_renderer(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _notify(self, event: str, **payload: object) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, **payload)
        except Exception:
            return
