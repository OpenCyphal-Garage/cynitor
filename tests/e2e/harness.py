"""What the browser end-to-end suites share: the frontend server, the test registry and the runner.

The tests are in suite_<view>.py; test_landing_page.py imports them in order
and runs them. They run against the static frontend only, no backend: the
landing page's assertions describe the disconnected state, and the views are
fed through the same paths live data takes.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

RESULT_FILE = Path(__file__).parent / "result.md"
WEBSITE_DIR = Path(__file__).resolve().parents[2] / "website"
BASE_URL = os.environ.get("CYNITOR_E2E_URL", "http://localhost:5500").rstrip("/")
WAIT_MS = 2000


# ── Frontend server ──

def _port_answers(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


@contextmanager
def frontend_server():
    """Serve website/ on BASE_URL's port unless something already answers there."""
    if "CYNITOR_E2E_URL" in os.environ or _port_answers(BASE_URL):
        yield
        return

    port = urlparse(BASE_URL).port
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(WEBSITE_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not _port_answers(BASE_URL):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"frontend server did not start on {BASE_URL}")
            time.sleep(0.1)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Test registry ──
#
# Each test is an async function taking the shared page. They run in the
# order registered (the suites' import order in test_landing_page.py), and
# the first one navigates, so keep it first. Each is kept with the suite
# that registered it, so that a run can take some suites only.

TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn, fn.__module__.removeprefix("suite_")))
        return fn
    return register


def selected(suites):
    """The tests of ``suites`` (names as in suite_<name>.py), after the first, which opens the page; all if none."""
    if not suites:
        return TESTS
    unknown = set(suites) - {suite for _, _, suite in TESTS}
    if unknown:
        raise SystemExit(f"No such suite: {', '.join(sorted(unknown))}; "
                         f"there are {', '.join(dict.fromkeys(suite for _, _, suite in TESTS))}")
    return [TESTS[0]] + [t for t in TESTS[1:] if t[2] in suites]


# ── Shared steps ──

async def answer_dialog(page, accept=True):
    """Answer the dashboard's own dialog (askConfirm, askText) as a user does, once it shows; what it said."""
    dialog = page.locator("#confirmDialog")
    await dialog.wait_for(state="visible", timeout=WAIT_MS)
    said = await page.evaluate("""['confirmDialogTitle', 'confirmDialogSubject', 'confirmDialogMeta',
        'confirmDialogMessage'].map((id) => el(id).textContent).filter(Boolean).join(' ')""")
    await page.locator("#confirmDialogOk" if accept else "#confirmDialog button[value='cancel']").click()
    await dialog.wait_for(state="hidden", timeout=WAIT_MS)
    return said

# ── Runner ──

async def run_tests(tests):
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # The assertions read numbers as English writes them (1,000); the browser would
        # otherwise take the system's locale (1.000 on a German Windows).
        context = await browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
        page = await context.new_page()
        for name, fn, _suite in tests:
            try:
                await fn(page)
                results.append((name, "PASS", ""))
            except Exception as e:
                results.append((name, "FAIL", str(e)))
        await browser.close()
    return results


def write_results(results):
    passed = sum(1 for _, status, _ in results if status == "PASS")
    failed = len(results) - passed
    lines = [
        "# Landing Page Test Results\n",
        f"**Passed:** {passed} | **Failed:** {failed} | **Total:** {len(results)}\n",
        "",
        "| Test | Status | Details |",
        "|------|--------|---------|",
    ]
    for name, status, detail in results:
        detail = detail.replace("|", "\\|")[:100]
        lines.append(f"| {name} | {status} | {detail} |")
    RESULT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nResults written to {RESULT_FILE}")
    return passed, failed


def main(suites=()):
    tests = selected(suites)
    try:
        with frontend_server():
            results = asyncio.run(run_tests(tests))
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        results = [("Test setup", "FAIL", str(e))]

    passed, failed = write_results(results)
    print(f"\n{'=' * 40}")
    print(f"  PASSED: {passed}  |  FAILED: {failed}")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)

