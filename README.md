# AI/ML Job Apply Bot

Current baseline: `v0.5.0`

Release note: [docs/releases/linkedin-verified-baseline-v0.3.0.md](docs/releases/linkedin-verified-baseline-v0.3.0.md)

Changelog: [CHANGELOG.md](CHANGELOG.md)

This project creates a local interface for scanning trusted vendor sites, Indeed, and LinkedIn, filtering to AI/ML jobs that match your saved criteria, and applying with your saved profile, resume, and answer rules.

## What It Does

- Loads trusted vendors from [White_Vendors_List.xlsx](/path/to/White_Vendors_List.xlsx).
- Loads Indeed search defaults from [config/indeed_search.json](config/indeed_search.json).
- Loads LinkedIn search defaults from [config/linkedin_search.json](config/linkedin_search.json).
- Scans vendor websites plus any direct job URLs you paste into [config/job_urls.txt](config/job_urls.txt).
- Scans Indeed in a persistent browser profile so you can log in once and reuse the same session.
- Scans LinkedIn from the visible results page and keeps only jobs that pass the saved criteria.
- Keeps only roles that look AI/ML-focused and were posted within the configured recent window.
- Opens no-login application forms and fills known fields from [config/candidate_profile.json](config/candidate_profile.json).
- Drives Indeed Easy Apply steps in a headed browser, filling visible fields from your profile and recurring answers.
- Drives LinkedIn Easy Apply with strict submit verification based on LinkedIn's own success state and applied markers.
- Uses a Chrome-MCP fast path for LinkedIn when the bridge and extension are connected, then falls back to Playwright if the bridge is unavailable.
- Uses [config/question_answers.json](config/question_answers.json) for recurring screening questions.
- Saves results to [runs/latest_results.json](runs/latest_results.json) and [runs/latest_results.md](runs/latest_results.md).

## Replicating This for Yourself

This bot is built to be fully customizable. Here is exactly what to change to make it work for your job search.

### Step 1 — Set up your candidate profile

Copy the example and fill in your details:

```bash
cp config/candidate_profile.example.json config/candidate_profile.json
```

Key fields to edit in `config/candidate_profile.json`:

| Field | What to put |
|-------|-------------|
| `full_name` | Your full name |
| `title` | Your job title (e.g. "Machine Learning Engineer") |
| `experience_years` | Your years of experience |
| `location` | Your city and state |
| `work_authorization` | e.g. `"OPT"`, `"H1B"`, `"US Citizen"`, `"Green Card"` |
| `requires_visa_sponsorship` | `true` or `false` |
| `linkedin_url` | Your LinkedIn profile URL |
| `short_pitch` | 1–2 sentence blurb about yourself |
| `education` | Your degree, school, field |

### Step 2 — Set your recurring screening answers

```bash
cp config/question_answers.example.json config/question_answers.json
```

Edit `config/question_answers.json`:
- `exact` — questions matched exactly (case-insensitive)
- `contains` — questions matched by substring

Common answers to set: location, years of experience, salary expectation, sponsorship needed, authorized to work.

### Step 3 — Configure your job search

**Indeed** — edit `config/indeed_search.json`:
```json
{
  "query": "Machine Learning Engineer",
  "location": "United States",
  "max_pages": 5,
  "max_jobs": 100,
  "recency_hours": 24
}
```

**LinkedIn** — edit `config/linkedin_search.json`:
```json
{
  "query": "AI Engineer",
  "location": "United States",
  "max_pages": 5,
  "max_jobs": 100,
  "recency_hours": 24,
  "contract_only": true,
  "remote_only": false,
  "experience_levels": ["2", "3"]
}
```

### Step 4 — Customize eligibility rules

All eligibility logic lives in `job_apply_bot/eligibility.py`.

**Change what counts as an AI/ML role** — edit `AI_ML_KEYWORDS`:
```python
AI_ML_KEYWORDS = [
    "machine learning", "llm", "generative ai", "python", "rag",
    # add your own keywords here
]
```

**Change what titles are always skipped** — edit `NON_TARGET_TITLE_TOKENS`:
```python
NON_TARGET_TITLE_TOKENS = (
    "data engineer",   # remove this if you want data engineer roles
    "devops",          # add roles you don't want
)
```

**Change what restrictions disqualify a job** — edit `BLOCKED_RESTRICTION_TOKENS`:
```python
BLOCKED_RESTRICTION_TOKENS = (
    "us citizen only",
    "security clearance",
    "no c2c",
    # add or remove based on your situation
)
```

**Change the max experience year filter** — in `config/candidate_profile.json`:
```json
"max_target_experience_years": 5
```
Or pass `--max-exp 6` on the CLI.

### Step 5 — Update the AI eligibility screener context

The Claude Haiku deep-read screener uses a hardcoded candidate context in `job_apply_bot/ai_assistant.py`. Update `_CANDIDATE_CONTEXT` to match your profile:

```python
_CANDIDATE_CONTEXT = """
Candidate profile:
- Visa: OPT (F-1) — fully authorized to work in the US
- Employment type: C2C contract only
- Experience: ~4 years in AI/ML
- Skills: Python, LLMs, RAG, NLP
- Location: Remote preferred
""".strip()
```

Also update `_ELIGIBILITY_SYSTEM` and `_ANSWER_SYSTEM` if your work authorization situation is different (e.g. US citizen, green card holder, H1B).

### Step 6 — Add your resume

Place your resume PDF at:
```
config/resume.pdf
```
Or set a custom path in `.env`:
```
JOB_BOT_RESUME_PATH=/path/to/your/resume.pdf
```

---

## Eligibility Rules (Permanent Exclusions)

| Rule | Detail |
|------|--------|
| Non-target titles | Data Engineer, DevOps, Frontend, QA, etc. — always skipped |
| Restrictions | US Citizen only, security clearance, active clearance — always skipped |
| No C2C | "No C2C", "W2 only", "no contractors", "no third party" — always skipped |
| Axle company | Skipped per user request |
| Experience | Job listings: >4 years skipped. Feed posts: >5 years skipped |
| OPT/F-1 only | Bot never applies to H1B-sponsored or green card required roles |

## Limits

- Arbitrary vendor sites are inconsistent. The scanner is best-effort and works best when vendor pages expose job links or structured job metadata.
- Auto-submit skips forms when required answers are missing or confirmation is ambiguous.
- Indeed may require manual sign-in or verification in the visible browser. The bot waits for you, but it does not bypass those checks.
- LinkedIn automation follows a stricter standard workflow; only a LinkedIn-confirmed apply counts as submitted. The workflow is documented in [docs/linkedin_standard_workflow.md](docs/linkedin_standard_workflow.md).
- The verified LinkedIn baseline from March 28, 2026 is documented with proof screenshots in [docs/releases/linkedin-verified-baseline-v0.3.0.md](docs/releases/linkedin-verified-baseline-v0.3.0.md).
- The bot never guesses missing legal or immigration answers.

## Setup

1. Create the environment and install dependencies:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium
```

2. Copy [.env.example](.env.example) to `.env` if you want to override defaults.

3. For Indeed automation, set these values in `.env`:
   - `JOB_BOT_HEADLESS=false`
   - `JOB_BOT_BROWSER_PROFILE_DIR=runs/browser-profile`
   - optionally `JOB_BOT_BROWSER_CHANNEL=chrome` if you want to reuse a Chrome install instead of bundled Chromium

4. Fill any missing fields in [config/candidate_profile.json](config/candidate_profile.json), especially:
   - `location`
   - `work_authorization`
   - `requires_visa_sponsorship`
   - `linkedin_url`

5. Add recurring screening answers to [config/question_answers.json](config/question_answers.json).

6. Optionally paste direct job URLs into [config/job_urls.txt](config/job_urls.txt).

7. Optional: tune the default Indeed query in [config/indeed_search.json](config/indeed_search.json).

## Run The Interface

```bash
./.venv/bin/python -m job_apply_bot serve
```

Open [http://127.0.0.1:5050](http://127.0.0.1:5050).

## Interface Workflow

1. For vendor pages, click `Start Vendor Scan`.
2. For Indeed, fill the `Indeed Search` form and click `Start Indeed Scan`.
3. Review the discovered jobs list.
4. Click `Apply` for a single eligible job, or `Apply Eligible Jobs` for a batch.
5. Check the submitted jobs list and the saved summaries under [runs](runs).

## Indeed Session Prep

Open a persistent browser and sign in to Indeed once:

```bash
./.venv/bin/python -m job_apply_bot indeed-login
```

After login, close the command when prompted. Future scans and applies will reuse the same profile directory from `JOB_BOT_BROWSER_PROFILE_DIR`.

## Terminal Commands

Scan without starting the UI:

```bash
./.venv/bin/python -m job_apply_bot scan --vendor-limit 25
```

Scan Indeed without starting the UI:

```bash
./.venv/bin/python -m job_apply_bot indeed-scan --query "AI Engineer" --location "United States"
```

Apply all currently eligible jobs in the saved dashboard state:

```bash
./.venv/bin/python -m job_apply_bot apply --all --submit-mode auto
```

Scan LinkedIn feed posts for recruiter C2C leads (last 2 hours):

```bash
JOB_BOT_OUTPUT_DIR=runs/$(date +%Y-%m-%d) \
  .venv/bin/python3 -m job_apply_bot linkedin-posts --hours 2 --scrolls 4
```

## LinkedIn Feed Post Scanner

Scans LinkedIn search results (feed posts, not job listings) for recruiters actively sharing C2C AI/ML contract roles in the last 1-2 hours. This is separate from the job listing scanner — it targets informal recruiter posts on the LinkedIn feed.

### How It Works

1. Searches LinkedIn content feed for: `"AI C2C"`, `"ML C2C"`, `"machine learning C2C"`, `"generative AI C2C contract"` sorted by latest
2. Clicks all `"… more"` expand buttons so full post text is loaded before reading
3. Splits page text on `"Feed post"` delimiter to isolate each post
4. Filters posts by:
   - Skip candidate availability posts ("I am looking", "open to work", "my resume", etc.)
   - Must have job posting signals ("we are hiring", "DM me", "share your resume", "urgent requirement", etc.)
   - Skip "No C2C", "W2 only", "no contractors" posts
   - Skip US Citizen / clearance requirements
   - Skip posts requiring more than 5 years experience
   - Must contain at least one AI/ML keyword
   - Must be posted within `--hours` hours (default 2)
5. Extracts email addresses from post text
6. Prints eligible leads with pre-written outreach email drafts
7. Saves all results (eligible + skipped) to `runs/YYYY-MM-DD/linkedin_posts_scan.json`

### Run

```bash
# Requires Chrome MCP bridge running + LinkedIn tab open and logged in
JOB_BOT_OUTPUT_DIR=runs/$(date +%Y-%m-%d) \
  .venv/bin/python3 -m job_apply_bot linkedin-posts --hours 2 --scrolls 4
```

Options:
- `--hours N` — how many hours back to look (default 2)
- `--scrolls N` — scroll passes per query to load more posts (default 4)
- `--query "..."` — add an extra search query on top of defaults

### Email Outreach

When a post contains an email address, the scanner prints a ready-to-send email draft. **Use Gmail forward** (not new draft) to automatically carry over the attached resume — search Gmail for a prior sent email with your resume attached, then forward it to the new lead's email with the updated subject and body.

The `linkedin-posts` command prints a long-form outreach template populated from your `candidate_profile.json` so all emails stay consistent.

---

## Chrome Bridge

This repo now includes a Chrome extension plus local bridge scaffold aimed at a Claude-style semantic browser workflow.

Files:

- [docs/chrome_mcp_architecture.md](docs/chrome_mcp_architecture.md)
- [docs/chrome_mcp_extension.md](docs/chrome_mcp_extension.md)
- [docs/chrome_mcp_server.md](docs/chrome_mcp_server.md)
- [docs/chrome_mcp_test_plan.md](docs/chrome_mcp_test_plan.md)
- [docs/chrome_mcp_benchmark.md](docs/chrome_mcp_benchmark.md)
- [chrome_mcp/extension/manifest.json](chrome_mcp/extension/manifest.json)
- [job_apply_bot/chrome_mcp_client.py](job_apply_bot/chrome_mcp_client.py)
- [job_apply_bot/apply/linkedin_bridge.py](job_apply_bot/apply/linkedin_bridge.py)
- [job_apply_bot/chrome_mcp_server.py](job_apply_bot/chrome_mcp_server.py)
- [runs/chrome_mcp_smoke_test.py](runs/chrome_mcp_smoke_test.py)
- [runs/chrome_mcp_benchmark.py](runs/chrome_mcp_benchmark.py)

Print the extension path:

```bash
./.venv/bin/python -m job_apply_bot chrome-mcp-extension-path
```

Run the local bridge:

```bash
./.venv/bin/python -m job_apply_bot chrome-mcp-server
```

The bridge now serves both WebSocket (for the extension and `ChromeMcpClient`) and an HTTP REST API for any LLM or script that prefers plain HTTP.

HTTP API — available once the bridge is running:

```bash
# List all open Chrome tabs
curl http://127.0.0.1:8765/api/tabs

# Read the current page state on a tab
curl -X POST http://127.0.0.1:8765/api/read \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "filter": "interactive", "limit": 40}'

# Find an element by label or text
curl -X POST http://127.0.0.1:8765/api/find \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "query": "Easy Apply"}'

# Click an element by id
curl -X POST http://127.0.0.1:8765/api/action \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "action": {"kind": "click", "targetId": "mcp-abc-123"}}'

# Set a field value
curl -X POST http://127.0.0.1:8765/api/action \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "action": {"kind": "setValue", "query": "Phone number", "value": "555-1234"}}'

# Navigate to a URL
curl -X POST http://127.0.0.1:8765/api/navigate \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "url": "https://www.linkedin.com/jobs/"}'

# Take a screenshot (returns base64 PNG data URL)
curl -X POST http://127.0.0.1:8765/api/screenshot \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123}'

# Wait for an element to appear (polls up to timeout_ms)
curl -X POST http://127.0.0.1:8765/api/wait \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "query": "Submit application", "timeout_ms": 8000}'

# Execute arbitrary JavaScript in the tab
curl -X POST http://127.0.0.1:8765/api/execute_js \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "code": "return document.title;"}'
```

After updating the extension code, reload `Chrome MCP Bridge` in `chrome://extensions` before the next LinkedIn run so new dialog-scope and navigation behavior is active.

Run the local fixture smoke test:

```bash
cd runs
.venv/bin/python -m http.server 8877
```

Open `http://127.0.0.1:8877/chrome_mcp_fixture.html` in Chromium with the unpacked extension loaded, then run:

```bash
./.venv/bin/python runs/chrome_mcp_smoke_test.py --url-contains chrome_mcp_fixture --fixture-demo
```

That smoke path now validates `tabs.list`, semantic page read, semantic find, direct DOM input, and direct DOM click before using the bridge on a real job site.

Benchmark the live bridge against the current LinkedIn tab:

```bash
./.venv/bin/python runs/chrome_mcp_benchmark.py --url-contains linkedin.com/jobs/view --find-query "Submitted resume" --verify-post-submit
```

---

## Acknowledgements

Built by [Deep Aman Ganta](https://github.com/deepganta).

AI eligibility screening and answer selection powered by [Claude](https://claude.ai) (Anthropic) — used as an embedded API component for job description analysis and form question handling.
