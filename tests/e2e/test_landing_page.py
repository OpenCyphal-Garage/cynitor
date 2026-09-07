#!/usr/bin/env python3
"""E2E tests for the Cynitor landing page.

Runs against the static frontend only; no backend is required, since these
assertions describe the disconnected state.

Prerequisites:
    pip install playwright
    playwright install chromium
    cd website && python3 -m http.server 5500   (in another terminal)

Usage:
    python test_landing_page.py

Environment:
    CYNITOR_E2E_URL   frontend base URL (default http://localhost:5500)
"""

import asyncio
import os
import sys
import traceback
from pathlib import Path

RESULT_FILE = Path(__file__).parent / "result.md"
BASE_URL = os.environ.get("CYNITOR_E2E_URL", "http://localhost:5500").rstrip("/")


async def run_tests():
    from playwright.async_api import async_playwright

    results = []
    passed = 0
    failed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})

        # ── Test: Page loads ──
        try:
            page = await context.new_page()
            await page.goto(BASE_URL, wait_until="networkidle")
            title = await page.title()
            assert "Cynitor" in title, f"Expected 'Cynitor' in title, got '{title}'"
            results.append(("Page loads with correct title", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Page loads with correct title", "FAIL", str(e)))
            failed += 1

        # ── Test: Sidebar visible ──
        try:
            sidebar = page.locator("nav.sidebar")
            assert await sidebar.is_visible(), "Sidebar not visible"
            results.append(("Sidebar is visible on load", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Sidebar is visible on load", "FAIL", str(e)))
            failed += 1

        # ── Test: Server semaphore starts disconnected (red) ──
        try:
            sem = page.locator("#serverSemaphore")
            assert await sem.is_visible(), "Server semaphore not visible"
            classes = await sem.get_attribute("class") or ""
            # Should NOT have 'on' class when disconnected
            assert "on" not in classes, f"Server semaphore should not be 'on', got classes: {classes}"
            results.append(("Server semaphore starts disconnected", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Server semaphore starts disconnected", "FAIL", str(e)))
            failed += 1

        # ── Test: CAN semaphore starts disconnected ──
        try:
            can_sem = page.locator("#canSemaphore")
            assert await can_sem.is_visible(), "CAN semaphore not visible"
            classes = await can_sem.get_attribute("class") or ""
            assert "on" not in classes, f"CAN semaphore should not be 'on', got classes: {classes}"
            results.append(("CAN semaphore starts disconnected", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("CAN semaphore starts disconnected", "FAIL", str(e)))
            failed += 1

        # ── Test: Nodes table exists with correct headers (Tabulator) ──
        try:
            await page.wait_for_selector(".tabulator-col-title", timeout=5000)
            raw_headers = await page.locator(".tabulator-col-title").all_text_contents()
            headers = [h.strip() for h in raw_headers if h.strip()]
            expected = ["ID", "Name", "State", "Health", "Rate", "Uptime", "Publishers", "Subscribers", "Servers", "Clients"]
            assert headers == expected, f"Expected headers {expected}, got {headers}"
            results.append(("Nodes table has correct headers", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Nodes table has correct headers", "FAIL", str(e)))
            failed += 1

        # ── Test: Empty table shows "Not connected" ──
        try:
            placeholder = page.locator(".tabulator-placeholder")
            text = await placeholder.text_content()
            assert "not connected" in text.lower(), f"Expected 'Not connected', got '{text}'"
            results.append(("Empty table shows 'Not connected'", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Empty table shows 'Not connected'", "FAIL", str(e)))
            failed += 1

        # ── Test: Filter row has 10 input fields (Tabulator header filters) ──
        try:
            filters = page.locator(".tabulator-header-filter input")
            count = await filters.count()
            assert count == 10, f"Expected 10 filter inputs, got {count}"
            results.append(("Filter row has 10 inputs", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Filter row has 10 inputs", "FAIL", str(e)))
            failed += 1

        # ── Test: API URL input has default value ──
        try:
            api_input = page.locator("#apiBase")
            value = await api_input.input_value()
            assert "localhost:8080" in value, f"Expected localhost:8080, got '{value}'"
            results.append(("API URL input has default value", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("API URL input has default value", "FAIL", str(e)))
            failed += 1

        # ── Test: Connect button exists and says "Connect" ──
        try:
            btn = page.locator("#connectDashboardBtn")
            text = await btn.text_content()
            assert "connect" in text.lower(), f"Expected 'Connect', got '{text}'"
            results.append(("Connect button shows 'Connect'", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Connect button shows 'Connect'", "FAIL", str(e)))
            failed += 1

        # Removed: the refresh slider (#nodesRefreshSlider) and the node/event
        # counters (#nodeCount, #eventCount) no longer exist in the UI, so the
        # assertions that covered them were deleted rather than repointed.

        # ── Test: Theme toggle button exists ──
        try:
            toggle = page.locator("#themeToggle")
            assert await toggle.is_visible(), "Theme toggle not visible"
            aria = await toggle.get_attribute("aria-label")
            assert aria, "Theme toggle missing aria-label"
            results.append(("Theme toggle exists with aria-label", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Theme toggle exists with aria-label", "FAIL", str(e)))
            failed += 1

        # ── Test: Theme toggle switches theme ──
        try:
            theme_before = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
            await page.locator("#themeToggle").click()
            await page.wait_for_timeout(300)
            theme_after = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
            assert theme_before != theme_after, f"Theme did not toggle (before={theme_before}, after={theme_after})"
            results.append(("Theme toggle switches theme", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Theme toggle switches theme", "FAIL", str(e)))
            failed += 1

        # ── Test: Sidebar collapse button toggles both ways ──
        try:
            collapse_btn = page.locator("#sidebarCollapseBtn")
            assert await collapse_btn.is_visible(), "Collapse button not visible"

            # The click handler toggles 'collapsed' on .sidebar itself. An
            # earlier version of this test looked for that class on .app-shell,
            # which never carries it, and then fell back to an `or` on the
            # pre-click visibility that was always true — so it could not fail.
            sidebar = page.locator("nav.sidebar")
            before = await sidebar.get_attribute("class") or ""
            assert "collapsed" not in before, f"Sidebar should start expanded, got: {before}"

            await collapse_btn.click()
            await page.wait_for_timeout(300)
            collapsed = await sidebar.get_attribute("class") or ""
            assert "collapsed" in collapsed, f"Sidebar did not collapse, classes: {collapsed}"

            # Clicking again must restore the expanded state, which also leaves
            # the page as later assertions expect to find it.
            await collapse_btn.click()
            await page.wait_for_timeout(300)
            restored = await sidebar.get_attribute("class") or ""
            assert "collapsed" not in restored, f"Sidebar did not expand again, classes: {restored}"

            results.append(("Sidebar collapse toggles both ways", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Sidebar collapse toggles both ways", "FAIL", str(e)))
            failed += 1

        # ── Test: Sortable columns exist (Tabulator) ──
        try:
            sortable = page.locator(".tabulator-col.tabulator-sortable")
            count = await sortable.count()
            assert count >= 4, f"Expected at least 4 sortable columns, got {count}"
            results.append(("Sortable columns exist", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Sortable columns exist", "FAIL", str(e)))
            failed += 1

        # ── Test: Sidebar collapse button has aria-label ──
        try:
            aria = await page.locator("#sidebarCollapseBtn").get_attribute("aria-label")
            assert aria, "Sidebar collapse button missing aria-label"
            results.append(("Sidebar collapse button has aria-label", "PASS", ""))
            passed += 1
        except Exception as e:
            results.append(("Sidebar collapse button has aria-label", "FAIL", str(e)))
            failed += 1

        await browser.close()

    return results, passed, failed


def write_results(results, passed, failed):
    lines = [
        "# Landing Page Test Results\n",
        f"**Passed:** {passed} | **Failed:** {failed} | **Total:** {passed + failed}\n",
        "",
        "| Test | Status | Details |",
        "|------|--------|---------|",
    ]
    for name, status, detail in results:
        icon = "PASS" if status == "PASS" else "FAIL"
        detail_escaped = detail.replace("|", "\\|")[:100]
        lines.append(f"| {name} | {icon} | {detail_escaped} |")

    RESULT_FILE.write_text("\n".join(lines) + "\n")
    print(f"\nResults written to {RESULT_FILE}")


def main():
    try:
        results, passed, failed = asyncio.run(run_tests())
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        write_results([("Test setup", "FAIL", str(e))], 0, 1)
        sys.exit(1)

    write_results(results, passed, failed)

    print(f"\n{'=' * 40}")
    print(f"  PASSED: {passed}  |  FAILED: {failed}")
    print(f"{'=' * 40}")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
