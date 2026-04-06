import json
import re
from datetime import timedelta
from html import unescape
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .browser import open_browser_session
from .config import Settings
from .models import JobRecord, Vendor
from .utils import (
    as_utc_iso,
    compact_text,
    ensure_url,
    is_known_ats_host,
    normalize_domain,
    normalize_text,
    parse_datetime,
    same_or_parent_domain,
    url_slug,
    utc_now,
    within_last_hours,
)


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

GENERIC_PAGE_TITLES = {
    "careers",
    "consultant careers",
    "job openings",
    "job search",
    "jobs",
    "open positions",
    "search results",
    "site",
}

JOB_LINK_HINTS = [
    "apply",
    "career",
    "job",
    "opening",
    "opportunit",
    "position",
    "requisition",
    "role",
    "search",
]

LISTING_PAGE_HINTS = [
    "find a job",
    "find work",
    "job results",
    "job search",
    "search jobs",
    "search results",
]

JOB_DETAIL_HINTS = [
    "/details/",
    "/job/",
    "/job-details/",
    "#/detail/",
]

ROLE_TITLE_HINTS = [
    "ai",
    "analyst",
    "architect",
    "consultant",
    "data",
    "developer",
    "devops",
    "engineer",
    "ml",
    "scientist",
    "software",
]

TECH_TITLE_HINTS = [
    "ai",
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
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def detect_provider(url: str) -> str:
    host = normalize_domain(url)
    if "indeed.com" in host:
        return "indeed"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "workdayjobs.com" in host:
        return "workday"
    return "generic"


class JobDiscoveryService:
    def __init__(
        self,
        settings: Settings,
        vendors: List[Vendor],
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        self.settings = settings
        self.vendors = vendors
        self.vendor_names = {normalize_text(vendor.name): vendor for vendor in vendors}
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self._rendered_html_cache: Dict[str, str] = {}
        self._rendered_links_cache: Dict[str, List[Dict[str, str]]] = {}
        self._playwright = None
        self._browser_session = None
        self._render_context = None
        self.progress_callback = progress_callback

    def load_manual_urls(self) -> List[str]:
        if not self.settings.job_urls_path.exists():
            return []

        urls: List[str] = []
        for line in self.settings.job_urls_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            urls.append(ensure_url(stripped))
        return urls

    def discover(self, vendor_limit: int = 25) -> List[JobRecord]:
        job_map: Dict[str, JobRecord] = {}
        selected_vendors = self.vendors[: max(vendor_limit, 0)]
        self._notify("scan_started", message=f"Scanning {len(selected_vendors)} vendor websites")
        self._start_renderer()
        try:
            for url in self.load_manual_urls():
                self._notify("vendor_url", vendor="Manual URLs", url=url, message=f"Inspecting manual URL {url}")
                job = self.inspect_job_url(url, discovered_from="manual")
                if job:
                    job_map[job.job_id] = job

            for vendor in selected_vendors:
                self._notify("vendor_started", vendor=vendor.name, url=vendor.website)
                for job in self.scan_vendor(vendor):
                    job_map[job.job_id] = job
                vendor_jobs = [job for job in job_map.values() if job.discovered_from == vendor.name]
                self._notify(
                    "vendor_done",
                    vendor=vendor.name,
                    jobs_found=len(vendor_jobs),
                    eligible_found=len([job for job in vendor_jobs if job.eligible]),
                    message=f"Finished {vendor.name}",
                )
        finally:
            self._stop_renderer()

        jobs = list(job_map.values())
        jobs.sort(key=lambda item: (item.eligible, item.posted_at, item.company, item.title), reverse=True)
        return jobs

    def scan_vendor(self, vendor: Vendor) -> List[JobRecord]:
        started_renderer = False
        if self._playwright is None:
            self._start_renderer()
            started_renderer = True

        discovered: Dict[str, JobRecord] = {}
        listing_queue = [vendor.website]
        seen_listing = set()
        inspected_jobs = set()

        while listing_queue and len(seen_listing) < 6 and len(discovered) < 12:
            listing_url = listing_queue.pop(0)
            if listing_url in seen_listing:
                continue
            seen_listing.add(listing_url)
            self._notify("vendor_url", vendor=vendor.name, url=listing_url, message=f"Inspecting {listing_url}")

            listing_html = self._fetch_html(listing_url)
            if listing_html and self._looks_like_job_page(listing_html, listing_url):
                job = self.inspect_job_url(listing_url, discovered_from=vendor.name)
                if job:
                    discovered[job.job_id] = job

            next_listing_urls: List[str] = []
            job_urls: List[str] = []

            if listing_html:
                next_listing_urls.extend(self._extract_listing_urls(listing_html, listing_url, vendor.domain))
                job_urls.extend(self._extract_job_urls(listing_html, listing_url, vendor.domain))

            rendered_links = self._fetch_rendered_links(listing_url)
            next_listing_urls.extend(self._extract_listing_urls_from_links(rendered_links, listing_url, vendor.domain))
            job_urls.extend(self._extract_job_urls_from_links(rendered_links, listing_url, vendor.domain))

            for next_url in self._dedupe_urls(next_listing_urls):
                if next_url not in seen_listing:
                    listing_queue.append(next_url)

            for job_url in self._dedupe_urls(job_urls):
                if job_url in inspected_jobs:
                    continue
                inspected_jobs.add(job_url)
                self._notify("vendor_url", vendor=vendor.name, url=job_url, message=f"Checking job page {job_url}")
                job = self.inspect_job_url(job_url, discovered_from=vendor.name)
                if job:
                    discovered[job.job_id] = job

        if started_renderer:
            self._stop_renderer()

        return list(discovered.values())

    def inspect_job_url(self, url: str, discovered_from: str) -> Optional[JobRecord]:
        html = self._fetch_html(url)
        if not html:
            html = self._fetch_rendered_html(url)
            if not html:
                return None

        soup = BeautifulSoup(html, "html.parser")
        metadata = self._extract_metadata(soup, url)
        if self._needs_rendered_html(url, metadata):
            rendered_html = self._fetch_rendered_html(url)
            if rendered_html:
                soup = BeautifulSoup(rendered_html, "html.parser")
                metadata = self._extract_metadata(soup, url)

        posted_at = self._extract_posted_at(metadata, soup)
        title = metadata.get("title") or ""
        description = metadata.get("description") or ""
        probable_detail = self._is_probable_job_detail(title, url, metadata.get("apply_url", ""))
        ai_ml_match, matched_keywords = self._is_ai_ml_job(title, description)
        if not probable_detail:
            ai_ml_match = False
            matched_keywords = []
        company = metadata.get("company") or discovered_from
        apply_url = metadata.get("apply_url") or url
        trusted = self._is_trusted(url, company, discovered_from)
        provider = detect_provider(apply_url or url)

        return JobRecord(
            job_id=url_slug(apply_url or url),
            source_url=url,
            discovered_from=discovered_from,
            company=company,
            title=title,
            location=metadata.get("location", ""),
            posted_at=as_utc_iso(posted_at),
            description=description,
            provider=provider,
            apply_url=apply_url,
            trusted=trusted,
            ai_ml_match=ai_ml_match,
            recency_ok=within_last_hours(posted_at, self.settings.recency_hours),
            matched_keywords=matched_keywords,
            status="pending",
            reason=self._default_reason(trusted, ai_ml_match, posted_at, probable_detail),
        )

    def _fetch_html(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=15)
            if response.status_code >= 400:
                return ""
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return ""
            return response.text
        except requests.RequestException:
            return ""

    def _extract_listing_urls(self, html: str, base_url: str, vendor_domain: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: List[str] = []

        for anchor in soup.find_all("a", href=True):
            text = normalize_text(anchor.get_text(" ", strip=True))
            href = urljoin(base_url, anchor["href"].strip())
            host = normalize_domain(href)
            combined = f"{text} {normalize_text(href)}"
            if not (same_or_parent_domain(host, vendor_domain) or is_known_ats_host(host)):
                continue
            if any(hint in combined for hint in JOB_LINK_HINTS):
                urls.append(href)

        return self._dedupe_urls(urls)

    def _extract_job_urls(self, html: str, base_url: str, vendor_domain: str) -> List[str]:
        urls: List[str] = []
        soup = BeautifulSoup(html, "html.parser")

        for job_posting in self._extract_json_ld_job_postings(soup):
            posting_url = ensure_url(job_posting.get("url", ""))
            if posting_url:
                urls.append(urljoin(base_url, posting_url))

        for anchor in soup.find_all("a", href=True):
            text = normalize_text(anchor.get_text(" ", strip=True))
            href = urljoin(base_url, anchor["href"].strip())
            host = normalize_domain(href)
            combined = f"{text} {normalize_text(href)}"
            if not (same_or_parent_domain(host, vendor_domain) or is_known_ats_host(host)):
                continue
            if any(keyword in combined for keyword in AI_ML_KEYWORDS) and any(hint in combined for hint in JOB_LINK_HINTS):
                urls.append(href)
            elif is_known_ats_host(host) and any(keyword in combined for keyword in AI_ML_KEYWORDS):
                urls.append(href)

        return self._dedupe_urls(urls)

    def _extract_listing_urls_from_links(
        self,
        links: List[Dict[str, str]],
        base_url: str,
        vendor_domain: str,
    ) -> List[str]:
        urls: List[str] = []
        for link in links:
            href = urljoin(base_url, (link.get("href") or "").strip())
            text = normalize_text(link.get("text", ""))
            combined = f"{text} {normalize_text(href)}"
            host = normalize_domain(href)
            if not href or not (same_or_parent_domain(host, vendor_domain) or is_known_ats_host(host)):
                continue
            if any(hint in combined for hint in LISTING_PAGE_HINTS + JOB_LINK_HINTS):
                urls.append(href)
        return self._dedupe_urls(urls)

    def _extract_job_urls_from_links(
        self,
        links: List[Dict[str, str]],
        base_url: str,
        vendor_domain: str,
    ) -> List[str]:
        urls: List[str] = []
        for link in links:
            raw_href = (link.get("href") or "").strip()
            href = urljoin(base_url, raw_href)
            text = link.get("text", "")
            normalized_text_value = normalize_text(text)
            host = normalize_domain(href)
            if not href or not (same_or_parent_domain(host, vendor_domain) or is_known_ats_host(host)):
                continue
            if any(fragment in href for fragment in JOB_DETAIL_HINTS):
                urls.append(href)
                continue
            if self._looks_like_role_title(normalized_text_value):
                urls.append(href)
        return self._dedupe_urls(urls)

    def _extract_metadata(self, soup: BeautifulSoup, url: str) -> Dict[str, str]:
        json_ld_job = self._best_json_ld_job(soup)
        metadata = {
            "title": "",
            "company": "",
            "location": "",
            "description": "",
            "date_posted": "",
            "apply_url": "",
        }

        if json_ld_job:
            metadata["title"] = json_ld_job.get("title", "")
            hiring_org = json_ld_job.get("hiringOrganization", {})
            if isinstance(hiring_org, dict):
                metadata["company"] = hiring_org.get("name", "")
            location = json_ld_job.get("jobLocation", {})
            if isinstance(location, list) and location:
                location = location[0]
            if isinstance(location, dict):
                address = location.get("address", {})
                if isinstance(address, dict):
                    pieces = [address.get("addressLocality", ""), address.get("addressRegion", "")]
                    metadata["location"] = ", ".join(piece for piece in pieces if piece)
            metadata["description"] = compact_text(unescape(re.sub(r"<[^>]+>", " ", json_ld_job.get("description", ""))))
            metadata["date_posted"] = json_ld_job.get("datePosted", "")
            metadata["apply_url"] = ensure_url(json_ld_job.get("url", ""))

        title_tag = soup.find(["h1", "h2"])
        if title_tag and not metadata["title"]:
            metadata["title"] = compact_text(title_tag.get_text(" ", strip=True), limit=200)

        if not metadata["title"] and soup.title:
            metadata["title"] = compact_text(soup.title.get_text(" ", strip=True), limit=200)

        if not metadata["company"]:
            og_site_name = soup.find("meta", attrs={"property": "og:site_name"})
            if og_site_name and og_site_name.get("content"):
                metadata["company"] = og_site_name["content"].strip()

        if not metadata["description"]:
            main_node = soup.find("main") or soup.find("body")
            if main_node:
                metadata["description"] = compact_text(main_node.get_text(" ", strip=True))

        if not metadata["apply_url"]:
            apply_link = soup.find("a", href=True, string=re.compile(r"apply", re.I))
            if apply_link:
                metadata["apply_url"] = urljoin(url, apply_link["href"].strip())

        if not metadata["apply_url"]:
            metadata["apply_url"] = url

        return metadata

    def _extract_posted_at(self, metadata: Dict[str, str], soup: BeautifulSoup):
        raw_candidates = [metadata.get("date_posted", "")]

        for tag in soup.find_all(["time", "span", "div", "p"]):
            text = tag.get_text(" ", strip=True)
            if re.search(r"posted|hours? ago|days? ago|today|yesterday", text, re.I):
                raw_candidates.append(text)

        for candidate in raw_candidates:
            parsed = self._parse_date_text(candidate)
            if parsed:
                return parsed

        return None

    def _parse_date_text(self, text: str):
        raw = compact_text(text, limit=200)
        direct = parse_datetime(raw)
        if direct:
            return direct

        lowered = raw.lower()
        now = utc_now()

        hours_match = re.search(r"(\d+)\s+hours?\s+ago", lowered)
        if hours_match:
            return now - timedelta(hours=int(hours_match.group(1)))

        minutes_match = re.search(r"(\d+)\s+minutes?\s+ago", lowered)
        if minutes_match:
            return now - timedelta(minutes=int(minutes_match.group(1)))

        days_match = re.search(r"(\d+)\s+days?\s+ago", lowered)
        if days_match:
            return now - timedelta(days=int(days_match.group(1)))

        if "today" in lowered or "just posted" in lowered or "less than 24 hours" in lowered:
            return now
        if "yesterday" in lowered:
            return now - timedelta(days=1)

        date_pattern = re.search(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},\s+\d{4}",
            lowered,
        )
        if date_pattern:
            return parse_datetime(date_pattern.group(0))

        slash_pattern = re.search(r"\d{1,2}/\d{1,2}/\d{4}", raw)
        if slash_pattern:
            return parse_datetime(slash_pattern.group(0))

        return None

    def _best_json_ld_job(self, soup: BeautifulSoup) -> Dict[str, str]:
        postings = self._extract_json_ld_job_postings(soup)
        return postings[0] if postings else {}

    def _extract_json_ld_job_postings(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        postings: List[Dict[str, str]] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            postings.extend(self._collect_job_postings(payload))
        return postings

    def _collect_job_postings(self, payload) -> List[Dict[str, str]]:
        found: List[Dict[str, str]] = []
        if isinstance(payload, list):
            for item in payload:
                found.extend(self._collect_job_postings(item))
            return found
        if isinstance(payload, dict):
            if payload.get("@type") == "JobPosting":
                found.append(payload)
            for value in payload.values():
                found.extend(self._collect_job_postings(value))
        return found

    def _looks_like_job_page(self, html: str, url: str) -> bool:
        lowered = normalize_text(html[:15000] + " " + url)
        return any(keyword in lowered for keyword in AI_ML_KEYWORDS) and any(
            hint in lowered for hint in ("apply", "job", "position", "role")
        )

    def _is_ai_ml_job(self, title: str, description: str) -> Tuple[bool, List[str]]:
        normalized_title = normalize_text(title)
        normalized_description = normalize_text(description)
        title_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_title]
        if normalized_title in GENERIC_PAGE_TITLES and not any(keyword in normalized_title for keyword in AI_ML_KEYWORDS):
            return False, []
        if title_matches:
            return True, title_matches
        if not any(hint in normalized_title for hint in TECH_TITLE_HINTS):
            return False, []
        description_matches = [keyword for keyword in AI_ML_KEYWORDS if keyword in normalized_description]
        return bool(description_matches), description_matches

    def _is_trusted(self, source_url: str, company: str, discovered_from: str) -> bool:
        # The user-provided workbook and pasted URLs are treated as trusted sources.
        return True

    def _default_reason(self, trusted: bool, ai_ml_match: bool, posted_at, probable_detail: bool) -> str:
        if not probable_detail:
            return "Search/listing page"
        if not ai_ml_match:
            return "Not AI/ML focused"
        if posted_at is None:
            return "Missing posted date"
        if not within_last_hours(posted_at, self.settings.recency_hours):
            return "Older than 24 hours"
        return ""

    def _dedupe_urls(self, urls: Iterable[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for url in urls:
            normalized = ensure_url(url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _looks_like_role_title(self, text: str) -> bool:
        if not text or text in GENERIC_PAGE_TITLES:
            return False
        if any(text == hint for hint in LISTING_PAGE_HINTS):
            return False
        words = text.split()
        if len(words) < 2 or len(words) > 12:
            return False
        if not re.search(r"[a-z]", text):
            return False
        return any(hint in text for hint in ROLE_TITLE_HINTS)

    def _needs_rendered_html(self, url: str, metadata: Dict[str, str]) -> bool:
        title = normalize_text(metadata.get("title", ""))
        company = normalize_text(metadata.get("company", ""))
        description = normalize_text(metadata.get("description", ""))
        return (
            "#/detail/" in url
            or title in GENERIC_PAGE_TITLES
            or not title
            or title == company
            or len(title.split()) <= 1
            or len(description) < 120
        )

    def _is_probable_job_detail(self, title: str, source_url: str, apply_url: str) -> bool:
        normalized_title = normalize_text(title)
        if self._looks_like_role_title(normalized_title):
            return True
        if any(fragment in source_url for fragment in JOB_DETAIL_HINTS) or any(fragment in apply_url for fragment in JOB_DETAIL_HINTS):
            return True
        path = urlparse(source_url).path.lower()
        if re.search(r"/job[s]?/[^/?#]+", path) and not path.endswith("/jobs/"):
            return True
        if re.search(r"/details/[^/?#]+", path):
            return True
        return False

    def _start_renderer(self) -> None:
        if self._playwright is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser_session = open_browser_session(self._playwright, self.settings)
        self._render_context = self._browser_session.context

    def _stop_renderer(self) -> None:
        self._render_context = None
        if self._browser_session is not None:
            self._browser_session.close()
            self._browser_session = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _fetch_rendered_html(self, url: str) -> str:
        if url in self._rendered_html_cache:
            return self._rendered_html_cache[url]
        if self._render_context is None:
            return ""
        page = self._render_context.new_page()
        page.set_default_timeout(min(self.settings.timeout_ms, 20000))
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightError:
                pass
            html = page.content()
            self._rendered_html_cache[url] = html
            return html
        except PlaywrightError:
            return ""
        finally:
            page.close()

    def _fetch_rendered_links(self, url: str) -> List[Dict[str, str]]:
        if url in self._rendered_links_cache:
            return self._rendered_links_cache[url]
        if self._render_context is None:
            return []
        page = self._render_context.new_page()
        page.set_default_timeout(min(self.settings.timeout_ms, 20000))
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightError:
                pass
            links = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]')).map((anchor) => ({
                  text: (anchor.innerText || anchor.textContent || '').replace(/\\s+/g, ' ').trim(),
                  href: anchor.href || '',
                })).filter((item) => item.href).slice(0, 400)
                """
            )
            self._rendered_links_cache[url] = links
            return links
        except PlaywrightError:
            return []
        finally:
            page.close()

    def _notify(self, event: str, **payload: object) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, **payload)
        except Exception:
            return
