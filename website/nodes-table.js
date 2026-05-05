// Tabulator-based node table: column definitions, formatters, build/refresh
// logic, and the favourite/hide actions. The Tabulator instance itself is
// stored on the global `nodesTabulator` (declared in state.js) so other
// files can read column widths/header filters when persisting settings.

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
  const v = cell.getValue();
  return `${escapeHtml(Number(v).toFixed(1))} Hz`;
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

const EYE_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

const actionsFormatter = () => {
  return `<button type="button" class="action-hide" aria-label="Hide node">${EYE_ICON}</button>`;
};

const toggleFavourite = (nodeId) => {
  if (state.favouriteNodeIds.has(nodeId)) {
    state.favouriteNodeIds.delete(nodeId);
  } else {
    state.favouriteNodeIds.add(nodeId);
  }
  saveSettings();
  renderNodesTable();
  // _fav is consulted by the wrapped favPinSorter but isn't itself a
  // sorted column, so Tabulator's auto-resort wouldn't fire. Force a
  // re-sort so the row moves to/from the top of its sort group.
  if (nodesTabulator) {
    const sorters = nodesTabulator.getSorters();
    if (sorters.length) {
      nodesTabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
    }
  }
};

const hideNode = (nodeId) => {
  state.hiddenNodeIds.add(nodeId);
  if (state.selectedNodeId === nodeId) {
    clearSelectedNode();
  }
  saveSettings();
  renderNodesTable();
  updateHiddenChip();
};

const unhideNode = (nodeId) => {
  state.hiddenNodeIds.delete(nodeId);
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

const populateHiddenPopover = () => {
  const popover = el('hiddenNodesPopover');
  const chip = el('hiddenNodesChip');
  if (!popover || !chip) return;

  if (state.hiddenNodeIds.size === 0) {
    popover.classList.add('hidden');
    return;
  }

  const rect = chip.getBoundingClientRect();
  popover.style.top = (rect.bottom + 4) + 'px';
  popover.style.right = (window.innerWidth - rect.right) + 'px';

  const nodes = state.latestNodesPayload?.nodes || {};
  let html = '<div class="hidden-popover-header"><span>Hidden nodes</span>'
    + '<button type="button" class="hidden-unhide-all" aria-label="Unhide all nodes">Unhide all</button></div>'
    + '<div class="hidden-popover-list">';
  for (const id of state.hiddenNodeIds) {
    const node = nodes[id];
    const name = getNodeAlias(node?.unique_id) || node?.name || `Node ${id}`;
    const online = node && !node.has_disappeared;
    const dotCls = online ? 'ok' : '';
    const stateLabel = online ? 'online' : 'offline';
    html += `<div class="hidden-popover-row" data-node-id="${id}">`
      + `<span class="hidden-popover-dot ${dotCls}"></span>`
      + `<span class="hidden-popover-id">${escapeHtml(id)}</span>`
      + `<span class="hidden-popover-name">${escapeHtml(name)}</span>`
      + `<span class="hidden-popover-state">${stateLabel}</span>`
      + `<button type="button" class="hidden-unhide-btn" aria-label="Unhide node ${id}">Unhide</button>`
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
      const nodeId = Number(btn.closest('.hidden-popover-row').dataset.nodeId);
      unhideNode(nodeId);
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
  if (!state.dashboardConnected) {
    if (state.pendingReconnect) {
      return svcStateMsg('<span class="svc-spinner"></span>', 'Reconnecting to backend…', 'Restoring previous session.');
    }
    return svcStateMsg('⏻', 'Not connected to backend', 'Connect to the backend server to discover CAN nodes.');
  }
  if (state.canState === CONN.CONNECTING) {
    return svcStateMsg('<span class="svc-spinner"></span>', 'Connecting to CAN interface…', 'Establishing CAN bus connection. Nodes will appear shortly.');
  }
  if (state.canState !== CONN.CONNECTED) {
    return svcStateMsg('⛓', 'CAN bus not connected', 'Connect a CAN interface to start discovering nodes.');
  }
  return svcStateMsg('<span class="svc-spinner"></span>', 'Waiting for nodes…', 'Listening on the CAN bus. Nodes will appear as they send heartbeats.');
};

const buildTableData = () => {
  const payloadNodes = state.latestNodesPayload?.nodes;
  const allNodes = payloadNodes && typeof payloadNodes === 'object' ? Object.values(payloadNodes) : [];

  const rows = allNodes
    .filter((node) => !state.hiddenNodeIds.has(node.node_id))
    .map((node) => {
      const nodeState = getNodeVisualState(node);
      const alias = getNodeAlias(node.unique_id);
      return {
        id: node.node_id,
        _fav: state.favouriteNodeIds.has(node.node_id),
        _uid: node.unique_id,
        name: alias || node.name || '-',
        state: nodeState,
        health: getNodeHealthValue(node.node_id) || '-',
        rate: getNodeRate(node.node_id),
        uptime: node.has_disappeared ? formatLastSeen(node.last_seen) : formatUptime(node.uptime),
        publishers: portsToString(node.publishers),
        subscribers: portsToString(node.subscribers),
        servers: portsToString(node.servers),
        clients: portsToString(node.clients),
        _actions: nodeState,
      };
    });
  return rows;
};

const initNodesTable = () => {
  const initialSort = state.tableSort.key
    ? [{ column: state.tableSort.key, dir: state.tableSort.dir }]
    : [{ column: 'id', dir: 'asc' }];

  const settings = readSettings();
  const savedWidths = settings.columnWidths || {};

  // Wrap sorters to always pin favourites to top
  const favPinSorter = (baseSorter) => (a, b, aRow, bRow, column, dir, sorterParams) => {
    const aFav = aRow.getData()._fav ? 1 : 0;
    const bFav = bRow.getData()._fav ? 1 : 0;
    if (aFav !== bFav) {
      // Favourites always on top regardless of sort direction
      return dir === 'asc' ? bFav - aFav : aFav - bFav;
    }
    if (typeof baseSorter === 'function') return baseSorter(a, b, aRow, bRow, column, dir, sorterParams);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (baseSorter === 'number') return Number(a) - Number(b);
    return String(a).localeCompare(String(b));
  };

  const colDef = (title, field, opts = {}) => {
    const def = { title, field, headerFilter: 'input', ...opts };
    def.sorter = favPinSorter(opts.sorter || 'string');
    if (savedWidths[field]) {
      def.width = savedWidths[field];
    }
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
      colDef('ID', 'id', { sorter: 'number', minWidth: 50, widthGrow: 0.5, headerFilterPlaceholder: 'id', cssClass: 'cell-scroll' }),
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
  nodesTabulator.on('columnResized', () => {
    saveSettings();
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

  nodesTabulator.updateOrAddData(data);

  // Remove rows that no longer exist
  const validIds = new Set(data.map((d) => d.id));
  const currentRows = nodesTabulator.getRows();
  for (const row of currentRows) {
    if (!validIds.has(row.getData().id)) {
      row.delete();
    }
  }

  // Highlight selected row
  for (const row of nodesTabulator.getRows()) {
    const rowEl = row.getElement();
    if (row.getData().id === state.selectedNodeId) {
      rowEl.classList.add('selected-row');
    } else {
      rowEl.classList.remove('selected-row');
    }
  }
};

const scheduleTableRefresh = () => {
  if (_tableRefreshPending) return;
  _tableRefreshPending = window.setTimeout(() => {
    _tableRefreshPending = null;
    renderNodesTable();
  }, 1000);
};
