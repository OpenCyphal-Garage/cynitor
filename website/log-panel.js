// Right-side log panel: shell (collapse/resize/persistence) + live log feed
// from three independently-toggled sources:
//   - cyphal:   uavcan.diagnostic.Record + user-added text subjects (WS push)
//   - server:   GET /api/logs poller (Python `logging` records)
//   - frontend: console.{debug,log,info,warn,error} hook
//
// Source of truth: state.logBuffer (in-memory ring buffer, NOT persisted).
// Persisted UI: state.logPanelCollapsed, state.logPanelWidth,
//               state.logSeverityFloor, state.logAutoscroll,
//               state.logSubjectIds,
//               state.logShowCyphal, state.logShowServer, state.logShowFrontend.

const LOG_PANEL_MIN_WIDTH_PX = 224;   // ~14rem
const LOG_PANEL_MAX_WIDTH_PX = 800;   // ~50rem
const LOG_BUFFER_CAP = 2000;
const LOG_DOM_CAP = 1000;
const AUTOSCROLL_STICKY_PX = 16;
const SERVER_LOG_POLL_MS = 2000;
const SERVER_LOG_FETCH_LIMIT = 200;

const SEVERITY_LABELS = ['TRACE', 'DEBUG', 'INFO', 'NOTICE', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT'];
const SEVERITY_CSS = ['trace', 'debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert'];

const SERVER_LEVEL_TO_SEVERITY = {
  DEBUG: 1, INFO: 2, WARNING: 4, ERROR: 5, CRITICAL: 6,
};
const CONSOLE_METHOD_TO_SEVERITY = {
  debug: 1, log: 2, info: 2, warn: 4, error: 5,
};

const DIAGNOSTIC_SUBJECT_ID = 8184;

// Per-source entry counters mirror state.logBuffer composition.
// Kept in sync via _pushEntry, _onClearLog, _recomputeSrcCounts.
const _srcCounts = { cyphal: 0, server: 0, frontend: 0 };

const _recomputeSrcCounts = () => {
  _srcCounts.cyphal = 0;
  _srcCounts.server = 0;
  _srcCounts.frontend = 0;
  for (const e of state.logBuffer) {
    if (e.source in _srcCounts) _srcCounts[e.source]++;
  }
};

const _refreshSourcePillBadges = () => {
  for (const pill of document.querySelectorAll('.log-source-pill')) {
    const src = pill.dataset.source;
    const countEl = pill.querySelector('.log-source-count');
    if (countEl) {
      const n = _srcCounts[src] || 0;
      countEl.textContent = n > 0 ? String(n) : '';
      countEl.classList.toggle('hidden', n === 0);
    }
    if (src === 'server') {
      const disconnected = state.logShowServer && !state.dashboardConnected;
      pill.classList.toggle('disconnected', disconnected);
    }
  }
};

// ── Width plumbing ──────────────────────────────────────────────────

const applyLogPanelWidth = (panel) => {
  const shell = panel.closest('.app-shell');
  if (!shell) return;
  if (state.logPanelWidth != null) {
    shell.style.setProperty('--log-w', state.logPanelWidth + 'px');
  } else {
    shell.style.removeProperty('--log-w');
  }
};

// ── Cyphal source helpers ───────────────────────────────────────────

const isDiagnosticRecord = (event) => {
  const mt = event?.message_type;
  return typeof mt === 'string' && mt.startsWith('Record_');
};

const isUserLoggedSubject = (event) => {
  return Number.isInteger(event?.subject_id) && state.logSubjectIds.has(event.subject_id);
};

const hasStringAttribute = (event) => {
  for (const a of event?.attributes || []) {
    if (typeof a.value === 'string' && a.value.length > 0) return true;
  }
  return false;
};

const firstStringValue = (event) => {
  for (const a of event?.attributes || []) {
    if (typeof a.value === 'string') return a.value;
  }
  return '';
};

const extractRecordEntry = (event) => {
  let severity = null;
  let text = '';
  for (const a of event.attributes || []) {
    if (a.attribute === 'severity' && Number.isInteger(a.value)) severity = a.value;
    else if (a.attribute === 'text' && typeof a.value === 'string') text = a.value;
  }
  if (severity == null || severity < 0 || severity > 7) severity = null;
  return { severity, text };
};

// ── Entry builders ──────────────────────────────────────────────────

const buildCyphalEntry = (event, kind) => {
  let severity = null;
  let text = '';
  if (kind === 'diagnostic') {
    const r = extractRecordEntry(event);
    severity = r.severity;
    text = r.text;
  } else {
    text = firstStringValue(event);
  }
  return {
    id: ++state._logSeq,
    t: event.timestamp_unix || (Date.now() / 1000),
    source: 'cyphal',
    kind,
    severity,
    nodeId: event.publisher_node_id ?? null,
    subjectId: event.subject_id ?? null,
    subjectName: typeof event.message_type === 'string' ? event.message_type : '',
    text,
  };
};

const buildServerEntry = (raw) => {
  const level = String(raw.level || '').toUpperCase();
  const severity = SERVER_LEVEL_TO_SEVERITY[level] ?? null;
  const tsMs = Date.parse(raw.timestamp || '');
  return {
    id: ++state._logSeq,
    t: Number.isFinite(tsMs) ? tsMs / 1000 : Date.now() / 1000,
    tsRaw: raw.timestamp || '',
    source: 'server',
    severity,
    level,
    loggerName: typeof raw.logger === 'string' ? raw.logger : '',
    text: typeof raw.message === 'string' ? raw.message : '',
  };
};

const stringifyConsoleArg = (a) => {
  if (a == null) return String(a);
  if (typeof a === 'string') return a;
  if (typeof a === 'number' || typeof a === 'boolean') return String(a);
  if (a instanceof Error) return a.stack || `${a.name}: ${a.message}`;
  try { return JSON.stringify(a); }
  catch { return String(a); }
};

const buildFrontendEntry = (method, args) => {
  return {
    id: ++state._logSeq,
    t: Date.now() / 1000,
    source: 'frontend',
    severity: CONSOLE_METHOD_TO_SEVERITY[method] ?? 2,
    consoleMethod: method,
    text: args.map(stringifyConsoleArg).join(' '),
  };
};

// ── Render ──────────────────────────────────────────────────────────

const formatLogTime = (unix) => {
  const d = new Date(unix * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
};

const isAtBottom = (container) => {
  return container.scrollHeight - container.scrollTop - container.clientHeight <= AUTOSCROLL_STICKY_PX;
};

const _rowLabelAndContext = (entry) => {
  if (entry.source === 'cyphal') {
    if (entry.kind === 'diagnostic') {
      return {
        label: entry.severity != null ? SEVERITY_LABELS[entry.severity] : '—',
        context: entry.nodeId != null ? `n${entry.nodeId}` : '—',
        title: '',
      };
    }
    return {
      label: entry.subjectName || `s${entry.subjectId ?? '?'}`,
      context: entry.nodeId != null ? `n${entry.nodeId}` : '—',
      title: entry.subjectId != null ? `subject ${entry.subjectId} (${entry.subjectName || ''})` : '',
    };
  }
  if (entry.source === 'server') {
    return {
      label: entry.level || '—',
      context: entry.loggerName || 'server',
      title: entry.loggerName ? `logger: ${entry.loggerName}` : '',
    };
  }
  // frontend
  return {
    label: (entry.consoleMethod || 'log').toUpperCase(),
    context: 'console',
    title: `console.${entry.consoleMethod || 'log'}`,
  };
};

const renderLogRow = (entry) => {
  const row = document.createElement('div');
  const sevClass = entry.severity != null ? `log-row-sev-${SEVERITY_CSS[entry.severity]}` : 'log-row-sev-none';
  const kindClass = entry.kind ? ` log-row-kind-${entry.kind}` : '';
  row.className = `log-row ${sevClass} log-row-src-${entry.source}${kindClass}`;
  row.dataset.severity = String(entry.severity ?? -1);
  row.dataset.source = entry.source;
  const { label, context, title } = _rowLabelAndContext(entry);
  row.innerHTML = `
    <span class="log-row-time">${escapeHtml(formatLogTime(entry.t))}</span>
    <span class="log-row-sev" title="${escapeHtml(title)}">${escapeHtml(label)}</span>
    <span class="log-row-node">${escapeHtml(context)}</span>
    <span class="log-row-text">${escapeHtml(entry.text)}</span>
  `;
  return row;
};

// ── Filters ─────────────────────────────────────────────────────────

const _sourceIsShown = (src) => {
  if (src === 'cyphal') return state.logShowCyphal;
  if (src === 'server') return state.logShowServer;
  if (src === 'frontend') return state.logShowFrontend;
  return true;
};

const applyAllFiltersToRow = (row) => {
  const sev = Number(row.dataset.severity);
  const floor = state.logSeverityFloor;
  const sevPasses = sev < 0 ? true : sev >= floor;
  const srcPasses = _sourceIsShown(row.dataset.source);
  row.classList.toggle('hidden', !(sevPasses && srcPasses));
};

const reapplyFiltersToAllRows = () => {
  const list = el('logList');
  if (!list) return;
  for (const row of list.children) applyAllFiltersToRow(row);
};

const updateEmptyHint = () => {
  const empty = el('logEmpty');
  if (!empty) return;
  const allOff = !state.logShowCyphal && !state.logShowServer && !state.logShowFrontend;
  const noEntries = state.logBuffer.length === 0;
  if (allOff) {
    empty.textContent = 'All log sources are off — enable one above.';
    empty.classList.remove('hidden');
  } else if (noEntries) {
    empty.textContent = 'Waiting for log messages…';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
  }
};

// ── Ingest (push entry into buffer + DOM) ───────────────────────────

const _pushEntry = (entry) => {
  state.logBuffer.push(entry);
  if (entry.source in _srcCounts) _srcCounts[entry.source]++;
  if (state.logBuffer.length > LOG_BUFFER_CAP) {
    const excess = state.logBuffer.length - LOG_BUFFER_CAP;
    for (let i = 0; i < excess; i++) {
      const s = state.logBuffer[i].source;
      if (s in _srcCounts) _srcCounts[s]--;
    }
    state.logBuffer.splice(0, excess);
  }
  _refreshSourcePillBadges();
  const list = el('logList');
  if (!list) return;
  const wasAtBottom = isAtBottom(list.parentElement);
  const row = renderLogRow(entry);
  applyAllFiltersToRow(row);
  list.appendChild(row);
  while (list.childElementCount > LOG_DOM_CAP) {
    list.removeChild(list.firstElementChild);
  }
  updateEmptyHint();
  if (state.logAutoscroll && wasAtBottom) {
    list.parentElement.scrollTop = list.parentElement.scrollHeight;
  }
};

// Cyphal WS hook (called unconditionally from connection.js).
const ingestLogEvent = (event) => {
  let kind;
  if (isDiagnosticRecord(event)) kind = 'diagnostic';
  else if (isUserLoggedSubject(event)) kind = 'subject';
  else return;
  const entry = buildCyphalEntry(event, kind);
  if (kind === 'subject' && !entry.text) return;
  _pushEntry(entry);
};

const ingestServerEntry = (raw) => {
  _pushEntry(buildServerEntry(raw));
};

const ingestFrontendEntry = (method, args) => {
  _pushEntry(buildFrontendEntry(method, args));
};

// ── Rebuild (used after major UI state changes) ─────────────────────

const rebuildLogList = () => {
  const list = el('logList');
  if (!list) return;
  list.replaceChildren();
  const slice = state.logBuffer.slice(-LOG_DOM_CAP);
  for (const entry of slice) {
    const row = renderLogRow(entry);
    applyAllFiltersToRow(row);
    list.appendChild(row);
  }
  updateEmptyHint();
  if (state.logAutoscroll) {
    list.parentElement.scrollTop = list.parentElement.scrollHeight;
  }
};

// ── Subject picker popover ──────────────────────────────────────────

const getCandidateSubjects = () => {
  const out = [];
  for (const [sid, event] of state.latestBySubject) {
    if (sid === DIAGNOSTIC_SUBJECT_ID) continue;
    if (!hasStringAttribute(event)) continue;
    out.push({
      subjectId: sid,
      messageType: typeof event.message_type === 'string' ? event.message_type : '',
    });
  }
  for (const sid of state.logSubjectIds) {
    if (sid === DIAGNOSTIC_SUBJECT_ID) continue;
    if (!out.some(s => s.subjectId === sid)) {
      out.push({ subjectId: sid, messageType: '' });
    }
  }
  out.sort((a, b) => a.subjectId - b.subjectId);
  return out;
};

const ensureLogPickerPopover = () => {
  let popover = el('logSubjectPickerPopover');
  if (popover) return popover;
  popover = document.createElement('div');
  popover.id = 'logSubjectPickerPopover';
  popover.className = 'hidden-popover log-subject-popover hidden';
  document.body.appendChild(popover);
  return popover;
};

const renderLogPickerPopover = () => {
  const popover = ensureLogPickerPopover();
  const candidates = getCandidateSubjects();

  let listHtml = '';
  if (candidates.length === 0) {
    listHtml = '<div class="log-subject-empty">No text-bearing subjects seen yet.</div>';
  } else {
    listHtml = '<div class="hidden-popover-list">';
    for (const c of candidates) {
      const checked = state.logSubjectIds.has(c.subjectId);
      listHtml += `
        <label class="log-subject-row">
          <input type="checkbox" class="log-subject-checkbox" data-subject-id="${c.subjectId}" ${checked ? 'checked' : ''}>
          <span class="hidden-popover-id">${escapeHtml(String(c.subjectId))}</span>
          <span class="hidden-popover-name">${escapeHtml(c.messageType || '—')}</span>
        </label>
      `;
    }
    listHtml += '</div>';
  }

  popover.innerHTML = `
    <div class="hidden-popover-header">
      <span>Text subjects in log</span>
    </div>
    ${listHtml}
  `;

  for (const cb of popover.querySelectorAll('.log-subject-checkbox')) {
    cb.addEventListener('change', () => {
      const sid = parseInt(cb.dataset.subjectId, 10);
      if (!Number.isInteger(sid)) return;
      if (cb.checked) state.logSubjectIds.add(sid);
      else state.logSubjectIds.delete(sid);
      saveSettings();
    });
  }
};

const toggleLogPickerPopover = () => {
  const popover = ensureLogPickerPopover();
  const btn = el('logAddSubjectBtn');
  if (!btn) return;
  if (popover.classList.contains('hidden')) {
    renderLogPickerPopover();
    positionPopover(popover, btn);
    popover.classList.remove('hidden');
  } else {
    popover.classList.add('hidden');
  }
};

// ── Server log poller ───────────────────────────────────────────────

let _serverPollTimer = null;
let _serverLastSeenTs = '';

const _serverEntryKey = (e) => `${e.timestamp}|${e.logger}|${e.message}`;

const _fetchServerLogsOnce = async () => {
  _refreshSourcePillBadges();
  if (!state.dashboardConnected) return;
  try {
    const data = await requestJson(`/api/logs?limit=${SERVER_LOG_FETCH_LIMIT}`);
    const logs = Array.isArray(data.logs) ? data.logs : [];
    let newestTs = _serverLastSeenTs;
    for (const e of logs) {
      const ts = e?.timestamp || '';
      if (!ts) continue;
      if (_serverLastSeenTs && ts <= _serverLastSeenTs) continue;
      ingestServerEntry(e);
      if (ts > newestTs) newestTs = ts;
    }
    _serverLastSeenTs = newestTs;
  } catch {
    // Backend may be unreachable mid-session — silent, retry on next tick.
  }
};

const startServerLogPoll = () => {
  if (_serverPollTimer) return;
  _fetchServerLogsOnce();
  _serverPollTimer = setInterval(_fetchServerLogsOnce, SERVER_LOG_POLL_MS);
};

const stopServerLogPoll = () => {
  if (_serverPollTimer) {
    clearInterval(_serverPollTimer);
    _serverPollTimer = null;
  }
};

// ── Frontend console hook ───────────────────────────────────────────

let _consoleHookInstalled = false;
const _origConsole = {};

const installConsoleHook = () => {
  if (_consoleHookInstalled) return;
  _consoleHookInstalled = true;
  for (const method of ['debug', 'log', 'info', 'warn', 'error']) {
    _origConsole[method] = console[method].bind(console);
    console[method] = (...args) => {
      _origConsole[method](...args);
      if (state.logShowFrontend) {
        try { ingestFrontendEntry(method, args); }
        catch { /* never let logging break the app */ }
      }
    };
  }
};

// ── Toolbar bits ────────────────────────────────────────────────────

const updateAutoscrollBtn = () => {
  const btn = el('logAutoscrollBtn');
  if (!btn) return;
  btn.classList.toggle('active', state.logAutoscroll);
  btn.setAttribute('aria-pressed', String(state.logAutoscroll));
};

const _isSourceOn = (src) =>
  src === 'cyphal' ? state.logShowCyphal
  : src === 'server' ? state.logShowServer
  : src === 'frontend' ? state.logShowFrontend
  : false;

const _setSourceOn = (src, on) => {
  if (src === 'cyphal') state.logShowCyphal = on;
  else if (src === 'server') state.logShowServer = on;
  else if (src === 'frontend') state.logShowFrontend = on;
};

const _refreshSourcePill = (pill) => {
  const on = _isSourceOn(pill.dataset.source);
  pill.setAttribute('aria-pressed', String(on));
};

const _onSourceToggle = (pill) => {
  const src = pill.dataset.source;
  const next = !_isSourceOn(src);
  _setSourceOn(src, next);
  _refreshSourcePill(pill);
  if (src === 'server') {
    if (next) startServerLogPoll();
    else stopServerLogPoll();
  }
  reapplyFiltersToAllRows();
  _refreshSourcePillBadges();
  updateEmptyHint();
  saveSettings();
};

// ── Init ────────────────────────────────────────────────────────────

const initLogPanel = () => {
  const panel = el('logPanel');
  const collapseBtn = el('logPanelCollapseBtn');
  const resizeHandle = el('logPanelResizeHandle');
  if (!panel || !collapseBtn || !resizeHandle) return;

  panel.style.width = '';
  panel.style.minWidth = '';

  if (state.logPanelCollapsed) {
    panel.classList.add('collapsed');
  } else {
    panel.classList.remove('collapsed');
    applyLogPanelWidth(panel);
  }

  collapseBtn.addEventListener('click', () => {
    state.logPanelCollapsed = !state.logPanelCollapsed;
    panel.classList.toggle('collapsed', state.logPanelCollapsed);
    if (!state.logPanelCollapsed) {
      applyLogPanelWidth(panel);
      rebuildLogList();
    }
    saveSettings();
  });

  let startX = 0;
  let startW = 0;
  const shell = panel.closest('.app-shell');

  const onMove = (e) => {
    const dx = startX - e.clientX;
    const newW = Math.max(LOG_PANEL_MIN_WIDTH_PX, Math.min(LOG_PANEL_MAX_WIDTH_PX, startW + dx));
    if (shell) shell.style.setProperty('--log-w', newW + 'px');
  };

  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    panel.classList.remove('no-transition');
    resizeHandle.classList.remove('dragging');
    state.logPanelWidth = panel.getBoundingClientRect().width;
    saveSettings();
  };

  resizeHandle.addEventListener('mousedown', (e) => {
    if (state.logPanelCollapsed) return;
    e.preventDefault();
    startX = e.clientX;
    startW = panel.getBoundingClientRect().width;
    panel.classList.add('no-transition');
    resizeHandle.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  resizeHandle.addEventListener('dblclick', () => {
    state.logPanelWidth = null;
    applyLogPanelWidth(panel);
    saveSettings();
  });

  const sevSelect = el('logSeveritySelect');
  if (sevSelect) {
    sevSelect.value = String(state.logSeverityFloor);
    sevSelect.addEventListener('change', () => {
      const v = parseInt(sevSelect.value, 10);
      state.logSeverityFloor = Number.isInteger(v) ? Math.max(0, Math.min(7, v)) : 0;
      reapplyFiltersToAllRows();
      saveSettings();
    });
  }

  const autoBtn = el('logAutoscrollBtn');
  if (autoBtn) {
    autoBtn.addEventListener('click', () => {
      state.logAutoscroll = !state.logAutoscroll;
      updateAutoscrollBtn();
      if (state.logAutoscroll) {
        const list = el('logList');
        if (list) list.parentElement.scrollTop = list.parentElement.scrollHeight;
      }
      saveSettings();
    });
    updateAutoscrollBtn();
  }

  const clearBtn = el('logClearBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      state.logBuffer.length = 0;
      _srcCounts.cyphal = 0;
      _srcCounts.server = 0;
      _srcCounts.frontend = 0;
      const list = el('logList');
      if (list) list.replaceChildren();
      _refreshSourcePillBadges();
      updateEmptyHint();
    });
  }

  const addBtn = el('logAddSubjectBtn');
  if (addBtn) {
    ensureLogPickerPopover();
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleLogPickerPopover();
    });
  }

  for (const pill of document.querySelectorAll('.log-source-pill')) {
    _refreshSourcePill(pill);
    pill.addEventListener('click', () => _onSourceToggle(pill));
  }

  installConsoleHook();
  if (state.logShowServer) startServerLogPoll();

  _recomputeSrcCounts();
  _refreshSourcePillBadges();
  updateEmptyHint();
};
