"""Browser end-to-end tests: the landing page. Run through test_landing_page.py, which imports every suite in order."""

import re

from harness import BASE_URL, WAIT_MS, WEBSITE_DIR, test


# ── Landing page ──

@test("Page loads with correct title")
async def _(page):
    await page.goto(BASE_URL, wait_until="networkidle")
    title = await page.title()
    assert "Cynitor" in title, f"Expected 'Cynitor' in title, got '{title}'"


@test("Sidebar is visible on load")
async def _(page):
    assert await page.locator("nav.sidebar").is_visible(), "Sidebar not visible"


def semaphore_starts_off(selector, label):
    @test(f"{label} semaphore starts disconnected")
    async def _(page):
        sem = page.locator(selector)
        assert await sem.is_visible(), f"{label} semaphore not visible"
        classes = await sem.get_attribute("class") or ""
        assert "on" not in classes, f"{label} semaphore should not be 'on', got classes: {classes}"


semaphore_starts_off("#serverSemaphore", "Server")
semaphore_starts_off("#canSemaphore", "CAN")


@test("Nodes table has correct headers")
async def _(page):
    await page.wait_for_selector(".tabulator-col-title", timeout=5000)
    raw_headers = await page.locator(".tabulator-col-title").all_text_contents()
    headers = [h.strip() for h in raw_headers if h.strip()]
    expected = ["ID", "Name", "State", "Health", "Mode", "VSSC", "SW", "Rate", "Uptime",
                "Publishers", "Subscribers", "Servers", "Clients"]
    assert headers == expected, f"Expected headers {expected}, got {headers}"


@test("Empty table shows 'Not connected'")
async def _(page):
    text = await page.locator(".tabulator-placeholder").text_content()
    assert "not connected" in text.lower(), f"Expected 'Not connected', got '{text}'"


@test("Filter row has 13 inputs")
async def _(page):
    count = await page.locator(".tabulator-header-filter input").count()
    assert count == 13, f"Expected 13 filter inputs, got {count}"


@test("Uptime reads at a glance; narrow tables drop the least telling columns")
async def _(page):
    shown = await page.evaluate("[59, 700, 7300, 266400].map(formatUptime)")
    assert shown == ["59s", "11m 40s", "2h 1m", "3d 2h"], f"Uptime shown as {shown}"
    size = page.viewport_size
    await page.set_viewport_size({"width": 900, "height": size["height"]})
    try:
        await page.wait_for_timeout(300)
        visible = await page.evaluate("nodesTabulator.getColumns().filter(c => c.isVisible()).map(c => c.getField())")
        for field in ("_sortId", "name", "state"):
            assert field in visible, f"{field} hidden at 900 px: {visible}"
        assert "clients" not in visible, f"Clients should hide first: {visible}"
    finally:
        await page.set_viewport_size(size)


@test("API URL input has default value")
async def _(page):
    value = await page.locator("#apiBase").input_value()
    assert "localhost:8080" in value, f"Expected localhost:8080, got '{value}'"


@test("Connect button shows 'Connect'")
async def _(page):
    text = await page.locator("#connectDashboardBtn").text_content()
    assert "connect" in text.lower(), f"Expected 'Connect', got '{text}'"


def has_aria_label(selector, label):
    @test(f"{label} has aria-label")
    async def _(page):
        assert await page.locator(selector).is_visible(), f"{label} not visible"
        assert await page.locator(selector).get_attribute("aria-label"), f"{label} missing aria-label"


has_aria_label("#themeToggle", "Theme toggle")
has_aria_label("#sidebarCollapseBtn", "Sidebar collapse button")


@test("Theme toggle switches theme")
async def _(page):
    # Light is the attribute being absent, matching the app's own default.
    read_theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    before = await page.evaluate(read_theme)
    await page.locator("#themeToggle").click()
    await page.wait_for_function(f"({read_theme}) !== {before!r}", timeout=WAIT_MS)


@test("Sidebar collapse toggles both ways")
async def _(page):
    # The click handler toggles 'collapsed' on the sidebar element itself.
    sidebar = page.locator("nav.sidebar")
    assert "collapsed" not in (await sidebar.get_attribute("class") or ""), "Sidebar should start expanded"
    await page.locator("#sidebarCollapseBtn").click()
    await page.locator("nav.sidebar.collapsed").wait_for(state="attached", timeout=WAIT_MS)
    await page.locator("#sidebarCollapseBtn").click()
    await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)


@test("Sortable columns exist")
async def _(page):
    count = await page.locator(".tabulator-col.tabulator-sortable").count()
    assert count >= 4, f"Expected at least 4 sortable columns, got {count}"


async def open_log_panel(page):
    if "collapsed" in (await page.locator("#logPanel").get_attribute("class") or ""):
        await page.locator("#logPanelCollapseBtn").click()
    await page.locator("#logPanel:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)


# The same path a node's uavcan.diagnostic.Record takes from the WebSocket.
INGEST_DIAGNOSTIC = """([ago, severity, text]) => ingestLogEvent({
    subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
    timestamp_unix: Date.now() / 1000 - ago,
    attributes: [{attribute: 'severity', value: severity}, {attribute: 'text', value: text}]})"""


@test("Cards: a diagnostic's severity by name, as the log panel names it, and a zero timestamp as unknown")
async def _(page):
    card = await page.evaluate("""() => {
        const div = document.createElement('div');
        div.innerHTML = renderSubjectTable('Publishers', [{subjectId: 8184, messageType: 'uavcan.diagnostic.Record.1.1',
            rate: 0.3, attributes: [{attribute: 'timestamp', value: 0, unit: 'microsecond'},
                                    {attribute: 'severity', value: 3}, {attribute: 'text', value: 'hi'}]}]);
        const other = document.createElement('div');
        other.innerHTML = renderSubjectTable('Publishers', [{subjectId: 1300, messageType: 'ns.Status.1.0', rate: 1,
            attributes: [{attribute: 'severity', value: 3}, {attribute: 'timestamp', value: 5, unit: 'microsecond'}]}]);
        const text = (d) => [...d.querySelectorAll('.metric')].map((m) =>
            [m.querySelector('.metric-key').textContent, m.querySelector('.metric-val-text').textContent,
             m.querySelector('.metric-val').title]);
        return [text(div), text(other)];
    }""")
    assert card == [[["timestamp", "unknown", "0"], ["severity", "NOTICE", "3"], ["text", "hi", "hi"]],
                    [["severity", "3", "3"], ["timestamp", "5", "5"]]], f"Cards: {card}"


@test("History: Cynitor's register writes and firmware updates are rows of the node's history")
async def _(page):
    rows = await page.evaluate("""() => {
        const div = document.createElement('div');
        div.innerHTML = renderHistoryTimeline([
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'cordicor.test.bits', value: '1 0 1', read_back: 'true, false, true', status: 'ok'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'limit', value: '<b>5</b>', status: 'timeout'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'gain', value: '5', status: 'not_applied'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'firmware_update', detail: {file: 'motor-1.2.app.bin'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'firmware_update',
             detail: {file: 'motor-1.3.app.bin', client: '192.168.1.20'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'service_call',
             detail: {service_id: 435, status: 'ok', latency_ms: 12, client: '<i>10.0.0.5</i>'}}]);
        return [...div.querySelectorAll('.hist-row')].map((r) =>
            [r.querySelector('.hist-badge').textContent, r.querySelector('.hist-detail').textContent]);
    }""")
    assert rows == [["Register write", "cordicor.test.bits = 1 0 1 · read back true, false, true"],
                    ["Register write", "limit = <b>5</b> · no answer"],
                    ["Register write", "gain = 5 · not taken by the node"],
                    ["Firmware update", "from motor-1.2.app.bin"],
                    ["Firmware update", "from motor-1.3.app.bin · by 192.168.1.20"],
                    ["Service call", "svc 435 · ok · 12ms · by <i>10.0.0.5</i>"]], f"Rows: {rows}"


@test("Log panel shows diagnostics' severity, in time order")
async def _(page):
    await open_log_panel(page)
    await page.evaluate(INGEST_DIAGNOSTIC, [1, 5, "e2e newer error"])
    await page.evaluate(INGEST_DIAGNOSTIC, [5, 4, "e2e older warning"])  # arrives late
    rows = page.locator("#logList .log-row", has_text="e2e ")
    texts = [" ".join(t.split()) for t in await rows.all_inner_texts()]
    assert len(texts) == 2, f"Expected 2 rows, got {texts}"
    assert "WARN" in texts[0] and "older warning" in texts[0], f"Older row should come first: {texts}"
    assert "ERROR" in texts[1] and "newer error" in texts[1], f"Newer row should come last: {texts}"


@test("Log filter hides other rows and says when none match")
async def _(page):
    await open_log_panel(page)
    await page.locator("#logFilterInput").fill("older warning")
    visible = await page.locator("#logList .log-row:not(.hidden)").all_inner_texts()
    assert len(visible) == 1 and "older warning" in visible[0], f"Filter left: {visible}"
    await page.locator("#logFilterInput").fill("no such message")
    hint = page.locator("#logEmpty")
    assert await hint.is_visible(), "No hint when the filter matches nothing"
    assert "match" in (await hint.text_content()).lower(), f"Unexpected hint: {await hint.text_content()}"
    await page.locator("#logFilterInput").fill("")


@test("Cyphal v1.1 traffic is noticed under the CAN status")
async def _(page):
    notice = page.locator("#canV11Notice")
    assert await notice.is_hidden(), "Notice shown without v1.1 traffic"
    await page.evaluate("""() => {
        state.canConnected = true;
        state.cyphalV11 = {transfers: 3, nodes: [110], subject_count: 1, subject_ids: [54580],
                           last_seen_unix: Date.now() / 1000};
        renderV11Notice(); }""")
    try:
        text = await notice.inner_text()
        assert "v1.1" in text and "110" in text and "not decoded" in text, f"Notice reads: {text}"
    finally:
        await page.evaluate("state.canConnected = false; state.cyphalV11 = null; renderV11Notice()")


@test("The dashboard asks in its own dialog: no script calls the browser's confirm, prompt or alert")
async def _(page):
    native = re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|prompt|alert)\(")
    found = [f"{path.name}:{number}" for path in sorted(WEBSITE_DIR.glob("*.js"))
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if native.search(line) and not line.lstrip().startswith("//")]
    assert not found, f"The browser's own dialog asks in {found}: use askConfirm or askText (state.js)"
