# End-to-End Tests

Playwright tests for the Cynitor frontend. These run against the static site
only and need no backend, because every assertion describes the disconnected
state of the landing page.

Backend unit tests live separately in `server/tests/` and run under pytest.

## Quick Start

```bash
pip install playwright
playwright install chromium

# Serve the frontend in one terminal
cd website && python3 -m http.server 5500

# Run the tests in another
cd tests/e2e && python3 test_landing_page.py
```

The script exits non-zero if any assertion fails, so it works as a CI gate. It
also writes a `result.md` summary table next to itself.

## Tests

| File | Covers |
|------|--------|
| `test_landing_page.py` | Landing page: layout, semaphores, nodes table, theme toggle, sidebar collapse, accessibility labels |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CYNITOR_E2E_URL` | `http://localhost:5500` | Frontend base URL |

## Notes

`result.md` is generated output, not source. Add `tests/e2e/result.md` to
`.gitignore` so it does not churn on every run.

When the UI changes, these assertions go stale silently unless CI runs them.
Three of them had already rotted before this suite was automated: the page
title assertion still expected the old product name, and two tests covered a
refresh slider and node/event counters that no longer exist in the interface.
