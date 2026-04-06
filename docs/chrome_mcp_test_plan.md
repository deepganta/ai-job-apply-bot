# Chrome MCP Test Plan

This plan covers the browser-native bridge we want to add on top of the current job apply bot:

- a Chrome extension that observes the active tab and exposes semantic page state
- a local Python bridge that receives page snapshots and issues DOM actions
- a smoke test harness that verifies the bridge before we trust it for job applications

The goal is to reach the same control style we saw in Claude in Chrome: structured page state, stable element handles, direct actions, and fewer screenshot-driven retries.

## Assumed Bridge Contract

The bridge should keep the protocol small and predictable.

The current extension code defines the message bus we should keep:

- the extension sends `hello` on connect with `clientId`, `protocolVersion`, and `capabilities`
- the bridge sends `ping`, `tabs.list`, `page.collect`, `page.read`, `page.find`, and `page.action`
- the content script responds with `pong`, `page.state`, `action.result`, and `tabs.list.result`

Suggested request shape:

```json
{
  "type": "page.collect",
  "id": "req-1",
  "tabId": 123
}
```

Suggested response shape:

```json
{
  "type": "page.state",
  "id": "req-1",
  "tabId": 123,
  "ok": true,
  "payload": {
    "state": {
      "url": "https://example.com",
      "title": "Example",
      "interactiveElements": []
    }
  }
}
```

Page snapshot should include:

- `tabId`
- `url`
- `title`
- `interactiveElements` or `elements` with stable ids
- visible text excerpt
- action metadata like `role`, `label`, `enabled`, `visible`, `value`, and bounding box if available

## Team Split

Two developers and one tester is the right split for this.

Developer 1:

- build the Chrome extension service worker and content script
- expose semantic page state and stable element ids
- send action requests to the current tab

Developer 2:

- build the local Python bridge and any small CLI entrypoints
- own transport, routing, and response envelopes
- keep the bridge easy to start and inspect locally

Tester:

- validate protocol shape
- verify the extension connects and keeps tabs registered
- verify snapshots include the right fields
- verify actions mutate the test page and return the expected response
- keep a list of regressions and confirm they are fixed before release

## Smoke Test Matrix

1. WebSocket connect succeeds and the extension emits a `hello` message.
2. `ping` returns a `pong`-style success response.
3. `tabs.list` returns at least one attached tab once Chrome is open.
4. `page.collect` returns the active page title, URL, and at least one interactive element on a fixture page.
5. `page.read` returns a compact interactive-element view suitable for controller reasoning.
6. `page.find` can locate the fixture input and button using semantic text.
7. `page.action` with `click` and `setValue` updates the fixture page and the next snapshot reflects the change.
6. Reloading the page preserves tab registration and does not leave stale handles.
7. Closing the tab removes it from the bridge state cleanly.

## Manual Verification

Use a deterministic fixture page before touching real job sites.

- open a test page with one button and one text field
- confirm the snapshot contains both controls
- click the button and confirm the button text or DOM text changes
- set the text field and confirm the new value is returned in the next snapshot

Do not use a live job application page for the first smoke pass. The point is to prove the transport and DOM bridge before we touch LinkedIn or Indeed.

## Pass Criteria

The bridge is ready for the next stage when all of these are true:

- the extension registers automatically in a normal Chrome session
- the Python bridge can list tabs and read page snapshots over the websocket contract
- the Python bridge can semantically find interactive elements without DOM selectors
- click and input actions work on a local fixture page
- the smoke harness exits cleanly with no retries or manual patching
- the snapshot format stays stable enough for the application bot to consume directly

## Risks

- Chrome extensions can fail silently if permissions or match patterns are wrong
- element ids must stay stable across rerenders or the action layer becomes brittle
- multi-tab support matters because the job bot frequently opens detail pages and application dialogs
- the bridge should never claim success unless the page state confirms the action

## Current Status

The fixture path is already validated locally:

- `tabs.list` returns the fixture tab
- `page.collect` returns `Chrome MCP Fixture`
- `page.action setValue` updates the `Name` and `Notes` fields
- `page.action click` updates the DOM text from `Waiting` to `Button clicked`

The smoke harness in `runs/chrome_mcp_smoke_test.py` is the first gate before any job-site integration.
