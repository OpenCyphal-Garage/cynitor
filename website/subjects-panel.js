// Subjects view — shows all active subjects/services on the bus.
// Derives data from state.latestNodesPayload (port lists) and state.latestBySubject (telemetry).

let subjectsTabulator = null;
let _subjectsTableReady = false;
let _suppressReattach = false;
const _serviceTypeCache = new Map();

const _fmtDate = (unix) => {
  const d = new Date(unix * 1000);
  const dd = String(d.getDate()).padStart(2, '0');
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}.${mo}.${d.getFullYear()}`;
};

const subjectsPlaceholder = () => {
  return eventSourcePlaceholder('browse subjects and services')
    || svcStateMsg('<span class="svc-spinner"></span>', 'Waiting for traffic…', 'Listening on the CAN bus. Subjects and services will appear as nodes communicate.');
};

const _lookupServiceType = (serviceId) => {
  for (const schemas of state.serviceSchemas.values()) {
    if (!Array.isArray(schemas)) continue;
    for (const svc of schemas) {
      if (svc.service_id === serviceId && svc.full_type) {
        _serviceTypeCache.set(serviceId, svc.full_type);
        return svc.full_type;
      }
    }
  }
  return _serviceTypeCache.get(serviceId) || '-';
};

const _fetchMissingServiceSchemas = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes) return;
  for (const node of Object.values(nodes)) {
    if (node.has_disappeared) continue;
    if (!(node.servers?.length || node.clients?.length)) continue;
    if (state.serviceSchemas.has(node.node_id)) continue;
    fetchServiceSchema(node.node_id);
  }
};

const buildSubjectsRows = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes || typeof nodes !== 'object') return [];

  const subjectMap = new Map();
  const serviceMap = new Map();

  for (const node of Object.values(nodes)) {
    if (node.has_disappeared) continue;
    const nid = node.node_id;

    for (const sid of node.publishers || []) {
      if (!subjectMap.has(sid)) subjectMap.set(sid, { publishers: [], subscribers: [] });
      subjectMap.get(sid).publishers.push(nid);
    }
    for (const sid of node.subscribers || []) {
      if (!subjectMap.has(sid)) subjectMap.set(sid, { publishers: [], subscribers: [] });
      subjectMap.get(sid).subscribers.push(nid);
    }
    for (const sid of node.servers || []) {
      if (!serviceMap.has(sid)) serviceMap.set(sid, { servers: [], clients: [] });
      serviceMap.get(sid).servers.push(nid);
    }
    for (const sid of node.clients || []) {
      if (!serviceMap.has(sid)) serviceMap.set(sid, { servers: [], clients: [] });
      serviceMap.get(sid).clients.push(nid);
    }
  }

  const rows = [];

  for (const [sid, info] of subjectMap) {
    if (state.hiddenSubjectIds.has(sid)) continue;
    const event = state.latestBySubject.get(sid);
    const pubNode = event?.publisher_node_id != null ? `Node ${event.publisher_node_id}` : '';
    rows.push({
      _rowId: `sub:${sid}`,
      id: sid,
      kind: 'Subject',
      messageType: event?.message_type || '-',
      publishers: info.publishers.sort((a, b) => a - b).join(', '),
      subscribers: info.subscribers.sort((a, b) => a - b).join(', '),
      rate: event?.rate ?? 0,
      lastTime: event?.timestamp_unix ? `${formatPlotTime(event.timestamp_unix)} ${pubNode}` : '-',
      lastDate: event?.timestamp_unix ? _fmtDate(event.timestamp_unix) : '-',
      _fav: state.favouriteSubjectIds.has(sid),
    });
  }

  for (const [sid, info] of serviceMap) {
    if (state.hiddenSubjectIds.has(`svc:${sid}`)) continue;
    const lastCall = state.serviceCallHistory.find((h) => h.serviceId === sid);
    const lastTs = lastCall ? lastCall.timestamp / 1000 : 0;
    const calledNode = lastCall ? `Node ${lastCall.nodeId}` : '-';
    rows.push({
      _rowId: `svc:${sid}`,
      id: sid,
      kind: 'Service',
      messageType: _lookupServiceType(sid),
      publishers: info.servers.sort((a, b) => a - b).join(', '),
      subscribers: info.clients.sort((a, b) => a - b).join(', '),
      rate: 0,
      lastTime: lastTs ? `${formatPlotTime(lastTs)} ${calledNode}` : '-',
      lastDate: lastTs ? _fmtDate(lastTs) : '-',
      _fav: state.favouriteSubjectIds.has(`svc:${sid}`),
    });
  }

  return rows;
};

const _subjectKey = (row) => row.kind === 'Service' ? `svc:${row.id}` : row.id;

const subjectFavFormatter = (cell) => {
  const isFav = cell.getValue();
  return `<button type="button" class="fav-star ${isFav ? 'active' : ''}" aria-label="Toggle favourite">${isFav ? '★' : '☆'}</button>`;
};

const subjectActionsFormatter = () => {
  return `<button type="button" class="action-hide" aria-label="Hide subject">${EYE_ICON}</button>`;
};

const toggleSubjectFavourite = (row) => {
  const key = _subjectKey(row);
  if (state.favouriteSubjectIds.has(key)) {
    state.favouriteSubjectIds.delete(key);
  } else {
    state.favouriteSubjectIds.add(key);
  }
  saveSettings();
  refreshSubjectsTable();
  if (subjectsTabulator) {
    const sorters = subjectsTabulator.getSorters();
    if (sorters.length) {
      subjectsTabulator.setSort(sorters.map((s) => ({ column: s.field, dir: s.dir })));
    }
  }
};

const hideSubject = (row) => {
  const key = _subjectKey(row);
  state.hiddenSubjectIds.add(key);
  saveSettings();
  refreshSubjectsTable();
  updateHiddenSubjectsChip();
};

const unhideSubject = (key) => {
  state.hiddenSubjectIds.delete(key);
  saveSettings();
  refreshSubjectsTable();
  updateHiddenSubjectsChip();
};

const unhideAllSubjects = () => {
  state.hiddenSubjectIds.clear();
  saveSettings();
  refreshSubjectsTable();
  updateHiddenSubjectsChip();
};

const updateHiddenSubjectsChip = () => {
  const chip = el('hiddenSubjectsChip');
  if (!chip) return;
  const count = state.hiddenSubjectIds.size;
  if (count === 0) {
    chip.classList.add('hidden');
    const pop = el('hiddenSubjectsPopover');
    if (pop) pop.classList.add('hidden');
    return;
  }
  chip.classList.remove('hidden');
  chip.innerHTML = EYE_OFF_ICON + `<span>${count}</span>`;
};

const populateHiddenSubjectsPopover = () => {
  const popover = el('hiddenSubjectsPopover');
  const chip = el('hiddenSubjectsChip');
  if (!popover || !chip) return;

  if (state.hiddenSubjectIds.size === 0) {
    popover.classList.add('hidden');
    return;
  }

  positionPopover(popover, chip);

  let html = '<div class="hidden-popover-header"><span>Hidden subjects</span>'
    + '<button type="button" class="hidden-unhide-all" aria-label="Unhide all">Unhide all</button></div>'
    + '<div class="hidden-popover-list">';

  for (const key of state.hiddenSubjectIds) {
    const isSvc = String(key).startsWith('svc:');
    const id = isSvc ? String(key).slice(4) : key;
    const label = isSvc ? `Service ${id}` : `Subject ${id}`;
    html += `<div class="hidden-popover-row" data-subject-key="${escapeHtml(String(key))}">`
      + `<span class="hidden-popover-id">${escapeHtml(label)}</span>`
      + `<button type="button" class="hidden-unhide-btn" aria-label="Unhide ${escapeHtml(label)}">Unhide</button>`
      + `</div>`;
  }
  html += '</div>';
  popover.innerHTML = html;

  popover.querySelector('.hidden-unhide-all')?.addEventListener('click', (e) => {
    e.stopPropagation();
    unhideAllSubjects();
  });
  for (const btn of popover.querySelectorAll('.hidden-unhide-btn')) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const k = btn.closest('.hidden-popover-row').dataset.subjectKey;
      const parsed = k.startsWith('svc:') ? k : Number(k);
      unhideSubject(parsed);
      populateHiddenSubjectsPopover();
    });
  }
};

const toggleHiddenSubjectsPopover = () => {
  const popover = el('hiddenSubjectsPopover');
  if (!popover) return;
  if (state.hiddenSubjectIds.size === 0) {
    popover.classList.add('hidden');
    return;
  }
  popover.classList.toggle('hidden');
  if (!popover.classList.contains('hidden')) {
    populateHiddenSubjectsPopover();
  }
};

const subjectFavPinSorter = makeFavPinSorter();

const initSubjectsTable = () => {
  if (subjectsTabulator) return;

  const settings = readSettings();
  const savedSort = settings.subjectsTableSort;
  const initialSort = savedSort?.key
    ? [{ column: savedSort.key, dir: savedSort.dir }]
    : [{ column: 'id', dir: 'asc' }];

  const col = (title, field, opts = {}) => {
    const def = { title, field, headerFilter: 'input', ...opts };
    def.sorter = subjectFavPinSorter(opts.sorter || 'string');
    return def;
  };

  subjectsTabulator = new Tabulator('#subjectsTable', {
    data: buildSubjectsRows(),
    index: '_rowId',
    layout: 'fitColumns',
    placeholder: subjectsPlaceholder(),
    initialSort,
    columns: [
      { title: '', field: '_fav', formatter: subjectFavFormatter, width: 36, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-fav', cellClick: (_e, cell) => { toggleSubjectFavourite(cell.getRow().getData()); } },
      col('ID', 'id', { sorter: 'number', minWidth: 50, widthGrow: 0.4, headerFilterPlaceholder: 'id' }),
      col('Kind', 'kind', { minWidth: 60, widthGrow: 0.4, headerFilterPlaceholder: 'kind' }),
      col('Message / Service Type', 'messageType', { minWidth: 140, widthGrow: 3, headerFilterPlaceholder: 'type', cssClass: 'cell-scroll' }),
      col('Publishers / Servers', 'publishers', { minWidth: 80, widthGrow: 1, headerFilterPlaceholder: 'pub/srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      col('Subscribers / Clients', 'subscribers', { minWidth: 80, widthGrow: 1, headerFilterPlaceholder: 'sub/clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll' }),
      col('Rate', 'rate', { sorter: 'number', minWidth: 60, widthGrow: 0.4, headerFilterPlaceholder: 'rate', formatter: (cell) => { const v = cell.getValue(); if (v == null) return '<span class="text-muted">-</span>'; return v < 1 ? '&lt;1 Hz' : `${v.toFixed(1)} Hz`; } }),
      col('Last seen/called', 'lastTime', { minWidth: 100, widthGrow: 0.8, headerFilterPlaceholder: 'time' }),
      col('', 'lastDate', { minWidth: 75, widthGrow: 0.4, headerFilterPlaceholder: 'date' }),
      { title: '', field: '_actions', formatter: subjectActionsFormatter, width: 56, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', titleFormatter: () => { const btn = document.createElement('button'); btn.type = 'button'; btn.id = 'hiddenSubjectsChip'; btn.className = 'hidden-chip hidden'; btn.setAttribute('aria-label', 'Show hidden subjects'); btn.addEventListener('click', (e) => { e.stopPropagation(); toggleHiddenSubjectsPopover(); }); return btn; }, cellClick: (e, cell) => { e.stopPropagation(); hideSubject(cell.getRow().getData()); } },
    ],
  });

  subjectsTabulator.on('rowClick', (_e, row) => {
    if (_e.target.closest('.fav-star') || _e.target.closest('.action-hide')) return;
    const data = row.getData();
    if (data.kind === 'Service') {
      openInlineServiceDetail(data);
      return;
    }
    openSubjectPlot(data);
  });

  subjectsTabulator.on('dataSorted', (sorters) => {
    if (sorters.length > 0) {
      state.subjectsTableSort = { key: sorters[0].field, dir: sorters[0].dir };
      saveSettings();
    }
    _reattachInlineDetail();
  });

  subjectsTabulator.on('tableBuilt', () => {
    _subjectsTableReady = true;
    const savedFilters = settings.subjectsHeaderFilters || {};
    for (const [field, value] of Object.entries(savedFilters)) {
      if (value) subjectsTabulator.setHeaderFilterValue(field, value);
    }
    // Popover for hidden subjects
    if (!document.getElementById('hiddenSubjectsPopover')) {
      const popover = document.createElement('div');
      popover.id = 'hiddenSubjectsPopover';
      popover.className = 'hidden-popover hidden';
      document.body.appendChild(popover);
    }
    updateHiddenSubjectsChip();
  });

  subjectsTabulator.on('dataFiltered', () => {
    saveSettings();
    _reattachInlineDetail();
  });

  subjectsTabulator.on('renderStarted', () => {
    _stashInlineDetail();
  });

  subjectsTabulator.on('renderComplete', () => {
    if (!_suppressReattach) _reattachInlineDetail();
  });
};

const getSubjectsHeaderFilters = () => {
  if (!subjectsTabulator) return null;
  const filters = {};
  for (const col of subjectsTabulator.getColumns()) {
    const field = col.getField();
    const headerEl = col.getElement().querySelector('.tabulator-header-filter input');
    if (headerEl && headerEl.value) {
      filters[field] = headerEl.value;
    }
  }
  return filters;
};

const refreshSubjectsTable = () => {
  if (!subjectsTabulator || !_subjectsTableReady || state.activeView !== 'subjects') return;
  _fetchMissingServiceSchemas();
  const data = buildSubjectsRows();
  if (!data.length) {
    _removeInlineDetail();
    subjectsTabulator.clearData();
    const ph = document.querySelector('#subjectsTable .tabulator-placeholder');
    if (ph) ph.innerHTML = subjectsPlaceholder();
    return;
  }

  _suppressReattach = true;

  diffUpdateTable(subjectsTabulator, data, '_rowId');

  _suppressReattach = false;
  _unstashInlineDetail();
};

const _removeInlineDetail = () => {
  if (state._expandedSubjectRowId && subjectsTabulator) {
    const row = subjectsTabulator.getRow(state._expandedSubjectRowId);
    if (row) row.getElement().classList.remove('selected-row');
  }
  const existing = document.getElementById('subjectInlineDetail');
  if (existing) existing.remove();
  state._expandedSubjectRowId = null;
  state._stashedInlineDetail = null;
};

const _stashInlineDetail = () => {
  if (state._stashedInlineDetail) return;
  const detail = document.getElementById('subjectInlineDetail');
  if (detail) {
    detail.remove();
    state._stashedInlineDetail = detail;
  }
};

const _unstashInlineDetail = () => {
  const detail = state._stashedInlineDetail;
  if (!detail || !state._expandedSubjectRowId || !subjectsTabulator) return;
  state._stashedInlineDetail = null;
  const row = subjectsTabulator.getRow(state._expandedSubjectRowId);
  if (row) {
    const rowEl = row.getElement();
    rowEl.after(detail);
    rowEl.classList.add('selected-row');
  } else {
    state._expandedSubjectRowId = null;
  }
};

const _reattachInlineDetail = () => {
  if (_suppressReattach) return;
  if (!state._expandedSubjectRowId || !subjectsTabulator) return;
  const row = subjectsTabulator.getRow(state._expandedSubjectRowId);
  if (!row) {
    _removeInlineDetail();
    return;
  }
  let detail = document.getElementById('subjectInlineDetail');
  if (!detail && state._stashedInlineDetail) {
    detail = state._stashedInlineDetail;
    state._stashedInlineDetail = null;
  }
  if (!detail) {
    const rowData = row.getData();
    if (rowData.kind === 'Service') openInlineServiceDetail(rowData, true);
    return;
  }
  const rowEl = row.getElement();
  if (rowEl.nextElementSibling !== detail) {
    rowEl.after(detail);
  }
};

const openInlineServiceDetail = async (rowData, forceOpen = false) => {
  const rowId = rowData._rowId;
  if (!forceOpen && state._expandedSubjectRowId === rowId) {
    _removeInlineDetail();
    return;
  }
  _removeInlineDetail();

  state._expandedSubjectRowId = rowId;
  const serviceId = rowData.id;
  const serverNodes = rowData.publishers
    ? rowData.publishers.split(',').map((s) => Number(s.trim())).filter(Number.isFinite)
    : [];

  const row = subjectsTabulator.getRow(rowId);
  if (!row) return;
  const rowEl = row.getElement();
  rowEl.classList.add('selected-row');

  const detail = document.createElement('div');
  detail.id = 'subjectInlineDetail';
  detail.className = 'subject-inline-detail';
  rowEl.after(detail);

  if (!serverNodes.length) {
    detail.innerHTML = svcStateMsg('○', 'No server nodes', 'No nodes advertise this service.');
    return;
  }

  const header = document.createElement('div');
  header.className = 'svc-detail-header';
  detail.appendChild(header);

  // Node selector
  const targetNodeId = state._subjectServiceNodeId && serverNodes.includes(state._subjectServiceNodeId)
    ? state._subjectServiceNodeId
    : serverNodes[0];
  state._subjectServiceNodeId = targetNodeId;

  if (serverNodes.length > 1) {
    const selector = document.createElement('div');
    selector.className = 'svc-node-selector';
    selector.innerHTML = '<span class="svc-node-selector-label">Target node:</span>' +
      serverNodes.map((nid) =>
        `<button type="button" class="svc-node-btn${nid === targetNodeId ? ' active' : ''}" data-node-id="${nid}">${nid}</button>`
      ).join('');
    header.appendChild(selector);
    selector.querySelectorAll('.svc-node-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const currentDetail = document.getElementById('subjectInlineDetail');
        if (!currentDetail) return;
        state._subjectServiceNodeId = Number(btn.dataset.nodeId);
        state._subjectExpandedServiceId = serviceId;
        state._subjectServiceCallState = null;
        _renderInlineServiceForm(currentDetail, serviceId, state._subjectServiceNodeId, serverNodes);
      });
    });
  }

  const collapseBar = document.createElement('button');
  collapseBar.type = 'button';
  collapseBar.className = 'svc-collapse-bar';
  collapseBar.setAttribute('aria-label', 'Collapse service detail');
  collapseBar.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>';
  collapseBar.addEventListener('click', () => _removeInlineDetail());
  header.appendChild(collapseBar);

  state._subjectExpandedServiceId = serviceId;
  state._subjectServiceCallState = null;

  await _renderInlineServiceForm(detail, serviceId, targetNodeId, serverNodes);
};

const _renderInlineServiceForm = async (detail, serviceId, nodeId, serverNodes) => {
  let formContainer = detail.querySelector('.svc-inline-form');
  if (!formContainer) {
    formContainer = document.createElement('div');
    formContainer.className = 'svc-inline-form';
    detail.appendChild(formContainer);
  }

  if (!state.serviceSchemas.has(nodeId)) {
    formContainer.innerHTML = svcStateMsg('<span class="svc-spinner"></span>', `Loading schema from node ${nodeId}…`, '');
    await fetchServiceSchema(nodeId);
  }

  const schemas = state.serviceSchemas.get(nodeId);
  if (!Array.isArray(schemas)) {
    formContainer.innerHTML = svcStateMsg('○', 'DSDL not available', 'Service schema could not be loaded for this node.');
    return;
  }

  const svc = schemas.find((s) => s.service_id === serviceId);
  if (!svc) {
    formContainer.innerHTML = svcStateMsg('○', 'DSDL not available', `No schema found for service ${serviceId} on node ${nodeId}.`);
    return;
  }

  formContainer.innerHTML = `<section class="svc-panel">${renderServiceCard(svc, true)}</section>`;
  bindServiceCardEvents(formContainer, nodeId, true);
  _loadPersistentHistory(formContainer, nodeId);

  // Update node selector active state
  detail.querySelectorAll('.svc-node-btn').forEach((btn) => {
    btn.classList.toggle('active', Number(btn.dataset.nodeId) === nodeId);
  });
};


const renderSubjectServiceCard = (svc, nodeId) => {
  const detail = document.getElementById('subjectInlineDetail');
  if (!detail) return;
  let formContainer = detail.querySelector('.svc-inline-form');
  if (!formContainer) {
    formContainer = document.createElement('div');
    formContainer.className = 'svc-inline-form';
    detail.appendChild(formContainer);
  }
  formContainer.innerHTML = `<section class="svc-panel">${renderServiceCard(svc, true)}</section>`;
  bindServiceCardEvents(formContainer, nodeId, true);
  _loadPersistentHistory(formContainer, nodeId);
};

const openSubjectPlot = (rowData) => {
  const sid = rowData.id;
  const detailPanel = el('detailPanel');
  const detailHandle = el('detailResizeHandle');
  const content = el('selectedNodeContent');
  const tabs = detailPanel.querySelector('.detail-tabs');

  if (state.selectedPlotSubject === sid) {
    state.selectedPlotSubject = null;
    state._subjectsPlotSubject = null;
    stopPlotAnim();
    _saveDetailPanelState('subjects');
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    tabs.classList.remove('hidden');
    _clearSubjectRowSelection();
    return;
  }

  state.plotPaused = false;
  state.plotPausedAt = null;
  state.selectedPlotSubject = sid;
  state._subjectsPlotSubject = sid;
  tabs.classList.add('hidden');
  detailHandle.classList.remove('hidden');
  detailPanel.classList.remove('hidden');
  _restoreDetailPanelState('subjects');

  const event = state.latestBySubject.get(sid);
  const typeName = event?.message_type || `Subject ${sid}`;
  content.innerHTML = `<div class="detail-split">
    <div class="detail-plot-area"></div>
  </div>`;

  _highlightSubjectRow(sid);
  startPlotAnim();
};

const _highlightSubjectRow = (sid) => {
  if (!subjectsTabulator) return;
  const keepId = state._expandedSubjectRowId;
  for (const row of subjectsTabulator.getRows()) {
    const d = row.getData();
    if (keepId && d._rowId === keepId) continue;
    const isSel = d.kind === 'Subject' && d.id === sid;
    row.getElement().classList.toggle('selected-row', isSel);
  }
};

const _clearSubjectRowSelection = () => {
  if (!subjectsTabulator) return;
  const keepId = state._expandedSubjectRowId;
  for (const row of subjectsTabulator.getRows()) {
    if (keepId && row.getData()._rowId === keepId) continue;
    row.getElement().classList.remove('selected-row');
  }
};

const _saveDetailPanelState = (viewKey) => {
  const detailPanel = el('detailPanel');
  const h = detailPanel.getBoundingClientRect().height;
  if (viewKey === 'nodes') {
    state._nodesDetailHeight = state.detailPanelCollapsed ? state._nodesDetailHeight : h;
    state._nodesDetailCollapsed = state.detailPanelCollapsed;
  } else {
    state._subjectsDetailHeight = state.detailPanelCollapsed ? state._subjectsDetailHeight : h;
    state._subjectsDetailCollapsed = state.detailPanelCollapsed;
  }
};

const _restoreDetailPanelState = (viewKey) => {
  const detailPanel = el('detailPanel');
  detailPanel.classList.add('no-transition');
  const height = viewKey === 'nodes' ? state._nodesDetailHeight : state._subjectsDetailHeight;
  const collapsed = viewKey === 'nodes' ? state._nodesDetailCollapsed : state._subjectsDetailCollapsed;

  state.detailPanelCollapsed = collapsed;
  if (collapsed) {
    detailPanel.classList.add('collapsed');
    detailPanel.style.height = '';
  } else {
    detailPanel.classList.remove('collapsed');
    detailPanel.style.height = height ? height + 'px' : '';
  }
  state.detailPanelHeight = height;
  const collapseBtn = el('detailCollapseBtn');
  if (collapseBtn) collapseBtn.classList.toggle('pointing-up', collapsed);
  detailPanel.offsetHeight;
  detailPanel.classList.remove('no-transition');
};

const switchView = (view) => {
  if (state.activeView === view) return;
  const prevView = state.activeView;
  state.activeView = view;

  const nodesEl = el('nodesTable');
  const subjectsEl = el('subjectsTable');
  const graphEl = el('graphContainer');
  const compareEl = el('compareContainer');
  const dsdlEl = el('dsdlContainer');
  const recordEl = el('recordContainer');
  const debugEl = el('debugContainer');
  const detailHandle = el('detailResizeHandle');
  const detailPanel = el('detailPanel');

  _saveDetailPanelState(prevView);

  // Tear down previous view
  if (prevView === 'subjects') {
    state._subjectsPlotSubject = state.selectedPlotSubject;
    stopPlotAnim();
    const inlineDetail = document.getElementById('subjectInlineDetail');
    if (inlineDetail) {
      state._stashedInlineDetail = inlineDetail;
      inlineDetail.remove();
    }
    _suppressReattach = true;
  } else if (prevView === 'nodes') {
    state._nodesPlotSubject = state.selectedPlotSubject;
  } else if (prevView === 'compare') {
    stopCompareAnim();
  } else if (prevView === 'graph') {
    GraphView.hide();
  } else if (prevView === 'dsdl') {
    DsdlView.hide();
  } else if (prevView === 'record') {
    setRecordViewActive(false);
  } else if (prevView === 'debug') {
    DebugView.hide();
  }

  // Hide all content panes
  nodesEl.classList.add('hidden');
  subjectsEl.classList.add('hidden');
  graphEl.classList.add('hidden');
  compareEl.classList.add('hidden');
  dsdlEl.classList.add('hidden');
  recordEl.classList.add('hidden');
  debugEl.classList.add('hidden');

  // Activate target view
  if (view === 'subjects') {
    _suppressReattach = false;
    state.selectedPlotSubject = state._subjectsPlotSubject ?? null;
    subjectsEl.classList.remove('hidden');
    stopPlotAnim();
    const hasPlot = state.selectedPlotSubject != null;
    detailHandle.classList.toggle('hidden', !hasPlot);
    detailPanel.classList.toggle('hidden', !hasPlot);
    const tabs = detailPanel.querySelector('.detail-tabs');
    if (tabs) tabs.classList.toggle('hidden', hasPlot);
    if (hasPlot) _restoreDetailPanelState('subjects');
    initSubjectsTable();
    refreshSubjectsTable();
    if (hasPlot) {
      const content = el('selectedNodeContent');
      content.innerHTML = `<div class="detail-split">
        <div class="detail-plot-area"></div>
      </div>`;
      _highlightSubjectRow(state.selectedPlotSubject);
      startPlotAnim();
    }
  } else if (view === 'compare') {
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    compareEl.classList.remove('hidden');
    initCompareView();
    startCompareAnim();
  } else if (view === 'graph') {
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    GraphView.show();
  } else if (view === 'dsdl') {
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    dsdlEl.classList.remove('hidden');
    DsdlView.init();
  } else if (view === 'record') {
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    recordEl.classList.remove('hidden');
    initRecordView();
    setRecordViewActive(true);
  } else if (view === 'debug') {
    detailHandle.classList.add('hidden');
    detailPanel.classList.add('hidden');
    debugEl.classList.remove('hidden');
    DebugView.init();
  } else {
    state.selectedPlotSubject = state._nodesPlotSubject ?? null;
    stopPlotAnim();
    nodesEl.classList.remove('hidden');
    detailHandle.classList.remove('hidden');
    detailPanel.classList.remove('hidden');
    const tabs = detailPanel.querySelector('.detail-tabs');
    if (tabs) tabs.classList.remove('hidden');
    _restoreDetailPanelState('nodes');
    const content = el('selectedNodeContent');
    delete content.dataset.svcTab;
    delete content.dataset.svcNodeId;
    delete content.dataset.nodeOffline;
    renderSelectedNodeContent();
  }

  document.querySelectorAll('[data-view]').forEach((btn) => {
    const isActive = btn.dataset.view === view;
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-selected', isActive);
  });

  saveSettings();
};
