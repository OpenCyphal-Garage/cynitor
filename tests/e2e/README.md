# End-to-End Tests

Playwright tests for the Cynitor frontend. These run against the static site
only and need no backend: the landing page's assertions describe its
disconnected state, and the Graph tab's tests feed it nodes and messages
through the same paths live data takes.

Backend unit tests live separately in `server/tests/` and run under pytest.

## Quick Start

```bash
pip install -r tests/e2e/requirements.txt
playwright install chromium

cd tests/e2e && python3 test_landing_page.py
```

The script serves `website/` on port 5500 itself and shuts that server down
afterwards. If something already answers on 5500, such as your own dev server,
it uses that instead. CI runs the identical command.

It exits non-zero if any assertion fails, so it works as a CI gate, and writes
a `result.md` summary table next to itself.

## Adding a test

Decorate an async function that takes the shared `page`:

```python
@test("Connect button shows 'Connect'")
async def _(page):
    text = await page.locator("#connectDashboardBtn").text_content()
    assert "connect" in text.lower(), f"Expected 'Connect', got '{text}'"
```

Tests run in registration order on one page; the first one navigates.

## Tests

| File | Covers |
|------|--------|
| `test_landing_page.py` | Landing page: layout, semaphores, nodes table, theme toggle, sidebar collapse, accessibility labels. Graph tab: silent subjects, per-publisher edge rates, devices that lost their node-ID, colour only for the unusual, ID-first subject labels, click vs. drag, "Nodes only" view, opt-in traffic animation and reduced motion, Fit, resize, status strip, Open in Nodes, service-call edges, recent restarts, docked inspector, legend popover, layered layout, edge tooltips, search-to-zoom, keyboard selection, SVG export, Cyphal v1.1 nodes. Nodes and Subjects tables: status strips that count and pick out what needs a look, the Subjects kind toggle, rows by keyboard, compact rows, the Nodes tab's plot controls, VSSC, short port lists, Subjects' age and bytes per second, silent subjects lose their rate (table, card, total), card rates follow the live rate, a subscriber's card shows the subject's total, severity sort with ghost rows kept last, re-sort as values change but not under the pointer, whole-ID filters, the selected row's colour, ghost-row aliases and confirmed forgetting, type names read one way, the Clients tab when the bus is down, Subjects refreshes with nothing arriving, scroll kept across tab switches. Plots: a node's card plots its own messages, Subjects plots each publisher apart, a vector plots per element, Fill Rate keeps up once a field's history is full, Compare opens what a node's plot shows in a new Compare graph, which gets an id of its own even before the Compare tab has opened. Compare: one publisher at a time for a subject several nodes publish; graphs that do not fit keep their height; a graph resumed after Pause All moves on, and Pause All's label follows the graphs; a graph paused by a click on its plot says so on its own pause button, to screen readers too; a click pauses a plot only with the graph's Click pauses on, and that is kept; a paused graph keeps what it showed, in a replay too; a series keeps to the node it was added from; a series with nothing to plot stays in the legend, and colours stay put; a graph says why it shows nothing; saved graphs and clones keep the whole graph, and untitled ones keep apart; Import checks the file first and asks before it replaces the graphs; plots take the colours of the theme in use, loaded in it too; the wheel scrolls past the graphs, Ctrl+wheel zooms one; the y-axis fits what is in view, thresholds included; the tooltip keeps a value's digits, and has none for a series gone quiet; subject 0 is compared like any other; a threshold's pill hides its line, and its style stays a line; with Fill Rate on, a cleared history leaves no old line; series picked, shown, removed and saved graphs opened by keyboard; the marker form saves on Enter and closes on Escape; graphs made, dragged and removed leave no listeners behind; a line draws what is in view, at most two points a pixel, keeping spikes; a graph out of view is not drawn and catches up in view; removing a graph with markers or drawings asks first; a graph's editing rows fold away under Edit, and a new graph starts on them; the legend lists each series' last, lowest and highest value in view, lined up; series are found by any part of their name, and ticked on and off in one list; a plot carries its graph's name; with no graph, the tab says how to start; everyday buttons are plain; a strip counts the graphs, and picks out quiet series and paused graphs; a window longer than the history keeps says so; a graph's parts show and hide by class, not inline style, and a dragged plot shows a grabbing hand; thresholds, drawings and markers left at their default colour take the theme's; each graph clips its lines to its own plot, not to the first graph's; drawings and markers scrolled past the y-axis are cut there, not drawn over it; a series' and a derived series' colours are picked from the legend; a graph lists its markers and drawings, shows a marker gone off screen, and removes them; a redraw writes lines in whole tenths of a pixel, and filters a publisher's points once; Min/Max follows the lowest and highest of the last samples, and an Add says what it is missing; synced graphs pause, take a time window and zoom together, and a graph that joins takes their view; synced graphs show each other's markers, and a graph that leaves no longer does; a synced graph above another synced graph leaves its time labels to it, and draws taller; a collapsed graph is its plot, a line of names and, at its side, its pause, window and Sync |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CYNITOR_E2E_URL` | `http://localhost:5500` | Frontend base URL. When set, the script never starts a server. |

## Notes

`result.md` is generated output, not source. Add `tests/e2e/result.md` to
`.gitignore` so it does not churn on every run.

When the UI changes, these assertions go stale silently unless CI runs them;
several had already rotted before this suite was automated.
