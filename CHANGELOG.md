# Changelog

## 0.5.4 - 2026-04-08

LinkedIn scan reliability + capture accuracy update.

- Updated [job_apply_bot/linkedin.py](job_apply_bot/linkedin.py) listing extraction to capture `data-job-id` shell rows (not only anchor-backed rows), reducing missed jobs on LinkedIn results pages.
- Improved LinkedIn candidate inspection fallback in [job_apply_bot/linkedin.py](job_apply_bot/linkedin.py) so jobs with missing in-card title can still be resolved from detail pages.
- Switched deduplication in [job_apply_bot/state.py](job_apply_bot/state.py) to prefer `job_id` keys, preventing distinct postings from collapsing when company/title are reused across locations.
- Enhanced LinkedIn scan orchestration in [job_apply_bot/cli.py](job_apply_bot/cli.py): supports default multi-keyword runs, preserves 3-page cap, and keeps high enough max-job budget to capture full scrolled pages.
- Added safer browser fallback behavior in [job_apply_bot/browser.py](job_apply_bot/browser.py) when CDP connects without usable contexts.
- Expanded LinkedIn post feed scanning robustness in [job_apply_bot/linkedin_posts.py](job_apply_bot/linkedin_posts.py) with stronger scrolling/expansion and larger text capture windows.

## 0.5.3 - 2026-04-06

Chrome MCP location cleanup and repo clarity pass.

- Moved Chrome extension assets from top-level `chrome_mcp/extension` into [job_apply_bot/chrome_mcp_extension](job_apply_bot/chrome_mcp_extension) so active Chrome MCP code and extension live in one place.
- Updated default extension path in [job_apply_bot/config.py](job_apply_bot/config.py) to the new in-package location.
- Updated Chrome MCP docs and README path references to the consolidated extension directory.
- Removed the duplicate top-level `chrome_mcp` folder to eliminate workflow confusion.

## 0.5.2 - 2026-04-06

Workflow split cleanup and dashboard alignment.

- Separated legacy vendor-workbook + Flask dashboard workflow into [legacy_vendor_workflow](legacy_vendor_workflow).
- Removed legacy `serve`/`scan` command overlap from the main CLI and added `dashboard-serve` for the active static dashboard flow.
- Updated [dashboard.html](dashboard.html) to auto-discover the latest dated run folder and refresh every 30 seconds.
- Updated docs to focus on active LinkedIn/Indeed scan + apply + LinkedIn posts/email workflow and documented legacy isolation.

## 0.5.1 - 2026-04-06

Resume-lock safety patch and LinkedIn apply flow hardening.

- Added default resume-lock behavior so the bot does not change resume/CV selection or upload fields unless explicitly allowed via `JOB_BOT_LOCK_RESUME_CHANGES=0`.
- Applied resume-lock checks in both [job_apply_bot/application.py](job_apply_bot/application.py) and [job_apply_bot/apply/vision_applier.py](job_apply_bot/apply/vision_applier.py).
- Added force-apply support for CLI apply runs with `--force-apply` in [job_apply_bot/cli.py](job_apply_bot/cli.py) and [job_apply_bot/application.py](job_apply_bot/application.py).
- Improved resume option matching logic in LinkedIn apply paths to reduce accidental selection of similarly named resumes.

## 0.5.0 - 2026-03-29

HTTP REST API + screenshot + execute_js + page.wait for the Chrome MCP bridge.

- Added HTTP REST API to [job_apply_bot/chrome_mcp_server.py](job_apply_bot/chrome_mcp_server.py) so any LLM (ChatGPT, Codex, etc.) can call the bridge via plain HTTP POST requests without managing a WebSocket connection.
- Added `page.screenshot` — bridge calls `chrome.tabs.captureVisibleTab` in the service worker and returns a PNG data URL. Useful for letting any LLM see the current page state.
- Added `page.execute_js` — bridge calls `chrome.scripting.executeScript` in the service worker and returns the result. The most powerful primitive: any LLM can run arbitrary JavaScript in any open tab.
- Added `page.wait` — Python-side polling in the bridge that repeatedly calls `find_elements` until the target element appears or a timeout is reached. Critical for LinkedIn's AJAX-driven dialog steps where clicking Next causes the form to reload.
- Updated [chrome_mcp/extension/service_worker.js](chrome_mcp/extension/service_worker.js) to handle `page.screenshot` and `page.execute_js` message types.
- Added `screenshot()`, `execute_js()`, `wait_for_element()` methods to [job_apply_bot/chrome_mcp_client.py](job_apply_bot/chrome_mcp_client.py).

HTTP API endpoints at `http://127.0.0.1:8765/api/`:

- `GET  /api/status`                  — server status and connected clients
- `GET  /api/tabs`                    — list all open Chrome tabs
- `POST /api/read`                    — read interactive page state
- `POST /api/find`                    — find elements by semantic query
- `POST /api/action`                  — click, setValue, scroll, navigate
- `POST /api/navigate`                — navigate tab to URL
- `POST /api/screenshot`              — capture visible tab as PNG (base64 data URL)
- `POST /api/wait`                    — wait for element to appear (poll up to timeout_ms)
- `POST /api/execute_js`              — run arbitrary JavaScript in a tab

After updating the extension, reload `Chrome MCP Bridge` in `chrome://extensions`.

## 0.4.0 - 2026-03-29

Chrome-MCP fast path for LinkedIn.

- Added a sync Chrome bridge client in [job_apply_bot/chrome_mcp_client.py](job_apply_bot/chrome_mcp_client.py).
- Added a LinkedIn bridge driver in [job_apply_bot/apply/linkedin_bridge.py](job_apply_bot/apply/linkedin_bridge.py).
- Integrated the LinkedIn application service with the Chrome bridge in [job_apply_bot/application.py](job_apply_bot/application.py), with Playwright kept as a fallback path.
- Tightened the bridge protocol in [job_apply_bot/chrome_mcp_server.py](job_apply_bot/chrome_mcp_server.py), [chrome_mcp/extension/content_script.js](chrome_mcp/extension/content_script.js), and [chrome_mcp/extension/service_worker.js](chrome_mcp/extension/service_worker.js) to support scoped dialog reads, exact control matching, query-based actions, and direct navigation.
- Added a bridge benchmark harness in [runs/chrome_mcp_benchmark.py](runs/chrome_mcp_benchmark.py) and usage notes in [docs/chrome_mcp_benchmark.md](docs/chrome_mcp_benchmark.md).
- Added a fresh timing report in [runs/chrome_mcp_benchmark_report.json](runs/chrome_mcp_benchmark_report.json).

Measured bridge timings on March 29, 2026:

- `tabs.list` p50 about `0.895 ms`
- `page.read` p50 about `9.523 ms`
- `page.find` p50 about `6.944 ms`

## 0.3.0 - 2026-03-28

LinkedIn verified-submit baseline.

- Added a package version marker in [job_apply_bot/__init__.py](job_apply_bot/__init__.py).
- Added CLI version output via `python -m job_apply_bot --version`.
- Kept the stricter LinkedIn rule: a job only counts as `submitted` after LinkedIn shows a real success state and the job page flips out of the incomplete flow.
- Preserved the invalidation logic for older LinkedIn rows that were previously marked submitted without verified completion.
- Documented the standard LinkedIn operator workflow in [docs/linkedin_standard_workflow.md](docs/linkedin_standard_workflow.md).
- Added a release note with proof screenshots for the verified LinkedIn submissions in [docs/releases/linkedin-verified-baseline-v0.3.0.md](docs/releases/linkedin-verified-baseline-v0.3.0.md).

Verified LinkedIn submissions captured in this baseline:

- `GENISYSAPP | Artificial Intelligence Engineer`
- `Amodal AI | Developer Content Engineer`
- `Turing | Remote Software Developer`
