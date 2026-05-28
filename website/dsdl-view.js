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
          <div class="dsdl-tree-header" id="dsdlTreeHeader"></div>
          <div class="dsdl-search-wrap">
            <input type="text" class="dsdl-search" id="dsdlSearch"
                   placeholder="Search types or fields…" aria-label="Search DSDL types" />
          </div>
          <div class="dsdl-tree" id="dsdlTree"></div>
        </div>
        <div class="dsdl-split-handle" id="dsdlSplitHandle"></div>
        <div class="dsdl-detail-panel" id="dsdlDetail">
          <div class="dsdl-detail-placeholder">
            <div class="dsdl-detail-placeholder-icon">{&nbsp;}</div>
            <div class="dsdl-detail-placeholder-text">Select a type to inspect</div>
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
      _renderTreeHeader();
      _renderTree();
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
  // Tree header (compile status + counts)
  // ------------------------------------------------------------------

  const _renderTreeHeader = () => {
    const header = document.getElementById('dsdlTreeHeader');
    if (!header || !_statusData) return;

    const count = _statusData.source_types + _statusData.custom_types;
    const dot = _statusData.compiled ? 'dsdl-dot-ok' : 'dsdl-dot-warn';
    const label = _statusData.compiled ? 'Compiled' : 'Not compiled';
    const age = _statusData.last_compiled ? ` · ${_formatAge(_statusData.last_compiled)}` : '';

    header.innerHTML = `
      <span class="dsdl-tree-count">${count} types</span>
      <span class="dsdl-tree-status"><span class="dsdl-dot ${dot}"></span>${escapeHtml(label)}${escapeHtml(age)}</span>`;
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

  const _renderTree = () => {
    const container = document.getElementById('dsdlTree');
    if (!container || !_namespacesData) return;

    const term = _searchTerm.toLowerCase().trim();
    let html = '';
    for (const [name, node] of Object.entries(_namespacesData)) {
      html += _renderNamespaceNode(name, name, node, 0, term);
    }

    if (!html) {
      container.innerHTML = '<div class="dsdl-tree-empty">No matching types.</div>';
      return;
    }
    container.innerHTML = html;
    _updateBusDots();
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
      _renderTypeDetail(data);
    } catch (err) {
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

    _updateBusDetail();
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
    requestAnimationFrame(() => {
      const entry = document.querySelector(`.dsdl-type-row[data-type="${CSS.escape(fullName)}"]`);
      if (entry) entry.scrollIntoView({ block: 'nearest' });
    });
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
        _saveDsdlState();
      }, 200);
    });

    const tree = document.getElementById('dsdlTree');
    tree?.addEventListener('click', (e) => {
      const nsRow = e.target.closest('.dsdl-ns-row');
      if (nsRow) {
        const ns = nsRow.dataset.ns;
        if (_expandedNodes.has(ns)) _expandedNodes.delete(ns);
        else _expandedNodes.add(ns);
        _renderTree();
        _saveDsdlState();
        return;
      }
      const typeRow = e.target.closest('.dsdl-type-row');
      if (typeRow) _loadTypeDetail(typeRow.dataset.type);
    });

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
  };

  // ------------------------------------------------------------------
  // Fallback states
  // ------------------------------------------------------------------

  const _renderDisconnected = () => {
    const header = document.getElementById('dsdlTreeHeader');
    if (header) header.innerHTML = '';
    const tree = document.getElementById('dsdlTree');
    if (tree) tree.innerHTML = '<div class="dsdl-tree-empty">Connect to server to browse DSDL types.</div>';
    const detail = document.getElementById('dsdlDetail');
    if (detail) detail.innerHTML = '<div class="dsdl-detail-placeholder"><div class="dsdl-detail-placeholder-text">Not connected.</div></div>';
  };

  const _renderError = (msg) => {
    const tree = document.getElementById('dsdlTree');
    if (tree) tree.innerHTML = `<div class="dsdl-tree-empty">Error: ${escapeHtml(msg)}</div>`;
  };

  return { init, hide };
})();
