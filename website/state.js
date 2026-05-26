// Global state, constants, foundational helpers, API, and settings persistence.
// Loaded first; everything below depends on what's declared here.

const STORAGE_KEY = 'cynitor.dashboard.settings.v1';
const _LEGACY_STORAGE_KEY = 'pycyphal.dashboard.settings.v2';

// Connection state enums — prevent impossible flag combinations
const CONN = Object.freeze({ IDLE: 'idle', CONNECTING: 'connecting', CONNECTED: 'connected', DISCONNECTING: 'disconnecting' });

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
  canState: CONN.IDLE,
  preferredCanInterface: '',
  favouriteNodeIds: new Set(),
  hiddenNodeIds: new Set(),
  latestNodesPayload: { node_count: 0, nodes: {} },
  latestBySubject: new Map(),
  latestByNode: new Map(),
  selectedNodeId: null,
  selectedDetailTab: 'publishers',
  selectedPlotSubject: null,
  _nodesPlotSubject: null,
  subjectHistory: new Map(),
  hiddenPlotSeries: new Map(),
  plotTimer: null,
  plotPaused: false,
  plotPausedAt: null,
  plotTimeWindow: 60,
  plotSmooth: 0,
  plotDisconnectPoints: false,
  plotStroke: 1.5,
  plotGrid: false,
  plotColorOverrides: {},
  compareGraphs: [],
  savedCompareConfigs: [],
  wsBytesAccum: 0,
  wsThroughput: 0,
  busUtilization: null,
  busLoadHistory: [],
  _busFullArmed: true,
  tableSort: { key: 'id', dir: 'asc' },
  sidebarCollapsed: false,
  detailPanelHeight: null,
  detailPanelCollapsed: false,
  _nodesDetailHeight: null,
  _nodesDetailCollapsed: false,
  _subjectsDetailHeight: null,
  _subjectsDetailCollapsed: false,
  splitRatio: 0.6,
  serviceSchemas: new Map(),
  serviceCallState: null,
  _subjectServiceCallState: null,
  _subjectServiceNodeId: null,
  serviceCallHistory: [],
  expandedServiceId: null,
  _subjectExpandedServiceId: null,
  nodeAliases: {},
  historyTimeRange: '1h',
  activeView: 'nodes',
  favouriteSubjectIds: new Set(),
  hiddenSubjectIds: new Set(),
  subjectsTableSort: { key: 'id', dir: 'asc' },
};

// Derived accessors for CAN connection state — keeps existing code readable
// while the source of truth is the single `state.canState` enum.
Object.defineProperties(state, {
  canConnected:     { get() { return this.canState === CONN.CONNECTED; },     set(v) { this.canState = v ? CONN.CONNECTED : CONN.IDLE; } },
  canConnecting:    { get() { return this.canState === CONN.CONNECTING; },    set(v) { if (v) this.canState = CONN.CONNECTING; else if (this.canState === CONN.CONNECTING) this.canState = CONN.IDLE; } },
  canDisconnecting: { get() { return this.canState === CONN.DISCONNECTING; }, set(v) { if (v) this.canState = CONN.DISCONNECTING; else if (this.canState === CONN.DISCONNECTING) this.canState = CONN.IDLE; } },
});

// Tabulator instance. Initialized in nodes-table.js#initNodesTable.
let nodesTabulator = null;

// Pending debounce ids for scheduleTableRefresh / scheduleDetailRefresh.
// Live here because settings.js#saveSettings reads nodesTabulator above.
let _tableRefreshPending = null;
let _detailRefreshPending = null;

const metricMaxLen = new Map();

// Read plot colors from CSS custom properties. Resolved once at script load.
const PLOT_COLORS = (() => {
  const root = getComputedStyle(document.documentElement);
  const fallback = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39d2c0'];
  return [1, 2, 3, 4, 5, 6].map((i) => {
    return root.getPropertyValue(`--plot-${i}`).trim() || fallback[i - 1];
  });
})();
const PLOT_TICK_MS = 100;

// ── Utility helpers ──

const el = (id) => document.getElementById(id);

const uniqueIdKey = (uid) => {
  if (!Array.isArray(uid) || !uid.length) return null;
  return uid.map((b) => b.toString(16).padStart(2, '0')).join('');
};

const nodeStableKey = (node) => node?.unique_id_hex || `nid:${node?.node_id}`;

const upgradeStableKeys = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes) return;
  let changed = false;
  for (const keySet of [state.favouriteNodeIds, state.hiddenNodeIds]) {
    const upgrades = [];
    for (const key of keySet) {
      if (typeof key !== 'string' || !key.startsWith('nid:')) continue;
      const nid = parseInt(key.slice(4), 10);
      const node = nodes[String(nid)];
      if (node?.unique_id_hex) {
        upgrades.push({ old: key, hex: node.unique_id_hex });
      }
    }
    for (const { old, hex } of upgrades) {
      keySet.delete(old);
      keySet.add(hex);
      changed = true;
    }
  }
  if (changed) saveSettings();
};

const getNodeAlias = (uid) => {
  const key = uniqueIdKey(uid);
  return key ? state.nodeAliases[key] || null : null;
};

const setNodeAlias = (uid, alias) => {
  const key = uniqueIdKey(uid);
  if (!key) return;
  const trimmed = alias?.trim();
  if (trimmed) {
    state.nodeAliases[key] = trimmed;
  } else {
    delete state.nodeAliases[key];
  }
  saveSettings();
};

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

const formatThroughput = (bytesPerSec) => {
  if (bytesPerSec >= 1_000_000) return `${(bytesPerSec / 1_000_000).toFixed(1)} MB/s`;
  if (bytesPerSec >= 1_000) return `${(bytesPerSec / 1_000).toFixed(1)} KB/s`;
  return `${Math.round(bytesPerSec)} B/s`;
};

const formatPlotTime = (unix) => {
  const d = new Date(unix * 1000);
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
};

const classifyHealth = (health) => {
  if (!health) return null;
  const v = String(health).toUpperCase();
  if (v === 'NOMINAL' || v === '0') return 'ok';
  if (v === 'ADVISORY' || v === '1') return 'ok';
  if (v === 'CAUTION' || v === '2') return 'warn';
  if (v === 'WARNING' || v === '3') return 'err';
  return null;
};

const HEALTH_CSS_CLASS = { ok: 'status-ok', warn: 'status-warn', err: 'status-err' };
const HEALTH_CSS_COLOR = { ok: 'var(--ok)', warn: 'var(--warn)', err: 'var(--error)' };

const getHealthCssClass = (health) => HEALTH_CSS_CLASS[classifyHealth(health)] || '';
const getHealthColor = (health) => HEALTH_CSS_COLOR[classifyHealth(health)] || 'var(--muted)';

const connectionPlaceholder = (context) => {
  if (!state.dashboardConnected) {
    if (state.pendingReconnect) {
      return svcStateMsg('<span class="svc-spinner"></span>', 'Reconnecting to backend…', 'Restoring previous session.');
    }
    return svcStateMsg('⏻', 'Not connected to backend', `Connect to the backend server to ${context}.`);
  }
  if (state.canState === CONN.CONNECTING) {
    return svcStateMsg('<span class="svc-spinner"></span>', 'Connecting to CAN interface…', 'Establishing CAN bus connection.');
  }
  if (state.canState !== CONN.CONNECTED) {
    return svcStateMsg('⛓', 'CAN bus not connected', `Connect a CAN interface to ${context}.`);
  }
  return null;
};

const diffUpdateTable = (tabulator, data, keyField) => {
  const currentRowMap = new Map();
  for (const row of tabulator.getRows()) {
    currentRowMap.set(row.getData()[keyField], row);
  }
  const newRows = [];
  const newIds = new Set();
  for (const d of data) {
    newIds.add(d[keyField]);
    const existing = currentRowMap.get(d[keyField]);
    if (!existing) { newRows.push(d); continue; }
    const cur = existing.getData();
    const diff = {};
    for (const k of Object.keys(d)) {
      if (d[k] !== cur[k]) diff[k] = d[k];
    }
    if (Object.keys(diff).length) existing.update(diff);
  }
  for (const [id, row] of currentRowMap) {
    if (!newIds.has(id)) row.delete();
  }
  if (newRows.length) tabulator.addData(newRows);
};

const positionPopover = (popover, anchorEl) => {
  const rect = anchorEl.getBoundingClientRect();
  popover.style.top = (rect.bottom + 4) + 'px';
  popover.style.right = (window.innerWidth - rect.right) + 'px';
};

const getStatusClass = (attr, value) => {
  const v = String(value).toUpperCase();
  switch (attr) {
    case 'health':
      return getHealthCssClass(value);
    case 'mode':
      if (v === 'OPERATIONAL' || v === '0') return 'status-ok';
      if (v === 'INITIALIZATION' || v === '1') return 'status-init';
      return '';
    default:
      return '';
  }
};

const makeFavPinSorter = ({ ghostField } = {}) => (baseSorter) =>
  (a, b, aRow, bRow, column, dir, sorterParams) => {
    if (ghostField) {
      const aGhost = aRow.getData()[ghostField] ? 1 : 0;
      const bGhost = bRow.getData()[ghostField] ? 1 : 0;
      if (aGhost !== bGhost) return aGhost - bGhost;
    }
    const aFav = aRow.getData()._fav ? 1 : 0;
    const bFav = bRow.getData()._fav ? 1 : 0;
    if (aFav !== bFav) return dir === 'asc' ? bFav - aFav : aFav - bFav;
    if (typeof baseSorter === 'function') return baseSorter(a, b, aRow, bRow, column, dir, sorterParams);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (baseSorter === 'number') {
      const aNum = Number(a), bNum = Number(b);
      const aNaN = isNaN(aNum), bNaN = isNaN(bNum);
      if (aNaN && bNaN) return String(a).localeCompare(String(b));
      if (aNaN) return 1;
      if (bNaN) return -1;
      return aNum - bNum;
    }
    return String(a).localeCompare(String(b));
  };

const getMetricMinWidth = (subjectId, attr, displayStr) => {
  const key = `${subjectId}:${attr}`;
  const prev = metricMaxLen.get(key) || 0;
  const len = Math.max(prev, displayStr.length);
  metricMaxLen.set(key, len);
  return len;
};

// ── Toast notifications ──

const showToast = (message, type = 'info', durationMs = 5000) => {
  const container = el('toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('toast-exit');
    toast.addEventListener('transitionend', () => toast.remove());
  }, durationMs);
};

// ── API helpers ──

const apiBase = () => el('apiBase').value.trim().replace(/\/$/, '');
const wsBase = () => apiBase().replace(/^http/, 'ws');

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

const REQUEST_TIMEOUT_MS = 15000;

const requestJson = async (path, options = {}) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      ...withSmartJsonHeaders(options),
      signal: controller.signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new Error(`Request timeout for ${path}`);
    }
    throw new Error(`Network error for ${path}: ${error?.message || error}`);
  } finally {
    clearTimeout(timeout);
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status} for ${path}`);
  }
  return data;
};

// ── localStorage settings ──

const readSettings = () => {
  try {
    let raw = localStorage.getItem(STORAGE_KEY);
    if (!raw && (raw = localStorage.getItem(_LEGACY_STORAGE_KEY))) {
      localStorage.setItem(STORAGE_KEY, raw);
      localStorage.removeItem(_LEGACY_STORAGE_KEY);
    }
    return JSON.parse(raw || '{}');
  } catch {
    return {};
  }
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

const _writeSettingsNow = () => {
  const interfacesSelect = el('interfacesSelect');
  const persisted = {
    apiBase: el('apiBase').value.trim(),
    canInterface: interfacesSelect ? interfacesSelect.value : '',
    dashboardConnected: state.dashboardConnected,
    selectedDetailTab: state.selectedDetailTab,
    tableSort: state.tableSort,
    sidebarCollapsed: state.sidebarCollapsed,
    detailPanelHeight: state.detailPanelHeight,
    detailPanelCollapsed: state.detailPanelCollapsed,
    nodesDetailHeight: state._nodesDetailHeight,
    nodesDetailCollapsed: state._nodesDetailCollapsed,
    subjectsDetailHeight: state._subjectsDetailHeight,
    subjectsDetailCollapsed: state._subjectsDetailCollapsed,
    theme: document.documentElement.getAttribute('data-theme') || 'light',
    headerFilters: getHeaderFilters(),
    selectedNodeId: state.selectedNodeId,
    splitRatio: state.splitRatio,
    plotTimeWindow: state.plotTimeWindow,
    plotSmooth: state.plotSmooth,
    plotDisconnectPoints: state.plotDisconnectPoints,
    plotStroke: state.plotStroke,
    plotGrid: state.plotGrid,
    plotColorOverrides: state.plotColorOverrides,
    compareGraphs: state.compareGraphs.map(g => ({
      id: g.id, name: g.name, series: g.series, thresholds: g.thresholds || [],
      timeWindow: g.timeWindow, smooth: g.smooth, stroke: g.stroke, disconnectPoints: g.disconnectPoints, grid: g.grid,
    })),
    savedCompareConfigs: state.savedCompareConfigs,
    favouriteNodeIds: [...state.favouriteNodeIds],
    hiddenNodeIds: [...state.hiddenNodeIds],
    nodeAliases: state.nodeAliases,
    activeView: state.activeView,
    favouriteSubjectIds: [...state.favouriteSubjectIds],
    hiddenSubjectIds: [...state.hiddenSubjectIds],
    subjectsTableSort: state.subjectsTableSort,
    subjectsHeaderFilters: typeof getSubjectsHeaderFilters === 'function' ? getSubjectsHeaderFilters() : null,
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
};

let _saveSettingsPending = null;

// Debounced. Most callers fire bursts (filter typing, drag-end + state-flip
// pairs, etc.) and we only need the final state on disk. The beforeunload
// listener at the bottom of this file flushes any pending write so a tab
// close during the debounce window doesn't lose the last change.
const saveSettings = () => {
  if (_saveSettingsPending) return;
  _saveSettingsPending = window.setTimeout(() => {
    _saveSettingsPending = null;
    _writeSettingsNow();
  }, 250);
};

window.addEventListener('beforeunload', () => {
  if (_saveSettingsPending) {
    clearTimeout(_saveSettingsPending);
    _saveSettingsPending = null;
    _writeSettingsNow();
  }
});

const loadSettings = () => {
  const settings = readSettings();
  if (typeof settings.apiBase === 'string' && settings.apiBase.trim()) {
    el('apiBase').value = settings.apiBase;
  }
  if (settings.tableSort && settings.tableSort.key) {
    state.tableSort = settings.tableSort;
  }
  if (typeof settings.selectedDetailTab === 'string') {
    const validTabs = ['publishers', 'subscribers', 'servers', 'clients', 'registers', 'history'];
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
  if (typeof settings.nodesDetailHeight === 'number') {
    state._nodesDetailHeight = settings.nodesDetailHeight;
  }
  if (settings.nodesDetailCollapsed) {
    state._nodesDetailCollapsed = true;
  }
  if (typeof settings.subjectsDetailHeight === 'number') {
    state._subjectsDetailHeight = settings.subjectsDetailHeight;
  }
  if (settings.subjectsDetailCollapsed) {
    state._subjectsDetailCollapsed = true;
  }
  if (settings.theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    el('themeLabel').textContent = 'Dark';
  }
  if (typeof settings.canInterface === 'string') {
    state.preferredCanInterface = settings.canInterface;
  }
  if (settings.dashboardConnected === true) {
    state.pendingReconnect = true;
  }
  if (Array.isArray(settings.favouriteNodeIds)) {
    state.favouriteNodeIds = new Set(settings.favouriteNodeIds.map(
      (v) => typeof v === 'number' ? `nid:${v}` : v
    ));
  }
  const savedHidden = settings.hiddenNodeIds || settings.deletedNodeIds;
  if (Array.isArray(savedHidden)) {
    state.hiddenNodeIds = new Set(savedHidden.map(
      (v) => typeof v === 'number' ? `nid:${v}` : v
    ));
  }
  if (Number.isInteger(settings.selectedNodeId)) {
    state.selectedNodeId = settings.selectedNodeId;
  }
  if (typeof settings.splitRatio === 'number' && settings.splitRatio > 0.2 && settings.splitRatio < 0.9) {
    state.splitRatio = settings.splitRatio;
  }
  if (settings.nodeAliases && typeof settings.nodeAliases === 'object') {
    state.nodeAliases = settings.nodeAliases;
  }
  if (settings.activeView === 'subjects' || settings.activeView === 'graph' || settings.activeView === 'compare') {
    state.activeView = settings.activeView;
  }
  if (Array.isArray(settings.favouriteSubjectIds)) {
    state.favouriteSubjectIds = new Set(settings.favouriteSubjectIds);
  }
  if (Array.isArray(settings.hiddenSubjectIds)) {
    state.hiddenSubjectIds = new Set(settings.hiddenSubjectIds);
  }
  if (settings.subjectsTableSort?.key) {
    state.subjectsTableSort = settings.subjectsTableSort;
  }
  if (typeof settings.plotTimeWindow === 'number' && settings.plotTimeWindow >= 0) {
    state.plotTimeWindow = settings.plotTimeWindow;
  }
  if (typeof settings.plotSmooth === 'number' && settings.plotSmooth >= 0 && settings.plotSmooth <= 30) {
    state.plotSmooth = settings.plotSmooth;
  }
  if (settings.plotDisconnectPoints === true) {
    state.plotDisconnectPoints = true;
  }
  if (typeof settings.plotStroke === 'number' && settings.plotStroke >= 1 && settings.plotStroke <= 5) {
    state.plotStroke = settings.plotStroke;
  }
  if (settings.plotGrid === true) {
    state.plotGrid = true;
  }
  if (settings.plotColorOverrides && typeof settings.plotColorOverrides === 'object') {
    state.plotColorOverrides = settings.plotColorOverrides;
  }
  if (Array.isArray(settings.compareGraphs)) {
    state.compareGraphs = settings.compareGraphs
      .filter(g => g && typeof g.id === 'string' && Array.isArray(g.series))
      .map(g => ({
        id: g.id, name: g.name || '',
        series: g.series.filter(s => Number.isInteger(s?.subjectId) && typeof s?.attribute === 'string'),
        paused: false, pausedAt: null,
        timeWindow: typeof g.timeWindow === 'number' ? g.timeWindow : 60,
        smooth: typeof g.smooth === 'number' ? g.smooth : 0,
        stroke: typeof g.stroke === 'number' ? g.stroke : 1.5,
        disconnectPoints: g.disconnectPoints === true,
        grid: g.grid === true,
        thresholds: Array.isArray(g.thresholds) ? g.thresholds.filter(t => typeof t.value === 'number') : [],
        _timer: null, _fingerprint: '', _hidden: new Set(),
      }));
  }
  if (Array.isArray(settings.savedCompareConfigs)) {
    state.savedCompareConfigs = settings.savedCompareConfigs
      .filter(c => c && typeof c.name === 'string' && Array.isArray(c.series))
      .map(c => ({
        name: c.name,
        series: c.series.filter(s => Number.isInteger(s?.subjectId) && typeof s?.attribute === 'string'),
      }));
  }
  if (!state.compareGraphs.length && Array.isArray(settings.plotCompareList)) {
    const migrated = settings.plotCompareList.filter(
      item => Number.isInteger(item?.subjectId) && typeof item?.attribute === 'string'
    );
    if (migrated.length) {
      state.compareGraphs.push({
        id: 'cg_migrated', name: '', series: migrated,
        paused: false, pausedAt: null,
        timeWindow: 60, smooth: 0, stroke: 1.5, disconnectPoints: false,
        _timer: null, _fingerprint: '', _hidden: new Set(),
      });
    }
  }
};
