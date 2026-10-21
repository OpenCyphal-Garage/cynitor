"""Browser end-to-end tests: Nodes and Subjects tables. Run through test_landing_page.py, which imports every suite in order."""

import asyncio

from harness import test
from suite_graph import _graph_ghost, _graph_node


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


@test("Registers: a node that does not answer says so, not that the backend is unreachable")
async def _(page):
    async def silent_node(route):
        await route.fulfill(status=504, json={"error": "Node 20 does not answer register requests"},
                            headers={"Access-Control-Allow-Origin": "*"})
    await page.route("**/api/registers/20", silent_node)
    await page.evaluate("setSelectedNode(20)")
    await page.locator('.detail-tab[data-tab="registers"]').click()
    try:
        await page.locator("#selectedNodeContent .reg-refresh-btn").wait_for(timeout=3000)
        helper = page.locator("#selectedNodeContent .svc-state-helper")
        text = await helper.inner_text()
        assert text.startswith("Node 20 does not answer register requests."), f"Registers tab says: {text!r}"
        assert "backend" not in text, f"Registers tab blames the backend: {text!r}"
    finally:
        await page.unroute("**/api/registers/20", silent_node)
        await page.evaluate("REG_CACHE.clear()")
        await page.locator('.detail-tab[data-tab="publishers"]').click()


@test("Registers: a write asks in the dashboard's own dialog, naming the register, the device, and its current and new value; Esc and Cancel write nothing")
async def _(page):
    cors = {"Access-Control-Allow-Origin": "*"}
    writes, native = [], []

    async def register_list(route):
        await route.fulfill(json={"registers": [{"register_name": "uavcan.node.description", "type": "string",
                                                 "access": "read-write", "value": "motor-left"}]}, headers=cors)

    async def register_set(route):
        writes.append(route.request.post_data_json)
        await route.fulfill(json={"status": "ok", "name": "uavcan.node.description", "value": "motor-right"}, headers=cors)

    def on_native(dialog):  # the browser's own confirm(), which the write no longer uses
        native.append(dialog.message)
        asyncio.ensure_future(dialog.dismiss())

    await page.route("**/api/registers/20", register_list)
    await page.route("**/api/registers/20/set", register_set)
    page.on("dialog", on_native)
    dialog, write = page.locator("#confirmDialog"), page.locator("#selectedNodeContent .reg-save-btn")
    try:
        await page.evaluate("REG_CACHE.clear(); setSelectedNode(20)")
        await page.locator('.detail-tab[data-tab="registers"]').click()
        await page.locator("#selectedNodeContent .reg-value-input").fill("motor-right")
        await write.click()
        await dialog.wait_for(state="visible", timeout=3000)
        shown = await page.evaluate("""() => ({title: el('confirmDialogTitle').textContent,
            subject: el('confirmDialogSubject').textContent, meta: el('confirmDialogMeta').textContent,
            rows: [...el('confirmDialogRows').children].map((e) => e.textContent),
            focus: document.activeElement.id, button: el('confirmDialogOk').textContent})""")
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="hidden", timeout=3000)
        after_esc = (len(writes), await page.evaluate("document.activeElement.classList.contains('reg-save-btn')"))
        await write.click()
        await page.locator("#confirmDialog button[value='cancel']").click()
        await dialog.wait_for(state="hidden", timeout=3000)
        after_cancel = len(writes)
        await write.click()
        await page.locator("#confirmDialogOk").click()
        await page.locator("#selectedNodeContent .reg-status", has_text="written and verified").wait_for(timeout=3000)
    finally:
        page.remove_listener("dialog", on_native)
        await page.unroute("**/api/registers/20", register_list)
        await page.unroute("**/api/registers/20/set", register_set)
        await page.evaluate("REG_CACHE.clear()")
        await page.locator('.detail-tab[data-tab="publishers"]').click()
    assert (shown["title"], shown["subject"]) == ("Write register", "uavcan.node.description"), f"The dialog: {shown}"
    assert shown["meta"].startswith("node 20"), f"It names the device: {shown['meta']}"
    assert shown["rows"] == ["type", "string", "current", "motor-left", "new", "motor-right"], f"Its rows: {shown['rows']}"
    assert (shown["focus"], shown["button"]) == ("confirmDialogOk", "Write"), f"Its button, and the focus: {shown}"
    assert after_esc == (0, True), f"Esc wrote nothing and gave the focus back to Write: {after_esc}"
    assert after_cancel == 0, "Cancel wrote the register"
    assert writes == [{"name": "uavcan.node.description", "value": "motor-right", "type": "string"}], f"Written: {writes}"
    assert native == [], f"A native dialog asked: {native}"


@test("Cards: a node's card shows its own message, not another publisher's of the subject")
async def _(page):
    # On a subject several nodes publish, the card showed whichever's came last
    # until the node's own arrived (audit F-043).
    shown = await page.evaluate("""() => {
        const saved = [new Map(state.latestByNode), new Map(state.latestBySubject)];
        const other = {subject_id: 7510, publisher_node_id: 100, timestamp_unix: Date.now() / 1000, rate: 1,
                       message_type: 'List_1_0', attributes: [{attribute: 'servers', value: [3333]}]};
        state.latestBySubject.set(7510, other);
        state.latestByNode.set(100, new Map([[7510, other]]));
        state.latestByNode.set(21, new Map());
        const before = buildSubjectDetailData([7510], 21)[0];
        const own = {...other, publisher_node_id: 21, attributes: [{attribute: 'servers', value: [430]}]};
        state.latestByNode.get(21).set(7510, own);
        const after = buildSubjectDetailData([7510], 21)[0];
        const subscriber = buildSubjectDetailData([7510], null)[0];
        state.latestByNode = saved[0]; state.latestBySubject = saved[1];
        return [before.attributes, before.messageType, after.attributes[0].value, subscriber.attributes[0].value];
    }""")
    assert shown == [[], "uavcan.node.port.List.1.0", [430], [3333]], f"Cards: {shown}"


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
    assert text == "6 nodes 2 offline 2 unusual health Hide offline Compact", f"Strip: {text!r}"
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    await strip.locator('[data-focus="health"]').click()
    try:
        assert await page.evaluate(ids) == [21, 30], "'unusual health' picks out nodes 21 and 30"
    finally:
        await strip.locator('[data-focus="health"]').click()
    assert len(await page.evaluate(ids)) == 6, "Clicked again, the count lets the rows go"


@test("Tables: Hide offline takes every offline node out of the Nodes table at once, says how many, and is remembered")
async def _(page):
    strip = page.locator("#nodesStatus")
    toggle = strip.locator("[data-hide-offline]")
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    said = "document.getElementById('nodesStatus').innerText.replace(/\\s+/g, ' ').trim()"
    listed = await page.evaluate(ids)
    assert await toggle.count() == 1, "The Nodes strip has no Hide offline"
    await toggle.click()
    try:
        await page.wait_for_function(f"({ids}).length === 4", timeout=3000)
        hidden = {"ids": await page.evaluate(ids), "strip": await page.evaluate(said),
                  "pressed": await toggle.get_attribute("aria-pressed")}
        await page.wait_for_function(
            "JSON.parse(localStorage.getItem('cynitor.dashboard.settings.v1')).hideOfflineNodes === true", timeout=2000)
        # With every other node hidden by its own eye button, only offline nodes are
        # left to list: the empty table says where they went.
        await page.evaluate("""() => { for (const id of [10, 20, 21, 30]) {
            state.hiddenNodeIds.add(nodeStableKey(state.latestNodesPayload.nodes[id])); } renderNodesTable(); }""")
        await page.wait_for_function(f"({ids}).length === 0", timeout=3000)
        empty = await page.locator("#nodesTable .tabulator-placeholder").inner_text()
    finally:
        await page.evaluate("state.hiddenNodeIds.clear(); renderNodesTable()")
        await toggle.click()
    await page.wait_for_function(f"({ids}).length === 6", timeout=3000)
    assert len(listed) == 6 and hidden["ids"] == [10, 20, 21, 30], f"Listed {listed}; with Hide offline on: {hidden['ids']}"
    assert hidden["strip"] == "4 nodes · 2 offline hidden 2 unusual health Hide offline Compact" \
        and hidden["pressed"] == "true", f"The strip with Hide offline on: {hidden}"
    assert "2 offline nodes are hidden" in " ".join(empty.split()), f"The table with only offline nodes, hidden: {empty!r}"


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
        await page.locator("#nodesTable .tabulator-row", has_text="old_sensor").locator(
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
