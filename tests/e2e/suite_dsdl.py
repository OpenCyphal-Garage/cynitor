"""Browser end-to-end tests: DSDL tab. Run through test_landing_page.py, which imports every suite in order."""

from urllib.parse import unquote
from urllib.parse import urlparse
import asyncio
import json
import re
import time

from harness import WAIT_MS, answer_dialog, test
from suite_graph import _graph_node


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

    async def source():
        return await page.locator("#dsdlEditorSource").input_value() if await page.locator("#dsdlEditorSource").count() else None

    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorClose").click()  # nothing written: closed without a word
        asked_at_once = await page.locator("#confirmDialog").is_visible()
        assert not asked_at_once and await source() is None, f"An empty editor: asked, or left {await source()!r}"
        draft = "uint8 lots_of_work\n@sealed\n"
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSource").fill(draft)
        await page.locator("#dsdlEditorClose").click()
        asked = [await answer_dialog(page, accept=False)]  # declined: the draft stays
        after_close = await source()
        await dsdl_new_type(page)
        asked.append(await answer_dialog(page, accept=False))  # a new type over the draft: declined too
        after_new = await source()
        await page.locator("#dsdlEditorClose").click()
        asked.append(await answer_dialog(page, accept=True))
        await page.wait_for_function("!document.getElementById('dsdlEditorSource')", timeout=WAIT_MS)
        assert len(asked) == 3 and all("unsaved changes" in said for said in asked) \
            and after_close == after_new == draft and await source() is None, \
            f"Asked {asked}; the draft after a declined close {after_close!r}, " \
            f"after a declined new type {after_new!r}; once agreed, the editor holds {await source()!r}"
    finally:
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
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        # Typed, closed, and another editor opened, within the preview's 250 ms. The keystroke is the
        # source's alone: its preview is due, and no draft is marked unsaved, which closing would ask about.
        await page.evaluate("""() => {
            const source = document.getElementById('dsdlEditorSource');
            source.value = 'uint8 stale_field\\n@sealed\\n';
            source.dispatchEvent(new Event('input', {bubbles: false}));
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
