from copy import deepcopy
from threading import Lock
from typing import Dict, List

from .models import Vendor
from .utils import as_utc_iso, utc_now


_LOCK = Lock()
_STATE: Dict[str, object] = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "current_vendor": "",
    "current_url": "",
    "message": "",
    "jobs_discovered": 0,
    "error": "",
    "vendors": [],
}


def initialize(vendors: List[Vendor]) -> None:
    with _LOCK:
        _STATE.update(
            {
                "running": True,
                "started_at": as_utc_iso(utc_now()),
                "finished_at": "",
                "current_vendor": "",
                "current_url": "",
                "message": "Starting vendor scan",
                "jobs_discovered": 0,
                "error": "",
                "vendors": [
                    {
                        "name": vendor.name,
                        "website": vendor.website,
                        "resolved_website": "",
                        "status": "pending",
                        "jobs_found": 0,
                        "eligible_found": 0,
                        "last_url": "",
                        "note": "",
                    }
                    for vendor in vendors
                ],
            }
        )


def update(event: str, **payload: object) -> None:
    with _LOCK:
        vendor_name = str(payload.get("vendor", "") or "")
        vendor_state = _find_vendor(vendor_name)

        if event == "scan_started":
            _STATE["message"] = str(payload.get("message", "Starting vendor scan"))
        elif event == "vendor_resolving" and vendor_state is not None:
            vendor_state["status"] = "running"
            vendor_state["note"] = str(payload.get("message", "Resolving career page"))
            vendor_state["last_url"] = str(payload.get("url", "") or vendor_state["website"])
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
            _STATE["message"] = vendor_state["note"]
        elif event == "vendor_resolved" and vendor_state is not None:
            vendor_state["status"] = "running"
            vendor_state["resolved_website"] = str(payload.get("career_page", "") or payload.get("url", "") or "")
            vendor_state["last_url"] = vendor_state["resolved_website"] or vendor_state["last_url"]
            vendor_state["note"] = str(payload.get("message", "Resolved career page"))
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
            _STATE["message"] = vendor_state["note"]
        elif event == "vendor_started" and vendor_state is not None:
            vendor_state["status"] = "running"
            vendor_state["note"] = "Scanning resolved career page"
            vendor_state["last_url"] = str(
                payload.get("url", "") or vendor_state.get("resolved_website") or vendor_state["website"]
            )
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
            _STATE["message"] = f"Inspecting {vendor_name}"
        elif event == "vendor_url" and vendor_state is not None:
            vendor_state["status"] = "running"
            vendor_state["last_url"] = str(payload.get("url", "") or "")
            vendor_state["note"] = str(payload.get("message", "Inspecting page"))
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
            _STATE["message"] = vendor_state["note"]
        elif event == "vendor_done" and vendor_state is not None:
            vendor_state["status"] = "done"
            vendor_state["jobs_found"] = int(payload.get("jobs_found", 0) or 0)
            vendor_state["eligible_found"] = int(payload.get("eligible_found", 0) or 0)
            vendor_state["note"] = str(payload.get("message", "Vendor scan complete"))
            _STATE["jobs_discovered"] = int(_STATE.get("jobs_discovered", 0)) + vendor_state["jobs_found"]
            _STATE["message"] = vendor_state["note"]
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
        elif event == "vendor_skipped" and vendor_state is not None:
            vendor_state["status"] = "skipped"
            vendor_state["last_url"] = str(
                payload.get("url", "") or vendor_state.get("resolved_website") or vendor_state["website"]
            )
            vendor_state["note"] = str(payload.get("message", "Skipped vendor"))
            _STATE["message"] = vendor_state["note"]
            _STATE["current_vendor"] = vendor_name
            _STATE["current_url"] = vendor_state["last_url"]
        elif event == "vendor_error" and vendor_state is not None:
            vendor_state["status"] = "error"
            vendor_state["note"] = str(payload.get("message", "Vendor scan failed"))
            _STATE["message"] = vendor_state["note"]
            _STATE["current_vendor"] = vendor_name
        elif event == "scan_finished":
            _STATE["running"] = False
            _STATE["finished_at"] = as_utc_iso(utc_now())
            _STATE["current_vendor"] = ""
            _STATE["current_url"] = ""
            _STATE["message"] = str(payload.get("message", "Scan finished"))
        elif event == "scan_error":
            _STATE["running"] = False
            _STATE["finished_at"] = as_utc_iso(utc_now())
            _STATE["error"] = str(payload.get("message", "Scan failed"))
            _STATE["message"] = _STATE["error"]
            _STATE["current_vendor"] = ""
            _STATE["current_url"] = ""
        elif event == "scan_cleared":
            _STATE.update(
                {
                    "running": False,
                    "started_at": "",
                    "finished_at": "",
                    "current_vendor": "",
                    "current_url": "",
                    "message": "Cleared previous fetched jobs",
                    "jobs_discovered": 0,
                    "error": "",
                    "vendors": [],
                }
            )


def snapshot() -> Dict[str, object]:
    with _LOCK:
        return deepcopy(_STATE)


def is_running() -> bool:
    with _LOCK:
        return bool(_STATE.get("running"))


def _find_vendor(vendor_name: str):
    for vendor in _STATE.get("vendors", []):
        if vendor.get("name") == vendor_name:
            return vendor
    return None
