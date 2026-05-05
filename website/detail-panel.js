// Detail panel: per-node subject cards (Publishers/Subscribers/Servers/Clients
// tabs), client cards, and the per-row selection helpers used by nodes-table.js.
// Plot code lives in plot.js.

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

    return `<div class="subject-card" data-subject="${s.subjectId}" tabindex="0" role="button">
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

const renderClientCards = (clients, enrichedMap) => {
  const cards = clients.map((clientId) => {
    const info = enrichedMap.get(clientId);
    const typeName = info?.full_type || '';
    const serverNodes = info?.server_nodes || [];
    const serverHtml = serverNodes.length
      ? `<span class="svc-client-servers" title="Nodes serving this service">→ node ${serverNodes.join(', ')}</span>`
      : '';
    return `<div class="svc-card">
      <div class="svc-card-header svc-card-header-static">
        <span class="svc-service-id">${clientId}</span>
        <span class="svc-service-type" title="${escapeHtml(typeName)}">${escapeHtml(typeName || `Client ${clientId}`)}</span>
        ${serverHtml}
      </div>
    </div>`;
  }).join('');
  return `<section class="svc-panel">${cards}</section>`;
};

const renderClientsTab = async () => {
  const content = el('selectedNodeContent');
  const nodeId = state.selectedNodeId;

  if (!state.dashboardConnected || state.canState !== CONN.CONNECTED) {
    content.innerHTML = svcStateMsg('○', 'No clients advertised', 'Connect to the CAN bus to see client info.');
    return;
  }
  if (nodeId == null) {
    content.innerHTML = svcStateMsg('○', 'Select a node', 'Choose a node to view its client ports.');
    return;
  }

  const node = getSelectedNode();
  const clients = node?.clients || [];
  if (!clients.length) {
    content.innerHTML = svcStateMsg('○', 'No clients advertised', 'This node does not use any service clients.');
    return;
  }

  if (node.has_disappeared) {
    const clientStaleLabel = getNodeAlias(node.unique_id) || node.name || `Node ${nodeId}`;
    content.innerHTML = `<div class="svc-stale-banner" role="alert"><span class="svc-stale-icon">⚠</span>${escapeHtml(clientStaleLabel)} is offline — client data may be stale.</div>`
      + `<div class="svc-panel-stale">${renderClientCards(clients, new Map())}</div>`;
    return;
  }

  content.innerHTML = svcStateMsg('<span class="svc-spinner"></span>', 'Loading client info…', '');

  let enriched = null;
  try {
    const data = await requestJson(`/api/clients/${nodeId}`);
    enriched = data.clients || [];
  } catch {
    enriched = null;
  }

  if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'clients') return;

  const enrichedMap = new Map();
  if (enriched) {
    for (const c of enriched) enrichedMap.set(c.service_id, c);
  }

  content.innerHTML = renderClientCards(clients, enrichedMap);
};

const renderListTab = (title, items) => `
  <section class="details-panel">
    <h3>${escapeHtml(title)}</h3>
    <ul class="details-list">
      ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
    </ul>
  </section>
`;

const renderSelectedNodeContent = () => {
  const content = el('selectedNodeContent');
  const node = getSelectedNode();

  document.querySelectorAll('.detail-tab').forEach((button) => {
    const isActive = button.dataset.tab === state.selectedDetailTab;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', String(isActive));
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
    if (!state.dashboardConnected) {
      content.innerHTML = state.pendingReconnect
        ? svcStateMsg('<span class="svc-spinner"></span>', 'Reconnecting to backend…', 'Restoring previous session.')
        : svcStateMsg('⏻', 'Not connected to backend', 'Connect to the backend server to inspect node details.');
    } else if (state.canState !== CONN.CONNECTED) {
      content.innerHTML = state.canState === CONN.CONNECTING
        ? svcStateMsg('<span class="svc-spinner"></span>', 'Connecting to CAN interface…', 'Establishing CAN bus connection.')
        : svcStateMsg('⛓', 'CAN bus not connected', 'Connect a CAN interface to discover online nodes.');
    } else if (state.selectedNodeId == null) {
      content.innerHTML = svcStateMsg('◎', 'Select a node to inspect details', 'Choose a node from the table above.');
    } else {
      const offlineNode = state.latestNodesPayload?.nodes?.[state.selectedNodeId];
      const offlineLabel = getNodeAlias(offlineNode?.unique_id) || offlineNode?.name || `Node ${state.selectedNodeId}`;
      content.innerHTML = svcStateMsg('⚠', `${offlineLabel} is offline`, 'This node disappeared from the CAN bus.');
    }
    return;
  }

  const wasOffline = content.dataset.nodeOffline === 'true';
  const isOffline = !!node.has_disappeared;
  content.dataset.nodeOffline = String(isOffline);

  if (wasOffline !== isOffline) {
    delete content.dataset.svcTab;
  }

  const staleLabel = getNodeAlias(node.unique_id) || node.name || `Node ${node.node_id}`;
  const staleBanner = isOffline
    ? `<div class="svc-stale-banner" role="alert"><span class="svc-stale-icon">⚠</span>${escapeHtml(staleLabel)} is offline — data may be stale.</div>`
    : '';

  const transitioned = wasOffline !== isOffline;

  const renderSubjectTab = (title, subjects) => {
    if (!staleBanner && !transitioned && updateSubjectTableInPlace(content, subjects)) {
      if (!state.plotTimer) startPlotAnim();
      return;
    }
    const selected = state.selectedPlotSubject;
    const pct = (state.splitRatio * 100).toFixed(1);
    content.innerHTML = `${staleBanner}<div class="detail-split${isOffline ? ' svc-panel-stale' : ''}">
      <div class="detail-subject-list" style="flex:0 0 ${pct}%">${renderSubjectTable(title, subjects)}</div>
      <div class="detail-split-handle"></div>
      <div class="detail-plot-area">${''}
      </div>
    </div>`;
    content.querySelectorAll('.subject-card').forEach((card) => {
      if (Number(card.dataset.subject) === selected) card.classList.add('selected');
    });
    bindSplitHandle(content.querySelector('.detail-split'));
    if (!isOffline) startPlotAnim();
  };

  if (state.selectedDetailTab !== 'servers' && state.selectedDetailTab !== 'clients' && state.selectedDetailTab !== 'registers') {
    delete content.dataset.svcTab;
  }

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
    if (content.dataset.svcTab !== 'servers' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'servers';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderServicesTab();
    }
    return;
  }

  if (state.selectedDetailTab === 'clients') {
    stopPlotAnim();
    if (content.dataset.svcTab !== 'clients' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'clients';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderClientsTab();
    }
    return;
  }

  if (state.selectedDetailTab === 'registers') {
    stopPlotAnim();
    if (content.dataset.svcTab !== 'registers' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'registers';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderRegistersTab();
    }
    return;
  }

  renderSubjectTab('Publishers', buildSubjectDetailData(node.publishers || [], node.node_id));
};

const scheduleDetailRefresh = () => {
  if (_detailRefreshPending) return;
  _detailRefreshPending = window.setTimeout(() => {
    _detailRefreshPending = null;
    renderSelectedNodeContent();
  }, 100);
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
