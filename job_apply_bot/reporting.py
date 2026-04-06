from typing import List

from .config import Settings
from .models import JobRecord
from .utils import save_json


def write_summary(settings: Settings, jobs: List[JobRecord]) -> None:
    save_json(settings.summary_json_path, [job.to_dict() for job in jobs])

    lines = ["# Job Bot Results", ""]
    submitted = [job for job in jobs if job.status == "submitted"]
    review = [job for job in jobs if job.status in {"review_required", "ready_to_submit"}]
    failed = [job for job in jobs if job.status == "failed"]

    lines.append(f"- Submitted: {len(submitted)}")
    lines.append(f"- Review required: {len(review)}")
    lines.append(f"- Failed: {len(failed)}")
    lines.append("")

    for job in jobs:
        lines.append(
            f"- {job.company or 'Unknown company'} | {job.title or 'Unknown role'} | "
            f"{job.status} | {job.reason or 'No notes'} | {job.apply_url or job.source_url}"
        )

    settings.summary_markdown_path.write_text("\n".join(lines), encoding="utf-8")

