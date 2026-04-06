import argparse

from . import __version__
from .application import JobApplicationService
from .config import Settings
from .indeed import IndeedDiscoveryService
from .job_discovery import JobDiscoveryService
from .linkedin import LinkedInDiscoveryService
from .models import DashboardState
from .reporting import write_summary
from .state import deduplicate_jobs, load_state, merge_jobs, replace_job, save_state
from .utils import utc_now
from .vendor_workbook import load_vendors
from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="job_apply_bot",
        description="Scan trusted vendor sites and apply to AI/ML roles.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the local web interface.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5050)

    scan_parser = subparsers.add_parser("scan", help="Scan vendors and manual URLs for eligible jobs.")
    scan_parser.add_argument("--vendor-limit", type=int, default=25)

    indeed_login_parser = subparsers.add_parser("indeed-login", help="Open a persistent browser for Indeed sign-in.")
    indeed_login_parser.add_argument("--url", default="https://www.indeed.com/")

    indeed_scan_parser = subparsers.add_parser("indeed-scan", help="Scan Indeed for recent AI/ML jobs.")
    indeed_scan_parser.add_argument("--query", default="")
    indeed_scan_parser.add_argument("--location", default="")
    indeed_scan_parser.add_argument("--max-pages", type=int, default=0)
    indeed_scan_parser.add_argument("--max-jobs", type=int, default=0)
    indeed_scan_parser.add_argument(
        "--include-non-easy-apply",
        action="store_true",
        help="Keep Indeed jobs that are not Easy Apply, but mark them as manual-only.",
    )

    linkedin_login_parser = subparsers.add_parser("linkedin-login", help="Open a persistent browser for LinkedIn sign-in.")
    linkedin_login_parser.add_argument("--url", default="https://www.linkedin.com/jobs/")

    linkedin_scan_parser = subparsers.add_parser("linkedin-scan", help="Scan LinkedIn for recent contract AI/ML jobs.")
    linkedin_scan_parser.add_argument("--query", default="")
    linkedin_scan_parser.add_argument("--location", default="")
    linkedin_scan_parser.add_argument("--max-pages", type=int, default=0)
    linkedin_scan_parser.add_argument("--max-jobs", type=int, default=0)
    linkedin_scan_parser.add_argument(
        "--include-non-easy-apply",
        action="store_true",
        help="Keep LinkedIn jobs that are not Easy Apply, but mark them as manual-only.",
    )
    linkedin_scan_parser.add_argument(
        "--include-non-contract",
        action="store_true",
        help="Keep LinkedIn jobs that do not explicitly show Contract filtering.",
    )
    linkedin_scan_parser.add_argument(
        "--include-onsite",
        action="store_true",
        help="Keep LinkedIn jobs that are not filtered to remote-only.",
    )
    linkedin_scan_parser.add_argument(
        "--experience-levels",
        default="",
        help="Comma-separated LinkedIn experience-level codes. Defaults to 2,3.",
    )

    apply_parser = subparsers.add_parser("apply", help="Apply to jobs from the last scan.")
    apply_parser.add_argument("--submit-mode", choices=["review", "auto"], default=None)
    apply_parser.add_argument("--all", action="store_true", help="Apply to all eligible jobs instead of just one.")
    apply_parser.add_argument("--job-id", default="", help="Apply to a single job id.")
    apply_parser.add_argument(
        "--force-apply",
        action="store_true",
        help="Bypass eligibility criteria checks and attempt application anyway.",
    )

    chrome_mcp_server_parser = subparsers.add_parser(
        "chrome-mcp-server",
        help="Run the local WebSocket bridge for the Chrome extension.",
    )
    chrome_mcp_server_parser.add_argument("--host", default="")
    chrome_mcp_server_parser.add_argument("--port", type=int, default=0)

    subparsers.add_parser(
        "chrome-mcp-extension-path",
        help="Print the expected Chrome extension directory for the bridge.",
    )

    posts_parser = subparsers.add_parser(
        "linkedin-posts",
        help="Scan LinkedIn feed posts for AI/ML C2C job leads (last 1-2 hours).",
    )
    posts_parser.add_argument("--hours", type=int, default=2, help="How many hours back to look (default 2).")
    posts_parser.add_argument("--query", default="", help="Extra search query (added to default queries).")
    posts_parser.add_argument("--scrolls", type=int, default=4, help="Scroll passes per query (default 4).")

    args = parser.parse_args()

    if args.command == "serve":
        app = create_app()
        app.run(host=args.host, port=args.port, debug=False)
        return

    settings = Settings.from_env()

    if args.command == "indeed-login":
        IndeedDiscoveryService(settings).bootstrap_session(url=args.url)
        print(f"Indeed session prepared using profile {settings.browser_profile_dir}.")
        return

    if args.command == "linkedin-login":
        LinkedInDiscoveryService(settings).bootstrap_session(url=args.url)
        print(f"LinkedIn session prepared using profile {settings.browser_profile_dir}.")
        return

    if args.command == "chrome-mcp-extension-path":
        from .chrome_mcp_server import extension_path

        print(extension_path(settings))
        return

    vendors = load_vendors(settings.vendor_workbook_path) if settings.vendor_workbook_path.exists() else []

    if args.command == "chrome-mcp-server":
        try:
            from .chrome_mcp_server import run_server

            run_server(settings, host=args.host or settings.chrome_mcp_host, port=args.port or settings.chrome_mcp_port)
        except KeyboardInterrupt:
            return
        return

    if args.command == "scan":
        discovery = JobDiscoveryService(settings, vendors)
        jobs = discovery.discover(vendor_limit=args.vendor_limit)
        previous_state = load_state(settings.state_path)
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=merge_jobs(previous_state.jobs, jobs),
        )
        save_state(settings.state_path, state)
        write_summary(settings, state.jobs)
        print(f"Scanned {len(vendors[:args.vendor_limit])} vendors. Found {len(jobs)} jobs.")
        return

    if args.command == "indeed-scan":
        saved_search = settings.load_indeed_search()
        query = args.query or saved_search.get("query", "")
        location = args.location or saved_search.get("location", "")
        max_pages = args.max_pages or int(saved_search.get("max_pages", 2) or 2)
        max_jobs = args.max_jobs or int(saved_search.get("max_jobs", 20) or 20)
        recency_hours = int(saved_search.get("recency_hours", 168) or 168)
        easy_apply_only = not args.include_non_easy_apply
        if not args.include_non_easy_apply:
            easy_apply_only = bool(saved_search.get("easy_apply_only", True))

        settings.save_indeed_search(
            {
                "query": query,
                "location": location,
                "max_pages": max_pages,
                "max_jobs": max_jobs,
                "easy_apply_only": easy_apply_only,
            }
        )

        discovery = IndeedDiscoveryService(settings)
        jobs = discovery.discover(
            query=query,
            location=location,
            max_pages=max_pages,
            max_jobs=max_jobs,
            easy_apply_only=easy_apply_only,
        )
        previous_state = load_state(settings.indeed_state_path)
        deduped = deduplicate_jobs(merge_jobs(previous_state.jobs, jobs))
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=deduped,
        )
        save_state(settings.indeed_state_path, state)
        # Rebuild combined dashboard
        _rebuild_dashboard(settings, vendors)
        print(f"Scanned Indeed for '{query}' in '{location or 'all locations'}'. Found {len(jobs)} jobs (deduped to {len(deduped)}).")
        return

    if args.command == "linkedin-scan":
        saved_search = settings.load_linkedin_search()
        query = args.query or saved_search.get("query", "")
        location = args.location or saved_search.get("location", "")
        max_pages = args.max_pages or int(saved_search.get("max_pages", 2) or 2)
        max_jobs = args.max_jobs or int(saved_search.get("max_jobs", 20) or 20)
        recency_hours = int(saved_search.get("recency_hours", 168) or 168)
        easy_apply_only = not args.include_non_easy_apply
        if not args.include_non_easy_apply:
            easy_apply_only = bool(saved_search.get("easy_apply_only", True))
        contract_only = not args.include_non_contract
        if not args.include_non_contract:
            contract_only = bool(saved_search.get("contract_only", True))
        remote_only = not args.include_onsite
        if not args.include_onsite:
            remote_only = bool(saved_search.get("remote_only", True))
        experience_levels = args.experience_levels or ",".join(saved_search.get("experience_levels", ["2", "3"]))
        levels = [item.strip() for item in str(experience_levels).split(",") if item.strip()]

        settings.save_linkedin_search(
            {
                "query": query,
                "location": location,
                "max_pages": max_pages,
                "max_jobs": max_jobs,
                "recency_hours": recency_hours,
                "easy_apply_only": easy_apply_only,
                "contract_only": contract_only,
                "remote_only": remote_only,
                "experience_levels": levels,
            }
        )

        discovery = LinkedInDiscoveryService(settings)
        jobs = discovery.discover(
            query=query,
            location=location,
            max_pages=max_pages,
            max_jobs=max_jobs,
            recency_hours=recency_hours,
            easy_apply_only=easy_apply_only,
            contract_only=contract_only,
            remote_only=remote_only,
            experience_levels=levels,
        )
        previous_state = load_state(settings.linkedin_state_path)
        deduped = deduplicate_jobs(merge_jobs(previous_state.jobs, jobs))
        state = DashboardState(
            vendors_loaded=len(vendors),
            last_scan_at=utc_now().replace(microsecond=0).isoformat(),
            jobs=deduped,
        )
        save_state(settings.linkedin_state_path, state)
        # Rebuild combined dashboard
        _rebuild_dashboard(settings, vendors)
        print(f"Scanned LinkedIn for '{query}' in '{location or 'all locations'}'. Found {len(jobs)} jobs (deduped to {len(deduped)}).")
        return

    if args.command == "apply":
        # Do NOT rebuild dashboard here — apply only what was explicitly queued in state_path
        state = load_state(settings.state_path)
        targets = []
        if args.job_id:
            if args.force_apply:
                targets = [job for job in state.jobs if job.job_id == args.job_id and job.status != "submitted"]
            else:
                targets = [job for job in state.jobs if job.job_id == args.job_id and job.eligible]
        elif args.all:
            if args.force_apply:
                targets = [job for job in state.jobs if job.status != "submitted"]
            else:
                targets = [job for job in state.jobs if job.eligible and job.status not in {"submitted", "review_required"}]

        if not targets:
            print("No eligible jobs selected.")
            return

        service = JobApplicationService(
            settings,
            settings.load_profile(),
            settings.load_question_answers(),
            force_apply=args.force_apply,
        )

        def save_progress(updated_job):
            state.jobs = replace_job(state.jobs, updated_job)
            save_state(settings.state_path, state)
            write_summary(settings, state.jobs)
            print(f"[{updated_job.status.upper()}] {updated_job.company} - {updated_job.title}")

        updated_jobs = service.apply_jobs(targets, submit_mode=args.submit_mode, on_job_complete=save_progress)
        print(f"\nFinalized {len(updated_jobs)} job(s). Dashboard updated.")

    if args.command == "linkedin-posts":
        import os
        from pathlib import Path
        from .linkedin_posts import scan_linkedin_posts, DEFAULT_SEARCH_QUERIES

        queries = list(DEFAULT_SEARCH_QUERIES)
        if args.query:
            queries.insert(0, args.query)

        ws_url = f"ws://{settings.chrome_mcp_host}:{settings.chrome_mcp_port}"
        leads = scan_linkedin_posts(
            queries=queries,
            max_hours=args.hours,
            scroll_passes=args.scrolls,
            ws_url=ws_url,
        )

        eligible = [l for l in leads if l.eligible]
        skipped = [l for l in leads if not l.eligible]

        # Save results
        env_dir = os.environ.get("JOB_BOT_OUTPUT_DIR", "")
        if env_dir:
            output_dir = Path(env_dir)
        else:
            output_dir = Path("runs") / utc_now().strftime("%Y-%m-%d")
        output_dir.mkdir(parents=True, exist_ok=True)
        out_file = output_dir / "linkedin_posts_scan.json"
        with open(out_file, "w") as f:
            import json as _json
            _json.dump(
                {
                    "scanned_at": utc_now().isoformat(),
                    "total": len(leads),
                    "eligible": len(eligible),
                    "leads": [l.to_dict() for l in leads],
                },
                f,
                indent=2,
            )

        print(f"\n{'='*60}")
        print(f"ELIGIBLE JOB LEADS ({len(eligible)})")
        print("=" * 60)
        profile = settings.load_profile()
        for i, lead in enumerate(eligible, 1):
            print(f"\n[{i}] {lead.title or '(title not extracted)'}")
            print(f"    Age      : {lead.age_minutes}m ago")
            print(f"    Location : {lead.location or 'see post'}")
            print(f"    Author   : {lead.author}")
            print(f"    Email    : {lead.email or '(no email in post)'}")
            print(f"    Post URL : {lead.url}")
            preview = lead.text[:300].replace("\n", " ")
            print(f"    Preview  : {preview}...")
            if lead.email:
                subject, body = _compose_outreach_email(lead, profile)
                print(f"\n    --- DRAFT EMAIL TO {lead.email} ---")
                print(f"    Subject: {subject}")
                print(f"    Body:")
                for line in body.splitlines():
                    print(f"    {line}")
                print(f"    --- END DRAFT ---")

        print(f"\nResults saved → {out_file}")


def _compose_outreach_email(lead, profile: dict) -> tuple:
    author = (lead.author or "").strip()
    first_name = author.split()[0] if author else "there"
    role = lead.title or "AI/ML Engineer"
    location = lead.location or "your location"
    full_name = profile.get("full_name", "Your Name")
    phone = profile.get("phone", "")
    linkedin = profile.get("linkedin_url", "")
    email_addr = profile.get("email", "")
    title_line = profile.get("title", "AI/ML Engineer")
    exp_years = profile.get("experience_years", 3)
    short_pitch = profile.get("short_pitch", "")
    work_auth = profile.get("work_authorization", "OPT (F-1)")
    subject = f"{title_line} – Available for C2C | {full_name}"
    contact_lines = "\n".join(filter(None, [
        f"Phone: {phone}" if phone else "",
        f"LinkedIn: {linkedin}" if linkedin else "",
        f"Email: {email_addr}" if email_addr else "",
    ]))
    body = (
        f"Hi {first_name},\n\n"
        f"I came across your LinkedIn post for the {role} role in\n"
        f"{location} and wanted to reach out directly.\n\n"
        f"I'm {full_name}, a {title_line} with around {exp_years} years of\n"
        f"experience building production-grade AI systems. I'm only open to C2C\n"
        f"contract engagements.\n\n"
        + (f"{short_pitch}\n\n" if short_pitch else "")
        + f"I'm on {work_auth} and authorized to work without employer sponsorship.\n"
        f"Available to start within 2 weeks on a C2C basis.\n\n"
        f"Resume attached. Happy to connect for a quick call.\n\n"
        f"Best,\n"
        f"{full_name}"
        + (f"\n{contact_lines}" if contact_lines else "")
    )
    return subject, body


def _rebuild_dashboard(settings, vendors) -> None:
    """Merge indeed_state.json + linkedin_state.json → dashboard_state.json (deduped)."""
    indeed_state = load_state(settings.indeed_state_path)
    linkedin_state = load_state(settings.linkedin_state_path)
    all_jobs = deduplicate_jobs(indeed_state.jobs + linkedin_state.jobs)
    combined = DashboardState(
        vendors_loaded=len(vendors),
        last_scan_at=utc_now().replace(microsecond=0).isoformat(),
        jobs=all_jobs,
    )
    save_state(settings.state_path, combined)
    write_summary(settings, combined.jobs)
