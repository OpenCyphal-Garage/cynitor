// Compare view — multiple independent graphs, each with its own series,
// controls and animation: their cards, editing rows, derived series and
// drawing. The plot itself (axes, legend, tooltip, zoom, markers) is plot.js's.

let _compareGraphIdCounter = 0;
// An id no graph has: past every graph's, wherever the new one is made (a
// Nodes or Subjects plot makes one before the Compare tab may have opened).
const _nextGraphId = () => {
  for (const g of state.compareGraphs) {
    const m = g.id && g.id.match(/^cg_(\d+)$/);
    if (m) _compareGraphIdCounter = Math.max(_compareGraphIdCounter, Number(m[1]));
  }
  return `cg_${++_compareGraphIdCounter}`;
};

// A graph's settings, whole and copied: what a saved graph, the workspace
// file, the dashboard's settings and a clone keep of it. Not its live state
// (pause, zoom, hidden series).
const compareGraphConfig = (g) => ({
  name: g.name,
  series: g.series.map((s) => ({ ...s })),
  thresholds: g.thresholds.map((t) => ({ ...t })),
  derivedSeries: g.derivedSeries.map((d) => ({ ...d })),
  markers: g.markers.map((m) => ({ ...m })),
  drawings: g.drawings.map((d) => ({ ...d, points: d.points.map((p) => ({ ...p })) })),
  timeWindow: g.timeWindow,
  smooth: g.smooth,
  stroke: g.stroke,
  disconnectPoints: g.disconnectPoints,
  grid: g.grid,
  clickPauses: g.clickPauses,
  sync: g.sync,
  collapsed: g.collapsed,
});

// A graph's settings as read from storage or a file: what is valid of them,
// defaults for the rest; null when it is no graph at all.
const sanitizeCompareGraph = (g) => {
  if (!g || typeof g !== 'object' || !Array.isArray(g.series)) return null;
  const list = (items, valid) => (Array.isArray(items) ? items.filter(valid) : []);
  return {
    name: typeof g.name === 'string' ? g.name : '',
    series: g.series.filter((s) => Number.isInteger(s?.subjectId) && typeof s?.attribute === 'string'),
    thresholds: list(g.thresholds, (t) => typeof t?.value === 'number'),
    derivedSeries: list(g.derivedSeries, (d) => d?.id && d?.type && d?.sourceA),
    markers: list(g.markers, (m) => typeof m?.t === 'number'),
    drawings: list(g.drawings, (d) => Array.isArray(d?.points) && d.points.length >= 2),
    timeWindow: typeof g.timeWindow === 'number' && g.timeWindow >= 0 ? g.timeWindow : 60,
    smooth: typeof g.smooth === 'number' && g.smooth >= 0 ? g.smooth : 0,
    stroke: typeof g.stroke === 'number' && g.stroke > 0 ? g.stroke : 1.5,
    disconnectPoints: g.disconnectPoints === true,
    grid: g.grid === true,
    clickPauses: g.clickPauses === true,
    sync: g.sync === true,
    collapsed: g.collapsed === true,
  };
};

// A graph from settings (an empty one by default), live and not paused.
const newCompareGraph = (config = { series: [] }, id = _nextGraphId()) => ({
  ...compareGraphConfig(sanitizeCompareGraph(config) || sanitizeCompareGraph({ series: [] })),
  id,
  paused: false,
  pausedAt: null,
  _fingerprint: '',
  _hidden: new Set(),
  _zoom: 1,
  _panOffset: 0,
});

// A name no saved graph has yet: "Untitled", then "Untitled 2", ...
const _freeSavedName = (base) => {
  const taken = new Set(state.savedCompareConfigs.map((c) => c.name));
  let name = base;
  for (let n = 2; taken.has(name); n++) name = `${base} ${n}`;
  return name;
};

// A Nodes or Subjects plot's series, as shown, in a new Compare graph, opened
// there: from a subject found in a table to comparing it with others.
const openPlotInCompare = (plotArea) => {
  const sid = state.selectedPlotSubject;
  const shown = plotArea._plotCtx?.visible || [];
  if (sid == null || !shown.length) return;
  const graph = newCompareGraph({
    name: `S${sid}`,
    series: shown.map((s) => (s.nodeId == null
      ? { subjectId: sid, attribute: s.field }
      : { subjectId: sid, attribute: s.field, nodeId: s.nodeId })),
  });
  state.compareGraphs.push(graph);
  saveSettings();
  switchView('compare');
  el('compareContainer').querySelector(`[data-graph-id="${graph.id}"]`)?.scrollIntoView({ block: 'nearest' });
};

// ── Derived series ──
//
// Computed from a graph's series when it draws: their kinds, how each is made, named.
const DERIVED_TYPES = {
  delta:       { label: 'Delta (A−B)',   sources: 2, hasWindow: false },
  rolling_avg: { label: 'Rolling Avg',        sources: 1, hasWindow: true  },
  min_max:     { label: 'Rolling Min/Max',    sources: 1, hasWindow: true  },
  rate:        { label: 'Rate (dv/dt)',       sources: 1, hasWindow: false },
  ratio:       { label: 'Ratio (A/B)',        sources: 2, hasWindow: false },
};

const _ALIGN_TOLERANCE = 2;

const _alignSeries = (dataA, dataB) => {
  if (!dataA?.length || !dataB?.length) return [];
  const result = [];
  let j = 0;
  for (const a of dataA) {
    while (j < dataB.length - 1 && dataB[j + 1].t <= a.t) j++;
    let best = dataB[j];
    if (j + 1 < dataB.length && Math.abs(dataB[j + 1].t - a.t) < Math.abs(best.t - a.t)) {
      best = dataB[j + 1];
    }
    if (Math.abs(best.t - a.t) <= _ALIGN_TOLERANCE) {
      result.push({ t: a.t, vA: a.v, vB: best.v });
    }
  }
  return result;
};

const _computeDerived = (type, dataA, dataB, windowSize) => {
  switch (type) {
    case 'delta': {
      const aligned = _alignSeries(dataA, dataB);
      return [aligned.map(p => ({ t: p.t, v: p.vA - p.vB }))];
    }
    case 'ratio': {
      const aligned = _alignSeries(dataA, dataB);
      return [aligned.filter(p => p.vB !== 0).map(p => ({ t: p.t, v: p.vA / p.vB }))];
    }
    case 'rate': {
      if (!dataA || dataA.length < 2) return [[]];
      const result = [];
      for (let i = 1; i < dataA.length; i++) {
        const dt = dataA[i].t - dataA[i - 1].t;
        if (dt > 0 && dt <= PLOT_GAP_THRESHOLD) {
          result.push({ t: dataA[i].t, v: (dataA[i].v - dataA[i - 1].v) / dt });
        }
      }
      return [result];
    }
    case 'rolling_avg': {
      if (!dataA || dataA.length < 2) return [[]];
      const w = Math.max(2, windowSize || 10);
      const result = [];
      let sum = 0;
      for (let i = 0; i < dataA.length; i++) {
        sum += dataA[i].v;
        if (i >= w) sum -= dataA[i - w].v;
        const count = Math.min(i + 1, w);
        result.push({ t: dataA[i].t, v: sum / count });
      }
      return [result];
    }
    case 'min_max': {
      // At each sample, the lowest and highest of the last `windowSize`: an
      // envelope that follows the signal. The window's candidates are kept
      // in order (lowest or highest first), so it costs one pass.
      if (!dataA || dataA.length < 2) return [[], []];
      const w = Math.max(2, windowSize || 10);
      const lows = [];
      const highs = [];
      const lo = [];  // indexes whose values rise
      const hi = [];  // indexes whose values fall
      for (let i = 0; i < dataA.length; i++) {
        const v = dataA[i].v;
        while (lo.length && dataA[lo[lo.length - 1]].v >= v) lo.pop();
        while (hi.length && dataA[hi[hi.length - 1]].v <= v) hi.pop();
        lo.push(i);
        hi.push(i);
        if (lo[0] <= i - w) lo.shift();
        if (hi[0] <= i - w) hi.shift();
        lows.push({ t: dataA[i].t, v: dataA[lo[0]].v });
        highs.push({ t: dataA[i].t, v: dataA[hi[0]].v });
      }
      return [lows, highs];
    }
    default:
      return [];
  }
};

const _derivedLabel = (d) => {
  const nameA = d.sourceA ? _keyLabel(d.sourceA) : '?';
  const nameB = d.sourceB ? _keyLabel(d.sourceB) : '';
  switch (d.type) {
    case 'delta': return `Δ(${nameA} − ${nameB})`;
    case 'ratio': return `${nameA} / ${nameB}`;
    case 'rolling_avg': return `Avg${d.window || 10}(${nameA})`;
    case 'rate': return `d/dt(${nameA})`;
    default: return '?';
  }
};

const _derivedMinMaxLabels = (d) => {
  const nameA = d.sourceA ? _keyLabel(d.sourceA) : '?';
  return [`Min${d.window || 10}(${nameA})`, `Max${d.window || 10}(${nameA})`];
};

const _nextDerivedId = (graph) => {
  let max = 0;
  for (const d of graph.derivedSeries || []) {
    const m = d.id?.match(/^ds_(\d+)$/);
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `ds_${max + 1}`;
};

const initCompareView = () => {
  const container = el('compareContainer');
  if (container.querySelector('.compare-toolbar')) {
    for (const graph of state.compareGraphs) {
      const card = container.querySelector(`[data-graph-id="${graph.id}"]`);
      if (card) card.querySelector('.plot-compare-panel')?._refreshSeriesList?.();
      // A graph made elsewhere (from a Nodes or Subjects plot) gets its card.
      else container.querySelector('.compare-cards').appendChild(_buildGraphCard(graph));
    }
    startCompareAnim();
    return;
  }
  container.innerHTML = '';

  const toolbar = document.createElement('div');
  toolbar.className = 'compare-toolbar';

  const addBtn = document.createElement('button');
  addBtn.className = 'compare-add-btn';
  addBtn.type = 'button';
  addBtn.textContent = '+ Add Graph';
  addBtn.addEventListener('click', () => {
    const graph = newCompareGraph();
    state.compareGraphs.push(graph);
    saveSettings();
    const card = _buildGraphCard(graph);
    cardsContainer.appendChild(card);
    _renderOneGraph(graph);
  });
  toolbar.appendChild(addBtn);

  const pauseAllBtn = document.createElement('button');
  pauseAllBtn.className = 'compare-add-btn compare-pause-all';
  pauseAllBtn.type = 'button';
  pauseAllBtn.setAttribute('aria-label', 'Pause/resume all graphs');
  pauseAllBtn.addEventListener('click', () => {
    // Each graph not already where it takes them: a paused one stays as it was.
    const allPaused = _allComparePaused();
    for (const graph of state.compareGraphs) {
      if (graph.paused === allPaused) togglePlotPause(graph);
    }
    // Synced graphs, each paused where it was drawn last, hold one moment.
    const synced = state.compareGraphs.find((g) => g.sync);
    if (synced) _shareView(synced);
    saveSettings();
    _syncPauseAll();
    for (const graph of state.compareGraphs) {
      const card = cardsContainer.querySelector(`[data-graph-id="${graph.id}"]`);
      if (!card) continue;
      syncPauseButton(card.querySelector('.plot-pause-btn'), graph);
      _renderOneGraph(graph);  // a graph just paused keeps what it shows now
    }
  });
  toolbar.appendChild(pauseAllBtn);

  const savedWrap = document.createElement('div');
  savedWrap.className = 'compare-saved-wrap';
  const savedBtn = document.createElement('button');
  savedBtn.className = 'compare-saved-btn';
  savedBtn.type = 'button';
  savedBtn.textContent = 'Saved graphs ▾';
  savedBtn.setAttribute('aria-haspopup', 'true');
  savedBtn.setAttribute('aria-expanded', 'false');
  savedBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _refreshSavedMenu(savedMenu, cardsContainer);
    showSavedMenu(savedMenu.classList.contains('hidden'));
  });
  savedWrap.appendChild(savedBtn);
  const savedMenu = document.createElement('div');
  savedMenu.className = 'compare-saved-menu hidden';
  savedWrap.appendChild(savedMenu);
  toolbar.appendChild(savedWrap);
  const showSavedMenu = (open) => {
    savedMenu.classList.toggle('hidden', !open);
    savedBtn.setAttribute('aria-expanded', String(open));
  };

  const sep = document.createElement('div');
  sep.className = 'compare-toolbar-sep';
  toolbar.appendChild(sep);

  const exportBtn = document.createElement('button');
  exportBtn.className = 'compare-add-btn';
  exportBtn.type = 'button';
  exportBtn.textContent = 'Export Workspace';
  exportBtn.setAttribute('aria-label', 'Export all graphs to file');
  exportBtn.addEventListener('click', () => {
    const data = {
      graphs: state.compareGraphs.map(compareGraphConfig),
      saved: state.savedCompareConfigs,
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `cynitor-compare-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('Session exported', 'info', 2000);
  });
  toolbar.appendChild(exportBtn);

  const importBtn = document.createElement('button');
  importBtn.className = 'compare-add-btn';
  importBtn.type = 'button';
  importBtn.textContent = 'Import Workspace';
  importBtn.setAttribute('aria-label', 'Import graphs from file');
  importBtn.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.addEventListener('change', () => {
      const file = input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        // The whole file is read and checked before anything changes.
        let graphs, saved;
        try {
          const data = JSON.parse(reader.result);
          if (!Array.isArray(data?.graphs)) throw new Error('no graphs in this file');
          graphs = data.graphs.map(sanitizeCompareGraph).filter(Boolean);
          if (data.graphs.length && !graphs.length) throw new Error('no valid graph in this file');
          saved = Array.isArray(data.saved) ? data.saved.map(sanitizeCompareGraph).filter(Boolean) : null;
        } catch (err) {
          showToast(`Import failed: ${err.message}`, 'error', 3000);
          return;
        }
        const replaces = state.compareGraphs.length || (saved && state.savedCompareConfigs.length);
        if (replaces && !window.confirm(
          `Import ${file.name}? It replaces the graphs here${saved ? ' and the saved graphs' : ''}.`)) return;
        state.compareGraphs.length = 0;
        for (const g of graphs) state.compareGraphs.push(newCompareGraph(g));
        if (saved) state.savedCompareConfigs = saved;
        saveSettings();
        for (const card of [...cardsContainer.querySelectorAll('.compare-graph-card')]) {
          _cardWatcher.unobserve(card);
          card.remove();
        }
        for (const graph of state.compareGraphs) cardsContainer.appendChild(_buildGraphCard(graph));
        startCompareAnim();
        showToast('Session imported', 'info', 2000);
      };
      reader.readAsText(file);
    });
    input.click();
  });
  toolbar.appendChild(importBtn);

  document.addEventListener('click', () => showSavedMenu(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') showSavedMenu(false);
  });

  container.appendChild(toolbar);

  // What needs a look, over the graphs (_syncCompareStatus); a count, clicked,
  // brings the first graph it counts into view.
  const status = document.createElement('div');
  status.className = 'table-status-bar compare-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-label', 'Graphs that need a look');
  status.addEventListener('click', (e) => {
    const kind = _COMPARE_STATUS.find((k) => k.key === e.target.closest('[data-focus]')?.dataset.focus);
    const graph = kind && state.compareGraphs.find((g) => kind.count(g));
    if (graph) cardsContainer.querySelector(`[data-graph-id="${graph.id}"]`)?.scrollIntoView({ block: 'nearest' });
  });
  container.appendChild(status);

  const cardsContainer = document.createElement('div');
  cardsContainer.className = 'compare-cards';

  // With no graph yet, the tab says how to start (_syncCompareEmpty).
  const empty = document.createElement('div');
  empty.className = 'compare-empty hidden';
  empty.innerHTML = svcStateMsg(
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 18 8 10 12 14 16 6 20 12"/></svg>',
    'No graphs yet',
    'Add a graph, then tick the series to compare in its list. A Nodes or Subjects plot opens its series here too, with its Compare button.',
    '<button type="button" class="graph-btn compare-empty-add">Add a graph</button>');
  empty.querySelector('.compare-empty-add').addEventListener('click', () => addBtn.click());
  cardsContainer.appendChild(empty);

  for (const graph of state.compareGraphs) {
    const card = _buildGraphCard(graph);
    cardsContainer.appendChild(card);
  }

  container.appendChild(cardsContainer);
  startCompareAnim();
};

const _refreshSavedMenu = (menu, cardsContainer) => {
  menu.innerHTML = '';
  if (!state.savedCompareConfigs.length) {
    const empty = document.createElement('div');
    empty.className = 'compare-saved-empty';
    empty.textContent = 'No saved graphs';
    menu.appendChild(empty);
    return;
  }
  for (let ci = 0; ci < state.savedCompareConfigs.length; ci++) {
    const config = state.savedCompareConfigs[ci];
    const item = document.createElement('div');
    item.className = 'compare-saved-item';
    const nameBtn = document.createElement('button');  // a button: the keyboard opens it too
    nameBtn.type = 'button';
    nameBtn.className = 'compare-saved-name';
    nameBtn.textContent = config.name || 'Untitled';
    nameBtn.addEventListener('click', () => {
      menu.classList.add('hidden');
      const graph = newCompareGraph(config);
      state.compareGraphs.push(graph);
      saveSettings();
      const card = _buildGraphCard(graph);
      cardsContainer.appendChild(card);
      _renderOneGraph(graph);
    });
    item.appendChild(nameBtn);
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'compare-saved-delete';
    delBtn.textContent = '×';
    delBtn.setAttribute('aria-label', `Delete ${config.name}`);
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      state.savedCompareConfigs.splice(ci, 1);
      saveSettings();
      _refreshSavedMenu(menu, cardsContainer);
    });
    item.appendChild(delBtn);
    menu.appendChild(item);
  }
};

// Every series Compare can plot: a numeric field of a subject from one of the
// nodes its history has it from (or from no node: anonymous), with what
// names it besides (type, node), by subject-ID, field and node.
const _comparableSeries = () => {
  const found = [];
  for (const [key, points] of state.subjectHistory) {
    const at = key.indexOf(':');
    const sid = Number(key.slice(0, at));
    const field = key.slice(at + 1);
    const type = dsdlTypeName(state.latestBySubject.get(sid)?.message_type) || '';
    const nids = new Set();
    for (const p of points) nids.add(Number.isInteger(p.n) ? p.n : null);
    for (const nid of nids) {
      const series = nid == null ? { subjectId: sid, attribute: field } : { subjectId: sid, attribute: field, nodeId: nid };
      found.push({ ...series, about: [type, nid == null ? 'anonymous' : nodeDisplayName(nid)].filter(Boolean).join(' · ') });
    }
  }
  return found.sort((a, b) => a.subjectId - b.subjectId
    || a.attribute.localeCompare(b.attribute, undefined, { numeric: true })
    || (a.nodeId ?? Infinity) - (b.nodeId ?? Infinity) || 0);
};

// A graph's editing rows: its series, derived series, thresholds and markers, a row each.
const buildComparePanel = (graph, onUpdate) => {
  const panel = document.createElement('div');
  panel.className = 'plot-compare-panel';

  const seriesSection = document.createElement('div');
  seriesSection.className = 'plot-series-section';
  const hdr = document.createElement('div');
  hdr.className = 'plot-compare-hdr';
  const title = document.createElement('span');
  title.textContent = 'Series';
  hdr.appendChild(title);
  seriesSection.appendChild(hdr);

  // Every series heard, in one list: the filter's words find them by any part
  // of their name, node or type; ticked, a series is in the graph. A series
  // keeps to one node ("S1300 · value · n21"), so a node that starts
  // publishing its subject later does not mix in.
  const picker = document.createElement('div');
  picker.className = 'plot-series-picker';
  const seriesFilter = document.createElement('input');
  seriesFilter.type = 'search';
  seriesFilter.className = 'plot-series-filter';
  seriesFilter.placeholder = 'Find by subject-ID, field, node or type';
  seriesFilter.setAttribute('aria-label', 'Find series to compare');
  const seriesList = document.createElement('div');
  seriesList.className = 'plot-series-list';
  seriesList.setAttribute('role', 'group');
  seriesList.setAttribute('aria-label', 'Series to compare');

  let listed = new Map();  // key -> series, as last listed
  const refreshSeriesList = () => {
    const words = seriesFilter.value.toLowerCase().split(/\s+/).filter(Boolean);
    const all = _comparableSeries();
    const shown = all.filter((s) => {
      const text = `${compareSeriesName(s)} ${s.about}`.toLowerCase();
      return words.every((w) => text.includes(w));
    });
    listed = new Map(shown.map((s) => [compareSeriesKey(s), s]));
    const fresh = document.createElement('div');
    fresh.innerHTML = shown.length
      ? shown.map((s) => `<label class="plot-series-option"><input type="checkbox" data-key="${escapeHtml(compareSeriesKey(s))}">`
        + `<span class="plot-series-key">${escapeHtml(compareSeriesName(s))}</span>`
        + `<span class="plot-series-about">${escapeHtml(s.about)}</span></label>`).join('')
      : `<div class="plot-series-none">${all.length
        ? `No series match “${escapeHtml(seriesFilter.value.trim())}”`
        : 'Nothing heard yet: the list fills as messages come.'}</div>`;
    // Rewritten in place, so a box under the pointer is not swapped out;
    // ticked as the graph has it (a box keeps its own state otherwise).
    patchChildren(seriesList, fresh);
    const inGraph = new Set(graph.series.map(compareSeriesKey));
    for (const box of seriesList.querySelectorAll('input[type="checkbox"]')) box.checked = inGraph.has(box.dataset.key);
  };
  seriesFilter.addEventListener('input', refreshSeriesList);
  seriesFilter.addEventListener('focus', refreshSeriesList);

  seriesList.addEventListener('change', (e) => {
    const key = e.target.dataset?.key;
    const s = listed.get(key);
    if (!s) return;
    const at = graph.series.findIndex((c) => compareSeriesKey(c) === key);
    if (e.target.checked && at === -1) {
      graph.series.push(s.nodeId == null
        ? { subjectId: s.subjectId, attribute: s.attribute }
        : { subjectId: s.subjectId, attribute: s.attribute, nodeId: s.nodeId });
    } else if (!e.target.checked && at !== -1) {
      graph.series.splice(at, 1);
    }
    onUpdate();
  });

  picker.appendChild(seriesFilter);
  picker.appendChild(seriesList);
  seriesSection.appendChild(picker);
  panel.appendChild(seriesSection);
  panel._refreshSeriesList = refreshSeriesList;
  refreshSeriesList();

  const derivedSection = document.createElement('div');
  derivedSection.className = 'plot-derived-section';
  const derivedHdr = document.createElement('span');
  derivedHdr.className = 'plot-threshold-hdr';
  derivedHdr.textContent = 'Derived';
  derivedSection.appendChild(derivedHdr);

  const derivedPicker = document.createElement('div');
  derivedPicker.className = 'plot-compare-picker';

  const typeSel = document.createElement('select');
  typeSel.className = 'plot-derived-type';
  typeSel.setAttribute('aria-label', 'Derived series type');
  for (const [key, info] of Object.entries(DERIVED_TYPES)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = info.label;
    typeSel.appendChild(opt);
  }

  const srcASel = document.createElement('select');
  srcASel.className = 'plot-derived-source';
  srcASel.setAttribute('aria-label', 'Source A');

  const srcBSel = document.createElement('select');
  srcBSel.className = 'plot-derived-source';
  srcBSel.setAttribute('aria-label', 'Source B');

  const windowInput = document.createElement('input');
  windowInput.type = 'number';
  windowInput.className = 'plot-derived-window';
  windowInput.placeholder = 'Window';
  windowInput.value = '10';
  windowInput.min = '2';
  windowInput.max = '200';
  windowInput.setAttribute('aria-label', 'Window size (samples)');

  const _refreshDerivedSources = () => {
    const buildOpts = (sel, label) => {
      const prev = sel.value;
      sel.innerHTML = '';
      const def = document.createElement('option');
      def.value = '';
      def.textContent = label;
      sel.appendChild(def);
      for (const s of graph.series) {
        const opt = document.createElement('option');
        opt.value = compareSeriesKey(s);
        opt.textContent = compareSeriesName(s);
        sel.appendChild(opt);
      }
      if (prev && sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
    };
    buildOpts(srcASel, 'Source A…');
    buildOpts(srcBSel, 'Source B…');
  };

  const _updateDerivedVisibility = () => {
    const info = DERIVED_TYPES[typeSel.value];
    srcBSel.classList.toggle('hidden', info?.sources !== 2);
    windowInput.classList.toggle('hidden', !info?.hasWindow);
  };

  typeSel.addEventListener('change', _updateDerivedVisibility);

  const derivedAddBtn = document.createElement('button');
  derivedAddBtn.className = 'plot-compare-add';
  derivedAddBtn.type = 'button';
  derivedAddBtn.textContent = 'Add';
  derivedAddBtn.setAttribute('aria-label', 'Add derived series');
  // What a derived series still needs, with the box to fill; or null.
  const _derivedMissing = (info) => {
    if (!graph.series.length) return [seriesFilter, 'Add a series to the graph first: derived series are made from its series'];
    if (!srcASel.value) return [srcASel, `Pick Source A for ${info.label}`];
    if (info.sources === 2 && !srcBSel.value) return [srcBSel, `Pick Source B for ${info.label}`];
    return null;
  };
  derivedAddBtn.addEventListener('click', () => {
    const type = typeSel.value;
    const info = DERIVED_TYPES[type];
    if (!info) return;
    const missing = _derivedMissing(info);
    if (missing) {
      missing[0].focus();
      showToast(missing[1], 'error');
      return;
    }
    const srcA = srcASel.value;
    const srcB = info.sources === 2 ? srcBSel.value : '';
    const win = info.hasWindow ? Math.max(2, parseInt(windowInput.value) || 10) : 0;
    if (!graph.derivedSeries) graph.derivedSeries = [];
    graph.derivedSeries.push({
      id: _nextDerivedId(graph),
      type,
      sourceA: srcA,
      sourceB: srcB,
      window: win,
      color: '',
    });
    onUpdate();
  });

  derivedPicker.appendChild(typeSel);
  derivedPicker.appendChild(srcASel);
  derivedPicker.appendChild(srcBSel);
  derivedPicker.appendChild(windowInput);
  derivedPicker.appendChild(derivedAddBtn);
  derivedSection.appendChild(derivedPicker);

  panel.appendChild(derivedSection);

  panel._refreshDerivedSources = _refreshDerivedSources;
  _refreshDerivedSources();
  _updateDerivedVisibility();

  const thSection = document.createElement('div');
  thSection.className = 'plot-threshold-section';
  const thLabel = document.createElement('span');
  thLabel.className = 'plot-threshold-hdr';
  thLabel.textContent = 'Thresholds';
  thSection.appendChild(thLabel);
  const thPicker = document.createElement('div');
  thPicker.className = 'plot-threshold-picker';
  const thInput = document.createElement('input');
  thInput.type = 'number';
  thInput.className = 'plot-threshold-input';
  thInput.placeholder = 'Value';
  thInput.setAttribute('aria-label', 'Threshold value');
  const thNameInput = document.createElement('input');
  thNameInput.type = 'text';
  thNameInput.className = 'plot-threshold-name-input';
  thNameInput.placeholder = 'Label';
  thNameInput.setAttribute('aria-label', 'Threshold label');
  const thAddBtn = document.createElement('button');
  thAddBtn.className = 'plot-compare-add';
  thAddBtn.type = 'button';
  thAddBtn.textContent = 'Add';
  thAddBtn.setAttribute('aria-label', 'Add threshold line');
  thAddBtn.addEventListener('click', () => {
    const val = parseFloat(thInput.value);
    if (isNaN(val)) {
      thInput.focus();
      showToast('Type a value for the threshold', 'error');
      return;
    }
    graph.thresholds.push({
      value: val,
      label: thNameInput.value.trim() || String(val),
      color: '',  // the theme's red, until one is picked
      style: 'dashed',
    });
    thInput.value = '';
    thNameInput.value = '';
    onUpdate();
  });
  thPicker.appendChild(thInput);
  thPicker.appendChild(thNameInput);
  thPicker.appendChild(thAddBtn);
  thSection.appendChild(thPicker);
  panel.appendChild(thSection);

  // Its markers, by time, and its drawings: found here once off screen, and
  // removed. While there are none, the row says how to add them.
  const marksSection = document.createElement('div');
  marksSection.className = 'compare-marks';
  const marksHdr = document.createElement('span');
  marksHdr.className = 'plot-threshold-hdr';
  marksHdr.textContent = 'Markers';
  const marksList = document.createElement('div');
  marksList.className = 'compare-marks-list';
  marksSection.append(marksHdr, marksList);
  panel.appendChild(marksSection);

  const drawingsCount = () => `${graph.drawings.length} drawing${graph.drawings.length === 1 ? '' : 's'}`;
  const refreshMarks = () => {
    const markers = graph.markers.map((m, i) => ({ m, i })).sort((a, b) => a.m.t - b.m.t);
    const hints = [!markers.length && 'Shift+click the plot to mark a moment', !graph.drawings.length && 'Alt+drag on it to draw'];
    const fresh = document.createElement('div');
    fresh.innerHTML = markers.map(({ m, i }) => {
      const label = escapeHtml(m.label || '');
      const about = `${new Date(m.t * 1000).toLocaleString()}${m.note ? ` · ${m.note}` : ''}`;
      return `<span class="compare-mark"><button type="button" class="compare-mark-go" data-i="${i}" title="Show it: ${escapeHtml(about)}">`
        + `${formatPlotTime(m.t)} ${label}</button><button type="button" class="compare-mark-delete" data-i="${i}"`
        + ` aria-label="Delete marker ${label}">×</button></span>`;
    }).join('')
      + (graph.drawings.length ? `<span class="compare-marks-drawings">${drawingsCount()}</span>`
        + '<button type="button" class="plot-compare-add compare-marks-clear">Clear</button>' : '')
      + (hints.some(Boolean) ? `<span class="compare-marks-hint">${hints.filter(Boolean).join(' · ')}</span>` : '');
    patchChildren(marksList, fresh);
  };
  marksList.addEventListener('click', (e) => {
    const show = e.target.closest('.compare-mark-go');
    const remove = e.target.closest('.compare-mark-delete');
    if (show) {
      _showMarker(graph, graph.markers[Number(show.dataset.i)]);
    } else if (remove) {
      graph.markers.splice(Number(remove.dataset.i), 1);
      onUpdate();
    } else if (e.target.closest('.compare-marks-clear') && window.confirm(`Clear this graph's ${drawingsCount()}?`)) {
      graph.drawings = [];
      onUpdate();
    }
  });
  panel._refreshMarks = refreshMarks;
  refreshMarks();

  return panel;
};

// A marker shown from its graph's list: the graph pauses with the marker in
// the middle of its window, whose right edge runs 20% past where it is held
// (computePlotScales). An "All" window shows it where the history reaches.
const _showMarker = (graph, marker) => {
  if (!marker) return;
  if (!graph.paused) togglePlotPause(graph);
  if (graph.timeWindow > 0) graph.pausedAt = marker.t + graph.timeWindow * 0.4;
  graph._zoom = 1;
  graph._panOffset = 0;
  graph._fingerprint = '';
  syncPauseButton(el('compareContainer').querySelector(`[data-graph-id="${graph.id}"] .plot-pause-btn`), graph);
  _shareView(graph);
  _renderOneGraph(graph);
};

// What a synced graph shares with the others: whether and where it is paused,
// its time window, zoom and pan.
const _viewOf = (g) => [g.paused, g.pausedAt, g.timeWindow, g._zoom, g._panOffset, g._resumeFrom, g._resumeStart].join('|');

// A synced graph's view goes to the other synced graphs, so they show the
// same time: one paused, given a window, zoomed or moved takes them with it.
const _shareView = (graph) => {
  if (!graph.sync) return;
  const view = _viewOf(graph);
  for (const other of state.compareGraphs) {
    if (other === graph || !other.sync || _viewOf(other) === view) continue;
    if (other.paused !== graph.paused) togglePlotPause(other);  // resumed, its Fill Rate starts anew
    Object.assign(other, {
      pausedAt: graph.pausedAt, _resumeFrom: graph._resumeFrom, _resumeStart: graph._resumeStart,
      timeWindow: graph.timeWindow, _zoom: graph._zoom, _panOffset: graph._panOffset, _fingerprint: '',
    });
    const card = el('compareContainer').querySelector(`[data-graph-id="${other.id}"]`);
    syncPauseButton(card?.querySelector('.plot-pause-btn'), other);
    for (const btn of card?.querySelectorAll('.plot-window-btn') || []) {
      btn.classList.toggle('active', Number(btn.dataset.secs) === other.timeWindow);
    }
    _renderOneGraph(other);
    saveSettings();  // its window is kept
  }
};

// The Markers row follows markers and drawings however they change: on the
// plot (Shift+click, the marker form, Alt+drag), or from the row itself.
const _syncMarks = (graph, card) => {
  const key = JSON.stringify([graph.markers, graph.drawings.length]);
  if (graph._marksKey === key) return;
  graph._marksKey = key;
  card.querySelector('.plot-compare-panel')?._refreshMarks?.();
};

const _buildGraphCard = (graph) => {
  const card = document.createElement('div');
  card.className = 'compare-graph-card';
  card.dataset.graphId = graph.id;
  _cardWatcher.observe(card);

  const plotArea = document.createElement('div');
  plotArea.className = 'detail-plot-area';

  const onUpdate = () => {
    graph._fingerprint = '';
    saveSettings();
    if (panel._refreshDerivedSources) panel._refreshDerivedSources();
    _renderCompareGraphNow(graph, plotArea);
  };

  // A card is its header (Edit, name, time window, actions), its editing rows
  // (series, derived, thresholds, display), folded away under Edit, and its plot.
  const panel = buildComparePanel(graph, onUpdate);

  const cardActions = document.createElement('div');
  cardActions.className = 'compare-card-actions';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'compare-graph-name';
  nameInput.placeholder = 'Untitled';
  nameInput.setAttribute('aria-label', 'Graph name');
  nameInput.value = graph.name;
  nameInput.addEventListener('input', () => {
    graph.name = nameInput.value;
    saveSettings();
  });

  const saveBtn = document.createElement('button');
  saveBtn.className = 'compare-graph-save';
  saveBtn.type = 'button';
  saveBtn.textContent = 'Save';
  saveBtn.setAttribute('aria-label', 'Save configuration');
  saveBtn.addEventListener('click', () => {
    // Saved without a name, a graph gets a free one: it saves apart from
    // other untitled graphs, and its later saves update it.
    if (!graph.name.trim()) {
      graph.name = _freeSavedName('Untitled');
      nameInput.value = graph.name;
    }
    const name = graph.name.trim();
    const config = { ...compareGraphConfig(graph), name };
    const existing = state.savedCompareConfigs.findIndex(c => c.name === name);
    if (existing !== -1) state.savedCompareConfigs[existing] = config;
    else state.savedCompareConfigs.push(config);
    saveSettings();
    showToast(`Saved "${name}"`, 'info', 2000);
  });
  cardActions.appendChild(saveBtn);

  const cloneBtn = document.createElement('button');
  cloneBtn.className = 'compare-graph-clone';
  cloneBtn.type = 'button';
  cloneBtn.textContent = '⧉';
  cloneBtn.setAttribute('aria-label', 'Clone graph');
  cloneBtn.addEventListener('click', () => {
    const clone = newCompareGraph({
      ...compareGraphConfig(graph),
      name: graph.name ? `${graph.name} (copy)` : '',
    });
    state.compareGraphs.push(clone);
    saveSettings();
    const cloneCard = _buildGraphCard(clone);
    card.parentElement.appendChild(cloneCard);
    _renderOneGraph(clone);
  });
  cardActions.appendChild(cloneBtn);

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'compare-graph-delete';
  deleteBtn.type = 'button';
  deleteBtn.textContent = '×';
  deleteBtn.setAttribute('aria-label', 'Remove graph');
  deleteBtn.addEventListener('click', () => {
    // Markers and drawings cannot be made again from the data: asked first.
    const notes = [[graph.markers.length, 'marker'], [graph.drawings.length, 'drawing']]
      .filter(([n]) => n).map(([n, what]) => `${n} ${what}${n === 1 ? '' : 's'}`);
    const which = graph.name.trim() ? `"${graph.name.trim()}"` : 'this graph';
    if (notes.length && !window.confirm(`Remove ${which}? Its ${notes.join(' and ')} go with it.`)) return;
    graph._fingerprint = '';
    const idx = state.compareGraphs.indexOf(graph);
    if (idx !== -1) state.compareGraphs.splice(idx, 1);
    saveSettings();
    _cardWatcher.unobserve(card);
    card.remove();
  });
  cardActions.appendChild(deleteBtn);

  // The time controls go in the header, the display ones in the editing rows.
  const opts = {
    cfg: graph,
    invalidate: () => {
      graph._fingerprint = '';
      if (graph.paused && !graph._frozen) _renderOneGraph(graph);  // just paused: keeps what it shows
      _shareView(graph);  // paused, resumed or given a window: synced graphs follow
    },
    rerender: () => _renderCompareGraphNow(graph, plotArea),
    restart: () => _renderOneGraph(graph),
    includeDraw: true,
  };
  const allControls = buildPlotControls(opts);
  const timeControls = document.createElement('div');
  timeControls.className = 'compare-time-controls';
  const visualControls = document.createElement('div');
  visualControls.className = 'compare-visual-controls';
  const displayHdr = document.createElement('span');
  displayHdr.className = 'plot-threshold-hdr';
  displayHdr.textContent = 'Display';
  visualControls.appendChild(displayHdr);
  let pastFirstSep = false;
  while (allControls.firstChild) {
    const child = allControls.firstChild;
    if (!pastFirstSep && child.classList?.contains('plot-controls-sep')) {
      child.remove();
      pastFirstSep = true;
      continue;
    }
    (pastFirstSep ? visualControls : timeControls).appendChild(child);
  }
  const kept = document.createElement('span');  // how much history there is, when short (_syncKept)
  kept.className = 'compare-kept hidden';
  timeControls.appendChild(kept);

  // Synced graphs show the same time: see _shareView. One that joins takes the group's view.
  const syncBtn = document.createElement('button');
  syncBtn.type = 'button';
  syncBtn.className = 'compare-sync-btn';
  syncBtn.textContent = 'Sync';
  syncBtn.title = 'Pause, time window, zoom and pan together with the other synced graphs';
  const showSync = () => {
    syncBtn.classList.toggle('active', graph.sync);
    syncBtn.setAttribute('aria-pressed', String(graph.sync));
  };
  syncBtn.addEventListener('click', () => {
    graph.sync = !graph.sync;
    showSync();
    saveSettings();
    const group = state.compareGraphs.find((g) => g.sync && g !== graph);
    if (graph.sync && group) _shareView(group);
  });
  showSync();
  timeControls.appendChild(syncBtn);

  // A click on the plot pauses it only when asked to: by accident, it would.
  const clickLabel = document.createElement('label');
  clickLabel.className = 'plot-check-label';
  const clickCb = document.createElement('input');
  clickCb.type = 'checkbox';
  clickCb.checked = graph.clickPauses;
  clickCb.setAttribute('aria-label', 'A click on the plot pauses it');
  clickCb.addEventListener('change', () => {
    graph.clickPauses = clickCb.checked;
    saveSettings();
  });
  clickLabel.append(clickCb, ' Click pauses');
  visualControls.querySelector('.plot-draw-group').previousElementSibling.before(clickLabel);  // after Grid

  const editor = document.createElement('div');
  editor.className = 'compare-card-editor';
  editor.append(panel, visualControls);

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'compare-edit-btn';
  editBtn.textContent = 'Edit';
  const showEditor = (open) => {
    editor.classList.toggle('hidden', !open);
    editBtn.classList.toggle('active', open);
    editBtn.setAttribute('aria-expanded', String(open));
    if (open) panel._refreshSeriesList();
  };
  editBtn.addEventListener('click', () => showEditor(editor.classList.contains('hidden')));
  // A new graph opens on its editing rows, to pick series; one that has them, on its plot.
  showEditor(!graph.series.length);

  const header = document.createElement('div');
  header.className = 'compare-card-header';
  header.append(editBtn, nameInput, timeControls, cardActions);

  // Collapsed, a graph is its plot: its header and editing rows go, its legend
  // is a line of names, and what watching needs (pause, time window, Sync)
  // moves to a column at the plot's side; so graphs sit close to each other.
  const side = document.createElement('div');
  side.className = 'compare-side';
  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'compare-collapse-btn';
  collapseBtn.textContent = '▴';
  collapseBtn.setAttribute('aria-label', 'Collapse the graph to its plot');
  collapseBtn.title = 'Show only the plot, its pause, time window and Sync at its side';
  const expandBtn = document.createElement('button');
  expandBtn.type = 'button';
  expandBtn.className = 'compare-collapse-btn';
  expandBtn.textContent = '▾';
  expandBtn.setAttribute('aria-label', 'Expand the graph: its header and legend');
  expandBtn.title = 'Show the graph\'s header and legend';
  const windowSel = document.createElement('select');  // the window buttons', in short (_syncKept keeps it)
  windowSel.className = 'compare-window-select';
  windowSel.setAttribute('aria-label', 'Time window');
  for (const tw of PLOT_TIME_WINDOWS) windowSel.add(new Option(tw.label, String(tw.secs)));
  windowSel.value = String(graph.timeWindow);
  windowSel.addEventListener('change', () => timeControls.querySelector(`.plot-window-btn[data-secs="${windowSel.value}"]`)?.click());
  side.append(expandBtn, windowSel);
  // The pause and Sync buttons move between the header and the column.
  const pauseBtn = timeControls.querySelector('.plot-pause-btn');
  const setCollapsed = (on) => {
    graph.collapsed = on;
    card.classList.toggle('collapsed', on);
    collapseBtn.setAttribute('aria-expanded', String(!on));
    expandBtn.setAttribute('aria-expanded', String(!on));
    if (on) {
      expandBtn.after(pauseBtn);
      windowSel.after(syncBtn);
    } else {
      timeControls.prepend(pauseBtn);
      timeControls.appendChild(syncBtn);
    }
  };
  const toggleCollapsed = (on, focus) => {
    setCollapsed(on);
    saveSettings();
    graph._fingerprint = '';
    _renderOneGraph(graph);  // its plot has another size
    focus.focus();
  };
  collapseBtn.addEventListener('click', () => toggleCollapsed(true, expandBtn));
  expandBtn.addEventListener('click', () => toggleCollapsed(false, collapseBtn));
  cardActions.prepend(collapseBtn);
  setCollapsed(graph.collapsed);

  const body = document.createElement('div');
  body.className = 'compare-card-body';
  body.append(plotArea, side);

  card.append(header, editor, body);
  return card;
};

// The points a graph plots for a key, as Fill Rate has them (`raw` for a
// derived series' source). A paused graph plots those it had when paused:
// the history goes on (3600 points a field), and anything that redraws it (a
// legend click, a tab switch) would otherwise show another picture.
const _graphPoints = (graph, key, raw = false) => {
  const points = () => (raw ? plotData(key) : _getSmoothBuf(graph, key));
  if (!graph._frozen) return points();
  const id = raw ? `raw ${key}` : key;
  if (!graph._frozen.has(id)) graph._frozen.set(id, points()?.slice());
  return graph._frozen.get(id);
};

const _formatSpan = (secs) => (secs < 120 ? `${Math.round(secs)} s` : `${Math.round(secs / 60)} min`);

// How much history a graph's series keep where the points a field keeps
// (HISTORY_POINTS) are what limits it, not how long it has been heard: the
// shortest such span, in seconds, or null.
const _keptSeconds = (graph) => {
  let kept = null;
  for (const s of graph.series) {
    if ((state.subjectHistory.get(`${s.subjectId}:${s.attribute}`)?.length || 0) < HISTORY_POINTS) continue;
    const data = _graphPoints(graph, compareSeriesKey(s), true);
    if (!data || data.length < 2) continue;
    const span = data[data.length - 1].t - data[0].t;
    if (kept === null || span < kept) kept = span;
  }
  return kept;
};

// The time window's buttons say what the history keeps: a window it cannot
// fill is dashed, and while the one chosen cannot, "kept 36 s" says so.
const _syncKept = (graph, card) => {
  const kept = _keptSeconds(graph);
  for (const btn of card.querySelectorAll('.compare-time-controls .plot-window-btn')) {
    const short = kept !== null && Number(btn.dataset.secs) > kept;
    const title = short ? `The history kept here covers ${_formatSpan(kept)} (${HISTORY_POINTS} points a field): this window is partly empty` : '';
    btn.classList.toggle('plot-window-btn--beyond', short);
    if (btn.title !== title) btn.title = title;
  }
  const label = card.querySelector('.compare-kept');
  const text = kept !== null && graph.timeWindow > kept ? `kept ${_formatSpan(kept)}` : '';
  if (label.textContent !== text) label.textContent = text;
  label.classList.toggle('hidden', !text);
  const windowSel = card.querySelector('.compare-window-select');  // a collapsed graph's window
  if (windowSel.value !== String(graph.timeWindow)) windowSel.value = String(graph.timeWindow);
};

// A series' last, lowest and highest value in view, or null when none is.
const _statsInView = (data, tLeft, tRight) => {
  let last = null;
  let min = Infinity;
  let max = -Infinity;
  for (let i = data.length - 1; i >= 0 && data[i].t >= tLeft; i--) {
    const { t, v } = data[i];
    if (t > tRight) continue;
    if (last === null) last = v;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return last === null ? null : { last, min, max };
};

// A series' unit, as the latest message on its subject names it ("volt" for
// an SI wrapper's field), or ''.
const _seriesUnit = (cmp) => {
  const event = cmp.nodeId != null
    ? state.latestByNode.get(cmp.nodeId)?.get(cmp.subjectId)
    : state.latestBySubject.get(cmp.subjectId);
  const field = cmp.attribute.replace(/\[\d+\]$/, '');
  return event?.attributes?.find((a) => a.attribute === field)?.unit || '';
};

// Said over a graph that shows nothing, or why it stopped: nothing can
// arrive (a replay aside), nothing came yet, or none of it is in view.
const _compareNote = (problem, compareSeries, xScale) => {
  if (problem) return problem.message;
  if (!compareSeries.length) return 'Waiting for data…';
  const [tLeft, tRight] = xScale.domain();
  const inView = (data) => {
    for (let i = data.length - 1; i >= 0 && data[i].t >= tLeft; i--) {
      if (data[i].t <= tRight) return true;
    }
    return false;
  };
  return compareSeries.some((s) => inView(s.data)) ? '' : 'No data in view';
};

// The points of a line worth drawing: those in view and one beyond each edge,
// so it runs to them; of a pixel column holding more, only its lowest and
// highest, so no spike is lost. A point after a gap stays: the line breaks
// there. A redraw then costs what the plot shows, not what the history holds.
const _pointsToDraw = (data, xScale) => {
  const [tLeft, tRight] = xScale.domain();
  const bisect = d3.bisector((p) => p.t);
  const from = Math.max(0, bisect.left(data, tLeft) - 1);
  const to = Math.min(data.length, bisect.right(data, tRight) + 1);
  if (to - from <= 2 * xScale.range()[1]) return data.slice(from, to);
  const out = [];
  let column = null;
  let lo = null;
  let hi = null;
  const flush = () => {
    if (lo) out.push(...(lo === hi ? [lo] : lo.t < hi.t ? [lo, hi] : [hi, lo]));
    lo = hi = null;
  };
  for (let i = from; i < to; i++) {
    const p = data[i];
    const c = Math.floor(xScale(p.t));
    if (p._gap || c !== column) {
      flush();
      column = c;
      if (p._gap) {
        out.push(p);
        continue;
      }
    }
    if (!lo || p.v < lo.v) lo = p;
    if (!hi || p.v > hi.v) hi = p;
  }
  flush();
  return out;
};

// A line's path through its points, as d3.line().defined((p) => !p._gap)
// draws it (a point after a gap starts it anew), in tenths of a pixel written
// as whole numbers, for a path scaled by 0.1: as text, a fraction costs six
// times a whole number, and a dense line's redraw was mostly that.
const _linePath = (points, xScale, yScale) => {
  const [t0, t1] = xScale.domain();
  const [x0, x1] = xScale.range();
  const [v0, v1] = yScale.domain();
  const [y0, y1] = yScale.range();
  const kx = ((x1 - x0) / (t1 - t0)) * 10;
  const ky = ((y1 - y0) / (v1 - v0)) * 10;
  let path = '';
  let pen = 'M';
  for (const p of points) {
    if (p._gap) {
      pen = 'M';
      continue;
    }
    path += `${pen}${Math.round(x0 * 10 + (p.t - t0) * kx)},${Math.round(y0 * 10 + (p.v - v0) * ky)}`;
    pen = 'L';
  }
  return path || null;
};

const _renderCompareOverlay = (g, compareSeries, xScale, panelH, w, cfg) => {
  let overlay = g.select('.plot-compare-overlay');

  if (!compareSeries.length) {
    if (!overlay.empty()) overlay.selectAll('*').remove();
    return;
  }

  if (overlay.empty()) {
    overlay = g.insert('g', '.plot-x-axis').attr('class', 'plot-compare-overlay');
  }

  // The y-axis fits what is in view, thresholds included: a spike that has
  // left the window, or a zoom, does not flatten what is shown. With nothing
  // in view, it fits all there is.
  const [tLeft, tRight] = xScale.domain();
  const valueRange = (inViewOnly) => {
    let lo = Infinity, hi = -Infinity;
    for (const s of compareSeries) {
      for (const p of s.data) {
        if (inViewOnly && (p.t < tLeft || p.t > tRight)) continue;
        if (p.v < lo) lo = p.v;
        if (p.v > hi) hi = p.v;
      }
    }
    return [lo, hi];
  };
  let [vMin, vMax] = valueRange(true);
  if (!isFinite(vMin)) [vMin, vMax] = valueRange(false);
  // A threshold hidden from the legend is neither drawn nor kept on the axis.
  const thresholds = (cfg?.thresholds || []).filter((th) => !cfg._hidden?.has(thresholdName(th)));
  for (const th of thresholds) {
    vMin = Math.min(vMin, th.value);
    vMax = Math.max(vMax, th.value);
  }
  if (vMin === vMax) { vMin -= 1; vMax += 1; }
  const pad = (vMax - vMin) * 0.05;
  const yScale = d3.scaleLinear().domain([vMin - pad, vMax + pad]).range([panelH, 0]);

  // Each graph its own clip path: under one id for the page, every graph
  // would be clipped to the first one's size.
  const clipId = `compare-panel-clip-${_safeId(cfg.id)}`;
  let clip = overlay.select('clipPath');
  if (clip.empty()) {
    clip = overlay.append('clipPath').attr('id', clipId);
    clip.append('rect');
  }
  clip.select('rect').attr('width', w).attr('height', panelH);

  let yAxisG = overlay.select('.panel-y-axis');
  if (yAxisG.empty()) yAxisG = overlay.append('g').attr('class', 'panel-y-axis');
  yAxisG.call(d3.axisLeft(yScale).ticks(3).tickSize(2));

  const showGrid = cfg ? cfg.grid : false;
  if (showGrid) _renderGrid(overlay, xScale, yScale, w, panelH);
  else overlay.select('.plot-grid').remove();

  _renderThresholds(overlay, thresholds, yScale, w);
  _renderMarkers(overlay, cfg?.markers, xScale, panelH);
  _renderDrawings(overlay, cfg?.drawings, xScale, panelH);
  // Markers and drawings keep to the plot, as its lines do: scrolled past the
  // y-axis with time, they are cut there, not drawn over it.
  overlay.selectAll('.plot-markers, .plot-drawings').attr('clip-path', `url(#${clipId})`);

  const strokeW = cfg ? cfg.stroke : 1.5;
  const showDots = cfg ? cfg.disconnectPoints : false;
  const showLine = !showDots;

  // The lines, written in tenths of a pixel (_linePath) and scaled back; their
  // stroke and dashes keep their size on screen. Their group holds the clip,
  // which on a scaled path would be scaled too.
  let linesG = overlay.select('.compare-lines');
  if (linesG.empty()) linesG = overlay.append('g').attr('class', 'compare-lines').attr('clip-path', `url(#${clipId})`);
  const lines = linesG.selectAll('.compare-line').data(compareSeries, (d) => d.name);
  lines.enter().append('path')
    .attr('class', 'compare-line')
    .attr('fill', 'none')
    .attr('transform', 'scale(0.1)')
    .attr('vector-effect', 'non-scaling-stroke')
    .merge(lines)
    .attr('stroke', (d, i) => d.color || PLOT_COLORS[i % PLOT_COLORS.length])
    .attr('stroke-width', strokeW)
    .attr('stroke-dasharray', (d) => {
      const st = d._lineStyle || 'solid';
      if (MARKER_SHAPES[st]) return null;
      return THRESHOLD_STYLES[st] || null;
    })
    .attr('d', (d) => showLine && d.data.length >= 2 ? _linePath(_pointsToDraw(d.data, xScale), xScale, yScale) : null)
    .attr('opacity', (d) => showLine && d.data.length >= 2 ? 1 : 0);
  lines.exit().remove();

  const markerR = Math.max(2, strokeW);
  const maxMarkers = 200;
  compareSeries.forEach((s, i) => {
    const color = s.color || PLOT_COLORS[i % PLOT_COLORS.length];
    const safeN = _safeId(s.name);
    const shape = MARKER_SHAPES[s._lineStyle];
    const needMarkers = showLine && shape;
    const needDots = showDots && !shape;
    const cls = `compare-markers-${safeN}`;
    let mG = overlay.select(`.${cls}`);
    if (!needMarkers && !needDots) {
      if (!mG.empty()) mG.remove();
      return;
    }
    if (mG.empty()) {
      mG = overlay.append('g').attr('class', cls)
        .attr('clip-path', `url(#${clipId})`);
    }
    const vis = s.data.filter((p) => !p._gap && xScale(p.t) >= 0 && xScale(p.t) <= w);
    const step = vis.length > maxMarkers ? Math.ceil(vis.length / maxMarkers) : 1;
    const sampled = step > 1 ? vis.filter((_, j) => j % step === 0) : vis;
    if (needMarkers) {
      mG.selectAll('circle').remove();
      const paths = mG.selectAll('path').data(sampled, (p) => p.t);
      paths.enter().append('path')
        .merge(paths)
        .attr('d', (p) => shape(xScale(p.t), yScale(p.v), markerR))
        .attr('fill', color)
        .attr('stroke', 'none');
      paths.exit().remove();
    } else {
      mG.selectAll('path').remove();
      const dots = mG.selectAll('circle').data(sampled, (p) => p.t);
      dots.enter().append('circle').attr('fill', color)
        .merge(dots)
        .attr('r', strokeW)
        .attr('cx', (p) => xScale(p.t))
        .attr('cy', (p) => yScale(p.v));
      dots.exit().remove();
    }
  });
  const activeMarkerClasses = new Set(compareSeries.map(s => `compare-markers-${_safeId(s.name)}`));
  overlay.selectAll('[class^="compare-markers-"]').each(function () {
    if (!activeMarkerClasses.has(this.getAttribute('class'))) d3.select(this).remove();
  });

  let label = overlay.select('.compare-panel-label');
  if (label.empty()) {
    label = overlay.append('text').attr('class', 'panel-label compare-panel-label').attr('x', 4).attr('y', 11);
  }
  label.text(cfg?.name?.trim() || '').attr('fill', 'var(--muted)');  // the graph's name, if it has one
};

const _renderCompareGraphNow = (graph, plotArea) => {
  if (!plotArea) return;
  graph._updateFillRate?.();
  graph._frozen = graph.paused ? (graph._frozen || new Map()) : null;

  const seriesKeys = graph.series.map(compareSeriesKey);
  _processSmooth(graph, seriesKeys);

  // Every series is in the legend: one with nothing to plot is muted there,
  // so it can still be found and removed. A colour follows the series' place
  // in the graph, not how many others have data.
  const compareSeries = [];
  const legendSeries = [];
  const addSeries = (entry, data) => {
    const drawn = data?.length >= 2;
    if (drawn) compareSeries.push({ ...entry, data });
    legendSeries.push(drawn ? entry : { ...entry, _silent: true });
  };
  for (let i = 0; i < graph.series.length; i++) {
    const cmp = graph.series[i];
    const buf = _graphPoints(graph, seriesKeys[i]);
    if (buf) _tagGaps(buf);
    addSeries({
      name: compareSeriesName(cmp),
      color: cmp.color || PLOT_COLORS[i % PLOT_COLORS.length],
      _lineStyle: cmp.lineStyle || 'solid',
      _unit: _seriesUnit(cmp),
    }, buf);
  }

  const _getRawData = (key) => (key ? _graphPoints(graph, key, true) : null);
  (graph.derivedSeries || []).forEach((d, di) => {
    const dataA = _getRawData(d.sourceA);
    const dataB = _getRawData(d.sourceB);
    const ready = dataA?.length >= 2 && (DERIVED_TYPES[d.type]?.sources !== 2 || dataB?.length >= 2);
    const outputs = ready ? _computeDerived(d.type, dataA, dataB, d.window) : [];
    const entry = {
      color: d.color || PLOT_COLORS[(graph.series.length + di) % PLOT_COLORS.length],
      _derived: true, _derivedId: d.id, _lineStyle: d.lineStyle || 'dashed',
    };
    const names = d.type === 'min_max' ? _derivedMinMaxLabels(d) : [_derivedLabel(d)];
    names.forEach((name, i) => {
      if (outputs[i]) _tagGaps(outputs[i]);
      addSeries({ ...entry, name }, outputs[i]);
    });
  });

  const visibleSeries = compareSeries.filter(s => !graph._hidden.has(s.name));
  const card = plotArea.closest('.compare-graph-card');
  if (card) {
    _syncKept(graph, card);
    _syncMarks(graph, card);
  }

  if (!legendSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">Add subjects and attributes to compare</div>';
    graph._fingerprint = '';
    return;
  }

  // A live graph draws at least once a second, data or not: its time axis
  // goes on, and its note follows the bus.
  const problem = state.replayActive ? null : connectionProblem('plot');
  const liveKey = graph.paused ? '' : `${Math.floor(Date.now() / 1000)}:${problem?.message || ''}`;
  const lastPts = compareSeries.map(s => s.data.length ? s.data[s.data.length - 1].t : 0);
  const hiddenKey = [...graph._hidden].sort().join(',');
  const thKey = (graph.thresholds || []).map(t => `${t.value}:${t.label || ''}:${t.color || ''}:${t.style || ''}`).join(';');
  const mkKey = (graph.markers || []).map(m => `${m.t}:${m.label}:${m.color || ''}:${m.lineStyle || ''}`).join(';');
  const dwKey = (graph.drawings || []).length;
  const styleKey = compareSeries.map(s => s._lineStyle || '').join(',');
  const fp = `cg:${graph.id}:${compareSeries.length}:${lastPts.join(',')}:w${graph.timeWindow}:p${graph.paused ? graph.pausedAt : 0}:s${graph.smooth}:d${graph.disconnectPoints}:k${graph.stroke}:g${graph.grid}:t${thKey}:m${mkKey}:dw${dwKey}:h${hiddenKey}:ls${styleKey}:z${graph._zoom || 1}:pan${graph._panOffset || 0}:l${liveKey}:n${graph.name}`;
  const rect = plotArea.getBoundingClientRect();
  const sizeKey = `${Math.round(rect.width)}x${Math.round(rect.height)}`;
  const fullFp = `${fp}:${sizeKey}`;
  if (fullFp === graph._fingerprint) return;
  graph._fingerprint = fullFp;

  const opts = {
    cfg: graph,
    noControls: true,
    invalidate: () => { graph._fingerprint = ''; },
    rerender: () => _renderCompareGraphNow(graph, plotArea),
    restart: () => _renderOneGraph(graph),
  };

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels') || !plotArea.querySelector('.plot-header')) {
    gNode = setupPlotSvg(plotArea, PLOT_MARGIN, opts);
  }

  const w = rect.width - PLOT_MARGIN.left - PLOT_MARGIN.right;
  const headerEl = plotArea.querySelector('.plot-header');
  const HEADER_H = headerEl ? Math.max(28, Math.ceil(headerEl.getBoundingClientRect().height)) : 28;
  const totalPanelsH = rect.height - PLOT_MARGIN.top - PLOT_MARGIN.bottom - HEADER_H;
  if (w < 40 || totalPanelsH < 40) return;

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) {
    svgEl.setAttribute('height', String(rect.height - HEADER_H));
    // Each plot announced by its graph's name and series, not one name for all.
    const about = `${graph.name.trim() || 'Untitled graph'}: ${legendSeries.map((s) => s.name).join(', ') || 'no series'}`;
    if (svgEl.getAttribute('aria-label') !== about) svgEl.setAttribute('aria-label', about);
  }

  const { xScale, panelH } = computePlotScales([], w, totalPanelsH, visibleSeries, graph);

  const g = d3.select(gNode);
  g.select('.plot-panels').selectAll('*').remove();
  _renderCompareOverlay(g, visibleSeries, xScale, panelH, w, graph);

  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  g.select('.plot-x-axis').attr('transform', `translate(0, ${totalPanelsH})`).call(xAxis);
  g.select('.plot-overlay').attr('width', w).attr('height', totalPanelsH);
  g.select('.plot-crosshair').attr('y1', 0).attr('y2', totalPanelsH);

  // Zoomed, moved, reset or paused on its plot, a synced graph takes the others with it.
  bindPlotTooltip(g, plotArea, visibleSeries, xScale, w, HEADER_H, rect, graph, () => {
    _shareView(graph);
    _renderOneGraph(graph);
  });
  setPlotNote(plotArea, _compareNote(problem, compareSeries, xScale));
  // The legend's values: each series' last, lowest and highest in view.
  const [tLeft, tRight] = xScale.domain();
  const statsByName = new Map(compareSeries.map((s) => [s.name, _statsInView(s.data, tLeft, tRight)]));
  for (const entry of legendSeries) entry._stats = statsByName.get(entry.name) ?? null;
  if (graph.thresholds?.length) {
    for (let i = 0; i < graph.thresholds.length; i++) {
      const th = graph.thresholds[i];
      legendSeries.push({
        name: thresholdName(th),
        color: th.color || 'var(--error)',
        _threshold: true,
        _thresholdIdx: i,
        _lineStyle: th.style || 'dashed',
      });
    }
  }
  updatePlotLegend(plotArea, legendSeries, graph._hidden);
};

let _compareAnimTimer = null;

// The cards in view: the timer draws only those, as drawing costs. A card
// scrolled back into view catches up at the next tick.
const _cardsInView = new WeakSet();
const _cardWatcher = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting) _cardsInView.add(entry.target);
    else _cardsInView.delete(entry.target);
  }
});

const _renderOneGraph = (graph, inViewOnly = false) => {
  const container = el('compareContainer');
  const card = container?.querySelector(`[data-graph-id="${graph.id}"]`);
  if (!card || (inViewOnly && !_cardsInView.has(card))) return;
  const plotArea = card.querySelector('.detail-plot-area');
  if (plotArea) _renderCompareGraphNow(graph, plotArea);
};

const _allComparePaused = () => state.compareGraphs.length > 0 && state.compareGraphs.every((g) => g.paused);

// Pause All says what a click on it does, however the graphs got paused: by
// it, by their own buttons, by a click on their plots.
const _syncPauseAll = () => {
  const btn = document.querySelector('.compare-pause-all');
  const label = _allComparePaused() ? 'Resume All' : 'Pause All';
  if (btn && btn.textContent !== label) btn.textContent = label;
};

// The empty tab's words, while there is no graph, however the last one went.
const _syncCompareEmpty = () => {
  document.querySelector('#compareContainer .compare-empty')?.classList.toggle('hidden', state.compareGraphs.length > 0);
};

// A series is quiet when its node no longer sends it, by the rule the
// tables call a subject silent (isEventFresh).
const _seriesQuiet = (s) => !isEventFresh(s.nodeId == null
  ? state.latestBySubject.get(s.subjectId)
  : state.latestByNode.get(s.nodeId)?.get(s.subjectId));

// What the strip over the graphs counts: quiet series (unusual), paused graphs.
const _COMPARE_STATUS = [
  { key: 'quiet', level: 'warn', label: 'series quiet', count: (g) => g.series.filter(_seriesQuiet).length },
  { key: 'paused', level: '', label: 'paused', count: (g) => (g.paused ? 1 : 0) },
];

// The strip: how many graphs and series, then what needs a look, else that
// nothing does; empty (hidden) with no graph, where the tab says how to start.
const _syncCompareStatus = () => {
  const strip = document.querySelector('#compareContainer .compare-status');
  if (!strip) return;
  const graphs = state.compareGraphs;
  const fresh = document.createElement('div');
  if (graphs.length) {
    const series = graphs.reduce((n, g) => n + g.series.length, 0);
    const counts = _COMPARE_STATUS.map((k) => ({ ...k, n: graphs.reduce((sum, g) => sum + k.count(g), 0) }))
      .filter((k) => k.n);
    fresh.innerHTML = `<span class="table-status-total">${graphs.length} graph${graphs.length === 1 ? '' : 's'} · ${series} series</span>`
      + (counts.length
        ? counts.map((k) => `<button type="button" class="table-chip${k.level ? ` table-chip--${k.level}` : ''}"`
          + ` data-focus="${k.key}">${k.n} ${k.label}</button>`).join('')
        : '<span class="table-status-usual">nothing unusual</span>');
  }
  patchChildren(strip, fresh);
};

let _seriesListsRefreshed = 0;

// Runs while the tab is open, paused graphs or not: a graph resumed on its
// own moves on at the next tick.
const _compareAnimTick = () => {
  if (state.activeView !== 'compare') { _compareAnimTimer = null; return; }
  for (const graph of state.compareGraphs) {
    if (!graph.paused) _renderOneGraph(graph, true);
  }
  _syncPauseAll();
  _syncCompareEmpty();
  _syncCompareStatus();
  // An open list of series follows what is heard, once a second.
  if (Date.now() - _seriesListsRefreshed >= 1000) {
    _seriesListsRefreshed = Date.now();
    for (const editor of document.querySelectorAll('#compareContainer .compare-card-editor:not(.hidden)')) {
      editor.querySelector('.plot-compare-panel')?._refreshSeriesList?.();
    }
  }
  _compareAnimTimer = window.setTimeout(_compareAnimTick, PLOT_TICK_MS);
};

const startCompareAnim = () => {
  for (const graph of state.compareGraphs) {
    if (graph.paused) _renderOneGraph(graph);
  }
  _syncPauseAll();
  _syncCompareEmpty();
  _syncCompareStatus();
  if (!_compareAnimTimer) {
    _compareAnimTimer = window.setTimeout(_compareAnimTick, PLOT_TICK_MS);
  }
};

const stopCompareAnim = () => {
  if (_compareAnimTimer) {
    clearTimeout(_compareAnimTimer);
    _compareAnimTimer = null;
  }
  for (const graph of state.compareGraphs) graph._fingerprint = '';
};
