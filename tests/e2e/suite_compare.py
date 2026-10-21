"""Browser end-to-end tests: Compare tab. Run through test_landing_page.py, which imports every suite in order."""

from harness import WAIT_MS, answer_dialog, test


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

# What COMPARE_START feeds, every 100 ms: wait for a few points of each, not a fixed time.
COMPARE_FED = """() => ['1100:value', '1700:current', '1400:value']
    .every((key) => (state.subjectHistory.get(key) || []).length >= 3)"""


async def compare_fed(page):
    await page.wait_for_function(COMPARE_FED, timeout=WAIT_MS)


COMPARE_STOP = """() => {
    clearInterval(window.e2eCompareFeed);
    // A graph with markers or drawings asks before it goes: none has any left.
    for (const graph of state.compareGraphs) { graph.markers = []; graph.drawings = []; }
    for (const button of [...document.querySelectorAll('.compare-graph-delete')]) button.click();
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
    shown = "() => [state.compareGraphs.length, document.querySelectorAll('.compare-graph-card').length]"
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1700, "current"))
        await compare_import(page, "not a workspace")
        assert await page.evaluate(shown) == [2, 2], "A file that is not JSON changed the graphs"
        await compare_import(page, '{"graphs": []}')
        asked = await answer_dialog(page, accept=False)
        assert "replaces the graphs" in asked, f"Import asked: {asked!r}"
        assert await page.evaluate(shown) == [2, 2], "Answered no, Import still replaced the graphs"
        await compare_import(page, '{"graphs": [{"name": "imported", "series": [{"subjectId": 1100, "attribute": "value"}],'
                                   ' "drawings": [{"color": "#f00"}]}, 5], "saved": [null, {"name": "kept", "series": []}]}')
        await answer_dialog(page, accept=True)
        await page.wait_for_function(f"({shown})().join() === '1,1'", timeout=WAIT_MS)
        saved = await page.evaluate("state.savedCompareConfigs.map(c => c.name)")
        assert saved == ["kept"], f"Saved graphs from the file: {saved}"
        await page.locator(".compare-saved-btn").click()
        assert await page.locator(".compare-saved-name", has_text="kept").is_visible(), "The saved graphs do not open"
        await page.keyboard.press("Escape")
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: plots take the colours of the theme in use, loaded in it too")
async def _(page):
    theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    token = "getComputedStyle(document.documentElement).getPropertyValue('--plot-1').trim()"
    stroke = "document.querySelector('.compare-graph-card .compare-line')?.getAttribute('stroke')"
    started_in = await page.evaluate(theme)
    await page.evaluate(COMPARE_START)
    try:
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
    graphs = "state.compareGraphs.length"
    try:
        await page.wait_for_timeout(300)
        plain = await compare_graph(page, (1100, "value"))
        await plain.locator('[aria-label="Remove graph"]').click()
        asked_at_once = await page.locator("#confirmDialog").is_visible()
        assert not asked_at_once and await page.evaluate(graphs) == 0, "A graph with no markers is not removed at once"
        card = await compare_graph(page, (1100, "value"))
        await page.evaluate("""() => state.compareGraphs[0].markers.push(
            {t: Date.now() / 1000 - 5, label: 'motor on', note: '', color: '#0969da', lineStyle: 'dashed'})""")
        await card.locator('[aria-label="Remove graph"]').click()
        asked = await answer_dialog(page, accept=False)
        assert "1 marker" in asked, f"Removing a graph with a marker asks {asked!r}"
        assert await page.evaluate(graphs) == 1, "Answered no, the graph is removed all the same"
        await card.locator('[aria-label="Remove graph"]').click()
        await answer_dialog(page, accept=True)
        await page.wait_for_function(f"{graphs} === 0", timeout=WAIT_MS)
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph's editing rows fold away under Edit, and a new graph starts on them")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        await compare_fed(page)
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
        asked = await answer_dialog(page, accept=True)
        assert "1 drawing" in asked, f"Clear asks {asked!r}"
        await page.wait_for_function("state.compareGraphs[0].drawings.length === 0", timeout=WAIT_MS)
    finally:
        await page.evaluate(COMPARE_STOP)
