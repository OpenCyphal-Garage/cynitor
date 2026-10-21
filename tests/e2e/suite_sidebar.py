"""Browser end-to-end tests: Sidebar. Run through test_landing_page.py, which imports every suite in order."""

from urllib.parse import unquote
from urllib.parse import urlparse
import asyncio
import json
import os
import re
import time

from harness import BASE_URL, WAIT_MS, WEBSITE_DIR, test
from suite_graph import _graph_node
from suite_dsdl import TOKEN_COLOUR
import suite_debug  # its socket route is shared: one per page


# ── Sidebar ──

@test("Sidebar: collapsed, none of its controls takes the focus, and its toggle says it is collapsed")
async def _(page):
    toggle = page.locator("#sidebarCollapseBtn")
    await toggle.click()
    try:
        await page.wait_for_timeout(400)  # the collapse plays for 0.2 s
        focusable = await page.evaluate("""() => [...document.querySelectorAll(
            'nav.sidebar button, nav.sidebar input, nav.sidebar select, nav.sidebar a[href]')]
            .filter((e) => { e.focus(); return document.activeElement === e; })
            .map((e) => e.id || e.textContent.trim())""")
        collapsed = await toggle.get_attribute("aria-expanded")
    finally:
        await toggle.click()
        await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)
    expanded = await toggle.get_attribute("aria-expanded")
    assert not focusable and (collapsed, expanded) == ("false", "true"), \
        f"Collapsed, these sidebar controls still take the focus: {focusable}; " \
        f"the toggle's aria-expanded collapsed/open: {collapsed}/{expanded}"


SIDEBAR_VCAN = {"interface": "vcan0", "label": "vcan0", "needs_bitrate": False, "supports_fd": False}
SIDEBAR_PEAK = {"interface": "pcan:PCAN_USBBUS1", "label": "PEAK PCAN-USB (PCAN_USBBUS1)",
                "needs_bitrate": True, "supports_fd": True}


class _SidebarServer:
    """The backend the sidebar talks to: status, CAN connect and disconnect, the nodes on the bus."""

    def __init__(self):
        self.can = None     # the interface CAN is connected to, or None
        self.down = False   # every request refused, as by a backend that is not running
        self.ws = None      # the server end of the dashboard's socket
        self.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK]  # what discovery lists
        self.connect_error = None  # what a CAN connect fails with, if it does
        self.last_error = None     # why the backend last ended a CAN session itself
        self.token = None          # the CYNITOR_AUTH_TOKEN it wants, if any
        self.connect_delay = 0     # seconds a CAN connect takes
        self.bus_errors = None     # what it says of errors on the bus, while connected
        self.update = None         # a newer release its update check found
        self.history_error = None  # why its database cannot be written, while connected
        self.listen_only = False   # whether the session it runs sends nothing
        self.no_commands = False   # whether the session it runs changes no node
        self.connect_body = None   # what the last CAN connect asked for
        self.allocator = None      # what it says of its node-ID allocator, while it runs one
        self.decode_on_demand = False  # whether it decodes only what is watched or recorded
        self.decoding_error = None     # what switching that fails with, if it does
        self.watched = []          # every list of watched subjects the dashboard sent, in turn

    async def handle(self, route):
        if self.down:
            await route.abort("connectionrefused")
            return
        request = route.request
        path = unquote(urlparse(request.url).path)
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            await route.fulfill(json={"error": "Missing or invalid token"}, status=401,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        if path == "/api/status":
            answer = {"status": "running" if self.can else "idle", "can_interface": self.can,
                      "can_bitrate": None, "can_data_bitrate": None, "can_fd": False,
                      "available_interfaces": ["vcan0"], "available_adapters": self.adapters,
                      "bus_utilization": 30.0 if self.can else None, "dropped": None,
                      "last_error": self.last_error, "cyphal_v11": None,
                      "bus_errors": self.bus_errors if self.can else None,
                      "allocator": self.allocator if self.can else None,
                      "decode_on_demand": self.decode_on_demand, "update": self.update,
                      "history_error": self.history_error if self.can else None,
                      "listen_only": bool(self.can) and self.listen_only,
                      "no_commands": bool(self.can) and self.no_commands,
                      "own_node_id": None if not self.can or self.listen_only else 127}
        elif path == "/api/decoding" and self.decoding_error:
            await route.fulfill(json={"error": self.decoding_error}, status=500,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        elif path == "/api/decoding":
            self.decode_on_demand = request.post_data_json["on_demand"]
            answer = {"decode_on_demand": self.decode_on_demand}
        elif path == "/api/can/connect" and self.connect_error:
            await route.fulfill(json={"error": self.connect_error}, status=500,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        elif path == "/api/can/connect":
            await asyncio.sleep(self.connect_delay)
            self.connect_body = request.post_data_json
            self.can = self.connect_body["interface"]
            # As the backend: its allocator runs only in a session asked to run it.
            self.allocator = {"other_allocator": None, "allocated": 0} if self.connect_body.get("allocator") else None
            self.listen_only = self.connect_body.get("listen_only") is True
            self.no_commands = self.connect_body.get("no_commands") is True
            answer = {"status": "running", "can_interface": self.can, "can_fd": False, "allocator": self.allocator,
                      "listen_only": self.listen_only, "no_commands": self.no_commands}
        elif path == "/api/can/disconnect":
            self.can, self.allocator, answer = None, None, {"status": "idle"}
        elif path == "/api/nodes" and self.can:
            answer = {"node_count": 2, "nodes": {"10": _graph_node(10, "org.example.imu", [100], [], uid_byte=1),
                                                 "11": _graph_node(11, "org.example.esc", [101], [], uid_byte=2)}}
        else:
            answer = {"/api/nodes": {"node_count": 0, "nodes": {}}, "/api/replay/status": {"active": False},
                      "/api/recordings": {"recordings": []}, "/api/rawlogs": {"active": None, "logs": []},
                      "/api/logs": {"logs": []}}.get(path, {})
        try:
            await route.fulfill(json=answer, headers={"Access-Control-Allow-Origin": "*"})
        except Exception:  # the page gave up on the request meanwhile
            pass

    def on_socket(self, ws):
        self.ws = ws
        ws.on_message(self._on_message)

    def _on_message(self, text):
        message = json.loads(text)
        if message.get("type") == "watch":
            self.watched.append(message["subject_ids"])


async def sidebar_open(page, server, saved_connected=False):
    """A freshly loaded page on the Nodes tab whose backend, socket too, is
    `server`, with a connected dashboard saved or not. The socket route is the
    Debug tests' (a page takes one), pointed at `server`."""
    suite_debug._debug_server = server
    if not suite_debug._debug_socket_routed:
        suite_debug._debug_socket_routed = True
        await page.route_web_socket(
            "**/ws**", lambda ws: suite_debug._debug_server and suite_debug._debug_server.on_socket(ws))
    await page.route("**/api/**", server.handle)
    await page.evaluate(f"""() => {{ state.dashboardConnected = {json.dumps(saved_connected)};
        state.activeView = 'nodes'; _writeSettingsNow(); }}""")
    await page.reload(wait_until="load")


async def sidebar_close(page, server):
    server.down = False
    await page.evaluate("state.preferredCanInterface = ''; disconnectAll()")  # no CAN pick left for the next test
    suite_debug._debug_server = None
    await page.unroute("**/api/**", server.handle)


async def resize_window(page, width, height):
    """Resize the window, and wait out the 0.2 s the sidebar's width takes to follow across 80rem."""
    await page.set_viewport_size({"width": width, "height": height})
    await page.wait_for_function("document.querySelector('nav.sidebar').getAnimations().length === 0", timeout=WAIT_MS)


async def wait_until(page, expression, seconds):
    """Whether `expression` became true in the page within `seconds`."""
    for _ in range(int(seconds * 5)):
        if await page.evaluate(expression):
            return True
        await page.wait_for_timeout(200)
    return False


@test("Sidebar: CAN Disconnect takes the bus's nodes and load curve with it, rather than leave them looking live")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        for load in (20, 35, 30):  # the backend's metrics, once a second
            server.ws.send(json.dumps({"type": "metrics", "bus_utilization": load}))
        await page.evaluate("getAllNodes()")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)
        shown = await page.evaluate("!el('busLoadSparkline').classList.contains('hidden')")
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)
        await page.wait_for_timeout(300)
        after = await page.evaluate("""() => ({rows: nodesTabulator.getRows().length,
            curve: !el('busLoadSparkline').classList.contains('hidden'),
            says: document.querySelector('#nodesTable .tabulator-placeholder')?.innerText || ''})""")
        assert shown and after["rows"] == 0 and not after["curve"] and "CAN bus not connected" in after["says"], \
            f"Connected, the curve was shown: {shown}; after CAN Disconnect: {after}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: errors on the bus show under the CAN status, its dot amber, until they stop")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        notice, dot = page.locator("#canBusNotice"), page.locator("#canSemaphore")
        before = (await notice.is_hidden(), await dot.get_attribute("class"))
        # A bus without its terminator, as the backend reports it.
        server.bus_errors = {"state": "ERROR-PASSIVE", "tx_errors": 0, "rx_errors": 128, "since_unix": time.time()}
        await page.evaluate("pollStatus()")  # the 5 s poll, without waiting for it
        await notice.wait_for(state="visible", timeout=WAIT_MS)
        text = " ".join((await notice.inner_text()).split())
        title, during = await notice.get_attribute("title"), await dot.get_attribute("class")
        server.bus_errors = None
        await page.evaluate("pollStatus()")
        await notice.wait_for(state="hidden", timeout=WAIT_MS)
        after = await dot.get_attribute("class")
        assert before == (True, "semaphore ok"), f"With no errors, notice hidden and the dot: {before}"
        assert text == "Bus errors · ERROR-PASSIVE Check 120 Ω termination and bitrate", f"Notice: {text}"
        assert "120 Ω at each end" in title and "TX 0, RX 128" in title, f"Its title: {title}"
        assert (during, after) == ("semaphore warn", "semaphore ok"), f"The dot with errors, then without: {during}, {after}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: a newer release the server found is a pill by the version, to its release page, and a toast once")
async def _(page):
    server = _SidebarServer()
    page_url = "https://github.com/OpenCyphal-Garage/cynitor/releases/tag/v9.1.0"
    toasts = "[...document.querySelectorAll('#toastContainer .toast')].map((t) => t.textContent).filter((t) => t.includes('9.1.0'))"
    overflow = """() => { const sb = document.querySelector('nav.sidebar');
        return [sb.scrollHeight - sb.clientHeight, sb.scrollWidth - sb.clientWidth]; }"""
    await sidebar_open(page, server)
    link = page.locator("#versionTag .version-update")
    pills = {}
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        none = await link.count()
        server.update = {"version": "9.1.0", "url": page_url}
        await page.evaluate("pollStatus()")
        await link.wait_for(state="visible", timeout=WAIT_MS)
        for width, height in ((1280, 800), (1600, 900)):  # the narrower sidebar, then the wider one
            await resize_window(page, width, height)
            pills[width] = (await link.inner_text(), await page.evaluate(overflow))
        found = (await link.get_attribute("href"), await link.get_attribute("target"), await link.get_attribute("aria-label"))
        if os.environ.get("CYNITOR_E2E_SHOTS"):  # a folder to see the footer in
            await page.locator(".sidebar-footer").screenshot(path=os.path.join(os.environ["CYNITOR_E2E_SHOTS"], "version-update.png"))
        await page.evaluate("pollStatus()")  # the next poll finds it again
        said = await page.evaluate(toasts)
        await page.evaluate("_writeSettingsNow()")
        await page.reload(wait_until="load")
        await link.wait_for(state="visible", timeout=5000)  # reconnected, and polled
        said_after_reload = await page.evaluate(toasts)
        server.update = {"version": "9.2.0", "url": "https://example.com/x"}  # only the project's own release pages
        await page.evaluate("pollStatus()")
        foreign = await link.count()
    finally:
        server.update = None
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.evaluate("state.update = null; renderVersionTag(); state.updateAnnounced = null; _writeSettingsNow()")
        await sidebar_close(page, server)
    assert none == 0, f"Before the server found one, a pill shows: {none}"
    assert pills == {1280: ("↑ v9.1.0", [0, 0]), 1600: ("↑ v9.1.0 available", [0, 0])}, \
        f"The pill, and the sidebar's overflow, in a narrow and a wide window: {pills}"
    assert found == (page_url, "_blank", "Cynitor 9.1.0 is out: open its release page"), f"Where it leads: {found}"
    assert said == ["Cynitor 9.1.0 is out: its link is under the sidebar"], f"Said once, on two polls: {said}"
    assert said_after_reload == [], f"Said again after a reload: {said_after_reload}"
    assert foreign == 0, "A release page off the project is linked"


@test("Sidebar: transmit errors alone say no other node acknowledges, not to check termination")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        # The node unplugged: Cynitor alone, its frames unacknowledged (audit E8-18).
        server.bus_errors = {"state": "ERROR-PASSIVE", "tx_errors": 128, "rx_errors": 0, "since_unix": time.time()}
        await page.evaluate("pollStatus()")
        hint = page.locator("#canBusNotice .can-bus-notice-hint")
        await hint.wait_for(state="visible", timeout=WAIT_MS)
        text = await hint.inner_text()
        assert text == "No other node acknowledges: check the bus is connected and a node is powered", f"Hint: {text}"
    finally:
        server.bus_errors = None
        await sidebar_close(page, server)


@test("Sidebar: a problem the backend reports while connected shows under the CAN form, and goes with it")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        server.last_error = "Another node uses Cynitor's own node-ID 127"
        await page.evaluate("pollStatus()")
        shown = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.last_error = None
        await page.evaluate("pollStatus()")
        gone = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.history_error = "disk I/O error"  # a full disk
        await page.evaluate("pollStatus()")
        unsaved = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.history_error = None
        assert (shown, gone, unsaved) == ("Another node uses Cynitor's own node-ID 127", None,
                                          "History and recordings are not being saved: disk I/O error"), \
            f"Shown: {shown!r}; then: {gone!r}; with the disk full: {unsaved!r}"
    finally:
        server.last_error = server.history_error = None
        await sidebar_close(page, server)


@test("Sidebar: a backend that stops answering is reconnected once it is back, its session kept meanwhile")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        server.down = True
        await page.evaluate("pollStatus()")  # the poll that finds it gone, without waiting the 5 s for it
        await page.wait_for_timeout(600)
        during = await page.evaluate("""() => ({button: el('connectDashboardBtn').textContent,
            saved: JSON.parse(localStorage.getItem(STORAGE_KEY)).dashboardConnected})""")
        server.down = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        assert back and during == {"button": "Disconnect", "saved": True}, \
            f"Back up, the dashboard reconnected within 6 s: {back}; while it was down: {during}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: a page opened while the backend is down connects once it answers")
async def _(page):
    server = _SidebarServer()
    server.down = True
    await sidebar_open(page, server, saved_connected=True)
    try:
        await page.wait_for_timeout(800)
        server.down = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert back, f"The backend answered again; 6 s later the dashboard is still not connected. Toasts: {toasts}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: when the backend serving the page is gone a while, the page comes back connected after its reload")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)

    async def page_server_down(route):  # the page's heartbeat asks its own server, with HEAD
        if route.request.method == "HEAD":
            await route.abort("connectionrefused")
        else:
            await route.fallback()
    routed = False
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        server.down = True
        await page.route(f"{BASE_URL}/**", page_server_down)
        routed = True
        shown = await wait_until(page, "!el('serverDownOverlay').classList.contains('hidden')", 26)
        server.down = False
        async with page.expect_event("load", timeout=8000):  # the heartbeat reloads it once its server answers
            await page.unroute(f"{BASE_URL}/**", page_server_down)
            routed = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        assert shown and back, f"The outage overlay showed: {shown}; after the reload, connected again: {back}"
    finally:
        if routed:
            await page.unroute(f"{BASE_URL}/**", page_server_down)
        await sidebar_close(page, server)


@test("Sidebar: the CAN interface picked last is picked again, its bitrate too, though settings were saved before connecting")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        # Saved from an earlier session: PEAK at 500 kbit/s.
        await page.evaluate("""() => { clearTimeout(_saveSettingsPending); _saveSettingsPending = null;
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
            saved.canInterface = 'pcan:PCAN_USBBUS1'; saved.canBitrates = {'pcan:PCAN_USBBUS1': 500000};
            localStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); }""")
        await page.reload(wait_until="load")
        for _ in range(2):  # a setting changed before connecting saves them all
            await page.locator("#themeToggle").click()
        await page.wait_for_timeout(400)
        await page.reload(wait_until="load")
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        picked = await page.evaluate("[el('interfacesSelect').value, el('canBitrateSelect').value]")
        assert picked == ["pcan:PCAN_USBBUS1", "500000"], f"Connected, the CAN list and bitrate read {picked}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: an adapter missing from the list for a moment is picked again once it is back")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "pcan:PCAN_USBBUS1")
        server.adapters = [SIDEBAR_VCAN]  # the USB adapter enumerating again
        await page.evaluate("loadInterfaces()")  # the list's 3-second refresh
        meanwhile = await page.evaluate("el('interfacesSelect').value")
        server.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK]
        await page.evaluate("loadInterfaces()")
        back = await page.evaluate("selectedCanTarget().interface")
        assert back == "pcan:PCAN_USBBUS1", \
            f"PEAK picked, gone for a refresh ({meanwhile} shown), then back: Connect would open {back}"
    finally:
        await sidebar_close(page, server)


SIDEBAR_CANABLE_SLCAN = {"interface": "slcan:COM5", "label": "CANable (slcan) COM5", "needs_bitrate": True,
                         "supports_fd": True, "data_bitrates": [2000000, 5000000]}


@test("Sidebar: an adapter with CAN FD data bitrate presets is offered only those, and loses a pick it cannot run")
async def _(page):
    server = _SidebarServer()
    server.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK, SIDEBAR_CANABLE_SLCAN]
    await sidebar_open(page, server)
    offered = "[...el('canDataBitrateSelect').options].filter((o) => !o.disabled).map((o) => o.value)"
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "pcan:PCAN_USBBUS1")
        peak = await page.evaluate(offered)
        await page.select_option("#interfacesSelect", "slcan:COM5")
        canable = await page.evaluate(offered)
        # Named under Other…, slcan is known by its interface.
        await page.select_option("#interfacesSelect", "__other__")
        await page.fill("#canSpecInput", "pcan:PCAN_USBBUS2")
        await page.select_option("#canDataBitrateSelect", "4000000")
        await page.fill("#canSpecInput", "slcan:COM7@115200")
        typed = await page.evaluate(offered)
        kept = await page.evaluate("el('canDataBitrateSelect').value")
    finally:
        await sidebar_close(page, server)
    assert peak == ["", "1000000", "2000000", "4000000", "5000000", "8000000"], f"PEAK is offered {peak}"
    assert canable == typed == ["", "2000000", "5000000"], \
        f"A CANable with slcan firmware is offered {canable}; one named slcan:COM7, {typed}"
    assert kept == "", f"4 Mbit/s, picked for a PEAK, is still picked for slcan: {kept!r}"


SIDEBAR_SIM = {"interface": "sim:classic", "label": "Simulated bus (no hardware)", "needs_bitrate": False,
               "supports_fd": False, "data_bitrates": None}


@test("Sidebar: the simulated bus connects without a bitrate, picked from the list or typed under Other…")
async def _(page):
    server = _SidebarServer()
    server.adapters = [SIDEBAR_VCAN, SIDEBAR_SIM]
    await sidebar_open(page, server)
    form = """() => ({bitrate: !el('canBitrateRow').classList.contains('hidden'),
        connect: !el('connectCanBtn').disabled})"""
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "sim:classic")
        listed = await page.evaluate(form)
        await page.select_option("#interfacesSelect", "__other__")
        await page.fill("#canSpecInput", "sim:fd")
        typed = await page.evaluate(form)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        connected = server.can
    finally:
        await sidebar_close(page, server)
    assert listed == typed == {"bitrate": False, "connect": True}, \
        f"The listed simulated bus: {listed}; one typed under Other…: {typed}"
    assert connected == "sim:fd", f"Connected to {connected!r}"


# What the CAN section says of its last failure, if anything (#canError shown).
SIDEBAR_CAN_ERROR = "el('canError') && !el('canError').classList.contains('hidden') ? el('canError').textContent : null"


@test("Sidebar: why a CAN connect failed, or the session ended, stays under the CAN form until the next try")
async def _(page):
    server = _SidebarServer()
    server.connect_error = "Failed to open can0: [Errno 19] No such device"
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)
        failed = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.connect_error = None
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        cleared = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.can, server.last_error = None, "Interface vcan0 no longer exists"  # ended by the watchdog
        await page.evaluate("pollStatus()")
        lost = await page.evaluate(SIDEBAR_CAN_ERROR)
        live = await page.get_attribute("#toastContainer", "aria-live")
        assert (failed, cleared, lost, live) == ("Connect failed: Failed to open can0: [Errno 19] No such device", None,
                                                 "Disconnected: Interface vcan0 no longer exists", "polite"), \
            f"Refused: {failed!r}; connected: {cleared!r}; ended by the backend: {lost!r}; toasts' aria-live: {live!r}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: screen readers hear what its fields are, and which Connect is the server's and which is CAN's")
async def _(page):
    connect = re.compile("connect", re.IGNORECASE)
    found = {"url": await page.get_by_role("textbox", name="Backend URL", exact=True).count(),
             "list": await page.get_by_role("combobox", name="CAN interface", exact=True).count(),
             "server": await page.get_by_role("group", name="Server").get_by_role("button", name=connect).count(),
             "can": await page.get_by_role("group", name="CAN").get_by_role("button", name=connect).count()}
    assert found == {"url": 1, "list": 1, "server": 1, "can": 1}, f"Found by their names: {found}"


@test("Sidebar: a backend that wants a token says so without calling itself unreachable; the prompt can be left, and keeps what is typed")
async def _(page):
    server = _SidebarServer()
    server.token = "s3cret"
    await sidebar_open(page, server)
    prompt = page.locator("#authModal")
    try:
        await page.locator("#connectDashboardBtn").click()
        await prompt.locator("#authModalInput").wait_for(state="visible", timeout=WAIT_MS)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        await page.keyboard.press("Escape")
        left = await prompt.is_hidden() and await page.evaluate("document.activeElement.id")
        await page.locator("#connectDashboardBtn").click(timeout=WAIT_MS)  # under a prompt still open, it cannot be
        await prompt.locator("#authModalInput").wait_for(state="visible", timeout=WAIT_MS)
        await page.wait_for_timeout(100)  # the field takes the focus
        stops = []
        for _ in range(4):
            await page.keyboard.press("Tab")
            stops.append(await page.evaluate("el('authModal').contains(document.activeElement)"))
        await prompt.locator("#authModalInput").fill("s3c")
        await page.evaluate("showAuthModal('Missing or invalid token')")  # what each refused request does
        kept = await prompt.locator("#authModalInput").input_value()
        await prompt.locator("#authModalInput").fill("s3cret")
        await prompt.locator("#authModalInput").press("Enter")
        connected = await wait_until(page, "state.dashboardConnected", 3)
        assert not toasts and left == "connectDashboardBtn" and all(stops) and kept == "s3c" and connected, \
            f"Toasts: {toasts}; Escape left it (focus back on): {left}; Tab stayed in it: {stops}; " \
            f"typed 's3c', then asked again: {kept!r}; the token saved connects: {connected}"
    finally:
        await page.evaluate("setAuthToken(''); hideAuthModal()")
        await sidebar_close(page, server)


@test("Sidebar: a CAN connect slower than other requests is waited for, its button saying Connecting…")
async def _(page):
    server = _SidebarServer()
    server.connect_delay = 16  # a first connect compiles the DSDL; other requests give up after 15 s
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_timeout(500)
        said = await page.locator("#connectCanBtn").text_content()
        connected = await wait_until(page, "state.canState === 'connected'", 18)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert said == "Connecting…" and connected and not toasts, \
            f"While connecting the button said {said!r}; connected after 16 s: {connected}; toasts: {toasts}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: with the server not connected, the CAN form is off, saying to connect the server first")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    can_form = """() => ({list: el('interfacesSelect').disabled, button: el('connectCanBtn').disabled,
        title: el('connectCanBtn').title})"""
    try:
        fresh = await page.evaluate(can_form)
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        connected = await page.evaluate(can_form)
        await page.locator("#connectDashboardBtn").click()  # Disconnect: the list stays, from the last session
        await page.wait_for_function("!state.dashboardConnected", timeout=WAIT_MS)
        left = await page.evaluate(can_form)
        off = {"list": True, "button": True, "title": "Connect to the server first"}
        assert fresh == off and left == off and connected == {"list": False, "button": False, "title": ""}, \
            f"The CAN form on a fresh page: {fresh}; connected: {connected}; after the server's Disconnect: {left}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: the outage overlay names the server that serves the page, and how to start that one")
async def _(page):
    origin = "http://cynitor.test:8080"  # the backend serving the page, as README's setup has it

    async def serve(route):
        file = WEBSITE_DIR / (urlparse(route.request.url).path.lstrip("/") or "index.html")
        await route.fulfill(path=file) if file.is_file() else await route.fulfill(status=404)
    await page.route(f"{origin}/**", serve)
    said = {}
    try:
        for url in (f"{origin}/", f"{BASE_URL}/"):
            await page.goto(url, wait_until="load")
            said[url] = " ".join(await page.evaluate("""[...el('serverDownOverlay').querySelectorAll('p')]
                .filter((p) => !p.classList.contains('hidden')).map((p) => p.textContent)"""))
    finally:
        await page.unroute(f"{origin}/**", serve)
        if not page.url.startswith(BASE_URL):
            await page.goto(BASE_URL, wait_until="load")
    backend, dev = said[f"{origin}/"], said[f"{BASE_URL}/"]
    dev_host = urlparse(BASE_URL).netloc
    assert "cynitor.test:8080" in backend and "http.server" not in backend and dev_host in dev, \
        f"Served by the backend, the overlay says: {backend!r}; served on {dev_host}: {dev!r}"


@test("Sidebar: it needs no scrollbar of its own, its version reads, and its logos show in both themes")
async def _(page):
    # A footer logo hung below it: on Linux and Windows a scrollbar showed at
    # every window size, wrapping the brand row, and below 80rem a second one.
    seen = await page.evaluate("""() => { const sb = document.querySelector('nav.sidebar');
        const html = document.documentElement, theme = html.getAttribute('data-theme');
        html.setAttribute('data-theme', 'dark');
        const hammer = getComputedStyle(document.querySelector('.cordicor-logo')).filter;
        if (theme) html.setAttribute('data-theme', theme); else html.removeAttribute('data-theme');
        const version = el('versionTag'), was = version.textContent;
        version.textContent = 'v0.12.10';  // as the backend serving the page has it
        const over = [sb.scrollHeight - sb.clientHeight, sb.scrollWidth - sb.clientWidth];
        version.textContent = was;
        return {over, darkHammer: hammer, version: parseFloat(getComputedStyle(version).fontSize)}; }""")
    assert seen["over"] == [0, 0] and "invert" in seen["darkHammer"] and seen["version"] >= 9, \
        f"Sidebar overflow (px, down and across): {seen['over']}; the hammer's filter in dark: " \
        f"{seen['darkHammer']!r}; the version's font size: {seen['version']} px"


@test("Sidebar: its name and the Dark mode switch keep to one line, in a narrow window too")
async def _(page):
    size = page.viewport_size
    seen = {}
    try:
        for width, height in ((1280, 800), (1600, 900)):  # the narrower sidebar, then the wider one
            await resize_window(page, width, height)
            seen[width] = await page.evaluate("""() => { const label = el('themeLabel');
                const sb = document.querySelector('nav.sidebar');
                return {label: label.innerText, switchName: el('themeToggle').getAttribute('aria-label'),
                        oneLine: label.getBoundingClientRect().height < 1.6 * parseFloat(getComputedStyle(label).fontSize),
                        over: sb.scrollWidth - sb.clientWidth}; }""")
    finally:
        await page.set_viewport_size(size)
    assert seen == {1280: {"label": "Dark", "switchName": "Dark mode", "oneLine": True, "over": 0},
                    1600: {"label": "Dark mode", "switchName": "Dark mode", "oneLine": True, "over": 0}}, \
        f"The brand row's label, the switch's name, one line, the sidebar's overflow: {seen}"


@test("Sidebar: in a narrow window, collapsed, it leaves no blank behind, and its toggle stays put under the pointer")
async def _(page):
    size = page.viewport_size
    toggle = page.locator("#sidebarCollapseBtn")
    box = """() => { const rect = (e) => e.getBoundingClientRect();
        return {sidebar: Math.round(rect(document.querySelector('nav.sidebar')).height),
                toggleTop: Math.round(rect(el('sidebarCollapseBtn')).top)}; }"""
    await page.set_viewport_size({"width": 900, "height": 700})
    try:
        await page.wait_for_timeout(300)
        await toggle.click()  # collapsed; the pointer stays on the toggle
        await page.wait_for_timeout(400)
        hovered = await page.evaluate(box)
        await page.mouse.move(450, 600)
        await page.wait_for_timeout(200)
        left = await page.evaluate(box)
    finally:
        await page.evaluate("scrollTo(0, 0)")
        if await page.evaluate("state.sidebarCollapsed"):
            await toggle.click()
        await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)
        await page.set_viewport_size(size)
    assert left == {"sidebar": 0, "toggleTop": 0}, f"Collapsed in a 900 px window, the sidebar's height and its toggle's top: {left}"
    assert hovered == left, f"Under the pointer the toggle moved: {hovered}, then {left} with the pointer away"


async def sidebar_connect_can(page):
    await page.locator("#connectCanBtn").click()
    await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)


async def sidebar_disconnect_can(page):
    await page.locator("#connectCanBtn").click()
    await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)


@test("Sidebar: Cynitor allocates node-IDs only once its box is ticked; Connect says so, and the choice is kept")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    row, box = page.locator("#canAllocatorRow"), page.locator("#canAllocatorToggle")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        offered = (await row.is_visible(), await box.is_checked(), await box.is_enabled())
        await sidebar_connect_can(page)
        unticked = (server.connect_body.get("allocator"), await row.is_hidden())
        await sidebar_disconnect_can(page)

        await box.check()
        await sidebar_connect_can(page)
        ticked = server.connect_body.get("allocator")
        running = (await row.is_visible(), await box.is_checked(), await box.is_disabled())
        server.allocator["allocated"] = 2
        await page.evaluate("pollStatus()")  # the 5 s poll, without waiting for it
        await page.wait_for_function("el('canAllocatorRow').title.includes('2 given')", timeout=WAIT_MS)
        await sidebar_disconnect_can(page)
        again = (await row.is_visible(), await box.is_checked(), await box.is_enabled())

        await page.evaluate("_writeSettingsNow()")
        await page.reload(wait_until="load")
        kept = (await page.evaluate("state.canAllocator"), await box.is_checked())
        assert offered == (True, False, True), f"Before connecting, the box shown, ticked, enabled: {offered}"
        assert unticked == (False, True), f"Unticked, Connect asked for the allocator, and connected the box is hidden: {unticked}"
        assert ticked is True, f"Ticked, Connect asked for the allocator: {ticked}"
        assert running == (True, True, True), f"Connected with it, the box shown, ticked, locked: {running}"
        assert again == (True, True, True), f"Disconnected again, the box shown, ticked, enabled: {again}"
        assert kept == (True, True), f"After a reload, the choice and the box: {kept}"
    finally:
        await page.evaluate("state.canAllocator = false; _writeSettingsNow()")  # off again for the tests after
        await sidebar_close(page, server)


@test("Sidebar: the CAN line says which node-ID Cynitor is, or that it only listens")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await sidebar_connect_can(page)
        await page.evaluate("pollStatus()")
        await page.wait_for_function("el('canUtilization').textContent.includes('as node 127')", timeout=WAIT_MS)
        await sidebar_disconnect_can(page)
        await page.locator("#canListenOnlyToggle").check()
        await sidebar_connect_can(page)
        await page.evaluate("pollStatus()")
        await page.wait_for_function("el('canUtilization').textContent.includes('listening only')", timeout=WAIT_MS)
    finally:
        await page.evaluate("state.canListenOnly = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: the CAN line keeps its traffic and load on one line, and what was lost and who Cynitor is each on a line under it")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    seen = {}
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await sidebar_connect_can(page)
        await page.evaluate("pollStatus()")
        await page.wait_for_function("el('canSelf').textContent === 'as node 127'", timeout=WAIT_MS)
        for width, height in ((1280, 800), (1600, 900)):  # the narrower sidebar, then the wider one
            await resize_window(page, width, height)
            seen[width] = await page.evaluate("""() => {
                state.droppedEvents = {scanner: 12, logger: 0, clients: 0};  // as /api/status gives a loss
                state.noCommands = true;
                updateSemaphores();
                return [...el('canUtilization').children].filter((e) => !e.classList.contains('hidden')).map((e) => {
                    const r = e.getBoundingClientRect();
                    return {text: e.textContent, top: r.top, classes: [...e.classList],
                            oneLine: r.height < 1.6 * parseFloat(getComputedStyle(e).fontSize)};
                });
            }""")
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.evaluate("state.droppedEvents = null; state.noCommands = false")
        await sidebar_close(page, server)
    for width, lines in seen.items():
        texts = [line["text"] for line in lines]
        assert len(lines) == 3 and "msg/s" in texts[0] and "% load" in texts[0] and "node" not in texts[0], \
            f"In a {width} px window, the traffic and its load on the first line, alone: {texts}"
        assert texts[1:] == ["12 dropped", "as node 127 · no commands"], \
            f"In a {width} px window, then what was lost, then who Cynitor is: {texts}"
        assert all(a["top"] < b["top"] for a, b in zip(lines, lines[1:])), \
            f"In a {width} px window, each on a line of its own: {lines}"
        assert all(line["oneLine"] for line in lines), f"In a {width} px window, a line wraps: {lines}"
        assert "load-warn" in lines[1]["classes"] and not any(c.startswith("load-") for c in lines[2]["classes"]), \
            f"In a {width} px window, what was lost in amber, who Cynitor is uncoloured: {lines}"


@test("Confirmations name the device: node-ID, name, the end of its unique-ID and the bus")
async def _(page):
    text = await page.evaluate("""() => {
        const saved = [state.latestNodesPayload, state.preferredCanInterface];
        state.latestNodesPayload = {nodes: {'21': {node_id: 21, name: 'org.example.esc',
                                                   unique_id: '0123456789abcdef0123456789abcdef'}}};
        state.preferredCanInterface = 'can0';
        const out = NODE_COMMANDS[0].question(21);
        [state.latestNodesPayload, state.preferredCanInterface] = saved;
        return out;
    }""")
    assert text == 'Restart node 21, "org.example.esc", unique-ID …89abcdef, on can0?', f"Question: {text}"


@test("Sidebar: Listen only connects sending nothing; while it runs the box says so, and the choice is kept")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    row, box = page.locator("#canListenOnlyRow"), page.locator("#canListenOnlyToggle")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        offered = (await row.is_visible(), await box.is_checked(), await box.is_enabled())
        await sidebar_connect_can(page)
        unticked = (server.connect_body.get("listen_only"), await row.is_hidden())
        await sidebar_disconnect_can(page)
        await box.check()
        await sidebar_connect_can(page)
        running = (server.connect_body.get("listen_only"), await row.is_visible(), await box.is_checked(),
                   await box.is_disabled(), "sends nothing" in (await row.get_attribute("title")))
        await sidebar_disconnect_can(page)
        await page.evaluate("_writeSettingsNow()")
        await page.reload(wait_until="load")
        kept = (await page.evaluate("state.canListenOnly"), await box.is_checked())
        assert offered == (True, False, True), f"Before connecting, the box shown, ticked, enabled: {offered}"
        assert unticked == (False, True), f"Unticked, Connect sent listen_only false and the box hides: {unticked}"
        assert running == (True, True, True, True, True), f"Ticked, connected listening only: {running}"
        assert kept == (True, True), f"After a reload, the choice and the box: {kept}"
    finally:
        await page.evaluate("state.canListenOnly = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: No commands connects a session that changes no node; the CAN line and the box say so")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    row, box = page.locator("#canNoCommandsRow"), page.locator("#canNoCommandsToggle")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        offered = (await row.is_visible(), await box.is_checked(), await box.is_enabled())
        await box.check()
        await sidebar_connect_can(page)
        await page.evaluate("pollStatus()")
        await page.wait_for_function("el('canUtilization').textContent.includes('no commands')", timeout=WAIT_MS)
        running = (server.connect_body.get("no_commands"), await row.is_visible(), await box.is_checked(),
                   await box.is_disabled(), "changes no node" in (await row.get_attribute("title")),
                   "as node 127" in await page.locator("#canUtilization").text_content())
        await sidebar_disconnect_can(page)
        await box.uncheck()
        await sidebar_connect_can(page)
        unticked = (server.connect_body.get("no_commands"), await row.is_hidden())
        assert offered == (True, False, True), f"Before connecting, the box shown, unticked, enabled: {offered}"
        assert running == (True, True, True, True, True, True), f"Ticked, connected taking no commands: {running}"
        assert unticked == (False, True), f"Unticked, Connect sent no_commands false and the box hides: {unticked}"
    finally:
        await page.evaluate("state.canNoCommands = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: an allocator that another on the bus has silenced names the node that allocates there, until CAN disconnects")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    notice = page.locator("#canAllocatorNotice")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#canAllocatorToggle").check()
        await sidebar_connect_can(page)
        answering = await notice.is_hidden()
        server.allocator = {"other_allocator": 42, "allocated": 1}  # node 42 answered a request too
        await page.evaluate("pollStatus()")
        await notice.wait_for(state="visible", timeout=WAIT_MS)
        text, title = await notice.inner_text(), await notice.get_attribute("title")
        await sidebar_disconnect_can(page)
        gone = await notice.is_hidden()
        assert answering, "While Cynitor's allocator answers, its notice shows"
        assert text == "Allocator silent · node 42 allocates here", f"Notice: {text}"
        assert "A bus has one allocator" in title and "Connect without the allocator" in title, f"Its title: {title}"
        assert gone, "After CAN Disconnect the notice still shows"
    finally:
        await page.evaluate("state.canAllocator = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: decoding on demand is switched in the Server section, and the switch follows what the server says")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    box = page.locator("#decodeOnDemandToggle")
    try:
        off = (await box.is_disabled(), await box.is_checked())
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        ready = (await box.is_enabled(), await box.is_checked())
        await box.check()
        await page.wait_for_function("state.decodeOnDemand === true", timeout=WAIT_MS)
        on = (server.decode_on_demand, await box.is_checked())
        server.decode_on_demand = False   # another dashboard switched it back
        await page.evaluate("pollStatus()")
        await page.wait_for_function("!el('decodeOnDemandToggle').checked", timeout=WAIT_MS)
        server.decoding_error = "CAN is busy"
        await box.click()
        toast = page.locator("#toastContainer .toast", has_text="Decoding not switched")
        await toast.wait_for(state="visible", timeout=WAIT_MS)
        refused = (server.decode_on_demand, await box.is_checked())
        title = await page.locator("#decodeOnDemandRow").get_attribute("title")
        assert off == (True, False), f"With no server, the switch is disabled, ticked: {off}"
        assert ready == (True, False), f"Connected to a server that decodes everything, enabled, ticked: {ready}"
        assert on == (True, True), f"Ticked, the server decodes on demand, and the box is ticked: {on}"
        assert refused == (False, False), f"Refused by the server, the server's mode and the box: {refused}"
        assert "rates still show" in title and "hold only what was decoded" in title, f"Its title: {title}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: the server is told which subjects' values are on screen, once, and again when they change")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        await sidebar_connect_can(page)
        await page.evaluate("getAllNodes()")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)

        async def told(expected):
            for _ in range(15):
                if server.watched and server.watched[-1] == expected:
                    return True
                await page.wait_for_timeout(100)
            return False

        nothing = await told([])                       # a new socket is told, though nothing is on screen
        await page.evaluate("setSelectedNode(10)")     # node 10 publishes subject 100
        node = await told([100])
        await page.evaluate("state.logSubjectIds.add(7)")   # a text subject in the log panel
        log = await told([7, 100])
        sent = len(server.watched)
        await page.wait_for_timeout(700)               # nothing changes: nothing more is said
        quiet = len(server.watched) == sent
        await page.evaluate("clearSelectedNode()")
        left = await told([7])
        assert nothing and node and log and left, \
            f"Told of nothing, node 10's subject, the log's too, then the log's alone: {server.watched}"
        assert quiet, f"With nothing changed, more was sent: {server.watched}"
    finally:
        await page.evaluate("state.logSubjectIds.delete(7); clearSelectedNode()")
        await sidebar_close(page, server)


@test("Sidebar: the server's CPU use shows under its data rate, amber when high and red when it is full")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    reading = page.locator("#serverCpu")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        before = await reading.is_hidden()
        seen = {}
        for cpu in (12.4, 75, 96.2):  # the backend's metrics, once a second
            server.ws.send(json.dumps({"type": "metrics", "bus_utilization": None, "server_cpu": cpu}))
            await page.wait_for_function(f"state.serverCpu === {cpu}", timeout=WAIT_MS)
            await page.evaluate("updateSemaphores()")  # the one-second timer, without waiting for it
            seen[cpu] = (await reading.inner_text(), sorted(set((await reading.get_attribute("class")).split())
                                                           & {"load-warn", "load-err"}))
        title = await reading.get_attribute("title")
        one_line = await page.evaluate("""() => { const r = el('serverCpu').getBoundingClientRect();
            return r.height < 1.6 * parseFloat(getComputedStyle(el('serverCpu')).fontSize); }""")
        await page.evaluate("disconnectAll()")
        gone = await reading.is_hidden()
        assert before, "Before the server said what it uses, the reading shows"
        assert seen[12.4] == ("CPU 12%", []), f"At 12.4 %: {seen[12.4]}"
        assert seen[75] == ("CPU 75%", ["load-warn"]), f"At 75 %: {seen[75]}"
        assert seen[96.2] == ("CPU 96%", ["load-err"]), f"At 96.2 %: {seen[96.2]}"
        assert "96.2% of one core" in title and "cannot keep up" in title, f"Its title: {title}"
        assert one_line, "The reading wraps"
        assert gone, "Disconnected from the server, its reading still shows"
    finally:
        await sidebar_close(page, server)


def _contrast(colour, behind):
    """The WCAG contrast of two opaque colours given as the browser writes them, rgb(r, g, b)."""
    def luminance(css):
        channels = [int(n) / 255 for n in re.findall(r"\d+", css)[:3]]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    light, dark = sorted((luminance(colour), luminance(behind)), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 2)


@test("Themes: muted text reads against the page and its panels, in the dark theme as in the light one")
async def _(page):
    seen = {}
    for _ in range(2):  # the theme in use, then the other, and back to the first
        theme = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
        muted = await page.evaluate(TOKEN_COLOUR, "muted")
        seen[theme] = {surface: _contrast(muted, await page.evaluate(TOKEN_COLOUR, surface))
                       for surface in ("bg", "surface", "surface-2")}
        await page.locator("#themeToggle").click()
    faint = {f"{theme}, on --{surface}": ratio for theme, ratios in seen.items()
             for surface, ratio in ratios.items() if ratio < 4.5}
    assert set(seen) == {"light", "dark"} and not faint, f"Muted text with less than 4.5:1 behind it: {faint}; all: {seen}"
