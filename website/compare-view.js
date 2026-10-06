// Compare view — multiple independent graphs, each with its own series,
// controls and animation. Uses plot.js utilities for rendering.

let _compareGraphIdCounter = 0;
const _seedGraphIdCounter = () => {
  for (const g of state.compareGraphs) {
    const m = g.id && g.id.match(/^cg_(\d+)$/);
    if (m) _compareGraphIdCounter = Math.max(_compareGraphIdCounter, Number(m[1]));
  }
};
const _nextGraphId = () => `cg_${++_compareGraphIdCounter}`;

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

const initCompareView = () => {
  _seedGraphIdCounter();
  const container = el('compareContainer');
  if (container.querySelector('.compare-toolbar')) {
    for (const graph of state.compareGraphs) {
      const card = container.querySelector(`[data-graph-id="${graph.id}"]`);
      if (card) {
        const panel = card.querySelector('.plot-compare-panel');
        if (panel) _refreshCompareSubjects(panel);
      }
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
    saveSettings();
    _syncPauseAll();
    for (const graph of state.compareGraphs) {
      const card = cardsContainer.querySelector(`[data-graph-id="${graph.id}"]`);
      if (!card) continue;
      const btn = card.querySelector('.plot-pause-btn');
      if (btn) {
        btn.textContent = graph.paused ? '▶' : '⏸';
        btn.classList.toggle('active', graph.paused);
      }
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
  savedBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _refreshSavedMenu(savedMenu, cardsContainer);
    savedMenu.classList.toggle('hidden');
  });
  savedWrap.appendChild(savedBtn);
  const savedMenu = document.createElement('div');
  savedMenu.className = 'compare-saved-menu hidden';
  savedWrap.appendChild(savedMenu);
  toolbar.appendChild(savedWrap);

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
        cardsContainer.innerHTML = '';
        for (const graph of state.compareGraphs) cardsContainer.appendChild(_buildGraphCard(graph));
        startCompareAnim();
        showToast('Session imported', 'info', 2000);
      };
      reader.readAsText(file);
    });
    input.click();
  });
  toolbar.appendChild(importBtn);

  document.addEventListener('click', () => savedMenu.classList.add('hidden'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') savedMenu.classList.add('hidden');
  });

  container.appendChild(toolbar);

  const cardsContainer = document.createElement('div');
  cardsContainer.className = 'compare-cards';

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
    const nameSpan = document.createElement('span');
    nameSpan.className = 'compare-saved-name';
    nameSpan.textContent = config.name || 'Untitled';
    nameSpan.addEventListener('click', () => {
      menu.classList.add('hidden');
      const graph = newCompareGraph(config);
      state.compareGraphs.push(graph);
      saveSettings();
      const card = _buildGraphCard(graph);
      cardsContainer.appendChild(card);
      _renderOneGraph(graph);
    });
    item.appendChild(nameSpan);
    const delBtn = document.createElement('button');
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

const _buildGraphCard = (graph) => {
  const card = document.createElement('div');
  card.className = 'compare-graph-card';
  card.dataset.graphId = graph.id;

  const plotArea = document.createElement('div');
  plotArea.className = 'detail-plot-area';

  const onUpdate = () => {
    graph._fingerprint = '';
    saveSettings();
    if (panel._refreshDerivedSources) panel._refreshDerivedSources();
    _renderCompareGraphNow(graph, plotArea);
  };

  // Zone 1: Series panel + card actions (name, save, clone, delete)
  const panel = buildComparePanel(graph, onUpdate);

  const cardActions = document.createElement('div');
  cardActions.className = 'compare-card-actions';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'compare-graph-name';
  nameInput.placeholder = 'Untitled';
  nameInput.value = graph.name;
  nameInput.addEventListener('input', () => {
    graph.name = nameInput.value;
    saveSettings();
  });
  cardActions.appendChild(nameInput);

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
    graph._fingerprint = '';
    const idx = state.compareGraphs.indexOf(graph);
    if (idx !== -1) state.compareGraphs.splice(idx, 1);
    saveSettings();
    card.remove();
  });
  cardActions.appendChild(deleteBtn);

  panel.appendChild(cardActions);
  card.appendChild(panel);

  _refreshCompareSubjects(panel);

  // Zone 3 + 4: Time controls + visual tuning (split from buildPlotControls)
  const opts = {
    cfg: graph,
    invalidate: () => {
      graph._fingerprint = '';
      if (graph.paused && !graph._frozen) _renderOneGraph(graph);  // just paused: keeps what it shows
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
  card.appendChild(timeControls);
  card.appendChild(visualControls);

  // Zone 5: Plot area
  card.appendChild(plotArea);

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
    }, buf);
  }

  const _getRawData = (key) => (key ? _graphPoints(graph, key, true) : null);
  (graph.derivedSeries || []).forEach((d, di) => {
    const dataA = _getRawData(d.sourceA);
    const dataB = _getRawData(d.sourceB);
    const ready = dataA?.length >= 2 && (DERIVED_TYPES[d.type]?.sources !== 2 || dataB?.length >= 2);
    const win = d.type === 'min_max' ? (graph.timeWindow || 0) : d.window;
    const outputs = ready ? _computeDerived(d.type, dataA, dataB, win) : [];
    const entry = {
      color: d.color || PLOT_COLORS[(graph.series.length + di) % PLOT_COLORS.length],
      _derived: true, _derivedId: d.id, _lineStyle: d.lineStyle || 'dashed',
    };
    if (d.type === 'min_max') {
      // Two flat lines across the window: no gaps to mark.
      _derivedMinMaxLabels(d).forEach((name, i) => addSeries({ ...entry, name }, outputs[i]));
    } else {
      if (outputs[0]) _tagGaps(outputs[0]);
      addSeries({ ...entry, name: _derivedLabel(d) }, outputs[0]);
    }
  });

  const visibleSeries = compareSeries.filter(s => !graph._hidden.has(s.name));

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
  const fp = `cg:${graph.id}:${compareSeries.length}:${lastPts.join(',')}:w${graph.timeWindow}:p${graph.paused ? graph.pausedAt : 0}:s${graph.smooth}:d${graph.disconnectPoints}:k${graph.stroke}:g${graph.grid}:t${thKey}:m${mkKey}:dw${dwKey}:h${hiddenKey}:ls${styleKey}:z${graph._zoom || 1}:pan${graph._panOffset || 0}:l${liveKey}`;
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
    plotArea.dataset.plotView = 'compare';
  }

  const w = rect.width - PLOT_MARGIN.left - PLOT_MARGIN.right;
  const headerEl = plotArea.querySelector('.plot-header');
  const HEADER_H = headerEl ? Math.max(28, Math.ceil(headerEl.getBoundingClientRect().height)) : 28;
  const totalPanelsH = rect.height - PLOT_MARGIN.top - PLOT_MARGIN.bottom - HEADER_H;
  if (w < 40 || totalPanelsH < 40) return;

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - HEADER_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) titleEl.style.display = 'none';

  const { xScale, panelH } = computePlotScales([], w, totalPanelsH, visibleSeries, graph);

  const g = d3.select(gNode);
  g.select('.plot-panels').selectAll('*').remove();
  _renderCompareOverlay(g, visibleSeries, xScale, panelH, w, 0, graph);

  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  g.select('.plot-x-axis').attr('transform', `translate(0, ${totalPanelsH})`).call(xAxis);
  g.select('.plot-overlay').attr('width', w).attr('height', totalPanelsH);
  g.select('.plot-crosshair').attr('y1', 0).attr('y2', totalPanelsH);

  bindPlotTooltip(g, plotArea, visibleSeries, xScale, w, HEADER_H, rect, graph, () => _renderOneGraph(graph));
  setPlotNote(plotArea, _compareNote(problem, compareSeries, xScale));
  if (graph.thresholds?.length) {
    for (let i = 0; i < graph.thresholds.length; i++) {
      const th = graph.thresholds[i];
      legendSeries.push({
        name: `${th.label || th.value}`,
        color: th.color || '#ef4444',
        _threshold: true,
        _thresholdIdx: i,
        _lineStyle: th.style || 'dashed',
      });
    }
  }
  updatePlotLegend(plotArea, legendSeries, graph._hidden);
};

let _compareAnimTimer = null;

const _renderOneGraph = (graph) => {
  const container = el('compareContainer');
  const card = container?.querySelector(`[data-graph-id="${graph.id}"]`);
  if (!card) return;
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

// Runs while the tab is open, paused graphs or not: a graph resumed on its
// own moves on at the next tick.
const _compareAnimTick = () => {
  if (state.activeView !== 'compare') { _compareAnimTimer = null; return; }
  for (const graph of state.compareGraphs) {
    if (!graph.paused) _renderOneGraph(graph);
  }
  _syncPauseAll();
  _compareAnimTimer = window.setTimeout(_compareAnimTick, PLOT_TICK_MS);
};

const startCompareAnim = () => {
  for (const graph of state.compareGraphs) {
    if (graph.paused) _renderOneGraph(graph);
  }
  _syncPauseAll();
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
