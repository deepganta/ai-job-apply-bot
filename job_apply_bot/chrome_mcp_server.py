import asyncio
import base64
import hashlib
import json
import secrets
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .config import Settings
from .utils import compact_text


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _request_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _normalized_text(value: Any, limit: int = 250) -> str:
    return compact_text(_coerce_text(value).lower(), limit=limit)


def _element_fields(element: Dict[str, Any]) -> List[str]:
    ordered_keys = (
        "label",
        "ariaLabel",
        "text",
        "placeholder",
        "name",
        "value",
        "role",
        "tagName",
        "type",
        "href",
    )
    values: List[str] = []
    for key in ordered_keys:
        candidate = _normalized_text(element.get(key))
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _summarize_element(element: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "id",
        "tagName",
        "type",
        "role",
        "label",
        "name",
        "ariaLabel",
        "placeholder",
        "text",
        "value",
        "checked",
        "required",
        "disabled",
        "href",
        "x",
        "y",
        "width",
        "height",
    )
    return {key: element.get(key) for key in keys}


def _score_element_match(query: str, element: Dict[str, Any]) -> int:
    query_text = _normalized_text(query)
    if not query_text:
        return 0

    fields = _element_fields(element)
    if not fields:
        return 0

    score = 0
    if any(query_text == field for field in fields):
        score += 120
    elif any(query_text in field for field in fields):
        score += 80

    for term in {part for part in query_text.split() if part}:
        if any(term == field for field in fields):
            score += 18
        elif any(term in field for field in fields):
            score += 8

    if element.get("disabled"):
        score -= 4

    return score


def _exact_element_match(query: str, element: Dict[str, Any]) -> bool:
    query_text = _normalized_text(query)
    if not query_text:
        return False
    return any(query_text == field for field in _element_fields(element))


@dataclass
class ChromeBridgeClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connected_at: float
    client_id: str = ""
    role: str = "unknown"
    protocol_version: str = ""
    capabilities: List[str] = field(default_factory=list)
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # URLs of tabs last reported by this extension client — used to prefer
    # the bot Chrome extension over any other Chrome that also connects.
    tab_urls: List[str] = field(default_factory=list)


class ChromeMcpBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.host = settings.chrome_mcp_host
        self.port = settings.chrome_mcp_port
        self.extension_dir = settings.chrome_mcp_extension_dir
        self.clients: Dict[str, ChromeBridgeClient] = {}
        self.tab_cache: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._action_log: Deque[Dict[str, Any]] = deque(maxlen=250)
        self._server: Optional[asyncio.AbstractServer] = None
        self._lock = asyncio.Lock()

    async def serve_forever(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        bind_host = host or self.host
        bind_port = port or self.port
        self._server = await asyncio.start_server(self._handle_connection, bind_host, bind_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        print(f"Chrome MCP bridge listening on ws://{bind_host}:{bind_port}")
        if sockets:
            print(f"Bound sockets: {sockets}")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for client in list(self.clients.values()):
            try:
                client.writer.close()
                await client.writer.wait_closed()
            except Exception:
                continue

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fallback_id = f"socket-{uuid.uuid4().hex[:8]}"
        client = ChromeBridgeClient(reader=reader, writer=writer, connected_at=time.time(), client_id=fallback_id)
        self.clients[fallback_id] = client
        try:
            request_line, headers = await self._read_http_request(reader)
            if not self._is_websocket_upgrade(request_line, headers):
                await self._handle_http_request(reader, writer, request_line, headers)
                return

            accept_key = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode("utf-8")).digest()
            ).decode("ascii")
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_key}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()

            while True:
                message = await self._read_ws_message(reader)
                if message is None:
                    break
                await self._handle_message(client, fallback_id, message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            await self._disconnect_client(client.client_id or fallback_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_message(self, client: ChromeBridgeClient, fallback_id: str, message: Dict[str, Any]) -> None:
        message_type = _coerce_text(message.get("type")).strip()
        request_id = _coerce_text(message.get("id") or message.get("requestId")).strip()

        self._action_log.append(
            {
                "timestamp": _now_iso(),
                "direction": "browser->bridge",
                "type": message_type,
                "id": request_id,
                "client_id": client.client_id or fallback_id,
            }
        )

        if message_type == "hello":
            client.client_id = _coerce_text(message.get("clientId") or fallback_id).strip() or fallback_id
            client.protocol_version = _coerce_text(message.get("protocolVersion"))
            client.capabilities = [str(item).strip() for item in message.get("capabilities", []) if str(item).strip()]
            client.role = self._infer_role(client)
            self.clients[client.client_id] = client
            await self._send_json(
                client,
                {
                    "type": "hello.ack",
                    "id": request_id or _request_id("hello"),
                    "clientId": client.client_id,
                    "serverTime": _now_iso(),
                },
            )
            # Proactively fetch tabs so _pick_client can use tab_urls immediately.
            if client.role == "extension":
                asyncio.get_event_loop().create_task(self._probe_extension_tabs(client.client_id))
            return

        if message_type == "tabs.list.result":
            payload = message.get("payload", [])
            if isinstance(payload, list):
                urls: List[str] = []
                for tab in payload:
                    tab_id = _coerce_text(tab.get("id") or tab.get("tabId"))
                    url = _coerce_text(tab.get("url"))
                    if tab_id:
                        self.tab_cache[tab_id] = {
                            "tabId": tab_id,
                            "url": url,
                            "title": _coerce_text(tab.get("title")),
                            "active": bool(tab.get("active", False)),
                            "windowId": tab.get("windowId"),
                            "timestamp": _now_iso(),
                        }
                    if url:
                        urls.append(url)
                # Keep the reporting client's tab list current so _pick_client
                # can prefer the Chrome instance that has LinkedIn open.
                client.tab_urls = urls

        if message_type == "page.state":
            payload = message.get("payload", {})
            if isinstance(payload, dict):
                tab_id = _coerce_text(payload.get("tabId") or message.get("tabId"))
                if tab_id:
                    self.tab_cache[tab_id] = payload

        if request_id and request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                future.set_result(message)
            return

        if client.role == "controller":
            response = await self._handle_controller_request(message)
            if response is not None:
                await self._send_json(client, response)
            return

    def _infer_role(self, client: ChromeBridgeClient) -> str:
        if client.client_id.startswith("chrome-mcp-"):
            return "extension"
        if any(capability.startswith("page.action.") for capability in client.capabilities):
            return "extension"
        return "controller"

    async def _handle_controller_request(self, message: Dict[str, Any]) -> Dict[str, Any]:
        message_type = _coerce_text(message.get("type")).strip()
        request_id = _coerce_text(message.get("id") or message.get("requestId")).strip() or _request_id("rpc")

        if message_type == "ping":
            return {
                "type": "pong",
                "id": request_id,
                "ok": True,
                "payload": {"kind": "pong", "serverTime": _now_iso()},
            }

        try:
            if message_type == "tabs.list":
                payload = await self.list_tabs()
                return {
                    "type": "tabs.list.result",
                    "id": request_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.collect":
                tab_id = message.get("tabId")
                options = message.get("options", {})
                payload = await self.collect_page(tab_id, options=options if isinstance(options, dict) else {})
                return {
                    "type": "page.state",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": {"state": payload},
                }

            if message_type == "page.read":
                tab_id = message.get("tabId")
                filter_mode = _coerce_text(message.get("filter") or "all").strip().lower() or "all"
                limit = max(1, min(int(message.get("limit") or 50), 250))
                scope = _coerce_text(message.get("scope") or "").strip().lower()
                payload = await self.read_page(tab_id, filter_mode=filter_mode, limit=limit, scope=scope)
                return {
                    "type": "page.read.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.find":
                tab_id = message.get("tabId")
                query = _coerce_text(message.get("query"))
                limit = max(1, min(int(message.get("limit") or 10), 50))
                scope = _coerce_text(message.get("scope") or "").strip().lower()
                exact = bool(message.get("exact", False))
                started_at = time.perf_counter()
                payload = await self.find_elements(tab_id, query=query, limit=limit, scope=scope, exact=exact)
                return {
                    "type": "page.find.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": {"query": query, "matches": payload, "elapsedMs": round((time.perf_counter() - started_at) * 1000.0, 1)},
                }

            if message_type == "page.action":
                tab_id = message.get("tabId")
                payload = await self.perform_action(tab_id, message.get("action", {}) or {})
                return {
                    "type": "action.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": bool(payload.get("ok", False)),
                    "payload": payload,
                }

            if message_type == "page.screenshot":
                tab_id = message.get("tabId")
                payload = await self.screenshot(tab_id)
                return {
                    "type": "page.screenshot.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.execute_js":
                tab_id = message.get("tabId")
                code = _coerce_text(message.get("code") or "")
                payload = await self.execute_js(tab_id, code)
                return {
                    "type": "page.execute_js.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.wait":
                tab_id = message.get("tabId")
                query = _coerce_text(message.get("query") or "")
                scope = _coerce_text(message.get("scope") or "dialog-interactive").strip().lower()
                timeout_ms = int(message.get("timeout_ms") or 10000)
                exact = bool(message.get("exact", False))
                element = await self.wait_for_element(
                    tab_id, query=query, scope=scope, timeout=timeout_ms / 1000.0, exact=exact
                )
                return {
                    "type": "page.wait.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": {"found": element is not None, "element": element, "query": query},
                }

            if message_type == "page.console":
                tab_id = message.get("tabId")
                limit = max(1, min(int(message.get("limit") or 50), 200))
                level = _coerce_text(message.get("level") or "").strip().lower() or None
                payload = await self.get_console_logs(tab_id, limit=limit, level=level)
                return {
                    "type": "page.console.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.console.clear":
                tab_id = message.get("tabId")
                await self.clear_console_logs(tab_id)
                return {"type": "page.console.clear.result", "id": request_id, "ok": True}

            if message_type == "page.network":
                tab_id = message.get("tabId")
                limit = max(1, min(int(message.get("limit") or 50), 300))
                event_filter = _coerce_text(message.get("event") or "").strip().lower() or None
                payload = await self.get_network_requests(tab_id, limit=limit, event_filter=event_filter)
                return {
                    "type": "page.network.result",
                    "id": request_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "page.network.clear":
                await self.clear_network_log()
                return {"type": "page.network.clear.result", "id": request_id, "ok": True}

            if message_type == "page.text":
                tab_id = message.get("tabId")
                selector = _coerce_text(message.get("selector") or "").strip() or None
                limit = max(200, min(int(message.get("limit") or 20000), 100000))
                payload = await self.get_page_text(tab_id, selector=selector, limit=limit)
                return {
                    "type": "page.text.result",
                    "id": request_id,
                    "tabId": tab_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "tabs.create":
                url = _coerce_text(message.get("url") or "about:blank").strip()
                active = bool(message.get("active", True))
                payload = await self.create_tab(url=url, active=active)
                return {
                    "type": "tabs.create.result",
                    "id": request_id,
                    "ok": True,
                    "payload": payload,
                }

            if message_type == "tabs.close":
                tab_id = message.get("tabId")
                await self.close_tab(tab_id)
                return {"type": "tabs.close.result", "id": request_id, "ok": True, "payload": {"tabId": tab_id}}

            if message_type == "tabs.activate":
                tab_id = message.get("tabId")
                await self.activate_tab(tab_id)
                return {"type": "tabs.activate.result", "id": request_id, "ok": True, "payload": {"tabId": tab_id}}

        except Exception as exc:
            return {
                "type": "error",
                "id": request_id,
                "error": "bridge_request_failed",
                "detail": str(exc),
            }

        return {
            "type": "error",
            "id": request_id,
            "error": "unsupported_type",
            "detail": message_type,
        }

    async def _probe_extension_tabs(self, client_id: str) -> None:
        """Background task: fetch tabs from a newly-connected extension so that
        _pick_client can use tab_urls for routing before the first controller request."""
        await asyncio.sleep(1.0)  # let the extension settle after hello
        try:
            response = await self._request({"type": "tabs.list"}, client_id=client_id, timeout=6.0)
            # tab_urls are populated by the tabs.list.result handler above.
            _ = response
        except Exception:
            pass

    async def _request(self, payload: Dict[str, Any], client_id: str = "", timeout: float = 12.0) -> Dict[str, Any]:
        client = self._pick_client(client_id)
        request_id = _coerce_text(payload.get("id")).strip() or _request_id(payload.get("type", "req"))
        payload = {**payload, "id": request_id}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_json(client, payload)
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending.pop(request_id, None)

    def _pick_client(self, client_id: str = "", preferred_url: str = "") -> ChromeBridgeClient:
        if client_id:
            client = self.clients.get(client_id)
            if client and client.role == "extension" and not client.writer.is_closing():
                return client
            raise RuntimeError(f"Chrome bridge client '{client_id}' is not connected.")

        live_clients = [
            client
            for client in self.clients.values()
            if client.client_id and client.role == "extension" and not client.writer.is_closing()
        ]
        if not live_clients:
            raise RuntimeError("No Chrome MCP extension client is connected.")

        # De-duplicate by client_id (service workers may register twice).
        seen: Dict[str, "ChromeBridgeClient"] = {}
        for c in live_clients:
            existing = seen.get(c.client_id)
            if existing is None or c.connected_at > existing.connected_at:
                seen[c.client_id] = c
        live_clients = list(seen.values())

        # Prefer the Chrome whose tabs include a LinkedIn jobs URL — this is
        # the bot Chrome, not the Claude-in-Chrome extension that may also
        # connect to this bridge server.
        hint = preferred_url or "linkedin.com/jobs"
        linkedin_clients = [
            c for c in live_clients
            if any(hint in url for url in c.tab_urls)
        ]
        if linkedin_clients:
            return sorted(linkedin_clients, key=lambda item: item.connected_at)[-1]

        # Fallback: most-recently-connected live extension.
        return sorted(live_clients, key=lambda item: item.connected_at)[-1]

    async def ping(self, client_id: str = "") -> Dict[str, Any]:
        try:
            return await self._request({"type": "ping"}, client_id=client_id)
        except RuntimeError:
            return {"type": "pong", "ok": True, "serverTime": _now_iso()}

    async def list_tabs(self, client_id: str = "") -> List[Dict[str, Any]]:
        try:
            response = await self._request({"type": "tabs.list"}, client_id=client_id)
        except RuntimeError:
            return []
        payload = response.get("payload", [])
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected tabs.list payload: {payload!r}")
        return payload

    async def collect_page(self, tab_id: Any, client_id: str = "", options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        started_at = time.perf_counter()
        collect_options = options if isinstance(options, dict) else {}
        try:
            response = await self._request(
                {"type": "page.collect", "tabId": tab_id, "options": collect_options},
                client_id=client_id,
            )
        except RuntimeError:
            cached = self.tab_cache.get(_coerce_text(tab_id), {})
            if cached:
                return cached
            raise
        payload = response.get("payload", {})
        if isinstance(payload, dict) and "state" in payload:
            payload = payload.get("state", {})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected page.collect payload: {payload!r}")
        tab_key = _coerce_text(payload.get("tabId") or tab_id)
        if tab_key:
            self.tab_cache[tab_key] = payload
        payload["elapsedMs"] = round((time.perf_counter() - started_at) * 1000.0, 1)
        return payload

    async def read_page(
        self,
        tab_id: Any,
        filter_mode: str = "all",
        limit: int = 50,
        scope: str = "",
        client_id: str = "",
    ) -> Dict[str, Any]:
        collect_scope = scope or ("dialog-interactive" if filter_mode == "interactive" else "page")
        state = await self.collect_page(
            tab_id,
            client_id=client_id,
            options={
                "scope": collect_scope,
                "interactiveLimit": limit,
                "textLimit": 4000 if collect_scope == "page" else 2200,
            },
        )
        if filter_mode == "interactive":
            interactive = state.get("interactiveElements", [])
            if not isinstance(interactive, list):
                interactive = []
            return {
                "url": state.get("url", ""),
                "title": state.get("title", ""),
                "scope": state.get("scope", collect_scope),
                "scrollX": state.get("scrollX", 0),
                "scrollY": state.get("scrollY", 0),
                "viewport": state.get("viewport", {}),
                "visibleTextExcerpt": compact_text(_coerce_text(state.get("visibleTextExcerpt")), limit=1200),
                "activeDialog": state.get("activeDialog"),
                "interactiveElements": [_summarize_element(element) for element in interactive[:limit]],
                "elapsedMs": state.get("elapsedMs", 0),
            }
        return state

    async def find_elements(
        self,
        tab_id: Any,
        query: str,
        limit: int = 10,
        scope: str = "",
        exact: bool = False,
        client_id: str = "",
    ) -> List[Dict[str, Any]]:
        state = await self.collect_page(
            tab_id,
            client_id=client_id,
            options={
                "scope": scope or "dialog-interactive",
                "interactiveLimit": max(limit * 2, 20),
                "textLimit": 2000,
            },
        )
        interactive = state.get("interactiveElements", [])
        if not isinstance(interactive, list):
            interactive = []

        ranked: List[Dict[str, Any]] = []
        for element in interactive:
            if not isinstance(element, dict):
                continue
            score = 120 if (_exact_element_match(query, element) if exact else False) else (_score_element_match(query, element) if not exact else 0)
            if score <= 0:
                continue
            ranked.append({**_summarize_element(element), "score": score})

        ranked.sort(key=lambda item: (-int(item.get("score", 0)), int(item.get("y", 0)), int(item.get("x", 0))))
        return ranked[:limit]

    async def perform_action(self, tab_id: Any, action: Dict[str, Any], client_id: str = "") -> Dict[str, Any]:
        started_at = time.perf_counter()
        response = await self._request(
            {
                "type": "page.action",
                "tabId": tab_id,
                "action": action,
            },
            client_id=client_id,
        )
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected action payload: {payload!r}")
        payload["elapsedMs"] = round((time.perf_counter() - started_at) * 1000.0, 1)
        return payload

    async def screenshot(self, tab_id: Any, client_id: str = "") -> Dict[str, Any]:
        response = await self._request(
            {"type": "page.screenshot", "tabId": tab_id},
            client_id=client_id,
        )
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected screenshot payload: {payload!r}")
        return payload

    async def execute_js(self, tab_id: Any, code: str, client_id: str = "") -> Dict[str, Any]:
        response = await self._request(
            {"type": "page.execute_js", "tabId": tab_id, "code": code},
            client_id=client_id,
        )
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected execute_js payload: {payload!r}")
        return payload

    async def wait_for_element(
        self,
        tab_id: Any,
        query: str,
        scope: str = "dialog-interactive",
        timeout: float = 10.0,
        poll_interval: float = 0.5,
        exact: bool = False,
        client_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                elements = await self.find_elements(
                    tab_id, query=query, limit=1, scope=scope, exact=exact, client_id=client_id
                )
                if elements:
                    return elements[0]
            except Exception:
                pass
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
        return None

    async def get_console_logs(
        self,
        tab_id: Any,
        limit: int = 50,
        level: Optional[str] = None,
        client_id: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"tabId": tab_id, "limit": limit}
        if level:
            payload["level"] = level
        response = await self._request({"type": "page.console", **payload}, client_id=client_id)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected page.console payload: {result!r}")
        return result

    async def clear_console_logs(self, tab_id: Any, client_id: str = "") -> None:
        await self._request({"type": "page.console.clear", "tabId": tab_id}, client_id=client_id)

    async def get_network_requests(
        self,
        tab_id: Any = None,
        limit: int = 50,
        event_filter: Optional[str] = None,
        client_id: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"limit": limit}
        if tab_id is not None:
            payload["tabId"] = tab_id
        if event_filter:
            payload["event"] = event_filter
        response = await self._request({"type": "page.network", **payload}, client_id=client_id)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected page.network payload: {result!r}")
        return result

    async def clear_network_log(self, client_id: str = "") -> None:
        await self._request({"type": "page.network.clear"}, client_id=client_id)

    async def get_page_text(
        self,
        tab_id: Any,
        selector: Optional[str] = None,
        limit: int = 20000,
        client_id: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"tabId": tab_id, "limit": limit}
        if selector:
            payload["selector"] = selector
        response = await self._request({"type": "page.text", **payload}, client_id=client_id)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected page.text payload: {result!r}")
        return result

    async def create_tab(self, url: str = "about:blank", active: bool = True, client_id: str = "") -> Dict[str, Any]:
        response = await self._request({"type": "tabs.create", "url": url, "active": active}, client_id=client_id)
        result = response.get("payload", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected tabs.create payload: {result!r}")
        return result

    async def close_tab(self, tab_id: Any, client_id: str = "") -> None:
        await self._request({"type": "tabs.close", "tabId": tab_id}, client_id=client_id)

    async def activate_tab(self, tab_id: Any, client_id: str = "") -> None:
        await self._request({"type": "tabs.activate", "tabId": tab_id}, client_id=client_id)

    def connection_snapshot(self) -> List[Dict[str, Any]]:
        snapshot: List[Dict[str, Any]] = []
        for client in sorted(self.clients.values(), key=lambda item: item.connected_at):
            if not client.client_id:
                continue
            snapshot.append(
                {
                    "client_id": client.client_id,
                    "role": client.role,
                    "protocol_version": client.protocol_version,
                    "capabilities": client.capabilities,
                    "connected_at": client.connected_at,
                }
            )
        return snapshot

    async def _disconnect_client(self, client_id: str) -> None:
        client = self.clients.pop(client_id, None)
        if client is None:
            return
        for pending in list(client.pending.values()):
            if not pending.done():
                pending.set_exception(ConnectionError("Chrome bridge client disconnected"))
        for request_id, pending in list(self._pending.items()):
            if pending.done():
                continue
            pending.set_exception(ConnectionError("Chrome bridge client disconnected"))
            self._pending.pop(request_id, None)

    async def _send_json(self, client: ChromeBridgeClient, payload: Dict[str, Any]) -> None:
        async with client.write_lock:
            await self._write_ws_frame(client.writer, _json_dumps(payload))

    async def _read_http_request(self, reader: asyncio.StreamReader) -> tuple[str, Dict[str, str]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        text = raw.decode("latin1")
        lines = text.split("\r\n")
        request_line = lines[0]
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return request_line, headers

    def _is_websocket_upgrade(self, request_line: str, headers: Dict[str, str]) -> bool:
        if not request_line.startswith("GET "):
            return False
        upgrade = headers.get("upgrade", "").lower()
        connection = headers.get("connection", "").lower()
        key = headers.get("sec-websocket-key", "").strip()
        return upgrade == "websocket" and "upgrade" in connection and bool(key)

    async def _handle_http_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request_line: str,
        headers: Dict[str, str],
    ) -> None:
        parts = request_line.split(" ", 2)
        method = parts[0].upper() if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        # Strip query string
        path = path.split("?")[0]

        # CORS preflight
        if method == "OPTIONS":
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                b"Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            return

        # Read request body
        body: Dict[str, Any] = {}
        content_length = 0
        try:
            content_length = int(headers.get("content-length", 0) or 0)
        except (ValueError, TypeError):
            content_length = 0
        if content_length > 0:
            try:
                raw_body = await reader.readexactly(content_length)
                body = json.loads(raw_body.decode("utf-8", "replace"))
                if not isinstance(body, dict):
                    body = {}
            except Exception:
                body = {}

        # Route API requests
        if path.startswith("/api") or path == "/":
            response = await self._route_http_api(path, method, body)
            encoded = _json_dumps(response).encode("utf-8")
            status_line = "200 OK" if response.get("ok", True) else "400 Bad Request"
            writer.write(
                (
                    f"HTTP/1.1 {status_line}\r\n"
                    "Content-Type: application/json; charset=utf-8\r\n"
                    f"Content-Length: {len(encoded)}\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    "Cache-Control: no-cache\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
                + encoded
            )
            await writer.drain()
            return

        # Non-API HTTP request: return upgrade hint
        plain = b"Chrome MCP bridge is running. Use /api for HTTP access or connect via WebSocket.\n"
        writer.write(
            (
                "HTTP/1.1 426 Upgrade Required\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(plain)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + plain
        )
        await writer.drain()

    async def _route_http_api(self, path: str, method: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if path in ("/", "/api", "/api/"):
            snapshot = self.connection_snapshot()
            return {
                "ok": True,
                "name": "Chrome MCP Bridge",
                "version": "0.6.0",
                "ws_url": f"ws://{self.host}:{self.port}",
                "http_url": f"http://{self.host}:{self.port}",
                "connected_clients": len(snapshot),
                "endpoints": [
                    "GET  /api/status",
                    "GET  /api/tabs",
                    "POST /api/read          {tabId, filter?, limit?, scope?}",
                    "POST /api/find          {tabId, query, limit?, scope?, exact?}",
                    "POST /api/action        {tabId, action: {kind, targetId?, query?, value?, url?, text?, ...}}",
                    "POST /api/navigate      {tabId, url}",
                    "POST /api/screenshot    {tabId}",
                    "POST /api/wait          {tabId, query, scope?, timeout_ms?, exact?}",
                    "POST /api/execute_js    {tabId, code}",
                    "POST /api/console       {tabId, limit?, level?}",
                    "POST /api/console/clear {tabId}",
                    "POST /api/network       {tabId?, limit?, event?}",
                    "POST /api/network/clear {}",
                    "POST /api/text          {tabId, selector?, limit?}",
                    "POST /api/tabs/create   {url?, active?}",
                    "POST /api/tabs/close    {tabId}",
                    "POST /api/tabs/activate {tabId}",
                ],
            }

        if path in ("/api/status", "/api/ping"):
            snapshot = self.connection_snapshot()
            return {
                "ok": True,
                "server_time": _now_iso(),
                "connected_clients": len(snapshot),
                "clients": snapshot,
                "tab_cache_size": len(self.tab_cache),
            }

        if path == "/api/tabs":
            try:
                tabs = await self.list_tabs()
                return {"ok": True, "tabs": tabs}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/read":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            filter_mode = _coerce_text(body.get("filter") or "all").strip().lower() or "all"
            limit = max(1, min(int(body.get("limit") or 50), 250))
            scope = _coerce_text(body.get("scope") or "").strip().lower()
            try:
                result = await self.read_page(tab_id, filter_mode=filter_mode, limit=limit, scope=scope)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/find":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            query = _coerce_text(body.get("query") or "")
            if not query:
                return {"ok": False, "error": "query is required"}
            limit = max(1, min(int(body.get("limit") or 10), 50))
            scope = _coerce_text(body.get("scope") or "").strip().lower()
            exact = bool(body.get("exact", False))
            try:
                matches = await self.find_elements(tab_id, query=query, limit=limit, scope=scope, exact=exact)
                return {"ok": True, "query": query, "matches": matches}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path in ("/api/action", "/api/click", "/api/set_value", "/api/scroll"):
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            action = body.get("action") or {}
            if not isinstance(action, dict):
                action = {}
            if path == "/api/click" and "kind" not in action:
                action = {**action, "kind": "click"}
            if path == "/api/set_value" and "kind" not in action:
                action = {**action, "kind": "setValue"}
            if path == "/api/scroll" and "kind" not in action:
                action = {**action, "kind": "scroll"}
            try:
                result = await self.perform_action(tab_id, action)
                return {"ok": bool(result.get("ok", False)), **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/navigate":
            tab_id = body.get("tabId")
            url = _coerce_text(body.get("url") or "").strip()
            if not tab_id:
                return {"ok": False, "error": "tabId is required"}
            if not url:
                return {"ok": False, "error": "url is required"}
            try:
                result = await self.perform_action(tab_id, {"kind": "navigate", "url": url})
                return {"ok": bool(result.get("ok", False)), **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/screenshot":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            try:
                result = await self.screenshot(tab_id)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/wait":
            tab_id = body.get("tabId")
            query = _coerce_text(body.get("query") or "")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            if not query:
                return {"ok": False, "error": "query is required"}
            scope = _coerce_text(body.get("scope") or "dialog-interactive").strip().lower()
            timeout_ms = max(500, min(int(body.get("timeout_ms") or 10000), 60000))
            exact = bool(body.get("exact", False))
            try:
                element = await self.wait_for_element(
                    tab_id, query=query, scope=scope, timeout=timeout_ms / 1000.0, exact=exact
                )
                return {"ok": True, "found": element is not None, "element": element, "query": query}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/execute_js":
            tab_id = body.get("tabId")
            code = _coerce_text(body.get("code") or "")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            if not code:
                return {"ok": False, "error": "code is required"}
            try:
                result = await self.execute_js(tab_id, code)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/console":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            limit = max(1, min(int(body.get("limit") or 50), 200))
            level = _coerce_text(body.get("level") or "").strip().lower() or None
            try:
                result = await self.get_console_logs(tab_id, limit=limit, level=level)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/console/clear":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            try:
                await self.clear_console_logs(tab_id)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/network":
            tab_id = body.get("tabId")
            limit = max(1, min(int(body.get("limit") or 50), 300))
            event_filter = _coerce_text(body.get("event") or "").strip().lower() or None
            try:
                result = await self.get_network_requests(tab_id, limit=limit, event_filter=event_filter)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/network/clear":
            try:
                await self.clear_network_log()
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/text":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            selector = _coerce_text(body.get("selector") or "").strip() or None
            limit = max(200, min(int(body.get("limit") or 20000), 100000))
            try:
                result = await self.get_page_text(tab_id, selector=selector, limit=limit)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/tabs/create":
            url = _coerce_text(body.get("url") or "about:blank").strip()
            active = bool(body.get("active", True))
            try:
                result = await self.create_tab(url=url, active=active)
                return {"ok": True, **result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/tabs/close":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            try:
                await self.close_tab(tab_id)
                return {"ok": True, "tabId": tab_id}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if path == "/api/tabs/activate":
            tab_id = body.get("tabId")
            if tab_id is None:
                return {"ok": False, "error": "tabId is required"}
            try:
                await self.activate_tab(tab_id)
                return {"ok": True, "tabId": tab_id}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": "not_found", "path": path, "available": "/api"}

    async def _read_ws_message(self, reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
        header = await reader.readexactly(2)
        first, second = header[0], header[1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        payload_len = second & 0x7F
        if opcode == 0x8:
            return None
        if payload_len == 126:
            payload_len = struct.unpack("!H", await reader.readexactly(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask = await reader.readexactly(4) if masked else None
        payload = await reader.readexactly(payload_len) if payload_len else b""
        if mask is not None:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x9:
            return {"type": "ping", "payload": payload.decode("utf-8", "ignore")}
        if opcode != 0x1:
            return {"type": "binary", "payload": payload.decode("utf-8", "ignore")}
        text = payload.decode("utf-8", "replace")
        if not text.strip():
            return None
        return json.loads(text)

    async def _write_ws_frame(self, writer: asyncio.StreamWriter, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytearray([0x80 | 0x1])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        writer.write(header + data)
        await writer.drain()


def extension_path(settings: Settings) -> Path:
    return settings.chrome_mcp_extension_dir


def run_server(settings: Settings, host: str = "", port: int = 0) -> None:
    if host or port:
        settings = replace(
            settings,
            chrome_mcp_host=host or settings.chrome_mcp_host,
            chrome_mcp_port=port or settings.chrome_mcp_port,
        )
    asyncio.run(ChromeMcpBridge(settings).serve_forever())
