// Tabulator-based node table: column definitions, formatters, build/refresh
// logic, and the favourite/delete actions. The Tabulator instance itself is
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
  return `<span class="health-text health-${escapeHtml(cls)}">${escapeHtml(v)}</span>`;
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

const actionsFormatter = (cell) => {
  const row = cell.getRow().getData();
  const offline = row.state === 'offline';
  const cls = offline ? 'enabled' : 'disabled';
  const disabledAttr = offline ? '' : ' disabled';
  return `<button type="button" class="action-delete ${cls}" aria-label="Remove offline node"${disabledAttr}>✕</button>`;
};

const toggleFavourite = (nodeId) => {
  if (state.favouriteNodeIds.has(nodeId)) {
    state.favouriteNodeIds.delete(nodeId);
  } else {
    state.favouriteNodeIds.add(nodeId);
  }
  saveSettings();
  renderNodesTable();
};

const deleteOfflineNode = (nodeId) => {
  state.deletedNodeIds.add(nodeId);
  state.latestByNode.delete(nodeId);
  saveSettings();
  renderNodesTable();
};

const buildTableData = () => {
  const payloadNodes = state.latestNodesPayload?.nodes;
  const allNodes = payloadNodes && typeof payloadNodes === 'object' ? Object.values(payloadNodes) : [];
  // If a deleted node comes back online, restore it
  for (const node of allNodes) {
    if (state.deletedNodeIds.has(node.node_id) && getNodeVisualState(node) !== 'offline') {
      state.deletedNodeIds.delete(node.node_id);
    }
  }

  const rows = allNodes
    .filter((node) => !state.deletedNodeIds.has(node.node_id))
    .map((node) => {
      const nodeState = getNodeVisualState(node);
      return {
        id: node.node_id,
        _fav: state.favouriteNodeIds.has(node.node_id),
        name: node.name || '-',
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
    placeholder: 'Not connected',
    initialSort,
    columns: [
      { title: '', field: '_fav', formatter: favFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-fav', cellClick: (_e, cell) => { toggleFavourite(cell.getRow().getData().id); } },
      colDef('ID', 'id', { sorter: 'number', minWidth: 50, widthGrow: 0.5, headerFilterPlaceholder: 'id', cssClass: 'cell-scroll' }),
      colDef('Name', 'name', { sorter: 'string', minWidth: 100, widthGrow: 2, headerFilterPlaceholder: 'name', cssClass: 'cell-scroll' }),
      colDef('State', 'state', { sorter: 'string', minWidth: 40, widthGrow: 0.7, formatter: stateFormatter, headerFilterPlaceholder: 'state', cssClass: 'td-state' }),
      colDef('Health', 'health', { sorter: 'string', minWidth: 70, widthGrow: 0.8, formatter: healthFormatter, headerFilterPlaceholder: 'health', cssClass: 'cell-scroll' }),
      colDef('Rate', 'rate', { sorter: 'number', minWidth: 70, widthGrow: 0.7, formatter: rateFormatter, headerFilterPlaceholder: 'rate', cssClass: 'cell-scroll', headerFilterFunc: (headerValue, rowValue) => { if (!headerValue) return true; return Number(rowValue).toFixed(1).includes(headerValue); } }),
      colDef('Uptime', 'uptime', { sorter: 'string', minWidth: 100, widthGrow: 1, headerFilterPlaceholder: 'uptime', cssClass: 'cell-scroll' }),
      colDef('Publishers', 'publishers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'pub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Subscribers', 'subscribers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'sub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Servers', 'servers', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Clients', 'clients', { minWidth: 80, widthGrow: 1, formatter: portsFormatter, headerFilterPlaceholder: 'clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      { title: '', field: '_actions', formatter: actionsFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', cellClick: (_e, cell) => { const d = cell.getRow().getData(); if (d.state === 'offline') deleteOfflineNode(d.id); } },
    ],
  });

  nodesTabulator.on('rowClick', (_e, row) => {
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
    renderNodesTable();
  });
};

const renderNodesTable = () => {
  const countEl = el('nodeCount');
  const data = buildTableData();

  if (countEl) {
    countEl.textContent = String(data.length);
  }

  if (!nodesTabulator) return;

  if (!data.length) {
    nodesTabulator.clearData();
    nodesTabulator.options.placeholder = state.dashboardConnected ? 'No nodes discovered yet' : 'Not connected';
    nodesTabulator.redraw(true);
    return;
  }

  nodesTabulator.updateOrAddData(data);

  // Re-apply current sort so favourite pin-order updates immediately
  const sorters = nodesTabulator.getSorters();
  if (sorters.length) {
    nodesTabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
  }

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
