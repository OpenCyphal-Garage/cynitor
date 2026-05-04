// Global state, constants, foundational helpers, API, and settings persistence.
// Loaded first; everything below depends on what's declared here.

const STORAGE_KEY = 'pycyphal.dashboard.settings.v2';

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
  deletedNodeIds: new Set(),
  latestNodesPayload: { node_count: 0, nodes: {} },
  latestBySubject: new Map(),
  latestByNode: new Map(),
  selectedNodeId: null,
  selectedDetailTab: 'publishers',
  selectedPlotSubject: null,
  subjectHistory: new Map(),
  hiddenPlotSeries: new Map(),
  plotTimer: null,
  wsBytesAccum: 0,
  wsThroughput: 0,
  busUtilization: null,
  tableSort: { key: 'id', dir: 'asc' },
  sidebarCollapsed: false,
  detailPanelHeight: null,
  detailPanelCollapsed: false,
  splitRatio: 0.6,
  serviceSchemas: new Map(),
  serviceCallState: null,
  serviceCallHistory: [],
  expandedServiceId: null,
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
const PLOT_STALE_THRESHOLD = 3;
const PLOT_TICK_MS = 100;

// ── Utility helpers ──

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

// ── localStorage settings ──

const readSettings = () => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
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
    theme: document.documentElement.getAttribute('data-theme') || 'light',
    columnWidths: getColumnWidths(),
    headerFilters: getHeaderFilters(),
    selectedNodeId: state.selectedNodeId,
    splitRatio: state.splitRatio,
    favouriteNodeIds: [...state.favouriteNodeIds],
    deletedNodeIds: [...state.deletedNodeIds],
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
    const validTabs = ['publishers', 'subscribers', 'servers', 'clients', 'registers'];
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
