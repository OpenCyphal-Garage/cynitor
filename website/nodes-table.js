// Tabulator-based node table: column definitions, formatters, build/refresh
// logic, and the favourite/hide actions. The Tabulator instance itself is
// stored on the global `nodesTabulator` (declared in state.js) so other
// files can read column widths/header filters when persisting settings.

const deleteGhostNode = async (uniqueIdHex) => {
  try {
    await requestJson(`/api/identity/${uniqueIdHex}`, { method: 'DELETE' });
    if (state.selectedNodeId === `uid:${uniqueIdHex}`) clearSelectedNode();
  } catch (e) {
    console.error('Failed to delete ghost node:', e);
  }
};

const idsHeaderFilter = (headerValue, rowValue) => {
  if (!headerValue) return true;
  const terms = headerValue.split(',').map((t) => t.trim()).filter(Boolean);
  if (!terms.length) return true;
  const cellStr = String(rowValue);
  return terms.some((t) => cellStr.includes(t));
};

const stateFormatter = (cell) => {
  const v = cell.getValue();
  return `<span class="state-cell"><span class="state-dot ${escapeHtml(v)}"></span><span class="state-label">${escapeHtml(v)}</span></span>`;
};

const healthFormatter = (cell) => {
  const v = cell.getValue() || '-';
  const cls = v === 'NOMINAL' ? 'nominal'
    : v === 'ADVISORY' ? 'advisory'
    : v === 'CAUTION' ? 'caution'
    : v === 'WARNING' ? 'warning'
    : 'unknown';
  const icon = v === 'NOMINAL' ? ''
    : v === 'ADVISORY' ? '<span class="health-icon" aria-hidden="true">~</span>'
    : v === 'CAUTION' ? '<span class="health-icon" aria-hidden="true">!</span>'
    : v === 'WARNING' ? '<span class="health-icon" aria-hidden="true">!!</span>'
    : '';
  return `<span class="health-text health-${escapeHtml(cls)}">${icon}${escapeHtml(v)}</span>`;
};

const portsFormatter = (cell) => {
  const text = cell.getValue() || '-';
  const cls = text !== '-' ? 'has-ports' : '';
  return `<span class="port-ids ${cls}">${escapeHtml(text)}</span>`;
};

// Pass these to Tabulator as pre-joined strings, not fresh arrays. Each
// /api/nodes response gives node.publishers a new array reference even
// when the contents didn't change, so Tabulator's strict-equality cell
// diff would treat the value as changed every time and re-render the
// cell. That re-render briefly destroys the cell's overflow state and
// makes the horizontal scrollbar blink. Strings compare by value.
const portsToString = (arr) => (Array.isArray(arr) && arr.length ? arr.join(', ') : '-');

const rateFormatter = (cell) => {
  const v = Number(cell.getValue());
  if (v > 0 && v < 1) return '&lt;1 Hz';
  return `${escapeHtml(v.toFixed(1))} Hz`;
};

const favFormatter = (cell) => {
  const isFav = cell.getValue();
  return `<button type="button" class="fav-star ${isFav ? 'active' : ''}" aria-label="Toggle favourite">${isFav ? '★' : '☆'}</button>`;
};

const nameFormatter = (cell) => {
  const row = cell.getRow().getData();
  const alias = getNodeAlias(row._uid);
  const original = (() => {
    const nodes = state.latestNodesPayload?.nodes || {};
    const node = nodes[row.id];
    return node?.name || null;
  })();
  const display = alias || original || '-';
  const tooltip = alias && original ? ` title="${escapeHtml(original)}"` : '';
  const editIcon = row._uid?.length
    ? '<span class="name-edit-icon" aria-hidden="true">✎</span>'
    : '';
  return `<span class="name-cell">${editIcon}<span class="name-display"${tooltip}>${escapeHtml(display)}</span></span>`;
};

const startNameEdit = (cell) => {
  const row = cell.getRow().getData();
  if (!row._uid?.length) return;
  const cellEl = cell.getElement();
  if (cellEl.querySelector('.name-input')) return;

  const alias = getNodeAlias(row._uid);
  const nodes = state.latestNodesPayload?.nodes || {};
  const original = nodes[row.id]?.name || '';

  cellEl.innerHTML = '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'name-input';
  input.value = alias || '';
  input.placeholder = original || 'Enter alias…';
  input.setAttribute('aria-label', `Alias for node ${row.id}`);
  cellEl.appendChild(input);
  input.focus();
  input.select();

  let done = false;
  const save = () => {
    if (done) return;
    done = true;
    setNodeAlias(row._uid, input.value);
    renderNodesTable();
  };
  const discard = () => {
    if (done) return;
    done = true;
    cellEl.innerHTML = nameFormatter(cell);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); discard(); }
  });
  input.addEventListener('blur', discard);
};

const EYE_ICON = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

const actionsFormatter = () => {
  return `<button type="button" class="action-hide" aria-label="Hide node">${EYE_ICON}</button>`;
};

const toggleFavourite = (nodeId) => {
  const nodes = state.latestNodesPayload?.nodes || {};
  const node = nodes[String(nodeId)];
  const key = node ? nodeStableKey(node) : `nid:${nodeId}`;
  if (state.favouriteNodeIds.has(key)) {
    state.favouriteNodeIds.delete(key);
  } else {
    state.favouriteNodeIds.add(key);
  }
  saveSettings();
  renderNodesTable();
  if (nodesTabulator) {
    const sorters = nodesTabulator.getSorters();
    if (sorters.length) {
      nodesTabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
    }
  }
};

const hideNode = (nodeId) => {
  const nodes = state.latestNodesPayload?.nodes || {};
  const node = nodes[String(nodeId)];
  const key = node ? nodeStableKey(node) : `nid:${nodeId}`;
  state.hiddenNodeIds.add(key);
  if (state.selectedNodeId === nodeId) {
    clearSelectedNode();
  }
  saveSettings();
  renderNodesTable();
  updateHiddenChip();
};

const unhideNode = (stableKey) => {
  state.hiddenNodeIds.delete(stableKey);
  saveSettings();
  renderNodesTable();
  updateHiddenChip();
};

const unhideAllNodes = () => {
  state.hiddenNodeIds.clear();
  saveSettings();
  renderNodesTable();
  updateHiddenChip();
};

const injectHiddenChip = () => {
  if (document.getElementById('hiddenNodesPopover')) return;

  // Popover lives on document.body so Tabulator's overflow:hidden can't clip it
  const popover = document.createElement('div');
  popover.id = 'hiddenNodesPopover';
  popover.className = 'hidden-popover hidden';
  document.body.appendChild(popover);
};

const updateHiddenChip = () => {
  const chip = el('hiddenNodesChip');
  if (!chip) return;
  const count = state.hiddenNodeIds.size;
  if (count === 0) {
    chip.classList.add('hidden');
    const popover = el('hiddenNodesPopover');
    if (popover) popover.classList.add('hidden');
    return;
  }
  chip.classList.remove('hidden');
  chip.innerHTML = EYE_OFF_ICON + `<span>${count}</span>`;
};

const _findNodeByStableKey = (key) => {
  const nodes = state.latestNodesPayload?.nodes || {};
  for (const node of Object.values(nodes)) {
    if (nodeStableKey(node) === key) return node;
  }
  if (key.startsWith('nid:')) {
    const nid = key.slice(4);
    return nodes[nid] || null;
  }
  return null;
};

const populateHiddenPopover = () => {
  const popover = el('hiddenNodesPopover');
  const chip = el('hiddenNodesChip');
  if (!popover || !chip) return;

  if (state.hiddenNodeIds.size === 0) {
    popover.classList.add('hidden');
    return;
  }

  positionPopover(popover, chip);

  let html = '<div class="hidden-popover-header"><span>Hidden nodes</span>'
    + '<button type="button" class="hidden-unhide-all" aria-label="Unhide all nodes">Unhide all</button></div>'
    + '<div class="hidden-popover-list">';
  for (const key of state.hiddenNodeIds) {
    const node = _findNodeByStableKey(key);
    const displayId = node ? node.node_id : key;
    const name = getNodeAlias(node?.unique_id) || node?.name || `Node ${displayId}`;
    const online = node && !node.has_disappeared;
    const dotCls = online ? 'ok' : '';
    const stateLabel = online ? 'online' : 'offline';
    html += `<div class="hidden-popover-row" data-stable-key="${escapeHtml(key)}">`
      + `<span class="hidden-popover-dot ${dotCls}"></span>`
      + `<span class="hidden-popover-id">${escapeHtml(String(displayId))}</span>`
      + `<span class="hidden-popover-name">${escapeHtml(name)}</span>`
      + `<span class="hidden-popover-state">${stateLabel}</span>`
      + `<button type="button" class="hidden-unhide-btn" aria-label="Unhide node ${escapeHtml(String(displayId))}">Unhide</button>`
      + `</div>`;
  }
  html += '</div>';
  popover.innerHTML = html;

  popover.querySelector('.hidden-unhide-all')?.addEventListener('click', (e) => {
    e.stopPropagation();
    unhideAllNodes();
  });
  for (const btn of popover.querySelectorAll('.hidden-unhide-btn')) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const stableKey = btn.closest('.hidden-popover-row').dataset.stableKey;
      unhideNode(stableKey);
      populateHiddenPopover();
    });
  }
};

const toggleHiddenPopover = () => {
  const popover = el('hiddenNodesPopover');
  if (!popover) return;
  if (state.hiddenNodeIds.size === 0) {
    popover.classList.add('hidden');
    return;
  }
  popover.classList.toggle('hidden');
  if (!popover.classList.contains('hidden')) {
    populateHiddenPopover();
  }
};

const tablePlaceholder = () => {
  return eventSourcePlaceholder('discover CAN nodes')
    || svcStateMsg('<span class="svc-spinner"></span>', 'Waiting for nodes…', 'Listening on the CAN bus. Nodes will appear as they send heartbeats.');
};

const buildTableData = () => {
  const payloadNodes = state.latestNodesPayload?.nodes;
  if (!payloadNodes || typeof payloadNodes !== 'object') return [];

  const rows = [];
  for (const [key, node] of Object.entries(payloadNodes)) {
    if (state.hiddenNodeIds.has(nodeStableKey(node))) continue;
    const isGhost = node._ghost === true;
    const nodeState = getNodeVisualState(node);
    const alias = getNodeAlias(isGhost ? null : node.unique_id);
    rows.push({
      id: isGhost ? key : node.node_id,
      _sortId: isGhost ? (node.last_node_id ?? Infinity) : node.node_id,
      _fav: state.favouriteNodeIds.has(nodeStableKey(node)),
      _uid: isGhost ? node.unique_id_hex : node.unique_id,
      _ghost: isGhost,
      name: alias || node.name || '-',
      state: nodeState,
      health: (isGhost || node.has_disappeared) ? '-' : (getNodeHealthValue(node.node_id) || '-'),
      rate: isGhost ? 0 : getNodeRate(node.node_id),
      uptime: isGhost ? formatLastSeen(node.last_seen) : (node.has_disappeared ? formatLastSeen(node.last_seen) : formatUptime(node.uptime)),
      publishers: portsToString(node.publishers),
      subscribers: portsToString(node.subscribers),
      servers: portsToString(node.servers),
      clients: portsToString(node.clients),
      _actions: nodeState,
    });
  }
  return rows;
};

const initNodesTable = () => {
  const sortKey = state.tableSort.key === 'id' ? '_sortId' : state.tableSort.key;
  const initialSort = sortKey
    ? [{ column: sortKey, dir: state.tableSort.dir }]
    : [{ column: '_sortId', dir: 'asc' }];

  const settings = readSettings();

  const favPinSorter = makeFavPinSorter({ ghostField: '_ghost' });

  const colDef = (title, field, opts = {}) => {
    const def = { title, field, headerFilter: 'input', ...opts };
    def.sorter = favPinSorter(opts.sorter || 'string');
    return def;
  };

  nodesTabulator = new Tabulator('#nodesTable', {
    data: [],
    layout: 'fitColumns',
    resizableColumns: true,
    selectable: 1,
    placeholder: tablePlaceholder(),
    initialSort,
    columns: [
      { title: '', field: '_fav', formatter: favFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-fav', cellClick: (_e, cell) => { toggleFavourite(cell.getRow().getData().id); } },
      colDef('ID', '_sortId', { sorter: 'number', minWidth: 50, widthGrow: 0.5, headerFilterPlaceholder: 'id', cssClass: 'cell-scroll', formatter: (cell) => {
        const row = cell.getRow().getData();
        if (!row._ghost) return escapeHtml(String(row.id));
        return '<button class="ghost-delete-btn" aria-label="Remove ghost node" title="Remove">✕</button>';
      }, cellClick: (e, cell) => {
        if (e.target.closest('.ghost-delete-btn')) {
          e.stopPropagation();
          const row = cell.getRow().getData();
          if (row._ghost && row._uid) deleteGhostNode(row._uid);
        }
      }, headerFilterFunc: (headerValue, _rowValue, rowData) => { if (!headerValue) return true; return String(rowData.id).includes(headerValue); } }),
      colDef('Name', 'name', { sorter: 'string', minWidth: 100, widthGrow: 2, formatter: nameFormatter, headerFilterPlaceholder: 'name', cssClass: 'cell-scroll cell-name', cellDblClick: (_e, cell) => { startNameEdit(cell); } }),
      colDef('State', 'state', { sorter: 'string', minWidth: 40, widthGrow: 0.7, formatter: stateFormatter, headerFilterPlaceholder: 'state', cssClass: 'td-state' }),
      colDef('Health', 'health', { sorter: 'string', minWidth: 70, widthGrow: 0.8, formatter: healthFormatter, headerFilterPlaceholder: 'health', cssClass: 'cell-scroll' }),
      colDef('Rate', 'rate', { sorter: 'number', minWidth: 70, widthGrow: 0.7, formatter: rateFormatter, headerFilterPlaceholder: 'rate', cssClass: 'cell-scroll', headerFilterFunc: (headerValue, rowValue) => { if (!headerValue) return true; return Number(rowValue).toFixed(1).includes(headerValue); } }),
      colDef('Uptime', 'uptime', { sorter: 'string', minWidth: 100, widthGrow: 1, headerFilterPlaceholder: 'uptime', cssClass: 'cell-scroll' }),
      colDef('Publishers', 'publishers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'pub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Subscribers', 'subscribers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'sub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Servers', 'servers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Clients', 'clients', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      { title: '', field: '_actions', formatter: actionsFormatter, width: 56, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', titleFormatter: () => { const btn = document.createElement('button'); btn.type = 'button'; btn.id = 'hiddenNodesChip'; btn.className = 'hidden-chip hidden'; btn.setAttribute('aria-label', 'Show hidden nodes'); btn.addEventListener('click', (e) => { e.stopPropagation(); toggleHiddenPopover(); }); return btn; }, cellClick: (e, cell) => { e.stopPropagation(); hideNode(cell.getRow().getData().id); } },
    ],
  });

  nodesTabulator.on('rowClick', (_e, row) => {
    if (_e.target.closest('.name-input') || _e.target.closest('.action-hide')) return;
    const clickedId = row.getData().id;
    if (clickedId === state.selectedNodeId) {
      clearSelectedNode();
    } else {
      setSelectedNode(clickedId);
    }
  });
  nodesTabulator.on('dataSorted', (sorters) => {
    if (sorters.length > 0) {
      state.tableSort = { key: sorters[0].field, dir: sorters[0].dir };
      saveSettings();
    }
  });
  nodesTabulator.on('dataFiltered', () => {
    saveSettings();
  });

  nodesTabulator.on('tableBuilt', () => {
    const savedFilters = settings.headerFilters || {};
    for (const [field, value] of Object.entries(savedFilters)) {
      if (value) {
        nodesTabulator.setHeaderFilterValue(field, value);
      }
    }
    injectHiddenChip();
    updateHiddenChip();
    renderNodesTable();
  });
};

const renderNodesTable = () => {
  if (state.activeView !== 'nodes') return;

  const data = buildTableData();

  if (!nodesTabulator) return;

  if (!data.length) {
    nodesTabulator.clearData();
    const ph = document.querySelector('#nodesTable .tabulator-placeholder');
    if (ph) {
      ph.innerHTML = tablePlaceholder();
    }
    return;
  }

  diffUpdateTable(nodesTabulator, data, 'id');

  for (const row of nodesTabulator.getRows()) {
    row.getElement().classList.toggle('selected-row', row.getData().id === state.selectedNodeId);
  }
};

const scheduleTableRefresh = () => {
  if (_tableRefreshPending) return;
  _tableRefreshPending = window.setTimeout(() => {
    _tableRefreshPending = null;
    renderNodesTable();
    refreshSubjectsTable();
  }, 1000);
};
