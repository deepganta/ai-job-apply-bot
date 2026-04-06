from pathlib import Path
from threading import Thread
from typing import List

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

from .application import JobApplicationService
from .career_pages import CareerPageResolver, apply_career_page_cache, load_career_page_cache
from .config import Settings
from .indeed import IndeedDiscoveryService, build_indeed_search_url
from .linkedin import LinkedInDiscoveryService, build_linkedin_search_url
from .job_discovery import JobDiscoveryService
from .models import DashboardState, JobRecord, Vendor
from .reporting import write_summary
from .scan_progress import initialize as initialize_scan
from .scan_progress import is_running as scan_is_running
from .scan_progress import snapshot as scan_snapshot
from .scan_progress import update as update_scan
from .state import load_state, replace_job, save_state
from .utils import prettify_timestamp, utc_now
from .vendor_workbook import load_vendors


def create_app() -> Flask:
    settings = Settings.from_env()
    template_folder = settings.root_dir / "job_apply_bot" / "templates"
    app = Flask(__name__, template_folder=str(template_folder))

    @app.get("/")
    def index():
        current_settings = Settings.from_env()
        state = load_state(current_settings.state_path)
        vendors = _safe_load_vendors(current_settings)
        career_page_cache = load_career_page_cache(current_settings)
        career_pages = _career_page_rows(vendors, career_page_cache)
        indeed_search = current_settings.load_indeed_search()
        manual_urls = _read_manual_urls(current_settings)
        critical_missing = _critical_missing_fields(current_settings)
        submitted_jobs = [job for job in state.jobs if job.status == "submitted"]
        current_scan = scan_snapshot()

        return render_template(
            "index.html",
            state=state,
            vendors_loaded=len(vendors),
            career_pages=career_pages,
            resolved_career_pages=len([row for row in career_pages if row["career_page"]]),
            indeed_search=indeed_search,
            manual_urls=manual_urls,
            critical_missing=critical_missing,
            submitted_jobs=submitted_jobs,
            scan_status=current_scan,
            prettify_timestamp=prettify_timestamp,
        )

    @app.post("/scan")
    def scan():
        current_settings = Settings.from_env()
        _write_manual_urls(current_settings, request.form.get("manual_urls", ""))
        vendors = _safe_load_vendors(current_settings)
        vendor_limit = int(request.form.get("vendor_limit", "25") or "25")
        selected_vendors = vendors[: max(vendor_limit, 0)]
        if not scan_is_running():
            initialize_scan(selected_vendors)
            worker = Thread(target=_run_scan_task, args=(current_settings, vendor_limit), daemon=True)
            worker.start()
        return redirect(url_for("index"))

    @app.post("/scan-indeed")
    def scan_indeed():
        current_settings = Settings.from_env()
        _write_indeed_search(
            current_settings,
            {
                "query": request.form.get("indeed_query", ""),
                "location": request.form.get("indeed_location", ""),
                "max_pages": request.form.get("indeed_max_pages", "2"),
                "max_jobs": request.form.get("indeed_max_jobs", "20"),
                "easy_apply_only": request.form.get("indeed_easy_apply_only", "true") != "false",
            },
        )

        if not scan_is_running():
            search = current_settings.load_indeed_search()
            initialize_scan([_indeed_progress_target(current_settings, search)])
            worker = Thread(target=_run_indeed_scan_task, args=(current_settings,), daemon=True)
            worker.start()
        return redirect(url_for("index"))

    @app.post("/scan-linkedin")
    def scan_linkedin():
        current_settings = Settings.from_env()
        _write_linkedin_search(
            current_settings,
            {
                "query": request.form.get("linkedin_query", ""),
                "location": request.form.get("linkedin_location", ""),
                "max_pages": request.form.get("linkedin_max_pages", "2"),
                "max_jobs": request.form.get("linkedin_max_jobs", "25"),
                "easy_apply_only": request.form.get("linkedin_easy_apply_only", "true") != "false",
                "contract_only": request.form.get("linkedin_contract_only", "true") != "false",
            },
        )

        if not scan_is_running():
            search = current_settings.load_linkedin_search()
            initialize_scan([_linkedin_progress_target(current_settings, search)])
            worker = Thread(target=_run_linkedin_scan_task, args=(current_settings,), daemon=True)
            worker.start()
        return redirect(url_for("index"))

    @app.get("/scan-status")
    def scan_status():
        return jsonify(scan_snapshot())

    @app.post("/apply/<job_id>")
    def apply_single(job_id: str):
        current_settings = Settings.from_env()
        state = load_state(current_settings.state_path)
        submit_mode = request.form.get("submit_mode", current_settings.submit_mode)

        target = next((job for job in state.jobs if job.job_id == job_id), None)
        if target and target.eligible and target.status != "submitted":
            service = JobApplicationService(
                current_settings,
                current_settings.load_profile(),
                current_settings.load_question_answers(),
            )
            updated_job = service.apply_jobs([target], submit_mode=submit_mode)[0]
            state.jobs = replace_job(state.jobs, updated_job)
            save_state(current_settings.state_path, state)
            write_summary(current_settings, state.jobs)

        return redirect(url_for("index"))

    @app.post("/apply-eligible")
    def apply_eligible():
        current_settings = Settings.from_env()
        state = load_state(current_settings.state_path)
        submit_mode = request.form.get("submit_mode", current_settings.submit_mode)
        targets = [job for job in state.jobs if job.eligible and job.status not in {"submitted", "review_required"}]

        if targets:
            service = JobApplicationService(
                current_settings,
                current_settings.load_profile(),
                current_settings.load_question_answers(),
            )
            updated_jobs = service.apply_jobs(targets, submit_mode=submit_mode)
            for updated_job in updated_jobs:
                state.jobs = replace_job(state.jobs, updated_job)
            save_state(current_settings.state_path, state)
            write_summary(current_settings, state.jobs)

        return redirect(url_for("index"))

    @app.get("/artifacts/<path:filename>")
    def artifacts(filename: str):
        current_settings = Settings.from_env()
        return send_from_directory(current_settings.output_dir, filename)

    @app.post("/clear-results")
    def clear_results():
        if scan_is_running():
            return redirect(url_for("index"))
        current_settings = Settings.from_env()
        _clear_previous_results(current_settings)
        update_scan("scan_cleared")
        return redirect(url_for("index"))

    return app


def _safe_load_vendors(settings: Settings):
    if not settings.vendor_workbook_path.exists():
        return []
    return load_vendors(settings.vendor_workbook_path)


def _read_manual_urls(settings: Settings) -> str:
    if not settings.job_urls_path.exists():
        return ""
    return settings.job_urls_path.read_text(encoding="utf-8")


def _write_manual_urls(settings: Settings, content: str) -> None:
    settings.job_urls_path.parent.mkdir(parents=True, exist_ok=True)
    settings.job_urls_path.write_text(content.strip() + "\n", encoding="utf-8")


def _write_indeed_search(settings: Settings, payload: dict) -> None:
    settings.save_indeed_search(payload)


def _critical_missing_fields(settings: Settings) -> List[str]:
    profile = settings.load_profile()
    missing = []
    for key, label in (
        ("location", "Location"),
        ("work_authorization", "U.S. work authorization"),
        ("requires_visa_sponsorship", "Visa sponsorship"),
        ("linkedin_url", "LinkedIn URL"),
    ):
        if not profile.get(key):
            missing.append(label)
    return missing


def _run_scan_task(settings: Settings, vendor_limit: int) -> None:
    try:
        vendors = _safe_load_vendors(settings)
        selected_vendors = vendors[: max(vendor_limit, 0)]
        resolver = CareerPageResolver(settings, progress_callback=update_scan)
        cache = resolver.resolve_all(selected_vendors)
        scan_vendors = []
        for vendor in selected_vendors:
            cached = cache.get(vendor.domain, {})
            career_page = str(cached.get("career_page", "") or "")
            method = str(cached.get("method", "") or "")
            score = int(cached.get("score", 0) or 0)
            if career_page:
                update_scan(
                    "vendor_resolved",
                    vendor=vendor.name,
                    url=career_page,
                    career_page=career_page,
                    message=f"Resolved career page: {career_page}",
                )
            if method in {"resolved", "override"} and score > 0:
                scan_vendors.append(vendor)
            else:
                update_scan(
                    "vendor_skipped",
                    vendor=vendor.name,
                    url=career_page or vendor.website,
                    message="Skipped vendor: no confident career page",
                )
        discovery = JobDiscoveryService(settings, apply_career_page_cache(scan_vendors, cache), progress_callback=update_scan)
        jobs = discovery.discover(vendor_limit=vendor_limit)
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=jobs,
        )
        save_state(settings.state_path, state)
        write_summary(settings, state.jobs)
        update_scan(
            "scan_finished",
            message=f"Scan finished: {len(jobs)} jobs collected from {len(scan_vendors)} vendors",
        )
    except Exception as exc:
        update_scan("scan_error", message=f"Scan failed: {exc}")


def _run_indeed_scan_task(settings: Settings) -> None:
    try:
        vendors = _safe_load_vendors(settings)
        search = settings.load_indeed_search()
        discovery = IndeedDiscoveryService(settings, progress_callback=update_scan)
        jobs = discovery.discover(
            query=str(search.get("query", "") or ""),
            location=str(search.get("location", "") or ""),
            max_pages=int(search.get("max_pages", 2) or 2),
            max_jobs=int(search.get("max_jobs", 20) or 20),
            easy_apply_only=bool(search.get("easy_apply_only", True)),
        )
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=jobs,
        )
        save_state(settings.state_path, state)
        write_summary(settings, state.jobs)
        update_scan(
            "scan_finished",
            message=f"Indeed scan finished: {len(jobs)} jobs collected",
        )
    except Exception as exc:
        update_scan("scan_error", message=f"Indeed scan failed: {exc}")


def _clear_previous_results(settings: Settings) -> None:
    vendors = _safe_load_vendors(settings)
    empty_state = DashboardState(vendors_loaded=len(vendors), last_scan_at="", jobs=[])
    save_state(settings.state_path, empty_state)
    write_summary(settings, [])

    for path in settings.output_dir.iterdir():
        if path.is_dir():
            continue
        if path.name in {"dashboard_state.json", "latest_results.json", "latest_results.md"}:
            continue
        if path.suffix.lower() in {".png", ".html"}:
            path.unlink(missing_ok=True)


def _write_linkedin_search(settings: Settings, payload: dict) -> None:
    settings.save_linkedin_search(payload)


def _run_linkedin_scan_task(settings: Settings) -> None:
    try:
        vendors = _safe_load_vendors(settings)
        search = settings.load_linkedin_search()
        discovery = LinkedInDiscoveryService(settings, progress_callback=update_scan)
        jobs = discovery.discover(
            query=str(search.get("query", "") or ""),
            location=str(search.get("location", "") or ""),
            max_pages=int(search.get("max_pages", 2) or 2),
            max_jobs=int(search.get("max_jobs", 25) or 25),
            easy_apply_only=bool(search.get("easy_apply_only", True)),
            contract_only=bool(search.get("contract_only", True)),
        )
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=jobs,
        )
        save_state(settings.state_path, state)
        write_summary(settings, state.jobs)
        update_scan(
            "scan_finished",
            message=f"LinkedIn scan finished: {len(jobs)} jobs collected",
        )
    except Exception as exc:
        update_scan("scan_error", message=f"LinkedIn scan failed: {exc}")


def _linkedin_progress_target(settings: Settings, search: dict) -> Vendor:
    query = str(search.get("query", "") or "AI ML Engineer")
    location = str(search.get("location", "") or "United States")
    label = f"LinkedIn: {query}" if not location else f"LinkedIn: {query} | {location}"
    url = build_linkedin_search_url(
        query=query,
        location=location,
        recency_hours=int(search.get("recency_hours", 168) or 168),
        easy_apply_only=bool(search.get("easy_apply_only", True)),
        contract_only=bool(search.get("contract_only", True)),
    )
    return Vendor(name=label, website=url, domain="linkedin.com")


def _indeed_progress_target(settings: Settings, search: dict) -> Vendor:
    query = str(search.get("query", "") or "AI Engineer")
    location = str(search.get("location", "") or "")
    label = f"Indeed: {query}" if not location else f"Indeed: {query} | {location}"
    url = build_indeed_search_url(query=query, location=location, recency_hours=settings.recency_hours)
    return Vendor(name=label, website=url, domain="indeed.com")


def _career_page_rows(vendors, cache):
    rows = []
    for vendor in vendors:
        cached = cache.get(vendor.domain, {})
        rows.append(
            {
                "name": vendor.name,
                "source_website": vendor.website,
                "career_page": str(cached.get("career_page", "") or ""),
                "method": str(cached.get("method", "") or ""),
                "score": int(cached.get("score", 0) or 0),
                "link_text": str(cached.get("link_text", "") or ""),
                "aliases": ", ".join(vendor.aliases),
            }
        )
    return rows
