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
    state.serviceSchemas.set(nodeId, data.services || []);
  } catch {
    state.serviceSchemas.set(nodeId, SVC_SCHEMA_ERROR);
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

const renderServiceCard = (svc) => {
  const isCallable = svc.callable !== false;
  const isExpanded = isCallable && state.expandedServiceId === svc.service_id;
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

    const callState = state.serviceCallState;
    const isThisCall = callState && callState.serviceId === svc.service_id && callState.nodeId === state.selectedNodeId;
    const isSending = isThisCall && callState.status === 'sending';

    let responseHtml = '';
    if (!isThisCall || callState.status === 'idle') {
      responseHtml = `<div class="svc-response svc-response-idle">
        <span class="svc-idle-text">Response will appear here after sending a request.</span>
      </div>`;
    } else if (isSending) {
      responseHtml = `<div class="svc-response svc-response-sending">
        <span class="svc-spinner"></span>
        <span class="svc-sending-text">Waiting for response from node ${state.selectedNodeId}…</span>
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

    formHtml = `<div class="svc-form">
      ${fieldsHtml}
      <div class="svc-actions">
        <button type="button" class="svc-btn svc-send-btn" data-service-id="${svc.service_id}" ${isSending ? 'disabled' : ''}>
          ${isSending ? 'Sending…' : 'Send Request'}
        </button>
      </div>
      ${responseHtml}
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

const saveFormState = () => {
  const container = el('selectedNodeContent');
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
  const container = el('selectedNodeContent');
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

const sendServiceRequest = async (nodeId, serviceId) => {
  const container = el('selectedNodeContent');
  const card = container.querySelector(`.svc-card[data-service-id="${serviceId}"]`);
  if (!card) return;

  const services = state.serviceSchemas.get(nodeId) || [];
  if (services === SVC_SCHEMA_ERROR) return;
  const schema = services.find((s) => s.service_id === serviceId);
  if (!schema) return;

  const attributes = collectFormAttributes(card, schema);
  const saved = saveFormState();

  state.serviceCallState = { nodeId, serviceId, status: 'sending', response: null, error: null, latencyMs: null };
  renderServicesTab();
  restoreFormState(saved);

  try {
    const data = await requestJson(`/api/services/${nodeId}/${serviceId}/call`, {
      method: 'POST',
      body: JSON.stringify({ attributes }),
    });

    // Check if node disappeared while we were waiting
    const nodeStillOnline = getSelectedNode();
    if (!nodeStillOnline && state.serviceCallState?.status === 'sending') {
      state.serviceCallState = { nodeId, serviceId, status: 'node-lost', response: null, error: 'Node went offline', latencyMs: null };
      renderServicesTab();
      restoreFormState(saved);
      return;
    }

    if (data.status === 'ok') {
      state.serviceCallState = { nodeId, serviceId, status: 'done', response: data.response, error: null, latencyMs: data.latency_ms };
    } else if (data.status === 'timeout') {
      state.serviceCallState = { nodeId, serviceId, status: 'timeout', response: null, error: data.error, latencyMs: data.latency_ms };
    } else {
      state.serviceCallState = { nodeId, serviceId, status: 'error', response: null, error: data.error || 'Unknown error', latencyMs: data.latency_ms };
    }
  } catch (e) {
    state.serviceCallState = { nodeId, serviceId, status: 'error', response: null, error: e.message || 'Request failed', latencyMs: null };
  }

  renderServicesTab();
  restoreFormState(saved);
};

const bindServiceCardEvents = (content, nodeId) => {
  content.querySelectorAll('.svc-card-header:not(.svc-card-header-static)').forEach((header) => {
    header.addEventListener('click', () => {
      const sid = Number(header.dataset.serviceId);
      state.expandedServiceId = state.expandedServiceId === sid ? null : sid;
      state.serviceCallState = null;
      renderServicesTab();
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
      if (state.serviceCallState?.response) {
        navigator.clipboard.writeText(state.serviceCallState.response);
        showToast('Copied to clipboard', 'info', 2000);
      }
    });
  });

  content.querySelectorAll('.svc-repeat-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (state.serviceCallState) {
        sendServiceRequest(state.serviceCallState.nodeId, state.serviceCallState.serviceId);
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
  const hasStaleSchema = state.serviceSchemas.has(nodeId);
  if (!node || node.has_disappeared) {
    if (hasStaleSchema && state.serviceSchemas.get(nodeId) !== SVC_SCHEMA_ERROR) {
      const services = state.serviceSchemas.get(nodeId) || [];
      if (services.length) {
        content.innerHTML = `
          <div class="svc-stale-banner">
            <span class="svc-stale-icon">⚠</span>
            Node ${nodeId} is offline — service data may be stale.
          </div>
          <section class="svc-panel svc-panel-stale">${services.map(renderServiceCard).join('')}</section>`;
        bindServiceCardEvents(content, nodeId);
        return;
      }
    }
    content.innerHTML = svcStateMsg(
      '⚠', `Node ${nodeId} is offline`,
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
  content.innerHTML = `<section class="svc-panel">${services.map(renderServiceCard).join('')}</section>`;
  bindServiceCardEvents(content, nodeId);
};
