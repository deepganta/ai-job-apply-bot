# Chrome MCP Extension Scaffold

This directory contains a minimal Manifest V3 extension that behaves like a browser-native control layer for a local MCP bridge.

## Files

- [manifest.json](../job_apply_bot/chrome_mcp_extension/manifest.json)
- [service_worker.js](../job_apply_bot/chrome_mcp_extension/service_worker.js)
- [content_script.js](../job_apply_bot/chrome_mcp_extension/content_script.js)

## Roles

- Dev A: owns the extension runtime, websocket bridge, and message routing.
- Dev B: owns page-state extraction, stable element ids, and action execution in the content script.
- Tester: loads the unpacked extension, runs the local bridge, and verifies collect/action/ping flows on real pages.

## Protocol

The bridge uses plain JSON messages.

Client hello:

```json
{
  "type": "hello",
  "clientId": "chrome-mcp-abc123",
  "protocolVersion": "0.1.0",
  "capabilities": [
    "page.collect",
    "page.action.click",
    "page.action.setValue",
    "page.action.scroll",
    "ping"
  ]
}
```

Bridge request types:

- `ping`
- `page.collect`
- `page.read`
- `page.find`
- `page.action`
- `tabs.list`

Bridge response types:

- `pong`
- `page.state`
- `action.result`
- `tabs.list.result`
- `error`

`page.collect` requires a `tabId` and returns:

```json
{
  "url": "https://example.com",
  "title": "Example",
  "scrollX": 0,
  "scrollY": 0,
  "viewport": { "width": 1720, "height": 1200 },
  "visibleTextExcerpt": "first chunk of page text",
  "interactiveElements": [
    {
      "id": "mcp-...",
      "tagName": "button",
      "type": "",
      "role": "",
      "label": "",
      "name": "",
      "ariaLabel": "Easy Apply",
      "placeholder": "",
      "text": "Easy Apply",
      "value": "",
      "checked": false,
      "disabled": false,
      "href": "",
      "x": 140,
      "y": 312,
      "width": 142,
      "height": 40
    }
  ]
}
```

`page.action` uses:

- `click`
- `setValue`
- `scroll`
- `ping`

Example action:

```json
{
  "type": "page.action",
  "id": "req-2",
  "tabId": 123,
  "action": {
    "kind": "setValue",
    "targetId": "mcp-abc",
    "value": "Your Full Name"
  }
}
```

## Workflow

1. Load the extension unpacked in Chrome.
2. Start the local websocket bridge on `ws://127.0.0.1:8765` or set `chrome.storage.local.bridgeUrl`.
3. Send `page.collect` for the active tab.
4. Use `page.find` for semantic lookup when possible, or the returned `interactiveElements[].id` values directly for `page.action`.
5. Verify the page state again after each action.

## Testing Scope

The first test loop should cover:

- one `tabs.list` round-trip from the bridge
- one `page.read` call with `filter=interactive`
- one `page.find` call for a labeled input
- one `setValue` action on a text field
- one `click` action on a visible button
- one `scroll` action inside a modal or page container
- one ping round-trip

The current scaffold is intentionally small. The next step is to add a thin local bridge server and a CLI that can relay these messages into the existing job-apply workflow.
