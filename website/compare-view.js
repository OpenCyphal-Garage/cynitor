// Compare view — multiple independent graphs, each with its own series,
// controls and animation. Uses plot.js utilities for rendering.

let _compareGraphIdCounter = 0;
const _nextGraphId = () => `cg_${++_compareGraphIdCounter}`;

const _newGraph = (preset = null) => ({
  id: _nextGraphId(),
  name: preset?.name || '',
  series: preset?.series ? preset.series.map(s => ({ ...s })) : [],
  paused: false,
  pausedAt: null,
  timeWindow: 60,
  smooth: 0,
  stroke: 1.5,
  disconnectPoints: false,
  _timer: null,
  _fingerprint: '',
  _hidden: new Set(),
});

const initCompareView = () => {
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
    _startGraphAnim(graph, card.querySelector('.detail-plot-area'));
  });
  toolbar.appendChild(addBtn);

  const savedWrap = document.createElement('div');
  savedWrap.className = 'compare-saved-wrap';
  const savedBtn = document.createElement('button');
  savedBtn.className = 'compare-saved-btn';
  savedBtn.type = 'button';
  savedBtn.textContent = 'Saved ▾';
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

  document.addEventListener('click', () => savedMenu.classList.add('hidden'));

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
    nameSpan.textContent = `${config.name || 'Untitled'} (${config.series.length})`;
    nameSpan.addEventListener('click', () => {
      menu.classList.add('hidden');
      const graph = _newGraph(config);
      state.compareGraphs.push(graph);
      saveSettings();
      const card = _buildGraphCard(graph);
      cardsContainer.appendChild(card);
      _startGraphAnim(graph, card.querySelector('.detail-plot-area'));
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

  const header = document.createElement('div');
  header.className = 'compare-graph-header';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'compare-graph-name';
  nameInput.placeholder = 'Untitled';
  nameInput.value = graph.name;
  nameInput.addEventListener('input', () => {
    graph.name = nameInput.value;
    saveSettings();
  });
  header.appendChild(nameInput);

  const saveBtn = document.createElement('button');
  saveBtn.className = 'compare-graph-save';
  saveBtn.type = 'button';
  saveBtn.textContent = 'Save';
  saveBtn.setAttribute('aria-label', 'Save configuration');
  saveBtn.addEventListener('click', () => {
    const name = graph.name.trim() || 'Untitled';
    const config = { name, series: graph.series.map(s => ({ ...s })) };
    const existing = state.savedCompareConfigs.findIndex(c => c.name === name);
    if (existing !== -1) state.savedCompareConfigs[existing] = config;
    else state.savedCompareConfigs.push(config);
    saveSettings();
    showToast(`Saved "${name}"`, 'info', 2000);
  });
  header.appendChild(saveBtn);

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'compare-graph-delete';
  deleteBtn.type = 'button';
  deleteBtn.textContent = '×';
  deleteBtn.setAttribute('aria-label', 'Remove graph');
  deleteBtn.addEventListener('click', () => {
    _stopGraphAnim(graph);
    const idx = state.compareGraphs.indexOf(graph);
    if (idx !== -1) state.compareGraphs.splice(idx, 1);
    saveSettings();
    card.remove();
  });
  header.appendChild(deleteBtn);
  card.appendChild(header);

  const layout = document.createElement('div');
  layout.className = 'compare-layout';

  const plotArea = document.createElement('div');
  plotArea.className = 'detail-plot-area';
  layout.appendChild(plotArea);

  const onUpdate = () => {
    graph._fingerprint = '';
    saveSettings();
    _updateComparePanelList(panel, graph, onUpdate);
    _renderCompareGraphNow(graph, plotArea);
  };

  const panel = buildComparePanel(graph, onUpdate);
  layout.appendChild(panel);
  card.appendChild(layout);

  _refreshCompareSubjects(panel);
  _updateComparePanelList(panel, graph, onUpdate);

  return card;
};

const _renderCompareGraphNow = (graph, plotArea) => {
  if (!plotArea) return;

  const compareSeries = [];
  for (const cmp of graph.series) {
    const key = `${cmp.subjectId}:${cmp.attribute}`;
    const buf = state.subjectHistory.get(key);
    if (buf && buf.length >= 2) {
      compareSeries.push({
        name: `S${cmp.subjectId} · ${cmp.attribute}`,
        data: buf,
        color: cmp.color || PLOT_COLORS[compareSeries.length % PLOT_COLORS.length],
      });
    }
  }

  const visibleSeries = compareSeries.filter(s => !graph._hidden.has(s.name));

  if (!compareSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">Add subjects and attributes to compare</div>';
    graph._fingerprint = '';
    return;
  }

  const lastPts = compareSeries.map(s => s.data.length ? s.data[s.data.length - 1].t : 0);
  const hiddenKey = [...graph._hidden].sort().join(',');
  const fp = `cg:${graph.id}:${compareSeries.length}:${lastPts.join(',')}:w${graph.timeWindow}:p${graph.paused ? graph.pausedAt : 0}:s${graph.smooth}:d${graph.disconnectPoints}:k${graph.stroke}:h${hiddenKey}`;
  const rect = plotArea.getBoundingClientRect();
  const sizeKey = `${Math.round(rect.width)}x${Math.round(rect.height)}`;
  const fullFp = `${fp}:${sizeKey}`;
  if (fullFp === graph._fingerprint) return;
  graph._fingerprint = fullFp;

  const opts = {
    cfg: graph,
    invalidate: () => { graph._fingerprint = ''; },
    rerender: () => _renderCompareGraphNow(graph, plotArea),
    restart: () => _startGraphAnim(graph, plotArea),
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
  if (titleEl) {
    const next = graph.name || `Compare · ${compareSeries.length} series`;
    if (titleEl.textContent !== next) titleEl.textContent = next;
  }

  const { xScale, panelH } = computePlotScales([], w, totalPanelsH, visibleSeries, graph);

  const g = d3.select(gNode);
  g.select('.plot-panels').selectAll('*').remove();
  _renderCompareOverlay(g, visibleSeries, xScale, panelH, w, 0, graph);

  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  g.select('.plot-x-axis').attr('transform', `translate(0, ${totalPanelsH})`).call(xAxis);
  g.select('.plot-overlay').attr('width', w).attr('height', totalPanelsH);
  g.select('.plot-crosshair').attr('y1', 0).attr('y2', totalPanelsH);

  bindPlotTooltip(g, plotArea, visibleSeries, xScale, w, HEADER_H, rect);
  updatePlotLegend(plotArea, compareSeries, graph._hidden);
};

const _startGraphAnim = (graph, plotArea) => {
  _stopGraphAnim(graph);
  if (graph.paused) {
    _renderCompareGraphNow(graph, plotArea);
    return;
  }
  const tick = () => {
    if (state.activeView !== 'compare') { graph._timer = null; return; }
    if (graph.paused) { graph._timer = null; return; }
    _renderCompareGraphNow(graph, plotArea);
    graph._timer = window.setTimeout(tick, PLOT_TICK_MS);
  };
  graph._timer = window.setTimeout(tick, PLOT_TICK_MS);
};

const _stopGraphAnim = (graph) => {
  if (graph._timer) {
    clearTimeout(graph._timer);
    graph._timer = null;
  }
  graph._fingerprint = '';
};

const startCompareAnim = () => {
  const container = el('compareContainer');
  for (const graph of state.compareGraphs) {
    const card = container.querySelector(`[data-graph-id="${graph.id}"]`);
    if (!card) continue;
    const plotArea = card.querySelector('.detail-plot-area');
    _startGraphAnim(graph, plotArea);
  }
};

const stopCompareAnim = () => {
  for (const graph of state.compareGraphs) {
    _stopGraphAnim(graph);
  }
};
