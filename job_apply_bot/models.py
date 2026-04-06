from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class Vendor:
    name: str
    website: str
    domain: str
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class CandidateProfile:
    full_name: str
    title: str
    email: str
    phone: str
    summary: str = ""
    short_pitch: str = ""
    location: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    current_company: str = ""
    current_role: str = ""
    experience_years: int = 0
    work_authorization: str = ""
    requires_visa_sponsorship: str = ""
    skills: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class JobRecord:
    job_id: str
    source_url: str
    discovered_from: str
    company: str = ""
    title: str = ""
    location: str = ""
    posted_at: str = ""
    description: str = ""
    provider: str = "generic"
    apply_url: str = ""
    easy_apply: bool = False
    apply_supported: bool = True
    trusted: bool = False
    ai_ml_match: bool = False
    recency_ok: bool = False
    criteria_ok: bool = True
    matched_keywords: List[str] = field(default_factory=list)
    status: str = "pending"
    reason: str = ""
    submitted_at: str = ""
    screenshot_path: str = ""
    submission_verified: bool = False

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @property
    def eligible(self) -> bool:
        return self.trusted and self.ai_ml_match and self.recency_ok and self.apply_supported and self.criteria_ok


@dataclass
class DashboardState:
    vendors_loaded: int = 0
    last_scan_at: str = ""
    jobs: List[JobRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "vendors_loaded": self.vendors_loaded,
            "last_scan_at": self.last_scan_at,
            "jobs": [job.to_dict() for job in self.jobs],
        }
