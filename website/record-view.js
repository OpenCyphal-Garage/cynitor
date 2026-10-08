// Record view: pickers (subjects + services + nodes) → selection → limits.
// Live recordings stream into per-recording event stores on the backend
// (events_source='dedicated'). Cards show progress bars for time + events.
// Cross-tab live indicator on the Record tab button.

const RECORD_POLL_LIVE_MS = 2000;
// Idle (no live recording) poll cadence. Recordings change when the user
// starts/stops one, auto-stop fires on the backend, or another client edits
// the list; 5 s keeps the UI feeling responsive without spamming the API.
const RECORD_POLL_IDLE_MS = 5000;
const BUFFER_POLL_MS = 15000;
const BYTES_PER_EVENT_ESTIMATE = 250;

const LENGTH_OPTIONS = [
  { label: '30 min', value: 1800 },
  { label: '1 hour', value: 3600 },
  { label: '2 hours', value: 7200 },
  { label: '4 hours', value: 14400 },
  { label: '8 hours', value: 28800 },
  { label: '12 hours', value: 43200 },
  { label: '24 hours', value: 86400 },
  { label: '2 days', value: 172800 },
  { label: '3 days', value: 259200 },
  { label: '7 days', value: 604800 },
  { label: '14 days', value: 1209600 },
  { label: '30 days', value: 2592000 },
];

const EVENTS_OPTIONS = [
  { label: '100', value: 100 },
  { label: '1k', value: 1_000 },
  { label: '10k', value: 10_000 },
  { label: '100k', value: 100_000 },
  { label: '1M', value: 1_000_000 },
  { label: '10M', value: 10_000_000 },
];

const PICKER_REFRESH_MS = 2000;

let _recordPollTimer = null;
let _bufferPollTimer = null;
let _recordViewActive = false;
let _subjectsPicker = null;
let _nodesPicker = null;
let _selectionPicker = null;
let _cardsTickerTimer = null;
let _pickerRefreshTimer = null;
let _highlightedNodeId = null;

const _isLiveRecording = (rec) => rec.end_unix == null;

const _formatRetention = (seconds) => {
  if (!seconds) return 'unlimited';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
};

// ── Picker data builders ────────────────────────────────────────────

const _subjectsPickerData = () => {
  // Build subject rows. Map publisher → owner for highlight matching.
  const subjects = Array.from(state.latestBySubject.entries()).map(([sid, ev]) => {
    const pub = Number.isInteger(ev?.publisher_node_id) ? ev.publisher_node_id : null;
    return {
      key: `subject-${sid}`,
      kind: 'subject',
      id: sid,
      label: ev?.message_type || '—',
      owner: pub ?? '—',
      ownerIds: pub != null ? [pub] : [],
      rate: ev?.rate ?? '',
    };
  });
  // Build service rows. Service IDs are unioned across all nodes that
  // either call or serve them; ownerIds collects the full set so a node
  // click can highlight every service it touches.
  const services = new Map();
  const nodes = state.latestNodesPayload?.nodes || {};
  for (const node of Object.values(nodes)) {
    if (!Number.isInteger(node?.node_id)) continue;
    for (const sid of (node.clients || [])) {
      if (!services.has(sid)) services.set(sid, new Set());
      services.get(sid).add(node.node_id);
    }
    for (const sid of (node.servers || [])) {
      if (!services.has(sid)) services.set(sid, new Set());
      services.get(sid).add(node.node_id);
    }
  }
  const serviceRows = Array.from(services.entries()).map(([sid, owners]) => ({
    key: `service-${sid}`,
    kind: 'service',
    id: sid,
    label: 'service',
    owner: owners.size === 1 ? [...owners][0] : `${owners.size} nodes`,
    ownerIds: [...owners],
    rate: '',
  }));
  return [...subjects, ...serviceRows];
};

const _nodesPickerData = () => {
  const nodes = state.latestNodesPayload?.nodes || {};
  return Object.values(nodes)
    .filter((n) => Number.isInteger(n?.node_id))
    .map((node) => ({
      key: `node-${node.node_id}`,
      kind: 'node',
      id: node.node_id,
      name: node.name || '—',
      publishers: (node.publishers || []).length,
      servers: (node.servers || []).length,
    }));
};

const _selectionData = () => {
  const draft = state.recordFilterDraft;
  const rows = [];
  for (const sid of draft.subject_ids) {
    const ev = state.latestBySubject.get(sid);
    rows.push({ key: `subject-${sid}`, kind: 'subject', id: sid, label: ev?.message_type || '—' });
  }
  for (const sid of draft.service_ids) {
    rows.push({ key: `service-${sid}`, kind: 'service', id: sid, label: 'service' });
  }
  for (const nid of draft.node_ids) {
    const node = state.latestNodesPayload?.nodes?.[String(nid)];
    rows.push({ key: `node-${nid}`, kind: 'node', id: nid, label: node?.name || 'node' });
  }
  return rows;
};

const _addToSelection = (row) => {
  const draft = state.recordFilterDraft;
  const id = Number(row.id);
  if (!Number.isFinite(id)) return;
  if (row.kind === 'subject' && !draft.subject_ids.includes(id)) draft.subject_ids.push(id);
  else if (row.kind === 'service' && !draft.service_ids.includes(id)) draft.service_ids.push(id);
  else if (row.kind === 'node' && !draft.node_ids.includes(id)) draft.node_ids.push(id);
  else return;
  saveSettings();
  _refreshSelection();
};

const _removeFromSelection = (row) => {
  const draft = state.recordFilterDraft;
  const id = Number(row.id);
  if (row.kind === 'subject') draft.subject_ids = draft.subject_ids.filter((x) => x !== id);
  else if (row.kind === 'service') draft.service_ids = draft.service_ids.filter((x) => x !== id);
  else if (row.kind === 'node') draft.node_ids = draft.node_ids.filter((x) => x !== id);
  saveSettings();
  _refreshSelection();
};

// Tabulator throws on data given before it has built ("verticalFillMode"), so
// a picker takes fresh data only once built; it is built with its data anyway.
const _builtPickers = new Set();

const _refreshPicker = (picker, data) => {
  if (_builtPickers.has(picker)) picker.replaceData(data);
};

const _refreshSelection = () => {
  _refreshPicker(_selectionPicker, _selectionData());
  _updateDiskHint();
};

// Tabulator's rowDblClick needs both clicks on one row element, but the pickers
// rebuild their rows (every refresh, and a node click redraws them all), so a
// double-click here is two clicks on the same row key in quick succession.
const DOUBLE_CLICK_MS = 400;
let _lastPickerClick = { key: null, at: 0 };

const _isSecondClick = (key) => {
  const now = Date.now();
  const second = _lastPickerClick.key === key && now - _lastPickerClick.at < DOUBLE_CLICK_MS;
  _lastPickerClick = second ? { key: null, at: 0 } : { key, at: now };
  return second;
};

const _setHighlightedNode = (nodeId) => {
  const next = _highlightedNodeId === nodeId ? null : nodeId;
  if (next === _highlightedNodeId) return;
  _highlightedNodeId = next;
  // Re-run row formatters on both tables so highlight + selected classes update.
  if (_subjectsPicker) _subjectsPicker.redraw(true);
  if (_nodesPicker) _nodesPicker.redraw(true);
};

const _refreshPickerTables = () => {
  _refreshPicker(_subjectsPicker, _subjectsPickerData());
  _refreshPicker(_nodesPicker, _nodesPickerData());
};

// ── Buffer chip + disk hint ─────────────────────────────────────────

const _renderBufferChip = () => {
  const chip = el('recBufferChip');
  if (!chip) return;
  const b = state.recordBuffer;
  if (!b) {
    chip.textContent = '';
    return;
  }
  chip.textContent = `Global buffer: last ${_formatRetention(b.retention_seconds)} · ${b.event_count.toLocaleString()} events · ${formatBytes(b.db_size_bytes)}`;
};

const _observedRatePerSec = () => {
  const b = state.recordBuffer;
  if (!b || !b.oldest_event_unix || !b.newest_event_unix || b.event_count < 20) return null;
  const span = b.newest_event_unix - b.oldest_event_unix;
  return span > 0 ? b.event_count / span : null;
};

const _updateDiskHint = () => {
  const hint = el('recDiskHint');
  if (!hint) return;
  const draft = state.recordFilterDraft;
  const rate = _observedRatePerSec();
  const maxBytes = draft.max_events * BYTES_PER_EVENT_ESTIMATE;
  if (!rate) {
    hint.textContent = `≈ up to ${formatBytes(maxBytes)} (no rate observed yet)`;
    return;
  }
  const fillSeconds = draft.max_events / rate;
  const cappedSeconds = Math.min(fillSeconds, draft.max_length_seconds);
  const projectedEvents = Math.min(draft.max_events, Math.round(rate * draft.max_length_seconds));
  const projectedBytes = projectedEvents * BYTES_PER_EVENT_ESTIMATE;
  const limitingFactor = fillSeconds < draft.max_length_seconds ? 'events cap' : 'time limit';
  hint.textContent = `≈ ${formatBytes(projectedBytes)} · ${formatUptime(cappedSeconds)} before ${limitingFactor} (~${rate.toFixed(0)} msg/s observed)`;
};

const fetchRecordBuffer = async () => {
  if (!state.dashboardConnected) {
    state.recordBuffer = null;
    _renderBufferChip();
    _updateDiskHint();
    return;
  }
  try {
    const data = await requestJson('/api/recordings/buffer');
    state.recordBuffer = data.buffer || null;
  } catch (e) {
    state.recordBuffer = null;
  }
  _renderBufferChip();
  _updateDiskHint();
};

// ── Cards (left pane) ───────────────────────────────────────────────

const refreshRecordTabIndicator = () => {
  const btn = el('viewTabRecord');
  if (!btn) return;
  btn.classList.toggle('recording-active', state.recordings.some(_isLiveRecording));
};

const _timeProgressPct = (rec) => {
  if (!rec.max_length_seconds) return 0;
  const end = rec.end_unix ?? Date.now() / 1000;
  return Math.max(0, (end - rec.start_unix) / rec.max_length_seconds * 100);
};

const _eventsProgressPct = (rec) => {
  if (!rec.max_events) return 0;
  return Math.max(0, (rec.event_count || 0) / rec.max_events * 100);
};

const _formatTimeRight = (rec) => {
  const end = rec.end_unix ?? Date.now() / 1000;
  const elapsed = Math.max(0, end - rec.start_unix);
  if (!rec.max_length_seconds) return `${formatUptime(elapsed)} · no limit`;
  return `${formatUptime(elapsed)} / ${formatUptime(rec.max_length_seconds)}`;
};

const _formatEventsRight = (rec) => {
  const n = (rec.event_count || 0).toLocaleString();
  if (!rec.max_events) return `${n} events · no limit`;
  return `${n} / ${rec.max_events.toLocaleString()}`;
};

const _progressBarHtml = (label, pct, rightLabel, noLimit = false) => {
  const clamped = Math.max(0, Math.min(100, pct));
  const overflow = pct > 100;
  const mod = (overflow ? ' overflow' : '') + (noLimit ? ' no-limit' : '');
  return `
    <div class="rec-bar${mod}">
      <span class="rec-bar-label">${label}</span>
      <div class="rec-bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${clamped.toFixed(0)}">
        <div class="rec-bar-fill" style="width:${clamped}%"></div>
      </div>
      <span class="rec-bar-right">${rightLabel}</span>
    </div>
  `;
};

const _filterSummary = (rec) => {
  const f = rec.filter || {};
  const fmt = (ids, label, lookup) => {
    if (!ids?.length) return null;
    const head = ids.slice(0, 6).map((id) => lookup ? lookup(id) : id).join(', ');
    const more = ids.length > 6 ? ` +${ids.length - 6}` : '';
    return `${label}: ${head}${more}`;
  };
  const parts = [
    fmt(f.subject_ids, 'subj', (id) => {
      const ev = state.latestBySubject.get(id);
      return ev?.message_type ? `${id}` : `${id}`;
    }),
    fmt(f.service_ids, 'svc'),
    fmt(f.node_ids, 'node'),
    fmt(f.message_types, 'type'),
  ].filter(Boolean);
  return parts.join(' · ');
};

const _formatCardMeta = (rec) => {
  const start = new Date(rec.start_unix * 1000);
  if (rec.end_unix == null) return start.toLocaleString();
  // Stopped: show the full run window plus duration.
  const end = new Date(rec.end_unix * 1000);
  const sameDay = start.toDateString() === end.toDateString();
  const endLabel = sameDay ? end.toLocaleTimeString() : end.toLocaleString();
  const duration = formatUptime(rec.end_unix - rec.start_unix);
  return `${start.toLocaleString()} → ${endLabel} · ran ${duration}`;
};

const _buildCard = (rec) => {
  const card = document.createElement('div');
  const live = _isLiveRecording(rec);
  const autoStopped = !!rec.auto_stopped;
  card.className = `record-card${live ? ' live' : ''}${autoStopped ? ' auto-stopped' : ''}`;
  card.dataset.id = String(rec.id);

  const filterText = _filterSummary(rec);
  const legacy = rec.events_source === 'global';

  card.innerHTML = `
    <div class="record-card-head">
      <span class="record-dot ${live ? 'rec' : 'done'}" aria-hidden="true"></span>
      <span class="record-name">${escapeHtml(rec.name)}</span>
      ${legacy ? '<span class="record-badge" title="Legacy bookmark (Phase 1); reads from the shared events buffer">bookmark</span>' : ''}
      ${autoStopped ? '<span class="record-badge record-badge-warn" title="Auto-stopped when a limit was hit">auto-stopped</span>' : ''}
      <span class="record-meta">${escapeHtml(_formatCardMeta(rec))}</span>
    </div>
    <div class="record-card-bars">
      ${_progressBarHtml('Time', _timeProgressPct(rec), _formatTimeRight(rec), !rec.max_length_seconds)}
      ${_progressBarHtml('Events', _eventsProgressPct(rec), _formatEventsRight(rec), !rec.max_events)}
    </div>
    ${filterText ? `<div class="record-card-filter">${escapeHtml(filterText)}</div>` : '<div class="record-card-filter muted">no filter (recording everything)</div>'}
    ${rec.notes ? `<div class="record-card-notes">${escapeHtml(rec.notes)}</div>` : ''}
    <div class="record-card-actions">
      ${live ? '<button class="btn-mini" data-action="stop">Stop</button>' : ''}
      ${live ? '<button class="btn-mini" data-action="edit-limits" aria-label="Edit limits">Edit limits</button>' : ''}
      ${!live ? _playButtonHtml(rec) : ''}
      <button class="btn-mini" data-action="duplicate" aria-label="Start new recording with same configuration" title="Start a new recording with the same filter and limits">New like this</button>
      <button class="btn-mini" data-action="export-csv" aria-label="Export CSV">CSV</button>
      <button class="btn-mini" data-action="export-jsonl" aria-label="Export JSONL">JSONL</button>
      <button class="btn-mini" data-action="rename" aria-label="Rename">Rename</button>
      <button class="btn-mini" data-action="delete" aria-label="Delete">Delete</button>
      ${legacy ? '<button class="btn-mini btn-danger" data-action="purge" aria-label="Delete + purge events from global buffer" title="Delete recording AND its events from the global buffer">Purge</button>' : ''}
    </div>
  `;

  card.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    if (btn.disabled) return;
    // The card may outlive this render (see renderRecordList): act on the latest data.
    const rec = state.recordings.find((r) => r.id === Number(card.dataset.id));
    if (!rec) return;
    const action = btn.dataset.action;
    if (action === 'stop') stopRecording(rec.id);
    else if (action === 'edit-limits') openEditLimitsModal(rec);
    else if (action === 'play') startReplay(rec.id);
    else if (action === 'duplicate') duplicateRecording(rec);
    else if (action === 'export-csv') exportRecording(rec.id, 'csv');
    else if (action === 'export-jsonl') exportRecording(rec.id, 'jsonl');
    else if (action === 'rename') renameRecording(rec.id);
    else if (action === 'delete') deleteRecording(rec.id, false);
    else if (action === 'purge') deleteRecording(rec.id, true);
  });
  return card;
};

const _playButtonHtml = (rec) => {
  // Replay plays a recording's own events, with CAN disconnected and no
  // other replay running.
  const why = rec.events_source === 'global' ? 'A bookmark keeps no events of its own to replay'
    : !rec.event_count ? 'Nothing was recorded'
    : state.canConnected ? 'Disconnect from CAN to replay'
    : state.replayActive ? 'Another replay is already running' : '';
  return `<button class="btn-mini" data-action="play" aria-label="Replay recording"`
    + ` title="${escapeHtml(why || 'Replay this recording')}"${why ? ' disabled' : ''}>▶ Play</button>`;
};

// Why the list could not be loaded the last time, or null. Said once, in
// place, above the recordings as last loaded: every poll would toast it.
let _recordListError = null;

const renderRecordList = () => {
  const list = el('recordList');
  if (!list) return;
  if (!state.dashboardConnected) {
    list.innerHTML = '<div class="record-empty">Connect to the backend to view recordings.</div>';
    return;
  }
  const fresh = document.createElement('div');
  if (_recordListError) {
    fresh.innerHTML = `<div class="record-list-error" role="alert">Cannot load the recordings: ${escapeHtml(_recordListError)}.`
      + `${state.recordings.length ? ' These are as last loaded.' : ''}</div>`;
  } else if (!state.recordings.length) {
    list.innerHTML = '<div class="record-empty">No recordings yet. Build a selection on the right, set limits, then press Start.</div>';
    return;
  }
  // Patched, not rebuilt: live cards refresh every second, and a rebuilt
  // Stop button would swallow a click in progress.
  fresh.append(...state.recordings.map(_buildCard));
  patchChildren(list, fresh);
};

const _tickLiveCards = () => {
  if (!state.recordings.some(_isLiveRecording)) return;
  renderRecordList();
};

// ── Recording API actions ───────────────────────────────────────────

const _draftFilter = () => {
  const d = state.recordFilterDraft;
  const out = {};
  if (d.subject_ids.length) out.subject_ids = d.subject_ids;
  if (d.service_ids.length) out.service_ids = d.service_ids;
  if (d.node_ids.length) out.node_ids = d.node_ids;
  if (d.message_types.length) out.message_types = d.message_types;
  return Object.keys(out).length ? out : null;
};

const fetchRecordings = async () => {
  if (!state.dashboardConnected) {
    state.recordings = [];
    state.activeRecordingId = null;
    refreshRecordTabIndicator();
    renderRecordList();
    // Keep the poll loop alive even while disconnected so the list resumes
    // updating automatically once the backend comes back, without requiring
    // the user to switch tabs or refresh the page.
    _scheduleNextRecordPoll();
    return;
  }
  try {
    const data = await requestJson('/api/recordings');
    state.recordings = Array.isArray(data.recordings) ? data.recordings : [];
    state.activeRecordingId = (state.recordings.find(_isLiveRecording) || {}).id ?? null;
    _recordListError = null;
  } catch (e) {
    _recordListError = e.message;
  }
  refreshRecordTabIndicator();
  renderRecordList();
  _scheduleNextRecordPoll();
};

const _scheduleNextRecordPoll = () => {
  if (_recordPollTimer) { clearTimeout(_recordPollTimer); _recordPollTimer = null; }
  const hasLive = state.recordings.some(_isLiveRecording);
  if (_recordViewActive) {
    _recordPollTimer = setTimeout(fetchRecordings, hasLive ? RECORD_POLL_LIVE_MS : RECORD_POLL_IDLE_MS);
  } else if (hasLive) {
    _recordPollTimer = setTimeout(fetchRecordings, RECORD_POLL_IDLE_MS);
  }
};

// Start only when a recording can begin, and once at a time; say why not.
let _starting = false;

const renderRecordStart = () => {
  const button = el('recStart');
  if (!button) return;
  const why = !state.dashboardConnected ? 'Connect to the backend to record.'
    : !state.canConnected ? 'Connect a CAN interface to record.' : '';
  button.disabled = _starting || Boolean(why);
  el('recStartWhy').textContent = why;
};

const startRecording = async () => {
  const draft = state.recordFilterDraft;
  if (!draft.name.trim()) {
    showToast('Recording name is required', 'warn');
    return;
  }
  _starting = true;
  renderRecordStart();
  try {
    await requestJson('/api/recordings', {
      method: 'POST',
      body: JSON.stringify({
        name: draft.name.trim(),
        filter: _draftFilter(),
        notes: draft.notes || undefined,
        max_length_seconds: draft.max_length_seconds,
        max_events: draft.max_events,
        stop_on_limit: !!draft.stop_on_limit,
      }),
    });
    showToast('Recording started', 'success');
    await fetchRecordings();
  } catch (e) {
    showToast(`Start failed: ${e.message}`, 'error');
  } finally {
    _starting = false;
    renderRecordStart();
  }
};

const stopRecording = async (id) => {
  try {
    await requestJson(`/api/recordings/${id}/stop`, { method: 'POST' });
    await fetchRecordings();
  } catch (e) {
    showToast(`Stop failed: ${e.message}`, 'error');
  }
};

const renameRecording = async (id) => {
  const cur = state.recordings.find((r) => r.id === id);
  if (!cur) return;
  const name = window.prompt('Rename recording:', cur.name);
  if (!name || !name.trim() || name === cur.name) return;
  try {
    await requestJson(`/api/recordings/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: name.trim() }),
    });
    await fetchRecordings();
  } catch (e) {
    showToast(`Rename failed: ${e.message}`, 'error');
  }
};

const deleteRecording = async (id, purge) => {
  const cur = state.recordings.find((r) => r.id === id);
  const label = cur ? `"${cur.name}"` : `#${id}`;
  const msg = purge
    ? `Delete ${label} AND purge its events from the global buffer? This cannot be undone.`
    : `Delete ${label}?`;
  if (!window.confirm(msg)) return;
  try {
    await requestJson(`/api/recordings/${id}${purge ? '?purge=true' : ''}`, { method: 'DELETE' });
    showToast(`Deleted ${label}`, 'success');
    await fetchRecordings();
  } catch (e) {
    showToast(`Delete failed: ${e.message}`, 'error');
  }
};

// The browser downloads the file itself, so an error page would replace the
// dashboard: make sure the recording is still there first.
const exportRecording = async (id, fmt) => {
  try {
    await requestJson(`/api/recordings/${id}`);
  } catch (e) {
    showToast(`Export failed: ${e.message}`, 'error');
    fetchRecordings();
    return;
  }
  window.location.href = withTokenParam(`${apiBase()}/api/recordings/${id}/export?format=${fmt}`);
};

const duplicateRecording = async (rec) => {
  // Suggest a unique-ish name based on the source.
  const baseName = rec.name.replace(/\s*\(copy\s*\d*\)\s*$/, '');
  const existingCopies = state.recordings
    .filter((r) => r.name.startsWith(baseName))
    .map((r) => {
      const m = r.name.match(/\(copy\s*(\d*)\)\s*$/);
      return m ? Number(m[1] || 1) : 0;
    });
  const next = existingCopies.length ? Math.max(...existingCopies) + 1 : 1;
  const name = `${baseName} (copy ${next})`;
  try {
    await requestJson('/api/recordings', {
      method: 'POST',
      body: JSON.stringify({
        name,
        filter: Object.keys(rec.filter || {}).length ? rec.filter : null,
        notes: rec.notes || undefined,
        max_length_seconds: rec.max_length_seconds || undefined,
        max_events: rec.max_events || undefined,
        stop_on_limit: !!rec.stop_on_limit,
      }),
    });
    showToast(`Started "${name}"`, 'success');
    await fetchRecordings();
  } catch (e) {
    showToast(`Duplicate failed: ${e.message}`, 'error');
  }
};

// ── Edit-limits modal (live recordings) ─────────────────────────────

let _editLimitsRecId = null;
let _editLimitsModalEl = null;

const _ensureEditLimitsModal = () => {
  if (_editLimitsModalEl) return _editLimitsModalEl;
  const backdrop = document.createElement('div');
  backdrop.className = 'rec-modal-backdrop hidden';
  backdrop.id = 'recEditLimitsBackdrop';
  backdrop.innerHTML = `
    <div class="rec-modal" role="dialog" aria-modal="true" aria-labelledby="recEditLimitsTitle">
      <h3 class="rec-modal-title" id="recEditLimitsTitle">Edit limits</h3>
      <div class="record-limit-row">
        <label>
          <span>Length</span>
          <select id="recEditLength"></select>
        </label>
        <label>
          <span>Max events</span>
          <select id="recEditMaxEvents"></select>
        </label>
        <label class="record-stop-toggle">
          <input type="checkbox" id="recEditStopOnLimit" />
          <span>Stop when limit hit</span>
        </label>
      </div>
      <p class="rec-modal-hint">Changes apply immediately. Lowering a cap below the current count triggers auto-stop on the next event.</p>
      <div class="rec-modal-actions">
        <button class="btn-mini" id="recEditCancel">Cancel</button>
        <button class="btn-primary" id="recEditApply">Apply</button>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeEditLimitsModal(); });
  el('recEditCancel').addEventListener('click', closeEditLimitsModal);
  el('recEditApply').addEventListener('click', applyEditLimits);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !backdrop.classList.contains('hidden')) closeEditLimitsModal();
  });
  _editLimitsModalEl = backdrop;
  return backdrop;
};

// The recording's own limit first when the list lacks it (none at all, or a
// length set some other way), so that the dialog shows what it has.
const _limitOptionsHtml = (options, current, label) => {
  const value = current ?? '';
  const listed = options.some((o) => o.value === value);
  return _buildOptionsHtml(listed ? options
    : [{ label: value === '' ? 'No limit' : label(value), value }, ...options], value);
};

const openEditLimitsModal = (rec) => {
  _ensureEditLimitsModal();
  _editLimitsRecId = rec.id;
  el('recEditLimitsTitle').textContent = `Edit limits — ${rec.name}`;
  el('recEditLength').innerHTML = _limitOptionsHtml(LENGTH_OPTIONS, rec.max_length_seconds, formatUptime);
  el('recEditMaxEvents').innerHTML = _limitOptionsHtml(EVENTS_OPTIONS, rec.max_events, (n) => n.toLocaleString());
  el('recEditStopOnLimit').checked = !!rec.stop_on_limit;
  _editLimitsModalEl.classList.remove('hidden');
};

const closeEditLimitsModal = () => {
  if (_editLimitsModalEl) _editLimitsModalEl.classList.add('hidden');
  _editLimitsRecId = null;
};

const applyEditLimits = async () => {
  const id = _editLimitsRecId;
  if (id == null) return;
  // Only what was changed: the backend keeps a limit it is not sent, and
  // cannot take one away, so "No limit" left alone stays.
  const rec = state.recordings.find((r) => r.id === id) || {};
  const length = el('recEditLength').value;
  const events = el('recEditMaxEvents').value;
  const stopOnLimit = el('recEditStopOnLimit').checked;
  const body = {};
  if (length !== String(rec.max_length_seconds ?? '')) body.max_length_seconds = Number(length);
  if (events !== String(rec.max_events ?? '')) body.max_events = Number(events);
  if (stopOnLimit !== !!rec.stop_on_limit) body.stop_on_limit = stopOnLimit;
  if (!Object.keys(body).length) {
    closeEditLimitsModal();
    return;
  }
  try {
    await requestJson(`/api/recordings/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    closeEditLimitsModal();
    showToast('Limits updated', 'success');
    await fetchRecordings();
  } catch (e) {
    showToast(`Update failed: ${e.message}`, 'error');
  }
};

// ── View init ───────────────────────────────────────────────────────

const _initPickers = () => {
  const subjectsEl = el('recSubjectsPicker');
  const nodesEl = el('recNodesPicker');
  const selectionEl = el('recSelectionPicker');
  const commonOpts = {
    layout: 'fitColumns',
    height: '14rem',
    placeholder: 'No data',
    selectable: false,
    movableColumns: false,
    index: 'key',
  };
  const tallOpts = { ...commonOpts, height: '45vh' };
  _subjectsPicker = new Tabulator(subjectsEl, {
    ...tallOpts,
    data: _subjectsPickerData(),
    rowFormatter: (row) => {
      const ids = row.getData().ownerIds || [];
      const hit = _highlightedNodeId != null && ids.includes(_highlightedNodeId);
      row.getElement().classList.toggle('rec-row-highlighted', hit);
    },
    columns: [
      { title: 'ID', field: 'id', width: 80, sorter: 'number' },
      { title: 'Type', field: 'label' },
      { title: 'Owner', field: 'owner', width: 110 },
      { title: 'Rate', field: 'rate', width: 70 },
    ],
  });
  _subjectsPicker.on('rowClick', (e, row) => {
    if (_isSecondClick(row.getData().key)) _addToSelection(row.getData());
  });

  _nodesPicker = new Tabulator(nodesEl, {
    ...tallOpts,
    data: _nodesPickerData(),
    rowFormatter: (row) => {
      const isSel = row.getData().id === _highlightedNodeId;
      row.getElement().classList.toggle('rec-row-selected', isSel);
    },
    columns: [
      { title: 'ID', field: 'id', width: 80, sorter: 'number' },
      { title: 'Name', field: 'name' },
      { title: 'Pubs', field: 'publishers', width: 70, sorter: 'number' },
      { title: 'Srvs', field: 'servers', width: 70, sorter: 'number' },
    ],
  });
  _nodesPicker.on('rowClick', (e, row) => {
    const data = row.getData();
    if (_isSecondClick(data.key)) _addToSelection(data);
    else _setHighlightedNode(data.id);
  });

  _selectionPicker = new Tabulator(selectionEl, {
    ...commonOpts,
    height: '18vh',
    placeholder: 'Empty = record everything. Build a selection on the pickers above.',
    data: _selectionData(),
    columns: [
      { title: 'Kind', field: 'kind', width: 90 },
      { title: 'ID', field: 'id', width: 80, sorter: 'number' },
      { title: 'Label', field: 'label' },
      {
        title: '', width: 50, hozAlign: 'center',
        formatter: () => '<button class="btn-mini" aria-label="Remove from selection">×</button>',
        cellClick: (e, cell) => _removeFromSelection(cell.getRow().getData()),
      },
    ],
  });
  // Click anywhere on a selection row also removes it (in addition to the × button).
  _selectionPicker.on('rowClick', (e, row) => _removeFromSelection(row.getData()));
  for (const picker of [_subjectsPicker, _nodesPicker, _selectionPicker]) {
    picker.on('tableBuilt', () => _builtPickers.add(picker));
  }
};

const _buildOptionsHtml = (opts, current) =>
  opts.map((o) => `<option value="${o.value}"${o.value === current ? ' selected' : ''}>${escapeHtml(o.label)}</option>`).join('');

const _renderViewShell = (container) => {
  const draft = state.recordFilterDraft;
  container.innerHTML = `
    <div class="record-view">
      <header class="record-header">
        <div class="record-header-text">
          <h2>Recordings</h2>
          <span class="record-hint">Pick subjects, services, and nodes on the right. Set limits. Press Start. Live recordings are stored independently and survive the global buffer.</span>
        </div>
        <span class="record-buffer-chip" id="recBufferChip"></span>
      </header>
      <section class="rawlog-panel" id="rawLogPanel" aria-label="Raw CAN log"></section>
      <div class="record-layout">
        <section class="record-list-pane">
          <div class="record-list" id="recordList"></div>
        </section>
        <section class="record-builder-pane">
          <div class="record-pickers-row">
            <div class="record-builder-block">
              <h3 class="record-builder-h">Subjects/services <span class="record-builder-sub">double-click to add</span></h3>
              <div id="recSubjectsPicker" class="record-picker"></div>
            </div>
            <div class="record-builder-block">
              <h3 class="record-builder-h">Nodes <span class="record-builder-sub">double-click to add · click to highlight subjects</span></h3>
              <div id="recNodesPicker" class="record-picker"></div>
            </div>
          </div>
          <div class="record-builder-block">
            <h3 class="record-builder-h">Selection <span class="record-builder-sub">click to remove</span></h3>
            <div id="recSelectionPicker" class="record-picker"></div>
          </div>
          <div class="record-builder-block record-limits">
            <div class="record-limit-row">
              <label>
                <span>Length</span>
                <select id="recLength">${_buildOptionsHtml(LENGTH_OPTIONS, draft.max_length_seconds)}</select>
              </label>
              <label>
                <span>Max events</span>
                <select id="recMaxEvents">${_buildOptionsHtml(EVENTS_OPTIONS, draft.max_events)}</select>
              </label>
              <label class="record-stop-toggle">
                <input type="checkbox" id="recStopOnLimit"${draft.stop_on_limit ? ' checked' : ''} />
                <span>Stop when limit hit</span>
              </label>
            </div>
            <div class="record-disk-hint" id="recDiskHint"></div>
          </div>
          <div class="record-builder-block">
            <label class="record-field">
              <span>Name</span>
              <input type="text" id="recName" placeholder="e.g. boot sequence" autocomplete="off" />
            </label>
            <label class="record-field">
              <span>Notes</span>
              <textarea id="recNotes" rows="2" placeholder="Optional" autocomplete="off"></textarea>
            </label>
            <div class="record-actions">
              <button id="recStart" class="btn-primary">Start recording</button>
            </div>
            <p class="record-start-why" id="recStartWhy" role="status"></p>
          </div>
        </section>
      </div>
    </div>
  `;
};

const _bindBuilderInputs = () => {
  const draft = state.recordFilterDraft;
  el('recName').value = draft.name;
  el('recNotes').value = draft.notes;

  el('recName').addEventListener('input', (e) => { draft.name = e.target.value; saveSettings(); });
  el('recNotes').addEventListener('input', (e) => { draft.notes = e.target.value; saveSettings(); });

  el('recLength').addEventListener('change', (e) => {
    draft.max_length_seconds = Number(e.target.value);
    saveSettings();
    _updateDiskHint();
  });
  el('recMaxEvents').addEventListener('change', (e) => {
    draft.max_events = Number(e.target.value);
    saveSettings();
    _updateDiskHint();
  });
  el('recStopOnLimit').addEventListener('change', (e) => {
    draft.stop_on_limit = e.target.checked;
    saveSettings();
  });

  el('recStart').addEventListener('click', startRecording);
  renderRecordStart();
};

// ── Raw CAN log: every frame on the bus, as a candump .log file ──

const RAW_LOG_POLL_MS = 2000;
let _rawLogState = null;   // last GET /api/rawlogs
let _rawLogTimer = null;

const fetchRawLogs = async () => {
  try {
    _rawLogState = state.dashboardConnected ? await requestJson('/api/rawlogs') : null;
  } catch {
    _rawLogState = null;
  }
  _renderRawLogPanel();
};

// Speeds a raw log can be played at, as [value, label]; 0 = as fast as possible.
const RAW_LOG_SPEEDS = [[1, '1×'], [10, '10×'], [100, '100×'], [0, 'as fast as possible']];

const _rawLogRow = (log) => `
  <li class="rawlog-item">
    <span class="record-name">${escapeHtml(log.name)}</span>
    <span class="record-meta">${escapeHtml(formatBytes(log.bytes))}</span>
    <button type="button" class="btn-mini" data-rawlog-play="${escapeHtml(log.name)}"
            ${state.canConnected ? 'disabled title="Disconnect CAN to play a raw log"' : 'title="Play it as if it were the bus"'}>Play</button>
    <a class="btn-mini" download
       href="${escapeHtml(withTokenParam(`${apiBase()}/api/rawlogs/${encodeURIComponent(log.name)}`))}">Download</a>
    <button type="button" class="btn-mini btn-danger" data-rawlog-delete="${escapeHtml(log.name)}">Delete</button>
  </li>`;

const _renderRawLogPanel = () => {
  const panel = el('rawLogPanel');
  if (!panel) return;
  const { active, logs } = _rawLogState || { active: null, logs: [] };
  const control = active
    ? `<span class="record-dot rec" aria-hidden="true"></span>
       <span class="record-name">${escapeHtml(active.name)}</span>
       <span class="record-meta">${Number(active.frames).toLocaleString()} frames · ${escapeHtml(formatBytes(active.bytes))}</span>
       ${active.error ? `<span class="record-badge record-badge-warn">${escapeHtml(active.error)}</span>` : ''}
       <button type="button" class="btn-mini" data-rawlog="stop">Stop</button>`
    : `<button type="button" class="btn-mini" data-rawlog="start"${state.canConnected ? '' : ' disabled title="Connect CAN first"'}>Start raw log</button>`;
  const saved = (logs || []).filter((log) => !active || log.name !== active.name);
  const speeds = RAW_LOG_SPEEDS.map(([value, label]) =>
    `<option value="${value}"${value === state.rawLogPlaybackSpeed ? ' selected' : ''}>${label}</option>`).join('');
  const speedPicker = saved.length
    ? `<label class="rawlog-speed">Play at <select data-rawlog="speed" aria-label="Playback speed">${speeds}</select></label>`
    : '';
  const fresh = document.createElement('div');
  fresh.innerHTML = `
    <div class="rawlog-head">
      <h3 class="record-builder-h">Raw CAN log
        <span class="record-builder-sub">every frame, candump .log · opens in python-can, SavvyCAN, can-utils</span></h3>
      <div class="rawlog-active">${speedPicker}${control}</div>
    </div>
    ${saved.length ? `<ul class="rawlog-list">${saved.map(_rawLogRow).join('')}</ul>` : ''}`;
  patchChildren(panel, fresh);  // keeps Stop in place while the frame count ticks
};

const _onRawLogClick = async (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  try {
    if (btn.dataset.rawlog === 'start') {
      await requestJson('/api/rawlogs', { method: 'POST' });
    } else if (btn.dataset.rawlog === 'stop') {
      await requestJson('/api/rawlogs/stop', { method: 'POST' });
    } else if (btn.dataset.rawlogPlay) {
      await requestJson(`/api/rawlogs/${encodeURIComponent(btn.dataset.rawlogPlay)}/play`, {
        method: 'POST',
        body: JSON.stringify({ speed: state.rawLogPlaybackSpeed }),
      });
      showToast(`Playing ${btn.dataset.rawlogPlay}; Disconnect stops it`, 'info');
      pollStatus();  // shows it as the connected interface now, not at the next poll
    } else if (btn.dataset.rawlogDelete) {
      const name = btn.dataset.rawlogDelete;
      if (!window.confirm(`Delete ${name}?`)) return;
      await requestJson(`/api/rawlogs/${encodeURIComponent(name)}`, { method: 'DELETE' });
    } else {
      return;
    }
  } catch (err) {
    showToast(`Raw log: ${err?.data?.error || err.message}`, 'error');
  }
  fetchRawLogs();
};

// Builds the tab, the first time it is shown; setRecordViewActive, which
// follows, fills it and keeps it current.
const initRecordView = () => {
  const container = el('recordContainer');
  if (container.dataset.ready) return;
  container.dataset.ready = '1';
  _renderViewShell(container);
  el('rawLogPanel').addEventListener('click', _onRawLogClick);
  el('rawLogPanel').addEventListener('change', (e) => {
    if (e.target.dataset.rawlog === 'speed') state.rawLogPlaybackSpeed = Number(e.target.value);
  });
  _initPickers();
  _bindBuilderInputs();
};

const setRecordViewActive = (active) => {
  _recordViewActive = !!active;
  if (active) {
    _refreshPickerTables();
    _refreshSelection();
    fetchRecordings();
    fetchRecordBuffer();
    if (!_bufferPollTimer) _bufferPollTimer = setInterval(fetchRecordBuffer, BUFFER_POLL_MS);
    if (!_cardsTickerTimer) _cardsTickerTimer = setInterval(_tickLiveCards, 1000);
    if (!_pickerRefreshTimer) _pickerRefreshTimer = setInterval(_refreshPickerTables, PICKER_REFRESH_MS);
    fetchRawLogs();
    if (!_rawLogTimer) _rawLogTimer = setInterval(fetchRawLogs, RAW_LOG_POLL_MS);
  } else {
    if (_rawLogTimer) { clearInterval(_rawLogTimer); _rawLogTimer = null; }
    if (_bufferPollTimer) { clearInterval(_bufferPollTimer); _bufferPollTimer = null; }
    if (_cardsTickerTimer) { clearInterval(_cardsTickerTimer); _cardsTickerTimer = null; }
    if (_pickerRefreshTimer) { clearInterval(_pickerRefreshTimer); _pickerRefreshTimer = null; }
    _scheduleNextRecordPoll();
  }
};
