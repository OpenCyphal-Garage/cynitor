"""Browser end-to-end tests: Log panel. Run through test_landing_page.py, which imports every suite in order."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse
import re
import time

from harness import BASE_URL, test
from suite_landing import INGEST_DIAGNOSTIC
from suite_record import set_log_panel
from suite_sidebar import _SidebarServer, sidebar_close, sidebar_open


# ── Log panel ──

class _LogServer(_SidebarServer):
    """A backend with a log of its own, which GET /api/logs serves as server/log_store.py does."""

    def __init__(self):
        super().__init__()
        self.records = []       # {timestamp, logger, level, message}, oldest first
        self.asked = []         # the query of every GET /api/logs
        self.logs_error = None  # what GET /api/logs fails with, if it does
        self._clock = time.time()

    def log(self, message, level="INFO"):
        self._clock += 0.001
        stamp = datetime.fromtimestamp(self._clock, tz=timezone.utc).isoformat(timespec="microseconds")
        self.records.append({"timestamp": stamp, "logger": "main", "level": level, "message": message})

    async def handle(self, route):
        url = urlparse(route.request.url)
        if url.path != "/api/logs" or self.down:
            await super().handle(route)
            return
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.asked.append(query)
        headers = {"Access-Control-Allow-Origin": "*"}
        if self.logs_error:
            await route.fulfill(json={"error": self.logs_error}, status=503, headers=headers)
            return
        logs = [r for r in self.records if r["timestamp"] >= query.get("since", "")][-int(query.get("limit", 200)):]
        await route.fulfill(json={"count": len(logs), "logs": logs}, headers=headers)


async def log_open(page, server):
    """A freshly loaded page connected to `server`, its log panel open and empty with the
    Server source on. The panel's own polling is off: a test asks with LOG_FETCH."""
    await page.evaluate("state.logShowServer = true; state.logPanelCollapsed = false")
    await sidebar_open(page, server)
    await page.evaluate("stopServerLogPoll()")
    await page.locator("#connectDashboardBtn").click()
    await page.wait_for_function("state.dashboardConnected", timeout=5000)


async def log_close(page, server):
    await page.evaluate("state.logShowServer = false; state.logPanelCollapsed = true")
    await sidebar_close(page, server)


LOG_FETCH = "_fetchServerLogsOnce()"
LOG_ROWS = "[...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim())"


@test("Log panel: the server's log is asked only for what came since the last record shown")
async def _(page):
    server = _LogServer()
    server.log("boot")
    await log_open(page, server)
    try:
        await page.evaluate(LOG_FETCH)
        server.log("one more")
        await page.evaluate(LOG_FETCH)
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, server)
    first, second = server.asked[0], server.asked[1]
    assert "since" not in first and second.get("since") == server.records[0]["timestamp"], \
        f"The two requests asked for: {first}, then {second}; the first record's time: {server.records[0]['timestamp']}"
    assert [row.split(" main ")[-1] for row in rows] == ["boot", "one more"], f"Rows: {rows}"


@test("Log panel: server records it could not get are marked as missing, where they are missing")
async def _(page):
    server = _LogServer()
    server.log("boot")
    await log_open(page, server)
    try:
        await page.evaluate(LOG_FETCH)
        for i in range(250):  # more than one request takes; the first 50 are the warnings
            server.log(f"burst {i}", "WARNING" if i < 50 else "INFO")
        await page.evaluate(LOG_FETCH)
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, server)
    assert len(rows) == 202 and "boot" in rows[0] and rows[2].endswith("burst 50"), \
        f"{len(rows)} rows, starting: {rows[:3]}"
    assert "missing" in rows[1] and "WARN" in rows[1], f"Between 'boot' and 'burst 50': {rows[1]}"


@test("Log panel: missing server records are marked also when it had none to show before, but a first backlog is not")
async def _(page):
    quiet = _LogServer()  # a log with nothing in it when first asked
    await log_open(page, quiet)
    try:
        await page.evaluate(LOG_FETCH)
        for i in range(250):
            quiet.log(f"burst {i}")
        await page.evaluate(LOG_FETCH)
        after_quiet = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, quiet)
    busy = _LogServer()  # a log longer than one request takes, when first asked
    for i in range(250):
        busy.log(f"earlier {i}")
    await log_open(page, busy)
    try:
        await page.evaluate(LOG_FETCH)
        backlog = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, busy)
    assert len(after_quiet) == 201 and "missing" in after_quiet[0] and after_quiet[1].endswith("burst 50"), \
        f"{len(after_quiet)} rows after a quiet start, starting: {after_quiet[:2]}"
    assert len(backlog) == 200 and not any("missing" in row for row in backlog), \
        f"{len(backlog)} rows of a first backlog, starting: {backlog[:2]}"


@test("Log panel: a server log that cannot be read marks its pill, saying why, until it can")
async def _(page):
    server = _LogServer()
    server.logs_error = "Log store is not configured"
    await log_open(page, server)
    pill = "({marked: el('logSrcServer').classList.contains('disconnected'), title: el('logSrcServer').title})"
    try:
        await page.evaluate(LOG_FETCH)
        failing = await page.evaluate(pill)
        server.logs_error = None
        await page.evaluate(LOG_FETCH)
        answering = await page.evaluate(pill)
    finally:
        await log_close(page, server)
    assert failing["marked"] and "Log store is not configured" in failing["title"], f"While it fails: {failing}"
    assert not answering["marked"] and "could not be read" not in answering["title"], f"Once it answers: {answering}"


@test("Log panel: every message it counts is in its list, so the filters find the oldest too")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            for (let i = 0; i < 1200; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1',
                publisher_node_id: 50, timestamp_unix: Date.now() / 1000 - 100 + i * 0.01,
                attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: `kept ${i}.`}]});
            const filter = (text) => { el('logFilterInput').value = text; el('logFilterInput').dispatchEvent(new Event('input')); };
            const oldest = state.logBuffer[0].text;
            filter(oldest);
            const found = el('logList').querySelectorAll('.log-row:not(.hidden)').length;
            filter('');
            return {kept: state.logBuffer.length, listed: el('logList').childElementCount, oldest, found,
                    counted: Number(el('logSrcCyphal').querySelector('.log-source-count').textContent)}; }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["kept"] == seen["listed"] == seen["counted"], \
        f"After 1200 messages it keeps {seen['kept']}, lists {seen['listed']} and counts {seen['counted']}"
    assert seen["found"] == 1, f"The filter finds {seen['found']} rows for the oldest message kept, {seen['oldest']!r}"


@test("Log panel: a replayed message is listed where it arrives, marked, with the time it was recorded")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [-30, 2, "live, before it"])  # by a server clock 30 s ahead
        # As server/replay.py sends it: the recording's time, two days old, and replay: true.
        await page.evaluate("""() => ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
            replay: true, timestamp_unix: new Date(2026, 9, 10, 9, 8, 7).getTime() / 1000,
            attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'replayed fault'}]})""")
        rows = await page.evaluate(LOG_ROWS)
        label, title = await page.evaluate("""() => { const tag = el('logList').lastElementChild.querySelector('.log-row-tag');
            return [tag?.textContent, tag?.title]; }""")
        await page.locator("#logFilterInput").fill("replay")
        filtered = await page.locator("#logList .log-row:not(.hidden)").count()
    finally:
        await page.locator("#logFilterInput").fill("")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 2 and "replayed fault" in rows[1], f"The replayed message should come last: {rows}"
    assert label == "replay" and "2026" in (title or "") and "9:08:07" in title, f"Its tag: {label!r}, saying {title!r}"
    assert filtered == 1, f"'replay' in the filter lists {filtered} rows"


@test("Log panel: shut, none of its controls takes the focus, and its toggle says whether it is open")
async def _(page):
    toggle = page.locator("#logPanelCollapseBtn")
    says = "[el('logPanelCollapseBtn').getAttribute('aria-expanded'), el('logPanelCollapseBtn').title]"
    was_open = await set_log_panel(page, False)
    try:
        await page.wait_for_timeout(400)  # the collapse plays for 0.2 s
        focusable = await page.evaluate("""() => [...document.querySelectorAll(
            '#logPanel button, #logPanel input, #logPanel select')]
            .filter((e) => { e.focus(); return document.activeElement === e; })
            .map((e) => e.id)""")
        shut = await page.evaluate(says)
        await set_log_panel(page, True)
        opened = await page.evaluate(says)
        await page.locator("#logFilterInput").focus()
        usable = await page.evaluate("document.activeElement.id")
    finally:
        await page.evaluate("document.activeElement.blur()")
        await set_log_panel(page, was_open)
    assert not focusable, f"Shut, these log panel controls still take the focus: {focusable}"
    assert shut[0] == "false" and "show" in shut[1].lower(), f"Shut, the toggle says: {shut}"
    assert opened[0] == "true" and "hide" in opened[1].lower(), f"Open, the toggle says: {opened}"
    assert usable == "logFilterInput", f"Open again, the filter box takes the focus: {usable}"
    assert await toggle.get_attribute("aria-controls") == "logPanel", "The toggle does not name the panel it controls"
    assert await toggle.locator("svg").get_attribute("aria-hidden") == "true", \
        "The toggle's arrow is read out as an image with no name"


@test("Log panel: a row keeps its node's name once the bus is gone and the list is drawn again")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    await page.evaluate("""() => { window.e2eNodesBefore = state.latestNodesPayload;
        state.latestNodesPayload = {node_count: 1, nodes: {50: {node_id: 50, name: 'demo.sensor', unique_id: []}}}; }""")
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 4, "supply voltage low"])
        named = (await page.evaluate(LOG_ROWS))[0]
        await page.evaluate("state.latestNodesPayload = {node_count: 0, nodes: {}}")  # as a CAN disconnect leaves it
        await set_log_panel(page, False)
        await set_log_panel(page, True)  # opening the panel draws its list again
        redrawn = (await page.evaluate(LOG_ROWS))[0]
    finally:
        await page.evaluate("state.latestNodesPayload = window.e2eNodesBefore; delete window.e2eNodesBefore")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert "50 demo.sensor supply voltage low" in named, f"The row as it arrived: {named}"
    assert redrawn == named, f"Drawn again after the disconnect: {redrawn!r}, was {named!r}"


@test("Log panel: a type of your own called Record is not taken for a diagnostic record")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        # myproject.Record.1.0 on subject 1234: its class is named as uavcan.diagnostic.Record's is.
        await page.evaluate("""() => ingestLogEvent({subject_id: 1234, message_type: 'Record_1_0', publisher_node_id: 51,
            timestamp_unix: Date.now() / 1000,
            attributes: [{attribute: 'index', value: 7}, {attribute: 'celsius', value: 21.5}]})""")
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 3, "a real diagnostic"])
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 1 and "NOTICE" in rows[0] and "a real diagnostic" in rows[0], f"Rows: {rows}"


@test("Log panel: markup in a message's node-ID or text is shown as text, never run")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const markup = '<img src="x" class="e2e-log-markup">';
            for (const [nodeId, text] of [[markup, 'its node-ID is markup'], [50, markup]]) {
                ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: nodeId,
                    timestamp_unix: Date.now() / 1000,
                    attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: text}]});
            }
            return {elements: el('logList').querySelectorAll('.e2e-log-markup').length,
                    rows: [...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim())}; }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["elements"] == 0, f"Markup from a message became {seen['elements']} element(s) in the log"
    assert len(seen["rows"]) == 2 and "its node-ID is markup" in seen["rows"][0] and "<img" in seen["rows"][1], \
        f"Rows: {seen['rows']}"


@test("Log panel: a text subject whose first string is empty is logged by the next one that says something")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        rows = await page.evaluate("""() => {
            state.logSubjectIds.add(1900);
            ingestLogEvent({subject_id: 1900, message_type: 'Status_1_0', publisher_node_id: 51,
                timestamp_unix: Date.now() / 1000,
                attributes: [{attribute: 'label', value: ''}, {attribute: 'detail', value: 'valve stuck'}]});
            state.logSubjectIds.delete(1900);
            return [...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim()); }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 1 and rows[0].endswith("s1900 valve stuck"), f"Rows: {rows}"


@test("Log panel: a width saved on a wide screen leaves the main area room in a smaller window, and is never too narrow to read")
async def _(page):
    size = page.viewport_size
    was_open = await set_log_panel(page, True)
    saved = await page.evaluate("state.logPanelWidth")
    measure = """async (width) => { state.logPanelWidth = width; applyLogPanelWidth(el('logPanel'));
        await new Promise((done) => setTimeout(done, 400));  // the width's transition
        const box = (e) => e.getBoundingClientRect(); const toggle = box(el('logPanelCollapseBtn'));
        const cut = [...document.querySelectorAll('.log-panel-header button, .log-panel-header input, .log-panel-header select')]
            .filter((e) => box(e).right > box(el('logPanel')).right).map((e) => e.id);
        return {panel: box(el('logPanel')).width, main: box(document.querySelector('.main-area')).width, cut,
                toggleOff: Math.abs(toggle.left + toggle.width / 2 - box(el('logPanel')).left)}; }"""
    try:
        await page.set_viewport_size({"width": 1100, "height": 700})
        wide = await page.evaluate(measure, 50)  # in rem: dragged to its widest on a large screen
        narrow = await page.evaluate(measure, 7.5)
    finally:
        await page.evaluate("(width) => { state.logPanelWidth = width; applyLogPanelWidth(el('logPanel')); }", saved)
        await page.set_viewport_size(size)
        await set_log_panel(page, was_open)
    assert wide["main"] >= 380 and wide["panel"] >= 380, \
        f"A panel saved 50rem wide, in a 1100 px window: panel {wide['panel']}, main area {wide['main']} px"
    assert narrow["panel"] >= 300 and not narrow["cut"], \
        f"A panel saved 7.5rem wide is {narrow['panel']} px wide; controls cut off at its edge: {narrow['cut']}"
    assert wide["toggleOff"] <= 8 and narrow["toggleOff"] <= 8, \
        f"The toggle is {wide['toggleOff']} and {narrow['toggleOff']} px from the panel's edge"


@test("Log panel: in a narrow window, where it sits under everything else, the page scrolls down to it")
async def _(page):
    size = page.viewport_size
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    box = """() => { const panel = el('logPanel').getBoundingClientRect();
        return {top: Math.round(panel.top), bottom: Math.round(panel.bottom), height: Math.round(panel.height),
                toggleTop: Math.round(el('logPanelCollapseBtn').getBoundingClientRect().top),
                toggleBottom: Math.round(el('logPanelCollapseBtn').getBoundingClientRect().bottom), window: innerHeight}; }"""
    try:
        for i in range(40):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"narrow window {i}"])
        await page.set_viewport_size({"width": 900, "height": 700})
        await page.wait_for_timeout(400)
        await page.mouse.move(450, 300)
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(300)
        opened = await page.evaluate(box)
        await set_log_panel(page, False)
        await page.wait_for_timeout(300)
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(300)
        shut = await page.evaluate(box)
    finally:
        await page.evaluate("scrollTo(0, 0); el('logClearBtn').click()")  # shut, its button takes no real click
        await page.set_viewport_size(size)
        await set_log_panel(page, was_open)
    assert 0 <= opened["top"] and opened["bottom"] <= opened["window"] and opened["height"] >= 100, \
        f"Scrolled to the end of a 900x700 window, the open panel is at {opened}"
    assert shut["height"] == 0 and shut["toggleBottom"] <= shut["window"], \
        f"Shut, the panel should take no room and leave its toggle in reach: {shut}"
    # The pointer is still on the toggle it clicked: it must not move from under it.
    assert shut["toggleTop"] == shut["bottom"], f"Under the pointer the toggle left its row: {shut}"


LOG_PILLS = """() => Object.fromEntries([['cyphal', 'logSrcCyphal'], ['server', 'logSrcServer']].map(([source, id]) =>
    [source, {marked: el(id).classList.contains('disconnected'), title: el(id).title}]))"""

# What the log panel is told of the connection, set as the dashboard sets it and
# shown through the call every change of connection makes (updateSemaphores).
LOG_CONNECTION = """(now) => { Object.assign(state, {dashboardConnected: false, canConnected: false, replayActive: false,
    logShowCyphal: true, logShowServer: false}, now); updateSemaphores(); }"""


@test("Log panel: a source's pill says what it is, and, turned on with nothing to come from, why")
async def _(page):
    try:
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True})
        no_backend = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True, "dashboardConnected": True})
        no_bus = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True, "dashboardConnected": True, "canConnected": True})
        all_on = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"dashboardConnected": True, "replayActive": True})
        replaying = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowCyphal": False})
        turned_off = await page.evaluate(LOG_PILLS)
    finally:
        await page.evaluate(LOG_CONNECTION, {})
    assert no_backend["cyphal"]["marked"] and no_backend["server"]["marked"] \
        and "Not connected to backend" in no_backend["cyphal"]["title"] \
        and "Not connected to backend" in no_backend["server"]["title"], f"With no backend: {no_backend}"
    assert no_bus["cyphal"]["marked"] and "CAN bus not connected" in no_bus["cyphal"]["title"] \
        and not no_bus["server"]["marked"], f"With a backend and no bus: {no_bus}"
    assert not all_on["cyphal"]["marked"] and not all_on["server"]["marked"] \
        and "uavcan.diagnostic.Record" in all_on["cyphal"]["title"] and "log" in all_on["server"]["title"], \
        f"With both connected, each should say what it is: {all_on}"
    assert not replaying["cyphal"]["marked"], f"A replay sends the nodes' messages with no bus: {replaying}"
    assert not turned_off["cyphal"]["marked"], f"A source turned off is not marked: {turned_off}"


@test("Log panel: empty, it says why: no backend, no bus, or what hides the messages it has")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    hint = "el('logEmpty').classList.contains('hidden') ? null : el('logEmpty').textContent"
    said = {}
    try:
        for state, now in (("no backend", {}), ("no bus", {"dashboardConnected": True}),
                           ("no bus, server on", {"dashboardConnected": True, "logShowServer": True}),
                           ("connected", {"dashboardConnected": True, "canConnected": True})):
            await page.evaluate(LOG_CONNECTION, now)
            said[state] = await page.evaluate(hint)
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 2, "an info"])
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 4, "a warning"])
        said["with messages"] = await page.evaluate(hint)
        await page.locator("#logSeveritySelect").select_option("5")
        said["under Min"] = await page.evaluate(hint)
        await page.locator("#logFilterInput").fill("Zzz")
        said["under Min and a filter"] = await page.evaluate(hint)
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        await page.locator("#logFilterInput").fill("")
        await page.locator("#logClearBtn").click()
        await page.evaluate(LOG_CONNECTION, {})
        await set_log_panel(page, was_open)
    assert "Not connected to backend" in said["no backend"], f"With no backend: {said['no backend']!r}"
    assert said["no bus"].startswith("CAN bus not connected"), f"With a backend and no bus: {said['no bus']!r}"
    assert said["no bus, server on"].startswith("Waiting for log messages") \
        and "CAN bus not connected" in said["no bus, server on"], \
        f"With no bus, the server's log turned on: {said['no bus, server on']!r}"
    assert said["connected"] == "Waiting for log messages…", f"Connected: {said['connected']!r}"
    assert said["with messages"] is None, f"With messages shown it should say nothing: {said['with messages']!r}"
    assert said["under Min"] == "None of the 2 messages kept match Min ERROR.", f"Under Min ERROR: {said['under Min']!r}"
    assert said["under Min and a filter"] == "None of the 2 messages kept match Min ERROR and the filter “Zzz”.", \
        f"Under Min ERROR and a filter: {said['under Min and a filter']!r}"


@test("Log panel: shut, its toggle counts the warnings and errors that came since, in the colour of the worst")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    badge = """() => { const badge = el('logUnread'); const box = badge?.getBoundingClientRect();
        return {shown: Boolean(badge) && !badge.classList.contains('hidden'), text: badge?.textContent,
                error: Boolean(badge?.classList.contains('log-unread--error')), title: el('logPanelCollapseBtn').title,
                inWindow: Boolean(box) && box.width > 0 && box.left >= 0 && box.right <= innerWidth}; }"""
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "an error seen while open"])
        while_open = await page.evaluate(badge)
        await set_log_panel(page, False)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "an info"])
        after_info = await page.evaluate(badge)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 4, "a warning"])
        after_warning = await page.evaluate(badge)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 6, "a critical"])
        after_critical = await page.evaluate(badge)
        await page.evaluate("state.logSeverityFloor = 7")  # Min ALERT: an error is not listed, so not counted
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "an error under Min ALERT"])
        await page.evaluate("state.logSeverityFloor = 0")
        after_hidden = await page.evaluate(badge)
        await set_log_panel(page, True)
        reopened = await page.evaluate(badge)
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert not while_open["shown"] and not after_info["shown"], \
        f"Nothing to count while open, or for an INFO: {while_open}, {after_info}"
    assert after_warning["shown"] and after_warning["text"] == "1" and not after_warning["error"] \
        and after_warning["inWindow"] and "1 warning or error" in after_warning["title"], f"After a warning: {after_warning}"
    assert after_critical["text"] == "2" and after_critical["error"], f"After a critical too: {after_critical}"
    assert after_hidden["text"] == "2", f"A message its Min hides should not count: {after_hidden}"
    assert not reopened["shown"] and "warning" not in reopened["title"], f"Opened, the count is gone: {reopened}"


@test("Log panel: scrolled up, it counts the messages that come below, and a click goes to them")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    cue = """() => { const cue = el('logNewBtn');
        return {shown: Boolean(cue) && !cue.classList.contains('hidden') && cue.getBoundingClientRect().height > 0,
                text: cue?.textContent, error: Boolean(cue?.classList.contains('log-new-btn--error')),
                atEnd: isAtBottom(el('logPanelBody'))}; }"""
    try:
        for i in range(80):  # more than the panel shows at once
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"line {i}"])
        following = await page.evaluate(cue)
        await page.evaluate("el('logPanelBody').scrollTop = 0")
        await page.wait_for_timeout(100)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "a new info"])
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "a new error"])
        reading = await page.evaluate(cue)
        kept_place = await page.evaluate("el('logPanelBody').scrollTop")
        if reading["shown"]:
            await page.locator("#logNewBtn").click(timeout=2000)
            await page.wait_for_timeout(200)
        clicked = await page.evaluate(cue)
        await page.evaluate("el('logPanelBody').scrollTop = 0")
        await page.wait_for_timeout(100)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "one more"])
        await page.evaluate("el('logPanelBody').scrollTop = el('logPanelBody').scrollHeight")
        await page.wait_for_timeout(200)
        scrolled_back = await page.evaluate(cue)
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert not following["shown"] and following["atEnd"], f"Following the end, there is nothing to count: {following}"
    assert reading["shown"] and reading["text"].startswith("2 new") and reading["error"] and kept_place == 0, \
        f"Reading the top while an info and an error came: {reading}, scrolled to {kept_place}"
    assert not clicked["shown"] and clicked["atEnd"], f"After a click on the count: {clicked}"
    assert not scrolled_back["shown"], f"After scrolling to the end oneself: {scrolled_back}"


@test("Log panel: Escape shuts the subject picker, and its button says whether it is open")
async def _(page):
    was_open = await set_log_panel(page, True)
    add = page.locator("#logAddSubjectBtn")
    seen = """() => ({shut: el('logSubjectPickerPopover').classList.contains('hidden'),
        expanded: el('logAddSubjectBtn').getAttribute('aria-expanded'), focus: document.activeElement.id})"""
    try:
        before = await page.evaluate(seen)
        await add.click()
        opened = await page.evaluate(seen)
        await page.keyboard.press("Escape")
        escaped = await page.evaluate(seen)
        await add.click()
        await page.mouse.click(5, 5)  # a click elsewhere shuts it too
        clicked_away = await page.evaluate(seen)
    finally:
        await page.evaluate("el('logSubjectPickerPopover').classList.add('hidden'); document.activeElement.blur()")
        await set_log_panel(page, was_open)
    assert before["shut"] and before["expanded"] == "false", f"Before it is opened: {before}"
    assert not opened["shut"] and opened["expanded"] == "true", f"Opened: {opened}"
    assert escaped == {"shut": True, "expanded": "false", "focus": "logAddSubjectBtn"}, f"After Escape: {escaped}"
    assert clicked_away["shut"] and clicked_away["expanded"] == "false", f"After a click elsewhere: {clicked_away}"


@test("Log panel: a message's time is to the millisecond, as the Debug tab's frames, with its date in the tooltip")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const second = new Date(2026, 9, 12, 11, 13, 40).getTime() / 1000;
            for (const [after, text] of [[0.25, 'a quarter past the second'], [0.5, 'half past it']]) {
                ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
                    timestamp_unix: second + after,
                    attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: text}]});
            }
            return [...el('logList').querySelectorAll('.log-row-time')].map((cell) =>
                ({time: cell.textContent, title: cell.title, cut: cell.scrollWidth > cell.clientWidth})); }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert [cell["time"] for cell in seen] == ["11:13:40.250", "11:13:40.500"], f"Times shown: {seen}"
    assert all("2026" in cell["title"] and not cell["cut"] for cell in seen), f"Tooltips, and whether a time is cut: {seen}"


# The WCAG contrast of an element's text against what is painted behind it, opacity counted.
CONTRAST_OF = """(selector) => {
    const parse = (colour) => { const n = colour.match(/[\\d.]+/g).map(Number); return {rgb: n.slice(0, 3), alpha: n.length > 3 ? n[3] : 1}; };
    const over = (top, alpha, under) => top.map((v, i) => v * alpha + under[i] * (1 - alpha));
    const luminance = (rgb) => { const [r, g, b] = rgb.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const element = document.querySelector(selector);
    const layers = [];
    let opacity = 1;
    for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        opacity *= Number(style.opacity);
        const fill = parse(style.backgroundColor);
        if (fill.alpha > 0 && !layers.some((layer) => layer.alpha === 1)) layers.push(fill);
    }
    let behind = [255, 255, 255];
    for (const layer of layers.reverse()) behind = over(layer.rgb, layer.alpha, behind);
    const text = parse(getComputedStyle(element).color);
    const [light, dark] = [luminance(over(text.rgb, text.alpha * opacity, behind)), luminance(behind)].sort((a, b) => b - a);
    return Math.round((light + 0.05) / (dark + 0.05) * 100) / 100; }"""


@test("Log panel: every severity's label reads against its row in both themes, and DEBUG takes no colour")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    levels = ["trace", "debug", "info", "notice", "warning", "error", "critical", "alert"]
    seen = {}
    try:
        for severity in range(8):
            await page.evaluate(INGEST_DIAGNOSTIC, [8 - severity, severity, f"severity {severity}"])
        for _ in range(2):  # the theme in use, then the other, and back to the first
            theme = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
            await page.mouse.move(400, 400)
            await page.wait_for_timeout(300)
            seen[theme] = {level: await page.evaluate(CONTRAST_OF, f".log-row-sev-{level} .log-row-sev") for level in levels}
            seen[theme]["debug is coloured"] = await page.evaluate("""() => getComputedStyle(
                document.querySelector('.log-row-sev-debug .log-row-sev')).color
                !== getComputedStyle(document.querySelector('.log-row-sev-debug .log-row-text')).color""")
            await page.locator("#themeToggle").click()
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert set(seen) == {"light", "dark"}, f"Themes checked: {list(seen)}"
    for theme, contrasts in seen.items():
        faint = {level: contrasts[level] for level in levels if contrasts[level] < 4.5}
        assert not faint, f"In the {theme} theme these labels have less than 4.5:1 against their row: {faint}"
        assert not contrasts["debug is coloured"], f"In the {theme} theme DEBUG's label has a colour of its own"


@test("Log panel: a message said again within seconds adds to its row's count, not a row")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const now = Date.now() / 1000;
            const say = (after, text, node = 50, severity = 4) => ingestLogEvent({subject_id: 8184,
                message_type: 'Record_1_1', publisher_node_id: node, timestamp_unix: now + after,
                attributes: [{attribute: 'severity', value: severity}, {attribute: 'text', value: text}]});
            for (let i = 0; i < 5; i++) say(i * 0.02, 'supply voltage low');  // at 50 Hz
            say(0.2, 'supply voltage low', 51);  // another node saying it: a row of its own
            say(0.3, 'supply voltage low');      // no longer in a row with the first five
            say(60, 'supply voltage low');       // a minute later: not a repeat
            const rows = [...el('logList').children];
            return {rows: rows.map((row) => row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, '')),
                    tag: rows[0].querySelector('.log-row-tag')?.title, kept: state.logBuffer.length,
                    counted: el('logSrcCyphal').querySelector('.log-source-count').textContent}; }""")
        await set_log_panel(page, False)
        unread = await page.evaluate("""() => { const later = Date.now() / 1000 + 120;
            for (let i = 0; i < 3; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
                timestamp_unix: later + i * 0.1,
                attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'I2C timeout'}]});
            return {unread: state.logUnreadCount, kept: state.logBuffer.length}; }""")
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["rows"] == ["WARN 50 ×5 supply voltage low", "WARN 51 supply voltage low",
                            "WARN 50 supply voltage low", "WARN 50 supply voltage low"], f"Rows: {seen['rows']}"
    assert "5 times" in (seen["tag"] or "") and seen["kept"] == 4 and seen["counted"] == "4", \
        f"The count's tooltip: {seen['tag']!r}; kept {seen['kept']}, counted {seen['counted']}"
    assert unread == {"unread": 3, "kept": 5}, f"Shut, an error said three times in a row: {unread}"


@test("Log panel: Save writes the messages listed, filters applied, to a file, and is off with none listed")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        off_when_empty = await page.evaluate("el('logSaveBtn')?.disabled")
        server_time = await page.evaluate("""() => {
            const earlier = new Date(Date.now() - 5000).toISOString();
            ingestServerEntry({timestamp: earlier, level: 'WARNING', logger: 'scanner_node', message: 'two types on subject 1620'});
            return earlier; }""")
        await page.evaluate(INGEST_DIAGNOSTIC, [3, 2, "temperature 21.4 C"])
        await page.evaluate(INGEST_DIAGNOSTIC, [2, 5, "I2C timeout on bus 1"])
        await page.evaluate(INGEST_DIAGNOSTIC, [1.5, 5, "I2C timeout on bus 1"])
        await page.locator("#logSrcServer").click()  # its rows are listed too
        await page.locator("#logSeveritySelect").select_option("4")  # Min WARNING leaves the INFO out
        assert off_when_empty is True and not await page.locator("#logSaveBtn").is_disabled(), \
            f"Save should be off with nothing listed ({off_when_empty}) and on with messages listed"
        async with page.expect_download() as download:
            await page.locator("#logSaveBtn").click()
        saved = await download.value
        name, text = saved.suggested_filename, Path(await saved.path()).read_text(encoding="utf-8")
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        if await page.evaluate("state.logShowServer"):
            await page.locator("#logSrcServer").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    lines = text.splitlines()
    assert re.fullmatch(r"cynitor-log-[\dTZ-]+\.log", name), f"The file's name: {name}"
    assert lines[0].startswith("#") and "UTC" in lines[0], f"Its first line should say what the columns are: {lines[0]!r}"
    rows = [line.split("\t") for line in lines[1:]]
    assert len(rows) == 2 and rows[0] == [server_time, "WARNING", "server scanner_node", "two types on subject 1620"], \
        f"The file's lines: {lines[1:]}"
    assert rows[1][1:] == ["ERROR", "node 50 ×2", "I2C timeout on bus 1"] and rows[1][0].endswith("Z"), \
        f"The folded error's line: {rows[1]}"


@test("Log panel: its width is kept across reloads, saved in rem, a px one from earlier versions too")
async def _(page):
    was_open = await set_log_panel(page, True)
    width = "Math.round(el('logPanel').getBoundingClientRect().width)"
    stored = "JSON.parse(localStorage.getItem(STORAGE_KEY))"
    try:
        start = await page.evaluate(width)
        handle = await page.locator("#logPanelResizeHandle").bounding_box()
        await page.mouse.move(handle["x"] + 2, 300)
        await page.mouse.down()
        await page.mouse.move(handle["x"] + 2 - 96, 300, steps=4)  # 6rem wider
        await page.mouse.up()
        await page.wait_for_timeout(300)
        dragged = await page.evaluate(width)
        await page.evaluate("_writeSettingsNow()")
        saved = await page.evaluate(f"({stored}).logPanelWidthRem")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)  # it opens over 0.2 s
        reloaded = await page.evaluate(width)
        # As an earlier version saved it: in px, under the old name.
        await page.evaluate(f"""() => {{ const settings = {stored}; delete settings.logPanelWidthRem;
            settings.logPanelWidth = 480; localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); }}""")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        from_px = [await page.evaluate(width), await page.evaluate("state.logPanelWidth")]
    finally:
        await page.evaluate("state.logPanelWidth = null; applyLogPanelWidth(el('logPanel')); _writeSettingsNow()")
        await set_log_panel(page, was_open)
    assert dragged == start + 96 and saved == dragged / 16, f"Dragged from {start} to {dragged} px; saved as {saved} rem"
    assert reloaded == dragged, f"After a reload it is {reloaded} px wide, was {dragged}"
    assert from_px == [480, 30], f"A width saved as 480 px by an earlier version gives: {from_px} (px shown, rem kept)"


LOG_VISIBLE = """[...el('logList').querySelectorAll('.log-row:not(.hidden)')].map((row) =>
    row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''))"""  # the rows shown, without their times


@test("Log panel: the Server source lists the server's records with their level and logger, and asks only while it is on")
async def _(page):
    server = _LogServer()
    server.log("WebSocket server started")
    server.log("two types on subject 1620", "WARNING")
    server.log("CAN connect failed", "ERROR")
    await log_open(page, server)
    pill = page.locator("#logSrcServer")
    try:
        await page.evaluate(LOG_FETCH)
        listed = await page.evaluate(LOG_VISIBLE)
        await page.locator("#logSeveritySelect").select_option("4")
        from_warning = await page.evaluate(LOG_VISIBLE)
        await page.locator("#logSeveritySelect").select_option("0")
        await pill.click()  # off: its rows go
        turned_off = await page.evaluate(LOG_VISIBLE)
        await pill.click()  # on: it asks at once, then every 2 s
        asked_before = len(server.asked)
        await page.wait_for_timeout(2400)
        asked_while_on = len(server.asked) - asked_before
        await pill.click()  # off again: it stops asking
        asked_before = len(server.asked)
        await page.wait_for_timeout(2400)
        asked_while_off = len(server.asked) - asked_before
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        await page.evaluate("stopServerLogPoll()")
        await log_close(page, server)
    assert listed == ["INFO main WebSocket server started", "WARN main two types on subject 1620",
                      "ERROR main CAN connect failed"], f"Rows: {listed}"
    assert from_warning == listed[1:], f"Under Min WARNING: {from_warning}"
    assert turned_off == [], f"With the source turned off these rows still show: {turned_off}"
    assert asked_while_on >= 1 and asked_while_off == 0, \
        f"Requests for the log in 2.4 s: {asked_while_on} while on, {asked_while_off} while off"


@test("Log panel: the subject picker offers the subjects that carry text, and one is logged while it is ticked")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    say = """(text) => ingestLogEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
        timestamp_unix: Date.now() / 1000, attributes: [{attribute: 'value', value: text}]})"""
    box = page.locator("#logSubjectPickerPopover .log-subject-checkbox")
    try:
        # A text subject, a subject of numbers, and a heartbeat, whose strings are enum names.
        await page.evaluate("""() => { for (const [subject_id, message_type, attribute, value] of [
                [1800, 'String_1_0', 'value', 'state: idle'], [1620, 'Scalar_1_0', 'kelvin', 294.5],
                [7509, 'Heartbeat_1_0', 'health', 'NOMINAL']]) {
            cacheEvent({subject_id, message_type, publisher_node_id: 50, attributes: [{attribute, value}]}); } }""")
        await page.locator("#logAddSubjectBtn").click()
        offered = await box.evaluate_all("(boxes) => boxes.map((box) => Number(box.dataset.subjectId))")
        await page.evaluate(say, "said before it is ticked")
        await box.first.check()
        await page.evaluate(say, "state: heating")
        picked = await page.evaluate("[...state.logSubjectIds]")
        await box.first.uncheck()
        await page.evaluate(say, "said after it is unticked")
        rows = await page.evaluate(LOG_VISIBLE)
    finally:
        await page.evaluate("""() => { closeLogPicker(); state.logSubjectIds.delete(1800); state.latestByNode.delete(50);
            for (const id of [1800, 1620, 7509]) state.latestBySubject.delete(id);
            state.subjectHistory.delete('1620:kelvin'); _writeSettingsNow(); }""")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert offered == [1800], f"Subjects offered: {offered}"
    assert picked == [1800] and rows == ["— 50 s1800 state: heating"], f"Picked: {picked}; rows: {rows}"


@test("Log panel: a source turned off hides its rows and keeps their count; Clear empties the list and the counts")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    seen = """() => ({rows: """ + LOG_VISIBLE + """, kept: state.logBuffer.length,
        counts: [...document.querySelectorAll('.log-source-pill')].map((pill) => pill.innerText.replace(/\\s+/g, ' ').trim()),
        pressed: el('logSrcCyphal').getAttribute('aria-pressed')})"""
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [2, 2, "from a node"])
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 4, "from a node, later"])
        await page.evaluate("ingestServerEntry({timestamp: new Date().toISOString(), level: 'INFO', logger: 'main', message: 'from the server'})")
        await page.locator("#logSrcServer").click()
        both = await page.evaluate(seen)
        await page.locator("#logSrcCyphal").click()
        cyphal_off = await page.evaluate(seen)
        await page.locator("#logSrcCyphal").click()
        back_on = await page.evaluate(seen)
        await page.locator("#logClearBtn").click()
        cleared = await page.evaluate(seen)
    finally:
        if await page.evaluate("state.logShowServer"):
            await page.locator("#logSrcServer").click()
        if not await page.evaluate("state.logShowCyphal"):
            await page.locator("#logSrcCyphal").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(both["rows"]) == 3 and both["counts"] == ["CYPHAL 2", "SERVER 1"], f"With both sources on: {both}"
    assert cyphal_off["rows"] == ["INFO main from the server"] and cyphal_off["counts"] == ["CYPHAL 2", "SERVER 1"] \
        and cyphal_off["pressed"] == "false", f"With Cyphal turned off: {cyphal_off}"
    assert back_on["rows"] == both["rows"] and back_on["pressed"] == "true", f"Turned on again: {back_on}"
    assert cleared["rows"] == [] and cleared["kept"] == 0 and cleared["counts"] == ["CYPHAL", "SERVER"], \
        f"After Clear: {cleared}"


@test("Log panel: Min, autoscroll, the sources, the subjects picked and whether it is open survive a reload; the filter does not")
async def _(page):
    was_open = await set_log_panel(page, True)
    try:
        await page.locator("#logSeveritySelect").select_option("5")
        await page.locator("#logAutoscrollBtn").click()  # off
        await page.locator("#logSrcServer").click()  # on
        await page.locator("#logSrcCyphal").click()  # off
        await page.locator("#logFilterInput").fill("i2c")
        await page.evaluate("state.logSubjectIds.add(1800); _writeSettingsNow()")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        kept = await page.evaluate("""() => ({min: el('logSeveritySelect').value,
            autoscroll: el('logAutoscrollBtn').getAttribute('aria-pressed'),
            server: el('logSrcServer').getAttribute('aria-pressed'), cyphal: el('logSrcCyphal').getAttribute('aria-pressed'),
            subjects: [...state.logSubjectIds], open: !el('logPanel').classList.contains('collapsed'),
            filter: el('logFilterInput').value + state.logTextFilter})""")
    finally:
        await page.evaluate("""() => { stopServerLogPoll();
            Object.assign(state, {logSeverityFloor: 0, logAutoscroll: true, logShowServer: false, logShowCyphal: true});
            state.logSubjectIds.delete(1800); _writeSettingsNow(); }""")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        await set_log_panel(page, was_open)
    assert kept == {"min": "5", "autoscroll": "false", "server": "true", "cyphal": "false", "subjects": [1800],
                    "open": True, "filter": ""}, f"After a reload: {kept}"


@test("Log panel: with autoscroll off a new message leaves the list where it is; turned on again, the list follows")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    place = "({top: Math.round(el('logPanelBody').scrollTop), atEnd: isAtBottom(el('logPanelBody'))})"
    try:
        for i in range(80):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"line {i}"])
        following = await page.evaluate(place)
        await page.locator("#logAutoscrollBtn").click()  # off
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "came with autoscroll off"])
        held = await page.evaluate(place)
        await page.locator("#logAutoscrollBtn").click()  # on: it goes to the end
        on_again = await page.evaluate(place)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "came with autoscroll on"])
        followed = await page.evaluate(place)
    finally:
        if not await page.evaluate("state.logAutoscroll"):
            await page.locator("#logAutoscrollBtn").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert following["atEnd"] and following["top"] > 0, f"80 messages in, the list should be at its end: {following}"
    assert held == {"top": following["top"], "atEnd": False}, f"With autoscroll off, after one more message: {held}"
    assert on_again["atEnd"] and followed["atEnd"] and followed["top"] > on_again["top"], \
        f"Turned on again: {on_again}; after one more message: {followed}"


# Notes, from before the page's own scripts run, every transition the log panel or
# its toggle starts. Stays registered for later loads, where it does nothing
# unless asked to through sessionStorage.
LOG_SLIDE_WATCH = """if (sessionStorage.getItem('e2eWatchLogSlide')) {
    window.e2eLogSlide = [];
    document.addEventListener('transitionrun', (event) => {
        if (['logPanel', 'logPanelCollapseBtn'].includes(event.target.id)) {
            window.e2eLogSlide.push(`${event.target.id} ${event.propertyName}`);
        }
    }, true);
}"""
_log_slide_watched = False


@test("Log panel: saved open, it is in place as the page loads rather than sliding in; its toggle still slides it")
async def _(page):
    global _log_slide_watched
    if not _log_slide_watched:
        _log_slide_watched = True
        await page.add_init_script(LOG_SLIDE_WATCH)
    was_open = await set_log_panel(page, True)
    try:
        await page.evaluate("_writeSettingsNow(); sessionStorage.setItem('e2eWatchLogSlide', '1')")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        at_load = await page.evaluate("window.e2eLogSlide.splice(0)")
        open_after_load = await page.evaluate("!el('logPanel').classList.contains('collapsed')")
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(400)
        on_toggle = await page.evaluate("window.e2eLogSlide.splice(0)")
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(400)
    finally:
        await page.evaluate("sessionStorage.removeItem('e2eWatchLogSlide')")
        await set_log_panel(page, was_open)
    assert open_after_load and at_load == [], f"Transitions started while the page loaded: {at_load}"
    assert "logPanel width" in on_toggle, f"Its toggle should still slide it: {on_toggle}"


@test("Log panel: a text subject that keeps saying new things does not push out what the nodes reported")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const now = Date.now() / 1000 - 100;
            ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50, timestamp_unix: now,
                attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'the one error'}]});
            state.logSubjectIds.add(1800);
            for (let i = 0; i < 1500; i++) {  // a text of its own every time: nothing to fold
                ingestLogEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
                    timestamp_unix: now + 1 + i * 0.02, attributes: [{attribute: 'value', value: `reading ${i}`}]});
            }
            state.logSubjectIds.delete(1800);
            const rows = [...el('logList').children];
            return {kept: state.logBuffer.length, listed: rows.length,
                    error: state.logBuffer.some((entry) => entry.text === 'the one error'),
                    first: rows[0].innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                    last: rows[rows.length - 1].innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                    inStep: state.logBuffer.map((entry) => String(entry.id)).join() === rows.map((row) => row.dataset.id).join()}; }""")
    finally:
        await page.evaluate("state.logSubjectIds.delete(1800)")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["error"] and seen["first"] == "ERROR 50 the one error", \
        f"After 1500 texts from a subject the node's error should still lead the list: {seen}"
    assert seen["kept"] == seen["listed"] == 1000 and seen["last"].endswith("reading 1499") and seen["inStep"], \
        f"The list should hold its 1000 newest in step with what is kept: {seen}"


@test("Log panel: shut, a screen reader is told of the first warning, and of the first error, not of every message")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    told = "el('logAnnounce')?.textContent"
    try:
        await set_log_panel(page, False)
        region = await page.evaluate("""() => { const region = el('logAnnounce'); if (!region) return null;
            window.e2eLogAnnounced = 0;
            new MutationObserver(() => { window.e2eLogAnnounced += 1; }).observe(region, {childList: true, characterData: true, subtree: true});
            const box = region.getBoundingClientRect();
            return {role: region.getAttribute('role'), drawn: box.width > 2 || box.height > 2, text: region.textContent}; }""")
        said = []
        for severity, text in ((2, "an info"), (4, "a warning"), (4, "another warning"), (5, "an error"), (6, "a critical")):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, severity, text])
            said.append(await page.evaluate(told))
        times = await page.evaluate("window.e2eLogAnnounced")
        await set_log_panel(page, True)
        opened = await page.evaluate(told)
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert region == {"role": "status", "drawn": False, "text": ""}, f"The region a screen reader hears: {region}"
    assert said[0] == "" and "warning" in said[1] and said[2] == said[1] and "error" in said[3] and said[4] == said[3], \
        f"What it held after an info, two warnings, an error and a critical: {said}"
    assert times == 2 and opened == "", f"It was told {times} times; with the panel open it holds {opened!r}"


@test("Log panel: the subject picker takes the focus when it opens, so its boxes are ticked by keyboard")
async def _(page):
    was_open = await set_log_panel(page, True)
    try:
        await page.evaluate("""() => cacheEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
            attributes: [{attribute: 'value', value: 'state: idle'}]})""")
        await page.locator("#logAddSubjectBtn").focus()
        await page.keyboard.press("Enter")
        focused = await page.evaluate("document.activeElement.className + ' ' + (document.activeElement.dataset.subjectId || '')")
        await page.keyboard.press("Space")
        ticked = await page.evaluate("[...state.logSubjectIds]")
        await page.keyboard.press("Escape")
        after = await page.evaluate("[document.activeElement.id, el('logSubjectPickerPopover').classList.contains('hidden')]")
    finally:
        await page.evaluate("""() => { closeLogPicker(); state.logSubjectIds.delete(1800); state.latestBySubject.delete(1800);
            state.latestByNode.delete(50); _writeSettingsNow(); document.activeElement.blur(); }""")
        await set_log_panel(page, was_open)
    assert focused == "log-subject-checkbox 1800", f"Opened with Enter, the focus is on: {focused!r}"
    assert ticked == [1800] and after == ["logAddSubjectBtn", True], f"Space ticked {ticked}; after Escape: {after}"


@test("Log panel: reading back in a full list, what is in view stays put as old messages go and late ones come in above")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    in_view = """() => { const top = el('logPanelBody').getBoundingClientRect().top;
        for (const row of el('logList').children) { const box = row.getBoundingClientRect();
            if (box.bottom > top + 1) return {row: row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                                              at: Math.round(box.top - top)}; } }"""
    ingest = """([count, text, ago]) => { const start = Date.now() / 1000 - ago;
        for (let i = 0; i < count; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
            timestamp_unix: start + i * 0.001, attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: `${text} ${i}`}]}); }"""
    try:
        await page.evaluate(ingest, [1000, "line", 100])  # the list is full
        await page.evaluate("el('logPanelBody').scrollTop = el('logPanelBody').scrollHeight / 2")
        await page.wait_for_timeout(100)
        reading = await page.evaluate(in_view)
        await page.evaluate(ingest, [50, "newer", 10])  # each pushes the oldest row out, above what is in view
        after_newer = await page.evaluate(in_view)
        await page.evaluate(ingest, [3, "late", 99.7])  # sorts in near the top of the list, above what is in view
        after_late = await page.evaluate(in_view)
        anchoring = await page.evaluate("getComputedStyle(el('logPanelBody')).overflowAnchor")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    def same_place(now):  # the same row, within the pixel a scroll position is rounded to
        return now["row"] == reading["row"] and abs(now["at"] - reading["at"]) <= 1

    assert reading["row"].startswith("INFO 50 line 5"), f"Scrolled to the middle of 1000 rows, the first in view: {reading}"
    assert same_place(after_newer), f"After 50 new messages the first row in view is {after_newer}, was {reading}"
    assert same_place(after_late), f"After 3 late messages sorted in above, the first row in view is {after_late}, was {reading}"
    assert anchoring == "none", f"The list keeps the place itself, so the browser must not as well: overflow-anchor is {anchoring}"


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
