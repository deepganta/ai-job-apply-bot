import re
from pathlib import Path
from typing import List

from .models import DashboardState, JobRecord
from .utils import load_json, save_json


def _dedup_key(job: JobRecord) -> str:
    """Canonical (company, title) key for deduplication — lowercased, punctuation stripped."""
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().strip())
    return f"{_norm(job.company)}|{_norm(job.title)}"


def deduplicate_jobs(jobs: List[JobRecord]) -> List[JobRecord]:
    """Keep only the first occurrence of each (company, title) pair.
    Prefer submitted/review_required over pending duplicates."""
    priority = {"submitted": 0, "review_required": 1, "ready_to_submit": 2, "pending": 3}
    seen: dict = {}
    for job in jobs:
        key = _dedup_key(job)
        if key not in seen:
            seen[key] = job
        else:
            existing = seen[key]
            if priority.get(job.status, 9) < priority.get(existing.status, 9):
                seen[key] = job
    return list(seen.values())


def load_state(path: Path) -> DashboardState:
    payload = load_json(path, {"vendors_loaded": 0, "last_scan_at": "", "jobs": []})
    jobs = [_normalize_loaded_job(JobRecord(**job)) for job in payload.get("jobs", [])]
    return DashboardState(
        vendors_loaded=payload.get("vendors_loaded", 0),
        last_scan_at=payload.get("last_scan_at", ""),
        jobs=jobs,
    )


def save_state(path: Path, state: DashboardState) -> None:
    save_json(path, state.to_dict())


def merge_jobs(existing_jobs: List[JobRecord], fresh_jobs: List[JobRecord]) -> List[JobRecord]:
    existing_by_id = {job.job_id: job for job in existing_jobs}
    merged: List[JobRecord] = []
    seen = set()

    for fresh in fresh_jobs:
        prior = existing_by_id.get(fresh.job_id)
        if prior:
            if prior.status == "submitted" and not prior.submission_verified:
                prior = None
            if prior and prior.status != "pending":
                fresh.status = prior.status
                fresh.reason = prior.reason or fresh.reason
                fresh.submitted_at = prior.submitted_at or fresh.submitted_at
                fresh.screenshot_path = prior.screenshot_path or fresh.screenshot_path
                fresh.submission_verified = prior.submission_verified or fresh.submission_verified
        merged.append(fresh)
        seen.add(fresh.job_id)

    for prior in existing_jobs:
        if prior.job_id in seen:
            continue
        if prior.status == "submitted" and not prior.submission_verified:
            continue
        # Keep all non-pending terminal states AND keep pending jobs so that
        # multi-query scans accumulate results instead of overwriting each other.
        if prior.status in {"submitted", "review_required", "ready_to_submit", "pending"}:
            merged.append(prior)

    return merged


def replace_job(jobs: List[JobRecord], updated_job: JobRecord) -> List[JobRecord]:
    rewritten: List[JobRecord] = []
    replaced = False
    for job in jobs:
        if job.job_id == updated_job.job_id:
            rewritten.append(updated_job)
            replaced = True
        else:
            rewritten.append(job)
    if not replaced:
        rewritten.append(updated_job)
    return rewritten


def _normalize_loaded_job(job: JobRecord) -> JobRecord:
    if job.status == "submitted" and job.provider != "linkedin" and not job.submission_verified:
        job.submission_verified = True
        return job

    if job.provider == "linkedin" and job.status == "submitted" and not job.submission_verified:
        job.status = "pending"
        job.reason = "Invalidated: prior LinkedIn run did not verify a completed submission"
        job.submitted_at = ""
        job.screenshot_path = ""
    return job
