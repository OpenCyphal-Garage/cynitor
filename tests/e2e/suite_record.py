"""Browser end-to-end tests: Record tab. Run through test_landing_page.py, which imports every suite in order."""

from urllib.parse import unquote
from urllib.parse import urlparse
import asyncio
import json
import re
import time

from harness import BASE_URL, WAIT_MS, answer_dialog, test


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


@test("Record: a service says it records all its calls on the bus")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_SERVICE)
    try:
        said = "all its calls on the bus"
        picked = await page.evaluate("_subjectsPicker.getRow('service-384').getCell('label').getElement().title")
        selected = await page.evaluate("_selectionPicker.getRow('service-384').getData().label")
        heading = await page.evaluate("el('recSubjectsPicker').previousElementSibling.textContent")
        assert said in picked and said in selected and said in heading, \
            f"Service 384 reads {picked!r} hovered in the picker, {selected!r} in the selection, " \
            f"and the picker's heading {heading!r}"
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


@test("Record: Rename asks for the name, and Delete asks first, in the dashboard's own dialog")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    card, field = page.locator('.record-card[data-id="1"]'), page.locator("#confirmDialogInput")

    async def from_menu(action):
        await card.locator("summary").click()
        await card.locator(f'.record-card-menu [data-action="{action}"]').click()

    try:
        await from_menu("rename")
        await field.wait_for(state="visible", timeout=WAIT_MS)
        offered = await page.evaluate("""[el('confirmDialogTitle').textContent, el('confirmDialogInput').value,
            document.activeElement.id, el('confirmDialogInput').selectionEnd]""")
        await page.keyboard.press("Escape")  # the name kept as it was
        await page.locator("#confirmDialog").wait_for(state="hidden", timeout=WAIT_MS)
        escaped = server.count("PATCH", "/api/recordings/1")
        await from_menu("rename")
        await field.wait_for(state="visible", timeout=WAIT_MS)
        await page.keyboard.type("motor run")  # over the name, which is selected
        await page.keyboard.press("Enter")
        await page.wait_for_function("state.recordings.some((r) => r.name === 'motor run')", timeout=WAIT_MS)
        renamed = [body for method, path, body in server.requests if (method, path) == ("PATCH", "/api/recordings/1")]
        await from_menu("delete")
        declined = await answer_dialog(page, accept=False)
        kept = server.count("DELETE", "/api/recordings/1")
        await from_menu("delete")
        red = await page.evaluate("el('confirmDialogOk').classList.contains('confirm-dialog-danger')")
        await answer_dialog(page, accept=True)
        await page.wait_for_function("!state.recordings.length", timeout=WAIT_MS)
    finally:
        await record_close(page, server)
    assert offered == ["Rename recording", "boot sequence", "confirmDialogInput", len("boot sequence")], \
        f"Rename offered (title, name, focus, selection's end) {offered}"
    assert escaped == 0, "Esc renamed the recording"
    assert renamed == [{"name": "motor run"}], f"Renamed with {renamed}"
    assert 'Delete "motor run"?' in declined and kept == 0 and red, \
        f"Delete asked {declined!r}; declined, it deleted {kept} times; its button red: {red}"


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
