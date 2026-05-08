// Service interaction panel — discover, invoke, and inspect Cyphal services.
// Implements full empty/loading/error state cascade per state design.

const SVC_SCHEMA_ERROR = Symbol('svc-schema-error');

const svcStateMsg = (icon, message, helper, actionHtml) => `
  <div class="svc-state">
    <span class="svc-state-icon">${icon}</span>
    <p class="svc-state-message">${escapeHtml(message)}</p>
    ${helper ? `<p class="svc-state-helper">${escapeHtml(helper)}</p>` : ''}
    ${actionHtml || ''}
  </div>`;

const svcSkeleton = () => `
  <div class="svc-panel svc-panel-skeleton">
    <div class="svc-skeleton-card"></div>
    <div class="svc-skeleton-card"></div>
    <div class="svc-skeleton-card"></div>
  </div>`;

const fetchServiceSchema = async (nodeId) => {
  try {
    const data = await requestJson(`/api/services/${nodeId}`);
    const services = data.services || [];
    state.serviceSchemas.set(nodeId, services);
    if (services.some((s) => s.callable === false)) {
      setTimeout(() => state.serviceSchemas.delete(nodeId), 3000);
    }
  } catch {
    state.serviceSchemas.set(nodeId, SVC_SCHEMA_ERROR);
    setTimeout(() => state.serviceSchemas.delete(nodeId), 5000);
  }
};

const renderServiceField = (field) => {
  const id = `svc-field-${field.name}`;
  const typeHint = field.type || '';
  if (field.kind === 'composite' && field.fields && field.fields.length) {
    const subInputs = field.fields.map((sub) => `
      <div class="svc-subfield">
        <label class="svc-field-label" for="svc-field-${field.name}.${sub.name}">
          ${escapeHtml(sub.name)} <span class="svc-type-hint">${escapeHtml(sub.type)}</span>
        </label>
        <input class="svc-field-input" id="svc-field-${field.name}.${sub.name}"
               data-field="${escapeHtml(field.name)}" data-subfield="${escapeHtml(sub.name)}"
               data-type="${escapeHtml(field.type)}"
               placeholder="${escapeHtml(sub.type)}" />
      </div>
    `).join('');
    return `<details class="svc-fieldset">
      <summary class="svc-field-label svc-fieldset-toggle">${escapeHtml(field.name)} <span class="svc-type-hint">${escapeHtml(typeHint)}</span></summary>
      <div class="svc-fieldset-body">${subInputs}</div>
    </details>`;
  }
  return `<div class="svc-field">
    <label class="svc-field-label" for="${id}">
      ${escapeHtml(field.name)} <span class="svc-type-hint">${escapeHtml(typeHint)}</span>
    </label>
    <input class="svc-field-input" id="${id}" data-field="${escapeHtml(field.name)}" placeholder="${escapeHtml(typeHint)}" />
  </div>`;
};

const renderServiceHistory = (_nodeId, serviceId) => {
  return `<div class="svc-history-container" data-service-id="${serviceId}"></div>`;
};

const _loadPersistentHistory = async (parentEl, nodeId) => {
  const containers = parentEl.querySelectorAll('.svc-history-container');
  for (const container of containers) {
    const serviceId = Number(container.dataset.serviceId);
    if (!serviceId) continue;
    try {
      const node = getSelectedNode();
      const uidParam = node?.unique_id_hex ? `&unique_id=${node.unique_id_hex}` : (nodeId != null ? `&node_id=${nodeId}` : '');
      const data = await requestJson(`/api/services/${serviceId}/history?range=7d&limit=50${uidParam}`);
      const entries = data.history || [];
      if (!entries.length) {
        container.innerHTML = '';
        continue;
      }
      const isOpen = state._svcHistoryOpen ? ' open' : '';
      let html = `<details class="svc-history"${isOpen}><summary class="svc-history-toggle">History (${entries.length})</summary><div class="svc-history-list">`;
      for (const entry of entries) {
        const time = new Date(entry.timestamp_unix * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
        const date = new Date(entry.timestamp_unix * 1000).toLocaleDateString([], { day: '2-digit', month: '2-digit' });
        const nodeName = entry.node_name || '';
        const nodeLabel = `Node ${entry.node_id}${nodeName ? ' (' + escapeHtml(nodeName) + ')' : ''}`;
        const uid = entry.node_unique_id ? entry.node_unique_id.map((b) => b.toString(16).padStart(2, '0')).join('') : '';
        const uidHtml = uid ? `<span class="svc-history-uid" title="${escapeHtml(uid)}">${escapeHtml(uid.slice(0, 8))}…</span>` : '';

        const responseHtml = entry.response
          ? `<pre class="svc-history-body">${escapeHtml(entry.response)}</pre>`
          : '';

        if (entry.status === 'ok') {
          if (entry.response) {
            html += `<details class="svc-history-entry svc-history-ok">
              <summary class="svc-history-row">
                <span class="svc-history-time">${escapeHtml(date)} ${escapeHtml(time)}</span>
                <span class="svc-history-node">${escapeHtml(nodeLabel)}</span>${uidHtml}
                <span class="svc-status-badge svc-badge-ok">&#10003; ${entry.latency_ms}ms</span>
              </summary>
              ${responseHtml}
            </details>`;
          } else {
            html += `<div class="svc-history-entry svc-history-row svc-history-ok">
              <span class="svc-history-time">${escapeHtml(date)} ${escapeHtml(time)}</span>
              <span class="svc-history-node">${escapeHtml(nodeLabel)}</span>${uidHtml}
              <span class="svc-status-badge svc-badge-ok">&#10003; ${entry.latency_ms}ms</span>
            </div>`;
          }
        } else if (entry.status === 'timeout') {
          html += `<div class="svc-history-entry svc-history-row svc-history-timeout">
            <span class="svc-history-time">${escapeHtml(date)} ${escapeHtml(time)}</span>
            <span class="svc-history-node">${escapeHtml(nodeLabel)}</span>${uidHtml}
            <span class="svc-status-badge svc-badge-timeout">&#10007; timeout ${entry.latency_ms}ms</span>
          </div>`;
        } else {
          html += `<div class="svc-history-entry svc-history-row svc-history-error">
            <span class="svc-history-time">${escapeHtml(date)} ${escapeHtml(time)}</span>
            <span class="svc-history-node">${escapeHtml(nodeLabel)}</span>${uidHtml}
            <span class="svc-status-badge svc-badge-error">&#10007; ${escapeHtml(entry.status || 'error')}</span>
          </div>`;
        }
      }
      html += '</div></details>';
      container.innerHTML = html;
      const detailsEl = container.querySelector('details.svc-history');
      if (detailsEl) {
        detailsEl.addEventListener('toggle', () => { state._svcHistoryOpen = detailsEl.open; });
      }
    } catch {
      container.innerHTML = '';
    }
  }
};

const renderServiceCard = (svc, forSubjects = false) => {
  const isCallable = svc.callable !== false;
  const expandedId = forSubjects ? state._subjectExpandedServiceId : state.expandedServiceId;
  const isExpanded = isCallable && expandedId === svc.service_id;
  const typeName = svc.full_type ? escapeHtml(svc.full_type) : `Service ${svc.service_id}`;
  const hasFields = svc.request_fields && svc.request_fields.length > 0;
  const schemaIncomplete = isCallable && !svc.request_fields;

  let formHtml = '';
  if (isExpanded) {
    let fieldsHtml;
    if (schemaIncomplete) {
      fieldsHtml = '<p class="svc-no-fields svc-schema-warn">Schema unavailable — you can still send an empty request.</p>';
    } else if (!hasFields) {
      fieldsHtml = '<p class="svc-no-fields">No request fields — sends empty request.</p>';
    } else {
      fieldsHtml = svc.request_fields.map(renderServiceField).join('');
    }

    const callState = forSubjects ? state._subjectServiceCallState : state.serviceCallState;
    const activeNodeId = forSubjects ? state._subjectServiceNodeId : state.selectedNodeId;
    const isThisCall = callState && callState.serviceId === svc.service_id && callState.nodeId === activeNodeId;
    const isSending = isThisCall && callState.status === 'sending';

    let responseHtml = '';
    if (!isThisCall || callState.status === 'idle') {
      responseHtml = `<div class="svc-response svc-response-idle">
        <span class="svc-idle-text">Response will appear here after sending a request.</span>
      </div>`;
    } else if (isSending) {
      responseHtml = `<div class="svc-response svc-response-sending">
        <span class="svc-spinner"></span>
        <span class="svc-sending-text">Waiting for response from node ${activeNodeId}…</span>
      </div>`;
    } else if (callState.status === 'done') {
      responseHtml = `<div class="svc-response svc-response-ok">
        <div class="svc-response-header">
          <span class="svc-status-badge svc-badge-ok">&#10003; ${callState.latencyMs}ms</span>
          <button type="button" class="svc-btn svc-btn-small svc-copy-btn" aria-label="Copy response">Copy</button>
          <button type="button" class="svc-btn svc-btn-small svc-repeat-btn" aria-label="Repeat request">Repeat</button>
        </div>
        <pre class="svc-response-body">${escapeHtml(callState.response)}</pre>
      </div>`;
    } else if (callState.status === 'timeout') {
      responseHtml = `<div class="svc-response svc-response-timeout">
        <div class="svc-response-header">
          <span class="svc-status-badge svc-badge-timeout">&#10007; No response within ${callState.latencyMs}ms</span>
          <button type="button" class="svc-btn svc-btn-small svc-repeat-btn" aria-label="Retry request">Retry</button>
        </div>
        <p class="svc-error-text">The node did not respond in time. It may be busy or the service ID may be incorrect.</p>
      </div>`;
    } else if (callState.status === 'node-lost') {
      responseHtml = `<div class="svc-response svc-response-error">
        <div class="svc-response-header">
          <span class="svc-status-badge svc-badge-error">&#10007; Node offline</span>
        </div>
        <p class="svc-error-text">Node ${callState.nodeId} went offline during the request. The request may or may not have been received.</p>
      </div>`;
    } else if (callState.status === 'error') {
      responseHtml = `<div class="svc-response svc-response-error">
        <div class="svc-response-header">
          <span class="svc-status-badge svc-badge-error">&#10007; Request failed</span>
          <button type="button" class="svc-btn svc-btn-small svc-repeat-btn" aria-label="Retry request">Retry</button>
        </div>
        <p class="svc-error-text">${escapeHtml(callState.error)}</p>
      </div>`;
    }

    const historyHtml = renderServiceHistory(activeNodeId, svc.service_id);
    formHtml = `<div class="svc-form">
      ${fieldsHtml}
      <div class="svc-actions">
        <button type="button" class="svc-btn svc-send-btn" data-service-id="${svc.service_id}" ${isSending ? 'disabled' : ''}>
          ${isSending ? 'Sending…' : 'Send Request'}
        </button>
      </div>
      ${responseHtml}
      ${historyHtml}
    </div>`;
  }

  const badgeHtml = !isCallable
    ? '<span class="svc-incomplete-badge svc-badge-unavailable">DSDL missing</span>'
    : schemaIncomplete ? '<span class="svc-incomplete-badge">no schema</span>' : '';

  const cardCls = [
    'svc-card',
    isExpanded ? 'svc-card-expanded' : '',
    !isCallable ? 'svc-card-unavailable' : '',
  ].filter(Boolean).join(' ');

  return `<div class="${cardCls}" data-service-id="${svc.service_id}">
    <div class="svc-card-header${!isCallable ? ' svc-card-header-static' : ''}" data-service-id="${svc.service_id}">
      ${isCallable ? `<span class="svc-expand-icon">${isExpanded ? '▾' : '▸'}</span>` : ''}
      <span class="svc-service-id">${svc.service_id}</span>
      <span class="svc-service-type" title="${escapeHtml(svc.full_type || '')}">${typeName}</span>
      ${badgeHtml}
    </div>
    ${formHtml}
  </div>`;
};

const collectFormAttributes = (cardEl, schema) => {
  const attributes = {};
  const fields = schema.request_fields || [];
  for (const field of fields) {
    if (field.kind === 'composite') {
      const input = cardEl.querySelector(`[data-field="${field.name}"]`);
      if (input && input.value.trim()) {
        attributes[field.name] = { value: input.value.trim(), type: field.type };
      }
    } else {
      const input = cardEl.querySelector(`[data-field="${field.name}"]`);
      if (input && input.value.trim()) {
        let val = input.value.trim();
        const num = Number(val);
        if (!isNaN(num) && val !== '') val = num;
        attributes[field.name] = { value: val };
      }
    }
  }
  return attributes;
};

const _getServiceContainer = () => {
  if (state.activeView === 'subjects') {
    const inline = document.getElementById('subjectInlineDetail');
    return inline?.querySelector('.svc-inline-form') || inline || el('selectedNodeContent');
  }
  return el('selectedNodeContent');
};

const saveFormState = () => {
  const container = _getServiceContainer();
  const values = {};
  container.querySelectorAll('.svc-field-input').forEach((input) => {
    if (input.value) values[input.id] = input.value;
  });
  const openDetails = [];
  container.querySelectorAll('details.svc-fieldset[open]').forEach((d) => {
    const summary = d.querySelector('summary');
    if (summary) openDetails.push(summary.textContent.trim());
  });
  return { values, openDetails };
};

const restoreFormState = (saved) => {
  const container = _getServiceContainer();
  for (const [id, val] of Object.entries(saved.values)) {
    const input = container.querySelector(`#${CSS.escape(id)}`);
    if (input) input.value = val;
  }
  container.querySelectorAll('details.svc-fieldset').forEach((d) => {
    const summary = d.querySelector('summary');
    if (summary && saved.openDetails.includes(summary.textContent.trim())) {
      d.open = true;
    }
  });
};

const _setCallState = (val, forSubjects = false) => {
  if (forSubjects) state._subjectServiceCallState = val;
  else state.serviceCallState = val;
};

const _getCallState = (forSubjects = false) => {
  return forSubjects ? state._subjectServiceCallState : state.serviceCallState;
};

const sendServiceRequest = async (nodeId, serviceId) => {
  const inSubjectsView = state.activeView === 'subjects';
  const container = _getServiceContainer();
  const card = container.querySelector(`.svc-card[data-service-id="${serviceId}"]`);
  if (!card) return;

  const services = state.serviceSchemas.get(nodeId) || [];
  if (services === SVC_SCHEMA_ERROR) return;
  const schema = services.find((s) => s.service_id === serviceId);
  if (!schema) return;

  const attributes = collectFormAttributes(card, schema);
  const saved = saveFormState();

  _setCallState({ nodeId, serviceId, status: 'sending', response: null, error: null, latencyMs: null }, inSubjectsView);
  _rerenderServiceUI(inSubjectsView, schema, nodeId);
  restoreFormState(saved);

  try {
    const data = await requestJson(`/api/services/${nodeId}/${serviceId}/call`, {
      method: 'POST',
      body: JSON.stringify({ attributes }),
    });

    const nodes = state.latestNodesPayload?.nodes;
    const nodeInfo = nodes ? nodes[String(nodeId)] : null;
    if ((!nodeInfo || nodeInfo.has_disappeared) && _getCallState(inSubjectsView)?.status === 'sending') {
      _setCallState({ nodeId, serviceId, status: 'node-lost', response: null, error: 'Node went offline', latencyMs: null }, inSubjectsView);
      _rerenderServiceUI(inSubjectsView, schema, nodeId);
      restoreFormState(saved);
      return;
    }

    if (data.status === 'ok') {
      _setCallState({ nodeId, serviceId, status: 'done', response: data.response, error: null, latencyMs: data.latency_ms }, inSubjectsView);
    } else if (data.status === 'timeout') {
      _setCallState({ nodeId, serviceId, status: 'timeout', response: null, error: data.error, latencyMs: data.latency_ms }, inSubjectsView);
    } else {
      _setCallState({ nodeId, serviceId, status: 'error', response: null, error: data.error || 'Unknown error', latencyMs: data.latency_ms }, inSubjectsView);
    }
  } catch (e) {
    _setCallState({ nodeId, serviceId, status: 'error', response: null, error: e.message || 'Request failed', latencyMs: null }, inSubjectsView);
  }

  _rerenderServiceUI(inSubjectsView, schema, nodeId);
  restoreFormState(saved);
};

const _rerenderServiceUI = (inSubjectsView, schema, nodeId) => {
  if (inSubjectsView && typeof renderSubjectServiceCard === 'function' && schema) {
    renderSubjectServiceCard(schema, nodeId);
  } else if (!inSubjectsView) {
    renderServicesTab();
  }
};

const bindServiceCardEvents = (content, nodeId, forSubjects = false) => {
  content.querySelectorAll('.svc-card-header:not(.svc-card-header-static)').forEach((header) => {
    header.addEventListener('click', () => {
      const sid = Number(header.dataset.serviceId);
      if (forSubjects) {
        state._subjectExpandedServiceId = state._subjectExpandedServiceId === sid ? null : sid;
        state._subjectServiceCallState = null;
        const schemas = state.serviceSchemas.get(nodeId) || [];
        const svc = Array.isArray(schemas) && schemas.find((s) => s.service_id === sid);
        if (svc) renderSubjectServiceCard(svc, nodeId);
      } else {
        state.expandedServiceId = state.expandedServiceId === sid ? null : sid;
        state.serviceCallState = null;
        renderServicesTab();
      }
    });
  });

  content.querySelectorAll('.svc-send-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const sid = Number(btn.dataset.serviceId);
      sendServiceRequest(nodeId, sid);
    });
  });

  content.querySelectorAll('.svc-copy-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cs = _getCallState(forSubjects);
      if (cs?.response) {
        navigator.clipboard.writeText(cs.response);
        showToast('Copied to clipboard', 'info', 2000);
      }
    });
  });

  content.querySelectorAll('.svc-repeat-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cs = _getCallState(forSubjects);
      if (cs) {
        sendServiceRequest(cs.nodeId, cs.serviceId);
      }
    });
  });
};

const renderServicesTab = async () => {
  const content = el('selectedNodeContent');
  const nodeId = state.selectedNodeId;

  // State 1: backend not connected
  if (!state.dashboardConnected) {
    if (state.pendingReconnect) {
      content.innerHTML = svcStateMsg(
        '<span class="svc-spinner"></span>', 'Reconnecting to backend…',
        'Restoring previous session.'
      );
    } else {
      content.innerHTML = svcStateMsg(
        '⏻', 'Not connected to backend',
        'Connect to the backend server to discover node services.',
        '<button class="svc-btn svc-state-action" data-action="connect-backend">Connect</button>'
      );
      content.querySelector('[data-action="connect-backend"]')?.addEventListener('click', () => {
        el('connectDashboardBtn')?.click();
      });
    }
    return;
  }

  // State 2/3: CAN not connected or connecting
  if (state.canState !== CONN.CONNECTED) {
    if (state.canState === CONN.CONNECTING) {
      content.innerHTML = svcStateMsg(
        '<span class="svc-spinner"></span>', 'Connecting to CAN interface…',
        'Establishing CAN bus connection. This usually takes a few seconds.'
      );
    } else {
      content.innerHTML = svcStateMsg(
        '⛓', 'CAN bus not connected',
        'Connect a CAN interface to discover online nodes and their services.',
        '<button class="svc-btn svc-state-action" data-action="connect-can">Connect CAN</button>'
      );
      content.querySelector('[data-action="connect-can"]')?.addEventListener('click', () => {
        el('connectCanBtn')?.click();
      });
    }
    return;
  }

  // State 4: no node selected
  if (nodeId == null) {
    content.innerHTML = svcStateMsg(
      '◎', 'Select a node to inspect its services',
      'Choose a node from the table above to view and call its advertised services.'
    );
    return;
  }

  // State 5: node disappeared
  const node = getSelectedNode();
  const offlineLabel = getNodeAlias(node?.unique_id) || node?.name || `Node ${nodeId}`;
  const hasStaleSchema = state.serviceSchemas.has(nodeId);
  if (!node || node.has_disappeared) {
    if (hasStaleSchema && state.serviceSchemas.get(nodeId) !== SVC_SCHEMA_ERROR) {
      const services = state.serviceSchemas.get(nodeId) || [];
      if (services.length) {
        state.expandedServiceId = null;
        const cards = services.map(svc => renderServiceCard(svc)).join('');
        content.innerHTML = `
          <div class="svc-stale-banner" role="alert">
            <span class="svc-stale-icon">⚠</span>
            ${escapeHtml(offlineLabel)} is offline — services are unavailable.
          </div>
          <section class="svc-panel svc-panel-stale">${cards}</section>`;
        return;
      }
    }
    const serverIds = node?.servers || [];
    if (serverIds.length) {
      const cards = serverIds.map((sid) => `<div class="svc-card svc-card-unavailable">
        <div class="svc-card-header svc-card-header-static">
          <span class="svc-service-id">${sid}</span>
          <span class="svc-service-type">Service ${sid}</span>
        </div>
      </div>`).join('');
      content.innerHTML = `
        <div class="svc-stale-banner" role="alert">
          <span class="svc-stale-icon">⚠</span>
          ${escapeHtml(offlineLabel)} is offline — services are unavailable.
        </div>
        <section class="svc-panel svc-panel-stale">${cards}</section>`;
      return;
    }
    content.innerHTML = svcStateMsg(
      '⚠', `${escapeHtml(offlineLabel)} is offline`,
      'This node disappeared from the CAN bus. Its services are unavailable until it returns.'
    );
    return;
  }

  // State 6: loading schema
  if (!state.serviceSchemas.has(nodeId)) {
    content.innerHTML = svcStateMsg(
      '<span class="svc-spinner"></span>',
      `Fetching services from node ${nodeId}…`,
      'Requesting service metadata. This may take a moment if the node has many registers.'
    ) + svcSkeleton();
    await fetchServiceSchema(nodeId);
    // Re-check that user hasn't navigated away during fetch
    if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'servers') return;
  }

  // State 7: schema fetch failed
  const schema = state.serviceSchemas.get(nodeId);
  if (schema === SVC_SCHEMA_ERROR) {
    content.innerHTML = svcStateMsg(
      '✕', `Failed to load services for node ${nodeId}`,
      'Could not reach the backend. Check your connection and try again.',
      '<button class="svc-btn svc-state-action" data-action="retry-schema">Retry</button>'
    );
    content.querySelector('[data-action="retry-schema"]')?.addEventListener('click', () => {
      state.serviceSchemas.delete(nodeId);
      renderServicesTab();
    });
    return;
  }

  // State 8: no services
  const services = schema || [];
  if (!services.length) {
    content.innerHTML = svcStateMsg(
      '○', `Node ${nodeId} has no services`,
      'This node does not advertise any service registers. It may only support pub/sub.'
    );
    return;
  }

  // State 9+: render service cards
  content.innerHTML = `<section class="svc-panel">${services.map(svc => renderServiceCard(svc)).join('')}</section>`;
  bindServiceCardEvents(content, nodeId);
  _loadPersistentHistory(content, nodeId);
};
