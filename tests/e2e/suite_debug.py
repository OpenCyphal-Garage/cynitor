"""Browser end-to-end tests: Debug tab. Run through test_landing_page.py, which imports every suite in order."""

from urllib.parse import unquote
from urllib.parse import urlparse
import json
import re
import time

from harness import WAIT_MS, test


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
        self.link = VCAN_LINK       # what the transport diagnostics say of the controller (or a function giving it)
        self.dropped = 0            # frames the stream lost to a full queue

    def _transport(self):
        frames_in = 1000 + int(100 * (time.monotonic() - self.started))
        stats = {"in_frames": frames_in, "in_frames_cyphal": frames_in, "in_frames_cyphal_accepted": frames_in,
                 "in_frames_errored": 0, "in_frames_loopback": 0, "out_frames": 40, "out_frames_timeout": 0,
                 "out_frames_loopback": 0, "media_acceptance_filtering_efficiency": 1.0, "lost_loopback_frames": 0}
        return {"connected": True, "interface": "vcan0", "statistics": stats, "capture_active": self.capturing,
                "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": False},
                "link": self.link() if callable(self.link) else self.link, "bus_utilization": 12.0}

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
        n, errors = len(self.ring), sum(1 for frame in self.ring if frame.get("error"))
        return {"active": True, "captured": n, "rx": n, "tx": 0, "cyphal": n - errors, "foreign": 0, "errors": errors,
                "dropped": self.dropped}

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
            ws.send(json.dumps({"type": "capture_status", "active": False, "capturing": self.capturing,
                                "forwarding": False}))
            return
        if not self.can:
            ws.send(json.dumps({"type": "capture_status", "active": False, "capturing": False,
                                "forwarding": False, "error": "CAN not connected"}))
            return
        earlier = self.ring[-500:] if self.queued is None else []
        if self.queued is None:
            self.queued = []
        self.capturing = True
        ws.send(json.dumps({"type": "capture_status", "active": True, "capturing": True, "forwarding": True,
                            "stats": self._stats(), "frames": earlier}))
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
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
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
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        await page.locator("#debugDetails summary").click()  # folded beneath the health strip
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
            "src": src, "dst": None, "transfer_id": n % 32, "start": True, "end": True, "toggle": True, "kind": "msg",
            "port": 7509}


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


@test("Debug: capture changes nothing on the bus: Stop ends it, and nothing says the bus stays in capture mode")
async def _(page):
    server = _DebugServer()
    server.capturing = True  # another dashboard captures, which leaves this one as it was
    await debug_open(page, server)
    try:
        await page.wait_for_timeout(1200)  # the diagnostics have answered
        idle = await page.locator("#fmEmpty").inner_text()
        await debug_start_capture(page)
        label = await page.locator("#fmToggle").inner_text()
        await page.locator("#fmToggle").click()  # Stop
        await page.wait_for_function("!el('fmToggle').classList.contains('active')", timeout=WAIT_MS)
        stopped = await page.locator("#fmToggle").inner_text()
        monitor = await page.locator(".frame-monitor").inner_text()
        assert idle.startswith("Capture is off") and label == "Stop" and stopped == "Start capture" \
            and "until CAN disconnect" not in monitor, \
            f"Before Start the table says {idle!r}; capturing, the button reads {label!r}, stopped {stopped!r}; " \
            f"the monitor says: {monitor[:200]!r}"
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
                   "adapter_send_failures": 0, "adapter_error_frames": 17, "adapter_rx_overflows": 2,
                   "adapter_frames_read_late": 5}  # what the hub knows of an adapter
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        shown = await page.evaluate(DEBUG_STATS)
        late = await page.evaluate("""[...document.querySelectorAll('#debugBody .debug-stat')]
            .find((row) => row.textContent.includes('read late'))?.classList.contains('stat-warn')""")
        blank = [label for label, value in shown.items() if value == "—"]
        assert not blank and shown.get("Arbitration bitrate") == "500 kbit/s" \
            and (shown.get("Adapter frames in") or "").startswith("123456"), \
            f"Rows with nothing to say: {blank}; bitrate {shown.get('Arbitration bitrate')!r}; " \
            f"the adapter's frames in {shown.get('Adapter frames in')!r}"
        assert (shown.get("Adapter overflows"), shown.get("Adapter frames read late"), late) == ("2", "5", True), \
            f"The adapter's overflows and frames read late, the latter amber: {shown}"
    finally:
        await debug_close(page, server)


@test("Debug: the diagnostics update in place, and a failed update keeps the last values, marked stale")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        await page.locator("#debugDetails summary").click()  # folded beneath the health strip
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


@test("Debug: colour marks only the unusual, in the theme's own colours: own frames, lost frames, a bus in trouble")
async def _(page):
    server = _DebugServer()
    server.link = dict(VCAN_LINK, operstate="up", state="ERROR-ACTIVE")  # a controller doing fine
    server.dropped = 3
    await debug_open(page, server)
    try:
        await page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        await debug_start_capture(page)
        server.capture([_can_frame(1), dict(_can_frame(2), dir="tx")])
        server.flush()
        await page.wait_for_timeout(1300)  # and the diagnostics have answered
        seen = await page.evaluate("""() => {
            const token = (name) => { const probe = document.createElement('span');
                probe.style.color = `var(${name})`; document.body.append(probe);
                const color = getComputedStyle(probe).color; probe.remove(); return color; };
            const style = (selector) => getComputedStyle(document.querySelector(selector));
            const state = [...document.querySelectorAll('#debugBody .debug-stat')]
                .find(row => row.textContent.includes('CAN state')).querySelector('.debug-stat-val');
            const dropped = document.querySelector('#fmCounters .fm-dropped');
            return {
                'own frames: the dark theme\\'s ok-muted': style('.fm-dir.fm-tx').backgroundColor === token('--ok-muted'),
                'frames received: plain': style('.fm-dir.fm-rx').color === token('--muted'),
                'ERROR-ACTIVE: plain': getComputedStyle(state).color === token('--text-bright'),
                'dropped: warning': !!dropped && getComputedStyle(dropped).color === token('--warn'),
            }; }""")
        assert all(seen.values()), f"Each colour as it should be: {seen}"
    finally:
        await debug_close(page, server)


# Each figure of the health strip, label: [value, colour].
DEBUG_HEALTH = """Object.fromEntries([...document.querySelectorAll('#debugHealth .debug-tile')].map(tile => [
    tile.querySelector('.debug-tile-label').textContent, [tile.querySelector('.debug-tile-val').textContent,
    tile.classList.contains('debug-tile-error') ? 'error' : tile.classList.contains('debug-tile-warn') ? 'warn' : '']]))"""


@test("Debug: the bus's health comes first, in one strip, with the details folded beneath it")
async def _(page):
    server = _DebugServer()
    since = time.monotonic()
    server.link = lambda: dict(VCAN_LINK, operstate="up", state="ERROR-PASSIVE", bitrate=1000000, berr_tx=136,
                               berr_rx=5, bus_off=1, bus_errors=1200 + int(30 * (time.monotonic() - since)))
    await debug_open(page, server)
    try:
        await page.wait_for_timeout(2300)  # two answers: the rates are known
        health = await page.evaluate(DEBUG_HEALTH)
        folded = await page.evaluate("el('debugDetails') ? !el('debugDetails').open : 'no fold'")
        table_at = await page.evaluate("el('fmRows').closest('.frame-monitor').getBoundingClientRect().top / innerHeight")
        frames_in = (health.get("Frames in") or [""])[0]
        assert health.get("Controller") == ["ERROR-PASSIVE", "warn"] and health.get("Error counters") == ["136 / 5", "error"] \
            and health.get("Bus-off") == ["1", "error"] and (health.get("Bus errors") or ["", ""])[1] == "warn" \
            and re.fullmatch(r"(9\d|10\d|110)/s", frames_in) and folded is True and table_at < 0.4, \
            f"The strip shows {health}; the details folded: {folded}; the frame table starts {table_at:.0%} down the window"
    finally:
        await debug_close(page, server)


# Each row of the frame table, newest first, as {column heading: cell text}; a
# cell across several columns goes under the first.
DEBUG_CELLS = """(() => { const heads = [...document.querySelectorAll('#fmTable thead th')].map(th => th.textContent);
    return [...document.querySelectorAll('#fmRows tr')].map((row) => { let column = 0;
        return Object.fromEntries([...row.cells].map((cell) => {
            const head = heads[column]; column += cell.colSpan; return [head, cell.textContent.trim()]; })); }); })()"""


@test("Debug: the frame table writes a standard ID in three digits, and gives a Cyphal frame's priority, transfer-ID and tail bits")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        first = dict(_can_frame(1), priority="FAST", transfer_id=5, end=False)                # a transfer's first frame
        last = dict(_can_frame(2), priority="FAST", transfer_id=5, start=False, toggle=False)  # and its last
        standard = {"t": 1000.003, "ts": 1.76e9 + 0.003, "dir": "rx", "id": "0x00000123", "ext": False, "dlc": 2,
                    "data": "AA BB", "cyphal": False}
        server.capture([first, last, standard])
        server.flush()
        await page.wait_for_timeout(300)
        shown = [(row.get("CAN ID"), row.get("Prio"), row.get("TID"), row.get("Flags"))
                 for row in await page.evaluate(DEBUG_CELLS)]
        await page.locator("#fmFilter").fill("0x123")
        found = [row.get("CAN ID") for row in await page.evaluate(DEBUG_CELLS)]
        assert shown == [("0x123", "", "", ""), ("0x107D5502", "fast", "5", "-E-"), ("0x107D5501", "fast", "5", "S-T")] \
            and found == ["0x123"], \
            f"(CAN ID, Prio, TID, Flags) of each row, newest first: {shown}; filtered on 0x123: {found}"
    finally:
        await debug_close(page, server)


@test("Debug: the frame filter takes node:, port:, dir: and the like, and a leading - leaves frames out")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        server.capture([_can_frame(1, src=42), _can_frame(2, src=4),  # heartbeats from nodes 42 and 4
                        dict(_can_frame(3, src=12), kind="req", dst=42, port=430),
                        dict(_can_frame(4, src=42), kind="resp", dst=12, port=430, dir="tx"),
                        {"t": 1000.005, "ts": 1.76e9 + 0.005, "dir": "rx", "id": "0x00000123", "ext": False, "dlc": 2,
                         "data": "AA BB", "cyphal": False}])
        server.flush()
        await page.wait_for_timeout(300)
        found = {}
        for typed in ("node:42", "port:7509 -node:42", "dir:tx", "-foreign kind:req", "node:", "02 03 04"):
            await page.locator("#fmFilter").fill(typed)
            found[typed] = sorted(row["CAN ID"][-2:] for row in await page.evaluate(DEBUG_CELLS))
        assert found == {"node:42": ["01", "03", "04"], "port:7509 -node:42": ["02"], "dir:tx": ["04"],
                         "-foreign kind:req": ["03"], "node:": ["01", "02", "03", "04", "23"],
                         "02 03 04": ["01", "02", "03", "04"]}, \
            f"The rows each filter shows, by the end of their CAN ID: {found}"
    finally:
        await debug_close(page, server)


# The data of the frame rows fully in view (under the table's heading), top to bottom.
DEBUG_IN_VIEW = """(() => { const wrap = document.querySelector('.fm-table-wrap').getBoundingClientRect();
    const top = document.querySelector('.fm-table thead').getBoundingClientRect().bottom;
    return [...document.querySelectorAll('#fmRows tr')].filter((row) => { const box = row.getBoundingClientRect();
        return box.top >= top - 0.5 && box.bottom <= wrap.bottom + 0.5; }).map((row) => row.querySelector('.fm-data').textContent); })()"""


@test("Debug: every frame kept can be scrolled to, the page drawing only the rows in view, which hold still as frames come")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    numbered = lambda n: dict(_can_frame(n), dlc=2, data=f"{n >> 8:02X} {n & 255:02X}")  # its number in its data
    try:
        await debug_start_capture(page)
        for first in range(0, 5000, 1000):  # as many as the table keeps
            server.capture([numbered(n) for n in range(first, first + 1000)])
            server.flush()
        await page.wait_for_timeout(500)
        drawn = await page.evaluate("document.querySelectorAll('#fmRows tr').length")
        await page.evaluate("document.querySelector('.fm-table-wrap').scrollTop = 1e9")
        await page.wait_for_timeout(300)
        oldest = (await page.evaluate(DEBUG_IN_VIEW))[-1:]
        await page.evaluate("document.querySelector('.fm-table-wrap').scrollTop = 30000")  # among older frames
        await page.wait_for_timeout(300)
        reading = await page.evaluate(DEBUG_IN_VIEW)
        server.capture([numbered(n) for n in range(5000, 5100)])
        server.flush()
        await page.wait_for_timeout(300)
        still = await page.evaluate(DEBUG_IN_VIEW)
        assert drawn < 100 and oldest == ["00 00"] and reading and still == reading, \
            f"With 5,000 frames kept, the page holds {drawn} rows; scrolled to the bottom, the last row is {oldest}; " \
            f"frames coming while older ones are read move them from {reading[:2]}… to {still[:2]}…"
    finally:
        await debug_close(page, server)


@test("Debug: each frame says how long after the frame shown before it it came, to the µs: with a filter, a subject's period")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        # 1 ms apart (see _can_frame), node 42's every 10 ms.
        server.capture([dict(_can_frame(n, src=42 if n % 10 == 0 else 7), t=1000 + n / 1000 + 0.000003 * (n == 20))
                        for n in range(21)])
        server.flush()
        await page.wait_for_timeout(300)
        gaps = [row.get("Δ ms") for row in await page.evaluate(DEBUG_CELLS)]
        await page.locator("#fmFilter").fill("src:42")
        periods = [row.get("Δ ms") for row in await page.evaluate(DEBUG_CELLS)]
        assert gaps[:3] == ["1.003", "1.000", "1.000"] and gaps[-1] == "" and periods == ["10.003", "10.000", ""], \
            f"Δ ms, newest first: {gaps[:3]}…{gaps[-1:]}; with only node 42's frames: {periods}"
    finally:
        await debug_close(page, server)


# Each row of the By ID table as {column heading: cell text}, with whether its age is marked quiet.
DEBUG_ID_ROWS = """(() => { const heads = [...document.querySelectorAll('#fmIdTable thead th')].map(th => th.textContent);
    return [...document.querySelectorAll('#fmIdRows tr')].map(row => Object.assign(
        Object.fromEntries([...row.cells].map((cell, i) => [heads[i], cell.textContent.trim()])),
        {quiet: row.querySelector('.fm-quiet') !== null})); })()"""


@test("Debug: By ID gives each CAN ID a row, with its count, cycle and last data, and marks one gone quiet")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    heartbeat = lambda n, src: dict(_can_frame(n, src=src), id=f"0x107D55{src:02X}", t=1000 + n * 0.1,
                                    data=f"0{n % 10} 00 00 00 00 00 00 E5")  # every 100 ms
    try:
        await debug_start_capture(page)
        if not await page.locator('[data-fm-view="ids"]').count():
            raise AssertionError("The frame monitor has no By ID view")
        await page.locator('[data-fm-view="ids"]').click()
        server.capture([heartbeat(n, 42) for n in range(10)] + [heartbeat(n, 7) for n in range(3)])
        server.flush()
        await page.wait_for_timeout(700)  # By ID is redrawn twice a second at most
        rows = await page.evaluate(DEBUG_ID_ROWS)
        await page.wait_for_timeout(2200)
        server.capture([heartbeat(n, 42) for n in range(10, 12)])  # node 42 goes on; node 7 has stopped
        server.flush()
        await page.wait_for_timeout(700)
        later = {row["CAN ID"]: row["quiet"] for row in await page.evaluate(DEBUG_ID_ROWS)}
        await page.locator("#fmFilter").fill("src:7")
        filtered = [row["CAN ID"] for row in await page.evaluate(DEBUG_ID_ROWS)]
        shown = [(row["CAN ID"], row["Count"], row["Cycle ms"], row["Last data"]) for row in rows]
        assert shown == [("0x107D5507", "3", "100.0", "02 00 00 00 00 00 00 E5"),
                         ("0x107D552A", "10", "100.0", "09 00 00 00 00 00 00 E5")] \
            and later == {"0x107D5507": True, "0x107D552A": False} and filtered == ["0x107D5507"], \
            f"(CAN ID, Count, Cycle, Last data): {shown}; quiet, 3 s on: {later}; filtered on src:7: {filtered}"
    finally:
        await debug_close(page, server)


@test("Debug: error frames show in the trace, in red, saying what went wrong, and are counted")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    error = {"t": 1000.002, "ts": 1.76e9 + 0.002, "dir": "rx", "id": "0x000000A8", "ext": False, "dlc": 8,
             "data": "00 00 04 00 00 00 00 00", "cyphal": False,
             "error": ["protocol violation: stuff", "no ACK", "bus error"]}  # as serialize_message reports one
    try:
        await page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        await debug_start_capture(page)
        server.capture([_can_frame(1), error])
        server.flush()
        await page.wait_for_timeout(300)
        seen = await page.evaluate("""() => { const red = (node) => { const probe = document.createElement('span');
                probe.style.color = 'var(--error)'; document.body.append(probe);
                const same = getComputedStyle(node).color === getComputedStyle(probe).color; probe.remove(); return same; };
            const row = document.querySelector('#fmRows tr.fm-error-frame');
            const counted = document.querySelector('#fmCounters .fm-errors');
            return {id: row?.querySelector('.fm-id').textContent, says: row?.querySelector('.fm-error-says')?.textContent,
                    red: !!row?.querySelector('.fm-error-says') && red(row.querySelector('.fm-error-says')),
                    counted: counted && counted.textContent, countedRed: !!counted && red(counted)}; }""")
        found = {}
        for typed in ("-error", "ack"):
            await page.locator("#fmFilter").fill(typed)
            found[typed] = [row.get("CAN ID") for row in await page.evaluate(DEBUG_CELLS)]
        assert seen == {"id": "error", "says": "protocol violation: stuff · no ACK · bus error", "red": True,
                        "counted": "errors 1", "countedRed": True} \
            and found == {"-error": ["0x107D5501"], "ack": ["error"]}, \
            f"The error frame's row and the count: {seen}; filtered: {found}"
    finally:
        await debug_close(page, server)


@test("Debug: Save to a raw log opens the Record tab at its raw log, which keeps every frame")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        link = page.locator("#fmRawLog")
        if not await link.count():
            raise AssertionError("The frame monitor has no way to a raw log")
        await link.click()
        await page.wait_for_timeout(300)
        seen = await page.evaluate("""() => { const box = el('rawLogPanel')?.getBoundingClientRect();
            return {view: state.activeView, inView: !!box && box.top >= 0 && box.top < innerHeight}; }""")
        assert seen == {"view": "record", "inView": True}, f"After the link: {seen}"
    finally:
        await debug_close(page, server)
