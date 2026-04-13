from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence

from websockets.sync.client import ClientConnection, connect

from .utils import compact_text, normalize_text


DEFAULT_WS_URL = "ws://127.0.0.1:8765"
DEFAULT_CAPABILITIES = ("ping", "tabs.list", "page.collect", "page.read", "page.find", "page.action")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_exact(value: Any) -> str:
    return " ".join(_coerce_text(value).split()).strip().lower()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ChromeMcpError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeTab:
    id: str
    url: str
    title: str
    active: bool = False
    window_id: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BridgeTab":
        tab_id = _coerce_text(payload.get("id") or payload.get("tabId"))
        return cls(
            id=tab_id,
            url=_coerce_text(payload.get("url")),
            title=_coerce_text(payload.get("title")),
            active=bool(payload.get("active", False)),
            window_id=payload.get("windowId"),
            raw=dict(payload),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "active": self.active,
            "windowId": self.window_id,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class BridgeElement:
    id: str
    tag_name: str
    type: str
    role: str
    label: str
    name: str
    aria_label: str
    placeholder: str
    text: str
    value: str
    checked: bool
    required: bool
    disabled: bool
    href: str
    x: int
    y: int
    width: int
    height: int
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BridgeElement":
        return cls(
            id=_coerce_text(payload.get("id")),
            tag_name=_coerce_text(payload.get("tagName")),
            type=_coerce_text(payload.get("type")),
            role=_coerce_text(payload.get("role")),
            label=_coerce_text(payload.get("label")),
            name=_coerce_text(payload.get("name")),
            aria_label=_coerce_text(payload.get("ariaLabel")),
            placeholder=_coerce_text(payload.get("placeholder")),
            text=_coerce_text(payload.get("text")),
            value=_coerce_text(payload.get("value")),
            checked=bool(payload.get("checked", False)),
            required=bool(payload.get("required", False)),
            disabled=bool(payload.get("disabled", False)),
            href=_coerce_text(payload.get("href")),
            x=_as_int(payload.get("x")),
            y=_as_int(payload.get("y")),
            width=_as_int(payload.get("width")),
            height=_as_int(payload.get("height")),
            raw=dict(payload),
        )

    def exact_terms(self) -> List[str]:
        terms = [
            self.text,
            self.aria_label,
            self.label,
            self.placeholder,
            self.name,
            self.value,
            self.href,
            self.role,
            self.tag_name,
            self.type,
        ]
        normalized: List[str] = []
        for term in terms:
            candidate = _normalize_exact(term)
            if candidate and candidate not in normalized:
                normalized.append(candidate)
        return normalized

    def matches_exact(self, query: str) -> bool:
        candidate = _normalize_exact(query)
        return bool(candidate) and candidate in self.exact_terms()

    def matches_contains(self, query: str) -> bool:
        candidate = normalize_text(query)
        if not candidate:
            return False
        fields = (
            self.text,
            self.aria_label,
            self.label,
            self.placeholder,
            self.name,
            self.value,
            self.href,
            self.role,
            self.tag_name,
            self.type,
        )
        return any(candidate in normalize_text(field) for field in fields if field)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tagName": self.tag_name,
            "type": self.type,
            "role": self.role,
            "label": self.label,
            "name": self.name,
            "ariaLabel": self.aria_label,
            "placeholder": self.placeholder,
            "text": self.text,
            "value": self.value,
            "checked": self.checked,
            "required": self.required,
            "disabled": self.disabled,
            "href": self.href,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class BridgePageSnapshot:
    url: str
    title: str
    scroll_x: int
    scroll_y: int
    viewport: Dict[str, Any]
    visible_text_excerpt: str
    interactive_elements: List[BridgeElement] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BridgePageSnapshot":
        interactive = payload.get("interactiveElements", [])
        return cls(
            url=_coerce_text(payload.get("url")),
            title=_coerce_text(payload.get("title")),
            scroll_x=_as_int(payload.get("scrollX")),
            scroll_y=_as_int(payload.get("scrollY")),
            viewport=dict(payload.get("viewport", {})) if isinstance(payload.get("viewport"), dict) else {},
            visible_text_excerpt=compact_text(_coerce_text(payload.get("visibleTextExcerpt")), limit=4000),
            interactive_elements=[BridgeElement.from_payload(item) for item in interactive if isinstance(item, Mapping)],
            raw=dict(payload),
        )

    def text_blob(self) -> str:
        return compact_text(" ".join(part for part in (self.title, self.visible_text_excerpt, self.url) if part), limit=8000)

    def find_controls(self, query: str, exact: bool = True, limit: int = 10) -> List[BridgeElement]:
        normalized = _normalize_exact(query) if exact else normalize_text(query)
        if not normalized:
            return []

        matches: List[BridgeElement] = []
        for element in self.interactive_elements:
            if exact:
                if element.matches_exact(query):
                    matches.append(element)
            elif element.matches_contains(query):
                matches.append(element)
            if len(matches) >= limit:
                break
        return matches

    def find_first_control(self, query: str, exact: bool = True) -> Optional[BridgeElement]:
        matches = self.find_controls(query, exact=exact, limit=1)
        return matches[0] if matches else None

    def contains_any(self, phrases: Iterable[str]) -> bool:
        blob = normalize_text(self.text_blob())
        return any(normalize_text(phrase) in blob for phrase in phrases)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "scrollX": self.scroll_x,
            "scrollY": self.scroll_y,
            "viewport": dict(self.viewport),
            "visibleTextExcerpt": self.visible_text_excerpt,
            "interactiveElements": [element.to_dict() for element in self.interactive_elements],
            "raw": dict(self.raw),
        }


@dataclass
class RequestTiming:
    count: int = 0
    total_seconds: float = 0.0
    errors: int = 0
    max_seconds: float = 0.0

    def add(self, duration: float, ok: bool = True) -> None:
        self.count += 1
        self.total_seconds += duration
        self.max_seconds = max(self.max_seconds, duration)
        if not ok:
            self.errors += 1

    def to_dict(self) -> Dict[str, Any]:
        average = self.total_seconds / self.count if self.count else 0.0
        return {
            "count": self.count,
            "total_seconds": round(self.total_seconds, 4),
            "average_ms": round(average * 1000.0, 2),
            "max_ms": round(self.max_seconds * 1000.0, 2),
            "errors": self.errors,
        }


class ChromeMcpClient(AbstractContextManager["ChromeMcpClient"]):
    def __init__(
        self,
        ws_url: str = DEFAULT_WS_URL,
        *,
        capabilities: Sequence[str] = DEFAULT_CAPABILITIES,
        protocol_version: str = "0.1.0",
        client_id_prefix: str = "controller",
        open_timeout: float = 10.0,
        close_timeout: float = 10.0,
        max_size: int = 8_000_000,
        request_timeout: float = 12.0,
    ) -> None:
        self.ws_url = ws_url
        self.capabilities = tuple(capabilities)
        self.protocol_version = protocol_version
        self.client_id = f"{client_id_prefix}-{secrets.token_hex(6)}"
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout
        self.max_size = max_size
        self.request_timeout = request_timeout
        self._ws: Optional[ClientConnection] = None
        self._request_sequence = 0
        self._timings: DefaultDict[str, RequestTiming] = defaultdict(RequestTiming)
        self._started_at = time.perf_counter()
        self._connected_at = 0.0

    def __enter__(self) -> "ChromeMcpClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> "ChromeMcpClient":
        if self._ws is not None:
            return self
        self._ws = connect(
            self.ws_url,
            open_timeout=self.open_timeout,
            close_timeout=self.close_timeout,
            max_size=self.max_size,
        )
        hello_id = self._next_request_id("hello")
        self._ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "id": hello_id,
                    "clientId": self.client_id,
                    "protocolVersion": self.protocol_version,
                    "capabilities": list(self.capabilities),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        response = self._recv_for(hello_id, timeout=self.request_timeout)
        if response.get("type") != "hello.ack":
            raise ChromeMcpError(f"Unexpected bridge hello response: {response!r}")
        self._connected_at = time.perf_counter()
        return self

    def close(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.close()
        finally:
            self._ws = None

    def _next_request_id(self, prefix: str) -> str:
        self._request_sequence += 1
        return f"{prefix}-{self._request_sequence}-{secrets.token_hex(4)}"

    def _ensure_connected(self) -> ClientConnection:
        if self._ws is None:
            raise ChromeMcpError("Chrome MCP client is not connected.")
        return self._ws

    def _recv_json(self, timeout: float) -> Dict[str, Any]:
        ws = self._ensure_connected()
        raw = ws.recv(timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str):
            raise ChromeMcpError(f"Unexpected bridge payload type: {type(raw)!r}")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected bridge message: {payload!r}")
        return payload

    def _recv_for(self, request_id: str, timeout: float) -> Dict[str, Any]:
        while True:
            message = self._recv_json(timeout)
            if _coerce_text(message.get("id")) == request_id:
                return message

    def request(self, message_type: str, timeout: Optional[float] = None, **payload: Any) -> Dict[str, Any]:
        ws = self._ensure_connected()
        request_id = self._next_request_id(message_type.replace(".", "-"))
        message = {"type": message_type, "id": request_id, **payload}
        started = time.perf_counter()
        ok = True
        try:
            ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            response = self._recv_for(request_id, timeout or self.request_timeout)
            ok = bool(response.get("ok", response.get("type") != "error"))
            if response.get("error"):
                detail = response.get("detail") or response.get("error")
                raise ChromeMcpError(f"{message_type} failed: {detail}")
            return response
        finally:
            self._timings[message_type].add(time.perf_counter() - started, ok=ok)

    def ping(self) -> Dict[str, Any]:
        return self.request("ping").get("payload", {})

    def list_tabs(self) -> List[BridgeTab]:
        response = self.request("tabs.list")
        payload = response.get("payload", [])
        if not isinstance(payload, list):
            raise ChromeMcpError(f"Unexpected tabs.list payload: {payload!r}")
        return [BridgeTab.from_payload(item) for item in payload if isinstance(item, Mapping)]

    def find_tab(
        self,
        *,
        url_contains: str = "",
        title_contains: str = "",
        active: Optional[bool] = None,
        tabs: Optional[Sequence[BridgeTab]] = None,
    ) -> Optional[BridgeTab]:
        haystack = list(tabs or self.list_tabs())
        url_term = normalize_text(url_contains)
        title_term = normalize_text(title_contains)
        for tab in haystack:
            if active is not None and tab.active != active:
                continue
            if url_term and url_term not in normalize_text(tab.url):
                continue
            if title_term and title_term not in normalize_text(tab.title):
                continue
            return tab
        return None

    def collect_page(
        self,
        tab_id: Any,
        *,
        scope: str = "",
        interactive_limit: int = 80,
        text_limit: int = 4000,
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = {
            "interactiveLimit": max(1, interactive_limit),
            "textLimit": max(200, text_limit),
        }
        if scope:
            options["scope"] = scope
        response = self.request("page.collect", tabId=tab_id, options=options)
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected page.collect payload: {payload!r}")
        return payload.get("state", payload)

    def read_page(
        self,
        tab_id: Any,
        *,
        filter_mode: str = "all",
        limit: int = 50,
        scope: str = "",
    ) -> BridgePageSnapshot:
        payload: Dict[str, Any] = {"tabId": tab_id, "filter": filter_mode, "limit": max(1, limit)}
        if scope:
            payload["scope"] = scope
        response = self.request("page.read", **payload)
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected page.read payload: {payload!r}")
        return BridgePageSnapshot.from_payload(payload)

    def find_elements(
        self,
        tab_id: Any,
        query: str,
        *,
        limit: int = 10,
        scope: str = "",
        exact: bool = False,
    ) -> List[BridgeElement]:
        payload: Dict[str, Any] = {
            "tabId": tab_id,
            "query": query,
            "limit": max(1, limit),
            "exact": bool(exact),
        }
        if scope:
            payload["scope"] = scope
        response = self.request("page.find", **payload)
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected page.find payload: {payload!r}")
        matches = payload.get("matches", [])
        if not isinstance(matches, list):
            raise ChromeMcpError(f"Unexpected page.find matches: {matches!r}")
        return [BridgeElement.from_payload(item) for item in matches if isinstance(item, Mapping)]

    def perform_action(self, tab_id: Any, action: Mapping[str, Any]) -> Dict[str, Any]:
        response = self.request("page.action", tabId=tab_id, action=dict(action))
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected action payload: {payload!r}")
        return payload

    def navigate(self, tab_id: Any, url: str) -> Dict[str, Any]:
        return self.perform_action(tab_id, {"kind": "navigate", "url": url})

    def screenshot(self, tab_id: Any) -> Dict[str, Any]:
        response = self.request("page.screenshot", tabId=tab_id)
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected screenshot payload: {payload!r}")
        return payload

    def execute_js(self, tab_id: Any, code: str) -> Dict[str, Any]:
        response = self.request("page.execute_js", tabId=tab_id, code=code)
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected execute_js payload: {payload!r}")
        return payload

    def scroll_feed(self, tab_id: Any, pixels: int = 3000) -> str:
        """Scroll LinkedIn's feed container. Returns the selector that was scrolled."""
        response = self.request("page.scroll_feed", tabId=tab_id, pixels=pixels)
        payload = response.get("payload", {})
        return payload.get("container", "unknown") if isinstance(payload, dict) else "unknown"

    def get_body_text(self, tab_id: Any) -> str:
        """Get full document.body.innerText with no character limit."""
        response = self.request("page.body_text", tabId=tab_id)
        payload = response.get("payload", {})
        return payload.get("text", "") if isinstance(payload, dict) else ""

    def expand_posts(self, tab_id: Any) -> int:
        """Click all 'see more' buttons in LinkedIn feed. Returns number clicked."""
        response = self.request("page.expand_posts", tabId=tab_id)
        payload = response.get("payload", {})
        return int(payload.get("clicked", 0)) if isinstance(payload, dict) else 0

    def wait_for_element(
        self,
        tab_id: Any,
        query: str,
        *,
        scope: str = "dialog-interactive",
        timeout_ms: int = 10000,
        exact: bool = False,
    ) -> Dict[str, Any]:
        response = self.request(
            "page.wait",
            tabId=tab_id,
            query=query,
            scope=scope,
            timeout_ms=max(500, min(timeout_ms, 60000)),
            exact=exact,
        )
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise ChromeMcpError(f"Unexpected page.wait payload: {payload!r}")
        return payload

    def get_console_logs(
        self,
        tab_id: Any,
        *,
        limit: int = 50,
        level: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return captured console logs from the page (log/info/warn/error/debug)."""
        payload: Dict[str, Any] = {"tabId": tab_id, "limit": max(1, limit)}
        if level:
            payload["level"] = level.strip().lower()
        response = self.request("page.console", **payload)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise ChromeMcpError(f"Unexpected page.console payload: {result!r}")
        return result

    def clear_console_logs(self, tab_id: Any) -> None:
        """Clear the console log buffer for the given tab."""
        self.request("page.console.clear", tabId=tab_id)

    def get_network_requests(
        self,
        tab_id: Any = None,
        *,
        limit: int = 50,
        event: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return captured network requests/responses (event='request'|'response'|'error')."""
        payload: Dict[str, Any] = {"limit": max(1, limit)}
        if tab_id is not None:
            payload["tabId"] = tab_id
        if event:
            payload["event"] = event.strip().lower()
        response = self.request("page.network", **payload)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise ChromeMcpError(f"Unexpected page.network payload: {result!r}")
        return result

    def clear_network_log(self) -> None:
        """Clear the network request buffer."""
        self.request("page.network.clear")

    def get_page_text(
        self,
        tab_id: Any,
        *,
        selector: Optional[str] = None,
        limit: int = 20000,
    ) -> Dict[str, Any]:
        """Return clean plain text from the page (or a specific element via CSS selector)."""
        payload: Dict[str, Any] = {"tabId": tab_id, "limit": max(200, limit)}
        if selector:
            payload["selector"] = selector
        response = self.request("page.text", **payload)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise ChromeMcpError(f"Unexpected page.text payload: {result!r}")
        return result

    def create_tab(self, url: str = "about:blank", *, active: bool = True) -> Dict[str, Any]:
        """Open a new browser tab and return its tab info."""
        response = self.request("tabs.create", url=url, active=active)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise ChromeMcpError(f"Unexpected tabs.create payload: {result!r}")
        return result

    def close_tab(self, tab_id: Any) -> None:
        """Close the specified tab."""
        self.request("tabs.close", tabId=tab_id)

    def activate_tab(self, tab_id: Any) -> None:
        """Switch focus to the specified tab."""
        self.request("tabs.activate", tabId=tab_id)

    def timing_summary(self) -> Dict[str, Any]:
        elapsed = time.perf_counter() - self._started_at
        connected_for = time.perf_counter() - self._connected_at if self._connected_at else 0.0
        return {
            "ws_url": self.ws_url,
            "connected": self._ws is not None,
            "elapsed_seconds": round(elapsed, 3),
            "connected_seconds": round(connected_for, 3),
            "requests": {name: timing.to_dict() for name, timing in sorted(self._timings.items())},
        }
