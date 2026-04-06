# Chrome MCP Benchmark

This harness measures the bridge path we care about for LinkedIn and other job sites:

- `tabs.list`
- `page.read`
- `page.find`
- `page.action`

It is meant to answer two questions:

1. How fast is the bridge path in the current browser session?
2. Does the post-submit state still look correct after an action?

## Run

From the repo root:

```bash
./.venv/bin/python runs/chrome_mcp_benchmark.py \
  --url-contains linkedin.com/jobs/view \
  --find-query "Submit application" \
  --verify-post-submit
```

The benchmark writes a structured JSON report to:

`./runs/chrome_mcp_benchmark_report.json`

and also prints the same JSON to stdout.

## Common Options

- `--tab-id`: pin the benchmark to one tab id.
- `--url-contains`: pick the first tab whose URL contains a substring.
- `--read-filter`: use `interactive` for controller-style reads, or `all` for a full snapshot.
- `--find-query`: semantic lookup query for `page.find`.
- `--action-kind`: `click`, `setValue`, or `scroll`.
- `--target-id`: stable interactive element id for `page.action`.
- `--verify-post-submit`: run a follow-up `page.read` after the action and check success markers.

## What The Numbers Mean

Lower is better.

- `tabs.list` should stay small. If it grows, the bridge connection or extension reload path is degrading.
- `page.read` is the main controller read cost. This is the best number to watch for overall flow speed.
- `page.find` is useful for semantic targeting. It should be close to `page.read` because it is built on top of the snapshot layer.
- `page.action` should be fast and predictable. For LinkedIn, the real goal is one click or input action per step, not repeated retries.

The report also includes:

- `sample_results.page.read.interactive_count`
- `sample_results.page.find`
- `sample_results.page.action`
- `post_submit.passed`

Use `post_submit.passed = true` as the regression gate for submit-state verification.

## Safety Note

`click` and `setValue` are not repeated by default. That keeps the benchmark from accidentally resubmitting a live application. If you explicitly need repeated action timing on a fixture, pass `--unsafe-repeat-action`.

## Recommended Workflow

1. Run the benchmark with `--url-contains linkedin.com/jobs/view`.
2. Compare `p50_ms` and `p95_ms` across code changes.
3. If submit verification is enabled, confirm `post_submit.passed` stays `true`.
4. Only then consider the flow ready for another LinkedIn iteration.
