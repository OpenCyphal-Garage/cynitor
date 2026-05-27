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

const _newGraph = (preset = null) => ({
  id: _nextGraphId(),
  name: preset?.name || '',
  series: preset?.series ? preset.series.map(s => ({ ...s })) : [],
  thresholds: preset?.thresholds ? preset.thresholds.map(t => ({ ...t })) : [],
  derivedSeries: preset?.derivedSeries ? preset.derivedSeries.map(d => ({ ...d })) : [],
  paused: false,
  pausedAt: null,
  timeWindow: 60,
  smooth: 0,
  stroke: 1.5,
  disconnectPoints: false,
  grid: false,
  _timer: null,
  _fingerprint: '',
  _hidden: new Set(),
  _zoom: 1,
  _panOffset: 0,
});

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
    const graph = _newGraph();
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
  const _updatePauseAllLabel = () => {
    const allPaused = state.compareGraphs.length > 0 && state.compareGraphs.every(g => g.paused);
    pauseAllBtn.textContent = allPaused ? 'Resume All' : 'Pause All';
  };
  _updatePauseAllLabel();
  pauseAllBtn.addEventListener('click', () => {
    const allPaused = state.compareGraphs.length > 0 && state.compareGraphs.every(g => g.paused);
    const now = Date.now() / 1000;
    for (const graph of state.compareGraphs) {
      graph.paused = !allPaused;
      graph.pausedAt = graph.paused ? now : null;
      if (!graph.paused) graph._resumeFrom = now;
    }
    saveSettings();
    if (allPaused) startCompareAnim();
    else stopCompareAnim();
    _updatePauseAllLabel();
    for (const graph of state.compareGraphs) {
      const card = cardsContainer.querySelector(`[data-graph-id="${graph.id}"]`);
      if (!card) continue;
      const btn = card.querySelector('.plot-pause-btn');
      if (btn) {
        btn.textContent = graph.paused ? '▶' : '⏸';
        btn.classList.toggle('active', graph.paused);
      }
      if (!graph.paused) {
        const plotArea = card.querySelector('.detail-plot-area');
        _renderOneGraph(graph);
      }
    }
  });
  toolbar.appendChild(pauseAllBtn);

  const savedWrap = document.createElement('div');
  savedWrap.className = 'compare-saved-wrap';
  const savedBtn = document.createElement('button');
  savedBtn.className = 'compare-saved-btn';
  savedBtn.type = 'button';
  savedBtn.textContent = 'Presets ▾';
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
      graphs: state.compareGraphs.map(g => ({
        name: g.name, series: g.series, thresholds: g.thresholds || [],
        derivedSeries: g.derivedSeries || [],
        timeWindow: g.timeWindow, smooth: g.smooth, stroke: g.stroke,
        disconnectPoints: g.disconnectPoints, grid: g.grid,
      })),
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
        try {
          const data = JSON.parse(reader.result);
          if (!Array.isArray(data.graphs)) throw new Error('Invalid format');
          stopCompareAnim();
          state.compareGraphs.length = 0;
          for (const g of data.graphs) {
            const graph = _newGraph(g);
            graph.timeWindow = g.timeWindow ?? 60;
            graph.smooth = g.smooth ?? 0;
            graph.stroke = g.stroke ?? 1.5;
            graph.disconnectPoints = g.disconnectPoints ?? false;
            graph.grid = g.grid ?? false;
            state.compareGraphs.push(graph);
          }
          if (Array.isArray(data.saved)) state.savedCompareConfigs = data.saved;
          saveSettings();
          cardsContainer.innerHTML = '';
          for (const graph of state.compareGraphs) {
            const card = _buildGraphCard(graph);
            cardsContainer.appendChild(card);
          }
          startCompareAnim();
          showToast('Session imported', 'info', 2000);
        } catch (err) {
          showToast(`Import failed: ${err.message}`, 'error', 3000);
        }
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
    empty.textContent = 'No saved configurations';
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
      const graph = _newGraph(config);
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
    const name = graph.name.trim() || 'Untitled';
    const config = {
      name,
      series: graph.series.map(s => ({ ...s })),
      derivedSeries: (graph.derivedSeries || []).map(d => ({ ...d })),
    };
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
    const clone = _newGraph({
      name: graph.name ? `${graph.name} (copy)` : '',
      series: graph.series,
      derivedSeries: graph.derivedSeries,
    });
    clone.timeWindow = graph.timeWindow;
    clone.smooth = graph.smooth;
    clone.stroke = graph.stroke;
    clone.disconnectPoints = graph.disconnectPoints;
    clone.grid = graph.grid;
    clone.thresholds = graph.thresholds.map(t => ({ ...t }));
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
    invalidate: () => { graph._fingerprint = ''; },
    rerender: () => _renderCompareGraphNow(graph, plotArea),
    restart: () => _renderOneGraph(graph),
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

const _renderCompareGraphNow = (graph, plotArea) => {
  if (!plotArea) return;
  graph._updateFillRate?.();

  const seriesKeys = graph.series.map(cmp => `${cmp.subjectId}:${cmp.attribute}`);
  _processSmooth(graph, seriesKeys);

  const compareSeries = [];
  for (let i = 0; i < graph.series.length; i++) {
    const cmp = graph.series[i];
    const key = seriesKeys[i];
    const buf = _getSmoothBuf(graph, key);
    if (buf && buf.length >= 2) {
      _tagGaps(buf);
      compareSeries.push({
        name: `S${cmp.subjectId} · ${cmp.attribute}`,
        data: buf,
        color: cmp.color || PLOT_COLORS[compareSeries.length % PLOT_COLORS.length],
        _lineStyle: cmp.lineStyle || 'solid',
      });
    }
  }

  if (graph.derivedSeries?.length) {
    const _getRawData = (key) => key ? state.subjectHistory.get(key) : null;
    for (let di = 0; di < graph.derivedSeries.length; di++) {
      const d = graph.derivedSeries[di];
      const dataA = _getRawData(d.sourceA);
      const dataB = _getRawData(d.sourceB);
      if (!dataA || dataA.length < 2) continue;
      if (DERIVED_TYPES[d.type]?.sources === 2 && (!dataB || dataB.length < 2)) continue;
      const win = d.type === 'min_max' ? (graph.timeWindow || 0) : d.window;
      const outputs = _computeDerived(d.type, dataA, dataB, win);
      const baseColor = d.color || PLOT_COLORS[(graph.series.length + di) % PLOT_COLORS.length];
      const style = d.lineStyle || 'dashed';
      if (d.type === 'min_max' && outputs.length === 2) {
        const [minLabel, maxLabel] = _derivedMinMaxLabels(d);
        if (outputs[0].length >= 2) {
          compareSeries.push({ name: minLabel, data: outputs[0], color: baseColor, _derived: true, _derivedId: d.id, _lineStyle: style });
        }
        if (outputs[1].length >= 2) {
          compareSeries.push({ name: maxLabel, data: outputs[1], color: baseColor, _derived: true, _derivedId: d.id, _lineStyle: style });
        }
      } else if (outputs[0]?.length >= 2) {
        _tagGaps(outputs[0]);
        compareSeries.push({ name: _derivedLabel(d), data: outputs[0], color: baseColor, _derived: true, _derivedId: d.id, _lineStyle: style });
      }
    }
  }

  const visibleSeries = compareSeries.filter(s => !graph._hidden.has(s.name));

  if (!compareSeries.length) {
    const msg = graph.series.length ? 'Waiting for data…' : 'Add subjects and attributes to compare';
    plotArea.innerHTML = `<div class="plot-empty">${msg}</div>`;
    graph._fingerprint = '';
    return;
  }

  const lastPts = compareSeries.map(s => s.data.length ? s.data[s.data.length - 1].t : 0);
  const hiddenKey = [...graph._hidden].sort().join(',');
  const thKey = (graph.thresholds || []).map(t => `${t.value}:${t.label || ''}:${t.color || ''}:${t.style || ''}`).join(';');
  const styleKey = compareSeries.map(s => s._lineStyle || '').join(',');
  const fp = `cg:${graph.id}:${compareSeries.length}:${lastPts.join(',')}:w${graph.timeWindow}:p${graph.paused ? graph.pausedAt : 0}:s${graph.smooth}:d${graph.disconnectPoints}:k${graph.stroke}:g${graph.grid}:t${thKey}:h${hiddenKey}:ls${styleKey}:z${graph._zoom || 1}:pan${graph._panOffset || 0}`;
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
  const legendSeries = [...compareSeries];
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

const _compareAnimTick = () => {
  if (state.activeView !== 'compare') { _compareAnimTimer = null; return; }
  for (const graph of state.compareGraphs) {
    if (!graph.paused) _renderOneGraph(graph);
  }
  _compareAnimTimer = window.setTimeout(_compareAnimTick, PLOT_TICK_MS);
};

const startCompareAnim = () => {
  for (const graph of state.compareGraphs) {
    if (graph.paused) _renderOneGraph(graph);
  }
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
