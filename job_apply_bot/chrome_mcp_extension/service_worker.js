const DEFAULT_BRIDGE_URL = "ws://127.0.0.1:8765";
const PROTOCOL_VERSION = "0.1.0";

let socket = null;
let reconnectTimer = null;
let reconnectDelayMs = 1000;
let bridgeUrl = DEFAULT_BRIDGE_URL;
let clientId = `chrome-mcp-${Math.random().toString(36).slice(2)}`;

// ── Network request capture ───────────────────────────────────────────────────
const NETWORK_BUFFER_SIZE = 300;
const networkBuffer = [];

function pushNetworkEntry(entry) {
  networkBuffer.push(entry);
  if (networkBuffer.length > NETWORK_BUFFER_SIZE) {
    networkBuffer.shift();
  }
}

if (chrome.webRequest) {
  chrome.webRequest.onBeforeRequest.addListener(
    (details) => {
      pushNetworkEntry({
        event: "request",
        requestId: details.requestId,
        url: details.url,
        method: details.method,
        tabId: details.tabId,
        type: details.type,
        timestamp: Date.now(),
      });
    },
    { urls: ["<all_urls>"] }
  );

  chrome.webRequest.onCompleted.addListener(
    (details) => {
      pushNetworkEntry({
        event: "response",
        requestId: details.requestId,
        url: details.url,
        statusCode: details.statusCode,
        statusLine: details.statusLine || "",
        tabId: details.tabId,
        type: details.type,
        timestamp: Date.now(),
      });
    },
    { urls: ["<all_urls>"] }
  );

  chrome.webRequest.onErrorOccurred.addListener(
    (details) => {
      pushNetworkEntry({
        event: "error",
        requestId: details.requestId,
        url: details.url,
        error: details.error,
        tabId: details.tabId,
        type: details.type,
        timestamp: Date.now(),
      });
    },
    { urls: ["<all_urls>"] }
  );
}

function log(...args) {
  console.log("[chrome-mcp]", ...args);
}

async function loadBridgeUrl() {
  const result = await chrome.storage.local.get(["bridgeUrl"]);
  const candidate = String(result.bridgeUrl || "").trim();
  if (candidate) {
    bridgeUrl = candidate;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectBridge().catch((error) => log("reconnect failed", error));
  }, reconnectDelayMs);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 15000);
}

async function connectBridge() {
  await loadBridgeUrl();
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  log("connecting", bridgeUrl);
  socket = new WebSocket(bridgeUrl);
  socket.onopen = () => {
    reconnectDelayMs = 1000;
    socket.send(JSON.stringify({
      type: "hello",
      clientId,
      protocolVersion: PROTOCOL_VERSION,
      capabilities: [
        "page.collect",
        "page.action.click",
        "page.action.setValue",
        "page.action.type",
        "page.action.upload_file",
        "page.action.scroll",
        "page.action.navigate",
        "page.console",
        "page.network",
        "page.text",
        "tabs.list",
        "tabs.create",
        "tabs.close",
        "tabs.activate",
        "ping",
      ],
    }));
    log("connected");
  };

  socket.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      socket.send(JSON.stringify({
        type: "error",
        error: "invalid_json_from_bridge",
        detail: String(error),
      }));
      return;
    }

    try {
      const response = await handleBridgeMessage(message);
      if (response) {
        socket.send(JSON.stringify(response));
      }
    } catch (error) {
      socket.send(JSON.stringify({
        type: "error",
        id: message?.id || null,
        error: "bridge_request_failed",
        detail: String(error),
      }));
    }
  };

  socket.onerror = (error) => {
    log("socket error", error);
  };

  socket.onclose = () => {
    log("disconnected");
    socket = null;
    scheduleReconnect();
  };
}

async function sendToTab(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message);
}

function normalizeTabId(tabId) {
  const numeric = Number(tabId);
  return Number.isFinite(numeric) ? numeric : tabId;
}

async function handleBridgeMessage(message) {
  const { type, id, tabId } = message || {};
  if (!type) {
    return {
      type: "error",
      id: id || null,
      error: "missing_type",
    };
  }

  if (type === "ping") {
    return {
      type: "pong",
      id: id || null,
      clientId,
      protocolVersion: PROTOCOL_VERSION,
    };
  }

  if (type === "page.collect") {
    const state = await sendToTab(normalizeTabId(tabId), {
      type: "CHROME_MCP_COLLECT_STATE",
      requestId: id,
      options: message.options || {},
    });
    return {
      type: "page.state",
      id: id || null,
      tabId,
      ok: true,
      payload: state,
    };
  }

  if (type === "page.action") {
    const actionResult = await sendToTab(normalizeTabId(tabId), {
      type: "CHROME_MCP_PERFORM_ACTION",
      requestId: id,
      action: message.action || {},
    });
    return {
      type: "action.result",
      id: id || null,
      tabId,
      ok: Boolean(actionResult?.ok),
      payload: actionResult,
    };
  }

  if (type === "tabs.list") {
    const tabs = await chrome.tabs.query({});
    return {
      type: "tabs.list.result",
      id: id || null,
      ok: true,
      payload: tabs.map((tab) => ({
        id: tab.id,
        url: tab.url || "",
        title: tab.title || "",
        active: Boolean(tab.active),
        windowId: tab.windowId,
      })),
    };
  }

  if (type === "page.screenshot") {
    const numericTabId = normalizeTabId(tabId);
    try {
      const allTabs = await chrome.tabs.query({});
      const targetTab = allTabs.find((t) => t.id === numericTabId);
      const windowId = targetTab ? targetTab.windowId : undefined;
      const dataUrl = await chrome.tabs.captureVisibleTab(windowId, { format: "png" });
      return {
        type: "page.screenshot.result",
        id: id || null,
        tabId,
        ok: true,
        payload: { dataUrl, format: "png" },
      };
    } catch (err) {
      return {
        type: "page.screenshot.result",
        id: id || null,
        tabId,
        ok: false,
        payload: { error: String(err) },
      };
    }
  }

  if (type === "page.execute_js") {
    const code = String(message.code || "");
    const numericTabId = normalizeTabId(tabId);
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: numericTabId },
        func: (jsCode) => {
          try {
            // eslint-disable-next-line no-new-func
            return (new Function(jsCode))();
          } catch (e) {
            return { __error: String(e) };
          }
        },
        args: [code],
      });
      const result = results?.[0]?.result ?? null;
      return {
        type: "page.execute_js.result",
        id: id || null,
        tabId,
        ok: true,
        payload: { result },
      };
    } catch (err) {
      return {
        type: "page.execute_js.result",
        id: id || null,
        tabId,
        ok: false,
        payload: { error: String(err) },
      };
    }
  }

  if (type === "page.console") {
    const numericTabId = normalizeTabId(tabId);
    try {
      const result = await sendToTab(numericTabId, {
        type: "CHROME_MCP_GET_CONSOLE",
        requestId: id,
        limit: message.limit || 50,
        level: message.level || null,
      });
      return {
        type: "page.console.result",
        id: id || null,
        tabId,
        ok: true,
        payload: result,
      };
    } catch (err) {
      return {
        type: "page.console.result",
        id: id || null,
        tabId,
        ok: false,
        payload: { logs: [], total: 0, error: String(err) },
      };
    }
  }

  if (type === "page.console.clear") {
    const numericTabId = normalizeTabId(tabId);
    try {
      await sendToTab(numericTabId, { type: "CHROME_MCP_CLEAR_CONSOLE", requestId: id });
    } catch (_err) { /* best-effort */ }
    return { type: "page.console.clear.result", id: id || null, ok: true };
  }

  if (type === "page.network") {
    const limit = Math.max(1, Math.min(Number(message.limit) || 50, NETWORK_BUFFER_SIZE));
    const filterTabId = tabId != null ? String(normalizeTabId(tabId)) : null;
    const filterEvent = message.event || null;
    let entries = filterTabId
      ? networkBuffer.filter((e) => String(e.tabId) === filterTabId)
      : networkBuffer.slice();
    if (filterEvent) {
      entries = entries.filter((e) => e.event === filterEvent);
    }
    return {
      type: "page.network.result",
      id: id || null,
      ok: true,
      payload: { requests: entries.slice(-limit), total: entries.length },
    };
  }

  if (type === "page.network.clear") {
    networkBuffer.length = 0;
    return { type: "page.network.clear.result", id: id || null, ok: true };
  }

  if (type === "page.text") {
    const numericTabId = normalizeTabId(tabId);
    try {
      const result = await sendToTab(numericTabId, {
        type: "CHROME_MCP_GET_PAGE_TEXT",
        requestId: id,
        selector: message.selector || null,
        limit: message.limit || 20000,
      });
      return {
        type: "page.text.result",
        id: id || null,
        tabId,
        ok: true,
        payload: result,
      };
    } catch (err) {
      return {
        type: "page.text.result",
        id: id || null,
        tabId,
        ok: false,
        payload: { text: "", error: String(err) },
      };
    }
  }

  if (type === "tabs.create") {
    const url = message.url || "about:blank";
    const active = message.active !== false;
    try {
      const tab = await chrome.tabs.create({ url, active });
      return {
        type: "tabs.create.result",
        id: id || null,
        ok: true,
        payload: { id: tab.id, url: tab.url || url, title: tab.title || "", active: Boolean(tab.active) },
      };
    } catch (err) {
      return { type: "tabs.create.result", id: id || null, ok: false, payload: { error: String(err) } };
    }
  }

  if (type === "tabs.close") {
    const numericTabId = normalizeTabId(tabId);
    try {
      await chrome.tabs.remove(numericTabId);
      return { type: "tabs.close.result", id: id || null, ok: true, payload: { tabId: numericTabId } };
    } catch (err) {
      return { type: "tabs.close.result", id: id || null, ok: false, payload: { error: String(err) } };
    }
  }

  if (type === "tabs.activate") {
    const numericTabId = normalizeTabId(tabId);
    try {
      await chrome.tabs.update(numericTabId, { active: true });
      return { type: "tabs.activate.result", id: id || null, ok: true, payload: { tabId: numericTabId } };
    } catch (err) {
      return { type: "tabs.activate.result", id: id || null, ok: false, payload: { error: String(err) } };
    }
  }

  if (type === "hello.ack" || type === "error") {
    return null;
  }

  return {
    type: "error",
    id: id || null,
    error: "unsupported_type",
    detail: type,
  };
}

chrome.runtime.onInstalled.addListener(() => {
  connectBridge().catch((error) => log("install connect failed", error));
});

chrome.runtime.onStartup.addListener(() => {
  connectBridge().catch((error) => log("startup connect failed", error));
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "CHROME_MCP_PAGE_READY") {
    connectBridge().catch((error) => log("page ready connect failed", error));
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type === "CHROME_MCP_PAGE_HEARTBEAT") {
    connectBridge().catch((error) => log("page heartbeat connect failed", error));
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type === "CHROME_MCP_PING_PAGE") {
    sendResponse({ ok: true, pong: true });
    return false;
  }

  return false;
});

connectBridge().catch((error) => log("initial connect failed", error));
