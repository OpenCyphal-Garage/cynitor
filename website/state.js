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
  dashboardConnecting: false,
  canState: CONN.IDLE,
  preferredCanInterface: '',
  // Adapters the backend offers (GET /api/status available_adapters).
  canAdapters: [],
  // Last bitrate used per interface spec, so the list preselects it next
  // time. Never a guess: an adapter not connected before starts unselected.
  canBitrates: {},
  canDataBitrates: {},   // CAN FD data bitrate last used per adapter; absent = Classic CAN
  rawLogPlaybackSpeed: 1,  // Record view: speed to play a raw log at; 0 = as fast as possible
  customCanSpec: '',
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
  plotSeriesSeen: new Map(),  // subject-ID -> names of its series plotted so far (see renderPlot)
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
  lastWsMessageMs: 0,
  busUtilization: null,
  droppedEvents: null,   // {scanner, logger, clients} from /api/status, null when not on CAN
  busLoadHistory: [],
  _busFullArmed: true,
  tableSort: { key: 'id', dir: 'asc' },
  sidebarCollapsed: false,
  logPanelCollapsed: true,
  logPanelWidth: null,
  logBuffer: [],
  logSeverityFloor: 0,
  logAutoscroll: true,
  logSubjectIds: new Set(),
  logShowCyphal: true,
  logShowServer: false,
  logTextFilter: '',
  cyphalV11: null,          // Cyphal v1.1 traffic seen on the bus (GET /api/status), or null       // the log panel's filter box, lowercased; not persisted
  _logSeq: 0,
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
  subjectsKind: 'all',   // what the Subjects table lists: 'all', 'Subject' or 'Service'
  compactRows: false,    // the tables' rows at about 70% height (Compact, in their strips)
  nodesFocus: null,      // the kind picked out in a table's status strip, or null
  subjectsFocus: null,
  recordings: [],
  activeRecordingId: null,
  recordBuffer: null,
  // Replay session state — populated by /api/replay/* responses and the
  // /api/replay/status poll while replay is active. Mirrors CANSession.replay
  // on the backend.
  replayActive: false,
  replayRecordingId: null,
  replayPositionS: 0,
  replayDurationS: 0,
  replaySpeed: 1.0,
  replayPaused: false,
  replayEventsEmitted: 0,
  replayTotalEvents: 0,
  replayStatusTimer: null,
  // True when the playback engine reached the end naturally (vs the user
  // clicking Stop). Keeps the strip visible in a "Finished" mode with a
  // Replay-again / Close affordance until the user dismisses it.
  replayFinished: false,
  recordFilterDraft: {
    subject_ids: [], service_ids: [], node_ids: [], message_types: [],
    name: '', notes: '',
    max_length_seconds: 3600,
    max_events: 100_000,
    stop_on_limit: true,
  },
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
// Tabulator's renderer exists only once the table is built; setting data
// before that throws ("reading 'verticalFillMode'"). A reload that
// restores the connection fetches nodes before the build finishes.
let _nodesTableReady = false;

// Pending debounce ids for scheduleTableRefresh / scheduleDetailRefresh.
// Live here because settings.js#saveSettings reads nodesTabulator above.
let _tableRefreshPending = null;
let _detailRefreshPending = null;

const metricMaxLen = new Map();

// Plot colours: the --plot-N tokens of the theme in use, read again whenever
// the theme is set (loadSettings, the theme toggle).
const PLOT_COLORS = [];
const refreshPlotColors = () => {
  const root = getComputedStyle(document.documentElement);
  const fallback = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39d2c0'];
  PLOT_COLORS.splice(0, PLOT_COLORS.length,
    ...fallback.map((color, i) => root.getPropertyValue(`--plot-${i + 1}`).trim() || color));
};
refreshPlotColors();
const PLOT_TICK_MS = 100;

// ── Utility helpers ──

const el = (id) => document.getElementById(id);

// A unique-ID as hex: from its bytes, or as it is when already hex (an offline
// node that lost its node-ID is known by its unique_id_hex).
const uniqueIdKey = (uid) => {
  if (typeof uid === 'string') return uid || null;
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

const formatBytes = (b) => {
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(1)} KB`;
  return `${b} B`;
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
  // The two largest units: enough to read at a glance, short enough to fit.
  const parts = [[Math.floor(s / 86400), 'd'], [Math.floor((s % 86400) / 3600), 'h'],
    [Math.floor((s % 3600) / 60), 'm'], [Math.floor(s % 60), 's']];
  const first = parts.findIndex(([n]) => n > 0);
  if (first < 0) return '0s';
  return parts.slice(first, first + 2).map(([n, unit]) => `${n}${unit}`).join(' ');
};

// How long ago, at a glance: "12s ago", "3m ago", "2h ago", "4d ago".
const formatAgo = (seconds) => {
  const ago = Math.max(0, Math.floor(seconds));
  if (ago < 60) return `${ago}s ago`;
  if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
  if (ago < 86400) return `${Math.floor(ago / 3600)}h ago`;
  return `${Math.floor(ago / 86400)}d ago`;
};

const formatLastSeen = (lastSeen) => {
  if (!Array.isArray(lastSeen) || !lastSeen.length) return '-';
  const ts = new Date(lastSeen[lastSeen.length - 1]);
  if (Number.isNaN(ts.getTime())) return '-';
  return formatAgo((Date.now() - ts.getTime()) / 1000);
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

// Why nothing can arrive from the bus, as svcStateMsg's parts; null when it can.
const connectionProblem = (context) => {
  const spinner = '<span class="svc-spinner"></span>';
  if (!state.dashboardConnected) {
    if (state.dashboardConnecting) {
      return { icon: spinner, message: 'Connecting to backend…', helper: 'Reaching the backend server.' };
    }
    if (state.pendingReconnect) {
      return { icon: spinner, message: 'Reconnecting to backend…', helper: 'Restoring previous session.' };
    }
    return { icon: '⏻', message: 'Not connected to backend', helper: `Connect to the backend server to ${context}.` };
  }
  if (state.canState === CONN.CONNECTING) {
    return { icon: spinner, message: 'Connecting to CAN interface…', helper: 'Establishing CAN bus connection.' };
  }
  if (state.canState !== CONN.CONNECTED) {
    return { icon: '⛓', message: 'CAN bus not connected', helper: `Connect a CAN interface to ${context}.` };
  }
  return null;
};

const connectionPlaceholder = (context) => {
  const problem = connectionProblem(context);
  return problem ? svcStateMsg(problem.icon, problem.message, problem.helper) : null;
};

// Same as connectionPlaceholder but treats an active replay session as a
// valid event source. Use this in panels that only consume cached events
// (node table, graph view, detail panel root, plots) — they should render
// during replay even though CAN is disconnected by definition. Live-RPC
// panels (registers, history, services) keep using connectionPlaceholder
// because they need a real bus.
const eventSourcePlaceholder = (context) => {
  if (state.replayActive) return null;
  return connectionPlaceholder(context);
};

// Updates rows in place; returns the fields whose values changed in any row.
const diffUpdateTable = (tabulator, data, keyField) => {
  const currentRowMap = new Map();
  for (const row of tabulator.getRows()) {
    currentRowMap.set(row.getData()[keyField], row);
  }
  const newRows = [];
  const newIds = new Set();
  const changed = new Set();
  for (const d of data) {
    newIds.add(d[keyField]);
    const existing = currentRowMap.get(d[keyField]);
    if (!existing) { newRows.push(d); continue; }
    const cur = existing.getData();
    const diff = {};
    for (const k of Object.keys(d)) {
      if (d[k] !== cur[k]) { diff[k] = d[k]; changed.add(k); }
    }
    if (Object.keys(diff).length) existing.update(diff);
  }
  for (const [id, row] of currentRowMap) {
    if (!newIds.has(id)) row.delete();
  }
  if (newRows.length) tabulator.addData(newRows);
  return changed;
};

// Tabulator sorts only when asked, so a row whose sorted value changed stays
// where it was. A live table sorts again once that happened, every few seconds
// at most, and not under the pointer: a row moving away takes a click with it.
const RESORT_MIN_MS = 3000;
const _resorts = new WeakMap();  // tabulator -> {last: ms, pending: bool}

const resortChanged = (tabulator, changed) => {
  const sorters = tabulator.getSorters();
  const resort = _resorts.get(tabulator) || { last: 0, pending: false };
  _resorts.set(tabulator, resort);
  if (sorters.some((s) => changed.has(s.field))) resort.pending = true;
  const now = Date.now();
  // Nor while a row has the keyboard: it would move away from under it.
  const busy = tabulator.element.matches(':hover') || tabulator.element.querySelector('.tabulator-row:focus-visible');
  if (!resort.pending || now - resort.last < RESORT_MIN_MS || busy) return;
  resort.last = now;
  resort.pending = false;
  tabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
};

// Put fresh content into `target`, replacing only the nodes that changed and
// going deeper where an element kept its tag and attributes. A periodic
// refresh then leaves unchanged buttons in place: a click is lost when its
// button is swapped out between press and release.
const _sameAttributes = (a, b) => a.attributes.length === b.attributes.length
  && [...a.attributes].every((attr) => b.getAttribute(attr.name) === attr.value);

const patchChildren = (target, fresh) => {
  const oldKids = [...target.childNodes];
  const newKids = [...fresh.childNodes];
  if (oldKids.length !== newKids.length) {
    target.replaceChildren(...newKids);
    return;
  }
  newKids.forEach((kid, i) => {
    const old = oldKids[i];
    if (kid.isEqualNode(old)) return;
    if (kid.nodeType === Node.ELEMENT_NODE && old.nodeType === Node.ELEMENT_NODE
        && kid.tagName === old.tagName && _sameAttributes(kid, old)) {
      patchChildren(old, kid);
    } else {
      old.replaceWith(kid);
    }
  });
};

// Rows by keyboard. Tab stops at the table; ↓/↑ go from row to row (into
// the rows: the selected one, else the first), Home/End to the ends, Enter
// or Space acts as a click on the row, Escape leaves the rows. `keys` maps
// more keys to actions on the focused row. Rows need tabindex -1 (see
// focusableRow) to take the focus.
const focusableRow = (row) => { row.getElement().tabIndex = -1; };

const bindRowKeys = (tabulator, activate, keys = {}) => {
  // At once when the row is drawn, so a held or quick key goes on from it.
  const focusRow = (row) => {
    if (!row) return;
    if (row.getElement().isConnected) row.getElement().focus();
    tabulator.scrollToRow(row, 'nearest', false).then(() => row.getElement().focus());
  };
  // Drawing the table again (on a resize, say) takes its rows out of the
  // page and puts them back, which loses the focus: the row that had it when
  // the drawing began gets it back.
  let focused = null;
  tabulator.on('renderStarted', () => {
    const active = document.activeElement;
    focused = active.classList.contains('tabulator-row') && tabulator.element.contains(active) ? active : null;
  });
  tabulator.on('renderComplete', () => {
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    focused = null;
  });
  tabulator.element.addEventListener('keydown', (e) => {
    const onRow = e.target.classList.contains('tabulator-row');
    if (!onRow && !e.target.classList.contains('tabulator-tableholder')) return;  // a filter, an input
    const rows = tabulator.getRows('active');
    const selected = tabulator.element.querySelector('.tabulator-row.selected-row');
    const row = onRow ? tabulator.getRow(e.target) : null;
    const actions = {
      ArrowDown: () => focusRow(row ? row.getNextRow() : (selected ? tabulator.getRow(selected) : rows[0])),
      ArrowUp: () => focusRow(row ? row.getPrevRow() : (selected ? tabulator.getRow(selected) : rows[0])),
      Home: () => focusRow(rows[0]),
      End: () => focusRow(rows[rows.length - 1]),
      Enter: () => row && activate(row),
      ' ': () => row && activate(row),
      Escape: () => row && e.target.closest('.tabulator-tableholder').focus(),
      ...Object.fromEntries(Object.entries(keys).map(([key, act]) => [key, () => row && act(row)])),
    };
    if (!actions[e.key]) return;
    e.preventDefault();
    actions[e.key]();
  });
};

// A strip over a table saying what needs a look: the total, then a count of
// each kind of row that does, which, clicked, picks those rows out (see the
// Graph's strip); last, the Compact toggle (see applyRowDensity). `before` is
// markup to put first. Returns the kinds counted.
const renderStatusStrip = (strip, total, kinds, rows, focus, before = '') => {
  const counts = kinds.map((k) => ({ ...k, count: rows.filter(k.test).length })).filter((k) => k.count);
  const fresh = document.createElement('div');
  fresh.innerHTML = `${before}<span class="table-status-total">${escapeHtml(total)}</span>`
    + (counts.length
      ? counts.map((k) => `<button type="button" class="table-chip table-chip--${k.level}" data-focus="${k.key}"`
        + ` aria-pressed="${focus === k.key}">${k.count} ${escapeHtml(k.label)}</button>`).join('')
      : '<span class="table-status-usual">nothing unusual</span>')
    + `<button type="button" class="table-kind-btn table-density-btn${state.compactRows ? ' active' : ''}"`
    + ` data-density aria-pressed="${state.compactRows}">Compact</button>`;
  patchChildren(strip, fresh);
  return counts;
};

// The first IDs of a list that fit in a few characters, then how many more:
// a long list reads at a glance, and its tooltip has it whole.
const LIST_SHOWN_CHARS = 14;
const shortIdList = (ids, render = (id) => escapeHtml(String(id))) => {
  let shown = 1;
  let chars = String(ids[0]).length;
  while (shown < ids.length && chars + 2 + String(ids[shown]).length <= LIST_SHOWN_CHARS) {
    chars += 2 + String(ids[shown]).length;
    shown += 1;
  }
  const more = ids.length - shown;
  return ids.slice(0, shown).map(render).join(', ') + (more ? `<span class="list-more"> +${more}</span>` : '');
};

const positionPopover = (popover, anchorEl) => {
  const rect = anchorEl.getBoundingClientRect();
  popover.style.top = (rect.bottom + 4) + 'px';
  popover.style.right = (window.innerWidth - rect.right) + 'px';
};

const getStatusClass = (attr, value) => {
  const v = String(value).toUpperCase();
  switch (attr) {
    // Only what is not the usual gets a colour, as in the node table.
    case 'health':
      if (v === 'NOMINAL' || v === '0') return '';
      if (v === 'ADVISORY' || v === '1') return 'status-warn';
      return getHealthCssClass(value);
    case 'mode':
      return v === 'OPERATIONAL' || v === '0' ? '' : 'status-warn';
    default:
      return '';
  }
};

// Favourites first and ghost rows last, whichever way the column sorts; empty
// values last too. Tabulator hands a descending sort its rows swapped, so a
// pinned order is flipped back for it.
const makeFavPinSorter = ({ ghostField } = {}) => (baseSorter) =>
  (a, b, aRow, bRow, column, dir, sorterParams) => {
    const pin = (order) => (dir === 'asc' ? order : -order);
    if (ghostField) {
      const aGhost = aRow.getData()[ghostField] ? 1 : 0;
      const bGhost = bRow.getData()[ghostField] ? 1 : 0;
      if (aGhost !== bGhost) return pin(aGhost - bGhost);
    }
    const aFav = aRow.getData()._fav ? 1 : 0;
    const bFav = bRow.getData()._fav ? 1 : 0;
    if (aFav !== bFav) return pin(bFav - aFav);
    if (typeof baseSorter === 'function') return baseSorter(a, b, aRow, bRow, column, dir, sorterParams);
    if (a == null && b == null) return 0;
    if (a == null) return pin(1);
    if (b == null) return pin(-1);
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

// Configuration the backend injects via /config.js before any page script
// runs. Empty when a plain static file server delivers the page instead, in
// which case the built-in defaults apply.
const serverConfig = () => window.__CYNITOR || {};

const AUTH_TOKEN_KEY = 'cynitor.auth.token';
const getAuthToken = () => {
  try { return localStorage.getItem(AUTH_TOKEN_KEY) || ''; } catch (_) { return ''; }
};
const setAuthToken = (token) => {
  try {
    if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
    else localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch (_) {}
};

// Append the auth token as a query parameter, for requests that cannot carry
// an Authorization header: WebSocket handshakes, and downloads the browser
// makes itself (links, window.location).
const withTokenParam = (url) => {
  const token = getAuthToken();
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
};

const wsUrlWithToken = (path) => withTokenParam(`${wsBase()}${path}`);

const withSmartJsonHeaders = (options = {}) => {
  const method = String(options.method || 'GET').toUpperCase();
  const hasBody = options.body !== undefined && options.body !== null;
  const isSimpleMethod = method === 'GET' || method === 'HEAD';
  const token = getAuthToken();
  const baseHeaders = {
    ...(options.headers || {}),
    ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
  };
  if (isSimpleMethod && !hasBody) {
    if (!token) return options;
    return { ...options, headers: baseHeaders };
  }
  return {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...baseHeaders,
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
  if (response.status === 401) {
    // Backend requires CYNITOR_AUTH_TOKEN. Drop any stale stored token and
    // prompt the user. Throws so the caller's catch handler runs.
    setAuthToken('');
    showAuthModal(data.error || 'Missing or invalid token');
    const err = new Error(data.error || 'Authentication required');
    err.status = 401;
    err.data = data;
    throw err;
  }
  if (!response.ok) {
    // Every route answers an error as {"error": message}, sometimes with more fields.
    const err = new Error(data.error || `HTTP ${response.status} for ${path}`);
    // Attach the raw status and body so callers can distinguish e.g. a 504
    // service-call timeout (body carries {status: "timeout", latency_ms, error})
    // from a generic 500 with the same envelope.
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
};

// ── Auth token modal ──

let _authModalResolver = null;

const showAuthModal = (errorMsg) => {
  const modal = el('authModal');
  if (!modal) return;
  const apiBaseEl = el('authModalApiBase');
  if (apiBaseEl) apiBaseEl.textContent = apiBase();
  const errEl = el('authModalError');
  if (errEl) {
    if (errorMsg) {
      errEl.textContent = errorMsg;
      errEl.classList.remove('hidden');
    } else {
      errEl.classList.add('hidden');
    }
  }
  const input = el('authModalInput');
  if (input) {
    input.value = '';
    setTimeout(() => input.focus(), 50);
  }
  modal.classList.remove('hidden');
};

const hideAuthModal = () => {
  const modal = el('authModal');
  if (modal) modal.classList.add('hidden');
};

const _bindAuthModalOnce = () => {
  const btn = el('authModalSave');
  const input = el('authModalInput');
  if (!btn || !input || btn.dataset.bound) return;
  btn.dataset.bound = '1';
  const commit = () => {
    const token = input.value.trim();
    if (!token) return;
    setAuthToken(token);
    hideAuthModal();
    // The caller decides what to retry — most paths will recover on the
    // next status poll / WS reconnect tick.
    if (typeof connectDashboard === 'function') {
      // best-effort reconnect; safe to call even if already connected
      try { connectDashboard(); } catch (_) {}
    }
  };
  btn.addEventListener('click', commit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
  });
};

document.addEventListener('DOMContentLoaded', _bindAuthModalOnce);

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
    canBitrates: state.canBitrates,
    canDataBitrates: state.canDataBitrates,
    customCanSpec: state.customCanSpec,
    dashboardConnected: state.dashboardConnected,
    selectedDetailTab: state.selectedDetailTab,
    tableSort: state.tableSort,
    sidebarCollapsed: state.sidebarCollapsed,
    logPanelCollapsed: state.logPanelCollapsed,
    logPanelWidth: state.logPanelWidth,
    logSeverityFloor: state.logSeverityFloor,
    logAutoscroll: state.logAutoscroll,
    logSubjectIds: [...state.logSubjectIds],
    logShowCyphal: state.logShowCyphal,
    logShowServer: state.logShowServer,
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
    compareGraphs: state.compareGraphs.map((g) => ({ id: g.id, ...compareGraphConfig(g) })),
    savedCompareConfigs: state.savedCompareConfigs,
    favouriteNodeIds: [...state.favouriteNodeIds],
    hiddenNodeIds: [...state.hiddenNodeIds],
    nodeAliases: state.nodeAliases,
    activeView: state.activeView,
    favouriteSubjectIds: [...state.favouriteSubjectIds],
    hiddenSubjectIds: [...state.hiddenSubjectIds],
    subjectsTableSort: state.subjectsTableSort,
    subjectsKind: state.subjectsKind,
    compactRows: state.compactRows,
    subjectsHeaderFilters: typeof getSubjectsHeaderFilters === 'function' ? getSubjectsHeaderFilters() : null,
    recordFilterDraft: state.recordFilterDraft,
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
  const injectedApiBase = serverConfig().apiBase;
  if (injectedApiBase) {
    el('apiBase').value = injectedApiBase;
  } else if (typeof settings.apiBase === 'string' && settings.apiBase.trim()) {
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
  if (settings.logPanelCollapsed === false) {
    state.logPanelCollapsed = false;
    document.querySelector('.log-panel')?.classList.remove('collapsed');
  }
  if (typeof settings.logPanelWidth === 'number' && settings.logPanelWidth >= 0) {
    state.logPanelWidth = settings.logPanelWidth;
  }
  if (Number.isInteger(settings.logSeverityFloor)
      && settings.logSeverityFloor >= 0 && settings.logSeverityFloor <= 7) {
    state.logSeverityFloor = settings.logSeverityFloor;
  }
  if (settings.logAutoscroll === false) {
    state.logAutoscroll = false;
  }
  if (Array.isArray(settings.logSubjectIds)) {
    state.logSubjectIds = new Set(settings.logSubjectIds.filter(Number.isInteger));
  }
  if (settings.logShowCyphal === false) state.logShowCyphal = false;
  if (settings.logShowServer === true) state.logShowServer = true;
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
    el('themeToggle').setAttribute('aria-checked', 'true');
  }
  refreshPlotColors();
  if (typeof settings.canInterface === 'string') {
    state.preferredCanInterface = settings.canInterface;
  }
  if (settings.canBitrates && typeof settings.canBitrates === 'object') {
    state.canBitrates = settings.canBitrates;
  }
  if (settings.canDataBitrates && typeof settings.canDataBitrates === 'object') {
    state.canDataBitrates = settings.canDataBitrates;
  }
  if (typeof settings.customCanSpec === 'string') {
    state.customCanSpec = settings.customCanSpec;
    el('canSpecInput').value = settings.customCanSpec;
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
  if (['subjects', 'graph', 'compare', 'dsdl', 'record', 'debug'].includes(settings.activeView)) {
    state.activeView = settings.activeView;
  }
  if (settings.recordFilterDraft && typeof settings.recordFilterDraft === 'object') {
    const d = settings.recordFilterDraft;
    state.recordFilterDraft = {
      subject_ids: Array.isArray(d.subject_ids) ? d.subject_ids.filter(Number.isInteger) : [],
      service_ids: Array.isArray(d.service_ids) ? d.service_ids.filter(Number.isInteger) : [],
      node_ids: Array.isArray(d.node_ids) ? d.node_ids.filter(Number.isInteger) : [],
      message_types: Array.isArray(d.message_types) ? d.message_types.filter(s => typeof s === 'string') : [],
      name: typeof d.name === 'string' ? d.name : '',
      notes: typeof d.notes === 'string' ? d.notes : '',
      max_length_seconds: typeof d.max_length_seconds === 'number' && d.max_length_seconds > 0 ? d.max_length_seconds : 3600,
      max_events: typeof d.max_events === 'number' && d.max_events > 0 ? d.max_events : 100_000,
      stop_on_limit: d.stop_on_limit !== false,
    };
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
  if (['all', 'Subject', 'Service'].includes(settings.subjectsKind)) {
    state.subjectsKind = settings.subjectsKind;
  }
  if (settings.compactRows === true) state.compactRows = true;
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
  // Graphs and saved graphs come back whole, what is valid of them (compare-view.js).
  if (Array.isArray(settings.compareGraphs)) {
    state.compareGraphs = settings.compareGraphs
      .filter((g) => typeof g?.id === 'string' && sanitizeCompareGraph(g))
      .map((g) => newCompareGraph(g, g.id));
  }
  if (Array.isArray(settings.savedCompareConfigs)) {
    state.savedCompareConfigs = settings.savedCompareConfigs.map(sanitizeCompareGraph).filter(Boolean);
  }
  if (!state.compareGraphs.length && Array.isArray(settings.plotCompareList)) {
    const migrated = settings.plotCompareList.filter(
      item => Number.isInteger(item?.subjectId) && typeof item?.attribute === 'string'
    );
    if (migrated.length) state.compareGraphs.push(newCompareGraph({ series: migrated }, 'cg_migrated'));
  }
};
