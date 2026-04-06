import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    root_dir: Path
    resume_path: Path
    vendor_workbook_path: Path
    profile_path: Path
    job_urls_path: Path
    indeed_search_path: Path
    linkedin_search_path: Path
    question_answers_path: Path
    output_dir: Path
    headless: bool
    submit_mode: str
    recency_hours: int
    timeout_ms: int
    delay_ms: int
    manual_gate_timeout_ms: int
    browser_profile_dir: Path
    browser_channel: str
    browser_cdp_url: str
    chrome_mcp_host: str
    chrome_mcp_port: int
    chrome_mcp_extension_dir: Path
    state_path: Path
    indeed_state_path: Path
    linkedin_state_path: Path
    summary_json_path: Path
    summary_markdown_path: Path
    career_pages_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        root_dir = ROOT_DIR
        output_dir = Path(os.getenv("JOB_BOT_OUTPUT_DIR", root_dir / "runs"))
        output_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            root_dir=root_dir,
            resume_path=Path(os.getenv("JOB_BOT_RESUME_PATH", root_dir / "config" / "resume.pdf")),
            vendor_workbook_path=Path(
                os.getenv("JOB_BOT_VENDOR_WORKBOOK_PATH", root_dir / "config" / "White_Vendors_List.xlsx")
            ),
            profile_path=Path(os.getenv("JOB_BOT_PROFILE_PATH", root_dir / "config" / "candidate_profile.json")),
            job_urls_path=Path(os.getenv("JOB_BOT_JOB_URLS_PATH", root_dir / "config" / "job_urls.txt")),
            indeed_search_path=Path(os.getenv("JOB_BOT_INDEED_SEARCH_PATH", root_dir / "config" / "indeed_search.json")),
            linkedin_search_path=Path(
                os.getenv("JOB_BOT_LINKEDIN_SEARCH_PATH", root_dir / "config" / "linkedin_search.json")
            ),
            question_answers_path=Path(
                os.getenv("JOB_BOT_QUESTION_ANSWERS_PATH", root_dir / "config" / "question_answers.json")
            ),
            output_dir=output_dir,
            headless=_bool("JOB_BOT_HEADLESS", True),
            submit_mode=os.getenv("JOB_BOT_SUBMIT_MODE", "review").strip().lower(),
            recency_hours=int(os.getenv("JOB_BOT_RECENCY_HOURS", "24")),
            timeout_ms=int(os.getenv("JOB_BOT_TIMEOUT_MS", "30000")),
            delay_ms=int(os.getenv("JOB_BOT_DELAY_MS", "1500")),
            manual_gate_timeout_ms=int(os.getenv("JOB_BOT_MANUAL_GATE_TIMEOUT_MS", "180000")),
            browser_profile_dir=Path(os.getenv("JOB_BOT_BROWSER_PROFILE_DIR", output_dir / "browser-profile")),
            browser_channel=os.getenv("JOB_BOT_BROWSER_CHANNEL", "").strip(),
            browser_cdp_url=os.getenv("JOB_BOT_CDP_URL", "").strip(),
            chrome_mcp_host=os.getenv("JOB_BOT_CHROME_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            chrome_mcp_port=int(os.getenv("JOB_BOT_CHROME_MCP_PORT", "8765")),
            chrome_mcp_extension_dir=Path(
                os.getenv("JOB_BOT_CHROME_MCP_EXTENSION_DIR", root_dir / "chrome_mcp" / "extension")
            ),
            state_path=output_dir / "dashboard_state.json",
            indeed_state_path=output_dir / "indeed_state.json",
            linkedin_state_path=output_dir / "linkedin_state.json",
            summary_json_path=output_dir / "latest_results.json",
            summary_markdown_path=output_dir / "latest_results.md",
            career_pages_path=output_dir / "career_pages.json",
        )

    def load_profile(self) -> dict:
        return json.loads(self.profile_path.read_text(encoding="utf-8"))

    def load_question_answers(self) -> dict:
        if not self.question_answers_path.exists():
            return {"exact": {}, "contains": {}}
        return json.loads(self.question_answers_path.read_text(encoding="utf-8"))

    def load_indeed_search(self) -> dict:
        default = {
            "query": "AI Engineer",
            "location": "United States",
            "max_pages": 2,
            "max_jobs": 20,
            "easy_apply_only": True,
        }
        if not self.indeed_search_path.exists():
            return default
        payload = json.loads(self.indeed_search_path.read_text(encoding="utf-8"))
        merged = {**default, **payload}
        merged["max_pages"] = max(1, int(merged.get("max_pages", 2) or 2))
        merged["max_jobs"] = max(1, int(merged.get("max_jobs", 20) or 20))
        merged["easy_apply_only"] = bool(merged.get("easy_apply_only", True))
        return merged

    def save_indeed_search(self, payload: dict) -> None:
        merged = {**self.load_indeed_search(), **payload}
        merged["max_pages"] = max(1, int(merged.get("max_pages", 2) or 2))
        merged["max_jobs"] = max(1, int(merged.get("max_jobs", 20) or 20))
        merged["easy_apply_only"] = bool(merged.get("easy_apply_only", True))
        self.indeed_search_path.parent.mkdir(parents=True, exist_ok=True)
        self.indeed_search_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    def load_linkedin_search(self) -> dict:
        default = {
            "query": "Machine Learning Engineer",
            "location": "United States",
            "max_pages": 2,
            "max_jobs": 20,
            "recency_hours": 168,
            "easy_apply_only": True,
            "contract_only": True,
            "remote_only": False,
            "experience_levels": ["2", "3"],
        }
        if not self.linkedin_search_path.exists():
            return default
        payload = json.loads(self.linkedin_search_path.read_text(encoding="utf-8"))
        merged = {**default, **payload}
        merged["max_pages"] = max(1, int(merged.get("max_pages", 2) or 2))
        merged["max_jobs"] = max(1, int(merged.get("max_jobs", 20) or 20))
        merged["recency_hours"] = max(1, int(merged.get("recency_hours", 168) or 168))
        merged["easy_apply_only"] = bool(merged.get("easy_apply_only", True))
        merged["contract_only"] = bool(merged.get("contract_only", True))
        merged["remote_only"] = bool(merged.get("remote_only", True))
        levels = merged.get("experience_levels", ["2", "3"])
        if isinstance(levels, str):
            levels = [item.strip() for item in levels.split(",") if item.strip()]
        merged["experience_levels"] = [str(item).strip() for item in levels if str(item).strip()]
        return merged

    def save_linkedin_search(self, payload: dict) -> None:
        merged = {**self.load_linkedin_search(), **payload}
        merged["max_pages"] = max(1, int(merged.get("max_pages", 2) or 2))
        merged["max_jobs"] = max(1, int(merged.get("max_jobs", 20) or 20))
        merged["recency_hours"] = max(1, int(merged.get("recency_hours", 168) or 168))
        merged["easy_apply_only"] = bool(merged.get("easy_apply_only", True))
        merged["contract_only"] = bool(merged.get("contract_only", True))
        merged["remote_only"] = bool(merged.get("remote_only", True))
        levels = merged.get("experience_levels", ["2", "3"])
        if isinstance(levels, str):
            levels = [item.strip() for item in levels.split(",") if item.strip()]
        merged["experience_levels"] = [str(item).strip() for item in levels if str(item).strip()]
        self.linkedin_search_path.parent.mkdir(parents=True, exist_ok=True)
        self.linkedin_search_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
