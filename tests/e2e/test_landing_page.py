#!/usr/bin/env python3
"""E2E tests for the Cynitor landing page and the Graph tab.

Runs against the static frontend only; no backend is required. The landing
page's assertions describe the disconnected state; the Graph tab is fed
through the same paths live data takes (see "Graph tab" below).

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Usage:
    python3 test_landing_page.py

The script serves website/ itself on port 5500 unless something already
answers there (your own dev server, for instance), and shuts its server down
afterwards. Set CYNITOR_E2E_URL to test a frontend hosted elsewhere; the
script then never starts a server.
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
# order registered, and the first one navigates, so keep it first.

TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn
    return register


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


# ── Graph tab ──
#
# Fed through the paths live data takes: /api/nodes polling stores its reply
# in state.latestNodesPayload, and every WebSocket message goes through
# cacheEvent(). A timer in the page plays the publishers. The first test opens
# the tab and the last one closes it, so keep them in this order.

def _graph_node(nid, name, pubs, subs, gone=False, uid_byte=0, servers=(), clients=()):
    return {"node_id": nid, "unique_id": [uid_byte] * 16, "unique_id_hex": bytes([uid_byte] * 16).hex(),
            "uptime": 100, "has_disappeared": gone, "has_responded_to_getinfo": True, "name": name,
            "publishers": pubs, "subscribers": subs, "clients": list(clients), "servers": list(servers),
            "last_seen": []}


def _graph_ghost(last_nid, name, pubs, uid_byte):
    # A device whose node-ID another device took: keyed by its unique-ID.
    return {**_graph_node(None, name, pubs, [], gone=True, uid_byte=uid_byte), "last_node_id": last_nid, "_ghost": True}


GRAPH_NODES = {"node_count": 4, "nodes": {
    "10": _graph_node(10, "org.example.imu", [1100, 7509], [], uid_byte=1, servers=[100]),
    "20": _graph_node(20, "org.example.flight_controller", [1200, 7509], [1100, 7509], uid_byte=2, clients=[100]),
    "30": _graph_node(30, "org.example.esc", [7509], [1200], uid_byte=3),
    "40": _graph_node(40, "org.example.gps", [7509], [], gone=True, uid_byte=4),
    "uid:" + "aa" * 16: _graph_ghost(37, "org.example.old_sensor_a", [1500], 0xAA),
    "uid:" + "bb" * 16: _graph_ghost(38, "org.example.old_sensor_b", [1600], 0xBB),
}}

# Node 10 publishes 1100 until window.e2eImuPublishing is cleared; 20 publishes
# 1200; 10, 20 and 30 send Heartbeat at 1 Hz each; 40 is offline.
GRAPH_START = """(payload) => {
    state.dashboardConnected = true;
    state.canConnected = true;
    state.latestNodesPayload = payload;
    const ev = (subject_id, publisher_node_id, message_type, rate, subject_rate, payload_bytes, attributes = []) =>
        ({subject_id, publisher_node_id, message_type, rate, subject_rate, payload_bytes, attributes,
          timestamp_unix: Date.now() / 1000});
    let tick = 0;
    window.e2eImuPublishing = true;
    window.e2eGraphFeed = setInterval(() => {
        cacheEvent(ev(1200, 20, 'Vector4_0_1', 10, 10, 16));
        if (window.e2eImuPublishing) cacheEvent(ev(1100, 10, 'Vector3_1_0', 10, 10, 12));
        if (tick++ % 10) return;
        for (const [nid, health] of [[10, 'NOMINAL'], [20, 'NOMINAL'], [30, 'CAUTION']]) {
            cacheEvent(ev(7509, nid, 'Heartbeat_1_0', 1, 3, 7, [{attribute: 'health', value: health}]));
        }
    }, 100);
}"""

GRAPH_STOP = """() => {
    clearInterval(window.e2eGraphFeed);
    state.dashboardConnected = false;
    state.canConnected = false;
    state.latestNodesPayload = null;
    state.latestBySubject.clear();
    state.latestByNode.clear();
    switchView('nodes');
}"""

# Edges and nodes as drawn, read back from D3's data on the SVG elements.
GRAPH_LINKS = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-link')].map(el => {
    const d = d3.select(el).datum();
    const end = (n) => typeof n === 'object' ? n.id : n;
    return [`${end(d.source)}->${end(d.target)}`, {live: el.classList.contains('graph-link--live'),
        silent: el.classList.contains('graph-link--silent'),
        arrow: el.getAttribute('marker-end'), subjects: d.subjects || (d.subjectId != null ? [d.subjectId] : [])}];
}))"""

GRAPH_NODES_DRAWN = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-node')].map(el => {
    const d = d3.select(el).datum();
    const label = [...document.querySelectorAll('#graphContainer .graph-label')].find(l => d3.select(l).datum().id === d.id);
    return [d.id, {pinned: el.classList.contains('graph-node--pinned'), fx: d.fx ?? null,
        shownId: el.querySelector('.graph-node-id')?.textContent ?? null, name: d.fullName ?? null,
        classes: el.getAttribute('class'), badge: el.querySelector('.graph-health-badge')?.textContent ?? null,
        label: label?.querySelector('.graph-label-name')?.textContent ?? null,
        status: label?.querySelector('.graph-label-status')?.textContent ?? null}];
}))"""


async def open_graph_display(page):
    if not await page.evaluate("document.getElementById('graphDisplay').open"):
        await page.locator("#graphDisplay > summary").click()


async def close_graph_display(page):
    if await page.evaluate("document.getElementById('graphDisplay').open"):
        await page.locator("#graphDisplay > summary").click()

GRAPH_LINK_STATS = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-link-label')].map(el => {
    const d = d3.select(el).datum();
    const end = (n) => typeof n === 'object' ? n.id : n;
    return [`${end(d.source)}->${end(d.target)}`, el.textContent];
}))"""


@test("Graph: a subject that stops publishing goes silent")
async def _(page):
    await page.evaluate(GRAPH_START, GRAPH_NODES)
    await page.locator("#viewTabGraph").click()
    # A new user starts without the system subjects; the tests below use Heartbeat.
    hide_system = page.locator("#graphHideSystem")
    assert await hide_system.is_checked(), "Hide system should start on"
    await hide_system.uncheck()
    imu = f"({GRAPH_LINKS})()['dev:10->sub:1100']"
    await page.wait_for_function(f"{imu}?.live === true", timeout=5000)
    await page.evaluate("window.e2eImuPublishing = false")
    # Silent after three message periods, two seconds at least.
    await page.wait_for_function(f"{imu}?.silent === true", timeout=5000)
    links = await page.evaluate(GRAPH_LINKS)
    assert not links["dev:10->sub:1100"]["live"], "A silent edge is still drawn as traffic"
    assert links["dev:20->sub:1200"]["live"], "Subject 1200, still publishing, went silent too"


@test("Graph: an edge carries its own publisher's rate, an offline one none")
async def _(page):
    await open_graph_display(page)
    await page.locator("#graphShowLinkStats").check()
    try:
        # Heartbeat's 3 Hz is three nodes at 1 Hz each.
        await page.wait_for_function(f"({GRAPH_LINK_STATS})()['dev:10->sub:7509'] === '1 Hz · 7 B'", timeout=3000)
        stats = await page.evaluate(GRAPH_LINK_STATS)
        assert stats["dev:30->sub:7509"] == "1 Hz · 7 B", f"Node 30's Heartbeat edge reads {stats['dev:30->sub:7509']!r}"
        assert stats["dev:40->sub:7509"] == "", f"Offline node 40's edge reads {stats['dev:40->sub:7509']!r}"
        links = await page.evaluate(GRAPH_LINKS)
        assert not links["dev:40->sub:7509"]["live"], "Offline node 40's edge is drawn live"
    finally:
        await page.locator("#graphShowLinkStats").uncheck()
        await close_graph_display(page)


@test("Graph: devices that lost their node-ID keep their own identity")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    ghosts = sorted((n["shownId"], n["name"]) for key, n in nodes.items() if key.startswith("dev:uid:"))
    expected = [("37", "org.example.old_sensor_a"), ("38", "org.example.old_sensor_b")]
    assert ghosts == expected, f"Displaced devices drawn as {ghosts}, of {list(nodes)}"


@test("Graph: only what is unusual is coloured, and nothing pulses")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    marked = ("graph-node--warn", "graph-node--err", "graph-node--offline")
    usual = {key: nodes[key]["classes"] for key in ("dev:10", "dev:20")
             if any(m in nodes[key]["classes"] for m in marked)}
    assert not usual, f"NOMINAL nodes are marked: {usual}"
    caution = nodes["dev:30"]
    assert "graph-node--warn" in caution["classes"] and caution["badge"] == "!", f"CAUTION node drawn as {caution}"
    offline = nodes["dev:40"]
    assert "graph-node--offline" in offline["classes"] and (offline["status"] or "").startswith("offline"), \
        f"Offline node drawn as {offline}"
    pulsing = await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node *')]
        .filter(el => getComputedStyle(el).animationName !== 'none').length""")
    assert pulsing == 0, f"{pulsing} node parts animate"


@test("Graph: subjects are labelled by their ID first")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    labels = {key: n["label"] for key, n in nodes.items() if key.startswith("sub:")}
    wrong = {key: label for key, label in labels.items() if not label.startswith(key[4:])}
    assert labels and not wrong, f"Subject labels not led by their ID: {wrong}"


@test("Graph: the layered layout puts devices and subjects in bands of their own")
async def _(page):
    await page.wait_for_function("""() => {
        const ys = (type) => [...document.querySelectorAll(`#graphContainer .graph-node--${type}`)]
            .map(el => d3.select(el).datum().y);
        return Math.max(...ys('device')) < Math.min(...ys('subject'));
    }""", timeout=5000)


@test("Graph: hovering an edge says what it carries")
async def _(page):
    mid = await page.evaluate("""() => {
        const el = [...document.querySelectorAll('#graphContainer .graph-link-hit')].find(e => {
            const d = d3.select(e).datum(); return d.source.id === 'dev:20' && d.target.id === 'sub:1200'; });
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""")
    await page.mouse.move(mid["x"], mid["y"])
    tooltip = page.locator("#graphTooltip")
    await tooltip.wait_for(state="visible", timeout=WAIT_MS)
    text = await tooltip.inner_text()
    await page.mouse.move(5, 5)
    assert "subject 1200" in text and "published by 20" in text and "Hz" in text, f"Tooltip reads: {text}"
    await tooltip.wait_for(state="hidden", timeout=WAIT_MS)


@test("Graph: a click selects a node without moving or pinning it")
async def _(page):
    box = await page.evaluate("""() => {
        const el = [...document.querySelectorAll('#graphContainer .graph-node')]
            .find(e => d3.select(e).datum().id === 'dev:20');
        const r = el.querySelector('circle').getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""")
    await page.mouse.click(box["x"], box["y"])
    await page.locator("#graphInfo:not(.hidden)").wait_for(timeout=WAIT_MS)
    node = (await page.evaluate(GRAPH_NODES_DRAWN))["dev:20"]
    assert not node["pinned"] and node["fx"] is None, f"The click pinned the node: {node}"


@test("Graph: the open inspector keeps its buttons while it refreshes, and draws rate sparklines")
async def _(page):
    # Rebuilt each second, its buttons were swapped out under the pointer and
    # clicks were lost. Sparklines move each second; the buttons must stay.
    kept = await page.evaluate("""() => new Promise(done => {
        const panel = document.getElementById('graphInfo');
        const before = [...panel.querySelectorAll('button')];
        setTimeout(() => {
            const after = [...panel.querySelectorAll('button')];
            done(before.length > 0 && before.length === after.length && before.every((b, i) => b === after[i]));
        }, 2500);
    })""")
    sparks = await page.locator("#graphInfo .graph-spark polyline").count()
    await page.locator("#graphInfoClose").click()
    assert kept, "The inspector replaced its buttons while refreshing"
    assert sparks > 0, "No rate sparklines in the inspector"


@test("Graph: the inspector docks beside the canvas, follows its rows, and Esc closes it")
async def _(page):
    await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node')]
        .find(e => d3.select(e).datum().id === 'dev:20').dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
    inspector = page.locator("#graphInfo")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    canvas = await page.locator("#graphContainer .graph-svg-wrap").bounding_box()
    panel = await inspector.bounding_box()
    assert canvas["x"] + canvas["width"] <= panel["x"] + 1, f"Inspector overlaps the canvas: {canvas} / {panel}"
    await inspector.locator('[data-select="sub:1200"]').click()
    text = await inspector.inner_text()
    assert "Subject-ID" in text and "1200" in text, f"A port row did not select its subject: {text}"
    await page.keyboard.press("Escape")
    await inspector.wait_for(state="hidden", timeout=WAIT_MS)


@test("Graph: Enter in the filter goes to the match; a focused node selects on Enter")
async def _(page):
    await page.fill("#graphFilter", "30")
    await page.press("#graphFilter", "Enter")
    inspector = page.locator("#graphInfo")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    assert "org.example.esc" in await inspector.inner_text(), "Enter did not select node 30"
    await page.fill("#graphFilter", "")
    await page.keyboard.press("Escape")
    await page.evaluate("document.querySelector('#graphContainer .graph-node--device').focus()")
    label = await page.evaluate("document.activeElement.getAttribute('aria-label')")
    await page.keyboard.press("Enter")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    await page.keyboard.press("Escape")
    assert label and label.startswith("Device "), f"Focused node is named {label!r}"


@test("Graph: the drawing exports as an SVG file")
async def _(page):
    await open_graph_display(page)
    try:
        async with page.expect_download() as download:
            await page.locator("#graphExportSvg").click()
        path = await (await download.value).path()
        text = Path(path).read_text()
        assert text.startswith("<svg") and "org.example.esc" in text and "graph-link-hit" not in text, \
            f"Export is not the drawing: {text[:120]}"
    finally:
        await close_graph_display(page)


@test("Graph: a node heard only in Cyphal v1.1 is drawn, marked not decoded")
async def _(page):
    await page.evaluate("""() => { state.cyphalV11 = {transfers: 9, nodes: [10, 110], subject_count: 2,
        subject_ids: [54580, 54581], last_seen_unix: Date.now() / 1000}; }""")
    try:
        await page.wait_for_function(f"({GRAPH_NODES_DRAWN})()['dev:v11:110'] !== undefined", timeout=5000)
        nodes = await page.evaluate(GRAPH_NODES_DRAWN)
        node = nodes["dev:v11:110"]
        assert "graph-node--v11" in node["classes"] and node["status"] == "v1.1 · not decoded", f"v1.1 node: {node}"
        assert "dev:v11:10" not in nodes, "Node 10, known from v1.0, drawn twice"
        chips = " ".join(await page.locator("#graphStatus .graph-chip").all_inner_texts())
        assert "1 Cyphal v1.1, not decoded" in " ".join(chips.split()), f"Strip: {chips}"
    finally:
        await page.evaluate("state.cyphalV11 = null")


@test("Graph: the legend opens from its button")
async def _(page):
    legend = page.locator("#graphLegend .graph-legend-menu")
    assert await legend.is_hidden(), "Legend shown without being asked for"
    await page.locator("#graphLegend > summary").click()
    assert await legend.is_visible() and "silent" in await legend.inner_text(), "Legend did not open"
    await page.keyboard.press("Escape")
    assert await legend.is_hidden(), "Escape did not close the legend"


@test("Graph: the status strip counts what needs a look, and picks it out")
async def _(page):
    chips = [" ".join(t.split()) for t in await page.locator("#graphStatus .graph-chip").all_inner_texts()]
    for expected in ("1 offline", "2 displaced", "1 unusual health", "1 silent"):
        assert expected in chips, f"Missing {expected!r} in the strip: {chips}"
    offline = page.locator('#graphStatus [data-focus="offline"]')
    await offline.click()
    dimmed = await page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-node')]
        .map(el => [d3.select(el).datum().id, el.classList.contains('graph-dim')]))""")
    await offline.click()
    assert dimmed["dev:40"] is False and all(d for key, d in dimmed.items() if key != "dev:40"), \
        f"Picking 'offline' left these lit: {[k for k, d in dimmed.items() if not d]}"


@test("Graph: 'Open in Nodes' selects the device in the Nodes tab")
async def _(page):
    await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node')]
        .find(e => d3.select(e).datum().id === 'dev:20').dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
    await page.locator("#graphInfoOpen").click()
    try:
        shown = await page.evaluate("({view: state.activeView, node: state.selectedNodeId})")
        assert shown == {"view": "nodes", "node": 20}, f"After 'Open in Nodes': {shown}"
    finally:
        await page.evaluate("clearSelectedNode()")
        await page.locator("#viewTabGraph").click()


@test("Graph: 'Nodes only' honours Hide system and shows direction")
async def _(page):
    await page.select_option("#graphView", "nodes")
    await page.locator("#graphHideSystem").check()
    try:
        links = await page.evaluate(GRAPH_LINKS)
        system_only = [key for key, link in links.items()
                       if link["subjects"] and all(s >= 6144 for s in link["subjects"])]
        assert links and not system_only, f"Edges made only of system subjects: {system_only}"
        unmarked = [key for key, link in links.items() if not link["arrow"]]
        assert not unmarked, f"Edges without an arrowhead: {unmarked}"
    finally:
        await page.locator("#graphHideSystem").uncheck()
        await page.select_option("#graphView", "node-centric")


@test("Graph: traffic animates only on request, and never under reduced motion")
async def _(page):
    live_animations = """() => [...document.querySelectorAll('#graphContainer .graph-link--live')]
        .map(el => getComputedStyle(el).animationName)"""
    names = await page.evaluate(live_animations)
    assert names and set(names) == {"none"}, f"Traffic animates by default: {names}"
    await open_graph_display(page)
    await page.locator("#graphAnimate").check()
    try:
        names = await page.evaluate(live_animations)
        assert set(names) == {"graph-link-flow"}, f"'Animate traffic' did not animate: {names}"
        await page.emulate_media(reduced_motion="reduce")
        names = await page.evaluate(live_animations)
        assert set(names) == {"none"}, f"Animations still running under reduced motion: {names}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await page.locator("#graphAnimate").uncheck()
        await close_graph_display(page)


@test("Graph: Fit brings every node into view")
async def _(page):
    svg = page.locator("#graphContainer .graph-svg-wrap > svg")
    box = await svg.bounding_box()
    # Drag the view far off, then fit it back.
    await page.mouse.move(box["x"] + 40, box["y"] + 40)
    await page.mouse.down()
    await page.mouse.move(box["x"] + box["width"] - 10, box["y"] + box["height"] - 10, steps=5)
    await page.mouse.up()
    await page.locator("#graphFit").click()
    await page.wait_for_function("""() => {
        const s = document.querySelector('#graphContainer .graph-svg-wrap > svg').getBoundingClientRect();
        return [...document.querySelectorAll('#graphContainer .graph-node')].every(el => {
            const r = el.getBoundingClientRect();
            return r.left >= s.left && r.right <= s.right && r.top >= s.top && r.bottom <= s.bottom;
        });
    }""", timeout=WAIT_MS)


@test("Graph: service calls are drawn from client to server")
async def _(page):
    links = await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-link--svc')]
        .map(el => { const d = d3.select(el).datum(); return [d.source.id, d.target.id, d.services]; })""")
    assert links == [["dev:20", "dev:10", [100]]], f"Service edges: {links}"


@test("Graph: recent restarts and node-ID conflicts are said on the node")
async def _(page):
    # The backend's event log, answered here: node 10 restarted two minutes ago.
    async def events(route):
        now = time.time()
        body = {"now_unix": now, "events": [{"id": 1, "node_id": 10, "unique_id": None, "detail": None,
                                             "timestamp_unix": now - 125, "event_type": "restart_suspected"}]}
        await route.fulfill(json=body, headers={"Access-Control-Allow-Origin": "*"})

    await page.route("**/api/nodes/events*", events)
    try:
        # Polled every 5 s while the tab is open.
        await page.wait_for_function(f"({GRAPH_NODES_DRAWN})()['dev:10'].status === 'restarted 2m ago'", timeout=8000)
        chips = await page.locator("#graphStatus .graph-chip").all_inner_texts()
        assert any("1 restarted" in " ".join(c.split()) for c in chips), f"Strip: {chips}"
    finally:
        await page.unroute("**/api/nodes/events*", events)


@test("Graph: resizing the window keeps the drawing at screen scale")
async def _(page):
    size = page.viewport_size
    await page.set_viewport_size({"width": 1000, "height": 700})
    try:
        await page.wait_for_function("""() => {
            const svg = document.querySelector('#graphContainer .graph-svg-wrap > svg');
            return svg.getAttribute('viewBox') === `0 0 ${svg.parentNode.clientWidth} ${svg.parentNode.clientHeight}`;
        }""", timeout=WAIT_MS)
    finally:
        await page.set_viewport_size(size)
        await page.evaluate(GRAPH_STOP)


# ── Nodes and Subjects tables ──
#
# /api/nodes is answered here and polled as in a live session; messages go
# through cacheEvent(), from a timer in the page. Nothing else refreshes the
# tables, as with a bus gone quiet. The first test starts it all and the last
# one stops it, so keep them in this order.

TABLES_NODES = {"node_count": 6, "nodes": {
    "10": _graph_node(10, "org.example.imu", [1100, 7509], [], uid_byte=1),
    "20": _graph_node(20, "org.example.flight_controller", [1200, 1300, 7509], [1100], uid_byte=2),
    "21": _graph_node(21, "org.example.fc_backup", [1300, 7509], [], uid_byte=5),
    "30": _graph_node(30, "org.example.esc", [7509], [1200, 1300], uid_byte=3),
    "40": _graph_node(40, "org.example.gps", [7509], [], gone=True, uid_byte=4),
    "uid:" + "aa" * 16: _graph_ghost(37, "org.example.old_sensor", [1500], 0xAA),
}}

# 10 publishes 1100 until e2eFeeds[1100] is cleared; 20 publishes 1200, a
# vector, at the rate in e2eFeeds.rate1200; 1300 has two publishers, 20 at
# 10 Hz (value 3) and 21 at e2eFeeds.rate21, 5 Hz (value 300); 10, 20, 21 and
# 30 send Heartbeat at 1 Hz.
TABLES_START = """() => {
    state.dashboardConnected = true;
    state.canConnected = true;
    const ev = (subject_id, publisher_node_id, rate, subject_rate, attributes = []) =>
        ({subject_id, publisher_node_id, message_type: 'Real32_1_0', rate, subject_rate, payload_bytes: 4,
          attributes, timestamp_unix: Date.now() / 1000});
    const value = (v) => [{attribute: 'value', value: v}];
    let tick = 0;
    window.e2eFeeds = {1100: true, rate1200: 10, rate21: 5};
    window.e2eTablesFeed = setInterval(() => {
        if (e2eFeeds[1100]) cacheEvent(ev(1100, 10, 10, 10, value(1)));
        cacheEvent(ev(1200, 20, e2eFeeds.rate1200, e2eFeeds.rate1200, [{attribute: 'velocity', value: [2, 3, 4]}]));
        cacheEvent(ev(1300, 20, 10, 15, value(3)));
        if (tick % 2 === 0) cacheEvent(ev(1300, 21, e2eFeeds.rate21, 15, value(300)));
        if (tick++ % 10) return;
        for (const [nid, health, vssc] of [[10, 'NOMINAL', 0], [20, 'NOMINAL', 0], [21, 'WARNING', 0], [30, 'CAUTION', 42]]) {
            cacheEvent({...ev(7509, nid, 1, 4, [{attribute: 'health', value: health}, {attribute: 'vssc', value: vssc}]),
                        message_type: 'Heartbeat_1_0'});
        }
    }, 100);
    getAllNodes();
    startNodesPolling();
}"""

TABLES_STOP = """() => {
    clearInterval(window.e2eTablesFeed);
    stopNodesPolling();
    switchView('nodes');
    clearSelectedNode();
    state.dashboardConnected = false;
    state.canConnected = false;
    state.latestNodesPayload = null;
    state.latestBySubject.clear();
    state.latestByNode.clear();
    renderNodesTable();
}"""

TABLES_PAYLOAD = {"body": TABLES_NODES}


async def _answer_nodes(route):
    await route.fulfill(json=TABLES_PAYLOAD["body"], headers={"Access-Control-Allow-Origin": "*"})


def _cell(table, row, field):
    # null until the row is there (getRow gives false)
    return f"({table}.getRow({row!r}) || null)?.getCell('{field}').getElement().innerText.trim()"


def _card_rate(subject_id):
    return f"document.querySelector('#selectedNodeContent .subject-card[data-subject=\"{subject_id}\"] .card-rate')?.innerText.trim()"


@test("Tables: a subject that stops publishing loses its rate, in the table and on its card")
async def _(page):
    await page.route("**/api/nodes", _answer_nodes)
    await page.evaluate(TABLES_START)
    await page.evaluate("setSelectedNode(10)")
    # 10 Hz on 1100 and Heartbeat's 1 Hz.
    await page.wait_for_function(f"{_cell('nodesTabulator', 10, 'rate')} === '11.0 Hz'", timeout=5000)
    await page.wait_for_function(f"{_card_rate(1100)} === '10.0 Hz'", timeout=3000)
    before = await page.evaluate("getTotalMessageRate()")
    await page.evaluate("e2eFeeds[1100] = false")
    # Silent after three message periods, two seconds at least; no message
    # refreshes the table, the node poll does.
    await page.wait_for_function(f"{_cell('nodesTabulator', 10, 'rate')} === '1.0 Hz'", timeout=5000)
    await page.wait_for_function(f"{_card_rate(1100)} === 'silent'", timeout=3000)
    dot = await page.evaluate("!!document.querySelector('.subject-card[data-subject=\"1100\"] .live-dot')")
    assert not dot, "A silent subject's card still shows the live dot"
    after = await page.evaluate("getTotalMessageRate()")
    assert abs(before - after - 10) < 0.5, f"Total rate went from {before} to {after}, not 10 less"


@test("Tables: a card's rate follows the live rate; a subscriber's card shows the subject's")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.wait_for_function(f"{_card_rate(1200)} === '10.0 Hz'", timeout=3000)
    await page.evaluate("e2eFeeds.rate1200 = 50")
    await page.wait_for_function(f"{_card_rate(1200)} === '50.0 Hz'", timeout=3000)
    # Node 30 subscribes 1300, which 20 sends at 10 Hz and 21 at 5 Hz.
    await page.evaluate("setSelectedNode(30)")
    await page.locator('.detail-tab[data-tab="subscribers"]').click()
    try:
        await page.wait_for_function(f"{_card_rate(1300)} === '15.0 Hz'", timeout=3000)
    finally:
        await page.locator('.detail-tab[data-tab="publishers"]').click()
        await page.evaluate("e2eFeeds.rate1200 = 10")


@test("Tables: offline nodes that lost their node-ID stay last; health sorts by severity")
async def _(page):
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    try:
        await page.evaluate("nodesTabulator.setSort('_sortId', 'desc')")
        order = await page.evaluate(ids)
        assert order[0] == 40 and order[-1] == "uid:" + "aa" * 16, f"ID descending: {order}"
        await page.evaluate("nodesTabulator.setSort('health', 'desc')")
        health = await page.evaluate("nodesTabulator.getRows('active').map(r => r.getData().health)")
        assert health[:4] == ["WARNING", "CAUTION", "NOMINAL", "NOMINAL"], f"Health descending: {health}"
        assert (await page.evaluate(ids))[-1] == "uid:" + "aa" * 16, "Health descending: the ghost is not last"
    finally:
        await page.evaluate("nodesTabulator.setSort('_sortId', 'asc')")


@test("Tables: a sorted table sorts again as values change, not under the pointer")
async def _(page):
    first = "nodesTabulator.getRows('active')[0].getData().id"
    await page.mouse.move(5, 5)  # off the table
    await page.evaluate("nodesTabulator.setSort('rate', 'desc')")
    try:
        assert await page.evaluate(first) == 20, "Node 20 sends the most"
        await page.locator("#nodesTable .tabulator-row").first.hover()
        await page.evaluate("e2eFeeds.rate21 = 50")
        await page.wait_for_timeout(4500)
        assert await page.evaluate(first) == 20, "Rows moved under the pointer"
        await page.mouse.move(5, 5)
        await page.wait_for_function(f"{first} === 21", timeout=5000)
    finally:
        await page.evaluate("e2eFeeds.rate21 = 5; nodesTabulator.setSort('_sortId', 'asc')")


@test("Tables: ID filters match whole IDs; the selected row shows no grey through")
async def _(page):
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    try:
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', '2')")
        assert await page.evaluate(ids) == [], "ID '2' matched nodes 20 and 21"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', '10, 21')")
        assert await page.evaluate(ids) == [10, 21], "ID '10, 21'"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', ''); nodesTabulator.setHeaderFilterValue('publishers', '130')")
        assert await page.evaluate(ids) == [], "Publishers '130' matched 1300"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('publishers', '1300')")
        assert await page.evaluate(ids) == [20, 21], "Publishers '1300'"
    finally:
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', ''); nodesTabulator.setHeaderFilterValue('publishers', '')")
    grey = await page.evaluate("getComputedStyle(document.querySelector('#nodesTable .tabulator-table')).backgroundColor")
    assert grey != "rgb(102, 102, 102)", "The table under the rows is the theme's grey"


@test("Tables: VSSC shows each heartbeat's vendor status code, 0 muted")
async def _(page):
    vssc = lambda nid: _cell('nodesTabulator', nid, 'vssc')  # noqa: E731
    assert await page.evaluate(vssc(30)) == "42", "Node 30 sends VSSC 42"
    assert await page.evaluate(vssc(40)) == "-", "An offline node has none"
    muted = await page.evaluate("nodesTabulator.getRow(10).getCell('vssc').getElement().querySelector('.text-muted') !== null")
    assert muted, "VSSC 0, the usual, is not muted"


@test("Tables: rows are reached and selected by keyboard")
async def _(page):
    # ↓ goes into the rows at the selected one, else at the first: none selected here.
    await page.evaluate("clearSelectedNode()")
    await page.focus("#nodesTable .tabulator-tableholder")
    try:
        for key in ("ArrowDown", "ArrowDown", "Enter"):
            await page.keyboard.press(key)
            await page.wait_for_timeout(150)
        assert await page.evaluate("state.selectedNodeId") == 20, "↓ ↓ Enter did not select node 20"
        await page.keyboard.press("Escape")
        on_holder = await page.evaluate("document.activeElement.classList.contains('tabulator-tableholder')")
        assert on_holder, "Escape did not leave the rows"
    finally:
        await page.evaluate("clearSelectedNode()")


@test("Tables: a port list shows its first IDs and how many more; the tooltip has it whole")
async def _(page):
    cell = "nodesTabulator.getRow(20).getCell('publishers').getElement()"
    shown = await page.evaluate(f"{cell}.innerText.trim()")
    assert shown == "1200, 1300 +1", f"Node 20's publishers read {shown!r}"
    title = await page.evaluate(f"{cell}.querySelector('.port-ids').title")
    assert title == "1200, 1300, 7509", f"Its tooltip: {title!r}"


@test("Tables: the Nodes strip counts what needs a look, and a count picks those rows out")
async def _(page):
    strip = page.locator("#nodesStatus")
    await page.wait_for_function("document.getElementById('nodesStatus').innerText.includes('unusual health')", timeout=3000)
    text = " ".join((await strip.inner_text()).split())
    assert text == "6 nodes 1 offline 1 displaced 2 unusual health Compact", f"Strip: {text!r}"
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    await strip.locator('[data-focus="health"]').click()
    try:
        assert await page.evaluate(ids) == [21, 30], "'unusual health' picks out nodes 21 and 30"
    finally:
        await strip.locator('[data-focus="health"]').click()
    assert len(await page.evaluate(ids)) == 6, "Clicked again, the count lets the rows go"


@test("Tables: Compact makes the rows of both tables shorter, and is remembered")
async def _(page):
    height = "Math.round(document.querySelector('#nodesTable .tabulator-row').getBoundingClientRect().height)"
    normal = await page.evaluate(height)
    await page.locator("#nodesStatus [data-density]").click()
    try:
        await page.wait_for_function(f"{height} < {normal}", timeout=3000)
        # Settings are written a moment after a change.
        await page.wait_for_function(
            "JSON.parse(localStorage.getItem('cynitor.dashboard.settings.v1')).compactRows === true", timeout=2000)
    finally:
        await page.locator("#nodesStatus [data-density]").click()
    await page.wait_for_function(f"{height} === {normal}", timeout=3000)


@test("Tables: a node that lost its node-ID keeps its alias, and is forgotten only when confirmed")
async def _(page):
    ghost = "uid:" + "aa" * 16
    forgotten = []

    async def identity(route):
        forgotten.append(route.request.method)
        await route.fulfill(json={"status": "ok"}, headers={"Access-Control-Allow-Origin": "*"})

    await page.route("**/api/identity/*", identity)
    try:
        await page.locator(f"#nodesTable .tabulator-row", has_text="old_sensor").locator(
            '.tabulator-cell[tabulator-field="name"]').dblclick()
        await page.locator(".name-input").fill("bench sensor")
        await page.keyboard.press("Enter")
        await page.wait_for_function(f"""nodesTabulator.getRow('{ghost}').getCell('name').getElement()
            .querySelector('.name-display')?.textContent === 'bench sensor'""", timeout=3000)
        delete = page.locator("#nodesTable .tabulator-row", has_text="bench sensor").locator(".ghost-delete-btn")
        page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
        await delete.click()
        await page.wait_for_timeout(300)
        assert forgotten == [], f"Forgotten without asking: {forgotten}"
        page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
        await delete.click()
        await page.wait_for_timeout(300)
        assert forgotten == ["DELETE"], f"Not forgotten once confirmed: {forgotten}"
    finally:
        await page.unroute("**/api/identity/*", identity)
        await page.evaluate(f"setNodeAlias('{'aa' * 16}', ''); clearSelectedNode()")


@test("Tables: the Clients tab says the bus is not connected")
async def _(page):
    await page.evaluate("setSelectedNode(20); state.canConnected = false")
    await page.locator('.detail-tab[data-tab="clients"]').click()
    try:
        text = await page.locator("#selectedNodeContent").inner_text()
        assert "CAN bus not connected" in text, f"Clients tab, CAN down: {text!r}"
    finally:
        await page.evaluate("state.canConnected = true")
        await page.locator('.detail-tab[data-tab="publishers"]').click()
        await page.evaluate("clearSelectedNode()")


PLOT_PANELS = "[...document.querySelectorAll('.detail-plot-area .panel-label')].map(e => e.textContent)"


@test("Plots: a node's card plots its own messages, a vector one line per element")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1300"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'value'", timeout=3000)
        top = await page.evaluate("""Math.max(...[...document.querySelectorAll('.detail-plot-area .panel-y-axis .tick text')]
            .map(t => Number(t.textContent)))""")
        assert top < 10, f"Node 20's plot of 1300 reaches {top}: node 21's values (300) are in it"
        await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        card = await page.evaluate("document.querySelector('.subject-card[data-subject=\"1200\"] .metric-val-text').textContent")
        assert card == "[2, 3, 4]", f"The vector's card reads {card!r}"
    finally:
        await page.evaluate("clearSelectedNode()")


@test("Plots: the Nodes tab's plot has the Subjects tab's controls, shared")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1300"]').click()
    try:
        await page.locator('.detail-plot-area .plot-window-btn[data-secs="300"]').click(timeout=3000)
        assert await page.evaluate("state.plotTimeWindow") == 300, "5m did not set the plot window"
        assert await page.locator(".detail-plot-area .plot-pause-btn").count() == 1, "No pause button"
    finally:
        await page.evaluate("state.plotTimeWindow = 60; saveSettings(); clearSelectedNode()")


@test("Plots: Subjects plots each publisher of a subject apart")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    # A cell, not the row's middle, where a column's resize handle may be.
    row = page.locator("#subjectsTable .tabulator-row", has_text="1300").first.locator(
        '.tabulator-cell[tabulator-field="messageType"]')
    await row.click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'value · n20,value · n21'", timeout=3000)
    finally:
        await row.click()  # closes the plot
    # Types read one way, though the backend gives a class name (Heartbeat_1_0).
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:7509', 'messageType'))
    assert shown == "uavcan.node.Heartbeat.1.0", f"Heartbeat's type reads {shown!r}"


@test("Plots: Fill Rate keeps up once a field's history is full")
async def _(page):
    key = "1200:velocity[0]"
    # A full history, 3600 points: six minutes of a 10 Hz subject.
    await page.evaluate(f"""(() => {{ const now = Date.now() / 1000;
        state.subjectHistory.set('{key}', Array.from({{length: 3600}}, (_, i) => ({{t: now - 360 + i / 10, v: 2, n: 20}})));
    }})()""")
    await page.evaluate("openSubjectPlot(subjectsTabulator.getRow('sub:1200').getData())")
    fill = page.locator('.plot-controls input[aria-label="Fill rate interpolation"]')
    try:
        await fill.check()
        await page.wait_for_timeout(3000)
        behind = await page.evaluate(f"Date.now() / 1000 - _detailPlotCfg._smoothBufs.get('{key}').at(-1).t")
        assert behind < 1.5, f"With Fill Rate on, the plot is {behind:.1f} s behind"
    finally:
        await fill.uncheck()
        await page.evaluate("closeSubjectPlot()")


@test("Compare: a subject several nodes publish is compared one publisher at a time")
async def _(page):
    await page.locator("#viewTabCompare").click()
    await page.locator(".compare-add-btn", has_text="+ Add Graph").click()
    card = page.locator(".compare-graph-card").last
    try:
        subject = card.locator(".plot-compare-subject")
        await subject.dispatch_event("mousedown")  # fills the list
        await subject.select_option("1300")
        publisher = card.locator(".plot-compare-publisher")
        assert await publisher.is_visible(), "No publisher choice for a subject two nodes publish"
        await card.locator(".plot-compare-attr").select_option("value")
        await publisher.select_option("21")
        await card.locator(".plot-compare-picker .plot-compare-add").first.click()
        await page.wait_for_function(
            "[...document.querySelectorAll('.compare-graph-card .plot-legend-item')].some(e => e.dataset.series === 'S1300 · value · n21')",
            timeout=3000)
        values = await page.evaluate("[...new Set(plotData('1300:value@21').map(p => p.v))]")
        assert values == [300], f"Node 21's series holds {values}"
    finally:
        await card.locator('[aria-label="Remove graph"]').click()


@test("Tables: Subjects say how long ago they were heard, and how many bytes a second")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    seen = lambda row: _cell('subjectsTabulator', row, 'age')  # noqa: E731
    await page.wait_for_function(f"/^\\d+s ago$/.test({seen('sub:1100')})", timeout=3000)
    warn = await page.evaluate("""subjectsTabulator.getRow('sub:1100').getCell('age').getElement()
        .querySelector('span').classList.contains('status-warn')""")
    assert warn, "A silent subject's age is not amber"
    assert await page.evaluate(seen("sub:1200")) == "now", "A live subject is not heard 'now'"
    # 1300: 4-byte messages at 15 Hz over its two publishers.
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:1300', 'bytesPerSec'))
    assert shown == "60 B/s", f"Subject 1300 shows {shown!r}"


@test("Tables: the Subjects strip lists one kind, and picks out the silent")
async def _(page):
    strip = page.locator("#subjectsStatus")
    rows = "subjectsTabulator.getRows('active').map(r => r.getData()._rowId)"
    await page.wait_for_function("document.getElementById('subjectsStatus').innerText.includes('1 silent')", timeout=3000)
    try:
        await strip.locator('[data-focus="silent"]').click()
        assert await page.evaluate(rows) == ["sub:1100"], "'silent' picks out subject 1100"
        await strip.locator('[data-focus="silent"]').click()
        await strip.locator('[data-kind="Service"]').click()
        assert await page.evaluate(rows) == [], "No node here serves anything"
    finally:
        await strip.locator('[data-kind="all"]').click()
    assert len(await page.evaluate(rows)) == 4, "All lists every subject again"


@test("Tables: Subjects says silent, and refreshes with nothing arriving")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    await page.wait_for_function(f"{_cell('subjectsTabulator', 'sub:1100', 'rate')} === 'silent'", timeout=5000)
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:1300', 'rate'))
    assert shown == "15.0 Hz", f"Subject 1300, published by two nodes, shows {shown}"
    # The bus goes quiet: no message arrives to refresh anything.
    await page.evaluate("clearInterval(window.e2eTablesFeed)")
    silent = " && ".join(f"{_cell('subjectsTabulator', row, 'rate')} === 'silent'"
                         for row in ("sub:1200", "sub:1300", "sub:7509"))
    await page.wait_for_function(silent, timeout=6000)


@test("Tables: a table scrolled down is still there after a tab switch")
async def _(page):
    # Enough nodes, each with a subject of its own, to scroll both tables.
    many = {str(n): _graph_node(n, f"org.example.node_{n}", [2000 + n], [], uid_byte=n) for n in range(50, 170)}
    TABLES_PAYLOAD["body"] = {"nodes": {**TABLES_NODES["nodes"], **many}}
    in_view = """(id) => { const h = document.querySelector('#' + id + ' .tabulator-tableholder');
        const r = h.getBoundingClientRect();
        return {top: Math.round(h.scrollTop), rows: [...h.querySelectorAll('.tabulator-row')].filter(x => {
            const b = x.getBoundingClientRect(); return b.bottom > r.top && b.top < r.bottom; }).length}; }"""
    try:
        for here, there in (("nodes", "subjects"), ("subjects", "nodes")):
            table = f"{here}Table"
            await page.locator(f"#viewTab{here.capitalize()}").click()
            await page.wait_for_function(f"{here}Tabulator.getDataCount() > 100", timeout=5000)
            # Until the table has laid out its new rows, it cannot scroll that far.
            await page.wait_for_function(f"""(() => {{ const h = document.querySelector('#{table} .tabulator-tableholder');
                h.scrollTop = 1500; return h.scrollTop === 1500; }})()""", timeout=5000)
            await page.wait_for_timeout(500)
            await page.locator(f"#viewTab{there.capitalize()}").click()
            await page.wait_for_timeout(500)
            await page.locator(f"#viewTab{here.capitalize()}").click()
            await page.wait_for_timeout(1000)
            seen = await page.evaluate(in_view, table)
            assert seen["rows"] > 0 and abs(seen["top"] - 1500) < 2, f"{here.capitalize()} after a tab switch: {seen}"
    finally:
        TABLES_PAYLOAD["body"] = TABLES_NODES
        await page.evaluate(TABLES_STOP)
        await page.unroute("**/api/nodes", _answer_nodes)


# ── Compare tab ──
#
# Fed as live data is (cacheEvent), by a timer in the page: node 10 publishes
# 1100 (value 80 ± 5), 1700 (a current of about 1 mA) and 1400 (value 10,
# until e2eCompare.mute1400 is set) at 10 Hz; node 20 joins it on 1400 (value
# 50) once e2eCompare.join1400 is set. Each test starts the feed, and in its
# finally stops it and removes its graphs.

COMPARE_START = """() => {
    state.dashboardConnected = true;
    state.canConnected = true;
    const ev = (subject_id, publisher_node_id, attribute, value) => ({subject_id, publisher_node_id,
        message_type: 'Real32_1_0', rate: 10, subject_rate: 10, payload_bytes: 4,
        attributes: [{attribute, value}], timestamp_unix: Date.now() / 1000});
    window.e2eCompare = {join1400: false, mute1400: false};
    window.e2eCompareFeed = setInterval(() => {
        const t = Date.now() / 1000;
        cacheEvent(ev(1100, 10, 'value', 80 + 5 * Math.sin(t)));
        cacheEvent(ev(1700, 10, 'current', 0.00095 + 0.0001 * Math.sin(t)));
        if (!e2eCompare.mute1400) cacheEvent(ev(1400, 10, 'value', 10));
        if (e2eCompare.join1400) cacheEvent(ev(1400, 20, 'value', 50));
    }, 100);
    switchView('compare');
}"""

COMPARE_STOP = """() => {
    clearInterval(window.e2eCompareFeed);
    for (const button of [...document.querySelectorAll('.compare-graph-delete')]) button.click();
    state.savedCompareConfigs = [];
    state.subjectHistory.clear();
    state.latestBySubject.clear();
    state.latestByNode.clear();
    state.dashboardConnected = false;
    state.canConnected = false;
    switchView('nodes');
    saveSettings();
}"""

# Each graph's card: how far its content runs past it, and the plot's height to draw in.
COMPARE_CARDS = """() => [...document.querySelectorAll('.compare-graph-card')].map(c => ({
    overflow: c.scrollHeight - c.clientHeight,
    drawHeight: Number(c.querySelector('.plot-overlay')?.getAttribute('height') || 0)}))"""


async def compare_graph(page, *series):
    """A new graph with these (subject-ID, field) series, added with its picker; its card."""
    n = await page.locator(".compare-graph-card").count()
    await page.locator(".compare-add-btn", has_text="+ Add Graph").click()
    card = page.locator(".compare-graph-card").nth(n)
    for sid, field in series:
        subject = card.locator(".plot-compare-subject")
        await subject.dispatch_event("mousedown")  # fills the list
        await subject.select_option(str(sid))
        await card.locator(".plot-compare-attr").select_option(field)
        await card.locator(".plot-compare-picker .plot-compare-add").first.click()
    return card


@test("Compare: graphs that do not fit keep their height, and the list scrolls")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(500)
        for i, card in enumerate(await page.evaluate(COMPARE_CARDS)):
            assert card["overflow"] <= 1, f"Graph {i + 1}'s content runs {card['overflow']} px past its card"
            assert card["drawHeight"] >= 40, f"Graph {i + 1} has {card['drawHeight']} px to draw in"
    finally:
        await page.evaluate(COMPARE_STOP)


# How far behind now a graph's plot is: a live 1m plot ends 12 s (20% of its
# window) after now.
COMPARE_LAG = "(c) => Date.now() / 1000 + 12 - c.querySelector('.detail-plot-area')._plotCtx.xScale.domain()[1]"


@test("Compare: a graph resumed after Pause All moves on, and Pause All says what it does")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        first = await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1100, "value"))
        pause_all = page.locator(".compare-pause-all")
        await pause_all.click()
        await first.locator(".plot-pause-btn").click()  # the first one alone goes on
        await page.wait_for_timeout(2500)  # past the 2 s glide back to live
        lag = await first.evaluate(COMPARE_LAG)
        assert lag < 0.5, f"The resumed graph is {lag:.1f} s behind"
        label = await pause_all.inner_text()
        assert label == "Pause All", f"With one graph going on, the toolbar says {label!r}"
        # Paused by a click on its plot, the first is paused again: all are.
        await first.locator(".detail-plot-area svg").click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        label = await pause_all.inner_text()
        assert label == "Resume All", f"With every graph paused, the toolbar says {label!r}"
        await pause_all.click()
        paused = await page.evaluate("state.compareGraphs.map(g => g.paused)")
        assert paused == [False, False], f"Resume All left these paused: {paused}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Points of a graph's first line that fall inside its plot.
COMPARE_POINTS_SHOWN = """(c) => { const d = c.querySelector('.compare-line')?.getAttribute('d') || '';
    const w = Number(c.querySelector('.plot-overlay').getAttribute('width'));
    return d.split(/[ML]/).filter(Boolean).map(s => Number(s.split(',')[0])).filter(x => x >= 0 && x <= w).length; }"""


@test("Compare: a paused graph keeps what it showed, in a replay too")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # 1500 at 100 Hz: its history (3600 points a field) holds 36 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: 1, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await card.locator(".plot-pause-btn").click()
        shown = await card.evaluate(COMPARE_POINTS_SHOWN)
        # 36 s later the history holds none of those points; a click on the legend redraws.
        await page.evaluate("""() => { const buf = state.subjectHistory.get('1500:value'); const last = buf.at(-1).t;
            for (let i = 1; i <= 3600; i++) { buf.push({t: last + 36 + i / 100, v: 5, n: 10}); buf.shift(); } }""")
        pill = card.locator(".plot-legend-item .plot-legend-label").first
        await pill.click()
        await pill.click()
        await page.wait_for_timeout(300)
        still = await card.evaluate(COMPARE_POINTS_SHOWN)
        assert still >= shown > 1000, f"Paused with {shown} points shown; redrawn, it shows {still}"
        await card.locator('[aria-label="Remove graph"]').click()

        # A replay of a recording made on 1 October: paused, the graph stays then.
        await page.evaluate("""() => { state.replayActive = true; window.e2eReplayClock = Date.UTC(2026, 9, 1, 9) / 1000;
            window.e2eReplayFeed = setInterval(() => { e2eReplayClock += 0.1;
                cacheEvent({subject_id: 1600, publisher_node_id: 10, message_type: 'Real32_1_0', rate: 10, subject_rate: 10,
                            payload_bytes: 4, attributes: [{attribute: 'value', value: 1}], timestamp_unix: e2eReplayClock}); }, 100); }""")
        await page.wait_for_timeout(500)
        card = await compare_graph(page, (1600, "value"))
        await page.wait_for_timeout(500)
        await card.locator(".plot-pause-btn").click()
        await page.locator("#viewTabNodes").click()
        await page.locator("#viewTabCompare").click()  # draws paused graphs again
        await page.wait_for_timeout(300)
        right_edge = "(c) => c.querySelector('.detail-plot-area')._plotCtx.xScale.domain()[1]"
        recorded = "Date.UTC(2026, 9, 2) / 1000"  # before then
        assert await card.evaluate(f"(c) => ({right_edge})(c) < {recorded}"), "Paused in a replay, the graph moved to today"
        await card.locator(".plot-pause-btn").click()
        await page.wait_for_timeout(500)
        assert await card.evaluate(f"(c) => ({right_edge})(c) < {recorded}"), "Resumed in a replay, the graph went to today"
    finally:
        await page.evaluate("clearInterval(window.e2eReplayFeed); state.replayActive = false;")
        await page.evaluate(COMPARE_STOP)


COMPARE_LEGEND = "(c) => [...c.querySelectorAll('.plot-legend-item')].map(e => e.dataset.series)"


@test("Compare: a series keeps to the node it was added from")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1400, "value"))  # node 10 alone publishes 1400
        await page.evaluate("e2eCompare.join1400 = true")  # then node 20 does too
        await page.wait_for_timeout(600)
        values = await page.evaluate(
            "[...new Set(plotData(compareSeriesKey(state.compareGraphs[0].series[0])).map(p => p.v))]")
        assert values == [10], f"The series of node 10's 1400 plots {values}"
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n10"], f"The legend reads {legend}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a series with nothing to plot stays in the legend, and colours stay put")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1400, "value"), (1100, "value"))
        await page.wait_for_timeout(300)
        stroke = """(c) => [...c.querySelectorAll('.compare-line')]
            .find(l => l.__data__.name === 'S1100 · value · n10')?.getAttribute('stroke')"""
        colour = await card.evaluate(stroke)
        # 1400 goes quiet, and its history goes too, as after a reconnect.
        await page.evaluate("e2eCompare.mute1400 = true; state.subjectHistory.delete('1400:value')")
        await page.wait_for_timeout(300)
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n10", "S1100 · value · n10"], f"The legend reads {legend}"
        silent = card.locator('.plot-legend-item[data-series="S1400 · value · n10"]')
        assert "plot-legend-silent" in await silent.get_attribute("class"), "The silent series is not marked"
        assert await card.evaluate(stroke) == colour, "S1100 changed colour when S1400 went quiet"
        # With only the silent series left, it is still there to remove.
        await card.locator('.plot-legend-item[data-series="S1100 · value · n10"] .plot-legend-remove').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(COMPARE_LEGEND) == ["S1400 · value · n10"], "The silent series left the legend"
        await silent.locator(".plot-legend-remove").click()
        await page.wait_for_timeout(300)
        series = await page.evaluate("state.compareGraphs[0].series.length")
        assert series == 0, f"Removed from the legend, the graph still has {series} series"
    finally:
        await page.evaluate(COMPARE_STOP)


COMPARE_NOTE = "() => document.querySelector('.compare-graph-card .plot-empty-window')?.textContent || ''"


async def compare_note_is(page, expected):
    try:
        await page.wait_for_function(f"({COMPARE_NOTE})() === {expected!r}", timeout=2500)
    except Exception:
        shown = await page.evaluate(COMPARE_NOTE)
        raise AssertionError(f"The plot says {shown!r}, not {expected!r}") from None


@test("Compare: a graph says why it shows nothing")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        await compare_note_is(page, "")
        await page.evaluate("state.canConnected = false")
        await compare_note_is(page, "CAN bus not connected")
        await page.evaluate("state.dashboardConnected = false")
        await compare_note_is(page, "Not connected to backend")
        await page.evaluate("state.dashboardConnected = true; state.canConnected = true")
        await compare_note_is(page, "")
        # The bus goes quiet: after a while nothing is left in a 30 s window.
        await page.locator('.compare-graph-card .plot-window-btn[data-secs="30"]').click()
        await page.evaluate("""() => { clearInterval(window.e2eCompareFeed);
            for (const points of state.subjectHistory.values()) for (const p of points) p.t -= 100; }""")
        await compare_note_is(page, "No data in view")
        await page.evaluate("state.subjectHistory.clear()")
        await compare_note_is(page, "Waiting for data…")
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: saved graphs and clones keep the whole graph, and untitled ones keep apart")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await card.locator('.plot-window-btn[data-secs="300"]').click()
        # A marker and a drawing, as Shift+click and Alt+drag leave them.
        await page.evaluate("""() => { const g = state.compareGraphs[0]; const now = Date.now() / 1000;
            g.markers.push({t: now - 5, label: 'motor on', note: '', color: '#0969da', lineStyle: 'dashed'});
            g.drawings.push({points: [{t: now - 8, y: 0.2}, {t: now - 2, y: 0.6}], color: '#ef4444', width: 2, dash: 'solid'}); }""")
        await card.locator(".compare-graph-name").fill("Motor A")
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").click()
        await page.locator(".compare-saved-name", has_text="Motor A").click()
        await card.locator('[aria-label="Clone graph"]').click()
        graphs = await page.evaluate(
            "state.compareGraphs.map(g => [g.thresholds.length, g.timeWindow, g.markers.length, g.drawings.length])")
        assert graphs == [[1, 300, 1, 1]] * 3, f"Original, opened from Saved graphs, and its clone: {graphs}"
        for series in ((1100, "value"), (1700, "current")):
            unnamed = await compare_graph(page, series)
            await unnamed.locator(".compare-graph-save").click()
        names = await page.evaluate("state.savedCompareConfigs.map(c => c.name)")
        assert names == ["Motor A", "Untitled", "Untitled 2"], f"Saved graphs: {names}"
    finally:
        await page.evaluate(COMPARE_STOP)


async def compare_import(page, text):
    async with page.expect_file_chooser() as chooser:
        await page.locator(".compare-add-btn", has_text="Import Workspace").click()
    await (await chooser.value).set_files({"name": "workspace.json", "mimeType": "application/json",
                                           "buffer": text.encode()})
    await page.wait_for_timeout(300)


@test("Compare: Import checks the file first, and asks before it replaces the graphs")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []
    accept = {"answer": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if accept["answer"] else dialog.dismiss())

    page.on("dialog", on_dialog)
    shown = "() => [state.compareGraphs.length, document.querySelectorAll('.compare-graph-card').length]"
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1700, "current"))
        await compare_import(page, "not a workspace")
        assert await page.evaluate(shown) == [2, 2], "A file that is not JSON changed the graphs"
        await compare_import(page, '{"graphs": []}')
        assert asked, "Import replaced the graphs without asking"
        assert await page.evaluate(shown) == [2, 2], "Answered no, Import still replaced the graphs"
        accept["answer"] = True
        await compare_import(page, '{"graphs": [{"name": "imported", "series": [{"subjectId": 1100, "attribute": "value"}],'
                                   ' "drawings": [{"color": "#f00"}]}, 5], "saved": [null, {"name": "kept", "series": []}]}')
        assert await page.evaluate(shown) == [1, 1], f"Imported, the graphs and cards are {await page.evaluate(shown)}"
        saved = await page.evaluate("state.savedCompareConfigs.map(c => c.name)")
        assert saved == ["kept"], f"Saved graphs from the file: {saved}"
        await page.locator(".compare-saved-btn").click()
        assert await page.locator(".compare-saved-name", has_text="kept").is_visible(), "The saved graphs do not open"
        await page.keyboard.press("Escape")
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


@test("Compare: plots take the colours of the theme in use, loaded in it too")
async def _(page):
    theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    token = "getComputedStyle(document.documentElement).getPropertyValue('--plot-1').trim()"
    stroke = "document.querySelector('.compare-graph-card .compare-line')?.getAttribute('stroke')"
    started_in = await page.evaluate(theme)
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        for _ in range(2):  # to the other theme, and back
            await page.locator("#themeToggle").click()
            await page.wait_for_timeout(400)
            line, wanted = await page.evaluate(f"[{stroke}, {token}]")
            assert line == wanted, f"In {await page.evaluate(theme)}, a line is {line}, not the theme's {wanted}"
        # Dark when the page loads, as for someone who keeps it dark.
        if await page.evaluate(theme) != "dark":
            await page.locator("#themeToggle").click()
        await page.evaluate("clearInterval(window.e2eCompareFeed); state.dashboardConnected = false;"
                            " state.canConnected = false; _writeSettingsNow()")
        await page.reload(wait_until="load")
        first, wanted = await page.evaluate(f"[PLOT_COLORS[0], {token}]")
        assert first == wanted, f"Loaded in dark, plots start with {first}, not the theme's {wanted}"
    finally:
        if await page.evaluate(theme) != started_in:
            await page.locator("#themeToggle").click()
        await page.evaluate(COMPARE_STOP)


@test("Compare: the wheel scrolls past the graphs, Ctrl+wheel zooms one")
async def _(page):
    await page.evaluate(COMPARE_START)
    cards = page.locator(".compare-cards")

    async def over_first_plot():
        await cards.evaluate("(c) => { c.scrollTop = 0; }")
        await page.wait_for_timeout(200)
        box = await page.locator(".compare-graph-card").first.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await over_first_plot()
        await page.mouse.wheel(0, 300)
        await page.wait_for_timeout(300)
        scrolled, zoom = await cards.evaluate("(c) => [c.scrollTop, state.compareGraphs[0]._zoom]")
        assert scrolled > 0 and zoom == 1, f"The wheel over a plot scrolled {scrolled} px and zoomed it to {zoom}"
        await over_first_plot()
        await page.keyboard.down("Control")
        await page.mouse.wheel(0, -300)
        await page.keyboard.up("Control")
        zoom = await page.evaluate("state.compareGraphs[0]._zoom")
        assert zoom > 1, f"Ctrl+wheel up left the plot at zoom {zoom}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the y-axis fits what is in view, thresholds included")
async def _(page):
    await page.evaluate(COMPARE_START)
    ticks = """() => [...document.querySelectorAll('.compare-graph-card .plot-compare-overlay .panel-y-axis .tick text')]
        .map(t => Number(t.textContent.replace(/,/g, '')))"""
    try:
        # 1100 read 1000 once, four minutes ago; live, it reads 80 ± 5.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1100:value', [{t: now - 240, v: 1000, n: 10}, {t: now - 239.9, v: 80, n: 10}]); }""")
        await page.wait_for_timeout(500)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        top = max(await page.evaluate(ticks))
        assert top < 100, f"With 75..85 in view, the y-axis goes up to {top}"
        await card.locator('[aria-label="Threshold value"]').fill("100")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await page.wait_for_timeout(400)
        y, height = await card.evaluate("""(c) => [Number(c.querySelector('.plot-threshold line').getAttribute('y1')),
            Number(c.querySelector('.plot-overlay').getAttribute('height'))]""")
        assert 0 <= y <= height, f"The threshold at 100 is drawn at y {y:.0f}, off the plot (0..{height:.0f})"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the tooltip keeps a value's digits, and has none for a series gone quiet")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1700, "current"), (1400, "value"))
        # 1400 went quiet 10 s ago.
        await page.evaluate("""() => { e2eCompare.mute1400 = true;
            for (const p of state.subjectHistory.get('1400:value')) p.t -= 10; }""")
        await page.wait_for_timeout(300)
        x = await card.evaluate("(c) => c.querySelector('.detail-plot-area')._plotCtx.xScale(Date.now() / 1000 - 0.3)")
        box = await card.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + x, box["y"] + 20)
        await page.wait_for_timeout(200)
        values = dict(await card.evaluate("""(c) => [...c.querySelectorAll('.plot-tooltip-row')]
            .map(r => [r.querySelector('.plot-tooltip-name').textContent, r.querySelector('.plot-tooltip-val').textContent])"""))
        current = values.get("S1700 · current · n10")
        assert current and 0.0008 < float(current) < 0.0011, f"A current of about 1 mA reads {current!r}"
        quiet = values.get("S1400 · value · n10")
        assert quiet == "–", f"Quiet for 10 s, S1400 reads {quiet!r} now"
    finally:
        await page.mouse.move(0, 0)
        await page.evaluate(COMPARE_STOP)


@test("Compare: subject 0 is compared like any other")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('0:value', Array.from({length: 20}, (_, i) => ({t: now - 2 + i / 10, v: 7, n: 10}))); }""")
        card = await compare_graph(page)
        subject = card.locator(".plot-compare-subject")
        await subject.dispatch_event("mousedown")
        await subject.select_option("0")
        field = card.locator(".plot-compare-attr")
        assert not await field.is_disabled(), "Subject 0 picked, its fields stay disabled"
        await field.select_option("value")
        await card.locator(".plot-compare-picker .plot-compare-add").first.click()
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S0 · value · n10"], f"The legend reads {legend}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a threshold's pill hides its line, and its style stays a line")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator('[aria-label="Threshold label"]').fill("limit")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await page.wait_for_timeout(300)
        pill = card.locator('.plot-legend-item[data-series="limit"]')
        await pill.locator(".plot-legend-label").click()
        await page.wait_for_timeout(300)
        assert await card.locator(".plot-threshold").count() == 0, "Hidden from the legend, the threshold is still drawn"
        await pill.locator(".plot-legend-label").click()
        await page.wait_for_timeout(300)
        assert await card.locator(".plot-threshold").count() == 1, "Shown again, the threshold is not drawn"
        for _ in range(4):
            await pill.locator(".plot-legend-style").click()
        style = await page.evaluate("state.compareGraphs[0].thresholds[0].style")
        assert style == "solid", f"Four clicks on its style from dashed made it {style!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: with Fill Rate on, a cleared history leaves no old line")
async def _(page):
    await page.evaluate(COMPARE_START)
    drawn = "(c) => [...c.querySelectorAll('.compare-line')].filter(l => l.getAttribute('d')).length"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('input[aria-label="Fill rate interpolation"]').check()
        await page.wait_for_timeout(1500)
        assert await card.evaluate(drawn) == 1, "With Fill Rate on, the series is not drawn"
        # A disconnect clears the history, and nothing comes after it.
        await page.evaluate("clearInterval(window.e2eCompareFeed); state.subjectHistory.clear()")
        await page.wait_for_timeout(1500)
        assert await card.evaluate(drawn) == 0, "The history cleared, Fill Rate still draws the old line"
    finally:
        await page.evaluate(COMPARE_STOP)


# Keep last: it reloads the page with every other host unreachable.
@test("Dashboard works with no internet access")
async def _(page):
    own_host = urlparse(BASE_URL).hostname
    external = []  # scripts, stylesheets and fonts asked of other hosts

    async def offline(route):
        request = route.request
        if urlparse(request.url).hostname == own_host:
            await route.continue_()
            return
        if request.resource_type in ("script", "stylesheet", "font"):
            external.append(request.url)
        await route.abort("internetdisconnected")

    await page.route("**/*", offline)
    try:
        await page.reload(wait_until="load")
        await page.wait_for_selector(".tabulator-col-title", timeout=5000)
        loaded = await page.evaluate("typeof d3 === 'object' && typeof Tabulator === 'function'")
        assert loaded, "D3 or Tabulator did not load without internet access"
        assert not external, f"Requested from other hosts: {external}"
    finally:
        await page.unroute("**/*", offline)


# ── Runner ──

async def run_tests():
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        for name, fn in TESTS:
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
    RESULT_FILE.write_text("\n".join(lines) + "\n")
    print(f"\nResults written to {RESULT_FILE}")
    return passed, failed


def main():
    try:
        with frontend_server():
            results = asyncio.run(run_tests())
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        results = [("Test setup", "FAIL", str(e))]

    passed, failed = write_results(results)
    print(f"\n{'=' * 40}")
    print(f"  PASSED: {passed}  |  FAILED: {failed}")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
