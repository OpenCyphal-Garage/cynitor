"""Browser end-to-end tests: Graph tab. Run through test_landing_page.py, which imports every suite in order."""

from pathlib import Path
import time

from harness import WAIT_MS, test


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
    assert ghosts == expected, f"Devices that lost their node-ID drawn as {ghosts}, of {list(nodes)}"


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
        text = Path(path).read_text(encoding="utf-8")
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
    # Offline is every device not on the bus: node 40, and the two remembered ones that hold no node-ID.
    for expected in ("3 offline", "1 unusual health", "1 silent"):
        assert expected in chips, f"Missing {expected!r} in the strip: {chips}"
    assert not any("displaced" in chip for chip in chips), f"The strip still counts devices as displaced: {chips}"
    offline = page.locator('#graphStatus [data-focus="offline"]')
    await offline.click()
    dimmed = await page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-node')]
        .map(el => [d3.select(el).datum().id, el.classList.contains('graph-dim')]))""")
    await offline.click()
    lit = sorted(key for key, dim in dimmed.items() if not dim)
    assert lit == ["dev:40", "dev:uid:" + "aa" * 16, "dev:uid:" + "bb" * 16], f"Picking 'offline' left these lit: {lit}"


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
