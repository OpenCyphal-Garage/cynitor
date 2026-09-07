# End-to-End Tests

Playwright tests for the Cynitor frontend. These run against the static site
only and need no backend, because every assertion describes the disconnected
state of the landing page.

Backend unit tests live separately in `server/tests/` and run under pytest.

## Quick Start

```bash
pip install -r tests/e2e/requirements.txt
playwright install chromium

cd tests/e2e && python3 test_landing_page.py
```

The script serves `website/` on port 5500 itself and shuts that server down
afterwards. If something already answers on 5500, such as your own dev server,
it uses that instead. CI runs the identical command.

It exits non-zero if any assertion fails, so it works as a CI gate, and writes
a `result.md` summary table next to itself.

## Adding a test

Decorate an async function that takes the shared `page`:

```python
@test("Connect button shows 'Connect'")
async def _(page):
    text = await page.locator("#connectDashboardBtn").text_content()
    assert "connect" in text.lower(), f"Expected 'Connect', got '{text}'"
```

Tests run in registration order on one page; the first one navigates.

## Tests

| File | Covers |
|------|--------|
| `test_landing_page.py` | Landing page: layout, semaphores, nodes table, theme toggle, sidebar collapse, accessibility labels |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CYNITOR_E2E_URL` | `http://localhost:5500` | Frontend base URL. When set, the script never starts a server. |

## Notes

`result.md` is generated output, not source. Add `tests/e2e/result.md` to
`.gitignore` so it does not churn on every run.

When the UI changes, these assertions go stale silently unless CI runs them;
several had already rotted before this suite was automated.
