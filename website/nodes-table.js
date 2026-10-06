// Tabulator-based node table: column definitions, formatters, build/refresh
// logic, and the favourite/hide actions. The Tabulator instance itself is
// stored on the global `nodesTabulator` (declared in state.js) so other
// files can read column widths/header filters when persisting settings.

// Forgets, for good, an offline node that lost its node-ID: asks first.
const deleteGhostNode = async (row) => {
  const label = `${row.name || 'this node'} (last node-ID ${row._lastNodeId ?? '-'})`;
  if (!window.confirm(`Forget ${label}? Cynitor drops what it remembers of this device.`)) return;
  try {
    await requestJson(`/api/identity/${row._uid}`, { method: 'DELETE' });
    if (state.selectedNodeId === `uid:${row._uid}`) clearSelectedNode();
  } catch (e) {
    showToast(`Could not forget ${label}: ${e.message}`, 'error');
  }
};

// IDs in a filter are whole: "10, 20" finds 10 or 20 in a cell's list, not 110 or 7510.
const idList = (text) => String(text ?? '').split(',').map((t) => t.trim()).filter(Boolean);
const idsHeaderFilter = (headerValue, rowValue) => {
  const terms = idList(headerValue);
  if (!terms.length) return true;
  const ids = idList(rowValue);
  return terms.some((t) => ids.includes(t));
};

// Health and state sort by how much they need a look, not alphabetically.
const HEALTH_ORDER = ['-', 'NOMINAL', 'ADVISORY', 'CAUTION', 'WARNING'];
const STATE_ORDER = ['active', 'idle', 'caution', 'warning', 'offline'];
const severitySorter = (order) => (a, b) => order.indexOf(a) - order.indexOf(b);

const stateFormatter = (cell) => {
  const v = cell.getValue();
  return `<span class="state-cell ${escapeHtml(v)}"><span class="state-dot ${escapeHtml(v)}"></span><span class="state-label">${escapeHtml(v)}</span></span>`;
};

// Uptime of a live node; for an offline one, since when it has been gone,
// worded so that it cannot be read as an uptime.
const uptimeFormatter = (cell) => {
  const text = escapeHtml(cell.getValue());
  return cell.getRow().getData()._offline ? `<span class="last-seen">${text}</span>` : text;
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

// A node's own ports first, the standard ones (fixed port-IDs) muted after
// them: those every node has say least about it. The first few, then how
// many more; the tooltip lists them all.
const portsFormatter = (kind) => (cell) => {
  const text = cell.getValue() || '-';
  if (text === '-') return '<span class="port-ids">-</span>';
  const html = shortIdList(text.split(', ').map(Number),
    (id) => (isFixedPortId(kind, id) ? `<span class="port-std">${id}</span>` : String(id)));
  return `<span class="port-ids has-ports" title="${escapeHtml(text)}">${html}</span>`;
};

// Pass these to Tabulator as pre-joined strings, not fresh arrays. Each
// /api/nodes response gives node.publishers a new array reference even
// when the contents didn't change, so Tabulator's strict-equality cell
// diff would treat the value as changed every time and re-render the
// cell. That re-render briefly destroys the cell's overflow state and
// makes the horizontal scrollbar blink. Strings compare by value.
const portsToString = (arr, kind) => {
  if (!Array.isArray(arr) || !arr.length) return '-';
  const own = arr.filter((id) => !isFixedPortId(kind, id)).sort((a, b) => a - b);
  const std = arr.filter((id) => isFixedPortId(kind, id)).sort((a, b) => a - b);
  return [...own, ...std].join(', ');
};

// A message rate; no messages (or an offline node) is no rate at all.
const rateFormatter = (cell) => {
  const v = Number(cell.getValue());
  if (!(v > 0)) return '<span class="text-muted">-</span>';
  if (v < 1) return '&lt;1 Hz';
  return `${escapeHtml(v.toFixed(1))} Hz`;
};

// Mode as the heartbeat names it; OPERATIONAL, the usual one, is muted.
const modeFormatter = (cell) => {
  const v = cell.getValue() || '-';
  const cls = v === 'OPERATIONAL' || v === '-' ? 'mode-usual' : 'mode-other';
  return `<span class="mode-text ${cls}">${escapeHtml(v)}</span>`;
};

// The heartbeat's vendor-specific status code; 0, the usual, is muted.
const vsscFormatter = (cell) => {
  const v = cell.getValue();
  if (v == null) return '<span class="text-muted">-</span>';
  return `<span class="${v === 0 ? 'text-muted' : ''}" title="0x${v.toString(16).toUpperCase()}">${v}</span>`;
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
  const editIcon = row._uid?.length
    ? '<span class="name-edit-icon" aria-hidden="true">✎</span>'
    : '';
  if (!alias && !original) {
    // Heartbeats arrive, but GetInfo (which carries the name) does not answer.
    const why = row._noInfo ? 'no name: GetInfo unanswered' : '-';
    return `<span class="name-cell">${editIcon}<span class="name-display name-missing">${why}</span></span>`;
  }
  const tooltip = alias && original ? ` title="${escapeHtml(original)}"` : '';
  return `<span class="name-cell">${editIcon}<span class="name-display"${tooltip}>${escapeHtml(alias || original)}</span></span>`;
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
  const rowEl = cell.getRow().getElement();  // where the keyboard goes back to
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); save(); rowEl.focus(); }
    if (e.key === 'Escape') { e.preventDefault(); discard(); rowEl.focus(); }
  });
  input.addEventListener('blur', discard);
};

const EYE_ICON = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

const actionsFormatter = (cell) => {
  const hide = `<button type="button" class="action-hide" aria-label="Hide node" title="Hide">${EYE_OFF_ICON}</button>`;
  if (!cell.getRow().getData()._ghost) return hide;
  // An offline node that lost its node-ID can also be forgotten for good.
  return `${hide}<button type="button" class="ghost-delete-btn" aria-label="Remove offline node" title="Remove">✕</button>`;
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

// What needs a look in the Nodes table, as the Graph's strip counts it, and
// nodes that send heartbeats but answer no request (GetInfo).
const NODE_FOCUS_KINDS = [
  { key: 'offline', label: 'offline', level: 'err', test: (r) => r._offline && !r._ghost },
  { key: 'displaced', label: 'displaced', level: 'err', test: (r) => r._ghost },
  { key: 'health', label: 'unusual health', level: 'warn', test: (r) => !r._offline && !!getStatusClass('health', r.health) },
  { key: 'mode', label: 'unusual mode', level: 'warn',
    test: (r) => !r._offline && r.mode !== '-' && !!getStatusClass('mode', r.mode) },
  { key: 'noinfo', label: 'not answering', level: 'warn', test: (r) => !r._offline && r._noInfo },
];

const setNodesFocus = (key) => {
  state.nodesFocus = key;
  const kind = NODE_FOCUS_KINDS.find((k) => k.key === key);
  if (kind) nodesTabulator.setFilter(kind.test);
  else nodesTabulator.clearFilter();
};

const renderNodesStatus = (rows) => {
  const strip = el('nodesStatus');
  if (!rows.length) {
    strip.replaceChildren();
    return;
  }
  const counts = renderStatusStrip(strip, `${rows.length} node${rows.length === 1 ? '' : 's'}`,
    NODE_FOCUS_KINDS, rows, state.nodesFocus);
  if (state.nodesFocus && !counts.some((k) => k.key === state.nodesFocus)) setNodesFocus(null);
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
    const alias = getNodeAlias(isGhost ? node.unique_id_hex : node.unique_id);
    const offline = isGhost || node.has_disappeared;
    const lastSeen = offline ? formatLastSeen(node.last_seen) : '-';
    const vssc = Number.parseInt(getNodeHeartbeatValue(node.node_id, 'vssc'), 10);
    rows.push({
      id: isGhost ? key : node.node_id,
      _sortId: isGhost ? (node.last_node_id ?? Infinity) : node.node_id,
      _fav: state.favouriteNodeIds.has(nodeStableKey(node)),
      _uid: isGhost ? node.unique_id_hex : node.unique_id,
      _ghost: isGhost,
      _lastNodeId: isGhost ? node.last_node_id : null,
      _offline: offline,
      _noInfo: !isGhost && !node.has_responded_to_getinfo,
      name: alias || node.name || '',
      state: nodeState,
      health: offline ? '-' : (getNodeHealthValue(node.node_id) || '-'),
      mode: offline ? '-' : (getNodeModeValue(node.node_id) || '-'),
      vssc: offline || Number.isNaN(vssc) ? null : vssc,
      sw: node.software_version ? `${node.software_version.major}.${node.software_version.minor}` : '-',
      rate: offline ? 0 : getNodeRate(node.node_id),
      uptime: offline ? (lastSeen !== '-' ? `last seen ${lastSeen}` : '-') : formatUptime(node.uptime),
      // Sorting: seconds up, then the offline nodes.
      _uptimeS: offline ? Infinity : Number(node.uptime ?? Infinity),
      publishers: portsToString(node.publishers, 'subject'),
      subscribers: portsToString(node.subscribers, 'subject'),
      servers: portsToString(node.servers, 'service'),
      clients: portsToString(node.clients, 'service'),
      _actions: nodeState,
    });
  }
  return rows;
};

// Selects a row's node, or lets it go if it is the one selected: a click, or Enter.
const toggleNodeRow = (row) => {
  const id = row.getData().id;
  if (id === state.selectedNodeId) clearSelectedNode();
  else setSelectedNode(id);
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
    // At narrow widths the least telling columns hide first (highest
    // `responsive`), instead of every column shrinking until none reads.
    responsiveLayout: 'hide',
    resizableColumns: true,
    selectable: 1,
    rowFormatter: focusableRow,
    keybindings: false,  // its Home/End move the focus off the rows; see bindRowKeys
    placeholder: tablePlaceholder(),
    initialSort,
    columns: [
      { title: '', field: '_fav', responsive: 0, formatter: favFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-fav', cellClick: (_e, cell) => { toggleFavourite(cell.getRow().getData().id); } },
      colDef('ID', '_sortId', { responsive: 0, sorter: 'number', minWidth: 50, widthGrow: 0.5, headerFilterPlaceholder: 'id', cssClass: 'cell-scroll', formatter: (cell) => {
        const row = cell.getRow().getData();
        if (!row._ghost) return escapeHtml(String(row.id));
        // An offline node that lost its node-ID: the one it last had.
        return `<span class="ghost-id" title="Last node-ID; the node is offline">${escapeHtml(String(row._lastNodeId ?? '-'))}</span>`;
      }, headerFilterFunc: (headerValue, _rowValue, rowData) =>
        idsHeaderFilter(headerValue, rowData._ghost ? rowData._lastNodeId : rowData.id) }),
      colDef('Name', 'name', { responsive: 0, sorter: 'string', minWidth: 120, widthGrow: 2, formatter: nameFormatter, headerFilterPlaceholder: 'name', cssClass: 'cell-scroll cell-name', cellDblClick: (_e, cell) => { startNameEdit(cell); } }),
      colDef('State', 'state', { responsive: 0, sorter: severitySorter(STATE_ORDER), minWidth: 90, widthGrow: 0.7, formatter: stateFormatter, headerFilterPlaceholder: 'state', cssClass: 'td-state' }),
      colDef('Health', 'health', { responsive: 1, sorter: severitySorter(HEALTH_ORDER), minWidth: 100, widthGrow: 0.8, formatter: healthFormatter, headerFilterPlaceholder: 'health', cssClass: 'cell-scroll' }),
      colDef('Mode', 'mode', { responsive: 5, sorter: 'string', minWidth: 110, widthGrow: 0.8, formatter: modeFormatter, headerFilterPlaceholder: 'mode', cssClass: 'cell-scroll' }),
      colDef('VSSC', 'vssc', { responsive: 6, sorter: 'number', minWidth: 64, widthGrow: 0.4, formatter: vsscFormatter, headerFilterPlaceholder: 'vssc', headerTooltip: 'Vendor-specific status code, from the heartbeat' }),
      colDef('SW', 'sw', { responsive: 6, sorter: 'string', minWidth: 56, widthGrow: 0.4, headerFilterPlaceholder: 'sw', cssClass: 'cell-scroll', headerTooltip: 'Software version' }),
      colDef('Rate', 'rate', { responsive: 2, sorter: 'number', minWidth: 70, widthGrow: 0.7, formatter: rateFormatter, headerFilterPlaceholder: 'rate', cssClass: 'cell-scroll', headerFilterFunc: (headerValue, rowValue) => { if (!headerValue) return true; return Number(rowValue).toFixed(1).includes(headerValue); } }),
      colDef('Uptime', 'uptime', { responsive: 3, sorter: (_a, _b, aRow, bRow) => { const a = aRow.getData()._uptimeS, b = bRow.getData()._uptimeS; return a === b ? 0 : a - b; }, minWidth: 110, widthGrow: 1, formatter: uptimeFormatter, headerFilterPlaceholder: 'uptime', cssClass: 'cell-scroll' }),
      colDef('Publishers', 'publishers', { responsive: 4, minWidth: 80, widthGrow: 1, formatter: portsFormatter('subject'), headerFilterPlaceholder: 'pub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Subscribers', 'subscribers', { responsive: 8, minWidth: 80, widthGrow: 1, formatter: portsFormatter('subject'), headerFilterPlaceholder: 'sub', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Servers', 'servers', { responsive: 7, minWidth: 80, widthGrow: 1, formatter: portsFormatter('service'), headerFilterPlaceholder: 'srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      colDef('Clients', 'clients', { responsive: 9, minWidth: 80, widthGrow: 1, formatter: portsFormatter('service'), headerFilterPlaceholder: 'clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      { title: '', field: '_actions', responsive: 0, formatter: actionsFormatter, width: 56, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', titleFormatter: () => { const btn = document.createElement('button'); btn.type = 'button'; btn.id = 'hiddenNodesChip'; btn.className = 'hidden-chip hidden'; btn.setAttribute('aria-label', 'Show hidden nodes'); btn.addEventListener('click', (e) => { e.stopPropagation(); toggleHiddenPopover(); }); return btn; }, cellClick: (e, cell) => {
        e.stopPropagation();
        const row = cell.getRow().getData();
        if (e.target.closest('.ghost-delete-btn')) {
          if (row._uid) deleteGhostNode(row);
          return;
        }
        hideNode(row.id);
      } },
    ],
  });

  nodesTabulator.on('rowClick', (_e, row) => {
    if (_e.target.closest('.name-input') || _e.target.closest('.action-hide')) return;
    toggleNodeRow(row);
  });
  bindRowKeys(nodesTabulator, toggleNodeRow, { F2: (row) => startNameEdit(row.getCell('name')) });
  nodesTabulator.on('dataSorted', (sorters) => {
    if (sorters.length > 0) {
      state.tableSort = { key: sorters[0].field, dir: sorters[0].dir };
      saveSettings();
    }
  });
  nodesTabulator.on('dataFiltered', () => {
    saveSettings();
  });

  el('nodesStatus').addEventListener('click', (e) => {
    const key = e.target.closest('[data-focus]')?.dataset.focus;
    if (!key) return;
    setNodesFocus(state.nodesFocus === key ? null : key);
    renderNodesTable();
  });

  nodesTabulator.on('tableBuilt', () => {
    _nodesTableReady = true;
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

  // Until built, there is nothing to render into; tableBuilt renders once ready.
  if (!nodesTabulator || !_nodesTableReady) return;

  const data = buildTableData();
  renderNodesStatus(data);

  if (!data.length) {
    nodesTabulator.clearData();
    const ph = document.querySelector('#nodesTable .tabulator-placeholder');
    if (ph) {
      ph.innerHTML = tablePlaceholder();
    }
    return;
  }

  resortChanged(nodesTabulator, diffUpdateTable(nodesTabulator, data, 'id'));

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
