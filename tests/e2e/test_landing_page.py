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
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

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
        await card.locator(".plot-series-filter").fill("1300 value")
        offered = await card.evaluate("(c) => [...c.querySelectorAll('.plot-series-list input')].map(i => i.dataset.key)")
        assert offered == ["1300:value@20", "1300:value@21"], f"For a subject two nodes publish, the list offers {offered}"
        await card.locator('.plot-series-list input[data-key="1300:value@21"]').check()
        await page.wait_for_function(
            "[...document.querySelectorAll('.compare-graph-card .plot-legend-item')].some(e => e.dataset.series === 'S1300 · value · n21')",
            timeout=3000)
        values = await page.evaluate("[...new Set(plotData('1300:value@21').map(p => p.v))]")
        assert values == [300], f"Node 21's series holds {values}"
    finally:
        await card.locator('[aria-label="Remove graph"]').click()


@test("Plots: Compare opens what a node's plot shows in a new Compare graph")
async def _(page):
    await page.locator("#viewTabNodes").click()
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        button = page.locator("#selectedNodeContent .plot-to-compare")
        assert await button.count() == 1, "The plot has no Compare button"
        await button.click()
        assert await page.evaluate("state.activeView") == "compare", "Compare did not open the Compare tab"
        keys = await page.evaluate("state.compareGraphs.at(-1).series.map(compareSeriesKey)")
        assert keys == ["1200:velocity[0]@20", "1200:velocity[1]@20", "1200:velocity[2]@20"], f"The new graph holds {keys}"
        card = page.locator(".compare-graph-card").last
        await page.wait_for_function(
            "document.querySelectorAll('.compare-graph-card:last-child .plot-legend-item').length === 3", timeout=3000)
        assert await card.locator(".compare-graph-name").input_value() == "S1200", "The new graph is not named for its subject"
    finally:
        await page.evaluate("""() => { const g = state.compareGraphs.at(-1);
            if (g) document.querySelector(`[data-graph-id="${g.id}"] .compare-graph-delete`)?.click();
            switchView('nodes'); clearSelectedNode(); }""")


@test("Plots: a graph made from a plot before Compare has opened gets an id of its own")
async def _(page):
    # As after a reload: a graph kept from before, and the Compare tab not
    # opened yet, so nothing has counted the graphs there are.
    await page.evaluate("""() => { state.compareGraphs.push(newCompareGraph({name: 'Kept', series: []}, 'cg_1'));
        _compareGraphIdCounter = 0; }""")
    await page.locator("#viewTabNodes").click()
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        await page.locator("#selectedNodeContent .plot-to-compare").click()
        ids = await page.evaluate("state.compareGraphs.map(g => g.id)")
        assert len(set(ids)) == len(ids), f"Graphs share an id, and so a card: {ids}"
    finally:
        await page.evaluate("""() => { for (const b of [...document.querySelectorAll('.compare-graph-delete')]) b.click();
            state.compareGraphs.length = 0; saveSettings(); switchView('nodes'); clearSelectedNode(); }""")


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
    const ask = window.confirm;
    window.confirm = () => true;  // a graph with markers asks before it goes
    for (const button of [...document.querySelectorAll('.compare-graph-delete')]) button.click();
    window.confirm = ask;
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


async def compare_pick(card, sid, field, node=None):
    """Ticks a series in a graph's list of series, found by typing in its filter."""
    await card.locator(".plot-series-filter").fill(f"S{sid} · {field}")
    key = f"{sid}:{field}" + (f"@{node}" if node is not None else "")
    match = "=" if node is not None else "^="  # any publisher's, else that node's
    await card.locator(f'.plot-series-list input[data-key{match}"{key}"]').first.check()
    await card.locator(".plot-series-filter").fill("")


async def compare_graph(page, *series):
    """A new graph with these (subject-ID, field[, node]) series, ticked in its list; its card."""
    n = await page.locator(".compare-graph-card").count()
    await page.locator(".compare-add-btn", has_text="+ Add Graph").click()
    card = page.locator(".compare-graph-card").nth(n)
    for s in series:
        await compare_pick(card, *s)
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


# The Display row's box that lets a click on the plot pause it (off by default).
CLICK_PAUSES = "A click on the plot pauses it"

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
        await first.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await first.locator(".detail-plot-area svg").click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        label = await pause_all.inner_text()
        assert label == "Resume All", f"With every graph paused, the toolbar says {label!r}"
        await pause_all.click()
        paused = await page.evaluate("state.compareGraphs.map(g => g.paused)")
        assert paused == [False, False], f"Resume All left these paused: {paused}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph paused by a click on its plot says so on its own pause button, to screen readers too")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        button = card.locator(".plot-pause-btn")
        assert await button.get_attribute("aria-pressed") == "false", "Running, the pause button is not an unpressed toggle"
        await card.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await card.locator(".detail-plot-area svg").click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)  # past the 250 ms that tells a click from a double-click
        assert await page.evaluate("state.compareGraphs[0].paused"), "A click on the plot did not pause it"
        says = await button.inner_text()
        lit = await button.evaluate("(b) => b.classList.contains('active')")
        assert says == "▶" and lit, f"Paused, the graph's own button reads {says!r}{'' if lit else ', unlit'}"
        assert await button.get_attribute("aria-pressed") == "true", "Paused, the button is not pressed for a screen reader"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a click on a plot pauses it only when the graph's Click pauses is on, and it is kept")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        plot = card.locator(".detail-plot-area svg")
        await plot.click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        assert not await page.evaluate("state.compareGraphs[0].paused"), "A plain click paused the graph"
        await card.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await plot.click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        assert await page.evaluate("state.compareGraphs[0].paused"), "With Click pauses on, a click did not pause the graph"
        kept = await page.evaluate("compareGraphConfig(state.compareGraphs[0]).clickPauses")
        assert kept is True, f"Saved, the graph's Click pauses reads {kept!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Points of a graph's first line that fall inside its plot.
COMPARE_POINTS_SHOWN = """(c) => { const line = c.querySelector('.compare-line'), d = line?.getAttribute('d') || '';
    const k = line?.transform.baseVal.consolidate()?.matrix.a ?? 1;  // a path drawn scaled (in tenths of a pixel)
    const w = Number(c.querySelector('.plot-overlay').getAttribute('width'));
    return d.split(/[ML]/).filter(Boolean).map(s => k * Number(s.split(',')[0])).filter(x => x >= 0 && x <= w).length; }"""


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
        assert still >= shown > 100, f"Paused with {shown} points shown; redrawn, it shows {still}"
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
        card = await compare_graph(page, (0, "value"))
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


@test("Compare: series are picked, shown, removed and saved graphs opened by keyboard")
async def _(page):
    # The graph is made before anything is heard: its list says so, then fills as messages come.
    await page.evaluate(COMPARE_START)
    await page.evaluate("clearInterval(window.e2eCompareFeed); state.subjectHistory.clear()")
    try:
        card = await compare_graph(page)
        heard = await card.locator(".plot-series-list").inner_text()
        assert "Nothing heard yet" in heard, f"Before any message, the list says {heard!r}"
        await page.evaluate(COMPARE_START)
        await page.wait_for_timeout(1500)
        await card.locator(".plot-series-filter").focus()
        await page.keyboard.type("1100")
        await page.keyboard.press("Tab")
        await page.keyboard.press("Space")
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs[0].series.length") == 1, "Typed and ticked by keyboard, no series"
        name = "S1100 · value · n10"
        label = card.locator(f'.plot-legend-item[data-series="{name}"] .plot-legend-label')
        await label.focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        hidden = await page.evaluate(f"state.compareGraphs[0]._hidden.has({name!r})")
        assert hidden and await label.get_attribute("aria-pressed") == "false", "Enter on a series' pill does not hide it"
        await card.locator(f'.plot-legend-item[data-series="{name}"] .plot-legend-remove').focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        assert await page.evaluate("state.compareGraphs[0].series.length") == 0, "Enter on × does not remove the series"
        name_box = card.locator(".compare-graph-name")
        assert await name_box.get_attribute("aria-label") == "Graph name", "The graph's name box has no label"
        await name_box.fill("Keys")
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").focus()
        await page.keyboard.press("Enter")
        assert await page.locator(".compare-saved-btn").get_attribute("aria-expanded") == "true", "The menu does not say it is open"
        await page.keyboard.press("Tab")
        focused = await page.evaluate("document.activeElement.className + ' ' + document.activeElement.textContent")
        assert focused == "compare-saved-name Keys", f"Tab from Saved graphs goes to {focused!r}"
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        graphs = await page.evaluate("[state.compareGraphs.length, state.savedCompareConfigs.length]")
        assert graphs == [2, 1], f"Enter on a saved graph: [graphs, saved graphs] = {graphs}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the marker form saves on Enter and closes on Escape")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        box = await card.locator(".plot-overlay").bounding_box()
        form = card.locator(".plot-marker-form")
        for x, keys in ((0.6, ("t", "h", "r", "o", "t", "t", "l", "e", "Enter")), (0.3, ("Escape",))):
            await page.keyboard.down("Shift")
            await page.mouse.click(box["x"] + box["width"] * x, box["y"] + 40)
            await page.keyboard.up("Shift")
            await page.wait_for_timeout(200)
            assert await form.count() == 1, "Shift+click opens no marker form"
            for key in keys:
                await page.keyboard.press(key)
            await page.wait_for_timeout(200)
            assert await form.count() == 0, f"{keys[-1]} leaves the marker form open"
        markers = await page.evaluate("state.compareGraphs[0].markers.map(m => m.label)")
        assert markers == ["throttle"], f"After Enter, then Escape, the markers are {markers}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Counts the page's mousemove listeners and the graphs' crosshair-sync ones
# that are added and not yet removed, from now on.
COUNT_LISTENERS = """() => {
    if (!window.e2eListeners) {
        const counted = window.e2eListeners = {moves: new Set(), syncs: new Set()};
        const proto = EventTarget.prototype, add = proto.addEventListener, remove = proto.removeEventListener;
        const kept = (target, type) => (type === 'mousemove' && target === window ? counted.moves
            : type === 'crosshair-sync' ? counted.syncs : null);
        proto.addEventListener = function (type, fn, opts) { kept(this, type)?.add(fn); return add.call(this, type, fn, opts); };
        proto.removeEventListener = function (type, fn, opts) { kept(this, type)?.delete(fn); return remove.call(this, type, fn, opts); };
        window.e2eListenersOff = () => { proto.addEventListener = add; proto.removeEventListener = remove; delete window.e2eListeners; };
    }
    return [e2eListeners.moves.size, e2eListeners.syncs.size];
}"""


@test("Compare: graphs made, dragged and removed leave no listeners behind")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        before = await page.evaluate(COUNT_LISTENERS)
        for _ in range(5):
            card = await compare_graph(page, (1100, "value"))
            await page.wait_for_timeout(200)
            box = await card.locator(".plot-overlay").bounding_box()
            await page.mouse.move(box["x"] + 200, box["y"] + 30)
            await page.mouse.down()  # a drag pans the plot
            await page.mouse.move(box["x"] + 260, box["y"] + 30)
            await page.mouse.up()
            await card.locator('[aria-label="Remove graph"]').click()
        # A crosshair moving over the graphs lets removed ones' handlers go.
        await page.evaluate("""document.querySelector('.compare-cards')
            .dispatchEvent(new CustomEvent('crosshair-sync', {detail: {t: 0, source: null}}))""")
        after = await page.evaluate(COUNT_LISTENERS)
        assert after == before, f"Five graphs later, [page mousemove, crosshair-sync] listeners went {before} -> {after}"
    finally:
        await page.evaluate("window.e2eListenersOff?.()")
        await page.evaluate(COMPARE_STOP)


# A graph's first line: how many points it draws, the highest point (least y),
# the plot's width and height.
COMPARE_LINE = """(c) => { const line = c.querySelector('.compare-line'), d = line?.getAttribute('d') || '';
    const k = line?.transform.baseVal.consolidate()?.matrix.a ?? 1;  // a path drawn scaled (in tenths of a pixel)
    const ys = d.split(/[ML]/).filter(Boolean).map(s => k * Number(s.split(',')[1]));
    const overlay = c.querySelector('.plot-overlay');
    return {points: ys.length, top: Math.min(...ys), width: Number(overlay.getAttribute('width')),
            height: Number(overlay.getAttribute('height'))}; }"""


@test("Compare: a line draws what is in view, at most two points a pixel, and no spike is lost")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # Six minutes of 1500 at 10 Hz: 300 points of them in a 30 s window.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 360 + i / 10, v: Math.sin(i / 5), n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        line = await card.evaluate(COMPARE_LINE)
        assert line["points"] < 400, f"A 30 s window shows 300 points; the line draws {line['points']}"
        # 36 s of 1600 at 100 Hz, one spike in it, all in view: more points than pixels.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1600:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: i === 1800 ? 50 : Math.sin(i / 20), n: 10}))); }""")
        card = await compare_graph(page, (1600, "value"))
        await card.locator('.plot-window-btn[data-secs="0"]').click()
        await page.wait_for_timeout(400)
        line = await card.evaluate(COMPARE_LINE)
        assert line["points"] <= 2 * line["width"] + 4, f"{line['width']:.0f} px wide, the line draws {line['points']} points"
        assert line["top"] < 0.1 * line["height"], f"The spike is lost: the line's top is at y {line['top']:.0f}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a redraw writes lines in whole tenths of a pixel, and filters a publisher's points once")
async def _(page):
    await page.evaluate(COMPARE_START)
    # The first line's path: whole numbers only, scaled back, its stroke as wide as before.
    written = """(c) => { const l = c.querySelector('.compare-line');
        return {fractions: /\\./.test(l.getAttribute('d')), transform: l.getAttribute('transform'),
                stroke: l.getAttribute('vector-effect')}; }"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value", 10))
        await page.wait_for_timeout(300)
        path = await card.evaluate(written)
        assert path == {"fractions": False, "transform": "scale(0.1)", "stroke": "non-scaling-stroke"}, \
            f"The line is written {path}"
        assert await card.evaluate(COMPARE_POINTS_SHOWN) > 0, "Scaled back, the line's points are not in the plot"
        # Asked twice while nothing new came, a publisher's points are filtered once; a new point makes them anew.
        same = await page.evaluate("""() => { clearInterval(window.e2eCompareFeed);
            return plotData('1100:value@10') === plotData('1100:value@10'); }""")
        assert same, "A publisher's points are filtered again with nothing new in the history"
        fresh = await page.evaluate("""() => { const before = plotData('1100:value@10');
            cacheEvent({subject_id: 1100, publisher_node_id: 10, message_type: 'Real32_1_0', rate: 10, subject_rate: 10,
                payload_bytes: 4, attributes: [{attribute: 'value', value: 99}], timestamp_unix: Date.now() / 1000});
            const after = plotData('1100:value@10');
            return after !== before && after[after.length - 1].v === 99; }""")
        assert fresh, "After a new point, a publisher's points are the old ones"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: synced graphs pause, take a time window and zoom together, and a graph that joins takes their view")
async def _(page):
    await page.evaluate(COMPARE_START)
    view = "(i) => { const g = state.compareGraphs[i]; return [g.paused, g.pausedAt, g.timeWindow, g._zoom]; }"
    try:
        await page.wait_for_timeout(300)
        a = await compare_graph(page, (1100, "value"))
        b = await compare_graph(page, (1100, "value"))
        c = await compare_graph(page, (1100, "value"))
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        assert await a.locator(".compare-sync-btn").get_attribute("aria-pressed") == "true", "Sync is not shown as on"
        await a.locator(".plot-pause-btn").click()
        va, vb, vc = [await page.evaluate(view, i) for i in range(3)]
        assert va[0] and va[:2] == vb[:2], f"Paused, the synced graphs read {va} and {vb}"
        assert not vc[0], "A graph not synced was paused with them"
        assert await b.locator(".plot-pause-btn").inner_text() == "▶", "The other synced graph's button does not say it is paused"
        await b.locator('.plot-window-btn[data-secs="300"]').click()
        assert (await page.evaluate(view, 0))[2] == 300, "A window picked on one synced graph is not the other's"
        assert "active" in (await a.locator('.plot-window-btn[data-secs="300"]').get_attribute("class")), "Its 5m button is not lit"
        assert (await page.evaluate(view, 2))[2] == 60, "A graph not synced took the window"
        await a.locator(".compare-edit-btn").click()  # its plot, not its editing rows, under the pointer
        await a.scroll_into_view_if_needed()
        box = await a.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await page.keyboard.down("Control")
        await page.mouse.wheel(0, -300)
        await page.keyboard.up("Control")
        za, zb = (await page.evaluate(view, 0))[3], (await page.evaluate(view, 1))[3]
        assert za > 1 and za == zb, f"Zoomed, the synced graphs read {za} and {zb}"
        await b.locator(".plot-pause-btn").click()  # resumed from the other: both go on
        assert not (await page.evaluate(view, 0))[0], "Resumed from one synced graph, the other stays paused"
        await c.locator(".compare-sync-btn").click()  # joins: takes their view
        assert await page.evaluate(view, 2) == await page.evaluate(view, 0), "A graph that joins keeps its own view"
        assert await page.evaluate("compareGraphConfig(state.compareGraphs[0]).sync") is True, "Sync is not kept with the graph"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: synced graphs show each other's markers, and a graph that leaves no longer does")
async def _(page):
    await page.evaluate(COMPARE_START)
    marks = "(i) => [...document.querySelectorAll('.compare-graph-card')[i].querySelectorAll('.plot-marker text')].map(t => t.textContent)"
    try:
        await page.wait_for_timeout(300)
        a = await compare_graph(page, (1100, "value"))
        b = await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1100, "value"))  # not synced
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        await b.locator(".plot-pause-btn").click()  # both paused: no tick redraws them
        await page.evaluate("""() => { const g = state.compareGraphs[0];
            g.markers.push({t: g.pausedAt - 5, label: 'motor on', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        shown = [await page.evaluate(marks, i) for i in range(3)]
        assert shown == [["motor on"], ["motor on"], []], f"The markers each graph shows: {shown}"
        said = await b.locator(".plot-marker title").text_content()
        assert "marked on" in said, f"On hover, a marker from another graph says {said!r}"
        await b.locator(".compare-sync-btn").click()  # leaves the group
        assert await page.evaluate(marks, 1) == [], "A graph that left the group still shows its markers"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a synced graph above another synced graph leaves its time labels to it, and draws taller")
async def _(page):
    await page.evaluate(COMPARE_START)
    labels = "(c) => [...c.querySelectorAll('.plot-x-axis .tick text')].filter(t => t.textContent).length"
    height = "(c) => Number(c.querySelector('.plot-overlay').getAttribute('height'))"
    try:
        await page.wait_for_timeout(300)
        a, b, c = [await compare_graph(page, (1100, "value")) for _ in range(3)]
        for card in (a, b, c):
            await card.locator(".compare-edit-btn").click()  # all alike: header, legend, plot
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        await page.wait_for_timeout(400)
        assert await a.evaluate(labels) == 0, "A synced graph above another synced graph keeps its time labels"
        assert await b.evaluate(labels) > 0 and await c.evaluate(labels) > 0, "The last synced graph, or one not synced, has none"
        assert await a.evaluate(height) > await b.evaluate(height) + 8, "Without its labels, the graph above draws no taller"
        await b.locator(".compare-sync-btn").click()  # B leaves: nothing synced below A now
        await page.wait_for_timeout(300)
        assert await a.evaluate(labels) > 0, "With no synced graph below, a synced graph has no time labels"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: Collapse All collapses every graph, Expand All expands them, and the button says which it does")
async def _(page):
    await page.evaluate(COMPARE_START)
    button = page.locator(".compare-collapse-all")
    collapsed = "() => [...document.querySelectorAll('.compare-graph-card')].map(c => c.classList.contains('collapsed'))"
    try:
        await page.wait_for_timeout(300)
        first, *_ = [await compare_graph(page, (1100, "value")) for _ in range(3)]
        await first.locator('[aria-label="Collapse the graph to its plot"]').click()  # one collapsed on its own
        await page.wait_for_timeout(200)
        assert await button.inner_text() == "Collapse All", f"With one graph collapsed, the toolbar says {await button.inner_text()!r}"
        await button.click()
        assert await page.evaluate(collapsed) == [True, True, True], "Collapse All left some graphs expanded"
        await page.wait_for_timeout(200)
        assert await button.inner_text() == "Expand All", f"With every graph collapsed, the toolbar says {await button.inner_text()!r}"
        await button.click()
        assert await page.evaluate(collapsed) == [False, False, False], "Expand All left some graphs collapsed"
        kept = await page.evaluate("state.compareGraphs.map(g => compareGraphConfig(g).collapsed)")
        assert kept == [False, False, False], f"The graphs keep {kept}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a collapsed graph is its plot, a line of names and, at its side, its pause, window and Sync")
async def _(page):
    await page.evaluate(COMPARE_START)
    shown = "(e) => e.getClientRects().length > 0"
    plot_height = "(c) => Number(c.querySelector('.plot-overlay').getAttribute('height'))"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"), (1700, "current"))
        await card.locator(".compare-edit-btn").click()  # folded: the header alone above the legend
        await page.wait_for_timeout(300)
        tall = await card.evaluate(plot_height)
        await card.locator('[aria-label="Collapse the graph to its plot"]').click()
        await page.wait_for_timeout(400)
        assert not await card.locator(".compare-card-header").evaluate(shown), "Collapsed, the graph keeps its header"
        assert not await card.locator(".compare-card-editor").evaluate(shown), "Collapsed, the graph keeps its editing rows"
        names = await card.evaluate("""(c) => [...c.querySelectorAll('.plot-legend-label')].map(l => Math.round(l.getBoundingClientRect().top))""")
        assert len(names) == 2 and len(set(names)) == 1, f"Collapsed, the names are not one line: their tops are {names}"
        values = await card.evaluate("(c) => [...c.querySelectorAll('.plot-legend-value, .plot-legend-head')].filter(e => e.getClientRects().length).length")
        assert values == 0, f"Collapsed, the legend still shows {values} values or headings"
        assert await card.evaluate(plot_height) > tall + 20, "Collapsed, the plot is no taller"
        side = card.locator(".compare-side")
        await side.locator('[aria-label="Time window"]').select_option("300")
        await side.locator(".plot-pause-btn").click()
        await side.locator(".compare-sync-btn").click()
        g = await page.evaluate("(() => { const g = state.compareGraphs[0]; return [g.timeWindow, g.paused, g.sync, compareGraphConfig(g).collapsed]; })()")
        assert g == [300, True, True, True], f"From the side: window, paused, synced, kept collapsed read {g}"
        await side.locator('[aria-label="Expand the graph: its header and legend"]').click()
        await page.wait_for_timeout(300)
        assert await card.locator(".compare-card-header").evaluate(shown), "Expanded, the graph has no header"
        back = await card.evaluate("(c) => ['.plot-pause-btn', '.compare-sync-btn'].map(s => !!c.querySelector(`.compare-time-controls ${s}`))")
        assert back == [True, True], f"Expanded, the pause and Sync buttons are back in the header: {back}"
        assert "active" in (await card.locator('.plot-window-btn[data-secs="300"]').get_attribute("class")), "The 5m button is not lit"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: Min/Max follows the lowest and highest of the last samples, and an Add says what it is missing")
async def _(page):
    await page.evaluate(COMPARE_START)
    toasts = "() => [...document.querySelectorAll('#toastContainer .toast')].map(t => t.textContent)"
    try:
        # 1500 rose 0, 1, ... 19 over the last 20 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 20}, (_, i) => ({t: now - 19.5 + i, v: i, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await card.locator(".plot-derived-type").select_option("min_max")
        window_box = card.locator('[aria-label="Window size (samples)"]')
        assert await window_box.is_visible(), "Min/Max asks for no window of samples"
        await window_box.fill("5")
        await card.locator('[aria-label="Source A"]').select_option("1500:value@10")
        await card.locator('[aria-label="Add derived series"]').click()
        await page.wait_for_timeout(400)
        rows = await card.evaluate(COMPARE_LEGEND_ROWS)
        last = {name[:4]: values[1] for name, values in rows.items() if name.startswith(("Min5", "Max5"))}
        assert last == {"Min5": "15", "Max5": "19"}, f"Over its last 5 samples, 15 to 19, Min/Max ends at {last} ({list(rows)})"
        # Delta with no Source B, then a threshold with no value: each Add says what it is missing.
        await card.locator(".plot-derived-type").select_option("delta")
        await card.locator('[aria-label="Add derived series"]').click()
        await card.locator('[aria-label="Add threshold line"]').click()
        said = await page.evaluate(toasts)
        assert any("Source B" in t for t in said) and any("threshold" in t for t in said), f"The Adds say {said}"
        assert await page.evaluate("state.compareGraphs[0].derivedSeries.length") == 1, "Delta was added with no Source B"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph scrolled out of view is not drawn, and catches up in view")
async def _(page):
    await page.evaluate(COMPARE_START)
    cards = page.locator(".compare-cards")
    line = "(c) => c.querySelector('.compare-line')?.getAttribute('d')"
    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await cards.evaluate("(c) => { c.scrollTop = 0; }")
        await page.wait_for_timeout(500)
        first, last = page.locator(".compare-graph-card").first, page.locator(".compare-graph-card").last
        before = [await first.evaluate(line), await last.evaluate(line)]
        await page.wait_for_timeout(1000)
        after = [await first.evaluate(line), await last.evaluate(line)]
        assert after[0] != before[0], "The graph in view is not drawn"
        assert after[1] == before[1], "The graph out of view is drawn all the same"
        await cards.evaluate("(c) => { c.scrollTop = c.scrollHeight; }")
        await page.wait_for_timeout(500)
        assert await last.evaluate(line) != after[1], "Scrolled into view, the graph does not catch up"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: removing a graph with markers or drawings asks first")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []
    answer = {"yes": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if answer["yes"] else dialog.dismiss())

    page.on("dialog", on_dialog)
    graphs = "state.compareGraphs.length"
    try:
        await page.wait_for_timeout(300)
        plain = await compare_graph(page, (1100, "value"))
        await plain.locator('[aria-label="Remove graph"]').click()
        assert not asked and await page.evaluate(graphs) == 0, "A graph with no markers is not removed at once"
        card = await compare_graph(page, (1100, "value"))
        await page.evaluate("""() => state.compareGraphs[0].markers.push(
            {t: Date.now() / 1000 - 5, label: 'motor on', note: '', color: '#0969da', lineStyle: 'dashed'})""")
        await card.locator('[aria-label="Remove graph"]').click()
        assert asked and "1 marker" in asked[-1], f"Removing a graph with a marker asks {asked}"
        assert await page.evaluate(graphs) == 1, "Answered no, the graph is removed all the same"
        answer["yes"] = True
        await card.locator('[aria-label="Remove graph"]').click()
        assert await page.evaluate(graphs) == 0, "Answered yes, the graph stays"
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph's editing rows fold away under Edit, and a new graph starts on them")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        editor, edit = card.locator(".compare-card-editor"), card.locator(".compare-edit-btn")
        assert await editor.is_visible(), "A new graph does not start on its editing rows"
        await edit.click()
        assert not await editor.is_visible(), "Edit does not fold the editing rows away"
        assert await edit.get_attribute("aria-expanded") == "false", "Edit does not say it is folded"
        header, plot, whole = await card.evaluate("""(c) => [c.querySelector('.compare-card-header'),
            c.querySelector('.detail-plot-area'), c].map(e => e.getBoundingClientRect().height)""")
        assert header < 50 and plot > 0.8 * whole, f"Folded: a {header:.0f} px header, a {plot:.0f} px plot in {whole:.0f} px"
        assert await card.locator(".plot-window-btn").first.is_visible(), "Folded, the time window is out of reach"
        # A graph that has its series, opened from Saved graphs, starts on its plot.
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").click()
        await page.locator(".compare-saved-name").first.click()
        opened = page.locator(".compare-graph-card").nth(1)
        assert not await opened.locator(".compare-card-editor").is_visible(), "A saved graph opens on its editing rows"
        await edit.click()
        assert await editor.is_visible(), "Edit does not bring the editing rows back"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: series are found by any part of their name, and ticked on and off in one list")
async def _(page):
    await page.evaluate(COMPARE_START)
    keys = "(c) => [...c.querySelectorAll('.plot-series-list input')].map(i => i.dataset.key)"
    ticked = "(c) => [...c.querySelectorAll('.plot-series-list input:checked')].map(i => i.dataset.key)"
    try:
        await page.evaluate("e2eCompare.join1400 = true")  # node 20 publishes 1400 too
        await page.wait_for_timeout(400)
        card = await compare_graph(page)
        offered = await card.evaluate(keys)
        assert offered == ["1100:value@10", "1400:value@10", "1400:value@20", "1700:current@10"], f"The list offers {offered}"
        find = card.locator(".plot-series-filter")
        await find.fill("1400")
        assert await card.evaluate(keys) == ["1400:value@10", "1400:value@20"], "Filtered by 1400, the list shows others"
        for key in ("1400:value@10", "1400:value@20"):
            await card.locator(f'.plot-series-list input[data-key="{key}"]').check()
        await card.locator('.plot-series-list input[data-key="1400:value@10"]').uncheck()
        await page.wait_for_timeout(300)
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n20"], f"Ticked on, then n10 off, the legend reads {legend}"
        await card.locator('.plot-legend-item[data-series="S1400 · value · n20"] .plot-legend-remove').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(ticked) == [], "Removed from the legend, the series stays ticked in the list"
        await find.fill("current n10")  # words in any order, of the name, the node or the type
        assert await card.evaluate(keys) == ["1700:current@10"], "Two words do not find the series"
        await find.fill("no such thing")
        matched = await card.locator(".plot-series-list").inner_text()
        assert "No series match" in matched, f"Nothing matching, the list says {matched!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: with no graph, the tab says how to start, and starts one")
async def _(page):
    await page.evaluate(COMPARE_START)
    empty = page.locator(".compare-empty")
    try:
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs.length") == 0, "The test needs no graph to start from"
        assert await empty.is_visible(), "With no graph, the tab is blank"
        words = await empty.inner_text()
        assert "No graphs yet" in words and "Compare button" in words, f"The empty tab says {words!r}"
        await empty.locator("button", has_text="Add a graph").click()
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs.length") == 1, "Add a graph adds none"
        assert not await empty.is_visible(), "With a graph, the empty tab's words stay"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: its everyday buttons are plain, as in the Graph tab: colour is for the unusual")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        coloured = await page.evaluate("""() => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent)';
            document.body.append(probe);
            const accent = getComputedStyle(probe).color;
            probe.remove();
            return [...document.querySelectorAll('.compare-toolbar button, .compare-graph-save, .compare-card-editor .plot-compare-add')]
                .filter(b => getComputedStyle(b).color === accent).map(b => b.textContent.trim()); }""")
        assert not coloured, f"These everyday buttons are in the accent colour: {coloured}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a strip counts the graphs, and picks out quiet series and paused graphs")
async def _(page):
    await page.evaluate(COMPARE_START)
    strip = page.locator(".compare-status")
    says = "() => document.querySelector('.compare-status')?.innerText.replace(/\\s+/g, ' ') ?? ''"
    try:
        await page.wait_for_timeout(300)
        for _ in range(3):
            await compare_graph(page, (1100, "value"))
        last = await compare_graph(page, (1400, "value"))  # the fourth, out of view at the top
        await page.wait_for_timeout(300)
        text = await page.evaluate(says)
        assert "4 graphs · 4 series" in text and "nothing unusual" in text, f"The strip says {text!r}"
        await page.evaluate("e2eCompare.mute1400 = true")
        try:
            await page.wait_for_function(f"({says})().includes('1 series quiet')", timeout=5000)
        except Exception:
            raise AssertionError(f"1400 gone quiet, the strip says {await page.evaluate(says)!r}") from None
        await page.locator(".compare-cards").evaluate("(c) => { c.scrollTop = 0; }")
        await strip.locator('[data-focus="quiet"]').click()
        await page.wait_for_timeout(300)
        shown = await last.evaluate("""(c) => { const r = c.getBoundingClientRect(), v = c.parentElement.getBoundingClientRect();
            return r.top >= v.top - 1 && r.bottom <= v.bottom + 1; }""")
        assert shown, "The quiet count does not bring its graph into view"
        await page.locator(".compare-pause-all").click()
        await page.wait_for_function(f"({says})().includes('4 paused')", timeout=2000)
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a window longer than the history keeps says so")
async def _(page):
    await page.evaluate(COMPARE_START)
    kept = "(c) => { const k = c.querySelector('.compare-kept'); return k && !k.classList.contains('hidden') ? k.textContent : ''; }"
    beyond = "(c) => [...c.querySelectorAll('.plot-window-btn--beyond')].map(b => b.textContent)"
    try:
        # 1500 at 100 Hz: its history is full, 3600 points over 36 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: 1, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await page.wait_for_timeout(400)
        assert await card.evaluate(kept) == "kept 36 s", f"A 1m window over 36 s of history reads {await card.evaluate(kept)!r}"
        assert await card.evaluate(beyond) == ["1m", "5m", "15m"], f"Windows marked as not filling: {await card.evaluate(beyond)}"
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(kept) == "", "A 30 s window, which the history fills, still says what is kept"
        # 1100 has been heard for a moment only: its history is not full, nothing is said.
        young = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(400)
        assert await young.evaluate(kept) == "" and await young.evaluate(beyond) == [], "A history still filling is called short"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a plot carries its graph's name, not the word Compare")
async def _(page):
    await page.evaluate(COMPARE_START)
    label = "(c) => c.querySelector('.compare-panel-label')?.textContent ?? ''"
    named = "(c) => c.querySelector('.detail-plot-area svg').getAttribute('aria-label')"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(300)
        assert await card.evaluate(label) == "", f"An untitled graph's plot reads {await card.evaluate(label)!r}"
        await card.locator(".compare-graph-name").fill("Motor A")
        await page.wait_for_timeout(300)
        assert await card.evaluate(label) == "Motor A", f"Named Motor A, its plot reads {await card.evaluate(label)!r}"
        aria = await card.evaluate(named)
        assert "Motor A" in aria and "S1100 · value · n10" in aria, f"The plot is announced as {aria!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


# A graph's legend rows: series -> [label, last, min, max].
COMPARE_LEGEND_ROWS = """(c) => Object.fromEntries([...c.querySelectorAll('.plot-legend-item')].map(r =>
    [r.dataset.series, [r.querySelector('.plot-legend-label').textContent,
     ...[...r.querySelectorAll('.plot-legend-value')].map(v => v.textContent)]]))"""


@test("Compare: the legend lists each series' last, lowest and highest value in view, lined up")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # 1500 read 1 to 10 over the last 10 s, and 99 an hour ago; 1600's value is in volts.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', [{t: now - 3600, v: 99, n: 10},
                ...Array.from({length: 10}, (_, i) => ({t: now - 10 + i, v: i + 1, n: 10}))]);
            for (const t of [now - 1, now]) cacheEvent({subject_id: 1600, publisher_node_id: 10, message_type: 'Scalar_1_0',
                rate: 1, subject_rate: 1, payload_bytes: 4, attributes: [{attribute: 'value', value: 12, unit: 'volt'}],
                timestamp_unix: t}); }""")
        await page.wait_for_timeout(300)  # 1400 heard
        card = await compare_graph(page, (1500, "value"), (1400, "value"), (1600, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        rows = await card.evaluate(COMPARE_LEGEND_ROWS)
        assert rows.get("S1500 · value · n10", [])[1:] == ["10", "1", "10"], f"S1500 in view: {rows.get('S1500 · value · n10')}"
        assert rows.get("S1400 · value · n10", [])[1:] == ["10", "10", "10"], f"S1400 in view: {rows.get('S1400 · value · n10')}"
        assert "volt" in rows.get("S1600 · value · n10", [""])[0], f"S1600's row names no unit: {rows.get('S1600 · value · n10')}"
        lefts = await card.evaluate("""(c) => [...c.querySelectorAll('.plot-legend-item')]
            .map(r => Math.round(r.querySelector('.plot-legend-value').getBoundingClientRect().left))""")
        assert len(set(lefts)) == 1, f"The values do not line up: their columns start at {lefts}"
        # 1400 goes quiet, its history cleared: its row stays, with no values.
        await page.evaluate("e2eCompare.mute1400 = true; state.subjectHistory.delete('1400:value')")
        await page.wait_for_timeout(1300)
        quiet = (await card.evaluate(COMPARE_LEGEND_ROWS)).get("S1400 · value · n10", [])[1:]
        assert quiet == ["–", "–", "–"], f"Gone quiet, S1400 reads {quiet}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Parts of a graph shown, hidden or placed by an inline style, as "tag.class: style".
COMPARE_INLINE_STYLES = """(c) => [...c.querySelectorAll('[style]')]
    .filter(e => /display|cursor|position/.test(e.getAttribute('style')))
    .map(e => `${e.tagName.toLowerCase()}.${e.getAttribute('class')}: ${e.getAttribute('style')}`)"""


@test("Compare: a graph's parts show and hide by class, not inline style, and a dragged plot shows a grabbing hand")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Which of the derived row's Source B and Window boxes show.
    derived_shown = """(c) => ['Source B', 'Window size (samples)'].map(l =>
        c.querySelector(`.plot-derived-section [aria-label="${l}"]`).getClientRects().length > 0)"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        assert await card.evaluate(derived_shown) == [True, False], "Delta (A−B) should ask for Source B, not a window"
        await card.locator(".plot-derived-type").select_option("rolling_avg")
        assert await card.evaluate(derived_shown) == [False, True], "Rolling Avg should ask for a window, not Source B"
        plot = card.locator(".detail-plot-area svg")
        box = await plot.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await page.mouse.move(x, y)  # the tooltip shows
        await page.mouse.down()
        await page.mouse.move(x + 60, y, steps=4)
        cursor = await card.evaluate("(c) => getComputedStyle(c.querySelector('.plot-overlay')).cursor")
        await page.mouse.up()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 200)  # out: the tooltip hides
        assert cursor == "grabbing", f"Dragged, the plot shows the {cursor!r} cursor"
        await card.locator(".plot-legend-swatch").first.click()  # its colour picker opens
        inline = await card.evaluate(COMPARE_INLINE_STYLES)
        assert not inline, f"Shown, hidden or placed by inline styles: {inline}"
    finally:
        await page.evaluate(COMPARE_STOP)


# The colours a graph's threshold (line and legend pill), drawing and marker
# are in, and the theme's red and accent, all as computed ("rgb(...)").
COMPARE_MARK_COLOURS = """(c) => {
    const drawn = (sel, prop) => { const e = c.querySelector(sel); return e ? getComputedStyle(e)[prop] : null; };
    const token = (name) => { const probe = document.createElement('i');
        probe.style.color = `var(${name})`; document.body.appendChild(probe);
        const rgb = getComputedStyle(probe).color; probe.remove(); return rgb; };
    return {threshold: drawn('.plot-threshold line', 'stroke'),
            pill: drawn('.plot-legend-item[data-threshold-idx] .plot-legend-swatch', 'backgroundColor'),
            drawing: drawn('.plot-drawings path', 'stroke'), marker: drawn('.plot-marker line', 'stroke'),
            red: token('--error'), accent: token('--accent')}; }"""


@test("Compare: thresholds, drawings and markers left at their default colour take the theme's")
async def _(page):
    theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    started_in = await page.evaluate(theme)
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator('[aria-label="Add threshold line"]').click()
        box = await card.locator(".plot-overlay").bounding_box()
        x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")  # Alt+drag draws
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 80, y + 20, steps=4)
        await page.mouse.up()
        await page.keyboard.up("Alt")
        await page.keyboard.down("Shift")  # Shift+click marks
        await page.mouse.click(x + 40, box["y"] + 40)
        await page.keyboard.up("Shift")
        await page.keyboard.type("start")
        await page.keyboard.press("Enter")
        for _ in range(2):  # in this theme, then the other
            await page.wait_for_timeout(400)
            c, now_in = await card.evaluate(COMPARE_MARK_COLOURS), await page.evaluate(theme)
            assert c["threshold"] == c["pill"] == c["red"], \
                f"In {now_in}, a threshold is {c['threshold']} (pill {c['pill']}), not the theme's red {c['red']}"
            assert c["drawing"] == c["red"], f"In {now_in}, a drawing is {c['drawing']}, not the theme's red {c['red']}"
            assert c["marker"] == c["accent"], f"In {now_in}, a marker is {c['marker']}, not the theme's accent {c['accent']}"
            await page.locator("#themeToggle").click()
    finally:
        if await page.evaluate(theme) != started_in:
            await page.locator("#themeToggle").click()
        await page.evaluate(COMPARE_STOP)


# Each graph: is its line clipped by a clip path of its own, as tall as its plot?
COMPARE_OWN_CLIPS = """() => [...document.querySelectorAll('.compare-graph-card')].map(card => {
    const id = card.querySelector('.compare-lines')?.getAttribute('clip-path')?.match(/#([^)]+)/)?.[1];
    const clip = id && document.getElementById(id);
    return !!clip && card.contains(clip)
        && clip.querySelector('rect').getAttribute('height') === card.querySelector('.plot-overlay').getAttribute('height'); })"""


@test("Compare: each graph clips its lines to its own plot, not to the first graph's")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))  # its editing rows open: a short plot
        second = await compare_graph(page, (1100, "value"))
        await second.locator(".compare-edit-btn").click()  # folded away: a taller plot
        await second.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)
        own = await page.evaluate(COMPARE_OWN_CLIPS)
        assert own == [True, True], f"Graphs clipped by a clip path of their own, as tall as their plot: {own}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: drawings and markers scrolled past the y-axis are cut there, not drawn over it")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Are these parts of a graph clipped by its plot's own clip path?
    clipped = """(c, sels) => sels.map(sel => {
        const id = c.querySelector(sel)?.getAttribute('clip-path')?.match(/#([^)]+)/)?.[1];
        const clip = id && document.getElementById(id);
        return !!clip && c.contains(clip); })"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        # A drawing and a marker made 70 s ago: in a 1m window, partly past the y-axis.
        await page.evaluate("""() => { const g = state.compareGraphs[0], now = Date.now() / 1000;
            g.drawings.push({points: [{t: now - 70, y: 0.5}, {t: now - 20, y: 0.6}], color: '', width: 2, dash: 'solid'});
            g.markers.push({t: now - 65, label: 'gone', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        cut = await card.evaluate(clipped, [".plot-drawings", ".plot-markers"])
        assert cut == [True, True], f"Clipped to the plot, the drawings and markers: {cut}"
        # A drawing dragged past the y-axis is cut there while it is drawn, too.
        box = await card.locator(".plot-overlay").bounding_box()
        y = box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")
        await page.mouse.move(box["x"] + 60, y)
        await page.mouse.down()
        await page.mouse.move(box["x"] - 30, y, steps=4)
        drawing = await card.evaluate(clipped, [".plot-drawing-temp"])
        await page.mouse.up()
        await page.keyboard.up("Alt")
        assert drawing == [True], "A drawing dragged past the y-axis is drawn over it"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a series' and a derived series' colours are picked from the legend")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Picks a colour in a legend row's colour box, as the browser's picker would.
    pick = "(i, hex) => { i.value = hex; i.dispatchEvent(new Event('change')); }"
    strokes = "(c) => [...c.querySelectorAll('.compare-line')].map(l => l.getAttribute('stroke'))"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator(".plot-derived-type").select_option("rate")
        await card.locator('[aria-label="Source A"]').select_option("1100:value@10")
        await card.locator('[aria-label="Add derived series"]').click()
        await page.wait_for_timeout(1200)  # two points of the rate at least
        for row, hex in (('[data-derived-id]', '#123456'), (':not([data-derived-id])', '#654321')):
            await card.locator(f".plot-legend-item{row} .plot-legend-swatch").click()
            await card.locator(f".plot-legend-item{row} .plot-legend-swatch input").evaluate(pick, hex)
        await page.wait_for_timeout(300)
        colours = await page.evaluate("[state.compareGraphs[0].series[0].color, state.compareGraphs[0].derivedSeries[0].color]")
        assert colours == ["#654321", "#123456"], f"The series and derived series are coloured {colours}"
        assert sorted(await card.evaluate(strokes)) == ["#123456", "#654321"], f"The lines are {await card.evaluate(strokes)}"
    finally:
        await page.evaluate(COMPARE_STOP)


# A graph's Markers row: its markers as listed, its drawings' count, its hint.
COMPARE_MARKS = """(c) => ({marks: [...c.querySelectorAll('.compare-mark-go')].map(b => b.textContent),
    drawings: c.querySelector('.compare-marks-drawings')?.textContent ?? '',
    hint: c.querySelector('.compare-marks-hint')?.textContent ?? ''})"""


@test("Compare: a graph lists its markers and drawings, shows a marker gone off screen, and removes them")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await dialog.accept()

    page.on("dialog", on_dialog)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(300)
        row = await card.evaluate(COMPARE_MARKS)
        assert row["marks"] == [] and "Shift+click" in row["hint"] and "Alt+drag" in row["hint"], \
            f"With nothing marked, the Markers row reads {row}"
        # A marker kept from ten minutes ago: off screen in a 1m window.
        await page.evaluate("""() => { const g = state.compareGraphs[0];
            g.markers.push({t: Date.now() / 1000 - 600, label: 'start', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        row = await card.evaluate(COMPARE_MARKS)
        assert len(row["marks"]) == 1 and row["marks"][0].endswith("start"), f"The Markers row lists {row['marks']}"
        await card.locator(".compare-mark-go").click()
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs[0].paused"), "Shown from the list, the graph does not hold still"
        where = await card.evaluate("""(c) => Number(c.querySelector('.plot-marker line')?.getAttribute('x1'))
            / Number(c.querySelector('.plot-overlay').getAttribute('width'))""")
        assert 0.4 < where < 0.6, f"Shown from the list, the marker is at {where:.2f} of the plot's width, not its middle"
        await card.locator(".compare-mark-delete").click()
        assert await page.evaluate("state.compareGraphs[0].markers.length") == 0, "× leaves the marker in the graph"
        # A drawing made on the plot is counted, and cleared from the row once asked.
        box = await card.locator(".plot-overlay").bounding_box()
        x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 60, y + 20, steps=4)
        await page.mouse.up()
        await page.keyboard.up("Alt")
        await page.wait_for_timeout(300)
        row = await card.evaluate(COMPARE_MARKS)
        assert row["drawings"] == "1 drawing", f"After a drawing, the Markers row reads {row}"
        await card.locator(".compare-marks-clear").click()
        assert asked and "1 drawing" in asked[-1], f"Clear asks {asked}"
        assert await page.evaluate("state.compareGraphs[0].drawings.length") == 0, "Clear leaves the drawing"
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


# ── DSDL tab ──
#
# /api/dsdl/* is answered by _DsdlServer from a few types shaped as the
# backend sends them; saving, deleting and compiling change them as the
# backend would. Each test opens the tab on a freshly loaded page, so what
# the tab remembers (selection, editor, search) starts empty.

def _dsdl_type(full_name, port=None, source="regulated", compiled=True, text="uint8 value\n@sealed\n"):
    *namespace, short_name, major, minor = full_name.split(".")
    return {"full_name": full_name, "namespace": ".".join(namespace), "short_name": short_name,
            "version": f"{major}.{minor}", "kind": "service" if "\n---\n" in text else "message",
            "fixed_port_id": port, "source": source, "compiled": compiled, "source_text": text}


# Two types share the class name Scalar_1_0, as 46 public ones do.
DSDL_TYPES = [
    _dsdl_type("uavcan.node.Heartbeat.1.0", port=7509, text="uint32 uptime\n@sealed\n"),
    _dsdl_type("uavcan.si.unit.temperature.Scalar.1.0", text="float32 kelvin\n@sealed\n"),
    _dsdl_type("uavcan.si.unit.voltage.Scalar.1.0", text="float32 volt\n@sealed\n"),
    _dsdl_type("myapp.Reading.1.0", source="custom", text="uint16 value\n@sealed\n"),
]


class _DsdlServer:
    """The backend's /api/dsdl/* routes, over DSDL_TYPES."""

    def __init__(self):
        self.types = {t["full_name"]: dict(t) for t in DSDL_TYPES}
        self.custom_namespaces = {"myapp"}
        self.last_custom_compiled = 1.0e9
        self.requests = []          # (method, path, JSON body)
        self.gates = {}             # path -> an asyncio.Event its answer waits for
        self.compile_answer = ({"ok": True}, 200)

    def _status(self):
        custom = sum(t["source"] == "custom" for t in self.types.values())
        return {"paths": [], "public_compilable": True, "compiled": True, "last_compiled": self.last_custom_compiled,
                "last_public_compiled": 1.0e9, "last_custom_compiled": self.last_custom_compiled,
                "source_types": len(self.types) - custom, "custom_types": custom}

    def _tree(self):
        tree = {}
        for t in self.types.values():
            root, *rest = t["namespace"].split(".")
            node = tree.setdefault(root, {"children": {}, "types": []})
            for part in rest:
                node = node["children"].setdefault(part, {"children": {}, "types": []})
            # The names search looks in: "type name" is a field, "type NAME = value" a constant.
            lines = [line.split("#")[0].split() for line in t["source_text"].split("\n")]
            named = [words for words in lines if len(words) >= 2 and words[0][0] != "@"]
            node["types"].append({key: t[key] for key in ("short_name", "full_name", "version", "kind",
                                                          "fixed_port_id", "source", "compiled")}
                                 | {"field_names": [w[1] for w in named if "=" not in w],
                                    "constant_names": [w[1] for w in named if "=" in w]})
        for namespace in sorted(self.custom_namespaces):  # empty ones too
            root, *rest = namespace.split(".")
            node = tree.setdefault(root, {"children": {}, "types": []})
            node["_source"] = "custom"
            for part in rest:
                node = node["children"].setdefault(part, {"children": {}, "types": []})
                node["_source"] = "custom"
        return {"namespaces": tree}

    @staticmethod
    def _detail(t):
        sections = [[]]
        for words in (line.split() for line in t["source_text"].split("\n")):
            if words == ["---"]:
                sections.append([])
            elif len(words) == 2 and words[0][0] not in "@#":
                sections[-1].append({"type": words[0], "name": words[1]})
        fields = sections[0] if len(sections) == 1 else {"request": sections[0], "response": sections[1]}
        composite = sorted({f["type"].split("[")[0] for section in sections for f in section if "." in f["type"]})
        # A test can give a type's whole detail: its fields' comments, say.
        return {**t, "source_file": "", "fields": fields, "constants": [], "dependencies": composite,
                **t.get("detail", {})}

    def _answer(self, method, path, body):
        if path == "/api/dsdl/status":
            return self._status(), 200
        if path == "/api/dsdl/namespaces":
            return self._tree(), 200
        if path == "/api/dsdl/custom/namespaces":
            return {"namespaces": sorted(self.custom_namespaces)}, 200
        if method == "GET" and path.startswith("/api/dsdl/type/"):
            t = self.types.get(path.removeprefix("/api/dsdl/type/"))
            return (self._detail(t), 200) if t else ({"error": "Type not found"}, 404)
        if method == "POST" and path == "/api/dsdl/custom/namespace":
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", body["namespace"]):
                return {"error": "Namespace must be lowercase dotted identifiers (e.g. myapp.sensors)"}, 400
            self.custom_namespaces.add(body["namespace"])
            return {"namespace": body["namespace"], "path": ""}, 201
        if method == "POST" and path == "/api/dsdl/custom/type":
            name = f"{body['namespace']}.{body['type_name']}.{body['version']}"
            old = self.types.get(name)
            if old and not body.get("overwrite"):
                return {"error": f"Type '{name}' already exists"}, 400
            if old and old["compiled"]:
                return {"error": f"Cannot edit '{name}': type is already compiled."}, 409
            self.types[name] = _dsdl_type(name, port=body.get("fixed_port_id"), source="custom", compiled=False,
                                          text=body["source_text"])
            self.custom_namespaces.add(body["namespace"])  # created with its first type, as the server does
            return {"full_name": name, "path": ""}, 201
        if method == "DELETE" and path.startswith("/api/dsdl/custom/namespace/"):
            name = path.removeprefix("/api/dsdl/custom/namespace/")
            inside = lambda ns: ns == name or ns.startswith(f"{name}.")
            if any(inside(t["namespace"]) for t in self.types.values()):
                return {"error": f"Namespace '{name}' still has types: delete them first"}, 400
            if name not in self.custom_namespaces:
                return {"error": f"Namespace not found: {name}"}, 404
            self.custom_namespaces = {ns for ns in self.custom_namespaces if not inside(ns)}
            return {"namespace": name, "deleted": True}, 200
        if method == "DELETE" and path.startswith("/api/dsdl/custom/type/"):
            name = path.removeprefix("/api/dsdl/custom/type/")
            if self.types.pop(name, None) is None:
                return {"error": f"Type not found: {name}"}, 404
            return {"full_name": name, "deleted": True}, 200
        return {"error": "not found"}, 404

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        if path in self.gates:
            await self.gates[path].wait()
        if (request.method, path) == ("POST", "/api/dsdl/compile"):
            answer, status = self.compile_answer
        else:
            answer, status = self._answer(request.method, path, body)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})


async def dsdl_open(page, server, setup=None, connected=True, wait=True):
    """The DSDL tab on a freshly loaded page, connected to `server` or not.
    `setup` runs in the page before the tab opens (nodes, messages); `wait`
    waits for its tree."""
    await page.evaluate("""() => { state.dashboardConnected = false; _writeSettingsNow();
        localStorage.removeItem('cynitor.dsdl.state'); }""")
    await page.reload(wait_until="load")
    await page.route("**/api/dsdl/**", server.handle)
    await page.evaluate(f"state.dashboardConnected = {json.dumps(connected)}")
    if setup:
        await page.evaluate(setup)
    await page.evaluate("switchView('dsdl')")
    if wait:
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row" if connected else "#dsdlTree .dsdl-tree-empty",
                                     timeout=5000)


async def dsdl_close(page, server):
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false;
        state.latestNodesPayload = null; state.latestBySubject.clear(); state.latestByNode.clear();
        localStorage.removeItem('cynitor.dsdl.state'); }""")
    await page.unroute("**/api/dsdl/**", server.handle)


@test("DSDL: a subject's bus dot goes on its own type, not on another with its class name")
async def _(page):
    server = _DsdlServer()
    # Subject 1100 is temperature by its publisher's registers; its messages
    # name only their class, Scalar_1_0, which voltage has too.
    nodes = {"node_count": 1, "nodes": {"10": _graph_node(10, "org.example.thermo", [1100], [], uid_byte=1)},
             "subject_types": {"1100": {"type": "uavcan.si.unit.temperature.Scalar_1_0", "set_by": "registers"}}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 1100, publisher_node_id: 10, message_type: 'Scalar_1_0', rate: 10,
                    subject_rate: 10, payload_bytes: 4, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await page.wait_for_selector(".dsdl-type-row .dsdl-bus-dot", state="attached", timeout=WAIT_MS)
        dotted = await page.evaluate("""[...document.querySelectorAll('.dsdl-type-row')]
            .filter(row => row.querySelector('.dsdl-bus-dot')).map(row => row.dataset.type)""")
        assert dotted == ["uavcan.si.unit.temperature.Scalar.1.0"], f"Marked active on the bus: {dotted}"
    finally:
        await dsdl_close(page, server)


async def dsdl_new_type(page, namespace="myapp"):
    """The editor for a new type, opened from its namespace's own button."""
    await page.locator(f'#dsdlCustomTree .dsdl-ns-row[data-ns="{namespace}"]').hover()
    await page.locator(f'[data-add-type="{namespace}"]').click()
    await page.wait_for_selector("#dsdlEditorName")


async def dsdl_save(page):
    """Save, and what the editor says once the server has answered."""
    async with page.expect_response("**/api/dsdl/custom/type"):
        await page.locator("#dsdlEditorSave").click()
    await page.wait_for_function("document.getElementById('dsdlEditorStatus').textContent !== 'Saving…'",
                                 timeout=WAIT_MS)
    return await page.locator("#dsdlEditorStatus").inner_text()


@test("DSDL: a type just created saves again, as an edit")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorName").fill("Draft")
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n")
        first = await dsdl_save(page)
        await page.locator("#dsdlEditorSource").fill("uint8 a\nuint8 b\n@sealed\n")
        second = await dsdl_save(page)
        assert (first, second) == ("Saved", "Saved"), f"Saved, then saved again: {first!r}, {second!r}"
        kept = server.types["myapp.Draft.1.0"]["source_text"]
        assert kept == "uint8 a\nuint8 b\n@sealed\n", f"The server keeps {kept!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: opened before connecting, the tab stops saying 'Not connected.' once connected")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server, connected=False)
    try:
        before = await page.locator("#dsdlDetail").inner_text()
        # What connectDashboard does with the tab open.
        await page.evaluate("state.dashboardConnected = true; DsdlView.init()")
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=5000)
        after = await page.locator("#dsdlDetail").inner_text()
        assert "Not connected" in before and "Select a type to inspect" in after, \
            f"Before connecting the pane said {before!r}; after, {after!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a compiled custom type can be deleted, and the tab says how to change it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        row = page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]')
        hint = await row.get_attribute("title")
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await row.click()
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        assert await page.locator("#dsdlDeleteBtn").count() == 1, "A compiled custom type cannot be deleted"
        assert await page.locator("#dsdlEditBtn").count() == 0, "A compiled custom type offers Edit"
        pane = await page.locator(".dsdl-doc-head").inner_text()
        assert "python_compiled_messages" not in hint and "new version" in hint and "new version" in pane, \
            f"The row says {hint!r}; the pane, {pane!r}"
        await page.locator("#dsdlDeleteBtn").click()
        await page.locator("#dsdlDelOk").click()
        await page.wait_for_selector('.dsdl-type-row[data-type="myapp.Reading.1.0"]', state="detached", timeout=WAIT_MS)
        assert "myapp.Reading.1.0" not in server.types, "The server still has the type"
    finally:
        await dsdl_close(page, server)


# The theme's --error, as the browser draws it.
ERROR_COLOUR = """(() => { const probe = document.createElement('span'); probe.style.color = 'var(--error)';
    document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; })()"""


@test("DSDL: compile and editor errors are drawn in the error colour")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        error = await page.evaluate(ERROR_COLOUR)
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-compile-error", timeout=WAIT_MS)
        compile_error = await page.evaluate("getComputedStyle(document.querySelector('.dsdl-compile-error')).color")
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSave").click()  # nothing filled in
        await page.wait_for_selector("#dsdlEditorStatus.dsdl-editor-error", timeout=WAIT_MS)
        editor_error = await page.evaluate("getComputedStyle(document.getElementById('dsdlEditorStatus')).color")
        assert compile_error == editor_error == error, \
            f"The error colour is {error}; a compile error is drawn {compile_error}, an editor error {editor_error}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor's example is a type that compiles")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        example = await page.locator("#dsdlEditorSource").get_attribute("placeholder")
        # The compiler refuses a type that is neither @sealed nor has an @extent.
        assert "@sealed" in example or "@extent" in example, f"The example: {example!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a fixed port ID the compiler would refuse is not saved, and the editor says which it takes")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorName").fill("Foo")
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n")
        said = {}
        for port in ("abc", "-5", "100"):
            await page.locator("#dsdlEditorPort").fill(port)
            await page.locator("#dsdlEditorSave").click()
            await page.wait_for_timeout(300)
            said[port] = await page.locator("#dsdlEditorStatus").inner_text()
        sent = [request for request in server.requests if request[0] == "POST"]
        assert not sent and all("6144" in text for text in said.values()), f"Sent {sent}; the editor said {said}"
        await page.locator("#dsdlEditorPort").fill("7000")
        assert await dsdl_save(page) == "Saved", "A port in range is refused"
        assert server.types["myapp.Foo.1.0"]["fixed_port_id"] == 7000, "Saved without its port"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a compile keeps its button busy until it ends, and is not asked for twice")
async def _(page):
    server = _DsdlServer()
    compiled = server.gates["/api/dsdl/compile"] = asyncio.Event()

    def asked(path):
        return sum(request[1] == path for request in server.requests)

    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        # A compile writes files as it goes: the status polled every 4 s
        # changes, and the tab redraws its headers mid-compile.
        server.last_custom_compiled += 1
        reloads = asked("/api/dsdl/namespaces")
        for _ in range(40):
            if asked("/api/dsdl/namespaces") > reloads:
                break
            await page.wait_for_timeout(200)
        await page.wait_for_timeout(300)
        button = await page.evaluate("""(() => { const b = document.getElementById('dsdlCustomCompileBtn');
            return {busy: b.classList.contains('dsdl-spin'), disabled: b.disabled}; })()""")
        await page.evaluate("document.getElementById('dsdlCustomCompileBtn').click()")
        await page.wait_for_timeout(300)
        assert button == {"busy": True, "disabled": True} and asked("/api/dsdl/compile") == 1, \
            f"Redrawn mid-compile, the button is {button}; compiles asked for: {asked('/api/dsdl/compile')}"
        compiled.set()
        await page.wait_for_function("!document.getElementById('dsdlCustomCompileBtn').disabled", timeout=WAIT_MS)
    finally:
        compiled.set()
        await dsdl_close(page, server)


@test("DSDL: the namespace Create and delete-confirm buttons keep their colours under the pointer")
async def _(page):
    server = _DsdlServer()
    background = "(id) => getComputedStyle(document.getElementById(id)).backgroundColor"

    async def resting_and_hovered(button_id):
        resting = await page.evaluate(background, button_id)
        await page.locator(f"#{button_id}").hover()
        await page.wait_for_timeout(400)  # past the hover transition
        return resting, await page.evaluate(background, button_id)

    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomAddNs").click()
        create = await resting_and_hovered("dsdlNsOk")
        await page.locator("#dsdlNsCancel").click()
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlDeleteBtn").click()
        delete = await resting_and_hovered("dsdlDelOk")
        assert create[0] == create[1] and delete[0] == delete[1], \
            f"At rest and under the pointer: Create {create}, Delete {delete}"
    finally:
        await dsdl_close(page, server)


# The field names cut off by their card, or with their type's text run into them.
FIELD_NAMES_UNREADABLE = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-fcol-name')].filter((cell) => {
    const card = cell.closest('.dsdl-card').getBoundingClientRect();
    const text = (node) => { const range = document.createRange(); range.selectNodeContents(node);
        return range.getBoundingClientRect(); };
    const name = text(cell), type = text(cell.previousElementSibling);
    return name.right > card.right + 0.5 || type.right > name.left;
}).map((cell) => cell.textContent)"""


@test("DSDL: long field types wrap, and never cut off or run into the field names")
async def _(page):
    server = _DsdlServer()
    long_type = "uavcan.si.unit.electric_current.Scalar.1.0"
    server.types["myapp.Pair.1.0"] = _dsdl_type("myapp.Pair.1.0", source="custom", compiled=False,
                                                text=f"{long_type} a\n{long_type} b\n@sealed\n")
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Pair.1.0"]').click()
        await page.wait_for_selector('.dsdl-doc-title:has-text("Pair")', timeout=WAIT_MS)
        await dsdl_new_type(page)  # the editor takes half the room
        await page.wait_for_timeout(300)
        width = await page.evaluate("Math.round(document.getElementById('dsdlDetail').getBoundingClientRect().width)")
        unreadable = await page.evaluate(FIELD_NAMES_UNREADABLE)
        assert not unreadable, f"In a {width} px pane these field names are cut off or run into: {unreadable}"
    finally:
        await dsdl_close(page, server)


async def dsdl_show(page, full_name):
    """A public type's detail, opened from the tree."""
    parts = full_name.split(".")
    for depth in range(1, len(parts) - 2):
        row = page.locator(f'#dsdlTree .dsdl-ns-row[data-ns="{".".join(parts[:depth])}"]')
        if await row.get_attribute("aria-expanded") != "true" and not await row.locator(".dsdl-chev.open").count():
            await row.click()
    await page.locator(f'.dsdl-type-row[data-type="{full_name}"]').click()
    await page.wait_for_selector(f'.dsdl-doc-title:has-text("{parts[-3]}")', timeout=WAIT_MS)


@test("DSDL: when the server goes away the tab says so, and comes back with it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        await page.evaluate("disconnectAll({persist: false})")
        tree = await page.locator("#dsdlTree").inner_text()
        pane = await page.locator("#dsdlDetail").inner_text()
        assert "Connect to server" in tree and "Not connected" in pane, \
            f"Disconnected, the tree reads {tree!r} and the pane {pane!r}"
        await page.locator("#dsdlSearch").fill("Heartbeat")
        await page.wait_for_timeout(400)  # the search's debounce
        searched = await page.locator("#dsdlTreePanel .dsdl-tree-scroll").inner_text()
        assert "Heartbeat" not in searched, f"Searched while disconnected, the tree shows {searched!r}"
        await page.locator("#dsdlSearch").fill("")
        await page.wait_for_timeout(400)
        # What connectDashboard does with the tab open.
        await page.evaluate("state.dashboardConnected = true; DsdlView.init()")
        await page.wait_for_selector('.dsdl-doc-title:has-text("Heartbeat")', timeout=WAIT_MS)
    finally:
        await dsdl_close(page, server)


@test("DSDL: the tree says it is loading until the types arrive")
async def _(page):
    server = _DsdlServer()
    arrived = server.gates["/api/dsdl/namespaces"] = asyncio.Event()
    await dsdl_open(page, server, wait=False)
    try:
        await page.wait_for_timeout(300)
        loading = await page.locator("#dsdlTree").inner_text()
        arrived.set()
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=WAIT_MS)
        assert "Loading" in loading, f"While the types load, the tree reads {loading!r}"
    finally:
        arrived.set()
        await dsdl_close(page, server)


@test("DSDL: the time since the last compile follows the clock")
async def _(page):
    server = _DsdlServer()
    server.last_custom_compiled = time.time() - 120
    await dsdl_open(page, server)
    try:
        status = "#dsdlCustomHeader .dsdl-tree-status"
        before = await page.locator(status).inner_text()
        # Three hours on; the status, polled every 4 s, has not changed.
        await page.evaluate("(() => { const now = Date.now; Date.now = () => now() + 3 * 3600 * 1000; })()")
        await page.wait_for_timeout(4500)
        after = await page.locator(status).inner_text()
        assert "2m ago" in before and "3h ago" in after, \
            f"Compiled two minutes ago, the header said {before!r}; three hours on, {after!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a refused namespace or delete is said where it was asked for")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)  # an editor open: its status line is about the type in it
        await page.locator("#dsdlCustomAddNs").click()
        await page.locator("#dsdlNsInput").fill("My-NS")
        await page.locator("#dsdlNsOk").click()
        await page.wait_for_timeout(300)
        in_dialog = await page.locator("#dsdlNsDialog").inner_text()
        in_editor = await page.locator("#dsdlEditorStatus").inner_text()
        await page.locator("#dsdlNsCancel").click()
        # Gone from the server since it was shown: deleting it is refused.
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.wait_for_selector("#dsdlDeleteBtn", timeout=WAIT_MS)
        del server.types["myapp.Reading.1.0"]
        await page.locator("#dsdlDeleteBtn").click()
        await page.locator("#dsdlDelOk").click()
        await page.wait_for_timeout(300)
        bar = page.locator(".dsdl-confirm-bar")
        in_bar = await bar.inner_text() if await bar.count() else "(no confirmation bar)"
        assert "lowercase" in in_dialog and not in_editor and "not found" in in_bar, \
            f"The namespace dialog says {in_dialog!r}, the editor {in_editor!r}; the delete bar {in_bar!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a public recompile leaves the custom types' compile error alone")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector("#dsdlCustomHeader + .dsdl-compile-error", timeout=WAIT_MS)
        server.compile_answer = ({"ok": True}, 200)
        async with page.expect_response("**/api/dsdl/compile"):
            await page.locator("#dsdlRecompileBtn").click()
        await page.wait_for_timeout(300)
        left = await page.locator("#dsdlCustomHeader + .dsdl-compile-error").count()
        assert left == 1, "A public recompile wiped the custom types' compile error, though they did not compile"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor asks before unsaved changes are dropped")
async def _(page):
    server = _DsdlServer()
    asked, answer = [], {"accept": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if answer["accept"] else dialog.dismiss())

    async def source():
        return await page.locator("#dsdlEditorSource").input_value() if await page.locator("#dsdlEditorSource").count() else None

    page.on("dialog", on_dialog)
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorClose").click()  # nothing written: closed without a word
        assert not asked and await source() is None, f"An empty editor: asked {asked}, left {await source()!r}"
        draft = "uint8 lots_of_work\n@sealed\n"
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSource").fill(draft)
        await page.locator("#dsdlEditorClose").click()  # declined: the draft stays
        after_close = await source()
        await dsdl_new_type(page)  # a new type over the draft: declined too
        after_new = await source()
        answer["accept"] = True
        await page.locator("#dsdlEditorClose").click()
        assert len(asked) == 3 and after_close == after_new == draft and await source() is None, \
            f"Asked {len(asked)} times; the draft after a declined close {after_close!r}, " \
            f"after a declined new type {after_new!r}; once agreed, the editor holds {await source()!r}"
    finally:
        page.remove_listener("dialog", on_dialog)
        await dsdl_close(page, server)


# The "Active on bus" chips and toggle not cut off by their section.
BUS_SHOWN = """() => { const section = document.getElementById('dsdlBusActivity').getBoundingClientRect();
    const inside = (el) => el && el.getBoundingClientRect().bottom <= section.bottom + 0.5;
    const toggle = document.querySelector('#dsdlBusActivity .dsdl-bus-toggle');
    return {chips: [...document.querySelectorAll('#dsdlBusActivity .dsdl-bus-chip')].filter(inside).length,
            toggle: toggle?.textContent.trim() ?? null, toggleShown: inside(toggle)}; }"""


@test("DSDL: a type busy on the bus shows a few of its publishers, and how many more")
async def _(page):
    server = _DsdlServer()
    nodes = {"node_count": 12, "nodes": {str(nid): _graph_node(nid, f"org.example.dev{nid}", [7509], [], uid_byte=nid)
                                         for nid in range(10, 22)}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 7509, publisher_node_id: 10, message_type: 'Heartbeat_1_0', rate: 1,
                    subject_rate: 12, payload_bytes: 7, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        await page.wait_for_selector("#dsdlBusActivity .dsdl-bus-toggle", state="attached", timeout=WAIT_MS)
        folded = await page.evaluate(BUS_SHOWN)
        assert folded["toggleShown"] and folded["toggle"] == f"+{12 - folded['chips']} more", \
            f"12 publishers, folded: {folded}"
        await page.locator("#dsdlBusActivity .dsdl-bus-toggle").click()
        await page.wait_for_timeout(100)
        unfolded = await page.evaluate(BUS_SHOWN)
        assert unfolded == {"chips": 12, "toggle": "show less", "toggleShown": True}, f"12 publishers, unfolded: {unfolded}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the type tree works by keyboard, one Tab stop and the arrows")
async def _(page):
    server = _DsdlServer()
    focused = "[document.activeElement.dataset.type ?? document.activeElement.dataset.ns ?? document.activeElement.id," \
              " document.activeElement.getAttribute('aria-expanded')]"
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlRecompileBtn").focus()
        await page.keyboard.press("Tab")
        into = await page.evaluate(focused)
        for key in ("ArrowRight", "ArrowDown", "ArrowRight", "ArrowDown", "Enter"):
            await page.keyboard.press(key)
        await page.wait_for_timeout(500)
        shown = (await page.locator("#dsdlDetail").inner_text()).split("\n")[0]
        on_type = await page.evaluate(focused)
        await page.keyboard.press("ArrowLeft")  # to its namespace
        await page.keyboard.press("ArrowLeft")  # which closes
        closed = await page.evaluate(focused)
        await page.keyboard.press("Tab")  # out of the tree in one step
        out = await page.evaluate("document.activeElement.closest('#dsdlTree') === null")
        assert (into, on_type, shown, closed) == (["uavcan", "false"], ["uavcan.node.Heartbeat.1.0", None],
                                                  "uavcan.node.Heartbeat", ["uavcan.node", "false"]) and out, \
            f"Tab into the tree: {into}; → ↓ → ↓ Enter: {on_type}, showing {shown!r}; ← ←: {closed}; " \
            f"one Tab leaves the tree: {out}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a custom namespace's buttons and the 'Depends on' links work by keyboard")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Pair.1.0"] = _dsdl_type("myapp.Pair.1.0", source="custom", compiled=False,
                                                text="uavcan.si.unit.temperature.Scalar.1.0 a\n@sealed\n")
    focused = "document.activeElement.dataset.ns ?? document.activeElement.getAttribute('aria-label')"
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomAddNs").focus()  # the Custom header's last button
        await page.keyboard.press("Tab")  # the custom tree's stop: its namespace
        stops = [await page.evaluate(focused)]
        for _ in range(2):
            await page.keyboard.press("Tab")  # on through the namespace's own buttons
            stops.append(await page.evaluate(focused))
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        editor = await page.locator("#dsdlEditorName").count()
        assert stops == ["myapp", "Add sub-namespace", "Add type"] and editor, \
            f"Tab, Tab, Tab from the Custom header: {stops}; Enter opened an editor: {bool(editor)}"
        await page.locator("#dsdlEditorClose").click()
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Pair.1.0"]').click()
        await page.wait_for_selector(".dsdl-dep-chip", timeout=WAIT_MS)
        await page.locator(".dsdl-dep-chip").focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        shown = (await page.locator("#dsdlDetail").inner_text()).split("\n")[0]
        assert shown == "uavcan.si.unit.temperature.Scalar", f"Enter on the dependency shows {shown!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: screen readers hear the editor's fields and the results, and the bus dots hold still on request")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    nodes = {"node_count": 1, "nodes": {"10": _graph_node(10, "org.example.dev", [7509], [], uid_byte=1)}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 7509, publisher_node_id: 10, message_type: 'Heartbeat_1_0', rate: 1,
                    subject_rate: 1, payload_bytes: 7, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await dsdl_new_type(page)
        fields = await page.evaluate("""['dsdlEditorNs', 'dsdlEditorName', 'dsdlEditorVer', 'dsdlEditorPort', 'dsdlEditorSource']
            .map((id) => [...document.getElementById(id).labels].map((label) => label.textContent.trim()).join('') || null)""")
        status = await page.locator("#dsdlEditorStatus").get_attribute("role")
        await page.locator("#dsdlCustomAddNs").click()
        cancel = await page.locator("#dsdlNsCancel").get_attribute("aria-label")
        await page.locator("#dsdlNsCancel").click()
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-compile-error", timeout=WAIT_MS)
        compile_error = await page.locator(".dsdl-compile-error").get_attribute("role")
        tip = await page.locator(".dsdl-info-tip").get_attribute("aria-label") or ""
        await page.emulate_media(reduced_motion="reduce")
        pulse = await page.evaluate("getComputedStyle(document.querySelector('.dsdl-type-row .dsdl-bus-dot'), '::after').animationName")
        await page.emulate_media(reduced_motion="no-preference")
        assert fields == ["Namespace", "Type name", "Version", "Port ID", "Source"] and cancel \
            and (status, compile_error) == ("status", "alert") and "compiled" in tip and pulse == "none", \
            f"Fields named {fields}; × named {cancel!r}; roles: status {status!r}, compile error {compile_error!r}; " \
            f"the ? says {tip!r}; with reduced motion the bus dot pulses: {pulse!r}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await dsdl_close(page, server)


def _layout(low, high, sealed=True, extent=None, union=False):
    return {"union": union, "sealed": sealed, "extent_bytes": high if extent is None else extent,
            "size_bytes": [low, high]}


# Each field card's title and, beside it, its size.
CARD_TITLES = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-card-label')]
    .filter((label) => label.querySelector('.dsdl-card-meta'))
    .map((label) => [label.firstChild.textContent.trim(), label.querySelector('.dsdl-card-meta').textContent])"""


@test("DSDL: a type's pane says whether it is a union, deprecated, and how many bytes it takes")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["layout"] = _layout(7, 7, sealed=False, extent=12)
    value = _dsdl_type("uavcan.register.Value.1.0", text="uavcan.primitive.Empty.1.0 empty\nuint8 natural8\n@sealed\n")
    value.update(deprecated=True, layout=_layout(1, 259, union=True))
    info = _dsdl_type("uavcan.node.GetInfo.1.0", port=430, text="@sealed\n---\nuint8[<=50] name\n@extent 448 * 8\n")
    info["layout"] = {"request": _layout(0, 0), "response": _layout(1, 51, sealed=False, extent=448)}
    server.types.update({value["full_name"]: value, info["full_name"]: info})
    await dsdl_open(page, server)
    try:
        seen = {}
        for name in ("uavcan.node.Heartbeat.1.0", "uavcan.register.Value.1.0", "uavcan.node.GetInfo.1.0"):
            await dsdl_show(page, name)
            badges = await page.locator(".dsdl-badge-row").inner_text()
            seen[name] = (await page.evaluate(CARD_TITLES), "Deprecated" in badges)
        assert seen == {
            "uavcan.node.Heartbeat.1.0": ([["Fields", "7 bytes · extent 12 bytes"]], False),
            "uavcan.register.Value.1.0": ([["One of", "1–259 bytes · sealed"]], True),
            "uavcan.node.GetInfo.1.0": ([["Request", "0 bytes · sealed"],
                                         ["Response", "1–51 bytes · extent 448 bytes"]], False),
        }, f"Card titles and sizes, and a Deprecated badge: {seen}"
    finally:
        await dsdl_close(page, server)


# Each field's and constant's name, and the comment shown under it: [text, full comment on hover].
FIELD_COMMENTS = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-fcol-name')].map((cell) => {
    const doc = cell.querySelector('.dsdl-fcol-doc');
    return [cell.firstChild.textContent.trim(), doc ? [doc.textContent, doc.title] : null]; })"""


@test("DSDL: a type's pane gives its description, and the comments on its fields and constants")
async def _(page):
    server = _DsdlServer()
    heartbeat = server.types["uavcan.node.Heartbeat.1.0"]
    heartbeat["doc"] = "Abstract node status information.\nAll nodes publish it.\n\nThe rest of the story."
    heartbeat["detail"] = {
        "fields": [{"type": "uint32", "name": "uptime", "doc": "[second]\nSeconds since the node started."},
                   {"type": "uint8", "name": "vendor_specific_status_code", "doc": ""}],
        "constants": [{"type": "uint16", "name": "MAX_PUBLICATION_PERIOD", "value": "1", "doc": "[second]"}],
    }
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        summary = await page.locator(".dsdl-doc-summary").all_inner_texts()
        comments = await page.evaluate(FIELD_COMMENTS)
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # no comments at all
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        bare = (await page.locator(".dsdl-doc-summary").count(), await page.evaluate(FIELD_COMMENTS))
        assert summary == ["Abstract node status information. All nodes publish it."] and comments == [
            ["uptime", ["[second]", "[second]\nSeconds since the node started."]],
            ["vendor_specific_status_code", None],
            ["MAX_PUBLICATION_PERIOD", ["[second]", "[second]"]],
        ] and bare == (0, [["value", None]]), \
            f"Description {summary}; comments {comments}; a type without any: {bare}"
    finally:
        await dsdl_close(page, server)


# What the pane says of a fault, and the source lines it marks.
PROBLEM_SHOWN = """() => [document.querySelector('#dsdlDetail .dsdl-problem')?.textContent.trim() ?? null,
    [...document.querySelectorAll('#dsdlDetail .dsdl-src-problem')].map((line) => line.textContent)]"""


@test("DSDL: a custom type that does not compile says why, with its line marked in the source")
async def _(page):
    server = _DsdlServer()
    typo = _dsdl_type("myapp.Typo.1.0", source="custom", compiled=False, text="uint8 a\nuint9x b\n@sealed\n")
    typo["problem"] = {"message": "Syntax error", "line": 2}
    unsealed = _dsdl_type("myapp.Unsealed.1.0", source="custom", compiled=False, text="uint8 a\n")
    unsealed["problem"] = {"message": "Either `@sealed` or `@extent ...` are required.", "line": None}
    server.types.update({t["full_name"]: t for t in (typo, unsealed)})
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        shown = {}
        for name in ("Typo", "Unsealed", "Reading"):
            await page.locator(f'.dsdl-type-row[data-type="myapp.{name}.1.0"]').click()
            await page.wait_for_selector(f'.dsdl-doc-title:has-text("{name}")', timeout=WAIT_MS)
            shown[name] = await page.evaluate(PROBLEM_SHOWN)
        assert shown == {
            "Typo": ["Does not compile — line 2: Syntax error", ["2uint9x b"]],
            "Unsealed": ["Does not compile: Either `@sealed` or `@extent ...` are required.", []],
            "Reading": [None, []],
        }, f"What each pane says, and the source lines it marks: {shown}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a first type is written from a 'New type' button, its namespace typed in")
async def _(page):
    server = _DsdlServer()
    del server.types["myapp.Reading.1.0"]  # no custom types, no namespace yet
    server.custom_namespaces.clear()
    await dsdl_open(page, server)
    try:
        new_type = page.get_by_role("button", name="New type")
        assert await new_type.count() == 1, "No 'New type' button where the custom types go"
        await new_type.click()
        await page.locator("#dsdlEditorNs").fill("myapp")
        await page.locator("#dsdlEditorName").fill("Reading")
        await page.locator("#dsdlEditorSource").fill("uint16 value\n@sealed\n")
        said = await dsdl_save(page)
        await page.wait_for_selector('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]',
                                     state="attached", timeout=WAIT_MS)
        assert said == "Saved" and "myapp.Reading.1.0" in server.types, f"Saving said {said!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: 'New version' opens a copy of a custom type at the next version not taken")
async def _(page):
    server = _DsdlServer()
    taken = _dsdl_type("myapp.Reading.1.1", source="custom", text="uint16 value\nuint8 more\n@sealed\n")
    server.types[taken["full_name"]] = taken
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # compiled
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        new_version = page.get_by_role("button", name="New version")
        assert await new_version.count() == 1, "A compiled custom type offers no 'New version'"
        await new_version.click()
        await page.wait_for_selector("#dsdlEditorName", timeout=WAIT_MS)
        copy = await page.evaluate("""['dsdlEditorNs', 'dsdlEditorName', 'dsdlEditorVer', 'dsdlEditorSource']
            .map((id) => document.getElementById(id).value)""")
        assert copy == ["myapp", "Reading", "1.2", "uint16 value\n@sealed\n"], f"The editor opens on {copy}"
        said = await dsdl_save(page)
        assert said == "Saved" and "myapp.Reading.1.2" in server.types, f"Saving the new version said {said!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: an empty custom namespace can be deleted, one with types cannot")
async def _(page):
    server = _DsdlServer()
    server.custom_namespaces.add("spare")  # no types in it
    await dsdl_open(page, server)
    try:
        offered = {ns: await page.locator(f'[data-delete-ns="{ns}"]').count() for ns in ("spare", "myapp")}
        assert offered == {"spare": 1, "myapp": 0}, f"A delete button for each namespace: {offered}"
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="spare"]').hover()
        await page.locator('[data-delete-ns="spare"]').click()
        await page.wait_for_selector('#dsdlCustomTree .dsdl-ns-row[data-ns="spare"]', state="detached", timeout=WAIT_MS)
        assert "spare" not in server.custom_namespaces, "The server still has it"
    finally:
        await dsdl_close(page, server)


@test("DSDL: types are listed by name, then by version")
async def _(page):
    server = _DsdlServer()
    # As the server sends them: by file name, port-IDs first.
    for name, port in (("Version.1.0", None), ("ExecuteCommand.1.10", 435), ("ExecuteCommand.1.9", 435),
                       ("Health.1.0", None)):
        added = _dsdl_type(f"uavcan.node.{name}", port=port)
        server.types[added["full_name"]] = added
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlTree .dsdl-ns-row[data-ns="uavcan"]').click()
        await page.locator('#dsdlTree .dsdl-ns-row[data-ns="uavcan.node"]').click()
        listed = await page.evaluate("""[...document.querySelectorAll(
            '.dsdl-ns-row[data-ns="uavcan.node"] + .dsdl-ns-children > .dsdl-type-row')].map((row) => row.dataset.type)""")
        assert [t.removeprefix("uavcan.node.") for t in listed] == [
            "ExecuteCommand.1.9", "ExecuteCommand.1.10", "Health.1.0", "Heartbeat.1.0", "Version.1.0"], f"Listed: {listed}"
    finally:
        await dsdl_close(page, server)


async def dsdl_search(page, term):
    """Searched, and what the count under the search box says."""
    await page.locator("#dsdlSearch").fill(term)
    await page.wait_for_timeout(400)  # the search's debounce
    count = page.locator("#dsdlSearchCount")
    return await count.inner_text() if await count.count() else None


@test("DSDL: the search sits above the trees and says how many types match")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        above = await page.evaluate("""document.getElementById('dsdlSearch').getBoundingClientRect().bottom
            <= document.querySelector('#dsdlTreePanel .dsdl-tree-scroll').getBoundingClientRect().top""")
        said = {term: await dsdl_search(page, term) for term in ("Scalar", "Heartbeat", "zzz", "")}
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').hover()
        await page.locator('[data-hide-ns="myapp"]').click()
        said["Reading, its namespace hidden"] = await dsdl_search(page, "Reading")
        assert above and said == {
            "Scalar": "2 types match", "Heartbeat": "1 type matches", "zzz": "No type matches", "": "",
            "Reading, its namespace hidden": "No type matches, 1 more in hidden namespaces",
        }, f"Search above the trees: {above}; the count says {said}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: your own types come first, above the public ones")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        first = await page.evaluate("""document.getElementById('dsdlCustomHeader').getBoundingClientRect().top
            < document.getElementById('dsdlPublicHeader').getBoundingClientRect().top""")
        assert first, "The custom types come after all the public ones"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the search finds a type by its compiled name, as messages and recordings give it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        found = {}
        for term in ("Heartbeat_1_0", "uavcan.node.Heartbeat_1_0"):
            await dsdl_search(page, term)
            found[term] = await page.evaluate(
                "[...document.querySelectorAll('#dsdlTreePanel .dsdl-type-row')].map((row) => row.dataset.type)")
        assert found == {term: ["uavcan.node.Heartbeat.1.0"] for term in found}, f"Found: {found}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the search finds a type by one of its constants")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["source_text"] = "uint16 MAX_PUBLICATION_PERIOD = 1\nuint32 uptime\n@sealed\n"
    await dsdl_open(page, server)
    try:
        count = await dsdl_search(page, "max_publication")
        found = await page.evaluate(
            "[...document.querySelectorAll('#dsdlTreePanel .dsdl-type-row')].map((row) => row.dataset.type)")
        assert found == ["uavcan.node.Heartbeat.1.0"], f"Found {found}; the count says {count!r}"
    finally:
        await dsdl_close(page, server)


# A theme colour, --muted for one, as the browser draws it.
TOKEN_COLOUR = """(name) => { const probe = document.createElement('span'); probe.style.color = `var(--${name})`;
    document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; }"""

# The computed `property` of the element `selector` finds.
COLOUR_OF = "([selector, property]) => getComputedStyle(document.querySelector(selector))[property]"


@test("DSDL: the tree keeps colour for the unusual: a service's tag, a type not compiled yet")
async def _(page):
    server = _DsdlServer()
    pending = _dsdl_type("myapp.Pending.1.0", source="custom", compiled=False)
    service = _dsdl_type("uavcan.node.GetInfo.1.0", port=430, text="@sealed\n---\nuint8 name\n@sealed\n")
    server.types.update({t["full_name"]: t for t in (pending, service)})
    await dsdl_open(page, server)
    try:
        tokens = {name: await page.evaluate(TOKEN_COLOUR, name) for name in ("muted", "accent", "warn")}
        seen = {
            "message tag": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.Heartbeat.1.0"] .dsdl-kind', "color"]),
            "its fill": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.Heartbeat.1.0"] .dsdl-kind', "backgroundColor"]),
            "service tag": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.GetInfo.1.0"] .dsdl-kind', "color"]),
            "not compiled": await page.evaluate(COLOUR_OF, ['[data-type="myapp.Pending.1.0"] .dsdl-type-name', "color"]),
            "its namespace": await page.evaluate(COLOUR_OF, ['.dsdl-ns-row[data-ns="myapp"] .dsdl-ns-label', "color"]),
        }
        assert seen == {"message tag": tokens["muted"], "its fill": "rgba(0, 0, 0, 0)", "service tag": tokens["accent"],
                        "not compiled": tokens["warn"], "its namespace": tokens["warn"]}, \
            f"Drawn {seen}; the theme's muted, accent and warn are {tokens}"
    finally:
        await dsdl_close(page, server)


# Each badge of the type shown, by its text: its colour.
BADGE_COLOURS = """() => Object.fromEntries([...document.querySelectorAll('#dsdlDetail .dsdl-badge-row .dsdl-badge')]
    .map((badge) => [badge.textContent.trim(), getComputedStyle(badge).color]))"""


@test("DSDL: a type's pane keeps colour for the unusual: not compiled, deprecated")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["detail"] = {
        "constants": [{"type": "uint16", "name": "MAX_PUBLICATION_PERIOD", "value": "1", "doc": ""}]}
    pending = _dsdl_type("myapp.Pending.1.0", source="custom", compiled=False)
    pending["deprecated"] = True
    server.types[pending["full_name"]] = pending
    await dsdl_open(page, server)
    try:
        tokens = {name: await page.evaluate(TOKEN_COLOUR, name) for name in ("muted", "text", "warn")}
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        heartbeat = await page.evaluate(BADGE_COLOURS)
        cells = [await page.evaluate(COLOUR_OF, [f"#dsdlDetail {cell}", "color"])
                 for cell in (".dsdl-fcol-type", ".dsdl-fcol-val")]
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        shown = {}
        for name in ("Reading", "Pending"):
            await page.locator(f'.dsdl-type-row[data-type="myapp.{name}.1.0"]').click()
            await page.wait_for_selector(f'.dsdl-doc-title:has-text("{name}")', timeout=WAIT_MS)
            shown[name] = await page.evaluate(BADGE_COLOURS)
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n---\nuint8 b\n@sealed\n")
        await page.wait_for_selector(".dsdl-preview-label-res", timeout=WAIT_MS)
        response = await page.evaluate(COLOUR_OF, [".dsdl-preview-label-res", "color"])
        plain, flagged = tokens["muted"], tokens["warn"]
        seen = {"Message": heartbeat["Message"], "Compiled": heartbeat["Compiled"], "Custom": shown["Reading"]["Custom"],
                "Not compiled": shown["Pending"]["Not compiled"], "Deprecated": shown["Pending"]["Deprecated"],
                "field type, constant value": cells, "preview's Response": response}
        assert seen == {"Message": plain, "Compiled": plain, "Custom": plain, "Not compiled": flagged,
                        "Deprecated": flagged, "field type, constant value": [tokens["text"]] * 2,
                        "preview's Response": plain}, f"Drawn {seen}; the theme's {tokens}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a type's buttons keep their height when the note beside them wraps")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # compiled: a note by its buttons
        await page.wait_for_selector("#dsdlNewVersionBtn", timeout=WAIT_MS)
        await dsdl_new_type(page)  # the editor beside it narrows the pane
        await page.wait_for_timeout(300)
        heights = await page.evaluate("""['dsdlEditorSave', 'dsdlNewVersionBtn', 'dsdlDeleteBtn']
            .map((id) => Math.round(document.getElementById(id).getBoundingClientRect().height))""")
        assert heights[0] == heights[1] == heights[2], f"Save, New version and Delete are {heights} tall"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor's preview shows its own source, not a closed editor's")
async def _(page):
    server = _DsdlServer()
    page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))  # closing a draft asks
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        # Typed, closed, and another editor opened, within the preview's 250 ms.
        await page.evaluate("""() => {
            const source = document.getElementById('dsdlEditorSource');
            source.value = 'uint8 stale_field\\n@sealed\\n';
            source.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('dsdlEditorClose').click();
            document.querySelector('[data-add-type="myapp"]').click();
        }""")
        await page.wait_for_selector("#dsdlEditorSource", timeout=WAIT_MS)
        await page.wait_for_timeout(500)
        preview = await page.locator("#dsdlPreviewContent").inner_text()
        assert "stale_field" not in preview, f"A new, empty editor's preview shows {preview!r}"
    finally:
        await dsdl_close(page, server)


# Inline styling in the DSDL tab and on the page's body: any property but a
# CSS variable, and any value in px.
INLINE_STYLING = """() => [...document.querySelectorAll('#dsdlContainer [style]'), document.body]
    .flatMap((el) => [...el.style].filter((prop) => !prop.startsWith('--') || el.style.getPropertyValue(prop).includes('px'))
    .map((prop) => `${el.id || el.tagName.toLowerCase()} ${prop}: ${el.style.getPropertyValue(prop)}`))"""


async def drag_by(page, selector, dx=0, dy=0):
    """Pressed on `selector` and moved by dx, dy; the caller lets go."""
    box = await page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + dx, y + dy, steps=4)


@test("DSDL: the tab is styled by its CSS, in rem, its splits still dragged and its rows indented as before")
async def _(page):
    server = _DsdlServer()
    width = "(id) => Math.round(document.getElementById(id).getBoundingClientRect().width)"
    height = "(id) => Math.round(document.getElementById(id).getBoundingClientRect().height)"
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.si.unit.temperature.Scalar.1.0")
        # Each depth a step of 1.125rem: a namespace's row at 0.5rem past it, a type's at 1rem.
        indents = await page.evaluate("""[
            getComputedStyle(document.querySelector('.dsdl-ns-row[data-ns="uavcan.si.unit.temperature"]')).paddingLeft,
            getComputedStyle(document.querySelector('.dsdl-type-row[data-type="uavcan.si.unit.temperature.Scalar.1.0"]')).paddingLeft]""")
        await dsdl_new_type(page)
        # Each split dragged, and what it moved measured before the next.
        moved, while_dragging = [], None
        for handle, measure, part, dx, dy in (("#dsdlEditorHandle", width, "dsdlEditorPanel", 60, 0),
                                              ("#dsdlPreviewHandle", height, "dsdlEditorSource", 0, 40),
                                              ("#dsdlSplitHandle", width, "dsdlTreePanel", 50, 0)):
            before = await page.evaluate(measure, part)
            await drag_by(page, handle, dx, dy)
            if handle == "#dsdlSplitHandle":
                while_dragging = await page.evaluate(INLINE_STYLING)
            await page.mouse.up()
            moved.append(await page.evaluate(measure, part) - before)
        styled = await page.evaluate(INLINE_STYLING)
        # As before: a split follows the pointer, so these are not quite 60 and 40.
        assert indents == ["62px", "88px"] and all(abs(m - e) <= 2 for m, e in zip(moved, (62, 46, 50))), \
            f"Indents {indents} (62px, 88px before); dragged by 60, 40 and 50 px, the editor, source and tree " \
            f"moved {moved} (62, 46 and 50 before)"
        assert not styled and not while_dragging, f"Styled inline: {styled}; while a split is dragged: {while_dragging}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the tree keeps its width across reloads, saved in rem, a px one from earlier versions too")
async def _(page):
    server = _DsdlServer()
    width = "() => Math.round(document.getElementById('dsdlTreePanel').getBoundingClientRect().width)"

    async def reopen(dsdl_state=None):
        await page.evaluate(f"""() => {{ switchView('nodes'); state.dashboardConnected = false; _writeSettingsNow();
            {f"localStorage.setItem('cynitor.dsdl.state', {json.dumps(json.dumps(dsdl_state))});" if dsdl_state else ""} }}""")
        await page.reload(wait_until="load")
        await page.evaluate("state.dashboardConnected = true; switchView('dsdl')")
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=5000)

    await page.route("**/api/dsdl/**", server.handle)
    try:
        await reopen({"treeWidth": 402})  # as an earlier version saved it, in px
        legacy = await page.evaluate(width)
        await drag_by(page, "#dsdlSplitHandle", dx=-40)
        await page.mouse.up()
        saved = await page.evaluate("JSON.parse(localStorage.getItem('cynitor.dsdl.state'))")
        await reopen()
        kept = await page.evaluate(width)
        assert (legacy, round(saved.get("treeWidthRem") or 0, 3), kept) == (402, 22.625, 362), \
            f"A px width saved earlier gives {legacy} px; dragged to 362 px it is saved as {saved}, and reopens at {kept} px"
    finally:
        await dsdl_close(page, server)


@test("DSDL: an editor open on a custom type locks itself once the type is compiled")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    usable = """() => [...document.getElementById('dsdlEditorPanel').querySelectorAll('input, textarea, button')]
        .filter(control => !control.disabled).map(control => control.id)"""
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlEditBtn").click()
        await page.wait_for_selector("#dsdlEditorSource", timeout=WAIT_MS)
        before = await page.evaluate(usable)
        # Compiled from this tab: the tree it reloads has the type compiled.
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector("#dsdlEditorPanel .dsdl-editor-locked-banner", timeout=WAIT_MS)
        banner = await page.locator(".dsdl-editor-locked-banner").inner_text()
        after = await page.evaluate(usable)
        assert (before, after) == (["dsdlEditorSave", "dsdlEditorClose", "dsdlEditorPort", "dsdlEditorSource"],
                                   ["dsdlEditorClose"]) and "new version" in banner, \
            f"Usable while editing: {before}; once compiled: {after}, and the editor says {banner!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: 'New version' from a locked editor opens a draft that can be edited")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlEditBtn").click()
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-editor-locked-banner", timeout=WAIT_MS)
        # What the lock's banner advises.
        await page.locator("#dsdlNewVersionBtn").click()
        await page.wait_for_function("document.getElementById('dsdlEditorVer')?.value === '1.1'", timeout=WAIT_MS)
        try:
            await page.locator("#dsdlEditorSource").click(timeout=WAIT_MS)
            await page.keyboard.type("uint8 extra\n")
            typed = await page.locator("#dsdlEditorSource").input_value()
        except Exception as e:
            typed = f"not clicked into: {str(e).splitlines()[0]}"
        save = await page.evaluate("getComputedStyle(document.getElementById('dsdlEditorSave')).opacity")
        assert "uint8 extra" in typed and save == "1", \
            f"The new version's source reads {typed!r}; its Save is drawn at opacity {save}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: after a compile that fails, the tree shows at once what did compile")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    server.types["other.Broken.1.0"] = _dsdl_type("other.Broken.1.0", source="custom", compiled=False,
                                                  text="uint8 x\n")
    server.custom_namespaces.add("other")
    server.compile_answer = ({"error": "other.Broken.1.0: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        # The compile compiles myapp, and fails in other. The status the tab
        # polls stays as it was, so no poll reloads the tree in its stead.
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        compiled = '#dsdlCustomTree .dsdl-type-row.dsdl-type-compiled[data-type="myapp.Reading.1.0"]'
        try:
            await page.wait_for_selector(compiled, timeout=WAIT_MS)
            shown = True
        except Exception:
            shown = False
        error = await page.locator("#dsdlCustomHeader ~ .dsdl-compile-error").inner_text()
        assert shown and "other.Broken.1.0" in error, \
            f"myapp.Reading.1.0 shown compiled: {shown}; the error under the header: {error!r}"
    finally:
        await dsdl_close(page, server)


# How many lines an element's text takes, and its words drawn across two.
SPLIT_WORDS = r"""(el) => {
    const text = el.firstChild, range = document.createRange(), split = [];
    range.selectNodeContents(text);
    const lines = new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
    for (const word of text.data.matchAll(/\S+/g)) {
        range.setStart(text, word.index);
        range.setEnd(text, word.index + word[0].length);
        if (range.getClientRects().length > 1) split.push(word[0]);
    }
    return { lines, split };
}"""


@test("DSDL: a compile error wraps between words, never inside one")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required. "
                                       "The smallest valid extent for this type (i.e., its max bit length) is 8 bits "
                                       "(1 bytes). If you are not sure what this means, add the following line near "
                                       "the end of this definition: `@extent 64 * 8`"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        error = page.locator("#dsdlCustomHeader ~ .dsdl-compile-error")
        await error.wait_for(timeout=WAIT_MS)
        drawn = await error.evaluate(SPLIT_WORDS)
        assert drawn["lines"] > 1 and not drawn["split"], \
            f"Over {drawn['lines']} lines, these words are cut in two: {drawn['split']}"
    finally:
        await dsdl_close(page, server)


# ── Record tab ──
#
# /api/recordings* and /api/rawlogs are answered by _RecordServer from
# recordings shaped as the backend sends them; starting, stopping, renaming
# and deleting change them as the backend would. Each test opens the tab on a
# freshly loaded page.

def _recording(rid, name, live=False, start=None, **fields):
    start = start or time.time() - 3600 + rid
    return {"id": rid, "name": name, "start_unix": start, "end_unix": None if live else start + 60,
            "filter": {}, "notes": None, "created_at": start, "max_length_seconds": 3600.0,
            "max_events": 100000, "stop_on_limit": True, "auto_stopped": False, "event_count": 120,
            "events_source": "dedicated", "subjects": [100], "duration_seconds": 60.0, **fields}


class _RecordServer:
    """The backend's /api/recordings* and /api/rawlogs routes, over a few recordings."""

    def __init__(self, recordings=()):
        self.recordings = {r["id"]: r for r in recordings}
        self.requests = []          # (method, path, JSON body)
        self.gates = {}             # (method, path) -> an asyncio.Event its answer waits for
        self.failures = {}          # (method, path) -> (status, error) it answers instead
        self.rawlogs = {"active": None, "logs": []}  # GET /api/rawlogs

    def _answer(self, method, path, body):
        if (method, path) in self.failures:
            status, error = self.failures[(method, path)]
            return {"error": error}, status
        recs = self.recordings
        if path == "/api/rawlogs":
            return self.rawlogs, 200
        if path == "/api/recordings/buffer":
            return {"buffer": {"retention_seconds": 86400, "max_events": 5000000, "event_count": 0,
                               "oldest_event_unix": None, "newest_event_unix": None, "db_size_bytes": 4096}}, 200
        if path == "/api/recordings":
            if method == "GET":
                return {"recordings": sorted(recs.values(), key=lambda r: -r["start_unix"])}, 200
            rid = max(recs, default=0) + 1
            recs[rid] = _recording(rid, body["name"], live=True, start=time.time(), event_count=0,
                                   filter=body.get("filter") or {}, max_length_seconds=body.get("max_length_seconds"),
                                   max_events=body.get("max_events"), stop_on_limit=bool(body.get("stop_on_limit")))
            return {"recording": recs[rid]}, 201
        if path == "/api/recordings/quick":  # the last seconds of the history, saved: 42 events in them
            rid, now = max(recs, default=0) + 1, time.time()
            recs[rid] = _recording(rid, body["name"], start=now - body["last_seconds"], end_unix=now, event_count=42,
                                   filter=body.get("filter") or {}, max_length_seconds=None, max_events=None,
                                   stop_on_limit=False)
            return {"recording": recs[rid]}, 201
        match = re.fullmatch(r"/api/recordings/(\d+)(/stop|/export)?", path)
        rec = recs.get(int(match[1])) if match else None
        if rec is None:
            return {"error": "Recording not found"}, 404
        if match[2] == "/stop":
            rec["end_unix"] = time.time()
        elif method == "PATCH":  # absent or null fields keep their value, as on the backend
            rec.update({key: value for key, value in body.items() if value is not None})
        elif method == "DELETE":
            del recs[rec["id"]]
            return {"deleted": True, "purged": False}, 200
        return {"recording": rec}, 200

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        if (request.method, path) in self.gates:
            await self.gates[(request.method, path)].wait()
        answer, status = self._answer(request.method, path, body)
        cors = {"Access-Control-Allow-Origin": "*"}
        if path.endswith("/export") and status == 200:
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", answer["recording"]["name"])
            await route.fulfill(body="recording_id,timestamp_unix\n", status=200, headers={
                **cors, "Content-Type": "text/csv", "Content-Disposition": f'attachment; filename="{name}.csv"'})
        else:
            await route.fulfill(json=answer, status=status, headers=cors)

    def count(self, method, path):
        return sum(1 for m, p, _ in self.requests if (m, p) == (method, path))


async def record_open(page, server, setup=None, can=True):
    """The Record tab on a freshly loaded page, served by `server`, with CAN
    connected or not. `setup` runs in the page before the tab opens."""
    await page.evaluate("() => { state.dashboardConnected = false; _writeSettingsNow(); }")
    await page.reload(wait_until="load")
    await page.route("**/api/recordings**", server.handle)
    await page.route("**/api/rawlogs**", server.handle)
    await page.evaluate(f"state.dashboardConnected = true; state.canConnected = {json.dumps(can)};")
    if setup:
        await page.evaluate(setup)
    await page.evaluate("switchView('record')")
    await page.wait_for_selector("#recordList .record-card, #recordList .record-empty", timeout=5000)


async def record_close(page, server):
    if not page.url.startswith(BASE_URL):  # a download that went wrong took the page away
        await page.goto(BASE_URL, wait_until="load")
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false; state.canConnected = false;
        state.latestNodesPayload = null; state.latestBySubject.clear(); state.latestByNode.clear(); }""")
    await page.unroute("**/api/recordings**", server.handle)
    await page.unroute("**/api/rawlogs**", server.handle)


@test("Record: an export downloads the file, and one that fails keeps the dashboard and says why")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        csv = page.locator('.record-card [data-action="export-csv"]')
        async with page.expect_download() as download:
            await csv.click()
        assert (await download.value).suggested_filename == "boot_sequence.csv"
        del server.recordings[1]  # deleted from another dashboard since the list was drawn
        await csv.click()
        await page.wait_for_timeout(1000)
        assert page.url.startswith(BASE_URL), f"The export replaced the dashboard with {page.url}"
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert any("Recording not found" in t for t in toasts), f"Toasts shown: {toasts}"
    finally:
        await record_close(page, server)


# Errors thrown, or promises rejected and not handled, from here on.
COLLECT_ERRORS = """() => { window.e2eErrors = [];
    window.addEventListener('error', (e) => e2eErrors.push(String(e.message)));
    window.addEventListener('unhandledrejection', (e) => e2eErrors.push(String(e.reason))); }"""


@test("Record: the tab opens without errors")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server, setup=COLLECT_ERRORS)
    try:
        await page.wait_for_timeout(500)
        errors = await page.evaluate("e2eErrors")
        assert not errors, f"Opening the tab threw: {errors}"
    finally:
        await record_close(page, server)


@test("Record: Start makes one recording however fast it is clicked")
async def _(page):
    server = _RecordServer()
    server.gates[("POST", "/api/recordings")] = gate = asyncio.Event()
    await record_open(page, server)
    try:
        await page.locator("#recName").fill("bench run")
        await page.locator("#recStart").dblclick()
        await page.wait_for_timeout(300)
        gate.set()
        await page.wait_for_selector(".record-card.live", timeout=WAIT_MS)
        await page.wait_for_timeout(300)
        posts = server.count("POST", "/api/recordings")
        assert posts == 1, f"A double-click on Start sent {posts} requests: {len(server.recordings)} recordings"
    finally:
        await record_close(page, server)


@test("Record: Start is off, and says why, until CAN is connected")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, can=False)
    try:
        start = page.locator("#recStart")
        why = await page.locator("#recStartWhy").all_inner_texts()
        assert await start.is_disabled() and why and "CAN" in why[0], \
            f"With CAN not connected, Start is disabled: {await start.is_disabled()}, and the tab says {why}"
        await page.evaluate("state.canConnected = true; updateSemaphores();")
        assert await start.is_enabled(), "Start stays disabled once CAN is connected"
        assert not await page.locator("#recStartWhy").inner_text(), "The reason stays once CAN is connected"
    finally:
        await record_close(page, server)


@test("Record: Edit limits shows a recording's own limits, and sends only what is changed")
async def _(page):
    server = _RecordServer([
        _recording(1, "no limits", live=True, max_length_seconds=None, max_events=None, stop_on_limit=False),
        _recording(2, "ten minutes", live=True, max_length_seconds=600.0)])
    await record_open(page, server)
    limits = ("max_length_seconds", "max_events", "stop_on_limit")
    try:
        for rid in (1, 2):  # opened and applied as it is, nothing changes
            before = {key: server.recordings[rid][key] for key in limits}
            await page.locator(f'.record-card[data-id="{rid}"] [data-action="edit-limits"]').click()
            shown = await page.evaluate("[el('recEditLength'), el('recEditMaxEvents')].map(s => s.selectedOptions[0].text)")
            await page.locator("#recEditApply").click()
            await page.wait_for_timeout(500)
            after = {key: server.recordings[rid][key] for key in limits}
            assert after == before, f"The dialog showed {shown} for {before}; Apply made it {after}"
        await page.locator('.record-card[data-id="1"] [data-action="edit-limits"]').click()
        await page.locator("#recEditMaxEvents").select_option("10000")
        await page.locator("#recEditApply").click()
        await page.wait_for_timeout(500)
        patches = [body for method, _, body in server.requests if method == "PATCH"]
        assert patches == [{"max_events": 10000}], f"Requests sent: {patches}"
    finally:
        await record_close(page, server)


@test("Record: a recording's times count whole seconds, never 60 of them")
async def _(page):
    now = time.time()
    server = _RecordServer([
        _recording(1, "two minutes less a bit", start=now - 7200, end_unix=now - 7200 + 119.6),
        _recording(2, "an hour less a bit", start=now - 18000, end_unix=now - 18000 + 3599.7)])
    await record_open(page, server)
    try:
        times = await page.locator(".record-card .rec-bar-right").all_inner_texts()
        assert any(t.startswith("1m 59s") for t in times) and any(t.startswith("59m 59s") for t in times) \
            and not any("60s" in t or "60m" in t for t in times), f"Times shown: {times}"
    finally:
        await record_close(page, server)


# Every toast shown from here on, in window.e2eToasts.
COLLECT_TOASTS = """() => { window.e2eToasts = [];
    new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach((n) => e2eToasts.push(n.textContent))))
        .observe(el('toastContainer'), {childList: true}); }"""


@test("Record: a list that cannot be loaded says so once, in place, and keeps what it showed")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server, setup=COLLECT_TOASTS)
    try:
        server.failures[("GET", "/api/recordings")] = (500, "database is locked")
        for _ in range(3):  # three polls
            await page.evaluate("fetchRecordings()")
            await page.wait_for_timeout(200)
        toasts = await page.evaluate("e2eToasts")
        note = await page.locator("#recordList .record-list-error").all_inner_texts()
        assert not toasts and len(note) == 1 and "database is locked" in note[0], \
            f"After three failed loads, toasts: {toasts}; a note in the list: {note}"
        assert await page.locator(".record-card").count() == 1, "The recordings shown before are gone"
        del server.failures[("GET", "/api/recordings")]
        await page.evaluate("fetchRecordings()")
        await page.wait_for_timeout(300)
        assert not await page.locator("#recordList .record-list-error").count(), "The note stays once the list loads"
    finally:
        await record_close(page, server)


@test("Record: opening the tab asks for each list once, and nothing while disconnected")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        await page.wait_for_timeout(500)
        asked = {path: server.count("GET", path) for path in ("/api/recordings", "/api/recordings/buffer", "/api/rawlogs")}
        assert set(asked.values()) == {1}, f"Requests on opening the tab: {asked}"
        await page.evaluate("state.dashboardConnected = false")
        before = len(server.requests)
        await page.wait_for_timeout(2500)  # the raw logs are polled every 2 s
        assert not server.requests[before:], f"Requests with the backend disconnected: {server.requests[before:]}"
    finally:
        await record_close(page, server)


@test("Record: Play is off, saying why, for a recording with nothing to replay")
async def _(page):
    server = _RecordServer([
        _recording(1, "old bookmark", events_source="global"),
        _recording(2, "nothing came", event_count=0),
        _recording(3, "boot sequence")])
    await record_open(page, server, can=False)  # replay needs CAN disconnected
    try:
        plays = {}
        for rid in (1, 2, 3):
            play = page.locator(f'.record-card[data-id="{rid}"] [data-action="play"]')
            plays[rid] = (await play.is_disabled(), await play.get_attribute("title"))
        assert plays[1][0] and plays[2][0] and not plays[3][0], f"Play (disabled, title) by recording: {plays}"
    finally:
        await record_close(page, server)


# Node 10 publishes subject 100 at 10 Hz; nodes 10 and 11 send Heartbeat at
# 1 Hz each: 12 messages a second in all, for as long as window.e2eRateFeed runs.
RECORD_RATES = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [100, 7509], subscribers: [], servers: [], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [7509], subscribers: [], servers: [], clients: []}}};
    const ev = (subject_id, publisher_node_id, rate, subject_rate) => ({subject_id, publisher_node_id,
        message_type: 'Real64_1_0', rate, subject_rate, attributes: [], timestamp_unix: Date.now() / 1000});
    const feed = () => { cacheEvent(ev(100, 10, 10, 10)); cacheEvent(ev(7509, 10, 1, 2)); cacheEvent(ev(7509, 11, 1, 2)); };
    feed();
    window.e2eRateFeed = setInterval(feed, 500);
}"""


@test("Record: the size estimate is for what the selection would record, at the rate it is sent now")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)
    try:
        await page.evaluate("state.recordFilterDraft.subject_ids = [100]; _refreshSelection();")
        selected = await page.locator("#recDiskHint").inner_text()
        await page.evaluate("state.recordFilterDraft.subject_ids = []; _refreshSelection();")
        everything = await page.locator("#recDiskHint").inner_text()
        assert "~10 msg/s" in selected and "~12 msg/s" in everything, \
            f"With subject 100 selected: {selected!r}; with nothing selected (everything): {everything!r}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed); state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a subject several nodes publish lists them all, at its total rate, and lights up for each")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)  # node 11's Heartbeat comes last
    try:
        await page.wait_for_timeout(2300)  # a picker refresh
        heartbeat = await page.evaluate("_subjectsPicker.getRow('subject-7509').getData()")
        assert (heartbeat["owner"], heartbeat["rate"]) == ("2 nodes", 2), \
            f"Subject 7509, from nodes 10 and 11 at 1 Hz each, reads owner {heartbeat['owner']!r}, rate {heartbeat['rate']!r}"
        await page.locator("#recNodesPicker .tabulator-row", has_text="org.example.imu").click()
        lit = await page.evaluate("""[...document.querySelectorAll('#recSubjectsPicker .tabulator-row.rec-row-highlighted')]
            .map((row) => row.querySelector('.tabulator-cell').textContent)""")
        assert sorted(lit) == ["100", "7509"], f"Node 10 lights up subjects {lit}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed)")
        await record_close(page, server)


@test("Record: the pickers work by keyboard, their refreshes keeping the focus")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)
    try:
        await page.locator("#recSubjectsPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")  # the first row: subject 100
        await page.wait_for_timeout(2500)  # the pickers refresh every 2 s
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        added = await page.evaluate("state.recordFilterDraft.subject_ids")
        assert added == [100], f"Enter on subject 100's row, after a refresh, selected {added}"
        await page.locator("#recSelectionPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(300)
        left = await page.evaluate("state.recordFilterDraft.subject_ids")
        kept = await page.evaluate("el('recSelectionPicker').contains(document.activeElement)")
        assert left == [] and kept, f"Delete left the selection at {left}; the focus stayed in it: {kept}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed); state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a row keeps the keyboard focus when its table is drawn again")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup="() => { _addToSelection({kind: 'subject', id: 100}); }")
    try:
        await page.locator("#recSelectionPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(200)
        await page.evaluate("_selectionPicker.redraw()")  # as a resize does
        await page.wait_for_timeout(200)
        focused = await page.evaluate("document.activeElement.className")
        assert "tabulator-row" in focused, f"After a redraw the focus is on {focused!r}, not the row"
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(300)
        left = await page.evaluate("state.recordFilterDraft.subject_ids")
        assert left == [], f"Delete after the redraw left the selection at {left}"
    finally:
        await page.evaluate("state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: Edit limits takes the focus, keeps Tab inside, and gives the focus back")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True)])
    await record_open(page, server)
    try:
        await page.locator('.record-card[data-id="1"] [data-action="edit-limits"]').focus()
        await page.keyboard.press("Enter")
        await page.wait_for_selector("#recEditLimitsBackdrop:not(.hidden)")
        focused = await page.evaluate("document.activeElement.id || document.activeElement.textContent")
        assert focused == "recEditLength", f"Opened, the dialog leaves the focus on {focused!r}"
        await page.locator("#recEditApply").focus()
        await page.keyboard.press("Tab")
        wrapped = await page.evaluate("document.activeElement.id || document.activeElement.textContent")
        assert wrapped == "recEditLength", f"Tab from Apply goes to {wrapped!r}"
        await page.keyboard.press("Escape")
        back = await page.evaluate("document.activeElement.dataset.action || document.activeElement.tagName")
        assert back == "edit-limits", f"Closed, the dialog leaves the focus on {back!r}"
    finally:
        await record_close(page, server)


@test("Record: a recording's progress bars say what they measure, and how far it got")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])  # 60 s of a 1 h limit, 120 of 100,000 events
    await record_open(page, server)
    try:
        card = page.locator('.record-card[data-id="1"]')
        read = {}
        for name in ("Time", "Events"):
            bar = card.get_by_role("progressbar", name=name)
            read[name] = await bar.get_attribute("aria-valuetext") if await bar.count() == 1 else None
        assert read == {"Time": "1m 0s / 1h 0m", "Events": "120 / 100,000"}, f"Progress bars by name: {read}"
    finally:
        await record_close(page, server)


@test("Record: the recording dots hold still on request")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True)])
    await page.emulate_media(reduced_motion="reduce")
    try:
        await record_open(page, server)
        moving = await page.evaluate("""[getComputedStyle(document.querySelector('.record-card .record-dot.rec')).animationName,
            getComputedStyle(el('viewTabRecord'), '::after').animationName]""")
        assert moving == ["none", "none"], f"With reduced motion asked for, the card's and the tab's dots run {moving}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await record_close(page, server)


@test("Record: Start is at the top of the tab, above the recordings however many they are")
async def _(page):
    server = _RecordServer([_recording(rid, f"run {rid}") for rid in range(1, 21)])
    await record_open(page, server)
    try:
        start = await page.locator("#recStart").bounding_box()
        recordings = await page.locator("#recordList").bounding_box()
        assert start["y"] + start["height"] <= 800 and start["y"] < recordings["y"], \
            f"In a window 800 px high, Start recording is at {start['y']:.0f} px, the recordings at {recordings['y']:.0f} px"
        hint = await page.locator(".record-header").inner_text()
        assert "on the right" not in hint, f"The tab's hint reads {hint!r}"
    finally:
        await record_close(page, server)


@test("Record: a recording needs no name, taking its start time, and the next one starts with an empty name")
async def _(page):
    server = _RecordServer()
    await record_open(page, server)
    try:
        await page.locator("#recName").fill("")
        await page.locator("#recStart").click()
        await page.wait_for_selector(".record-card.live", timeout=WAIT_MS)
        await page.locator("#recName").fill("bench run")
        await page.locator("#recNotes").fill("PWM at 50 %")
        await page.locator("#recStart").click()
        await page.wait_for_timeout(800)
        names = [body["name"] for method, path, body in server.requests if (method, path) == ("POST", "/api/recordings")]
        assert len(names) == 2 and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", names[0]) and names[1] == "bench run", \
            f"Recordings started: {names}"
        left = [await page.locator(f).input_value() for f in ("#recName", "#recNotes")]
        assert left == ["", ""], f"After a start the name and notes read {left}"
    finally:
        await record_close(page, server)


@test("Record: Save the last minutes keeps what the bus sent before it was pressed")
async def _(page):
    server = _RecordServer()
    await record_open(page, server)
    try:
        assert await page.locator("#recSaveLast").count() == 1, "The record bar has no Save the last"
        await page.locator("#recSaveLastSeconds").select_option("300")
        await page.locator("#recSaveLast").click()
        await page.wait_for_selector(".record-card", timeout=WAIT_MS)
        saves = [body for method, path, body in server.requests if (method, path) == ("POST", "/api/recordings/quick")]
        assert len(saves) == 1 and saves[0]["last_seconds"] == 300, f"Saves asked for: {saves}"
        card = await page.locator(".record-card").first.inner_text()
        assert "5m 0s" in card and "42" in card, f"The saved recording's card reads {card!r}"
    finally:
        await record_close(page, server)


# Node 10 serves register access (384), node 11 calls it, and 384 is selected.
RECORD_SERVICE = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [], subscribers: [], servers: [384], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [], subscribers: [], servers: [], clients: [384]}}};
    state.recordFilterDraft.service_ids = [384];
}"""


@test("Record: a service says it records only the calls made from Cynitor")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_SERVICE)
    try:
        picked = await page.evaluate("_subjectsPicker.getRow('service-384').getCell('label').getElement().title")
        selected = await page.evaluate("_selectionPicker.getRow('service-384').getData().label")
        assert "from Cynitor" in picked and "from Cynitor" in selected, \
            f"Service 384 reads {picked!r} hovered in the picker, {selected!r} in the selection"
    finally:
        await page.evaluate("state.recordFilterDraft.service_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a recording's name is shown whole, its times under it")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence"),
                            _recording(2, "ESC thermal soak, node 12, 8 h at full PWM", auto_stopped=True)])
    await page.set_viewport_size({"width": 1440, "height": 900})  # the recordings take a narrow column
    try:
        await record_open(page, server)
        cut = await page.evaluate("""[...document.querySelectorAll('.record-card .record-name')]
            .filter((name) => name.scrollWidth > name.clientWidth + 1).map((name) => name.textContent)""")
        assert not cut, f"Names cut short: {cut}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: an auto-stopped recording says which limit stopped it, in amber only for the events cap")
async def _(page):
    now = time.time()
    server = _RecordServer([
        _recording(1, "capped", start=now - 9000, end_unix=now - 9000 + 600, auto_stopped=True,
                   max_events=1000, event_count=1000),
        _recording(2, "timed", start=now - 5000, end_unix=now - 5000 + 1802, auto_stopped=True,
                   max_length_seconds=1800.0, event_count=50)])
    await record_open(page, server)
    try:
        read = {}
        for rid in (1, 2):
            card = page.locator(f'.record-card[data-id="{rid}"]')
            badge = card.locator(".record-badge")
            read[rid] = (await badge.text_content(), "record-badge-warn" in await badge.get_attribute("class"),
                         "auto-stopped" in await card.get_attribute("class"))
        assert read == {1: ("stopped at 1,000 events", True, True), 2: ("stopped at 30m 0s", False, False)}, \
            f"(badge, amber badge, amber card) by recording: {read}"
    finally:
        await record_close(page, server)


@test("Record: a recording running while CAN is down says it waits for CAN")
async def _(page):
    server = _RecordServer([_recording(1, "overnight", live=True)])
    await record_open(page, server, can=False)
    try:
        card = page.locator('.record-card[data-id="1"]')
        badges = await card.locator(".record-badge").all_text_contents()
        dot = (await card.locator(".record-dot").get_attribute("class")).split()
        assert "waiting for CAN" in badges and "rec" not in dot, f"With CAN down: badges {badges}, dot {dot}"
        await page.evaluate("state.canConnected = true; renderRecordList();")
        badges = await card.locator(".record-badge").all_text_contents()
        dot = (await card.locator(".record-dot").get_attribute("class")).split()
        assert "waiting for CAN" not in badges and "rec" in dot, f"With CAN up: badges {badges}, dot {dot}"
    finally:
        await record_close(page, server)


@test("Record: the recording being replayed is marked on its card")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence"), _recording(2, "motor test")])
    await record_open(page, server, can=False)
    try:
        await page.evaluate("""() => { _applyReplayStatus({active: true, recording_id: 2, position_s: 0, duration_s: 60,
            speed: 1, paused: false, events_emitted: 0, total_events: 120, finished: false}); showReplayStrip(); }""")
        marked = await page.evaluate("[...document.querySelectorAll('.record-card.replaying')].map((c) => c.dataset.id)")
        badges = await page.locator('.record-card[data-id="2"] .record-badge').all_text_contents()
        assert marked == ["2"] and "replaying" in badges, f"Cards marked: {marked}; recording 2's badges: {badges}"
        await page.evaluate("hideReplayStrip()")
        assert not await page.locator(".record-card.replaying").count(), "A card stays marked after the replay"
    finally:
        await page.evaluate("hideReplayStrip()")
        await record_close(page, server)


@test("Record: the two pickers line up, however many lines their headings take")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})  # side by side, headings of different lengths
    try:
        await record_open(page, server)
        tops = await page.evaluate("['recSubjectsPicker', 'recNodesPicker'].map((id) => Math.round(el(id).getBoundingClientRect().top))")
        assert tops[0] == tops[1], f"The subjects and nodes pickers start at {tops}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: a card shows its main actions, the others in a menu that stays open as the card is drawn again")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        card = page.locator('.record-card[data-id="1"]')
        shown = [text.strip() for text in await card.locator("button:visible, summary:visible").all_inner_texts()]
        assert shown == ["▶ Play", "CSV", "JSONL", "⋯"], f"A stopped recording's card shows {shown}"
        await card.locator("summary").click()
        await page.evaluate("renderRecordList()")  # as every second while something records
        menu = [text.strip() for text in await card.locator(".record-card-menu button:visible").all_inner_texts()]
        assert menu == ["Record again", "Rename", "Delete"], f"Its menu, drawn again, holds {menu}"
        await card.locator('.record-card-menu [data-action="duplicate"]').click()
        await page.wait_for_timeout(500)
        assert server.count("POST", "/api/recordings") == 1, "Record again started no recording"
        assert not await page.locator(".record-card-menu:visible").count(), "The menu stays open after its action"
    finally:
        await record_close(page, server)


# A raw log is running, and one from yesterday is saved.
RAW_LOGS = {"active": {"name": "cynitor-20261008-101500.log", "frames": 1200, "bytes": 64000,
                       "started_unix": 1.79e9, "error": None},
            "logs": [{"name": "cynitor-20261008-101500.log", "bytes": 64000, "modified_unix": 1.79e9},
                     {"name": "cynitor-20261007-091500.log", "bytes": 265000, "modified_unix": 1.79e9}]}
RAW_LOG_SPEED = '#rawLogPanel select[data-rawlog="speed"]'


@test("Record: a raw log's playback speed sits with the saved logs, and is remembered")
async def _(page):
    server = _RecordServer()
    server.rawlogs = RAW_LOGS
    await record_open(page, server)
    try:
        await page.wait_for_selector(RAW_LOG_SPEED, timeout=WAIT_MS)
        assert not await page.locator('.rawlog-active select[data-rawlog="speed"]').count(), \
            "The playback speed sits beside the running log's Stop"
        await page.locator(RAW_LOG_SPEED).select_option("10")
        await record_close(page, server)
        await record_open(page, server)  # the page loaded again
        await page.wait_for_selector(RAW_LOG_SPEED, timeout=WAIT_MS)
        speed = await page.locator(RAW_LOG_SPEED).input_value()
        assert speed == "10", f"Loaded again, the playback speed reads {speed}"
    finally:
        await page.evaluate("state.rawLogPlaybackSpeed = 1; saveSettings();")
        await record_close(page, server)


@test("Record: the Record tab's dot shows while a raw log runs")
async def _(page):
    server = _RecordServer()
    server.rawlogs = RAW_LOGS
    await record_open(page, server)
    try:
        await page.wait_for_selector("#rawLogPanel .rawlog-active .record-dot", timeout=WAIT_MS)
        tab = await page.locator("#viewTabRecord").get_attribute("class")
        assert "recording-active" in tab, f"A raw log runs, and the Record tab reads {tab!r}"
    finally:
        await record_close(page, server)


# The Record tab's style rules that set a length in px other than a 1px line,
# or set anything !important; and what its elements are styled with inline,
# custom properties aside unless in px (as for the DSDL tab, INLINE_STYLING).
RECORD_CSS_FAULTS = r"""() => {
    // A style rule has cssRules too (nesting), and a shorthand using var() has
    // no longhand values: the rules' own text is what is read.
    const rules = [];
    const walk = (list) => { for (const rule of list) {
        if (rule instanceof CSSStyleRule) rules.push(rule);
        if (rule.cssRules) walk(rule.cssRules); } };
    for (const sheet of document.styleSheets) { try { walk(sheet.cssRules); } catch (e) { /* another origin */ } }
    const ruleFaults = rules.filter((rule) => /record|rec-|rawlog/.test(rule.selectorText)
            && /!important|(^|[^.\d])([2-9]|\d\d+)(\.\d+)?px/.test(rule.style.cssText))
        .map((rule) => `${rule.selectorText} { ${rule.style.cssText} }`);
    const inline = [...document.querySelectorAll('#recordContainer [style]')]
        .filter((el) => !el.closest('.tabulator'))  // Tabulator sizes its own parts
        .flatMap((el) => [...el.style]
            .filter((prop) => !prop.startsWith('--') || el.style.getPropertyValue(prop).includes('px'))
            .map((prop) => `${el.className} ${prop}: ${el.style.getPropertyValue(prop)}`));
    return [...ruleFaults, ...inline];
}"""


@test("Record: the tab is styled by its CSS, in rem, without !important")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True), _recording(2, "capped", auto_stopped=True,
                                                                               max_events=1000, event_count=1000)])
    await record_open(page, server)
    try:
        faults = await page.evaluate(RECORD_CSS_FAULTS)
        assert not faults, "Styled in px, !important or inline:\n" + "\n".join(faults)
        widths = await page.evaluate("""[...document.querySelectorAll('.record-card[data-id="2"] .rec-bar-fill')]
            .map((fill) => Math.round(fill.getBoundingClientRect().width / fill.parentElement.getBoundingClientRect().width * 100))""")
        assert widths[1] == 100, f"A recording at its events cap fills {widths[1]}% of its Events bar"
    finally:
        await record_close(page, server)


@test("Record: the history chip names the history, and is not drawn while it is unknown")
async def _(page):
    server = _RecordServer()
    server.failures[("GET", "/api/recordings/buffer")] = (500, "database is locked")
    await record_open(page, server)
    try:
        await page.wait_for_timeout(300)
        assert not await page.locator("#recBufferChip").is_visible(), "An empty chip is drawn in the header"
        del server.failures[("GET", "/api/recordings/buffer")]
        await page.evaluate("fetchRecordBuffer()")
        await page.wait_for_timeout(300)
        chip = await page.locator("#recBufferChip").inner_text()
        assert chip.startswith("History"), f"The chip reads {chip!r}"
    finally:
        await record_close(page, server)


RECORD_TYPES = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [100, 7509], subscribers: [], servers: [], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [7509], subscribers: [], servers: [], clients: []}}};
    const ev = (subject_id, publisher_node_id, message_type) => ({subject_id, publisher_node_id, message_type,
        rate: 100.5, subject_rate: 100.5, attributes: [], timestamp_unix: Date.now() / 1000});
    cacheEvent(ev(100, 10, 'Real64_1_0')); cacheEvent(ev(7509, 10, 'Heartbeat_1_0')); cacheEvent(ev(7509, 11, 'Heartbeat_1_0'));
}"""

# The headers and cells of a table whose text is cut short.
CUT_SHORT = """(id) => [...el(id).querySelectorAll('.tabulator-col-title, .tabulator-cell')]
    .filter((part) => part.scrollWidth > part.clientWidth).map((part) => part.textContent.trim())"""


async def set_log_panel(page, shown):
    """Opens or closes the log panel with its button; returns whether it was open."""
    was_open = not await page.evaluate("state.logPanelCollapsed")
    if was_open != shown:
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(300)  # the tables take their new widths
    return was_open


@test("Record: the pickers show types and node names whole, the columns of numbers as wide as theirs")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})  # two pickers side by side, beside the recordings
    log_was_open = None
    try:
        await record_open(page, server, setup=RECORD_TYPES)
        log_was_open = await set_log_panel(page, False)  # the room it takes is another test's
        cut = {picker: await page.evaluate(CUT_SHORT, picker) for picker in ("recSubjectsPicker", "recNodesPicker")}
        assert cut == {"recSubjectsPicker": [], "recNodesPicker": []}, f"Cut short: {cut}"
        hover = await page.evaluate("_subjectsPicker.getRow('subject-7509').getCell('label').getElement().title")
        assert hover == "Heartbeat_1_0", f"Heartbeat's type, hovered, reads {hover!r}"
    finally:
        if log_was_open is not None:
            await set_log_panel(page, log_was_open)
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: the tab takes one column when the log panel leaves too little room for two")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await page.set_viewport_size({"width": 1440, "height": 900})
    columns = "getComputedStyle(document.querySelector('.record-layout')).gridTemplateColumns.split(' ').length"
    log_was_open = None
    try:
        await record_open(page, server, setup=RECORD_TYPES)
        log_was_open = await set_log_panel(page, False)
        beside = await page.evaluate(columns)
        await set_log_panel(page, True)
        under = await page.evaluate(columns)
        cut = {picker: await page.evaluate(CUT_SHORT, picker) for picker in ("recSubjectsPicker", "recNodesPicker")}
        sideways = await page.evaluate("el('recordContainer').scrollWidth > el('recordContainer').clientWidth")
        assert (beside, under) == (2, 1) and cut == {"recSubjectsPicker": [], "recNodesPicker": []} and not sideways, \
            f"Columns with the log panel shut, then open: {beside}, {under}; then cut short: {cut}; scrolls sideways: {sideways}"
    finally:
        if log_was_open is not None:
            await set_log_panel(page, log_was_open)
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: a service's row in the picker is not cut short")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})
    try:
        await record_open(page, server, setup=RECORD_SERVICE)
        cut = await page.evaluate(CUT_SHORT, "recSubjectsPicker")
        assert not cut, f"Cut short: {cut}"
    finally:
        await page.evaluate("state.recordFilterDraft.service_ids = []; saveSettings();")
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: an empty picker says why, and what to do, in the tab's own text")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, can=False)
    try:
        said = """['recSubjectsPicker', 'recNodesPicker'].map((id) =>
            el(id).querySelector('.tabulator-placeholder').innerText.replace(/\\s+/g, ' ').trim())"""
        down = await page.evaluate(said)
        assert all("Connect a CAN interface" in words for words in down), f"With CAN down, the pickers say {down}"
        await page.evaluate("state.canConnected = true; _refreshPickerTables();")
        quiet = await page.evaluate(said)
        assert quiet[0].startswith("Waiting for traffic") and quiet[1].startswith("Waiting for nodes"), \
            f"With CAN up and nothing sent yet, they say {quiet}"
        drawn = await page.evaluate("""(() => { const style = getComputedStyle(
            el('recSelectionPicker').querySelector('.tabulator-placeholder-contents'));
            return [parseFloat(style.fontSize) / parseFloat(getComputedStyle(document.documentElement).fontSize), style.fontWeight]; })()""")
        assert drawn == [0.8125, "400"], f"An empty selection's words are {drawn[0]}rem, weight {drawn[1]}"
    finally:
        await record_close(page, server)


@test("Record: the search lists the recordings whose name, notes or selection hold what is typed, and the one being made")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence", notes="The IMU came up late"),
                            _recording(2, "motor test", filter={"node_ids": [12]}),
                            _recording(3, "field test", live=True)])
    await record_open(page, server)
    listed = "[...document.querySelectorAll('#recordList .record-name')].map((name) => name.textContent)"
    try:
        found = {}
        for typed in ("imu", "Node: 12", "nothing like it"):
            await page.locator("#recSearch").fill(typed, timeout=WAIT_MS)
            found[typed] = (await page.evaluate(listed), await page.locator("#recSearchCount").inner_text())
        assert found == {"imu": (["field test", "boot sequence"], "2 of 3"),
                         "Node: 12": (["field test", "motor test"], "2 of 3"),
                         "nothing like it": (["field test"], "1 of 3")}, f"Searched for, listed: {found}"
        await page.locator("#recSearch").fill("")
        assert len(await page.evaluate(listed)) == 3, "An empty search does not list every recording"
    finally:
        await record_close(page, server)


@test("Replay strip: an hour-long replay reads h:mm:ss, its counters are not read out each second, and its end counts every event")
async def _(page):
    try:
        await page.evaluate("""() => {
            state.recordings = [{id: 1, name: 'long soak'}];
            _applyReplayStatus({active: true, recording_id: 1, position_s: 300, duration_s: 7480, speed: 1,
                                paused: false, events_emitted: 1499, total_events: 1500, finished: false});
            showReplayStrip(); }""")
        shown = await page.locator("#replayTime").inner_text()
        assert shown == "0:05:00 / 2:04:40", f"The strip's time reads {shown!r}"
        live = await page.evaluate("[...document.querySelectorAll('#replayStrip [aria-live]')].map((e) => e.id)")
        assert live == ["replayStripLabel"], f"Read out as they change: {live}"
        # The replay's end, through the handler the WebSocket's messages go to.
        await page.evaluate("""() => { connectWs();
            state.ws.onmessage({data: JSON.stringify({type: 'replay_ended', recording_id: 1, finished: true})}); }""")
        await page.wait_for_selector("#replayStrip.replay-strip-finished", timeout=WAIT_MS)
        events = await page.locator("#replayEvents").inner_text()
        assert events.startswith("1,500 / 1,500"), f"At its end the strip counts {events!r}"
    finally:
        await page.evaluate("disconnectWs(); hideReplayStrip(); state.recordings = [];")


# ── Debug tab ──
#
# _DebugServer answers /api/* as the backend does with a CAN session up on
# vcan0, where frames come in at exactly 100 a second, so a rate the tab
# shows can be checked; it also plays the server end of /ws. Each test opens
# the tab on a freshly loaded page.

# What get_can_link_diagnostics finds on a vcan: no controller to report on.
VCAN_LINK = {"operstate": "unknown", "state": None, "bitrate": None, "dbitrate": None, "berr_tx": None,
             "berr_rx": None, "restart_ms": None, "restarts": None, "bus_errors": None, "arbitration_lost": None,
             "error_warning": None, "error_passive": None, "bus_off": None}


class _DebugServer:
    """The backend's /api/* and /ws for the Debug tab, over a CAN session on vcan0."""

    def __init__(self):
        self.started = time.monotonic()
        self.requests = []          # (monotonic time, path)
        self.answers = {}           # path -> (JSON, status) given instead of the usual answer
        self.ws = None              # the server end of the dashboard's socket
        self.received = []          # what the dashboard sent on it
        self.ring = []              # frames captured, oldest first
        self.queued = None          # captured since the dashboard subscribed, not sent yet (None: not subscribed)
        self.right_after = []       # frames the bus carries the moment the dashboard subscribes
        self.can = True             # a CAN session is up
        self.capturing = False      # its transport capture is on (it stays on until CAN disconnects)
        self.link = VCAN_LINK       # what the transport diagnostics say of the controller

    def _transport(self):
        frames_in = 1000 + int(100 * (time.monotonic() - self.started))
        stats = {"in_frames": frames_in, "in_frames_cyphal": frames_in, "in_frames_cyphal_accepted": frames_in,
                 "in_frames_errored": 0, "in_frames_loopback": 0, "out_frames": 40, "out_frames_timeout": 0,
                 "out_frames_loopback": 0, "media_acceptance_filtering_efficiency": 1.0, "lost_loopback_frames": 0}
        return {"connected": True, "interface": "vcan0", "statistics": stats, "capture_active": self.capturing,
                "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": False},
                "link": self.link, "bus_utilization": 12.0}

    def _answer(self, path):
        if path in self.answers:
            return self.answers[path]
        if path == "/api/status":
            return {"status": "running" if self.can else "idle", "can_interface": "vcan0" if self.can else None,
                    "can_bitrate": None, "can_data_bitrate": None, "can_fd": False,
                    "available_interfaces": ["vcan0"], "available_adapters": [
                        {"interface": "vcan0", "label": "vcan0 (SocketCAN)", "needs_bitrate": False}],
                    "bus_utilization": 12.0 if self.can else None, "dropped": None, "last_error": None,
                    "cyphal_v11": None}, 200
        if path == "/api/can/transport":
            return (self._transport() if self.can else {"connected": False}), 200
        if path == "/api/can/capture":
            return {"active": True, "stats": self._stats(), "frames": self.ring[-500:]}, 200
        quiet = {"/api/nodes": {"node_count": 0, "nodes": {}}, "/api/replay/status": {"active": False},
                 "/api/recordings": {"recordings": []}, "/api/rawlogs": {"active": None, "logs": []}}
        return (quiet[path], 200) if path in quiet else ({"error": "Not found"}, 404)

    def _stats(self):
        n = len(self.ring)
        return {"active": True, "captured": n, "rx": n, "tx": 0, "cyphal": n, "foreign": 0, "dropped": 0}

    def capture(self, frames):
        """Frames the bus carried: into the ring, and queued for a subscribed dashboard."""
        self.ring += frames
        if self.queued is not None:
            self.queued += frames

    def flush(self):
        """What is queued, as one can_frame batch, as _capture_loop sends it."""
        if self.queued is None:  # the dashboard is not subscribed: nothing goes to it
            return
        frames, self.queued = self.queued, []
        self.ws.send(json.dumps({"type": "can_frame", "frames": frames, "stats": self._stats()}))

    async def handle(self, route):
        path = unquote(urlparse(route.request.url).path)
        self.requests.append((time.monotonic(), path))
        answer, status = self._answer(path)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})

    def on_socket(self, ws):
        self.ws = ws
        ws.on_message(lambda message: self._on_message(ws, json.loads(message)))

    def _on_message(self, ws, msg):
        """A `capture` message, answered as _handle_capture_message does."""
        self.received.append(msg)
        if msg.get("type") != "capture":
            return
        if not msg.get("enabled"):
            self.queued = None
            ws.send(json.dumps({"type": "capture_status", "active": False, "forwarding": False}))
            return
        if not self.can:
            ws.send(json.dumps({"type": "capture_status", "active": False, "error": "CAN not connected"}))
            return
        earlier = self.ring[-500:] if self.queued is None else []
        if self.queued is None:
            self.queued = []
        self.capturing = True
        ws.send(json.dumps({"type": "capture_status", "active": True, "stats": self._stats(), "frames": earlier}))
        self.capture(self.right_after)

    def polls(self, since):
        """How often the tab asked for the diagnostics since `since` (monotonic time)."""
        return sum(1 for t, path in self.requests if path == "/api/can/transport" and t >= since)


# The page's /ws reaches the server of the Debug test that runs. Playwright
# cannot take a socket route back, so it is made once and follows this.
_debug_server = None
_debug_socket_routed = False


async def debug_open(page, server, restore=False, connected=True):
    """The Debug tab on a freshly loaded page, served by `server`. With restore
    the page loads on the tab with a connected dashboard saved, as after a
    reload; otherwise it loads on Nodes, connects (or not), and opens the tab."""
    global _debug_server, _debug_socket_routed
    _debug_server = server
    if not _debug_socket_routed:
        _debug_socket_routed = True
        await page.route_web_socket("**/ws**", lambda ws: _debug_server and _debug_server.on_socket(ws))
    await page.route("**/api/**", server.handle)
    await page.evaluate(f"""() => {{ state.dashboardConnected = {json.dumps(restore)};
        state.activeView = {json.dumps('debug' if restore else 'nodes')}; _writeSettingsNow(); }}""")
    await page.reload(wait_until="load")
    if not restore:
        if connected:
            await page.locator("#connectDashboardBtn").click()
            await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.evaluate("switchView('debug')")
    if restore or connected:
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)


async def debug_close(page, server):
    global _debug_server
    await page.evaluate("() => { switchView('nodes'); disconnectAll(); }")
    _debug_server = None
    await page.unroute("**/api/**", server.handle)


# The value a diagnostics card shows for `label`.
DEBUG_STAT = """(label) => [...document.querySelectorAll('#debugBody .debug-stat')]
    .find(row => row.querySelector('.debug-stat-label').textContent === label)
    ?.querySelector('.debug-stat-val').textContent"""


@test("Debug: a page reopened on the tab asks once a second, and shows the bus's own frame rate")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server, restore=True)
    try:
        await page.wait_for_selector("#debugBody .debug-card", timeout=WAIT_MS)
        await page.wait_for_timeout(500)  # the connect that follows the reload has run
        since, read = time.monotonic(), []
        for _ in range(10):
            await page.wait_for_timeout(300)
            read.append(await page.evaluate(DEBUG_STAT, "Frames in"))
        polls = server.polls(since)
        rates = [int(m[1]) for text in read if (m := re.search(r"\(\+(\d+)/s\)", text or ""))]
        assert polls <= 4 and rates and all(90 <= rate <= 110 for rate in rates), \
            f"In 3 s the tab asked {polls} times; at 100 frames/s, Frames in read {sorted(set(read))}"
    finally:
        await debug_close(page, server)


@test("Debug: disconnected, the tab asks the backend nothing and says why, so a token can be typed in peace")
async def _(page):
    server = _DebugServer()
    for path in ("/api/status", "/api/nodes", "/api/can/transport"):  # a backend that wants a token
        server.answers[path] = ({"error": "Missing or invalid token"}, 401)
    await debug_open(page, server, connected=False)
    try:
        await page.wait_for_timeout(2500)
        asked, said = server.polls(0), await page.locator("#debugBody").inner_text()
        await page.evaluate("connectDashboard()")  # Connect, which the token prompt answers
        await page.locator("#authModalInput").fill("pasted-token")
        await page.wait_for_timeout(2000)  # reaching for "Save and retry"
        kept = await page.locator("#authModalInput").input_value()
        assert asked == 0 and "Not connected to backend" in said and kept == "pasted-token", \
            f"Disconnected, the tab asked for the diagnostics {asked} times and said {said!r}; " \
            f"2 s after pasting, the token field holds {kept!r}"
    finally:
        await page.evaluate("hideAuthModal()")
        await debug_close(page, server)


@test("Debug: the diagnostics cards show every row, and scroll when the window is short")
async def _(page):
    server = _DebugServer()
    await page.set_viewport_size({"width": 1024, "height": 800})
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", timeout=WAIT_MS)
        cut = await page.evaluate("""[...document.querySelectorAll('#debugBody .debug-card')]
            .map(card => card.scrollHeight - card.clientHeight)""")
        panel = await page.evaluate("[el('debugBody').scrollHeight, el('debugBody').clientHeight]")
        assert cut and max(cut) <= 1 and panel[0] > panel[1], \
            f"At 1024×800, rows cut off the bottom of each card (px): {cut}; the panel's scroll and visible height: {panel}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await debug_close(page, server)


async def debug_start_capture(page):
    await page.locator("#fmToggle").click()
    await page.wait_for_function("el('fmToggle').classList.contains('active')", timeout=WAIT_MS)


def _can_frame(n, src=42):
    """Captured frame n, as serialize_capture makes it: a message from node src, n ms into the capture."""
    return {"t": 1000 + n / 1000, "ts": 1.76e9 + n / 1000, "dir": "rx", "id": f"0x{0x107D5500 + n % 256:08X}",
            "ext": True, "dlc": 8, "data": "01 02 03 04 05 06 07 E5", "cyphal": True, "priority": "NOMINAL",
            "src": src, "dst": None, "transfer_id": n % 32, "start": True, "end": True, "kind": "msg", "port": 7509}


# The frames the table shows, newest first, known by their time (each is a different millisecond).
DEBUG_ROWS = "[...document.querySelectorAll('#fmRows tr')].map(row => row.querySelector('.fm-time').textContent)"


@test("Debug: a capture whose connection closes stops, and says so")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await server.ws.close(code=1012, reason="backend restarting")
        await page.wait_for_timeout(500)
        button, said = await page.locator("#fmToggle").inner_text(), await page.locator("#fmStatus").inner_text()
        assert button == "Start capture" and "Capture stopped" in said, \
            f"Once the socket closed, the button reads {button!r} and the status says {said!r}"
    finally:
        await debug_close(page, server)


@test("Debug: Start capture shows the frames caught before it, then the live ones, each once")
async def _(page):
    server = _DebugServer()
    server.capture([_can_frame(n) for n in range(10)])
    server.right_after = [_can_frame(n) for n in range(10, 13)]
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.wait_for_timeout(500)  # anything the tab asks for besides is answered
        server.flush()                    # the first live batch
        await page.wait_for_timeout(300)
        shown = await page.evaluate(DEBUG_ROWS)
        assert len(shown) == 13 and len(set(shown)) == 13, \
            f"13 frames came, the table has {len(shown)} rows, {len(shown) - len(set(shown))} of them repeats"
    finally:
        await debug_close(page, server)


async def debug_frames_come(page, server, numbers):
    server.capture([_can_frame(n) for n in numbers])
    server.flush()
    await page.wait_for_timeout(300)


@test("Debug: what comes while the frame table is paused shows on Resume")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await debug_frames_come(page, server, range(5))
        await page.locator("#fmPause").click()
        await debug_frames_come(page, server, range(5, 10))
        held = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmPause").click()  # Resume
        await debug_frames_come(page, server, range(10, 15))
        shown = await page.evaluate(DEBUG_ROWS)
        assert held == 5 and len(shown) == 15 and shown == sorted(shown, reverse=True), \
            f"Paused, the table held {held} rows (5 came before Pause); after Resume it shows {len(shown)} of the 15"
    finally:
        await debug_close(page, server)


@test("Debug: the frame filter shows the same rows whenever it is typed, and says when none match")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.locator("#fmFilter").fill("n42")
        for first in range(0, 6000, 250):  # one frame in a hundred from node 42, in the backend's batches
            server.capture([_can_frame(n, src=42 if n % 100 == 0 else 7) for n in range(first, first + 250)])
            server.flush()
        await page.wait_for_timeout(800)
        live = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmFilter").press("End")
        await page.locator("#fmFilter").press("Space")  # the same filter, typed again
        await page.wait_for_timeout(300)
        again = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmFilter").fill("no such frame")
        said = await page.locator("#fmEmpty").inner_text()
        assert live == again and said == "No frames match the filter.", \
            f"Filtered on n42, {live} rows as the frames came and {again} once typed again; " \
            f"a filter nothing matches says {said!r}"
    finally:
        await debug_close(page, server)


@test("Debug: screen readers are not read the frame counters on every batch")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.evaluate("""() => { window.e2eSaid = [];  // what live regions would read out
            document.querySelectorAll('.frame-monitor [aria-live]:not([aria-live=off]), .frame-monitor [role=status]')
                .forEach((node) => new MutationObserver(() => e2eSaid.push(node.id))
                    .observe(node, {childList: true, characterData: true, subtree: true})); }""")
        for first in range(0, 25, 5):
            await debug_frames_come(page, server, range(first, first + 5))
        said = await page.evaluate("e2eSaid")
        assert not said, f"In 5 batches of frames, live regions changed {len(said)} times: {sorted(set(said))}"
    finally:
        await debug_close(page, server)


@test("Debug: Start capture is off, saying why, until there is a bus to capture from")
async def _(page):
    server = _DebugServer()
    server.can = False
    await debug_open(page, server)
    start, empty = page.locator("#fmToggle"), page.locator("#fmEmpty")
    try:
        can_idle = (await start.is_disabled(), await empty.inner_text())
        server.can = True
        await page.evaluate("state.canConnected = true; updateSemaphores();")
        can_up = await start.is_disabled()
        await page.evaluate("disconnectWs(); updateSemaphores();")  # as in the 5 s before the socket opens
        no_socket = (await start.is_disabled(), await start.get_attribute("title") or "")
        assert can_idle == (True, "Connect a CAN interface to capture frames.") and not can_up \
            and no_socket[0] and "connection" in no_socket[1], \
            f"Start disabled, and why: with CAN idle {can_idle}; with CAN up {can_up}; with no socket yet {no_socket}"
    finally:
        await debug_close(page, server)


@test("Debug: the frame table says when the bus is in capture mode already, and its stop button reads Stop")
async def _(page):
    server = _DebugServer()
    server.capturing = True  # started from another dashboard: it stays on until CAN disconnects
    await debug_open(page, server)
    try:
        await page.wait_for_timeout(1200)  # the diagnostics have answered
        said = await page.locator("#fmEmpty").inner_text()
        await debug_start_capture(page)
        label = await page.locator("#fmToggle").inner_text()
        assert "until CAN disconnects" in said and label == "Stop", \
            f"With the bus capturing already, the table says {said!r}; capturing, the button reads {label!r}"
    finally:
        await debug_close(page, server)


@test("Debug: the frame table keeps its frames, filter and pause across tabs, and takes in what came meanwhile")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await debug_frames_come(page, server, range(5))
        await page.locator("#fmFilter").fill("n42")
        await page.evaluate("switchView('nodes')")
        await debug_frames_come(page, server, range(5, 10))  # while another tab is shown
        await page.evaluate("switchView('debug')")
        back = (len(await page.evaluate(DEBUG_ROWS)), await page.locator("#fmFilter").input_value(),
                await page.locator("#fmToggle").inner_text())
        await page.locator("#fmPause").click()
        await page.evaluate("switchView('nodes'); switchView('debug')")
        still_paused = await page.locator("#fmPause").get_attribute("aria-pressed")
        assert back == (10, "n42", "Stop") and still_paused == "true", \
            f"Back on the tab, (rows, filter, button) are {back}; paused before leaving, aria-pressed={still_paused}"
    finally:
        await debug_close(page, server)


# Each diagnostics row, label: value.
DEBUG_STATS = """Object.fromEntries([...document.querySelectorAll('#debugBody .debug-stat')].map(
    row => [row.querySelector('.debug-stat-label').textContent, row.querySelector('.debug-stat-val').textContent]))"""


@test("Debug: the diagnostics show what the interface reports, in the sidebar's units, and nothing it does not")
async def _(page):
    server = _DebugServer()
    server.link = {"bitrate": 500000, "dbitrate": None, "adapter_frames_in": 123456, "adapter_frames_out": 789,
                   "adapter_send_failures": 0, "adapter_error_frames": 17}  # what the hub knows of an adapter
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", timeout=WAIT_MS)
        shown = await page.evaluate(DEBUG_STATS)
        blank = [label for label, value in shown.items() if value == "—"]
        assert not blank and shown.get("Arbitration bitrate") == "500 kbit/s" \
            and (shown.get("Adapter frames in") or "").startswith("123456"), \
            f"Rows with nothing to say: {blank}; bitrate {shown.get('Arbitration bitrate')!r}; " \
            f"the adapter's frames in {shown.get('Adapter frames in')!r}"
    finally:
        await debug_close(page, server)


@test("Debug: the diagnostics update in place, and a failed update keeps the last values, marked stale")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", timeout=WAIT_MS)
        await page.evaluate("""() => { const value = [...document.querySelectorAll('#debugBody .debug-stat')]
            .find(row => row.textContent.includes('Interface')).querySelector('.debug-stat-val');
            const range = document.createRange(); range.selectNodeContents(value);
            getSelection().removeAllRanges(); getSelection().addRange(range); }""")
        await page.wait_for_timeout(1300)  # a poll has answered
        kept = await page.evaluate("getSelection().toString()")
        server.answers["/api/can/transport"] = ({"error": "Internal Server Error"}, 500)
        await page.wait_for_timeout(1300)
        stale = await page.evaluate("""() => ({cards: document.querySelectorAll('#debugBody .debug-card').length,
            said: document.querySelector('#debugBody .debug-stale-note')?.textContent || ''})""")
        assert kept == "vcan0" and stale["cards"] == 3 and "failed" in stale["said"], \
            f"Selected 'vcan0', a poll later the selection is {kept!r}; after a failed poll: {stale}"
    finally:
        await debug_close(page, server)


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
