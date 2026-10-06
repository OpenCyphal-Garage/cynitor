// Subjects view — shows all active subjects/services on the bus.
// Derives data from state.latestNodesPayload (port lists) and state.latestBySubject (telemetry).

let subjectsTabulator = null;
let _subjectsTableReady = false;
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

const subjectTypeFormatter = (cell) => {
  const row = cell.getRow().getData();
  if (row._untyped) {
    return '<span class="type-unknown">type unknown</span><span class="type-set-hint">click to set</span>';
  }
  const setByUser = row._userType ? '<span class="type-user">set by you</span>' : '';
  return `${escapeHtml(cell.getValue())}${setByUser}`;
};

const kindFormatter = (cell) =>
  `<span class="kind-badge kind-${cell.getValue() === 'Service' ? 'service' : 'subject'}">${escapeHtml(cell.getValue())}</span>`;

const subjectRateFormatter = (cell) => {
  if (cell.getRow().getData()._silent) return '<span class="status-warn">silent</span>';
  const v = Number(cell.getValue());
  if (!(v > 0)) return '<span class="text-muted">-</span>';
  return v < 1 ? '&lt;1 Hz' : `${v.toFixed(1)} Hz`;
};

// When, and from whom: the time only, today; the date too, before.
const lastSeenFormatter = (cell) => {
  const t = cell.getValue();
  if (!t) return '<span class="text-muted">-</span>';
  const row = cell.getRow().getData();
  const today = new Date().toDateString() === new Date(t * 1000).toDateString();
  const when = today ? formatPlotTime(t) : `${_fmtDate(t)} ${formatPlotTime(t)}`;
  const who = row._lastNode != null ? `node ${row._lastNode} ${nodeDisplayName(row._lastNode)}`.trim() : '';
  const title = `${_fmtDate(t)} ${formatPlotTime(t)}${who ? `, ${who}` : ''}`;
  return `<span title="${escapeHtml(title)}">${escapeHtml(when)}</span>`;
};

const nodeIdsFormatter = (cell) => {
  const text = cell.getValue() || '-';
  return text === '-' ? '<span class="text-muted">-</span>'
    : `<span title="${escapeHtml(nodeIdsTitle(text))}">${escapeHtml(text)}</span>`;
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
  const now = Date.now();

  for (const [sid, info] of subjectMap) {
    if (state.hiddenSubjectIds.has(sid)) continue;
    const event = state.latestBySubject.get(sid);
    const fresh = isEventFresh(event, now);
    rows.push({
      _rowId: `sub:${sid}`,
      id: sid,
      kind: 'Subject',
      messageType: subjectTypeName(sid, event),
      _untyped: isUntypedSubject(sid),
      _userType: subjectTypeSource(sid) === 'user',
      publishers: info.publishers.sort((a, b) => a - b).join(', ') || '-',
      subscribers: info.subscribers.sort((a, b) => a - b).join(', ') || '-',
      // A row redraws a cell only when its value changes: none, not 0, once silent.
      rate: fresh ? getSubjectRate(event) : null,
      _silent: Boolean(event) && !fresh,  // it published, and has stopped
      lastTime: event?.timestamp_unix || null,
      _lastNode: event?.publisher_node_id ?? null,
      _fav: state.favouriteSubjectIds.has(sid),
    });
  }

  for (const [sid, info] of serviceMap) {
    if (state.hiddenSubjectIds.has(`svc:${sid}`)) continue;
    const lastCall = state.serviceCallHistory.find((h) => h.serviceId === sid);
    const lookedUp = _lookupServiceType(sid);
    rows.push({
      _rowId: `svc:${sid}`,
      id: sid,
      kind: 'Service',
      messageType: lookedUp !== '-' ? dsdlTypeName(lookedUp) : STANDARD_SERVICE_TYPES[sid] || '-',
      publishers: info.servers.sort((a, b) => a - b).join(', ') || '-',
      subscribers: info.clients.sort((a, b) => a - b).join(', ') || '-',
      rate: null,  // a service has calls, not a message rate
      lastTime: lastCall ? lastCall.timestamp / 1000 : null,
      _lastNode: lastCall ? lastCall.nodeId : null,
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
  return `<button type="button" class="action-hide" aria-label="Hide subject" title="Hide">${EYE_OFF_ICON}</button>`;
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
      col('ID', 'id', { sorter: 'number', minWidth: 50, widthGrow: 0.4, headerFilterPlaceholder: 'id', headerFilterFunc: idsHeaderFilter }),
      col('Kind', 'kind', { minWidth: 88, widthGrow: 0.3, headerFilterPlaceholder: 'kind', formatter: kindFormatter }),
      col('Message / Service Type', 'messageType', { minWidth: 160, widthGrow: 2.2, headerFilterPlaceholder: 'type', cssClass: 'cell-scroll', formatter: subjectTypeFormatter }),
      col('Publishers / Servers', 'publishers', { minWidth: 90, widthGrow: 1, headerFilterPlaceholder: 'pub/srv', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll', formatter: nodeIdsFormatter }),
      col('Subscribers / Clients', 'subscribers', { minWidth: 90, widthGrow: 1, headerFilterPlaceholder: 'sub/clt', headerFilterFunc: idsHeaderFilter, cssClass: 'cell-scroll', formatter: nodeIdsFormatter }),
      col('Rate', 'rate', { sorter: 'number', minWidth: 90, widthGrow: 0.4, headerFilterPlaceholder: 'rate', formatter: subjectRateFormatter }),
      col('Last seen', 'lastTime', { sorter: 'number', minWidth: 90, widthGrow: 0.6, headerFilter: false, formatter: lastSeenFormatter, headerTooltip: 'Last message, or last call from this dashboard' }),
      { title: '', field: '_actions', formatter: subjectActionsFormatter, width: 56, resizable: false, headerSort: false, headerFilter: false, hozAlign: 'center', cssClass: 'cell-actions', titleFormatter: () => { const btn = document.createElement('button'); btn.type = 'button'; btn.id = 'hiddenSubjectsChip'; btn.className = 'hidden-chip hidden'; btn.setAttribute('aria-label', 'Show hidden subjects'); btn.addEventListener('click', (e) => { e.stopPropagation(); toggleHiddenSubjectsPopover(); }); return btn; }, cellClick: (e, cell) => { e.stopPropagation(); hideSubject(cell.getRow().getData()); } },
    ],
  });

  subjectsTabulator.on('rowClick', (_e, row) => {
    if (_e.target.closest('.fav-star') || _e.target.closest('.action-hide')) return;
    const data = row.getData();
    if (data.kind === 'Service') {
      openSubjectService(data);
      return;
    }
    openSubjectPlot(data);
  });

  subjectsTabulator.on('dataSorted', (sorters) => {
    if (sorters.length > 0) {
      state.subjectsTableSort = { key: sorters[0].field, dir: sorters[0].dir };
      saveSettings();
    }
  });

  subjectsTabulator.on('tableBuilt', () => {
    _subjectsTableReady = true;
    const savedFilters = settings.subjectsHeaderFilters || {};
    const fields = new Set(subjectsTabulator.getColumns().map((c) => c.getField()));
    for (const [field, value] of Object.entries(savedFilters)) {
      if (value && fields.has(field)) subjectsTabulator.setHeaderFilterValue(field, value);  // old settings may name gone columns
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
  });

  // Rows re-render on sorting, filtering and refreshes: mark the open one again.
  subjectsTabulator.on('renderComplete', () => _highlightSubjectRow());
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
    closeSubjectsDetail();
    subjectsTabulator.clearData();
    const ph = document.querySelector('#subjectsTable .tabulator-placeholder');
    if (ph) ph.innerHTML = subjectsPlaceholder();
    return;
  }

  resortChanged(subjectsTabulator, diffUpdateTable(subjectsTabulator, data, '_rowId'));
  _highlightSubjectRow();
};

// Below the Subjects table, the detail panel holds a subject's plot and a
// service's call card, each on its own: side by side when both are open,
// the whole width for one. Rows never move.
const _detailSlots = () => {
  const content = el('selectedNodeContent');
  let root = content.querySelector(':scope > .subjects-detail');
  if (!root) {
    content.innerHTML = '<div class="subjects-detail"><div class="subjects-detail-plot"></div>'
      + '<div class="subjects-detail-service"></div></div>';
    root = content.firstElementChild;
  }
  return { root, plot: root.children[0], service: root.children[1] };
};

// Shows or hides the panel and lays out the slots for what is open.
const _updateSubjectsDetail = () => {
  const hasPlot = state.selectedPlotSubject != null;
  const hasService = state._subjectsServiceRowId != null;
  const detailPanel = el('detailPanel');
  const shown = !detailPanel.classList.contains('hidden');
  if (hasPlot || hasService) {
    const { root } = _detailSlots();
    root.classList.toggle('has-plot', hasPlot);
    root.classList.toggle('has-service', hasService);
    if (!shown) {
      detailPanel.querySelector('.detail-tabs').classList.add('hidden');
      el('detailResizeHandle').classList.remove('hidden');
      detailPanel.classList.remove('hidden');
      _restoreDetailPanelState('subjects');
    }
  } else if (shown) {
    _saveDetailPanelState('subjects');
    el('detailResizeHandle').classList.add('hidden');
    detailPanel.classList.add('hidden');
    detailPanel.querySelector('.detail-tabs').classList.remove('hidden');
  }
  _highlightSubjectRow();
};

const closeSubjectPlot = () => {
  state.selectedPlotSubject = null;
  state._subjectsPlotSubject = null;
  stopPlotAnim();
  if (!el('detailPanel').classList.contains('hidden')) _detailSlots().plot.replaceChildren();
  _updateSubjectsDetail();
};

const closeSubjectService = () => {
  state._subjectsServiceRowId = null;
  state._stashedServiceCard = null;
  if (!el('detailPanel').classList.contains('hidden')) _detailSlots().service.replaceChildren();
  _updateSubjectsDetail();
};

const closeSubjectsDetail = () => {
  closeSubjectPlot();
  closeSubjectService();
};

const openSubjectService = (rowData) => {
  const rowId = rowData._rowId;
  if (state._subjectsServiceRowId === rowId) {
    closeSubjectService();
    return;
  }
  state._subjectsServiceRowId = rowId;
  _detailSlots().service.replaceChildren(_buildServiceCard(rowData));
  _updateSubjectsDetail();
};

// The call card: which service, which node serves it, the request form.
const _buildServiceCard = (rowData) => {
  const serviceId = rowData.id;
  const serverNodes = rowData.publishers
    ? rowData.publishers.split(',').map((s) => Number(s.trim())).filter(Number.isFinite)
    : [];
  const card = document.createElement('div');
  card.className = 'subject-service-card';
  card.innerHTML = `
    <div class="subject-service-head">
      <span class="subject-service-title"><span class="subject-service-id">${serviceId}</span>
        ${escapeHtml(rowData.messageType || `Service ${serviceId}`)}</span>
      <button type="button" class="subject-service-close" aria-label="Close service ${serviceId}" title="Close">✕</button>
    </div>`;
  card.querySelector('.subject-service-close').addEventListener('click', closeSubjectService);
  if (!serverNodes.length) {
    card.insertAdjacentHTML('beforeend', svcStateMsg('○', 'No server nodes', 'No nodes advertise this service.'));
    return card;
  }

  const targetNodeId = state._subjectServiceNodeId && serverNodes.includes(state._subjectServiceNodeId)
    ? state._subjectServiceNodeId
    : serverNodes[0];
  state._subjectServiceNodeId = targetNodeId;
  if (serverNodes.length > 1) {
    const selector = document.createElement('div');
    selector.className = 'svc-node-selector';
    selector.innerHTML = '<span class="svc-node-selector-label">Target node:</span>' +
      serverNodes.map((nid) =>
        `<button type="button" class="svc-node-btn${nid === targetNodeId ? ' active' : ''}" data-node-id="${nid}"
          title="${escapeHtml(nodeIdsTitle(String(nid)))}">${nid}</button>`
      ).join('');
    card.appendChild(selector);
    selector.querySelectorAll('.svc-node-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        state._subjectServiceNodeId = Number(btn.dataset.nodeId);
        state._subjectExpandedServiceId = serviceId;
        state._subjectServiceCallState = null;
        _renderInlineServiceForm(card, serviceId, state._subjectServiceNodeId, serverNodes);
      });
    });
  }
  state._subjectExpandedServiceId = serviceId;
  state._subjectServiceCallState = null;
  _renderInlineServiceForm(card, serviceId, targetNodeId, serverNodes);
  return card;
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
  const detail = document.querySelector('#selectedNodeContent .subject-service-card');
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
  if (state.selectedPlotSubject === sid) {
    closeSubjectPlot();
    return;
  }

  state.plotPaused = false;
  state.plotPausedAt = null;
  state.selectedPlotSubject = sid;
  state._subjectsPlotSubject = sid;
  _updateSubjectsDetail();
  _renderSubjectPlotSlot(sid);
};

const _renderSubjectPlotSlot = (sid) => {
  const slot = _detailSlots().plot;
  if (isUntypedSubject(sid)) {
    openSubjectTypePanel(sid);
    return;
  }
  const typeBar = subjectTypeSource(sid) === 'user' ? renderUserTypeBar(sid) : '';
  slot.innerHTML = `${typeBar}<div class="detail-split">
    <div class="detail-plot-area"></div>
  </div>`;
  slot.querySelector('[data-type-change]')?.addEventListener('click', () => openSubjectTypePanel(sid));
  slot.querySelector('[data-type-clear]')?.addEventListener('click', () => clearSubjectType(sid));
  startPlotAnim();
};

// ── Types for subjects no register names (see WEBSOCKET_README "Subject types") ──

const GUESS_LISTEN_S = 3;

const renderUserTypeBar = (sid) => {
  const type = state.latestNodesPayload?.subject_types?.[sid]?.type || '';
  return `<div class="subject-type-bar">
    <span>Decoded as <code>${escapeHtml(type)}</code>, set by you</span>
    <button type="button" class="svc-btn" data-type-change>Change</button>
    <button type="button" class="svc-btn" data-type-clear>Clear</button>
  </div>`;
};

// The compiled message types, for the type field's suggestions.
const fetchMessageTypeNames = async () => {
  const data = await requestJson('/api/dsdl/namespaces');
  const names = [];
  const walk = (node) => {
    for (const t of node.types || []) {
      if (t.kind === 'message' && t.compiled) names.push(t.full_name);
    }
    Object.values(node.children || {}).forEach(walk);
  };
  Object.values(data.namespaces || {}).forEach(walk);
  return names.sort();
};

const openSubjectTypePanel = (sid) => {
  stopPlotAnim();
  const content = _detailSlots().plot;
  content.innerHTML = `<div class="subject-type-panel">
    <h3 class="subject-type-title">Subject ${sid}: which type is it?</h3>
    <p class="subject-type-hint">No publisher names its type in registers, so Cynitor cannot decode it
      on its own. Choose from the types its messages fit, or enter one.</p>
    <form class="subject-type-form" data-subject-type-form>
      <input type="text" list="subjectTypeNames" data-subject-type-input autocomplete="off" required
             aria-label="DSDL type" placeholder="e.g. uavcan.primitive.scalar.Real32.1.0">
      <datalist id="subjectTypeNames"></datalist>
      <button type="submit" class="svc-btn">Decode</button>
    </form>
    <div class="subject-type-guess" data-subject-type-guess aria-live="polite"></div>
  </div>`;
  const input = content.querySelector('[data-subject-type-input]');
  content.querySelector('[data-subject-type-form]').addEventListener('submit', (e) => {
    e.preventDefault();
    applySubjectType(sid, input.value.trim());
  });
  fetchMessageTypeNames().then((names) => {
    content.querySelector('#subjectTypeNames').innerHTML =
      names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join('');
  }).catch(() => {});  // suggestions only
  guessSubjectType(sid, content.querySelector('[data-subject-type-guess]'));
};

const renderTypeCandidates = (data) => `
  <div class="subject-type-guess-head">
    <span>${data.matches} type${data.matches === 1 ? '' : 's'} fit the ${data.samples} messages heard${
      data.matches > data.candidates.length ? `; the best ${data.candidates.length} are shown` : ''}.
      Each preview is the latest message decoded as that type.</span>
    <input type="search" data-guess-filter aria-label="Filter types" placeholder="filter">
  </div>
  <ul class="subject-type-candidates">${data.candidates.map((c) => `
    <li class="subject-type-candidate" data-type="${escapeHtml(c.type.toLowerCase())}">
      <span class="subject-type-name">${escapeHtml(c.type)}${
        c.custom ? ' <span class="subject-type-badge">custom</span>' : ''}</span>
      <code class="subject-type-preview" title="${escapeHtml(JSON.stringify(c.preview))}">${
        escapeHtml(JSON.stringify(c.preview))}</code>
      <button type="button" class="svc-btn" data-use-type="${escapeHtml(c.type)}">Use</button>
    </li>`).join('')}
  </ul>`;

const guessSubjectType = async (sid, box) => {
  box.textContent = `Listening to subject ${sid} for ${GUESS_LISTEN_S} s…`;
  let data;
  try {
    data = await requestJson(`/api/subjects/${sid}/type-guesses`);
  } catch (e) {
    data = { error: e?.data?.error || e.message };
  }
  if (!box.isConnected) return;
  const again = '<button type="button" class="svc-btn" data-guess-again>Listen again</button>';
  if (data.error) {
    box.innerHTML = `<p>Could not listen: ${escapeHtml(data.error)}</p>${again}`;
  } else if (!data.samples) {
    box.innerHTML = `<p>Nothing was published on subject ${sid} in ${GUESS_LISTEN_S} s.</p>${again}`;
  } else if (!data.candidates.length) {
    box.innerHTML = `<p>${data.samples} messages heard, and no compiled type fits them exactly. If it is
      your own type, add its DSDL in the DSDL view, compile it, and listen again.</p>${again}`;
  } else {
    box.innerHTML = renderTypeCandidates(data);
    box.querySelector('[data-guess-filter]').addEventListener('input', (e) => {
      const text = e.target.value.trim().toLowerCase();
      box.querySelectorAll('.subject-type-candidate').forEach((li) => {
        li.classList.toggle('hidden', !li.dataset.type.includes(text));
      });
    });
    box.querySelectorAll('[data-use-type]').forEach((btn) => {
      btn.addEventListener('click', () => applySubjectType(sid, btn.dataset.useType));
    });
  }
  box.querySelector('[data-guess-again]')?.addEventListener('click', () => guessSubjectType(sid, box));
};

// Reopens the subject once its type changed: its plot, or the type panel.
const reopenSubject = (sid) => {
  _renderSubjectPlotSlot(sid);
  _highlightSubjectRow();
};

const applySubjectType = async (sid, type) => {
  if (!type) return;
  try {
    await requestJson(`/api/subjects/${sid}/type`, { method: 'PUT', body: JSON.stringify({ type }) });
  } catch (e) {
    showToast(`Subject ${sid}: ${e?.data?.error || e.message}`, 'error');
    return;
  }
  const types = state.latestNodesPayload?.subject_types;
  if (types) types[sid] = { type, set_by: 'user' };  // until the next node poll says so
  showToast(`Subject ${sid} is decoded as ${type}`, 'info');
  reopenSubject(sid);
};

const clearSubjectType = async (sid) => {
  try {
    await requestJson(`/api/subjects/${sid}/type`, { method: 'DELETE' });
  } catch (e) {
    showToast(`Subject ${sid}: ${e?.data?.error || e.message}`, 'error');
    return;
  }
  delete state.latestNodesPayload?.subject_types?.[sid];
  state.latestBySubject.delete(sid);  // its last decoded message is not its type any more
  reopenSubject(sid);
};

// Marks the rows whose plot and call card are open.
const _highlightSubjectRow = () => {
  if (!subjectsTabulator) return;
  const open = new Set([state._subjectsServiceRowId,
    state.selectedPlotSubject != null ? `sub:${state.selectedPlotSubject}` : null]);
  for (const row of subjectsTabulator.getRows()) {
    row.getElement().classList.toggle('selected-row', open.has(row.getData()._rowId));
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

// A table hidden while scrolled down came back blank: Tabulator (6.4) redraws
// it while hidden and loses its place. It is hidden at its top instead, and
// goes back to where it was after the redraw that showing it brings.
const TABLE_RESTORE_MS = 1000;
const _tablePlaces = {};  // table element id -> {top, restore}

const _tableHolder = (id) => el(id)?.querySelector('.tabulator-tableholder');

const _parkTable = (tabulator, id) => {
  const holder = tabulator && _tableHolder(id);
  if (!holder) return;
  const pending = _tablePlaces[id]?.restore ? _tablePlaces[id] : null;
  if (pending) tabulator.off('renderComplete', pending.restore);  // left again before it got its place back
  _tablePlaces[id] = { top: pending ? pending.top : holder.scrollTop };
  holder.scrollTop = 0;
};

const _unparkTable = (tabulator, id) => {
  const place = _tablePlaces[id];
  if (!tabulator || !place?.top) return;
  const shownAt = Date.now();
  place.restore = () => {
    tabulator.off('renderComplete', place.restore);
    place.restore = null;
    if (Date.now() - shownAt < TABLE_RESTORE_MS) _tableHolder(id).scrollTop = place.top;
  };
  tabulator.on('renderComplete', place.restore);
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
    const card = document.querySelector('#selectedNodeContent .subject-service-card');
    if (card) {
      state._stashedServiceCard = card;
      card.remove();
    }
    _parkTable(subjectsTabulator, 'subjectsTable');
  } else if (prevView === 'nodes') {
    state._nodesPlotSubject = state.selectedPlotSubject;
    _parkTable(nodesTabulator, 'nodesTable');
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
    state.selectedPlotSubject = state._subjectsPlotSubject ?? null;
    subjectsEl.classList.remove('hidden');
    stopPlotAnim();
    const card = state._stashedServiceCard;
    const hasPlot = state.selectedPlotSubject != null;
    const hasDetail = hasPlot || Boolean(card);
    detailHandle.classList.toggle('hidden', !hasDetail);
    detailPanel.classList.toggle('hidden', !hasDetail);
    const tabs = detailPanel.querySelector('.detail-tabs');
    if (tabs) tabs.classList.toggle('hidden', hasDetail);
    if (hasDetail) _restoreDetailPanelState('subjects');
    initSubjectsTable();
    refreshSubjectsTable();
    _unparkTable(subjectsTabulator, 'subjectsTable');
    if (hasDetail) {
      el('selectedNodeContent').replaceChildren();  // the nodes view's content
      const slots = _detailSlots();
      if (card) {
        state._stashedServiceCard = null;
        slots.service.replaceChildren(card);
      }
      if (hasPlot) _renderSubjectPlotSlot(state.selectedPlotSubject);
      _updateSubjectsDetail();
    }
    _highlightSubjectRow();
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
    _unparkTable(nodesTabulator, 'nodesTable');
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
