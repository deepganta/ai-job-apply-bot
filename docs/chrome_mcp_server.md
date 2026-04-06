# Chrome MCP Bridge

This repo now includes a small local bridge server intended to sit between the Chrome extension scaffold and the Python job bot. The goal is to move from brittle screenshot-only automation to a semantic browser-control layer.

## What Ships In V1

- A local Python WebSocket bridge in [`job_apply_bot/chrome_mcp_server.py`](job_apply_bot/chrome_mcp_server.py)
- CLI commands to start the bridge and print the expected extension path
- Connected-client tracking
- RPCs for `tabs.list`, `page.collect`, `page.read`, `page.find`, `page.action`, and `ping`
- A deterministic fixture page plus smoke harness for end-to-end verification

## CLI

Print the extension directory:

```bash
./.venv/bin/python -m job_apply_bot chrome-mcp-extension-path
```

Run the bridge:

```bash
./.venv/bin/python -m job_apply_bot chrome-mcp-server --host 127.0.0.1 --port 8765
```

The default extension directory is:

```text
job_apply_bot/chrome_mcp_extension
```

You can override the bridge host and port with:

- `JOB_BOT_CHROME_MCP_HOST`
- `JOB_BOT_CHROME_MCP_PORT`
- `JOB_BOT_CHROME_MCP_EXTENSION_DIR`

## Protocol

Messages are JSON over WebSocket and use a top-level `type` plus an `id` when a response is expected.

Extension/bridge messages:

- `hello`
- `ping`
- `tabs.list`
- `tabs.list.result`
- `page.collect`
- `page.read`
- `page.read.result`
- `page.find`
- `page.find.result`
- `page.state`
- `page.action`
- `action.result`
- `pong`
- `error`

## Snapshot Shape

The page state returned by `page.collect` includes these fields:

- `url`
- `title`
- `scrollX`
- `scrollY`
- `viewport`
- `visibleTextExcerpt`
- `interactiveElements`

Each interactive element should have a stable `id` plus semantic fields like `tagName`, `role`, `ariaLabel`, `name`, `type`, `value`, `placeholder`, `disabled`, `checked`, `href`, and bounding box coordinates.

The content script now also extracts a best-effort `label` for form fields, which makes LinkedIn-style form filling far more tractable than relying on raw `name` attributes.

## Validated Smoke Path

The reliable local validation path in this repo is:

1. Start the bridge:

```bash
./.venv/bin/python -m job_apply_bot chrome-mcp-server
```

2. Serve the fixture page from [`runs/chrome_mcp_fixture.html`](runs/chrome_mcp_fixture.html):

```bash
cd runs
.venv/bin/python -m http.server 8877
```

3. Launch Chromium with the unpacked extension loaded and open:

```text
http://127.0.0.1:8877/chrome_mcp_fixture.html
```

4. Run the smoke harness:

```bash
./.venv/bin/python runs/chrome_mcp_smoke_test.py --url-contains chrome_mcp_fixture --fixture-demo
```

That flow has been validated end-to-end: `tabs.list` succeeded, `page.collect` returned semantic state, `page.action setValue` updated the form fields, and `page.action click` changed the fixture result from `Waiting` to `Button clicked`.

## Team Split

This is a good split for two developers and one tester:

1. Developer A builds the Chrome extension service worker and content script that collect page state and execute actions.
2. Developer B hardens the Python bridge and response routing.
3. Tester validates tab listing, snapshot refresh, and click/input/scroll actions against real pages.

## V1 Limits

- No screenshots through the bridge
- No multi-window orchestration
- No native OS control
- No auth or encryption beyond localhost
- Google Chrome's unpacked-extension flags were unreliable in this environment; Playwright Chromium was the stable local test host

The existing Playwright path stays intact until the bridge is stable.
