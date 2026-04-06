import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse


ATS_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "myworkdayjobs.com",
}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def normalize_domain(url_or_host: str) -> str:
    parsed = urlparse(url_or_host if "://" in url_or_host else f"https://{url_or_host}")
    host = parsed.netloc or parsed.path
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        return f"https://{url}"
    return url


def url_slug(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None

    cleaned = raw.replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.split(".")[0], cleaned.split("T")[0]):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    patterns = (
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    )
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def within_last_hours(posted_at: Optional[datetime], hours: int) -> bool:
    if posted_at is None:
        return False
    return utc_now() - posted_at <= timedelta(hours=hours)


def is_known_ats_host(host: str) -> bool:
    normalized = normalize_domain(host)
    return any(normalized == ats_host or normalized.endswith(f".{ats_host}") for ats_host in ATS_HOSTS)


def compact_text(value: str, limit: int = 5000) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:limit]


def first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def as_utc_iso(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def prettify_timestamp(value: str) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return ""
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def same_or_parent_domain(candidate_host: str, trusted_host: str) -> bool:
    candidate = normalize_domain(candidate_host)
    trusted = normalize_domain(trusted_host)
    return candidate == trusted or candidate.endswith(f".{trusted}")


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned or "capture"

