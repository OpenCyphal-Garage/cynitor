// History tab: node lifecycle timeline and subject activity summary.
// Fetches from /api/nodes/{id}/history and /api/nodes/{id}/history/subjects.

const HISTORY_RANGES = ['5m', '15m', '1h', '6h', '24h', '7d'];

const HISTORY_EVENT_LABELS = {
  first_seen: 'First seen',
  appeared: 'Appeared',
  reappeared: 'Reappeared',
  disappeared: 'Disappeared',
  restart_suspected: 'Restart',
  health_change: 'Health',
  mode_change: 'Mode',
  port_change: 'Ports changed',
  service_call: 'Service call',
  got_node_id: 'Got node ID',
  lost_node_id: 'Lost node ID',
};

const HISTORY_EVENT_CLS = {
  first_seen: 'hist-ok',
  appeared: 'hist-ok',
  reappeared: 'hist-ok',
  disappeared: 'hist-err',
  restart_suspected: 'hist-warn',
  health_change: 'hist-warn',
  mode_change: 'hist-warn',
  port_change: 'hist-info',
  service_call: 'hist-muted',
  got_node_id: 'hist-ok',
  lost_node_id: 'hist-err',
};

const formatHistoryTime = (unix) => {
  const d = new Date(unix * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const now = new Date();
  if (d.toDateString() !== now.toDateString()) {
    const mon = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${mon}-${day} ${hh}:${mm}:${ss}`;
  }
  return `${hh}:${mm}:${ss}`;
};

const formatEventDetail = (evt) => {
  const d = evt.detail;
  if (!d) return '';
  switch (evt.event_type) {
    case 'health_change':
      return `${escapeHtml(d.old)} → ${escapeHtml(d.new)}`;
    case 'mode_change':
      return `${escapeHtml(d.old)} → ${escapeHtml(d.new)}`;
    case 'restart_suspected':
      return `uptime ${d.old_uptime}s → ${d.new_uptime}s`;
    case 'service_call':
      return `svc ${d.service_id} · ${d.status} · ${d.latency_ms}ms`;
    case 'got_node_id':
    case 'lost_node_id':
      return `#${d.node_id}`;
    case 'port_change': {
      const parts = [];
      for (const kind of ['publishers', 'subscribers', 'servers', 'clients']) {
        const oldSet = new Set(d.old?.[kind] || []);
        const newSet = new Set(d.new?.[kind] || []);
        const added = [...newSet].filter((x) => !oldSet.has(x));
        const removed = [...oldSet].filter((x) => !newSet.has(x));
        if (added.length) parts.push(`+${kind}: ${added.join(', ')}`);
        if (removed.length) parts.push(`-${kind}: ${removed.join(', ')}`);
      }
      return escapeHtml(parts.join(' | ') || 'ports updated');
    }
    default:
      return '';
  }
};

const renderHistoryTimeline = (events) => {
  if (!events.length) {
    return '<div class="hist-empty">No events in this time range.</div>';
  }
  const rows = events.map((evt) => {
    const cls = HISTORY_EVENT_CLS[evt.event_type] || '';
    const label = HISTORY_EVENT_LABELS[evt.event_type] || evt.event_type;
    const detail = formatEventDetail(evt);
    const detailHtml = detail ? `<span class="hist-detail">${detail}</span>` : '';
    return `<div class="hist-row">
      <span class="hist-time">${formatHistoryTime(evt.timestamp_unix)}</span>
      <span class="hist-badge ${cls}">${escapeHtml(label)}</span>
      ${detailHtml}
    </div>`;
  }).join('');
  return `<div class="hist-timeline">${rows}</div>`;
};

const renderSubjectSummary = (subjects) => {
  if (!subjects.length) return '';
  const rows = subjects.map((s) => {
    const firstSeen = formatHistoryTime(s.first_seen_unix);
    const lastSeen = formatHistoryTime(s.last_seen_unix);
    return `<div class="hist-subj-row">
      <span class="hist-subj-id">${s.subject_id}</span>
      <span class="hist-subj-type" title="${escapeHtml(s.message_type)}">${escapeHtml(s.message_type)}</span>
      <span class="hist-subj-count">${s.total_events}</span>
      <span class="hist-subj-rate">${s.avg_rate} Hz</span>
      <span class="hist-subj-range">${firstSeen} — ${lastSeen}</span>
    </div>`;
  }).join('');
  return `<details class="hist-subjects">
    <summary>Subject activity (${subjects.length})</summary>
    <div class="hist-subj-header">
      <span>ID</span><span>Type</span><span>Events</span><span>Avg rate</span><span>Range</span>
    </div>
    ${rows}
  </details>`;
};

const renderHistoryTab = async () => {
  const content = el('selectedNodeContent');
  const nodeId = state.selectedNodeId;

  if (!state.dashboardConnected || state.canState !== CONN.CONNECTED) {
    content.innerHTML = svcStateMsg('○', 'Connect to view history', 'Connect to the CAN bus to see node history.');
    return;
  }
  if (nodeId == null) {
    content.innerHTML = svcStateMsg('○', 'Select a node', 'Choose a node to view its history.');
    return;
  }

  const rangeButtons = HISTORY_RANGES.map((r) =>
    `<button class="hist-range-btn${r === state.historyTimeRange ? ' active' : ''}" data-range="${r}">${r}</button>`
  ).join('');

  content.innerHTML = `<section class="hist-panel">
    <div class="hist-toolbar">
      <div class="hist-range-btns">${rangeButtons}</div>
    </div>
    <div class="hist-body">
      ${svcStateMsg('<span class="svc-spinner"></span>', 'Loading history…', '')}
    </div>
  </section>`;

  content.querySelector('.hist-range-btns').addEventListener('click', (e) => {
    const btn = e.target.closest('.hist-range-btn');
    if (!btn) return;
    state.historyTimeRange = btn.dataset.range;
    delete content.dataset.svcTab;
    renderHistoryTab();
  });

  try {
    const node = getSelectedNode();
    const apiNodeId = node?.node_id ?? 0;
    const uidParam = node?.unique_id_hex ? `&unique_id=${node.unique_id_hex}` : '';
    const [histData, subjData] = await Promise.all([
      requestJson(`/api/nodes/${apiNodeId}/history?range=${state.historyTimeRange}${uidParam}`),
      requestJson(`/api/nodes/${apiNodeId}/history/subjects${uidParam ? '?unique_id=' + node.unique_id_hex : ''}`),
    ]);

    if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'history') return;

    const events = histData.events || [];
    const subjects = subjData.subjects || [];

    const body = content.querySelector('.hist-body');
    if (body) {
      body.innerHTML = renderHistoryTimeline(events) + renderSubjectSummary(subjects);
    }
  } catch (err) {
    const body = content.querySelector('.hist-body');
    if (body) {
      body.innerHTML = svcStateMsg('⚠', 'Failed to load history', String(err));
    }
  }
};
