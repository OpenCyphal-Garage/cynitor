"""Browser end-to-end tests: Test nodes tab. Run through test_landing_page.py, which imports every suite in order."""

from urllib.parse import unquote
from urllib.parse import urlparse
import json
import re

from harness import WAIT_MS, answer_dialog, test
from suite_graph import _graph_node


# ── Test nodes tab ──

_VOLTAGE = "uavcan.si.unit.voltage.Scalar.1.0"
_VOLTAGE_SCHEMA = {"kind": "struct", "type": _VOLTAGE, "constants": [], "fields": [
    {"name": "volt", "schema": {"kind": "float", "type": "saturated float32", "min": -3.4e38, "max": 3.4e38}}]}
_TEST_NODE_OPTIONS = {
    "health": ["nominal", "advisory", "caution", "warning"],
    "modes": ["operational", "initialization", "maintenance", "software_update"],
    "waves": ["sine", "ramp", "square", "noise", "count"],
    "limits": {"test_nodes": 16, "publishers": 8, "node_id": [1, 125], "subject_id": [0, 8191],
               "rate_hz": [0.01, 100.0], "total_rate_hz": 1000.0},
}


def _test_node(test_id, name, node_id=60, running=False):
    return {"id": test_id, "unique_id": "ab" * 16, "name": name, "node_id": node_id, "health": "nominal",
            "mode": "operational", "vendor_status": 0, "software_version": {"major": 1, "minor": 0},
            "publishers": [{"subject_id": 1000, "type": _VOLTAGE, "rate_hz": 10.0, "port": "voltage_scalar",
                            "value": {"volt": 12}}],
            "assigned_node_id": None, "running": running, "error": None, "started_unix": None}


class _TestNodesServer:
    """The backend's test node routes, as server/websocket_server.py answers them."""

    def __init__(self, bus="sim:classic"):
        self.bus = bus        # the bus CAN is connected to (sim:... is simulated); None while it is not
        self.nodes = []       # as GET /api/testnodes lists them
        self.requests = []    # (method, path, body)
        self.refuse = None    # an error POST /api/testnodes answers with, 400

    def _answer(self, method, path, body):
        if path == "/api/dsdl/namespaces":
            return {"namespaces": {"uavcan": {"children": {}, "types": [
                {"kind": "message", "compiled": True, "full_name": _VOLTAGE}]}}}, 200
        if path == f"/api/dsdl/value-schema/{_VOLTAGE}":
            return _VOLTAGE_SCHEMA, 200
        if path == "/api/testnodes" and method == "GET":
            bus = self.bus and {"simulated": self.bus.startswith("sim:"), "interface": self.bus}
            return {"test_nodes": self.nodes, "bus": bus, **_TEST_NODE_OPTIONS}, 200
        if path == "/api/testnodes":
            if self.refuse:
                return {"error": self.refuse}, 400
            node = {**_test_node(f"t{len(self.nodes) + 1}", body["name"]), **body}
            self.nodes.append(node)
            return node, 201
        match = re.fullmatch(r"/api/testnodes/(\w+)(/start|/stop)?", path)
        node = next((n for n in self.nodes if match and n["id"] == match[1]), None)
        if node is None:
            return {"error": "There is no test node"}, 404
        if match[2] == "/start":
            if self.bus is None:
                return {"error": "Connect CAN first: test nodes run on the bus Cynitor is connected to"}, 409
            if not self.bus.startswith("sim:") and not (body or {}).get("confirm_real_bus"):
                return {"error": "This is a real bus. Confirm to start it", "confirm": "real_bus"}, 409
            if node["node_id"] is None:  # left to the server, which gives it a free one
                node["assigned_node_id"] = node["assigned_node_id"] or 100
            node["running"] = True
        elif match[2] == "/stop":
            node["running"] = False
        elif method == "PUT":
            node.update(body)
        elif method == "DELETE":
            self.nodes.remove(node)
            return {"deleted": node["id"]}, 200
        return node, 200

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        answer, status = self._answer(request.method, path, body)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})

    def sent(self, method, path):
        """The bodies of the `method` requests to `path`, in order."""
        return [body for m, p, body in self.requests if (m, p) == (method, path)]


async def testnodes_open(page, server):
    """The Test nodes tab on a freshly loaded page, with a connected dashboard, served by `server`."""
    await page.evaluate("() => { state.dashboardConnected = false; _writeSettingsNow(); }")
    await page.reload(wait_until="load")
    await page.route("**/api/testnodes**", server.handle)
    await page.route("**/api/dsdl/**", server.handle)
    await page.evaluate("state.dashboardConnected = true; switchView('testnodes')")
    await page.wait_for_selector("#tnList .tn-card, #tnList .tn-empty", timeout=5000)


async def testnodes_close(page, server):
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false; state.canConnected = false;
        state.latestNodesPayload = null; renderNodesTable(); TestNodesView.refreshIndicator(); }""")
    await page.unroute("**/api/testnodes**", server.handle)
    await page.unroute("**/api/dsdl/**", server.handle)


@test("Test nodes: one is made in the editor, its fields built from its type, a waveform sent as the API takes it")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    try:
        assert "nothing real can move" in await page.locator("#tnBus").inner_text()
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        await editor.get_by_label("Name", exact=True).fill("test.battery")
        await editor.get_by_role("button", name="Add publisher").click()
        type_field = editor.get_by_label("Message type")
        await type_field.fill(_VOLTAGE)
        await type_field.dispatch_event("change")
        await editor.get_by_label("value.volt, constant or waveform").select_option("sine")
        for what, value in (("from", "11"), ("to", "12.6"), ("period in seconds", "20")):
            await editor.get_by_label(f"value.volt, {what}").fill(value)
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_selector("#tnList .tn-card", timeout=WAIT_MS)
        [sent] = server.sent("POST", "/api/testnodes")
        card = await page.locator("#tnList .tn-card").inner_text()
        assert (sent["name"], sent["node_id"], sent["health"]) == ("test.battery", None, "nominal") and \
            sent["publishers"] == [{"subject_id": 1000, "type": _VOLTAGE, "rate_hz": 1,
                                    "value": {"volt": {"$wave": "sine", "min": 11, "max": 12.6, "period_s": 20}}}], \
            f"Sent: {sent}"
        assert "test.battery" in card and "node-ID auto" in card and _VOLTAGE in card, f"The card: {card!r}"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: the node-ID is left to Cynitor unless one is typed, and the card shows the one it was given")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    card_ids = page.locator("#tnList .tn-card .tn-card-id")
    try:
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        field = editor.get_by_label("Node-ID", exact=True)
        offered = [await field.input_value(), await field.get_attribute("placeholder"), await field.get_attribute("required")]
        await editor.get_by_label("Name", exact=True).fill("left.to.cynitor")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_selector("#tnList .tn-card", timeout=WAIT_MS)
        before_start = await card_ids.first.inner_text()
        await page.get_by_role("button", name="Start left.to.cynitor").click()
        await page.wait_for_selector(".tn-state-running", timeout=WAIT_MS)
        running = await card_ids.first.inner_text()
        await page.locator("#tnNew").click()
        await editor.get_by_label("Name", exact=True).fill("set.by.hand")
        await editor.get_by_label("Node-ID", exact=True).fill("42")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_function("document.querySelectorAll('#tnList .tn-card').length === 2", timeout=WAIT_MS)
        typed = await card_ids.nth(1).inner_text()
        sent = [body["node_id"] for body in server.sent("POST", "/api/testnodes")]
    finally:
        await testnodes_close(page, server)
    assert offered == ["", "auto", None], f"A new test node's Node-ID field (value, placeholder, required): {offered}"
    assert sent == [None, 42], f"Node-IDs sent for the two test nodes: {sent}"
    assert (before_start, running, typed) == ("node-ID auto", "node-ID 100 · auto", "node-ID 42"), \
        f"The cards' node-IDs: {before_start!r}, running {running!r}; the typed one {typed!r}"


@test("Test nodes: a definition the server refuses says why in the editor, which keeps what was typed")
async def _(page):
    server = _TestNodesServer()
    server.refuse = "publishers[0].type: There is no DSDL type demo.Nope.1.0"
    await testnodes_open(page, server)
    try:
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        await editor.get_by_label("Name", exact=True).fill("probe")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_function("el('tnEditor').querySelector('.tn-form-error')?.textContent", timeout=WAIT_MS)
        said = await editor.locator(".tn-form-error").inner_text()
        kept = await editor.get_by_label("Name", exact=True).input_value()
        assert "There is no DSDL type demo.Nope.1.0" in said and kept == "probe", f"Said {said!r}; the name: {kept!r}"
        await page.keyboard.press("Escape")
        assert await page.locator("#tnEditor").is_hidden(), "Escape did not close the editor"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: on a real bus Start asks first, and only a confirmed start goes again")
async def _(page):
    server = _TestNodesServer(bus="pcan:PCAN_USBBUS1")
    server.nodes.append(_test_node("t1", "probe"))
    await testnodes_open(page, server)
    try:
        banner = await page.locator("#tnBus").inner_text()
        start = page.get_by_role("button", name="Start probe")
        await start.click()
        asked = [await answer_dialog(page, accept=False)]  # declined the first time, then accepted
        await page.wait_for_function("!document.querySelector('.tn-card button[data-action=\"start\"]')?.disabled",
                                     timeout=WAIT_MS)
        declined = server.sent("POST", "/api/testnodes/t1/start")
        await start.click()
        asked.append(await answer_dialog(page, accept=True))
        await page.wait_for_selector(".tn-state-running", timeout=WAIT_MS)
        assert "real bus" in banner and len(asked) == 2 and "every node on the bus" in asked[0], \
            f"The banner: {banner!r}; asked: {asked}"
        assert declined == [None] and server.sent("POST", "/api/testnodes/t1/start") == [
            None, None, {"confirm_real_bus": True}], f"Starts sent: {server.sent('POST', '/api/testnodes/t1/start')}"
        focused = await page.evaluate("document.activeElement?.getAttribute('aria-label')")
        assert focused == "Stop probe", f"After Start, the focus is on {focused!r}"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: health and mode change from the card, sent with the rest of the node as it is")
async def _(page):
    server = _TestNodesServer()
    server.nodes.append(_test_node("t1", "probe", running=True))
    await testnodes_open(page, server)
    try:
        await page.get_by_label("Health of probe").select_option("warning")
        await page.wait_for_function("document.querySelector('.tn-card select[data-action=\"health\"]')?.disabled === false",
                                     timeout=WAIT_MS)
        [sent] = server.sent("PUT", "/api/testnodes/t1")
        expected = {key: value for key, value in _test_node("t1", "probe").items()
                    if key not in ("id", "unique_id", "assigned_node_id", "running", "error", "started_unix")}
        assert sent == {**expected, "health": "warning"}, f"Sent: {sent}"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: without CAN, Start is off and the tab says the simulated bus needs no hardware")
async def _(page):
    server = _TestNodesServer(bus=None)
    server.nodes.append(_test_node("t1", "probe"))
    await testnodes_open(page, server)
    try:
        banner = await page.locator("#tnBus").inner_text()
        start = page.get_by_role("button", name="Start probe")
        assert "CAN is not connected" in banner and "simulated bus" in banner, f"The banner: {banner!r}"
        assert await start.is_disabled() and await start.get_attribute("title") == "Connect CAN first"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: a running one is marked in the Nodes table and the Graph, and the tab has a dot meanwhile")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    nodes = {"60": {**_graph_node(60, "test.battery", [1000, 7509], [], uid_byte=0xAB), "test_node": "t1"},
             "61": _graph_node(61, "org.example.esc", [7509], [], uid_byte=2)}
    try:
        await page.evaluate(f"""() => {{ state.canConnected = true; state.latestNodesPayload =
            {{node_count: 2, nodes: {json.dumps(nodes)}}}; switchView('nodes'); renderNodesTable();
            TestNodesView.refreshIndicator(); }}""")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)
        marked = await page.evaluate("""() => [...document.querySelectorAll('#nodesTable .tabulator-row')]
            .filter(row => row.querySelector('.test-node-badge')).map(row => row.innerText.split('\\n').join(' '))""")
        dot = await page.evaluate("el('viewTabTestNodes').classList.contains('testnodes-active')")
        await page.evaluate("switchView('graph')")
        await page.wait_for_function("""[...document.querySelectorAll('.graph-label-status')]
            .some(s => s.textContent === 'test node')""", timeout=WAIT_MS)
        await page.evaluate("""() => { state.latestNodesPayload.nodes['60'].has_disappeared = true;
            TestNodesView.refreshIndicator(); }""")
        gone = await page.evaluate("el('viewTabTestNodes').classList.contains('testnodes-active')")
        assert len(marked) == 1 and "test.battery" in marked[0] and dot and not gone, \
            f"Rows with the badge: {marked}; the dot while it ran: {dot}, once it stopped: {gone}"
    finally:
        await testnodes_close(page, server)
