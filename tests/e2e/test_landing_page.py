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
    expected = ["ID", "Name", "State", "Health", "Mode", "SW", "Rate", "Uptime",
                "Publishers", "Subscribers", "Servers", "Clients"]
    assert headers == expected, f"Expected headers {expected}, got {headers}"


@test("Empty table shows 'Not connected'")
async def _(page):
    text = await page.locator(".tabulator-placeholder").text_content()
    assert "not connected" in text.lower(), f"Expected 'Not connected', got '{text}'"


@test("Filter row has 12 inputs")
async def _(page):
    count = await page.locator(".tabulator-header-filter input").count()
    assert count == 12, f"Expected 12 filter inputs, got {count}"


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


@test("Graph: the open info panel is left alone while nothing changes")
async def _(page):
    # Rebuilt each second, its buttons were swapped out under the pointer.
    changes = await page.evaluate("""() => new Promise(done => {
        let n = 0;
        const observer = new MutationObserver(records => { n += records.length; });
        observer.observe(document.getElementById('graphInfo'), {childList: true, subtree: true, characterData: true});
        setTimeout(() => { observer.disconnect(); done(n); }, 2500);
    })""")
    await page.locator("#graphInfoClose").click()
    assert changes == 0, f"Info panel changed {changes} times in 2.5 s without new data"


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
