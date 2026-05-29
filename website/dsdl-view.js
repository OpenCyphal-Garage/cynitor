// DSDL Inspector view — top-level tab for browsing DSDL namespaces and types.
// Loaded before app.js; exposes DsdlView global used by switchView().

const DsdlView = (() => {
  let _initialized = false;
  let _namespacesData = null;
  let _statusData = null;
  let _selectedType = null;
  let _searchTerm = '';
  let _expandedNodes = new Set();
  let _treeWidth = null;
  let _busActivityMap = new Map();
  let _busRefreshTimer = null;
  let _lastDetailData = null;
  let _editorOpen = false;
  let _editorMode = 'new';
  let _editPrefill = null;
  let _editorSplitRatio = 0.5;
  let _previewRatio = 0.5;
  let _customNamespaces = [];
  let _hiddenNamespaces = new Set();
  let _showHidden = false;

  const init = () => {
    const container = el('dsdlContainer');
    if (!_initialized) {
      _restoreState();
      container.innerHTML = _buildLayout();
      _bindEvents();
      const searchInput = document.getElementById('dsdlSearch');
      if (searchInput && _searchTerm) searchInput.value = _searchTerm;
      _initialized = true;
    }
    _loadData();
  };

  const hide = () => { _stopBusRefresh(); };

  const _buildLayout = () => `
    <div class="dsdl-view">
      <div class="dsdl-split">
        <div class="dsdl-tree-panel" id="dsdlTreePanel">
          <div class="dsdl-tree-scroll">
            <div class="dsdl-tree-section">
              <div class="dsdl-section-header" id="dsdlPublicHeader"></div>
              <div class="dsdl-tree" id="dsdlTree"></div>
            </div>
            <div class="dsdl-tree-divider"></div>
            <div class="dsdl-tree-section">
              <div class="dsdl-section-header" id="dsdlCustomHeader"></div>
              <div class="dsdl-tree" id="dsdlCustomTree"></div>
            </div>
          </div>
          <div class="dsdl-search-wrap">
            <input type="text" class="dsdl-search" id="dsdlSearch"
                   placeholder="Search types or fields…" aria-label="Search DSDL types" />
          </div>
        </div>
        <div class="dsdl-split-handle" id="dsdlSplitHandle"></div>
        <div class="dsdl-detail-area" id="dsdlDetailArea">
          <div class="dsdl-detail-panel" id="dsdlDetail">
            <div class="dsdl-detail-placeholder">
              <div class="dsdl-detail-placeholder-icon">{&nbsp;}</div>
              <div class="dsdl-detail-placeholder-text">Select a type to inspect</div>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  // ------------------------------------------------------------------
  // Data loading
  // ------------------------------------------------------------------

  const _loadData = async () => {
    if (!state.dashboardConnected) {
      _renderDisconnected();
      return;
    }
    try {
      const [statusResp, nsResp] = await Promise.all([
        requestJson('/api/dsdl/status'),
        requestJson('/api/dsdl/namespaces'),
      ]);
      _statusData = statusResp;
      _namespacesData = nsResp.namespaces;
      _buildTelemetryIndex();
      _renderTreeHeaders();
      _renderTree();
      _renderCustomTree();
      _startBusRefresh();
      if (_selectedType) {
        _loadTypeDetail(_selectedType);
        _expandToType(_selectedType);
      }
    } catch (err) {
      _renderError(err.message);
    }
  };

  // ------------------------------------------------------------------
  // Section headers
  // ------------------------------------------------------------------

  const _renderTreeHeaders = () => {
    _renderPublicHeader();
    _renderCustomHeader();
  };

  const _renderPublicHeader = () => {
    const header = document.getElementById('dsdlPublicHeader');
    if (!header || !_statusData) return;

    const count = _statusData.source_types;
    const dot = _statusData.compiled ? 'dsdl-dot-ok' : 'dsdl-dot-warn';
    const label = _statusData.compiled ? 'Compiled' : 'Not compiled';
    const age = _statusData.last_public_compiled ? ` · ${_formatAge(_statusData.last_public_compiled)}` : '';

    header.innerHTML = `
      <span class="dsdl-section-title">Public regulated</span>
      <span class="dsdl-section-count">${count}</span>
      <span class="dsdl-section-right">
        <span class="dsdl-tree-status"><span class="dsdl-dot ${dot}"></span>${escapeHtml(label)}${escapeHtml(age)}</span>
        <button class="dsdl-hdr-btn dsdl-hdr-compile" id="dsdlRecompileBtn" aria-label="Recompile public types" title="Recompile public types">
          <svg width="10" height="10" viewBox="0 0 12 12"><path d="M1 6a5 5 0 019-2M11 6a5 5 0 01-9 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none"/></svg>
        </button>
      </span>`;

    document.getElementById('dsdlRecompileBtn')?.addEventListener('click', _recompilePublic);
  };

  const _renderCustomHeader = () => {
    const header = document.getElementById('dsdlCustomHeader');
    if (!header) return;

    const count = _statusData?.custom_types || 0;
    const countHtml = count > 0 ? `<span class="dsdl-section-count">${count}</span>` : '';

    const hasCustom = count > 0;
    const compiled = hasCustom && _isCustomFullyCompiled();
    const dotCls = !hasCustom ? '' : compiled ? 'dsdl-dot-ok' : 'dsdl-dot-warn';
    const statusLabel = !hasCustom ? '' : compiled ? 'Compiled' : 'Not compiled';
    const age = hasCustom && _statusData?.last_custom_compiled
      ? ` · ${_formatAge(_statusData.last_custom_compiled)}` : '';
    const statusHtml = hasCustom ? `
      <span class="dsdl-tree-status"><span class="dsdl-dot ${dotCls}"></span>${escapeHtml(statusLabel)}${escapeHtml(age)}</span>
      <button class="dsdl-hdr-btn dsdl-hdr-compile" id="dsdlCustomCompileBtn" aria-label="Compile custom types" title="Compile custom types">
        <svg width="10" height="10" viewBox="0 0 12 12"><path d="M1 6a5 5 0 019-2M11 6a5 5 0 01-9 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none"/></svg>
      </button>` : '';

    header.innerHTML = `
      <span class="dsdl-section-title">Custom</span>
      ${countHtml}
      <span class="dsdl-section-right">
        ${statusHtml}
        <button class="dsdl-hdr-btn" id="dsdlCustomAddNs" aria-label="Add namespace" title="Add namespace">
          <svg width="10" height="10" viewBox="0 0 10 10"><path d="M5 1v8M1 5h8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
          <svg width="10" height="10" viewBox="0 0 16 16"><path d="M2 3h5l2 2h5v8H2z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>
        </button>
      </span>`;

    document.getElementById('dsdlCustomAddNs')?.addEventListener('click', () => _showNewNamespaceDialog());
    document.getElementById('dsdlCustomCompileBtn')?.addEventListener('click', _compileCustom);
  };

  const _isCustomFullyCompiled = () => {
    if (!_namespacesData) return false;
    const index = _getTypeIndex();
    let any = false;
    for (const [, t] of Object.entries(index)) {
      if (t.source !== 'custom') continue;
      any = true;
      if (!t.compiled) return false;
    }
    return any;
  };

  const _compileCustom = async () => {
    const btn = document.getElementById('dsdlCustomCompileBtn');
    if (btn) btn.classList.add('dsdl-spin');
    _clearCustomCompileError();
    try {
      const result = await requestJson('/api/dsdl/compile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'custom' }),
      });
      if (!result.ok) {
        _showCustomCompileError((result.errors || []).join('\n'));
      }
      await _reloadTree();
    } catch (err) {
      _showCustomCompileError(err.message);
    } finally {
      if (btn) btn.classList.remove('dsdl-spin');
    }
  };

  const _showCustomCompileError = (msg) => {
    const header = document.getElementById('dsdlCustomHeader');
    if (!header) return;
    let errEl = header.parentElement.querySelector('.dsdl-compile-error');
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'dsdl-compile-error';
      header.after(errEl);
    }
    errEl.textContent = msg;
  };

  const _clearCustomCompileError = () => {
    const section = document.getElementById('dsdlCustomHeader')?.parentElement;
    section?.querySelector('.dsdl-compile-error')?.remove();
  };

  const _formatAge = (timestamp) => {
    const seconds = Math.floor(Date.now() / 1000 - timestamp);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  };

  // ------------------------------------------------------------------
  // Namespace tree
  // ------------------------------------------------------------------

  const _publicNamespaces = () => {
    if (!_namespacesData) return {};
    const pub = {};
    for (const [name, node] of Object.entries(_namespacesData)) {
      if (node._source === 'custom') continue;
      pub[name] = node;
    }
    return pub;
  };

  const _customNamespacesFromTree = () => {
    if (!_namespacesData) return {};
    const custom = {};
    for (const [name, node] of Object.entries(_namespacesData)) {
      if (node._source === 'custom') custom[name] = node;
    }
    return custom;
  };

  const _renderTree = () => {
    const container = document.getElementById('dsdlTree');
    if (!container || !_namespacesData) return;

    const term = _searchTerm.toLowerCase().trim();
    const publicNs = _publicNamespaces();
    let html = '';
    for (const [name, node] of Object.entries(publicNs)) {
      html += _renderNamespaceNode(name, name, node, 0, term);
    }

    if (!html && term) {
      container.innerHTML = '<div class="dsdl-tree-empty">No matching types.</div>';
      return;
    }
    container.innerHTML = html;
    _updateBusDots();
  };

  const _renderCustomTree = () => {
    const container = document.getElementById('dsdlCustomTree');
    if (!container) return;

    const term = _searchTerm.toLowerCase().trim();
    const customNs = _customNamespacesFromTree();
    const allKeys = Object.keys(customNs);
    const hasCustom = allKeys.length > 0;

    if (!hasCustom) {
      container.innerHTML = '<div class="dsdl-custom-empty">No custom types yet</div>';
      return;
    }

    const visibleKeys = allKeys.filter(k => !_hiddenNamespaces.has(k));
    const hiddenCount = allKeys.length - visibleKeys.length;

    let html = '';
    for (const name of visibleKeys) {
      html += _renderCustomNsNode(name, name, customNs[name], 0, term);
    }

    if (_showHidden && hiddenCount > 0) {
      for (const name of allKeys.filter(k => _hiddenNamespaces.has(k))) {
        html += _renderCustomNsNode(name, name, customNs[name], 0, term, true);
      }
    }

    if (hiddenCount > 0) {
      const label = _showHidden ? 'Hide hidden' : `Show ${hiddenCount} hidden`;
      html += `<button class="dsdl-hidden-toggle" id="dsdlToggleHidden">${escapeHtml(label)}</button>`;
    }

    if (!html && term) {
      container.innerHTML = '<div class="dsdl-tree-empty">No matching custom types.</div>';
      return;
    }
    container.innerHTML = html;

    document.getElementById('dsdlToggleHidden')?.addEventListener('click', () => {
      _showHidden = !_showHidden;
      _renderCustomTree();
    });

    container.querySelectorAll('[data-hide-ns]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const ns = btn.dataset.hideNs;
        _hiddenNamespaces.add(ns);
        _saveDsdlState();
        _renderCustomTree();
      });
    });

    container.querySelectorAll('[data-unhide-ns]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const ns = btn.dataset.unhideNs;
        _hiddenNamespaces.delete(ns);
        _saveDsdlState();
        _renderCustomTree();
      });
    });

    _updateBusDots();
  };

  const _renderCustomNsNode = (name, fullPath, node, depth, searchTerm, dimmed = false) => {
    const filteredTypes = _filterTypes(node.types || [], searchTerm);
    let childHtml = '';
    let childCount = filteredTypes.length;

    for (const [childName, childNode] of Object.entries(node.children || {})) {
      const childPath = `${fullPath}.${childName}`;
      const rendered = _renderCustomNsNode(childName, childPath, childNode, depth + 1, searchTerm);
      if (rendered) {
        childHtml += rendered;
        childCount++;
      }
    }

    const expanded = searchTerm || _expandedNodes.has(fullPath);
    const hasChildren = Object.keys(node.children || {}).length || filteredTypes.length;

    let typesHtml = '';
    for (const t of filteredTypes) {
      const kindCls = t.kind === 'service' ? 'dsdl-kind-service' : 'dsdl-kind-message';
      const kindLabel = t.kind === 'service' ? 'SRV' : 'MSG';
      const portHtml = t.fixed_port_id != null
        ? `<span class="dsdl-port-id">${t.fixed_port_id}</span>`
        : '';
      const selected = _selectedType === t.full_name ? ' dsdl-type-selected' : '';
      const lockedCls = t.compiled ? ' dsdl-type-compiled' : '';
      const lockTitle = t.compiled
        ? ' title="Compiled — locked. Clear python_compiled_messages/ and recompile to edit."'
        : '';
      typesHtml += `
        <div class="dsdl-type-row${selected}${lockedCls}" data-type="${escapeHtml(t.full_name)}"${lockTitle}
             style="padding-left: ${(depth + 1) * 1.125 + 1}rem">
          <span class="dsdl-kind ${kindCls}">${kindLabel}</span>
          <span class="dsdl-type-name">${escapeHtml(t.short_name)}</span>
          <span class="dsdl-type-ver">${escapeHtml(t.version)}</span>
          ${portHtml}
        </div>`;
    }

    const chevron = hasChildren
      ? `<svg class="dsdl-chev ${expanded ? 'open' : ''}" width="10" height="10" viewBox="0 0 10 10"><path d="M3 2l4 3-4 3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`
      : '<span class="dsdl-chev-spacer"></span>';

    const hideBtn = depth === 0
      ? (dimmed
        ? `<button class="dsdl-ns-add" data-unhide-ns="${escapeHtml(fullPath)}" title="Show namespace" aria-label="Show namespace">
            <svg width="9" height="9" viewBox="0 0 16 16"><path d="M1 8s3-5 7-5 7 5 7 5-3 5-7 5-7-5-7-5z" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>
          </button>`
        : `<button class="dsdl-ns-add" data-hide-ns="${escapeHtml(fullPath)}" title="Hide namespace" aria-label="Hide namespace">
            <svg width="9" height="9" viewBox="0 0 16 16"><path d="M1 8s3-5 7-5 7 5 7 5-3 5-7 5-7-5-7-5z" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3 13L13 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
          </button>`)
      : '';

    const addBtns = `<span class="dsdl-ns-actions">
      ${dimmed ? '' : `<button class="dsdl-ns-add" data-add-ns="${escapeHtml(fullPath)}" title="Add sub-namespace" aria-label="Add sub-namespace">
        <svg width="8" height="8" viewBox="0 0 8 8"><path d="M4 1v6M1 4h6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
        <svg width="9" height="9" viewBox="0 0 16 16"><path d="M2 3h5l2 2h5v8H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
      </button>
      <button class="dsdl-ns-add" data-add-type="${escapeHtml(fullPath)}" title="Add type" aria-label="Add type">
        <svg width="8" height="8" viewBox="0 0 8 8"><path d="M4 1v6M1 4h6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
        <svg width="9" height="9" viewBox="0 0 16 16"><path d="M4 2h8v12H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M7 6h2M7 8.5h2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>
      </button>`}
      ${hideBtn}
    </span>`;

    const dimCls = dimmed ? ' dsdl-ns-dimmed' : '';

    if (childCount === 0 && !searchTerm) {
      return `
        <div class="dsdl-ns${dimCls}">
          <div class="dsdl-ns-row dsdl-ns-custom" data-ns="${escapeHtml(fullPath)}"
               style="padding-left: ${depth * 1.125 + 0.5}rem">
            ${chevron}
            <span class="dsdl-ns-label">${escapeHtml(name)}</span>
            ${addBtns}
          </div>
          <div class="dsdl-ns-children ${expanded ? '' : 'hidden'}">
            <div class="dsdl-custom-empty" style="padding-left: ${(depth + 1) * 1.125 + 1}rem">Empty</div>
          </div>
        </div>`;
    }
    if (childCount === 0) return '';

    return `
      <div class="dsdl-ns${dimCls}">
        <div class="dsdl-ns-row dsdl-ns-custom" data-ns="${escapeHtml(fullPath)}"
             style="padding-left: ${depth * 1.125 + 0.5}rem">
          ${chevron}
          <span class="dsdl-ns-label">${escapeHtml(name)}</span>
          ${addBtns}
        </div>
        <div class="dsdl-ns-children ${expanded ? '' : 'hidden'}">
          ${typesHtml}
          ${childHtml}
        </div>
      </div>`;
  };

  const _renderNamespaceNode = (name, fullPath, node, depth, searchTerm) => {
    const filteredTypes = _filterTypes(node.types || [], searchTerm);
    let childHtml = '';
    let childCount = filteredTypes.length;

    for (const [childName, childNode] of Object.entries(node.children || {})) {
      const childPath = `${fullPath}.${childName}`;
      const rendered = _renderNamespaceNode(childName, childPath, childNode, depth + 1, searchTerm);
      if (rendered) {
        childHtml += rendered;
        childCount++;
      }
    }

    if (childCount === 0) return '';

    const expanded = searchTerm || _expandedNodes.has(fullPath);
    const hasChildren = Object.keys(node.children || {}).length || filteredTypes.length;

    let typesHtml = '';
    for (const t of filteredTypes) {
      const kindCls = t.kind === 'service' ? 'dsdl-kind-service' : 'dsdl-kind-message';
      const kindLabel = t.kind === 'service' ? 'SRV' : 'MSG';
      const portHtml = t.fixed_port_id != null
        ? `<span class="dsdl-port-id">${t.fixed_port_id}</span>`
        : '';
      const selected = _selectedType === t.full_name ? ' dsdl-type-selected' : '';
      typesHtml += `
        <div class="dsdl-type-row${selected}" data-type="${escapeHtml(t.full_name)}"
             style="padding-left: ${(depth + 1) * 1.125 + 1}rem">
          <span class="dsdl-kind ${kindCls}">${kindLabel}</span>
          <span class="dsdl-type-name">${escapeHtml(t.short_name)}</span>
          <span class="dsdl-type-ver">${escapeHtml(t.version)}</span>
          ${portHtml}
        </div>`;
    }

    const chevron = hasChildren
      ? `<svg class="dsdl-chev ${expanded ? 'open' : ''}" width="10" height="10" viewBox="0 0 10 10"><path d="M3 2l4 3-4 3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`
      : '<span class="dsdl-chev-spacer"></span>';

    return `
      <div class="dsdl-ns">
        <div class="dsdl-ns-row" data-ns="${escapeHtml(fullPath)}"
             style="padding-left: ${depth * 1.125 + 0.5}rem">
          ${chevron}
          <span class="dsdl-ns-label">${escapeHtml(name)}</span>
        </div>
        <div class="dsdl-ns-children ${expanded ? '' : 'hidden'}">
          ${typesHtml}
          ${childHtml}
        </div>
      </div>`;
  };

  const _filterTypes = (types, term) => {
    if (!term) return types;
    return types.filter(t => {
      if (t.short_name.toLowerCase().includes(term)) return true;
      if (t.full_name.toLowerCase().includes(term)) return true;
      if (t.fixed_port_id != null && String(t.fixed_port_id).includes(term)) return true;
      if (t.field_names?.some(f => f.toLowerCase().includes(term))) return true;
      return false;
    });
  };

  // ------------------------------------------------------------------
  // Bus activity cross-reference (refreshed every 10s)
  // ------------------------------------------------------------------

  const _dsdlToTelemetryName = (fullName) => {
    const parts = fullName.split('.');
    if (parts.length < 3) return fullName;
    return `${parts[parts.length - 3]}_${parts[parts.length - 2]}_${parts[parts.length - 1]}`;
  };

  const _rebuildBusActivity = () => {
    _busActivityMap = new Map();
    if (!_namespacesData) { _updateBusDots(); return; }

    const subjectTypeMap = new Map();
    for (const [subjectId, evt] of state.latestBySubject) {
      if (!evt.message_type) continue;
      const fullName = _telemetryIndex.get(evt.message_type);
      if (fullName) subjectTypeMap.set(subjectId, fullName);
    }

    const nodes = state.latestNodesPayload?.nodes;
    if (nodes) {
      for (const [, node] of Object.entries(nodes)) {
        if (!Number.isInteger(node.node_id) || node.has_disappeared) continue;
        const nodeId = node.node_id;
        for (const sid of (node.publishers || [])) {
          const fullName = subjectTypeMap.get(sid);
          if (!fullName) continue;
          if (!_busActivityMap.has(fullName)) _busActivityMap.set(fullName, []);
          const list = _busActivityMap.get(fullName);
          if (!list.some(e => e.subjectId === sid && e.nodeId === nodeId)) {
            list.push({ subjectId: sid, nodeId });
          }
        }
      }
    }

    _updateBusDots();
    _updateBusDetail();
  };

  const _buildTelemetryIndex = () => {
    _telemetryIndex = new Map();
    if (!_namespacesData) return;
    const walk = (node) => {
      for (const t of (node.types || [])) {
        _telemetryIndex.set(_dsdlToTelemetryName(t.full_name), t.full_name);
      }
      for (const child of Object.values(node.children || {})) walk(child);
    };
    for (const ns of Object.values(_namespacesData)) walk(ns);
  };

  let _telemetryIndex = new Map();

  const _updateBusDots = () => {
    document.querySelectorAll('.dsdl-type-row').forEach(row => {
      const fullName = row.dataset.type;
      const active = _busActivityMap.has(fullName);
      let dot = row.querySelector('.dsdl-bus-dot');
      if (active && !dot) {
        dot = document.createElement('span');
        dot.className = 'dsdl-bus-dot';
        dot.title = 'Active on bus';
        row.insertBefore(dot, row.firstChild);
      } else if (!active && dot) {
        dot.remove();
      }
    });
  };

  const _getNodeName = (nodeId) => {
    const nodes = state.latestNodesPayload?.nodes;
    if (!nodes) return null;
    const node = nodes[String(nodeId)];
    if (!node) return null;
    const alias = node.unique_id ? getNodeAlias(node.unique_id) : null;
    return alias || node.name || null;
  };

  const _formatNodeLabel = (nodeId) => {
    const name = _getNodeName(nodeId);
    if (typeof nodeId === 'number') {
      return name ? `${name} (${nodeId})` : `node ${nodeId}`;
    }
    return name || String(nodeId).slice(0, 8);
  };

  let _busExpanded = false;

  const _updateBusDetail = () => {
    const section = document.getElementById('dsdlBusActivity');
    if (!section || !_selectedType) return;
    const entries = _busActivityMap.get(_selectedType);
    if (!entries || !entries.length) {
      section.innerHTML = '';
      section.classList.add('hidden');
      return;
    }
    section.classList.remove('hidden');
    const chips = entries.map(s =>
      `<span class="dsdl-bus-chip">subject ${s.subjectId} · ${escapeHtml(_formatNodeLabel(s.nodeId))}</span>`
    ).join('');

    section.innerHTML = `
      <span class="dsdl-bus-dot"></span>
      <span class="dsdl-bus-label">Active on bus</span>
      ${chips}`;

    if (!_busExpanded) {
      section.classList.add('dsdl-bus-collapsed');
    } else {
      section.classList.remove('dsdl-bus-collapsed');
    }

    requestAnimationFrame(() => {
      const overflows = section.scrollHeight > section.clientHeight + 2;
      const existing = section.querySelector('.dsdl-bus-toggle');
      if (overflows && !_busExpanded) {
        if (!existing) {
          const btn = document.createElement('button');
          btn.className = 'dsdl-bus-toggle';
          btn.textContent = `+${entries.length} more`;
          btn.addEventListener('click', () => { _busExpanded = true; _updateBusDetail(); });
          section.appendChild(btn);
        }
      } else if (_busExpanded && entries.length > 4) {
        if (!existing) {
          const btn = document.createElement('button');
          btn.className = 'dsdl-bus-toggle';
          btn.textContent = 'show less';
          btn.addEventListener('click', () => { _busExpanded = false; _updateBusDetail(); });
          section.appendChild(btn);
        }
      }
    });
  };

  const _startBusRefresh = () => {
    _stopBusRefresh();
    _rebuildBusActivity();
    setTimeout(_rebuildBusActivity, 3000);
    setTimeout(_rebuildBusActivity, 7000);
    _busRefreshTimer = setInterval(_rebuildBusActivity, 10000);
  };

  const _stopBusRefresh = () => {
    if (_busRefreshTimer) {
      clearInterval(_busRefreshTimer);
      _busRefreshTimer = null;
    }
  };

  // ------------------------------------------------------------------
  // Type detail
  // ------------------------------------------------------------------

  const _loadTypeDetail = async (fullName) => {
    _selectedType = fullName;
    _busExpanded = false;
    _highlightSelected();
    _saveDsdlState();
    const panel = document.getElementById('dsdlDetail');
    if (!panel) return;

    panel.innerHTML = '<div class="dsdl-detail-placeholder"><div class="dsdl-detail-placeholder-text">Loading…</div></div>';

    try {
      const data = await requestJson(`/api/dsdl/type/${encodeURIComponent(fullName)}`);
      if (_selectedType !== fullName) return;
      _renderTypeDetail(data);
    } catch (err) {
      if (_selectedType !== fullName) return;
      panel.innerHTML = `<div class="dsdl-detail-placeholder"><div class="dsdl-detail-placeholder-text">Failed to load: ${escapeHtml(err.message)}</div></div>`;
    }
  };

  const _renderTypeDetail = (data) => {
    const panel = document.getElementById('dsdlDetail');
    if (!panel) return;

    const kindCls = data.kind === 'service' ? 'dsdl-badge-service' : 'dsdl-badge-message';
    const kindLabel = data.kind === 'service' ? 'Service' : 'Message';
    const sourceCls = data.source === 'custom' ? 'dsdl-badge-custom' : 'dsdl-badge-regulated';
    const sourceLabel = data.source === 'custom' ? 'Custom' : 'Regulated';
    const compiledCls = data.compiled ? 'dsdl-badge-ok' : 'dsdl-badge-warn';
    const compiledLabel = data.compiled ? 'Compiled' : 'Not compiled';
    const portBadge = data.fixed_port_id != null
      ? `<span class="dsdl-badge dsdl-badge-port">${data.fixed_port_id}</span>`
      : '';

    _lastDetailData = data;

    let fieldsHtml;
    if (data.kind === 'service') {
      fieldsHtml = `
        <div class="dsdl-card">
          <div class="dsdl-card-label">Request</div>
          ${_renderFieldTable(data.fields.request || [])}
        </div>
        <div class="dsdl-card">
          <div class="dsdl-card-label">Response</div>
          ${_renderFieldTable(data.fields.response || [])}
        </div>`;
    } else {
      fieldsHtml = `
        <div class="dsdl-card">
          <div class="dsdl-card-label">Fields</div>
          ${_renderFieldTable(data.fields || [])}
        </div>`;
    }

    const constantsHtml = data.constants.length ? `
      <div class="dsdl-card">
        <div class="dsdl-card-label">Constants</div>
        <table class="dsdl-ftable">
          <tbody>
            ${data.constants.map(c => `
              <tr class="dsdl-frow">
                <td class="dsdl-fcol-type">${escapeHtml(c.type)}</td>
                <td class="dsdl-fcol-name">${escapeHtml(c.name)}</td>
                <td class="dsdl-fcol-val"><span class="dsdl-const-eq">=</span> ${escapeHtml(c.value)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>` : '';

    const depsHtml = data.dependencies.length ? `
      <div class="dsdl-inline-section">
        <span class="dsdl-inline-label">Depends on</span>
        ${data.dependencies.map(d => `<a class="dsdl-dep-chip" data-dep="${escapeHtml(d)}">${escapeHtml(d)}</a>`).join('')}
      </div>` : '';

    const sourceLines = data.source_text.replace(/\n$/, '').split('\n');
    const numberedLines = sourceLines.map((line, i) =>
      `<span class="dsdl-src-num">${i + 1}</span>${escapeHtml(line)}`
    ).join('\n');

    const canEdit = data.source === 'custom' && !data.compiled;
    const actionsHtml = canEdit ? `
      <div class="dsdl-doc-actions">
        <button class="dsdl-editor-btn dsdl-editor-btn-save" id="dsdlEditBtn" aria-label="Edit type">Edit</button>
        <button class="dsdl-editor-btn dsdl-editor-btn-danger" id="dsdlDeleteBtn" aria-label="Delete type">Delete</button>
      </div>` : '';

    panel.innerHTML = `
      <div class="dsdl-doc">
        <div class="dsdl-doc-head">
          <div class="dsdl-doc-title-row">
            <h2 class="dsdl-doc-title"><span class="dsdl-doc-ns">${escapeHtml(data.namespace)}.</span>${escapeHtml(data.short_name)}</h2>
            <span class="dsdl-doc-ver">${escapeHtml(data.version)}</span>
          </div>
          <div class="dsdl-badge-row">
            <span class="dsdl-badge ${kindCls}">${kindLabel}</span>
            ${portBadge}
            <span class="dsdl-badge ${sourceCls}">${sourceLabel}</span>
            <span class="dsdl-badge ${compiledCls}">${compiledLabel}</span>
          </div>
          ${actionsHtml}
          <div class="dsdl-bus-section hidden" id="dsdlBusActivity"></div>
        </div>
        <div class="dsdl-doc-body">
          ${fieldsHtml}
          ${constantsHtml}
          ${depsHtml}
          <div class="dsdl-card dsdl-card-source">
            <div class="dsdl-card-label">Source</div>
            <pre class="dsdl-src">${numberedLines}</pre>
          </div>
        </div>
      </div>`;

    panel.querySelectorAll('.dsdl-dep-chip').forEach(link => {
      link.addEventListener('click', () => _navigateToDependency(link.dataset.dep));
    });

    document.getElementById('dsdlEditBtn')?.addEventListener('click', () => _openEditorEdit(data));
    document.getElementById('dsdlDeleteBtn')?.addEventListener('click', () => _confirmDeleteType(data.full_name));

    _updateBusDetail();

    if (_editorOpen) {
      const editorHandle = document.getElementById('dsdlEditorHandle');
      if (editorHandle) editorHandle.style.display = '';
      const editorPanel = document.getElementById('dsdlEditorPanel');
      const detail = document.getElementById('dsdlDetail');
      if (editorPanel) editorPanel.style.flex = `0 0 ${_editorSplitRatio * 100}%`;
      if (detail) detail.style.flex = '1';
    }
  };

  const _renderFieldTable = (fields) => {
    if (!fields.length) return '<div class="dsdl-field-empty">No fields</div>';
    return `<table class="dsdl-ftable"><tbody>
      ${fields.map(f => `
        <tr class="dsdl-frow">
          <td class="dsdl-fcol-type">${escapeHtml(f.type)}</td>
          <td class="dsdl-fcol-name">${escapeHtml(f.name)}</td>
        </tr>`).join('')}
    </tbody></table>`;
  };

  const _highlightSelected = () => {
    document.querySelectorAll('.dsdl-type-row').forEach(el => {
      el.classList.toggle('dsdl-type-selected', el.dataset.type === _selectedType);
    });
  };

  const _navigateToDependency = (depName) => {
    if (!_namespacesData) return;
    const matching = Object.keys(_getTypeIndex()).filter(k => {
      const parts = k.split('.');
      const nameWithoutVersion = parts.slice(0, -2).join('.');
      return nameWithoutVersion === depName || k === depName;
    });
    if (matching.length > 0) {
      const bestMatch = matching.sort().pop();
      _loadTypeDetail(bestMatch);
      _expandToType(bestMatch);
    }
  };

  const _getTypeIndex = () => {
    const index = {};
    const walk = (node) => {
      for (const t of (node.types || [])) index[t.full_name] = t;
      for (const child of Object.values(node.children || {})) walk(child);
    };
    for (const ns of Object.values(_namespacesData || {})) walk(ns);
    return index;
  };

  const _expandToType = (fullName) => {
    const parts = fullName.split('.');
    let path = '';
    for (let i = 0; i < parts.length - 3; i++) {
      path = path ? `${path}.${parts[i]}` : parts[i];
      _expandedNodes.add(path);
    }
    _renderTree();
    _renderCustomTree();
    requestAnimationFrame(() => {
      const entry = document.querySelector(`.dsdl-type-row[data-type="${CSS.escape(fullName)}"]`);
      if (entry) entry.scrollIntoView({ block: 'nearest' });
    });
  };

  // ------------------------------------------------------------------
  // Namespace dialog
  // ------------------------------------------------------------------

  const _showNewNamespaceDialog = (parentNs) => {
    const existing = document.getElementById('dsdlNsDialog');
    if (existing) { existing.remove(); return; }

    const prefix = parentNs ? `${parentNs}.` : '';
    const placeholder = parentNs ? `e.g. ${parentNs}.subsystem` : 'e.g. myapp.sensors';

    const dialog = document.createElement('div');
    dialog.id = 'dsdlNsDialog';
    dialog.className = 'dsdl-inline-dialog';
    dialog.innerHTML = `
      <input type="text" class="dsdl-dialog-input" id="dsdlNsInput"
             placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(prefix)}" aria-label="Namespace name" />
      <button class="dsdl-dialog-ok" id="dsdlNsOk">Create</button>
      <button class="dsdl-dialog-cancel" id="dsdlNsCancel">&times;</button>`;

    const anchor = document.getElementById('dsdlCustomHeader');
    anchor?.after(dialog);

    const input = document.getElementById('dsdlNsInput');
    input?.focus();
    if (prefix) input.setSelectionRange(prefix.length, prefix.length);

    document.getElementById('dsdlNsOk')?.addEventListener('click', async () => {
      const ns = input?.value.trim();
      if (!ns) return;
      try {
        await requestJson('/api/dsdl/custom/namespace', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ namespace: ns }),
        });
        dialog.remove();
        await _reloadTree();
        _expandedNodes.add(ns.split('.')[0]);
        _renderCustomTree();
      } catch (err) {
        _showEditorToast(err.message, true);
      }
    });

    document.getElementById('dsdlNsCancel')?.addEventListener('click', () => dialog.remove());
    input?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') document.getElementById('dsdlNsOk')?.click();
      if (e.key === 'Escape') dialog.remove();
    });
  };

  // ------------------------------------------------------------------
  // Editor panel
  // ------------------------------------------------------------------

  const _openEditorNew = async (prefilledNs) => {
    _editorOpen = true;
    _editorMode = 'new';
    _editPrefill = null;
    try {
      const resp = await requestJson('/api/dsdl/custom/namespaces');
      _customNamespaces = resp.namespaces || [];
    } catch { _customNamespaces = []; }
    _renderEditorSplit(prefilledNs);
  };

  const _confirmDeleteType = (fullName) => {
    const panel = document.getElementById('dsdlDetail');
    if (!panel) return;
    panel.querySelector('.dsdl-confirm-bar')?.remove();
    const bar = document.createElement('div');
    bar.className = 'dsdl-inline-dialog dsdl-confirm-bar';
    bar.innerHTML = `
      <span class="dsdl-confirm-text">Delete <code class="dsdl-confirm-code">${escapeHtml(fullName)}</code>?</span>
      <button class="dsdl-dialog-ok dsdl-dialog-danger" id="dsdlDelOk">Delete</button>
      <button class="dsdl-dialog-cancel" id="dsdlDelCancel" aria-label="Cancel delete">&times;</button>`;
    panel.insertBefore(bar, panel.firstChild);
    document.getElementById('dsdlDelOk')?.addEventListener('click', async () => {
      bar.remove();
      await _deleteType(fullName);
    });
    document.getElementById('dsdlDelCancel')?.addEventListener('click', () => bar.remove());
  };

  const _deleteType = async (fullName) => {
    try {
      await requestJson(`/api/dsdl/custom/type/${encodeURIComponent(fullName)}`, { method: 'DELETE' });
    } catch (err) {
      _showEditorToast(err.message, true);
      return;
    }
    if (_editorOpen && _editorMode === 'edit' && _editPrefill?.full_name === fullName) {
      _closeEditor();
      _editorMode = 'new';
      _editPrefill = null;
    }
    _selectedType = null;
    _lastDetailData = null;
    _saveDsdlState();
    const panel = document.getElementById('dsdlDetail');
    if (panel) {
      panel.innerHTML = `
        <div class="dsdl-detail-placeholder">
          <div class="dsdl-detail-placeholder-icon">{&nbsp;}</div>
          <div class="dsdl-detail-placeholder-text">Select a type to inspect</div>
        </div>`;
    }
    await _reloadTree();
  };

  const _openEditorEdit = async (typeData) => {
    _editorOpen = true;
    _editorMode = 'edit';
    _editPrefill = {
      namespace: typeData.namespace,
      type_name: typeData.short_name,
      version: typeData.version,
      source_text: typeData.source_text || '',
      fixed_port_id: typeData.fixed_port_id,
      full_name: typeData.full_name,
    };
    try {
      const resp = await requestJson('/api/dsdl/custom/namespaces');
      _customNamespaces = resp.namespaces || [];
    } catch { _customNamespaces = []; }
    _renderEditorSplit(typeData.namespace);
  };

  const _closeEditor = () => {
    _editorOpen = false;
    const area = document.getElementById('dsdlDetailArea');
    if (!area) return;

    const editorPanel = document.getElementById('dsdlEditorPanel');
    const editorHandle = document.getElementById('dsdlEditorHandle');
    if (editorPanel) editorPanel.remove();
    if (editorHandle) editorHandle.remove();

    const detail = document.getElementById('dsdlDetail');
    if (detail) detail.style.flex = '';
  };

  const _renderEditorSplit = (prefilledNs) => {
    const area = document.getElementById('dsdlDetailArea');
    if (!area) return;

    let editorPanel = document.getElementById('dsdlEditorPanel');
    if (!editorPanel) {
      editorPanel = document.createElement('div');
      editorPanel.id = 'dsdlEditorPanel';
      editorPanel.className = 'dsdl-editor-panel';
      area.insertBefore(editorPanel, area.firstChild);

      const handle = document.createElement('div');
      handle.id = 'dsdlEditorHandle';
      handle.className = 'dsdl-editor-handle';
      area.insertBefore(handle, editorPanel.nextSibling);

      _initEditorDrag();
    }

    const detail = document.getElementById('dsdlDetail');
    const hasDetail = _selectedType && _lastDetailData;
    const editorHandle = document.getElementById('dsdlEditorHandle');
    if (hasDetail) {
      editorPanel.style.flex = `0 0 ${_editorSplitRatio * 100}%`;
      if (detail) detail.style.flex = '1';
      if (editorHandle) editorHandle.style.display = '';
    } else {
      editorPanel.style.flex = '1';
      if (detail) detail.style.flex = '0 0 0';
      if (editorHandle) editorHandle.style.display = 'none';
    }

    const isEdit = _editorMode === 'edit';
    const prefill = _editPrefill || {};
    const nameValue = isEdit ? (prefill.type_name || '') : '';
    const versionValue = isEdit ? (prefill.version || '1.0') : '1.0';
    const portValue = isEdit && prefill.fixed_port_id != null ? String(prefill.fixed_port_id) : '';
    const sourceValue = isEdit ? (prefill.source_text || '') : '';
    const lockAttr = isEdit ? ' disabled' : '';
    const titleText = isEdit ? 'Edit DSDL Type' : 'New DSDL Type';
    const saveLabel = isEdit ? 'Save changes' : 'Save';

    const nsOptions = _customNamespaces.map(ns => {
      const sel = (prefilledNs && ns === prefilledNs) ? ' selected' : '';
      return `<option value="${escapeHtml(ns)}"${sel}>${escapeHtml(ns)}</option>`;
    }).join('');
    const nsHint = (!isEdit && _customNamespaces.length === 0)
      ? `<div class="dsdl-editor-hint">No namespaces yet — create one with the “+” button in the Custom section.</div>`
      : '';

    const policyTip = "Only types that aren't compiled can be edited or deleted. Compiled types are loaded by the running CAN runtime — changing them would diverge source from live code. Recompile (or clear python_compiled_messages/) to free a type for editing.";

    editorPanel.innerHTML = `
      <div class="dsdl-editor-toolbar">
        <span class="dsdl-editor-title">${titleText}</span>
        <span class="dsdl-editor-toolbar-actions">
          <span class="dsdl-editor-status" id="dsdlEditorStatus"></span>
          <span class="dsdl-info-tip" tabindex="0" aria-label="Save policy" data-tip="${escapeHtml(policyTip)}">?</span>
          <button class="dsdl-editor-btn dsdl-editor-btn-save" id="dsdlEditorSave">${saveLabel}</button>
          <button class="dsdl-editor-close" id="dsdlEditorClose" aria-label="Close editor">&times;</button>
        </span>
      </div>
      <div class="dsdl-editor-form">
        ${nsHint}
        <div class="dsdl-editor-row">
          <label class="dsdl-editor-label">Namespace</label>
          <div class="dsdl-editor-ns-wrap">
            <select class="dsdl-editor-select" id="dsdlEditorNs"${lockAttr}>
              <option value="">— select —</option>
              ${nsOptions}
            </select>
          </div>
        </div>
        <div class="dsdl-editor-row dsdl-editor-row-inline">
          <div>
            <label class="dsdl-editor-label">Type name</label>
            <input type="text" class="dsdl-editor-input" id="dsdlEditorName" placeholder="MyMessage" value="${escapeHtml(nameValue)}"${lockAttr} />
          </div>
          <div>
            <label class="dsdl-editor-label">Version</label>
            <input type="text" class="dsdl-editor-input dsdl-editor-ver" id="dsdlEditorVer" placeholder="1.0" value="${escapeHtml(versionValue)}"${lockAttr} />
          </div>
          <div>
            <label class="dsdl-editor-label">Port ID</label>
            <input type="text" class="dsdl-editor-input dsdl-editor-port" id="dsdlEditorPort" placeholder="optional" value="${escapeHtml(portValue)}" />
          </div>
        </div>
        <div class="dsdl-editor-row dsdl-editor-row-grow">
          <label class="dsdl-editor-label">Source</label>
          <div class="dsdl-editor-source-wrap" id="dsdlEditorSourceWrap">
            <textarea class="dsdl-editor-source" id="dsdlEditorSource" spellcheck="false"
                      placeholder="# Write your DSDL definition here&#10;uint32 my_field&#10;float32 temperature&#10;# add --- to split request/response for a service">${escapeHtml(sourceValue)}</textarea>
            <div class="dsdl-editor-preview-handle" id="dsdlPreviewHandle"></div>
            <div class="dsdl-editor-preview" id="dsdlEditorPreview">
              <div class="dsdl-editor-preview-label">Preview</div>
              <div class="dsdl-editor-preview-content" id="dsdlPreviewContent">
                <div class="dsdl-custom-empty">Type DSDL source above</div>
              </div>
            </div>
          </div>
        </div>
      </div>`;

    if (isEdit && sourceValue) _updatePreview(sourceValue);

    document.getElementById('dsdlEditorClose')?.addEventListener('click', _closeEditor);
    document.getElementById('dsdlEditorSave')?.addEventListener('click', _saveType);

    const sourceEl = document.getElementById('dsdlEditorSource');
    let previewTimer;
    sourceEl?.addEventListener('input', () => {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(() => _updatePreview(sourceEl.value), 250);
    });

    _initPreviewDrag();
    _applyPreviewRatio();
  };

  const _getEditorNamespace = () => {
    const select = document.getElementById('dsdlEditorNs');
    return select?.value || '';
  };

  const _parseDsdlSource = (text) => {
    const fieldRe = /^(?:truncated\s+|saturated\s+)?(\S+)\s+([a-zA-Z_]\w*)(?:\s*=\s*([^#]+))?/;
    let kind = 'message';
    let section = 'message';
    const fields = { message: [] };
    const constants = [];

    for (const line of text.split('\n')) {
      const s = line.trim();
      if (s === '---') {
        kind = 'service';
        fields.request = fields.message || [];
        delete fields.message;
        fields.response = [];
        section = 'response';
        continue;
      }
      if (!s || s.startsWith('#') || s.startsWith('@')) continue;
      const m = s.match(fieldRe);
      if (!m) continue;
      const [, type, name, value] = m;
      if (value !== undefined) { constants.push({ type, name, value: value.trim() }); continue; }
      if (type.startsWith('void')) continue;
      if (!fields[section]) fields[section] = [];
      fields[section].push({ type, name });
    }

    return {
      kind,
      fields: kind === 'service'
        ? { request: fields.request || [], response: fields.response || [] }
        : fields.message || [],
      constants,
    };
  };

  const _updatePreview = (sourceText) => {
    const content = document.getElementById('dsdlPreviewContent');
    if (!content) return;

    if (!sourceText.trim()) {
      content.innerHTML = '<div class="dsdl-custom-empty">Type DSDL source above</div>';
      return;
    }

    const parsed = _parseDsdlSource(sourceText);
    const isService = parsed.kind === 'service';
    const kindLabel = isService ? 'Service' : 'Message';
    const kindCls = isService ? 'dsdl-badge-service' : 'dsdl-badge-message';

    let fieldsHtml;
    if (isService) {
      fieldsHtml = `
        <div class="dsdl-preview-section dsdl-preview-section-req">
          <span class="dsdl-preview-label dsdl-preview-label-req">Request</span>
          ${_renderPreviewFields(parsed.fields.request)}
        </div>
        <div class="dsdl-preview-divider" aria-hidden="true">⎯ ⎯ ⎯</div>
        <div class="dsdl-preview-section dsdl-preview-section-res">
          <span class="dsdl-preview-label dsdl-preview-label-res">Response</span>
          ${_renderPreviewFields(parsed.fields.response)}
        </div>`;
    } else {
      fieldsHtml = `
        <div class="dsdl-preview-section">
          <span class="dsdl-preview-label">Fields</span>
          ${_renderPreviewFields(parsed.fields)}
        </div>`;
    }

    const constHtml = parsed.constants.length ? `
      <div class="dsdl-preview-section">
        <span class="dsdl-preview-label">Constants</span>
        <table class="dsdl-ftable"><tbody>
          ${parsed.constants.map(c => `<tr class="dsdl-frow">
            <td class="dsdl-fcol-type">${escapeHtml(c.type)}</td>
            <td class="dsdl-fcol-name">${escapeHtml(c.name)}</td>
            <td class="dsdl-fcol-val"><span class="dsdl-const-eq">=</span> ${escapeHtml(c.value)}</td>
          </tr>`).join('')}
        </tbody></table>
      </div>` : '';

    content.innerHTML = `
      <div class="dsdl-preview-header"><span class="dsdl-badge ${kindCls}">${kindLabel}</span></div>
      ${fieldsHtml}${constHtml}`;
  };

  const _renderPreviewFields = (fields) => {
    if (!fields.length) return '<div class="dsdl-custom-empty">No fields</div>';
    return `<table class="dsdl-ftable"><tbody>
      ${fields.map(f => `<tr class="dsdl-frow">
        <td class="dsdl-fcol-type">${escapeHtml(f.type)}</td>
        <td class="dsdl-fcol-name">${escapeHtml(f.name)}</td>
      </tr>`).join('')}
    </tbody></table>`;
  };

  const _applyPreviewRatio = () => {
    const wrap = document.getElementById('dsdlEditorSourceWrap');
    const source = document.getElementById('dsdlEditorSource');
    const preview = document.getElementById('dsdlEditorPreview');
    if (!wrap || !source || !preview) return;
    source.style.flex = `${_previewRatio}`;
    preview.style.flex = `${1 - _previewRatio}`;
  };

  const _initPreviewDrag = () => {
    const handle = document.getElementById('dsdlPreviewHandle');
    const wrap = document.getElementById('dsdlEditorSourceWrap');
    if (!handle || !wrap) return;

    let dragging = false;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('dragging');
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      if (!dragging) return;
      const rect = wrap.getBoundingClientRect();
      const y = e.clientY - rect.top;
      _previewRatio = Math.max(0.15, Math.min(0.85, y / rect.height));
      _applyPreviewRatio();
    };

    const onUp = () => {
      dragging = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  };

  const _saveType = async () => {
    const namespace = _getEditorNamespace();
    const typeName = document.getElementById('dsdlEditorName')?.value.trim();
    const version = document.getElementById('dsdlEditorVer')?.value.trim();
    const source = document.getElementById('dsdlEditorSource')?.value;
    const portStr = document.getElementById('dsdlEditorPort')?.value.trim();
    const portId = portStr ? parseInt(portStr, 10) : null;

    if (!namespace || !typeName || !version || !source) {
      _showEditorToast('Fill in namespace, name, version, and source', true);
      return;
    }

    const status = document.getElementById('dsdlEditorStatus');
    if (status) { status.textContent = 'Saving…'; status.className = 'dsdl-editor-status'; }

    try {
      const overwrite = _editorMode === 'edit';
      await requestJson('/api/dsdl/custom/type', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          namespace, type_name: typeName, version,
          source_text: source, fixed_port_id: portId, overwrite,
        }),
      });

      const fullName = `${namespace}.${typeName}.${version}`;
      _editPrefill = {
        namespace,
        type_name: typeName,
        version,
        source_text: source,
        fixed_port_id: portId,
        full_name: fullName,
      };

      _showEditorToast('Saved', false);
      await _reloadTree();
      _loadTypeDetail(fullName);
      _expandToType(fullName);
    } catch (err) {
      _showEditorToast(err.message, true);
    }
  };

  const _reloadTree = async () => {
    _namespacesData = null;
    _statusData = null;
    try {
      const [statusResp, nsResp] = await Promise.all([
        requestJson('/api/dsdl/status'),
        requestJson('/api/dsdl/namespaces'),
      ]);
      _statusData = statusResp;
      _namespacesData = nsResp.namespaces;
      _buildTelemetryIndex();
      _renderTreeHeaders();
      _renderTree();
      _renderCustomTree();
      _lockEditorIfCompiled();
      if (_selectedType) _loadTypeDetail(_selectedType);
    } catch {}
  };

  const _lockEditorIfCompiled = () => {
    if (!_editorOpen || !_editPrefill?.full_name) return;
    const fresh = _getTypeIndex()[_editPrefill.full_name];
    if (!fresh || !fresh.compiled) return;

    const panel = document.getElementById('dsdlEditorPanel');
    if (!panel || panel.classList.contains('dsdl-editor-locked')) return;
    panel.classList.add('dsdl-editor-locked');

    panel.querySelectorAll('input, textarea, select, button:not(.dsdl-editor-close)').forEach(el => {
      el.disabled = true;
    });

    const form = panel.querySelector('.dsdl-editor-form');
    if (form && !panel.querySelector('.dsdl-editor-locked-banner')) {
      const banner = document.createElement('div');
      banner.className = 'dsdl-editor-locked-banner';
      banner.textContent = 'This type was compiled — editing is locked. Clear python_compiled_messages/ and recompile to edit it again.';
      panel.insertBefore(banner, form);
    }
  };

  const _recompilePublic = async () => {
    const btn = document.getElementById('dsdlRecompileBtn');
    if (btn) btn.classList.add('dsdl-spin');
    _clearCompileError();
    try {
      const result = await requestJson('/api/dsdl/compile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'public' }),
      });
      if (!result.ok) {
        _showCompileError((result.errors || []).join('\n'));
      }
      await _reloadTree();
    } catch (err) {
      _showCompileError(err.message);
    } finally {
      if (btn) btn.classList.remove('dsdl-spin');
    }
  };

  const _showCompileError = (msg) => {
    const header = document.getElementById('dsdlPublicHeader');
    if (!header) return;
    let errEl = header.parentElement.querySelector('.dsdl-compile-error');
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'dsdl-compile-error';
      header.after(errEl);
    }
    errEl.textContent = msg;
  };

  const _clearCompileError = () => {
    document.querySelector('.dsdl-compile-error')?.remove();
  };

  const _showEditorToast = (msg, isError) => {
    const status = document.getElementById('dsdlEditorStatus');
    if (status) {
      status.textContent = msg;
      status.className = `dsdl-editor-status ${isError ? 'dsdl-editor-error' : 'dsdl-editor-ok'}`;
      setTimeout(() => { if (status.textContent === msg) status.textContent = ''; }, 5000);
      return;
    }
    const tree = document.getElementById('dsdlTree');
    if (!tree) return;
    const toast = document.createElement('div');
    toast.className = `dsdl-toast ${isError ? 'dsdl-toast-error' : 'dsdl-toast-ok'}`;
    toast.textContent = msg;
    tree.parentElement.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
  };

  const _initEditorDrag = () => {
    const handle = document.getElementById('dsdlEditorHandle');
    const area = document.getElementById('dsdlDetailArea');
    if (!handle || !area) return;

    let dragging = false;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      if (!dragging) return;
      const rect = area.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const ratio = Math.max(0.15, Math.min(0.85, x / rect.width));
      _editorSplitRatio = ratio;
      const editor = document.getElementById('dsdlEditorPanel');
      const detail = document.getElementById('dsdlDetail');
      if (editor) editor.style.flex = `0 0 ${ratio * 100}%`;
      if (detail) detail.style.flex = '1';
    };

    const onUp = () => {
      dragging = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  };

  // ------------------------------------------------------------------
  // Events
  // ------------------------------------------------------------------

  const _bindEvents = () => {
    const searchInput = document.getElementById('dsdlSearch');
    let debounceTimer;
    searchInput?.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        _searchTerm = searchInput.value;
        _renderTree();
        _renderCustomTree();
        _saveDsdlState();
      }, 200);
    });

    const treeHandler = (e) => {
      const addNsBtn = e.target.closest('[data-add-ns]');
      if (addNsBtn) {
        e.stopPropagation();
        _showNewNamespaceDialog(addNsBtn.dataset.addNs);
        return;
      }
      const addTypeBtn = e.target.closest('[data-add-type]');
      if (addTypeBtn) {
        e.stopPropagation();
        _openEditorNew(addTypeBtn.dataset.addType);
        return;
      }
      const nsRow = e.target.closest('.dsdl-ns-row');
      if (nsRow) {
        const ns = nsRow.dataset.ns;
        if (_expandedNodes.has(ns)) _expandedNodes.delete(ns);
        else _expandedNodes.add(ns);
        _renderTree();
        _renderCustomTree();
        _saveDsdlState();
        return;
      }
      const typeRow = e.target.closest('.dsdl-type-row');
      if (typeRow) _loadTypeDetail(typeRow.dataset.type);
    };

    document.getElementById('dsdlTree')?.addEventListener('click', treeHandler);
    document.getElementById('dsdlCustomTree')?.addEventListener('click', treeHandler);

    _initSplitDrag();
  };

  // ------------------------------------------------------------------
  // Split drag handle
  // ------------------------------------------------------------------

  const _initSplitDrag = () => {
    const handle = document.getElementById('dsdlSplitHandle');
    const panel = document.getElementById('dsdlTreePanel');
    if (!handle || !panel) return;

    const saved = _loadDsdlState();
    if (saved.treeWidth) {
      panel.style.width = saved.treeWidth + 'px';
    }

    let startX, startW;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      startX = e.clientX;
      startW = panel.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      const dx = e.clientX - startX;
      const newW = Math.max(180, Math.min(600, startW + dx));
      panel.style.width = newW + 'px';
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      _treeWidth = panel.getBoundingClientRect().width;
      _saveDsdlState();
    };
  };

  // ------------------------------------------------------------------
  // State persistence
  // ------------------------------------------------------------------

  const _saveDsdlState = () => {
    try {
      const data = {
        expandedNodes: [..._expandedNodes],
        selectedType: _selectedType,
        searchTerm: _searchTerm,
        treeWidth: _treeWidth,
        hiddenNamespaces: [..._hiddenNamespaces],
      };
      localStorage.setItem('cynitor.dsdl.state', JSON.stringify(data));
    } catch {}
  };

  const _loadDsdlState = () => {
    try {
      return JSON.parse(localStorage.getItem('cynitor.dsdl.state') || '{}');
    } catch { return {}; }
  };

  const _restoreState = () => {
    const saved = _loadDsdlState();
    if (Array.isArray(saved.expandedNodes)) {
      _expandedNodes = new Set(saved.expandedNodes);
    }
    if (saved.selectedType) {
      _selectedType = saved.selectedType;
    }
    if (saved.searchTerm) {
      _searchTerm = saved.searchTerm;
    }
    if (saved.treeWidth) {
      _treeWidth = saved.treeWidth;
    }
    if (Array.isArray(saved.hiddenNamespaces)) {
      _hiddenNamespaces = new Set(saved.hiddenNamespaces);
    }
  };

  // ------------------------------------------------------------------
  // Fallback states
  // ------------------------------------------------------------------

  const _renderDisconnected = () => {
    const ph = document.getElementById('dsdlPublicHeader');
    if (ph) ph.innerHTML = '';
    const ch = document.getElementById('dsdlCustomHeader');
    if (ch) ch.innerHTML = '';
    const tree = document.getElementById('dsdlTree');
    if (tree) tree.innerHTML = '<div class="dsdl-tree-empty">Connect to server to browse DSDL types.</div>';
    const ct = document.getElementById('dsdlCustomTree');
    if (ct) ct.innerHTML = '';
    const detail = document.getElementById('dsdlDetail');
    if (detail) detail.innerHTML = '<div class="dsdl-detail-placeholder"><div class="dsdl-detail-placeholder-text">Not connected.</div></div>';
  };

  const _renderError = (msg) => {
    const tree = document.getElementById('dsdlTree');
    if (tree) tree.innerHTML = `<div class="dsdl-tree-empty">Error: ${escapeHtml(msg)}</div>`;
  };

  return { init, hide };
})();
