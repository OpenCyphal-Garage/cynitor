// Register browser — fetch, display, and edit Cyphal node registers.

const REG_CACHE = new Map();

const regAccessBadge = (access) =>
  access === 'read-write'
    ? '<span class="reg-badge reg-badge-rw">RW</span>'
    : '<span class="reg-badge reg-badge-ro">RO</span>';

const regTypeBadge = (type) =>
  `<span class="reg-badge reg-badge-type">${escapeHtml(type)}</span>`;

const renderRegisterRow = (reg, index) => {
  const isEditable = reg.access === 'read-write';
  const valueId = `reg-val-${CSS.escape(reg.register_name)}`;
  return `<div class="reg-row" data-reg-name="${escapeHtml(reg.register_name)}">
    <div class="reg-index">${index}</div>
    <div class="reg-name" title="${escapeHtml(reg.register_name)}">${escapeHtml(reg.register_name)}</div>
    <div class="reg-cell-type">${regTypeBadge(reg.type)}</div>
    <div class="reg-cell-access">${regAccessBadge(reg.access)}</div>
    <div class="reg-value-cell">
      ${isEditable
        ? `<input class="reg-value-input" id="${valueId}" value="${escapeHtml(reg.value)}"
             data-reg-name="${escapeHtml(reg.register_name)}" data-reg-type="${escapeHtml(reg.type)}"
             data-original="${escapeHtml(reg.value)}" aria-describedby="${valueId}-err" />
           <span class="reg-error-hint" id="${valueId}-err" role="alert"></span>`
        : `<span class="reg-value-ro">${escapeHtml(reg.value)}</span>`}
    </div>
    ${isEditable
      ? `<button class="svc-btn svc-btn-small reg-save-btn" data-reg-name="${escapeHtml(reg.register_name)}"
           disabled aria-label="Write register">Write</button>`
      : '<span class="reg-save-placeholder"></span>'}
  </div>`;
};

const showRegStatus = (panel, message, type = 'info') => {
  const toolbar = panel.querySelector('.reg-toolbar');
  if (!toolbar) return;
  toolbar.querySelector('.reg-status')?.remove();
  const status = document.createElement('span');
  status.className = `reg-status reg-status-${type}`;
  status.textContent = message;
  toolbar.insertBefore(status, toolbar.querySelector('.reg-refresh-btn'));
  setTimeout(() => status.classList.add('reg-status-visible'), 10);
  setTimeout(() => {
    status.classList.remove('reg-status-visible');
    status.addEventListener('transitionend', () => status.remove());
  }, 4000);
};

const VALID_BOOL = new Set(['true', 'false', '1', '0']);

const REG_TYPE_RANGES = {
  natural8:  [0, 255],
  natural16: [0, 65535],
  natural32: [0, 4294967295],
  natural64: [0, Number.MAX_SAFE_INTEGER],
  integer8:  [-128, 127],
  integer16: [-32768, 32767],
  integer32: [-2147483648, 2147483647],
  integer64: [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER],
  real16:    [-65504, 65504],
  real32:    [-3.4028235e+38, 3.4028235e+38],
  real64:    [-Number.MAX_VALUE, Number.MAX_VALUE],
};

const validateRegValue = (value, type) => {
  if (type === 'string') return null;
  const raw = value.trim();
  if (!raw) return 'Value cannot be empty';

  const inner = raw.startsWith('[') && raw.endsWith(']') ? raw.slice(1, -1).trim() : raw;
  const parts = inner.split(/[,\s]+/).filter(Boolean);
  if (!parts.length) return 'Value cannot be empty';

  if (type === 'bit') {
    for (const p of parts) {
      if (!VALID_BOOL.has(p.toLowerCase())) return `Invalid boolean: "${p}" — use True/False or 1/0`;
    }
    return null;
  }
  if (type === 'unstructured') {
    if (!/^[0-9a-fA-F]*$/.test(inner.replace(/[\s,]/g, ''))) return 'Hex characters only (0-9, a-f)';
    return null;
  }
  const isNatural = type.startsWith('natural');
  const isInteger = type.startsWith('integer');
  const isReal = type.startsWith('real');
  const range = REG_TYPE_RANGES[type];
  if (isNatural) {
    for (const p of parts) {
      if (!/^\d+$/.test(p)) return `"${p}" — natural must be a non-negative integer`;
      if (range) {
        const n = Number(p);
        if (n < range[0] || n > range[1]) return `"${p}" out of range for ${type} (${range[0]}–${range[1]})`;
      }
    }
    return null;
  }
  if (isInteger) {
    for (const p of parts) {
      if (!/^-?\d+$/.test(p)) return `Invalid integer: "${p}"`;
      if (range) {
        const n = Number(p);
        if (n < range[0] || n > range[1]) return `"${p}" out of range for ${type} (${range[0]}–${range[1]})`;
      }
    }
    return null;
  }
  if (isReal) {
    for (const p of parts) {
      if (isNaN(Number(p))) return `Invalid number: "${p}"`;
      if (range) {
        const n = Number(p);
        if (n < range[0] || n > range[1]) return `"${p}" out of range for ${type}`;
      }
    }
    return null;
  }
  return null;
};

const bindRegisterEvents = (container, nodeId) => {
  const panel = container.querySelector('.reg-panel');

  container.querySelectorAll('.reg-value-input').forEach((input) => {
    const saveBtn = container.querySelector(`.reg-save-btn[data-reg-name="${CSS.escape(input.dataset.regName)}"]`);
    input.addEventListener('input', () => {
      const changed = input.value !== input.dataset.original;
      const error = changed ? validateRegValue(input.value, input.dataset.regType) : null;
      input.classList.toggle('reg-input-invalid', !!error);
      input.setAttribute('aria-invalid', String(!!error));
      input.title = error || '';
      const hint = container.querySelector(`#${CSS.escape(input.id)}-err`);
      if (hint) hint.textContent = error || '';
      if (saveBtn) saveBtn.disabled = !changed || !!error;
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && saveBtn && !saveBtn.disabled) saveBtn.click();
    });
  });

  container.querySelectorAll('.reg-save-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.regName;
      const input = container.querySelector(`.reg-value-input[data-reg-name="${CSS.escape(name)}"]`);
      if (!input) return;

      const sentValue = input.value;
      if (!confirm(`Write "${name}" = ${sentValue}?`)) return;

      btn.disabled = true;
      btn.textContent = '…';
      try {
        const data = await requestJson(`/api/registers/${nodeId}/set`, {
          method: 'POST',
          body: JSON.stringify({ name, value: sentValue, type: input.dataset.regType }),
        });
        if (data.status === 'ok') {
          const readBack = data.value != null ? String(data.value) : sentValue;
          input.value = readBack;
          input.dataset.original = readBack;
          const cached = REG_CACHE.get(nodeId);
          if (Array.isArray(cached)) {
            const entry = cached.find((r) => r.register_name === name);
            if (entry) entry.value = readBack;
          }
          if (readBack !== sentValue) {
            showRegStatus(panel, `"${name}" — node accepted: ${readBack}`, 'ok');
          } else {
            showRegStatus(panel, `"${name}" written and verified`, 'ok');
          }
        } else {
          showRegStatus(panel, data.error || 'Write failed', 'error');
        }
      } catch (e) {
        showRegStatus(panel, e.message || 'Request failed', 'error');
      }
      btn.textContent = 'Write';
    });
  });

  container.querySelector('.reg-refresh-btn')?.addEventListener('click', () => {
    REG_CACHE.delete(nodeId);
    renderRegistersTab(true);
  });
};

const renderRegistersTab = async (force) => {
  const content = el('selectedNodeContent');
  const nodeId = state.selectedNodeId;

  if (!state.dashboardConnected || state.canState !== CONN.CONNECTED) {
    content.innerHTML = svcStateMsg('○', 'Connect to view registers', 'Connect to the CAN bus first.');
    return;
  }
  if (nodeId == null) {
    content.innerHTML = svcStateMsg('○', 'Select a node', 'Choose a node to view its registers.');
    return;
  }

  const node = getSelectedNode();
  if (!node || node.has_disappeared) {
    const regLabel = getNodeAlias(node?.unique_id) || node?.name || `Node ${nodeId}`;
    const cached = REG_CACHE.get(nodeId);
    if (cached && cached !== 'error' && cached.length) {
      renderRegisterList(content, cached, nodeId);
      const panel = content.querySelector('.reg-panel');
      if (panel) {
        panel.classList.add('svc-panel-stale');
        panel.insertAdjacentHTML('afterbegin',
          `<div class="svc-stale-banner" role="alert"><span class="svc-stale-icon">⚠</span>${escapeHtml(regLabel)} is offline — register data may be stale.</div>`);
      }
      return;
    }
    content.innerHTML = svcStateMsg('⚠', `${escapeHtml(regLabel)} is offline`, 'Cannot fetch registers from an offline node.');
    return;
  }

  if (!force && REG_CACHE.has(nodeId)) {
    const cached = REG_CACHE.get(nodeId);
    if (cached === 'error') {
      content.innerHTML = svcStateMsg(
        '✕', `Failed to load registers for node ${nodeId}`,
        'Could not reach the backend. Check your connection and try again.',
        '<button class="svc-btn svc-state-action reg-refresh-btn">Retry</button>'
      );
      content.querySelector('.reg-refresh-btn')?.addEventListener('click', () => {
        REG_CACHE.delete(nodeId);
        renderRegistersTab(true);
      });
      return;
    }
    renderRegisterList(content, cached, nodeId);
    return;
  }

  content.innerHTML = svcStateMsg(
    '<span class="svc-spinner"></span>',
    `Fetching registers from node ${nodeId}…`,
    '<span class="reg-progress-text">Reading all registers. This may take a moment.</span>'
  ) + svcSkeleton();

  const progressEl = content.querySelector('.reg-progress-text');
  const t0 = Date.now();
  const progressTimer = setInterval(() => {
    if (!progressEl || !progressEl.isConnected) { clearInterval(progressTimer); return; }
    const elapsed = ((Date.now() - t0) / 1000).toFixed(0);
    progressEl.textContent = `Reading registers… ${elapsed}s elapsed`;
  }, 1000);

  try {
    const data = await requestJson(`/api/registers/${nodeId}`);
    clearInterval(progressTimer);
    if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'registers') return;
    REG_CACHE.set(nodeId, data.registers || []);
  } catch {
    clearInterval(progressTimer);
    if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'registers') return;
    REG_CACHE.set(nodeId, 'error');
    renderRegistersTab();
    return;
  }

  renderRegisterList(content, REG_CACHE.get(nodeId), nodeId);
};

const renderRegisterList = (content, registers, nodeId) => {
  if (!registers.length) {
    content.innerHTML = svcStateMsg('○', `Node ${nodeId} has no registers`, 'This node does not expose any registers.');
    return;
  }

  const rows = registers.map((reg, i) => renderRegisterRow(reg, i)).join('');
  content.innerHTML = `<section class="svc-panel reg-panel">
    <div class="reg-sticky">
      <div class="reg-toolbar">
        <span class="reg-count">${registers.length} registers</span>
        <button class="svc-btn svc-btn-small reg-refresh-btn" aria-label="Refresh registers">Refresh</button>
      </div>
      <div class="reg-header">
        <div class="reg-index">#</div>
        <div>Name</div>
        <div>Type</div>
        <div>Access</div>
        <div>Value</div>
        <span class="reg-save-placeholder"></span>
      </div>
    </div>
    <div class="reg-body">${rows}</div>
  </section>`;
  bindRegisterEvents(content, nodeId);
};
