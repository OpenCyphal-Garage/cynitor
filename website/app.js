const STORAGE_KEY = 'pycyphal.dashboard.settings.v2';

const state = {
  ws: null,
  wsReconnectTimer: null,
  wsReconnectAttempts: 0,
  userClosedWs: false,
  interfacesTimer: null,
  statusTimer: null,
  canStartupTimer: null,
  nodesTimer: null,
  throughputTimer: null,
  dashboardConnected: false,
  canConnected: false,
  canConnecting: false,
  canDisconnecting: false,
  preferredCanInterface: '',
  favouriteNodeIds: new Set(),
  deletedNodeIds: new Set(),
  eventCount: 0,
  latestNodesPayload: { node_count: 0, nodes: {} },
  latestBySubject: new Map(),
  latestByNode: new Map(),
  selectedNodeId: null,
  selectedDetailTab: 'publishers',
  selectedPlotSubject: null,
  subjectHistory: new Map(),
  hiddenPlotSeries: new Set(),
  plotTimer: null,
  wsBytesAccum: 0,
  wsThroughput: 0,
  busUtilization: null,
  tableSort: { key: 'id', dir: 'asc' },
  sidebarCollapsed: false,
  detailPanelHeight: null,
  detailPanelCollapsed: false,
  splitRatio: 0.6,
};

const el = (id) => document.getElementById(id);

const escapeHtml = (value) =>
  String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

const formatUptime = (seconds) => {
  if (seconds == null) return '-';
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return '-';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return `${d}d ${h}h ${m}m ${sec}s`;
};

const formatLastSeen = (lastSeen) => {
  if (!Array.isArray(lastSeen) || !lastSeen.length) return '-';
  const ts = new Date(lastSeen[lastSeen.length - 1]);
  if (Number.isNaN(ts.getTime())) return '-';
  const ago = Math.floor((Date.now() - ts.getTime()) / 1000);
  if (ago < 60) return `${ago}s ago`;
  if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
  if (ago < 86400) return `${Math.floor(ago / 3600)}h ago`;
  return `${Math.floor(ago / 86400)}d ago`;
};

const apiBase = () => el('apiBase').value.trim().replace(/\/$/, '');
const wsBase = () => apiBase().replace(/^http/, 'ws');

const readSettings = () => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
};

const saveSettings = () => {
  const interfacesSelect = el('interfacesSelect');
  const persisted = {
    apiBase: el('apiBase').value.trim(),
    canInterface: interfacesSelect ? interfacesSelect.value : '',
    dashboardConnected: state.dashboardConnected,
    nodesRefreshSeconds: el('nodesRefreshSlider').value,
    selectedDetailTab: state.selectedDetailTab,
    tableSort: state.tableSort,
    sidebarCollapsed: state.sidebarCollapsed,
    detailPanelHeight: state.detailPanelHeight,
    detailPanelCollapsed: state.detailPanelCollapsed,
    theme: document.documentElement.getAttribute('data-theme') || 'dark',
    columnWidths: getColumnWidths(),
    headerFilters: getHeaderFilters(),
    selectedNodeId: state.selectedNodeId,
    splitRatio: state.splitRatio,
    favouriteNodeIds: [...state.favouriteNodeIds],
    deletedNodeIds: [...state.deletedNodeIds],
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
};

const getColumnWidths = () => {
  if (!nodesTabulator) return null;
  const widths = {};
  for (const col of nodesTabulator.getColumns()) {
    widths[col.getField()] = col.getWidth();
  }
  return widths;
};

const getHeaderFilters = () => {
  if (!nodesTabulator) return null;
  const filters = {};
  for (const col of nodesTabulator.getColumns()) {
    const field = col.getField();
    const headerEl = col.getElement().querySelector('.tabulator-header-filter input');
    if (headerEl && headerEl.value) {
      filters[field] = headerEl.value;
    }
  }
  return filters;
};

const loadSettings = () => {
  const settings = readSettings();
  if (typeof settings.apiBase === 'string' && settings.apiBase.trim()) {
    el('apiBase').value = settings.apiBase;
  }
  if (settings.tableSort && settings.tableSort.key) {
    state.tableSort = settings.tableSort;
  }
  if (typeof settings.nodesRefreshSeconds === 'string' && settings.nodesRefreshSeconds) {
    const val = Math.max(1, Math.min(60, Number(settings.nodesRefreshSeconds) || 3));
    el('nodesRefreshSlider').value = String(val);
    el('refreshValue').textContent = val >= 60 ? '1m' : `${val}s`;
  }
  if (typeof settings.selectedDetailTab === 'string') {
    const validTabs = ['publishers', 'subscribers', 'servers', 'clients'];
    const tab = settings.selectedDetailTab === 'services' ? 'servers' : settings.selectedDetailTab;
    state.selectedDetailTab = validTabs.includes(tab) ? tab : 'publishers';
  }
  if (settings.sidebarCollapsed) {
    state.sidebarCollapsed = true;
    document.querySelector('.sidebar')?.classList.add('collapsed');
  }
  if (typeof settings.detailPanelHeight === 'number') {
    state.detailPanelHeight = settings.detailPanelHeight;
  }
  if (settings.detailPanelCollapsed) {
    state.detailPanelCollapsed = true;
  }
  if (settings.theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    el('themeLabel').textContent = 'White';
  }
  if (typeof settings.canInterface === 'string') {
    state.preferredCanInterface = settings.canInterface;
  }
  if (settings.dashboardConnected === true) {
    state.pendingReconnect = true;
  }
  if (Array.isArray(settings.favouriteNodeIds)) {
    state.favouriteNodeIds = new Set(settings.favouriteNodeIds);
  }
  if (Array.isArray(settings.deletedNodeIds)) {
    state.deletedNodeIds = new Set(settings.deletedNodeIds);
  }
  if (Number.isInteger(settings.selectedNodeId)) {
    state.selectedNodeId = settings.selectedNodeId;
  }
  if (typeof settings.splitRatio === 'number' && settings.splitRatio > 0.2 && settings.splitRatio < 0.9) {
    state.splitRatio = settings.splitRatio;
  }
};

const formatThroughput = (bytesPerSec) => {
  if (bytesPerSec >= 1_000_000) return `${(bytesPerSec / 1_000_000).toFixed(1)} MB/s`;
  if (bytesPerSec >= 1_000) return `${(bytesPerSec / 1_000).toFixed(1)} KB/s`;
  return `${Math.round(bytesPerSec)} B/s`;
};

const getTotalMessageRate = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes || typeof nodes !== 'object') return 0;
  let total = 0;
  for (const node of Object.values(nodes)) {
    total += getNodeRate(node.node_id);
  }
  return total;
};

const updateSemaphores = () => {
  const serverDot = el('serverSemaphore');
  const canDot = el('canSemaphore');
  const serverInfo = el('serverThroughput');
  const canInfo = el('canUtilization');

  if (serverDot) {
    if (state.dashboardConnected) {
      serverDot.className = 'semaphore ok';
    } else {
      serverDot.className = 'semaphore';
    }
  }

  if (canDot) {
    if (state.canDisconnecting) {
      canDot.className = 'semaphore disconnecting';
    } else if (state.canConnecting) {
      canDot.className = 'semaphore connecting';
    } else if (state.canConnected) {
      canDot.className = 'semaphore ok';
    } else {
      canDot.className = 'semaphore';
    }
  }

  // Lock fields when connected
  el('apiBase').disabled = state.dashboardConnected;
  el('interfacesSelect').disabled = state.canConnected || state.canConnecting;

  if (serverInfo) {
    if (state.dashboardConnected && state.wsThroughput > 0) {
      serverInfo.textContent = formatThroughput(state.wsThroughput);
      serverInfo.classList.remove('hidden');
    } else if (state.dashboardConnected) {
      serverInfo.textContent = '0 B/s';
      serverInfo.classList.remove('hidden');
    } else {
      serverInfo.classList.add('hidden');
    }
  }

  if (canInfo) {
    if (state.canConnected) {
      const rate = getTotalMessageRate();
      const util = state.busUtilization;
      const utilStr = util != null ? ` · ${util}% load` : '';
      canInfo.textContent = `${rate.toFixed(1)} msg/s${utilStr}`;
      canInfo.classList.remove('hidden');
    } else {
      canInfo.classList.add('hidden');
    }
  }
};

const startThroughputTimer = () => {
  if (state.throughputTimer) clearInterval(state.throughputTimer);
  state.throughputTimer = window.setInterval(() => {
    state.wsThroughput = state.wsBytesAccum;
    state.wsBytesAccum = 0;
    updateSemaphores();
  }, 1000);
};

const stopThroughputTimer = () => {
  if (state.throughputTimer) {
    clearInterval(state.throughputTimer);
    state.throughputTimer = null;
  }
  state.wsBytesAccum = 0;
  state.wsThroughput = 0;
};

const withSmartJsonHeaders = (options = {}) => {
  const method = String(options.method || 'GET').toUpperCase();
  const hasBody = options.body !== undefined && options.body !== null;
  const isSimpleMethod = method === 'GET' || method === 'HEAD';
  if (isSimpleMethod && !hasBody) {
    return options;
  }
  return {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  };
};

const requestJson = async (path, options = {}) => {
  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, withSmartJsonHeaders(options));
  } catch (error) {
    throw new Error(`Network error for ${path}: ${error?.message || error}`);
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status} for ${path}`);
  }
  return data;
};

const getStatusClass = (attr, value) => {
  const v = String(value).toUpperCase();
  switch (attr) {
    case 'health':
      if (v === 'NOMINAL' || v === '0') return 'status-ok';
      if (v === 'ADVISORY' || v === '1') return 'status-ok';
      if (v === 'CAUTION' || v === '2') return 'status-warn';
      if (v === 'WARNING' || v === '3') return 'status-err';
      return '';
    case 'mode':
      if (v === 'OPERATIONAL' || v === '0') return 'status-ok';
      if (v === 'INITIALIZATION' || v === '1') return 'status-init';
      return '';
    default:
      return '';
  }
};

const cacheEvent = (event) => {
  if (!event || !Number.isInteger(event.subject_id)) {
    return;
  }

  state.latestBySubject.set(event.subject_id, event);

  if (Number.isInteger(event.publisher_node_id)) {
    if (!state.latestByNode.has(event.publisher_node_id)) {
      state.latestByNode.set(event.publisher_node_id, new Map());
    }
    state.latestByNode.get(event.publisher_node_id).set(event.subject_id, event);
  }

  const now = event.timestamp_unix || (Date.now() / 1000);
  for (const a of event.attributes || []) {
    if (typeof a.value !== 'number') continue;
    const key = `${event.subject_id}:${a.attribute}`;
    if (!state.subjectHistory.has(key)) state.subjectHistory.set(key, []);
    const buf = state.subjectHistory.get(key);
    buf.push({ t: now, v: a.value });
    if (buf.length > 3600) buf.shift();
  }
};

const getSelectedNode = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes || state.selectedNodeId === null) {
    return null;
  }
  return nodes[String(state.selectedNodeId)] || null;
};

const getNodeRate = (nodeId) => {
  const nodes = state.latestNodesPayload?.nodes;
  const node = nodes ? nodes[String(nodeId)] : null;
  if (node && node.has_disappeared) {
    return 0;
  }
  const map = state.latestByNode.get(nodeId);
  if (!map) {
    return 0;
  }
  let total = 0;
  for (const event of map.values()) {
    total += Number(event.rate) || 0;
  }
  return total;
};

const getNodeHealthValue = (nodeId) => {
  const map = state.latestByNode.get(nodeId);
  if (!map) {
    return null;
  }
  for (const event of map.values()) {
    if (!Array.isArray(event.attributes)) {
      continue;
    }
    for (const attr of event.attributes) {
      if (String(attr.attribute).toLowerCase() === 'health') {
        return String(attr.value);
      }
    }
  }
  return null;
};

const getNodeVisualState = (node) => {
  if (!node) {
    return 'idle';
  }
  if (node.has_disappeared) {
    return 'offline';
  }
  const health = getNodeHealthValue(node.node_id);
  if (health && health !== 'NOMINAL') {
    return 'error';
  }
  return getNodeRate(node.node_id) > 0 ? 'active' : 'idle';
};

const setSelectedNode = (nodeId) => {
  const numericNodeId = Number.parseInt(String(nodeId), 10);
  if (!Number.isInteger(numericNodeId)) {
    return;
  }
  state.selectedNodeId = numericNodeId;
  // Immediately highlight the selected row in the table
  if (nodesTabulator) {
    for (const row of nodesTabulator.getRows()) {
      const rowEl = row.getElement();
      if (row.getData().id === numericNodeId) {
        rowEl.classList.add('selected-row');
      } else {
        rowEl.classList.remove('selected-row');
      }
    }
  }
  renderSelectedNodeContent();
};

const clearSelectedNode = () => {
  state.selectedNodeId = null;
  if (nodesTabulator) {
    for (const row of nodesTabulator.getRows()) {
      row.getElement().classList.remove('selected-row');
    }
  }
  renderSelectedNodeContent();
};

const buildSubjectDetailData = (subjectIds, nodeId) => {
  if (!Array.isArray(subjectIds) || !subjectIds.length) {
    return [];
  }
  const perNodeEvents = Number.isInteger(nodeId) ? state.latestByNode.get(nodeId) : null;

  return subjectIds.map((subjectId) => {
    const nodeEvent = perNodeEvents?.get(subjectId);
    const networkEvent = state.latestBySubject.get(subjectId);
    const event = nodeEvent || networkEvent;
    return {
      subjectId,
      messageType: event?.message_type || null,
      rate: event?.rate ?? null,
      attributes: Array.isArray(event?.attributes) ? event.attributes : [],
    };
  });
};

const metricMaxLen = new Map();

const getMetricMinWidth = (subjectId, attr, displayStr) => {
  const key = `${subjectId}:${attr}`;
  const prev = metricMaxLen.get(key) || 0;
  const len = Math.max(prev, displayStr.length);
  metricMaxLen.set(key, len);
  return len;
};

const renderMetric = (a, subjectId) => {
  const statusCls = getStatusClass(a.attribute, a.value);
  const rawStr = String(a.value ?? '-');
  const displayStr = typeof a.value === 'number' && !Number.isInteger(a.value)
    ? a.value.toFixed(2) : rawStr;
  const minW = getMetricMinWidth(subjectId, a.attribute, displayStr);
  const unitStr = a.unit ? `<span class="metric-unit">${escapeHtml(a.unit)}</span>` : '';
  const valCls = `metric-val${statusCls ? ' ' + statusCls : ''}`;
  return `<span class="metric" data-subject="${subjectId}" data-attr="${escapeHtml(a.attribute)}"><span class="metric-key">${escapeHtml(a.attribute)}</span><span class="${valCls}" title="${escapeHtml(rawStr)}"><span class="metric-val-text" style="min-width:${minW}ch">${escapeHtml(displayStr)}</span>${unitStr}</span></span>`;
};

const renderSubjectTable = (title, subjects) => {
  if (!subjects.length) {
    return `<div class="subject-cards"><div class="detail-empty">No ${title.toLowerCase()} discovered.</div></div>`;
  }
  const cards = subjects.map((s) => {
    const rateStr = s.rate != null ? `${s.rate} Hz` : '';
    const liveDot = s.rate != null && s.rate > 0
      ? '<span class="live-dot"></span>' : '';
    const metrics = s.attributes.length
      ? `<div class="card-metrics">${s.attributes.map((a) => renderMetric(a, s.subjectId)).join('')}</div>`
      : '<div class="card-metrics"><span class="metrics-empty">no telemetry data</span></div>';

    return `<div class="subject-card" data-subject="${s.subjectId}">
      <div class="card-header">
        <span class="card-subject-id">${escapeHtml(String(s.subjectId))}</span>
        <span class="card-type" title="${escapeHtml(s.messageType || '')}">${escapeHtml(s.messageType || 'awaiting data')}</span>
        <span class="card-rate">${liveDot}${escapeHtml(rateStr)}</span>
      </div>
      ${metrics}
    </div>`;
  }).join('');

  return `<div class="subject-cards">${cards}</div>`;
};

const updateSubjectTableInPlace = (container, subjects) => {
  const wrap = container.querySelector('.subject-cards');
  if (!wrap) return false;

  const existingIds = [...wrap.querySelectorAll('.subject-card')].map((c) => c.dataset.subject);
  const newIds = subjects.map((s) => String(s.subjectId));
  if (existingIds.length !== newIds.length || existingIds.some((id, i) => id !== newIds[i])) {
    return false;
  }

  for (const s of subjects) {
    for (const a of s.attributes) {
      const metric = wrap.querySelector(`.metric[data-subject="${s.subjectId}"][data-attr="${a.attribute}"]`);
      if (!metric) return false;

      const valEl = metric.querySelector('.metric-val');
      const valText = valEl?.querySelector('.metric-val-text');
      if (!valText) return false;

      const rawStr = String(a.value ?? '-');
      const displayStr = typeof a.value === 'number' && !Number.isInteger(a.value)
        ? a.value.toFixed(2) : rawStr;

      if (valText.textContent !== displayStr) {
        const minW = getMetricMinWidth(s.subjectId, a.attribute, displayStr);
        valText.textContent = displayStr;
        valText.style.minWidth = minW + 'ch';
        valEl.title = rawStr;

        const statusCls = getStatusClass(a.attribute, a.value);
        valEl.classList.remove('status-ok', 'status-warn', 'status-err', 'status-init');
        if (statusCls) valEl.classList.add(statusCls);
      }
    }
  }
  return true;
};

const PLOT_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39d2c0'];

const formatPlotTime = (unix) => {
  const d = new Date(unix * 1000);
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
};

const PLOT_STALE_THRESHOLD = 3;

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    return;
  }

  const allSeries = [];
  for (const [key, buf] of state.subjectHistory) {
    if (!key.startsWith(sid + ':')) continue;
    if (buf.length < 2) continue;
    allSeries.push({ name: key.split(':')[1], data: buf });
  }

  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    return;
  }

  const visible = allSeries.filter((s) => !state.hiddenPlotSeries.has(s.name));

  const margin = { top: 8, right: 12, bottom: 24, left: 48 };
  const rect = plotArea.getBoundingClientRect();
  const w = rect.width - margin.left - margin.right;
  const h = rect.height - margin.top - margin.bottom - 28;
  if (w < 40 || h < 40) return;

  let allMin = Infinity, allMax = -Infinity, tDataMin = Infinity, tDataMax = -Infinity;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.v < allMin) allMin = p.v;
      if (p.v > allMax) allMax = p.v;
      if (p.t < tDataMin) tDataMin = p.t;
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  if (!isFinite(allMin)) { allMin = 0; allMax = 1; tDataMin = Date.now() / 1000 - 60; tDataMax = Date.now() / 1000; }
  if (allMin === allMax) { allMin -= 1; allMax += 1; }
  const pad = (allMax - allMin) * 0.05;
  allMin -= pad;
  allMax += pad;

  const WINDOW_SECS = 60;
  const now = Date.now() / 1000;
  const dataIsLive = (now - tDataMax) < PLOT_STALE_THRESHOLD;
  const tRight = dataIsLive ? now : tDataMax;
  const tWindowStart = tRight - WINDOW_SECS;
  const tWindowEnd = tRight + WINDOW_SECS * 0.5;

  const xScale = d3.scaleLinear().domain([tWindowStart, tWindowEnd]).range([0, w]);
  const yScale = d3.scaleLinear().domain([allMin, allMax]).range([h, 0]);

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode) {
    plotArea.innerHTML = '';
    const svgEl = d3.select(plotArea).append('svg')
      .attr('width', '100%').attr('height', rect.height - 28);
    const clipId = 'plot-clip-' + Date.now();
    svgEl.append('defs').append('clipPath').attr('id', clipId)
      .append('rect').attr('width', w).attr('height', h);
    const g = svgEl.append('g').attr('class', 'plot-root')
      .attr('transform', `translate(${margin.left},${margin.top})`);
    g.append('g').attr('class', 'plot-x-axis').attr('transform', `translate(0,${h})`);
    g.append('g').attr('class', 'plot-y-axis');
    g.append('g').attr('class', 'plot-lines').attr('clip-path', `url(#${clipId})`);
    gNode = g.node();
  }

  const g = d3.select(gNode);
  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  const yAxis = d3.axisLeft(yScale).ticks(5);

  g.select('.plot-x-axis').call(xAxis);
  g.select('.plot-y-axis').call(yAxis);

  const line = d3.line()
    .x((d) => xScale(d.t))
    .y((d) => yScale(d.v))
    .curve(d3.curveMonotoneX);

  const linesG = g.select('.plot-lines');
  const paths = linesG.selectAll('path').data(visible, (d) => d.name);
  paths.enter().append('path')
    .attr('fill', 'none')
    .attr('stroke-width', 1.5)
    .merge(paths)
    .attr('stroke', (_, i) => PLOT_COLORS[i % PLOT_COLORS.length])
    .attr('d', (d) => line(d.data));
  paths.exit().remove();

  let legend = plotArea.querySelector('.plot-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.className = 'plot-legend';
    legend.addEventListener('change', (e) => {
      const cb = e.target.closest('input[type="checkbox"]');
      if (!cb) return;
      if (cb.checked) {
        state.hiddenPlotSeries.delete(cb.dataset.series);
      } else {
        state.hiddenPlotSeries.add(cb.dataset.series);
      }
    });
    plotArea.appendChild(legend);
  }
  legend.innerHTML = allSeries.map((s, i) => {
    const checked = !state.hiddenPlotSeries.has(s.name) ? ' checked' : '';
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    return `<label class="plot-legend-item"><input type="checkbox" data-series="${escapeHtml(s.name)}"${checked}><span class="plot-legend-swatch" style="background:${color}"></span>${escapeHtml(s.name)}</label>`;
  }).join('');

  return dataIsLive;
};

const PLOT_TICK_MS = 100;

const stopPlotAnim = () => {
  if (state.plotTimer) {
    clearTimeout(state.plotTimer);
    state.plotTimer = null;
  }
};

const startPlotAnim = () => {
  stopPlotAnim();
  const container = el('selectedNodeContent');
  const tick = () => {
    const isLive = renderPlot(container);
    if (isLive) {
      state.plotTimer = window.setTimeout(tick, PLOT_TICK_MS);
    } else {
      state.plotTimer = null;
    }
  };
  state.plotTimer = window.setTimeout(tick, PLOT_TICK_MS);
};

const bindSplitHandle = (splitEl) => {
  const handle = splitEl.querySelector('.detail-split-handle');
  if (!handle) return;
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const list = splitEl.querySelector('.detail-subject-list');
    const startX = e.clientX;
    const startW = list.getBoundingClientRect().width;
    const totalW = splitEl.getBoundingClientRect().width;

    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      const ratio = Math.min(0.85, Math.max(0.25, (startW + dx) / totalW));
      list.style.flex = `0 0 ${(ratio * 100).toFixed(1)}%`;
      state.splitRatio = ratio;
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      saveSettings();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
};

const buildServicesDetailItems = (services) => {
  if (!Array.isArray(services) || !services.length) {
    return ['No services advertised'];
  }
  return services.map((serviceId) => `Service ${serviceId}`);
};

const renderListTab = (title, items) => `
  <section class="details-panel">
    <h3>${escapeHtml(title)}</h3>
    <ul class="details-list">
      ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
    </ul>
  </section>
`;

const buildClientsDetailItems = (clients) => {
  if (!Array.isArray(clients) || !clients.length) {
    return ['No clients advertised'];
  }
  return clients.map((clientId) => `Client ${clientId}`);
};

const renderSelectedNodeContent = () => {
  const content = el('selectedNodeContent');
  const node = getSelectedNode();

  document.querySelectorAll('.detail-tab').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === state.selectedDetailTab);
  });

  const tabCounts = node ? {
    publishers: (node.publishers || []).length,
    subscribers: (node.subscribers || []).length,
    servers: (node.servers || []).length,
    clients: (node.clients || []).length,
  } : {};
  document.querySelectorAll('.detail-tab').forEach((button) => {
    const count = tabCounts[button.dataset.tab];
    let badge = button.querySelector('.tab-count');
    if (count != null) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'tab-count';
        button.appendChild(badge);
      }
      badge.textContent = count;
    } else if (badge) {
      badge.remove();
    }
  });

  if (!node) {
    stopPlotAnim();
    content.innerHTML = '<div class="detail-empty">Select a node to inspect details.</div>';
    return;
  }

  const renderSubjectTab = (title, subjects) => {
    if (updateSubjectTableInPlace(content, subjects)) {
      if (!state.plotTimer) startPlotAnim();
      return;
    }
    const selected = state.selectedPlotSubject;
    const pct = (state.splitRatio * 100).toFixed(1);
    content.innerHTML = `<div class="detail-split">
      <div class="detail-subject-list" style="flex:0 0 ${pct}%">${renderSubjectTable(title, subjects)}</div>
      <div class="detail-split-handle"></div>
      <div class="detail-plot-area">${''}
      </div>
    </div>`;
    content.querySelectorAll('.subject-card').forEach((card) => {
      if (Number(card.dataset.subject) === selected) card.classList.add('selected');
    });
    bindSplitHandle(content.querySelector('.detail-split'));
    startPlotAnim();
  };

  if (state.selectedDetailTab === 'publishers') {
    renderSubjectTab('Publishers', buildSubjectDetailData(node.publishers || [], node.node_id));
    return;
  }

  if (state.selectedDetailTab === 'subscribers') {
    renderSubjectTab('Subscribers', buildSubjectDetailData(node.subscribers || [], null));
    return;
  }

  if (state.selectedDetailTab === 'servers') {
    stopPlotAnim();
    content.innerHTML = renderListTab('Servers', buildServicesDetailItems(node.servers || []));
    return;
  }

  if (state.selectedDetailTab === 'clients') {
    stopPlotAnim();
    content.innerHTML = renderListTab('Clients', buildClientsDetailItems(node.clients || []));
    return;
  }

  renderSubjectTab('Publishers', buildSubjectDetailData(node.publishers || [], node.node_id));
};

const scheduleReconnect = () => {
  if (state.userClosedWs) {
    return;
  }
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
  }

  state.wsReconnectAttempts += 1;
  const delaySeconds = Math.min(30, 2 ** Math.min(state.wsReconnectAttempts, 5));
  state.wsReconnectTimer = window.setTimeout(() => connectWs(), delaySeconds * 1000);
};

const connectWs = () => {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  state.userClosedWs = false;
  state.ws = new WebSocket(`${wsBase()}/ws`);

  state.ws.onopen = () => {
    state.wsReconnectAttempts = 0;
    updateSemaphores();
  };

  state.ws.onclose = () => {
    updateSemaphores();
    if (state.userClosedWs) {
      return;
    }
    scheduleReconnect();
  };

  state.ws.onerror = () => {
    updateSemaphores();
  };

  state.ws.onmessage = (message) => {
    try {
      state.wsBytesAccum += (message.data?.length || 0);
      const event = JSON.parse(message.data);
      if (event.type === 'filter_updated' || event.type === 'pong') {
        return;
      }
      if (event.type === 'metrics') {
        state.busUtilization = event.bus_utilization ?? null;
        return;
      }
      cacheEvent(event);
      state.eventCount += 1;
      el('eventCount').textContent = String(state.eventCount);
      renderSelectedNodeContent();
      scheduleTableRefresh();
    } catch {
    }
  };
};

const disconnectWs = () => {
  state.userClosedWs = true;
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
};

const updateDashboardConnectButton = () => {
  const button = el('connectDashboardBtn');
  if (!button) {
    return;
  }
  button.textContent = state.dashboardConnected ? 'Disconnect' : 'Connect';
  button.disabled = state.canConnecting || state.canDisconnecting;
  updateSemaphores();
};

const updateCanConnectButton = () => {
  const button = el('connectCanBtn');
  if (!button) {
    return;
  }
  button.textContent = state.canConnected ? 'Disconnect' : 'Connect';
  button.disabled = state.canConnecting || state.canDisconnecting;
  updateSemaphores();
};

const loadInterfaces = async () => {
  try {
    const data = await requestJson('/api/status');
    const interfaces = data.available_interfaces || [];
    const select = el('interfacesSelect');
    const preferredValue = select.value || state.preferredCanInterface;
    select.innerHTML = '';
    for (const iface of interfaces) {
      const option = document.createElement('option');
      option.value = iface;
      option.textContent = iface;
      select.appendChild(option);
    }

    if (interfaces.includes(preferredValue)) {
      select.value = preferredValue;
    } else if (interfaces.length > 0) {
      select.value = interfaces[0];
    }
    state.preferredCanInterface = select.value;
    saveSettings();
    return data;
  } catch (error) {
    if (state.interfacesTimer) {
      clearInterval(state.interfacesTimer);
      state.interfacesTimer = null;
    }
    return null;
  }
};

const startInterfacePolling = () => {
  if (state.interfacesTimer) {
    clearInterval(state.interfacesTimer);
  }
  state.interfacesTimer = window.setInterval(() => {
    if (!state.dashboardConnected || state.canConnected) {
      return;
    }
    loadInterfaces();
  }, 3000);
};

const stopInterfacePolling = () => {
  if (state.interfacesTimer) {
    clearInterval(state.interfacesTimer);
    state.interfacesTimer = null;
  }
};

const selectInterface = async () => {
  const selected = el('interfacesSelect').value;
  if (!selected) {
    return null;
  }

  try {
    const data = await requestJson('/api/can/connect', {
      method: 'POST',
      body: JSON.stringify({ interface: selected }),
    });
    state.preferredCanInterface = selected;
    saveSettings();
    return data;
  } catch (error) {
    return null;
  }
};

const disconnectAll = ({ persist = true } = {}) => {
  state.dashboardConnected = false;
  state.canConnected = false;
  state.busUtilization = null;
  stopStatusPolling();
  stopCanStartupDelay();
  stopNodesPolling();
  stopInterfacePolling();
  stopThroughputTimer();
  stopPlotAnim();
  disconnectWs();
  updateDashboardConnectButton();
  updateCanConnectButton();
  renderNodesTable();
  updateSemaphores();
  if (persist) saveSettings();
};

const pollStatus = async () => {
  if (state.canConnecting || state.canDisconnecting) {
    return;
  }

  let data;
  try {
    data = await requestJson('/api/status');
  } catch {
    if (state.dashboardConnected) {
      disconnectAll();
    }
    return;
  }

  state.busUtilization = data.bus_utilization ?? null;

  const backendCanRunning = data.status === 'running' && !!data.can_interface;

  if (backendCanRunning && !state.canConnected) {
    // Another client connected CAN
    const select = el('interfacesSelect');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = data.can_interface;
    option.textContent = data.can_interface;
    select.appendChild(option);
    select.value = data.can_interface;
    state.preferredCanInterface = data.can_interface;
    state.canConnected = true;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(3000);
    saveSettings();
  } else if (!backendCanRunning && state.canConnected) {
    // CAN disconnected (by another client or due to error)
    state.canConnected = false;
    state.busUtilization = null;
    updateCanConnectButton();
    stopCanStartupDelay();
    stopNodesPolling();
    stopThroughputTimer();
    disconnectWs();
    renderNodesTable();
    saveSettings();
    await loadInterfaces();
    startInterfacePolling();

    if (data.last_error) {
      alert(`CAN disconnected due to error:\n${data.last_error}`);
    }
  }

  updateSemaphores();
};

const startStatusPolling = () => {
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
  }
  state.statusTimer = window.setInterval(pollStatus, 5000);
};

const stopStatusPolling = () => {
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
};

let _tableRefreshPending = null;
const scheduleTableRefresh = () => {
  if (_tableRefreshPending) return;
  _tableRefreshPending = window.setTimeout(() => {
    _tableRefreshPending = null;
    renderNodesTable();
  }, 1000);
};

// ── Tabulator: custom header filter for comma-separated ID lists ──
const idsHeaderFilter = (headerValue, rowValue) => {
  if (!headerValue) return true;
  const terms = headerValue.split(',').map((t) => t.trim()).filter(Boolean);
  if (!terms.length) return true;
  const cellStr = String(rowValue);
  return terms.some((t) => cellStr.includes(t));
};

// ── Tabulator: formatters ──
const stateFormatter = (cell) => {
  const v = cell.getValue();
  return `<span class="state-cell"><span class="state-dot ${escapeHtml(v)}"></span><span class="state-label">${escapeHtml(v)}</span></span>`;
};

const healthFormatter = (cell) => {
  const v = cell.getValue() || '-';
  const cls = v === 'NOMINAL' ? 'nominal'
    : v === 'ADVISORY' ? 'advisory'
    : v === 'CAUTION' ? 'caution'
    : v === 'WARNING' ? 'warning'
    : 'unknown';
  return `<span class="health-text health-${escapeHtml(cls)}">${escapeHtml(v)}</span>`;
};

const portsFormatter = (cell) => {
  const v = cell.getValue();
  const text = Array.isArray(v) && v.length ? v.join(', ') : '-';
  const cls = text !== '-' ? 'has-ports' : '';
  return `<span class="port-ids ${cls}">${escapeHtml(text)}</span>`;
};

const rateFormatter = (cell) => {
  const v = cell.getValue();
  return `${escapeHtml(Number(v).toFixed(1))} Hz`;
};

const favFormatter = (cell) => {
  const isFav = cell.getValue();
  return `<span class="fav-star ${isFav ? 'active' : ''}" aria-label="Toggle favourite">${isFav ? '★' : '☆'}</span>`;
};

const actionsFormatter = (cell) => {
  const row = cell.getRow().getData();
  const cls = row.state === 'offline' ? 'enabled' : 'disabled';
  return `<span class="action-delete ${cls}" aria-label="Remove offline node">✕</span>`;
};

const toggleFavourite = (nodeId) => {
  if (state.favouriteNodeIds.has(nodeId)) {
    state.favouriteNodeIds.delete(nodeId);
  } else {
    state.favouriteNodeIds.add(nodeId);
  }
  saveSettings();
  renderNodesTable();
};

const deleteOfflineNode = (nodeId) => {
  state.deletedNodeIds.add(nodeId);
  saveSettings();
  renderNodesTable();
};

// ── Tabulator: build flat row data from nodes state ──
const buildTableData = () => {
  const payloadNodes = state.latestNodesPayload?.nodes;
  const allNodes = payloadNodes && typeof payloadNodes === 'object' ? Object.values(payloadNodes) : [];
  // If a deleted node comes back online, restore it
  for (const node of allNodes) {
    if (state.deletedNodeIds.has(node.node_id) && getNodeVisualState(node) !== 'offline') {
      state.deletedNodeIds.delete(node.node_id);
    }
  }

  const rows = allNodes
    .filter((node) => !state.deletedNodeIds.has(node.node_id))
    .map((node) => {
      const nodeState = getNodeVisualState(node);
      return {
        id: node.node_id,
        _fav: state.favouriteNodeIds.has(node.node_id),
        name: node.name || '-',
        state: nodeState,
        health: getNodeHealthValue(node.node_id) || '-',
        rate: getNodeRate(node.node_id),
        uptime: node.has_disappeared ? formatLastSeen(node.last_seen) : formatUptime(node.uptime),
        publishers: node.publishers || [],
        subscribers: node.subscribers || [],
        servers: node.servers || [],
        clients: node.clients || [],
        _actions: nodeState,
      };
    });
  return rows;
};

// ── Tabulator instance (created once at startup) ──
let nodesTabulator = null;

const initNodesTable = () => {
  const initialSort = state.tableSort.key
    ? [{ column: state.tableSort.key, dir: state.tableSort.dir }]
    : [{ column: 'id', dir: 'asc' }];

  const settings = readSettings();
  const savedWidths = settings.columnWidths || {};

  // Wrap sorters to always pin favourites to top
  const favPinSorter = (baseSorter) => (a, b, aRow, bRow, column, dir, sorterParams) => {
    const aFav = aRow.getData()._fav ? 1 : 0;
    const bFav = bRow.getData()._fav ? 1 : 0;
    if (aFav !== bFav) {
      // Favourites always on top regardless of sort direction
      return dir === 'asc' ? bFav - aFav : aFav - bFav;
    }
    // Within same group, use the base sorter
    if (typeof baseSorter === 'function') return baseSorter(a, b, aRow, bRow, column, dir, sorterParams);
    // Fallback for built-in sorter types
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (baseSorter === 'number') return Number(a) - Number(b);
    return String(a).localeCompare(String(b));
  };

  const colDef = (title, field, opts = {}) => {
    const def = { title, field, headerFilter: 'input', ...opts };
    def.sorter = favPinSorter(opts.sorter || 'string');
    if (savedWidths[field]) {
      def.width = savedWidths[field];
    }
    return def;
  };

  nodesTabulator = new Tabulator('#nodesTable', {
    data: [],
    layout: 'fitColumns',
    resizableColumns: true,
    selectable: 1,
    placeholder: 'Not connected',
    initialSort,
    columns: [
      { title: '', field: '_fav', formatter: favFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-fav', cellClick: (_e, cell) => { toggleFavourite(cell.getRow().getData().id); } },
      colDef('ID', 'id', { sorter: 'number', minWidth: 50, widthGrow: 0.5, headerFilterPlaceholder: 'id', cssClass: 'cell-scroll' }),
      colDef('Name', 'name', { sorter: 'string', minWidth: 100, widthGrow: 2, headerFilterPlaceholder: 'name', cssClass: 'cell-scroll' }),
      colDef('State', 'state', { sorter: 'string', minWidth: 40, widthGrow: 0.7, formatter: stateFormatter, headerFilterPlaceholder: 'state', cssClass: 'td-state' }),
      colDef('Health', 'health', { sorter: 'string', minWidth: 70, widthGrow: 0.8, formatter: healthFormatter, headerFilterPlaceholder: 'health', cssClass: 'cell-scroll' }),
      colDef('Rate', 'rate', { sorter: 'number', minWidth: 70, widthGrow: 0.7, formatter: rateFormatter, headerFilterPlaceholder: 'rate', cssClass: 'cell-scroll', headerFilterFunc: (headerValue, rowValue) => { if (!headerValue) return true; return Number(rowValue).toFixed(1).includes(headerValue); } }),
      colDef('Uptime', 'uptime', { sorter: 'string', minWidth: 100, widthGrow: 1, headerFilterPlaceholder: 'uptime', cssClass: 'cell-scroll' }),
      colDef('Publishers', 'publishers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'pub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Subscribers', 'subscribers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'sub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Servers', 'servers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Clients', 'clients', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      { title: '', field: '_actions', formatter: actionsFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', cellClick: (_e, cell) => { const d = cell.getRow().getData(); if (d.state === 'offline') deleteOfflineNode(d.id); } },
    ],
  });

  nodesTabulator.on('rowClick', (_e, row) => {
    const clickedId = row.getData().id;
    if (clickedId === state.selectedNodeId) {
      clearSelectedNode();
    } else {
      setSelectedNode(clickedId);
    }
  });
  nodesTabulator.on('dataSorted', (sorters) => {
    if (sorters.length > 0) {
      state.tableSort = { key: sorters[0].field, dir: sorters[0].dir };
      saveSettings();
    }
  });
  nodesTabulator.on('columnResized', () => {
    saveSettings();
  });
  nodesTabulator.on('dataFiltered', () => {
    saveSettings();
  });

  // Restore state after table is built
  nodesTabulator.on('tableBuilt', () => {
    const savedFilters = settings.headerFilters || {};
    for (const [field, value] of Object.entries(savedFilters)) {
      if (value) {
        nodesTabulator.setHeaderFilterValue(field, value);
      }
    }
    renderNodesTable();
  });
};

const renderNodesTable = () => {
  const countEl = el('nodeCount');
  const data = buildTableData();

  if (countEl) {
    countEl.textContent = String(data.length);
  }

  if (!nodesTabulator) return;

  if (!data.length) {
    nodesTabulator.clearData();
    nodesTabulator.options.placeholder = state.dashboardConnected ? 'No nodes discovered yet' : 'Not connected';
    nodesTabulator.redraw(true);
    return;
  }

  nodesTabulator.updateOrAddData(data);

  // Re-apply current sort so favourite pin-order updates immediately
  const sorters = nodesTabulator.getSorters();
  if (sorters.length) {
    nodesTabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
  }

  // Remove rows that no longer exist
  const validIds = new Set(data.map((d) => d.id));
  const currentRows = nodesTabulator.getRows();
  for (const row of currentRows) {
    if (!validIds.has(row.getData().id)) {
      row.delete();
    }
  }

  // Highlight selected row
  for (const row of nodesTabulator.getRows()) {
    const rowEl = row.getElement();
    if (row.getData().id === state.selectedNodeId) {
      rowEl.classList.add('selected-row');
    } else {
      rowEl.classList.remove('selected-row');
    }
  }
};

const getAllNodes = async () => {
  try {
    const data = await requestJson('/api/nodes');
    state.latestNodesPayload = data;
    const selectedStillExists = state.selectedNodeId !== null && data.nodes && data.nodes[String(state.selectedNodeId)];
    if (!selectedStillExists) {
      state.selectedNodeId = null;
    }
    renderNodesTable();
    renderSelectedNodeContent();
  } catch (error) {
    state.latestNodesPayload = { node_count: 0, nodes: {} };
    state.selectedNodeId = null;
    renderNodesTable();
    renderSelectedNodeContent();
  }
};

const startNodesPolling = () => {
  const seconds = Math.max(1, Math.min(60, Number.parseInt(el('nodesRefreshSlider').value, 10) || 3));
  el('nodesRefreshSlider').value = String(seconds);
  el('refreshValue').textContent = seconds >= 60 ? '1m' : `${seconds}s`;
  saveSettings();

  if (state.nodesTimer) {
    clearInterval(state.nodesTimer);
  }
  state.nodesTimer = window.setInterval(getAllNodes, seconds * 1000);
};

const stopNodesPolling = () => {
  if (state.nodesTimer) {
    clearInterval(state.nodesTimer);
    state.nodesTimer = null;
  }
};

const stopCanStartupDelay = () => {
  if (state.canStartupTimer) {
    clearTimeout(state.canStartupTimer);
    state.canStartupTimer = null;
  }
};

const schedulePostCanStartup = (delayMs = 10000) => {
  stopCanStartupDelay();
  state.canStartupTimer = window.setTimeout(async () => {
    state.canStartupTimer = null;
    if (!state.dashboardConnected || !state.canConnected) {
      return;
    }

    await getAllNodes();
    startNodesPolling();
    startThroughputTimer();
    connectWs();
  }, delayMs);
};

const connectDashboard = async () => {
  if (state.dashboardConnected) {
    disconnectAll();
    return;
  }

  let statusData;
  try {
    statusData = await requestJson('/api/status');
  } catch {
    updateSemaphores();
    return;
  }

  state.dashboardConnected = true;
  updateDashboardConnectButton();
  startStatusPolling();

  // If backend already has CAN running, sync state
  if (statusData.status === 'running' && statusData.can_interface) {
    const select = el('interfacesSelect');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = statusData.can_interface;
    option.textContent = statusData.can_interface;
    select.appendChild(option);
    select.value = statusData.can_interface;
    state.preferredCanInterface = statusData.can_interface;
    state.canConnected = true;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(5000);
  } else {
    await loadInterfaces();
    startInterfacePolling();
  }

  saveSettings();
};

const connectCan = async () => {
  if (state.canConnected) {
    // Disconnect CAN
    state.canDisconnecting = true;
    updateCanConnectButton();

    try {
      await requestJson('/api/can/disconnect', { method: 'POST' });
    } catch {
    }

    state.canDisconnecting = false;
    state.canConnected = false;
    updateCanConnectButton();
    stopCanStartupDelay();
    stopNodesPolling();
    stopThroughputTimer();
    disconnectWs();
    renderNodesTable();
    saveSettings();

    // Reload available interfaces
    if (state.dashboardConnected) {
      await loadInterfaces();
      startInterfacePolling();
    }
    updateSemaphores();
    return;
  }

  // Connect CAN
  stopInterfacePolling();
  state.canConnecting = true;
  updateCanConnectButton();

  const result = await selectInterface();
  state.canConnecting = false;
  if (!result) {
    updateCanConnectButton();
    startInterfacePolling();
    return;
  }

  state.canConnected = true;
  updateCanConnectButton();
  saveSettings();

  schedulePostCanStartup(5000);

  if (!state.dashboardConnected) {
    await connectDashboard();
  }
};

const bindTabs = () => {
  document.querySelectorAll('.detail-tab').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedDetailTab = button.dataset.tab;
      saveSettings();
      renderSelectedNodeContent();
    });
  });
};

const bind = () => {
  el('connectDashboardBtn').addEventListener('click', connectDashboard);
  el('connectCanBtn').addEventListener('click', connectCan);
  el('interfacesSelect').addEventListener('change', () => {
    state.preferredCanInterface = el('interfacesSelect').value;
    saveSettings();
  });
  el('nodesRefreshSlider').addEventListener('input', () => {
    const val = el('nodesRefreshSlider').value;
    el('refreshValue').textContent = Number(val) >= 60 ? '1m' : `${val}s`;
    startNodesPolling();
  });
  el('sidebarCollapseBtn').addEventListener('click', () => {
    const sidebar = document.querySelector('.sidebar');
    sidebar.classList.toggle('collapsed');
    state.sidebarCollapsed = sidebar.classList.contains('collapsed');
    saveSettings();
  });
  // Detail panel resize
  const detailPanel = el('detailPanel');
  const resizeHandle = el('detailResizeHandle');
  const mainArea = detailPanel.parentElement;

  const applyDetailHeight = () => {
    if (state.detailPanelCollapsed) {
      detailPanel.classList.add('collapsed');
    } else if (state.detailPanelHeight != null) {
      detailPanel.style.height = state.detailPanelHeight + 'px';
    }
  };
  applyDetailHeight();

  // Detail panel toggle button (inside resize handle, pure CSS positioning)
  const collapseBtn = el('detailCollapseBtn');
  collapseBtn.addEventListener('mousedown', (e) => e.stopPropagation());

  const updateCollapseChevron = () => {
    collapseBtn.classList.toggle('pointing-up', state.detailPanelCollapsed);
  };
  updateCollapseChevron();

  collapseBtn.addEventListener('click', () => {
    state.detailPanelCollapsed = !state.detailPanelCollapsed;
    if (state.detailPanelCollapsed) {
      detailPanel.classList.add('collapsed');
      detailPanel.style.height = '';
    } else {
      detailPanel.classList.remove('collapsed');
      detailPanel.style.height = state.detailPanelHeight ? state.detailPanelHeight + 'px' : '33.3%';
    }
    updateCollapseChevron();
    saveSettings();
  });

  (() => {
    let startY, startH;

    resizeHandle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      startY = e.clientY;
      startH = detailPanel.getBoundingClientRect().height;
      detailPanel.classList.add('no-transition');
      resizeHandle.classList.add('dragging');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      const dy = startY - e.clientY;
      const totalH = mainArea.getBoundingClientRect().height;
      const newH = Math.max(0, Math.min(totalH * 0.9, startH + dy));
      detailPanel.classList.remove('collapsed');
      detailPanel.style.height = newH + 'px';
    };

    const onUp = (e) => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      detailPanel.classList.remove('no-transition');
      resizeHandle.classList.remove('dragging');

      const totalH = mainArea.getBoundingClientRect().height;
      const currentH = detailPanel.getBoundingClientRect().height;

      if (currentH < totalH / 10) {
        state.detailPanelCollapsed = true;
        state.detailPanelHeight = null;
        detailPanel.classList.add('collapsed');
        detailPanel.style.height = '';
      } else {
        state.detailPanelCollapsed = false;
        state.detailPanelHeight = currentH;
        detailPanel.style.height = currentH + 'px';
      }
      updateCollapseChevron();
      saveSettings();
    };
  })();

  // Double-click handle to reset to default
  resizeHandle.addEventListener('dblclick', () => {
    state.detailPanelCollapsed = false;
    state.detailPanelHeight = null;
    detailPanel.classList.remove('collapsed');
    detailPanel.style.height = '33.3%';
    updateCollapseChevron();
    saveSettings();
  });

  el('themeToggle').addEventListener('click', () => {
    const html = document.documentElement;
    const isLight = html.getAttribute('data-theme') === 'light';
    if (isLight) {
      html.removeAttribute('data-theme');
    } else {
      html.setAttribute('data-theme', 'light');
    }
    el('themeLabel').textContent = isLight ? 'Black' : 'White';
    saveSettings();
  });
  el('apiBase').addEventListener('change', saveSettings);

  el('selectedNodeContent').addEventListener('click', (e) => {
    const card = e.target.closest('.subject-card');
    if (!card) return;
    const sid = Number(card.dataset.subject);
    state.selectedPlotSubject = sid;
    el('selectedNodeContent').querySelectorAll('.subject-card').forEach((c) => {
      c.classList.toggle('selected', Number(c.dataset.subject) === sid);
    });
    startPlotAnim();
  });

  bindTabs();
  initNodesTable();
};

loadSettings();
bind();
updateDashboardConnectButton();
updateCanConnectButton();
renderSelectedNodeContent();
updateSemaphores();

// Restore previous connection state
(async () => {
  if (state.pendingReconnect) {
    delete state.pendingReconnect;
    delete state.pendingCanReconnect;
    await connectDashboard();
  }
  updateSemaphores();
})();

// ── Frontend server heartbeat ──
(() => {
  const HEARTBEAT_INTERVAL = 5000;
  const overlay = el('serverDownOverlay');
  let serverDown = false;

  // persist=false so on heartbeat recovery (page reload) auto-reconnect
  // sees the prior connected state and resumes without manual action.
  const tearDown = () => disconnectAll({ persist: false });

  const check = async () => {
    try {
      const resp = await fetch(window.location.href, { method: 'HEAD', cache: 'no-store' });
      if (!resp.ok) throw new Error();
      if (serverDown) {
        serverDown = false;
        overlay.classList.add('hidden');
        window.location.reload();
      }
    } catch {
      if (!serverDown) {
        serverDown = true;
        tearDown();
        overlay.classList.remove('hidden');
      }
    }
  };

  setInterval(check, HEARTBEAT_INTERVAL);
})();
