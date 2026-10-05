// Network topology graph — fully decoupled module.
// Reads state.latestNodesPayload (read-only). Owns its own state,
// persistence, rendering, selection, and info panel.

const GraphView = (() => {
  // ── Own state ──
  const STORAGE_KEY = 'cynitor.graph.v1';
  const REFRESH_MS = 1000;
  const GRID = 40;
  const LABEL_MAX = 18;
  const _snap = (v) => Math.round(v / GRID) * GRID;
  const _truncate = (s, max = LABEL_MAX) => {
    if (!s) return '';
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  };

  const gState = {
    positions: {},
    selectedId: null,
    zoom: null,
    view: 'node-centric',
    hideOffline: false,
    // Heartbeat, port.List and the like connect every node to every other;
    // a node's health already says what its Heartbeat does.
    hideSystem: true,
    filterText: '',
    gravityMetric: 'none',
    showGrid: true,
    showLinkStats: false,
    animateTraffic: false,
    focus: null,  // a status-strip kind picked out of the graph; not saved
    initialized: false,
  };

  const VIEW_OPTIONS = [
    { value: 'nodes', label: 'Nodes only' },
    { value: 'node-centric', label: 'Node-centric' },
    { value: 'subject-centric', label: 'Subject-centric' },
  ];

  const SYSTEM_SUBJECT_MIN = 6144;
  const SYSTEM_SERVICE_MIN = 256;
  const _isSystemSubject = (sid) => Number(sid) >= SYSTEM_SUBJECT_MIN;
  const _isSystemService = (sid) => Number(sid) >= SYSTEM_SERVICE_MIN;
  const _showSubs = () => gState.view !== 'nodes';

  const GRAVITY_OPTIONS = [
    { value: 'none', label: 'none' },
    { value: 'degree', label: 'total links' },
    { value: 'subjects', label: 'subject channels' },
    { value: 'services', label: 'service channels' },
    { value: 'rate', label: 'publish rate' },
    { value: 'payload', label: 'payload size' },
  ];

  let svg, container, simulation, gLinks, gNodes, gLabels, gLinkLabels;
  let zoomBehavior;
  let refreshTimer = null;
  let prevSnapshot = null;
  let _savePending = null;
  // Fit the view once the layout settles, unless the user zooms or pans first.
  let fitPending = false;
  let overlayHtml = null;

  // ── Persistence ──

  const _writeNow = () => {
    const data = {
      positions: gState.positions,
      zoom: gState.zoom,
      view: gState.view,
      hideOffline: gState.hideOffline,
      hideSystem: gState.hideSystem,
      filterText: gState.filterText,
      gravityMetric: gState.gravityMetric,
      showGrid: gState.showGrid,
      showLinkStats: gState.showLinkStats,
      animateTraffic: gState.animateTraffic,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  };

  const save = () => {
    if (_savePending) return;
    _savePending = window.setTimeout(() => {
      _savePending = null;
      _writeNow();
    }, 300);
  };

  const load = () => {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      if (raw.positions && typeof raw.positions === 'object') gState.positions = raw.positions;
      if (raw.zoom && typeof raw.zoom === 'object') gState.zoom = raw.zoom;
      if (typeof raw.view === 'string' && VIEW_OPTIONS.some(o => o.value === raw.view)) {
        gState.view = raw.view;
      } else if (typeof raw.showSubjects === 'boolean') {
        gState.view = raw.showSubjects ? 'node-centric' : 'nodes';
      }
      if (typeof raw.hideOffline === 'boolean') gState.hideOffline = raw.hideOffline;
      if (typeof raw.hideSystem === 'boolean') gState.hideSystem = raw.hideSystem;
      if (typeof raw.filterText === 'string') gState.filterText = raw.filterText;
      if (typeof raw.gravityMetric === 'string' && GRAVITY_OPTIONS.some(o => o.value === raw.gravityMetric)) {
        gState.gravityMetric = raw.gravityMetric;
      }
      if (typeof raw.showGrid === 'boolean') gState.showGrid = raw.showGrid;
      if (typeof raw.showLinkStats === 'boolean') gState.showLinkStats = raw.showLinkStats;
      if (typeof raw.animateTraffic === 'boolean') gState.animateTraffic = raw.animateTraffic;
    } catch { /* ignore corrupt storage */ }
  };

  window.addEventListener('beforeunload', () => {
    if (_savePending) { clearTimeout(_savePending); _savePending = null; _writeNow(); }
  });

  // ── Data derivation ──

  const deriveGraph = () => {
    const payload = state.latestNodesPayload;
    const nodes = payload?.nodes || {};
    const deviceNodes = [];
    const subjectNodes = [];
    const links = [];
    const subjectSet = new Map();

    const hiddenSubs = state.hiddenSubjectIds || new Set();
    const hiddenNodeKeys = state.hiddenNodeIds || new Set();
    const _isHiddenSubject = (sid) => hiddenSubs.has(sid) || hiddenSubs.has(String(sid));
    const _isHiddenNode = (node) => {
      const key = typeof nodeStableKey === 'function' ? nodeStableKey(node) : null;
      return key ? hiddenNodeKeys.has(key) : false;
    };

    // Devices are keyed as /api/nodes keys them: by node-ID, or "uid:<hex>"
    // for one whose node-ID another device took. pubs/subs hold these keys.
    for (const [key, node] of Object.entries(nodes)) {
      if (_isHiddenNode(node)) continue;
      const nid = node.node_id;
      const id = `dev:${key}`;
      const shownId = _shownNodeId(node);
      deviceNodes.push({
        id,
        nodeId: nid,
        payloadKey: key,
        shownId,
        ghost: node._ghost === true,
        type: 'device',
        label: _deviceDisplayLabel(node, shownId),
        fullName: _deviceFullName(node, shownId),
        uniqueId: node.unique_id || null,
        stableKey: typeof nodeStableKey === 'function' ? nodeStableKey(node) : null,
        health: getNodeHealthValue(nid),
        disappeared: !!node.has_disappeared,
        status: _deviceStatus(node, nid),
      });

      for (const sid of node.publishers || []) {
        if (_isHiddenSubject(sid)) continue;
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).pubs.push(key);
        links.push({ source: id, target: `sub:${sid}`, type: 'pub', subjectId: sid, publisherId: nid });
      }
      for (const sid of node.subscribers || []) {
        if (_isHiddenSubject(sid)) continue;
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).subs.push(key);
        links.push({ source: `sub:${sid}`, target: id, type: 'sub', subjectId: sid, subscriberId: nid });
      }
    }

    for (const [sid, meta] of subjectSet) {
      const ev = state.latestBySubject.get(sid);
      subjectNodes.push({
        id: `sub:${sid}`,
        subjectId: sid,
        type: 'subject',
        label: _subjectLabel(sid, ev),
        fullType: _subjectFullType(sid, ev),
        pubs: meta.pubs,
        subs: meta.subs,
        status: _subjectStatus(sid, meta.pubs, meta.subs),
      });
    }

    const adjacency = new Map();
    const addAdj = (a, b) => {
      if (!adjacency.has(a)) adjacency.set(a, new Set());
      adjacency.get(a).add(b);
    };
    for (const link of links) {
      const src = typeof link.source === 'object' ? link.source.id : link.source;
      const tgt = typeof link.target === 'object' ? link.target.id : link.target;
      addAdj(src, tgt);
      addAdj(tgt, src);
    }

    // Collapsed device-to-device links (for when subjects hidden)
    const collapsedLinks = [];
    const collapsedAdj = new Map();
    for (const [sid, meta] of subjectSet) {
      if (gState.hideSystem && _isSystemSubject(sid)) continue;
      for (const pub of meta.pubs) {
        for (const sub of meta.subs) {
          if (pub === sub) continue;
          const src = `dev:${pub}`;
          const tgt = `dev:${sub}`;
          const key = `${src}|${tgt}`;
          if (!collapsedAdj.has(key)) {
            collapsedAdj.set(key, {
              source: src, target: tgt, type: 'dev', subjects: [],
              publisherId: nodes[pub]?.node_id ?? null,
              subscriberId: nodes[sub]?.node_id ?? null,
            });
          }
          collapsedAdj.get(key).subjects.push(sid);
        }
      }
    }
    for (const link of collapsedAdj.values()) collapsedLinks.push(link);

    // In "Nodes only" a device's drawn neighbours are devices: the filter and
    // the highlight must reach them, not just the (undrawn) subjects between.
    if (!_showSubs()) {
      for (const link of collapsedLinks) {
        addAdj(link.source, link.target);
        addAdj(link.target, link.source);
      }
    }

    return { deviceNodes, subjectNodes, links, collapsedLinks, adjacency };
  };

  // ── Traffic visualization helpers ──

  const _linkSubjectIds = (link) => {
    if (link.subjectId != null) return [link.subjectId];
    if (Array.isArray(link.subjects)) return link.subjects;
    return [];
  };

  // A rate arrives only with a message, so a publisher that stops keeps its
  // last one. A message counts as current for three of its periods (two
  // seconds at least); the period is the rate's, or the gap between messages
  // when that is longer, as for subjects too slow to report a rate. Of the
  // last two gaps the shorter counts: one stray message after a long pause
  // does not make a stopped subject look slow and alive.
  // A paused or finished replay keeps the picture it stopped at.
  const FRESH_PERIODS = 3;
  const FRESH_MIN_MS = 2000;

  const _isFresh = (ev, now) => {
    if (!ev) return false;
    if (state.replayPaused || state.replayFinished) return true;
    const gapMs = Math.min(ev._gapMs ?? Infinity, ev._prevGapMs ?? Infinity);
    const periodMs = Math.max(ev.rate > 0 ? 1000 / ev.rate : 0, Number.isFinite(gapMs) ? gapMs : 0);
    return now - (ev._rxMs || 0) <= Math.max(FRESH_MIN_MS, FRESH_PERIODS * periodMs);
  };

  const _isOnline = (nodeId) => nodeId != null && !isNodeDisappeared(nodeId);

  // The message an edge's traffic is judged by, or null when an end of the
  // edge is offline. A publisher's edge goes by that publisher's own messages,
  // since a subject may have several publishers (every node publishes
  // Heartbeat); a subscriber's edge goes by the subject's.
  const _edgeEvent = (link, sid) => {
    if (link.type !== 'pub' && !_isOnline(link.subscriberId)) return null;
    if (link.type === 'sub') return state.latestBySubject.get(sid) || null;
    return _isOnline(link.publisherId) ? state.latestByNode.get(link.publisherId)?.get(sid) || null : null;
  };

  // What an edge carries now: whether anything current flows on it, whether
  // a subject on it has gone silent, at what message rate, and the payload
  // size of the latest messages.
  const _linkTraffic = (link) => {
    const now = Date.now();
    const traffic = { live: false, silent: false, rate: 0, payload: 0 };
    for (const sid of _linkSubjectIds(link)) {
      const ev = _edgeEvent(link, sid);
      if (!ev) continue;
      if (!_isFresh(ev, now)) { traffic.silent = true; continue; }
      traffic.live = true;
      traffic.rate += link.type === 'sub' ? getSubjectRate(ev) : (Number(ev.rate) || 0);
      traffic.payload += ev.payload_bytes || 0;
    }
    return traffic;
  };

  const rateToWidth = (rate) => {
    if (!rate || rate <= 0) return 1;
    return Math.min(4, 1 + Math.log10(rate + 1) * 1.1);
  };

  // Traffic is drawn plain, being the usual; an edge with a subject gone
  // silent stands out, even where other subjects on it still flow.
  const _paintLink = function(d) {
    const { live, silent, rate } = _linkTraffic(d);
    d3.select(this)
      .attr('stroke-width', rateToWidth(rate))
      .classed('graph-link--live', live && !silent)
      .classed('graph-link--silent', silent);
  };

  // Publisher and device-to-device edges point at whoever receives the data.
  const _hasArrow = (link) => link.type !== 'sub';

  // The Nodes table's health icons; NOMINAL, the usual, needs none.
  const HEALTH_ICONS = { ADVISORY: '~', CAUTION: '!', WARNING: '!!', 1: '~', 2: '!', 3: '!!' };

  // Ring and icon coloured as the Nodes table colours health: only what is
  // not the usual. An offline device's ring is dashed.
  const _paintDevice = function(d) {
    const health = d.disappeared ? '' : getStatusClass('health', d.health);
    d3.select(this)
      .classed('graph-node--offline', d.disappeared)
      .classed('graph-node--warn', health === 'status-warn')
      .classed('graph-node--err', health === 'status-err')
      .select('.graph-health-badge')
      .text(health ? HEALTH_ICONS[String(d.health).toUpperCase()] || '' : '');
  };

  // What is unusual about a device, said under its name: since when it has
  // been offline, or a mode other than OPERATIONAL. Null when all is usual.
  const _deviceStatus = (raw, nodeId) => {
    if (raw?.has_disappeared) {
      const ago = typeof formatLastSeen === 'function' ? formatLastSeen(raw.last_seen) : '-';
      return { text: ago === '-' ? 'offline' : `offline · ${ago}`, level: 'err' };
    }
    const mode = getNodeModeValue(nodeId);
    return mode && getStatusClass('mode', mode) ? { text: mode, level: 'warn' } : null;
  };

  // What is unusual about a subject: subscribed to but published by no node,
  // so that its subscribers wait for nothing; or of a type nothing names, so
  // that it cannot be decoded. Null when all is usual.
  const _subjectStatus = (sid, pubs, subs) => {
    if (!pubs.length && subs.length) return { text: 'no publisher', level: 'warn' };
    if (typeof isUntypedSubject === 'function' && isUntypedSubject(sid)) return { text: 'type unknown', level: 'warn' };
    return null;
  };

  const _paintSubject = function(d) {
    d3.select(this).classed('graph-node--warn', !!d.status);
  };

  // The node-ID a device is shown under: its own, or for one whose node-ID
  // another device took, the one it last had, as the Nodes table shows it.
  const _shownNodeId = (raw) => (raw?._ghost === true ? raw.last_node_id : raw?.node_id) ?? '-';

  // A displaced device goes without its alias, as in the Nodes table.
  const _deviceFullName = (raw, shownId) => {
    const alias = typeof getNodeAlias === 'function' && raw?._ghost !== true ? getNodeAlias(raw?.unique_id) : null;
    return alias || raw?.name || `Node ${shownId}`;
  };

  // A dotted name (org.example.flight_controller) keeps its end, which tells
  // nodes apart; other names, such as aliases, keep their start.
  const _truncateName = (s, max) => {
    if (!s || s.length <= max || !/^[\w-]+(\.[\w-]+)+$/.test(s)) return _truncate(s, max);
    const parts = s.split('.');
    let tail = parts.pop();
    while (parts.length && parts[parts.length - 1].length + 1 + tail.length < max) {
      tail = `${parts.pop()}.${tail}`;
    }
    return `…${tail.slice(-(max - 1))}`;
  };

  const _deviceDisplayLabel = (raw, shownId) => _truncateName(_deviceFullName(raw, shownId), 20);

  // A subject's type in full, as the tables name it, or null while nothing
  // names it.
  const _subjectFullType = (sid, ev) => {
    const type = typeof subjectTypeName === 'function' ? subjectTypeName(sid, ev) : ev?.message_type;
    return type && type !== '-' ? type : null;
  };

  // "uavcan.si.sample.angular_velocity.Vector3.1.0" or "Vector3_1_0": "Vector3".
  const _subjectShortType = (sid, ev) => {
    const type = _subjectFullType(sid, ev);
    if (!type || type === 'type unknown') return '';
    const parts = type.split('.').filter((p) => !/^\d+$/.test(p));
    return (parts.pop() || '').replace(/_\d+_\d+$/, '');
  };

  // "1100 Vector3": the subject-ID first, as many subjects share a type.
  const _subjectLabel = (sid, ev) => {
    const short = _subjectShortType(sid, ev);
    return _truncate(short ? `${sid} ${short}` : String(sid));
  };

  const _linkStatText = (link) => {
    const { silent, rate, payload } = _linkTraffic(link);
    if (silent) return 'silent';
    const parts = [];
    if (rate > 0) parts.push(`${Math.round(rate)} Hz`);
    if (payload > 0) parts.push(`${payload} B`);
    return parts.join(' · ');
  };

  // ── Gravity ──

  const _deviceSubjectIds = (raw) => [
    ...(raw?.publishers || []),
    ...(raw?.subscribers || []),
  ];

  const _metricValue = (node, metric) => {
    if (metric === 'none') return 0;
    const raw = state.latestNodesPayload?.nodes?.[node.payloadKey];
    const filterSubs = (arr) => gState.hideSystem ? arr.filter(sid => !_isSystemSubject(sid)) : arr;
    const filterSvcs = (arr) => gState.hideSystem ? arr.filter(sid => !_isSystemService(sid)) : arr;
    if (node.type === 'device') {
      const pubsList = filterSubs(raw?.publishers || []);
      const subsList = filterSubs(raw?.subscribers || []);
      const serversList = filterSvcs(raw?.servers || []);
      const clientsList = filterSvcs(raw?.clients || []);
      const pubs = pubsList.length;
      const subs = subsList.length;
      const servers = serversList.length;
      const clients = clientsList.length;
      switch (metric) {
        case 'degree': return pubs + subs + servers + clients;
        case 'subjects': return pubs + subs;
        case 'services': return servers + clients;
        case 'rate': return typeof getNodeRate === 'function' ? getNodeRate(node.nodeId) : 0;
        case 'payload': {
          let total = 0;
          for (const sid of [...pubsList, ...subsList]) {
            total += state.latestBySubject.get(sid)?.payload_bytes || 0;
          }
          return total;
        }
      }
      return 0;
    }
    // Subject node
    const ev = state.latestBySubject.get(node.subjectId);
    const channelDegree = (node.pubs?.length || 0) + (node.subs?.length || 0);
    switch (metric) {
      case 'degree':
      case 'subjects': return channelDegree;
      case 'services': return 0;
      case 'rate': return getSubjectRate(ev);
      case 'payload': return ev?.payload_bytes || 0;
    }
    return 0;
  };

  const _applyGravity = () => {
    if (!simulation) return;
    const metric = gState.gravityMetric || 'none';
    if (metric === 'none') {
      simulation.force('gravity', null);
      return;
    }
    const nodes = simulation.nodes();
    let max = 0;
    const values = new Map();
    for (const n of nodes) {
      const v = _metricValue(n, metric);
      values.set(n.id, v);
      if (v > max) max = v;
    }
    if (max <= 0) {
      simulation.force('gravity', null);
      return;
    }
    const svgEl = svg.node();
    const w = svgEl.viewBox.baseVal.width || 800;
    const h = svgEl.viewBox.baseVal.height || 600;
    const MAX_STRENGTH = 0.18;
    simulation.force('gravity',
      d3.forceRadial(0, w / 2, h / 2).strength(n => MAX_STRENGTH * (values.get(n.id) || 0) / max));
  };

  // ── Initialization ──

  const init = () => {
    load();
    const root = document.getElementById('graphContainer');
    if (!root || gState.initialized) return;

    root.innerHTML = '';

    // Toolbar
    const toolbar = document.createElement('div');
    toolbar.className = 'graph-toolbar';
    toolbar.innerHTML = `
      <div class="graph-select-group">
        <label for="graphView">View</label>
        <select id="graphView" class="graph-select" aria-label="View mode">
          ${VIEW_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
        </select>
      </div>
      <label class="graph-toggle">
        <input type="checkbox" id="graphHideOffline" ${gState.hideOffline ? 'checked' : ''} />
        <span>Hide offline</span>
      </label>
      <label class="graph-toggle">
        <input type="checkbox" id="graphHideSystem" ${gState.hideSystem ? 'checked' : ''} />
        <span>Hide system</span>
      </label>
      <input type="text" id="graphFilter" class="graph-filter" placeholder="Filter by id, name, or type…" aria-label="Filter graph" />
      <details class="graph-display" id="graphDisplay">
        <summary class="graph-btn">Display</summary>
        <div class="graph-display-menu">
          <label class="graph-toggle">
            <input type="checkbox" id="graphShowGrid" ${gState.showGrid ? 'checked' : ''} />
            <span>Show grid</span>
          </label>
          <label class="graph-toggle">
            <input type="checkbox" id="graphShowLinkStats" ${gState.showLinkStats ? 'checked' : ''} />
            <span>Link stats</span>
          </label>
          <label class="graph-toggle">
            <input type="checkbox" id="graphAnimate" ${gState.animateTraffic ? 'checked' : ''} />
            <span>Animate traffic</span>
          </label>
          <div class="graph-select-group">
            <label for="graphGravity">Gravity</label>
            <select id="graphGravity" class="graph-select" aria-label="Gravity metric">
              ${GRAVITY_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
            </select>
          </div>
        </div>
      </details>
      <button class="graph-btn" id="graphFit" aria-label="Fit graph to view">Fit</button>
      <button class="graph-btn" id="graphResetLayout" aria-label="Reset graph layout">Reset layout</button>
      <button class="graph-btn" id="graphUnpinAll" aria-label="Unpin all nodes">Unpin all</button>
      <button class="graph-btn graph-hidden-badge hidden" id="graphHiddenBadge" aria-label="Show all hidden">0 hidden — show</button>
    `;
    root.appendChild(toolbar);

    const statusStrip = document.createElement('div');
    statusStrip.className = 'graph-status-bar';
    statusStrip.id = 'graphStatus';
    statusStrip.setAttribute('role', 'status');
    statusStrip.addEventListener('click', (e) => {
      const key = e.target.closest('[data-focus]')?.dataset.focus;
      if (!key) return;
      _setFocus(gState.focus === key ? null : key);
      _renderStatusStrip();
    });
    root.appendChild(statusStrip);

    const legendBar = document.createElement('div');
    legendBar.className = 'graph-legend-bar';
    legendBar.innerHTML = `
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device"></span>device</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--subject"></span>subject</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--warn"></span>advisory ~, caution !</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--err"></span>warning !!</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--offline"></span>offline</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--pinned"></span>pinned</span>
      <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--live"></span>traffic</span>
      <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--silent"></span>silent</span>
      <span class="graph-legend-item"><span class="graph-legend-line"></span>no data</span>
    `;
    root.appendChild(legendBar);

    document.getElementById('graphFilter').value = gState.filterText || '';
    document.getElementById('graphGravity').value = gState.gravityMetric || 'none';
    document.getElementById('graphView').value = gState.view || 'node-centric';

    // SVG
    const svgWrap = document.createElement('div');
    svgWrap.className = 'graph-svg-wrap';
    root.appendChild(svgWrap);

    // Info panel — placed inside svgWrap so positioning is in canvas pixel space.
    const info = document.createElement('div');
    info.className = 'graph-info hidden';
    info.id = 'graphInfo';
    svgWrap.appendChild(info);
    // One listener for the panel's buttons: the panel is refreshed in place,
    // so a button outlives any single render.
    info.addEventListener('click', (e) => {
      const action = e.target.closest('button')?.id;
      if (action === 'graphInfoClose') { _selectNode(null); return; }
      const node = simulation?.nodes().find((n) => n.id === gState.selectedId);
      if (!node) return;
      if (action === 'graphInfoHide') _hideFromGraph(node);
      else if (action === 'graphInfoRename') _startRename(node);
      else if (action === 'graphInfoOpen') _openInNodes(node);
      else if (action === 'graphInfoPlot') _openPlot(node);
    });

    const width = svgWrap.clientWidth || 800;
    const height = svgWrap.clientHeight || 600;

    svg = d3.select(svgWrap).append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`);

    const defs = svg.append('defs');
    defs.append('marker')
      .attr('id', 'graph-arrow-pub')
      .attr('viewBox', '0 0 10 6')
      .attr('refX', 10).attr('refY', 3)
      .attr('markerWidth', 8).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,0 L10,3 L0,6').attr('fill', 'var(--muted)');
    defs.append('marker')
      .attr('id', 'graph-arrow-pub-hi')
      .attr('viewBox', '0 0 10 6')
      .attr('refX', 10).attr('refY', 3)
      .attr('markerWidth', 8).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,0 L10,3 L0,6').attr('fill', 'var(--accent)');

    const gridPattern = defs.append('pattern')
      .attr('id', 'graph-grid-pattern')
      .attr('width', GRID)
      .attr('height', GRID)
      .attr('patternUnits', 'userSpaceOnUse');
    gridPattern.append('circle')
      .attr('cx', 0).attr('cy', 0).attr('r', 1.5)
      .attr('class', 'graph-grid-dot');

    container = svg.append('g').attr('class', 'graph-layer');
    container.append('rect')
      .attr('class', 'graph-grid-bg')
      .classed('graph-grid-hidden', !gState.showGrid)
      .attr('x', -10000).attr('y', -10000)
      .attr('width', 20000).attr('height', 20000)
      .attr('fill', 'url(#graph-grid-pattern)')
      .attr('pointer-events', 'none');
    gLinks = container.append('g').attr('class', 'graph-links')
      .classed('graph-animate', gState.animateTraffic);
    gNodes = container.append('g').attr('class', 'graph-nodes');
    gLabels = container.append('g').attr('class', 'graph-labels');
    gLinkLabels = container.append('g').attr('class', 'graph-link-labels');

    // Zoom
    zoomBehavior = d3.zoom()
      .scaleExtent([0.1, 4])
      .on('zoom', (e) => {
        if (e.sourceEvent) fitPending = false;  // the user has placed the view
        container.attr('transform', e.transform);
        gState.zoom = { k: e.transform.k, x: e.transform.x, y: e.transform.y };
        if (gState.selectedId) _positionInfoPanel();
        save();
      });
    svg.call(zoomBehavior);

    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => {
        _syncViewBox();
        if (gState.selectedId) _positionInfoPanel();
      });
      ro.observe(svgWrap);
    }

    if (gState.zoom) {
      svg.call(zoomBehavior.transform,
        d3.zoomIdentity.translate(gState.zoom.x, gState.zoom.y).scale(gState.zoom.k));
    } else {
      fitPending = true;
    }

    // Click on background deselects
    svg.on('click', (e) => {
      if (!e.target.closest('.graph-node')) _selectNode(null);
    });

    // Toolbar events
    document.getElementById('graphView').addEventListener('change', (e) => {
      gState.view = e.target.value;
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphHideOffline').addEventListener('change', (e) => {
      gState.hideOffline = e.target.checked;
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphHideSystem').addEventListener('change', (e) => {
      gState.hideSystem = e.target.checked;
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphGravity').addEventListener('change', (e) => {
      gState.gravityMetric = e.target.value;
      save();
      _applyGravity();
      simulation.alpha(0.3).restart();
    });
    document.getElementById('graphShowGrid').addEventListener('change', (e) => {
      gState.showGrid = e.target.checked;
      save();
      container.select('.graph-grid-bg').classed('graph-grid-hidden', !gState.showGrid);
    });
    document.getElementById('graphShowLinkStats').addEventListener('change', (e) => {
      gState.showLinkStats = e.target.checked;
      save();
      _renderLinkStats();
    });
    document.getElementById('graphAnimate').addEventListener('change', (e) => {
      gState.animateTraffic = e.target.checked;
      save();
      gLinks.classed('graph-animate', gState.animateTraffic);
    });
    document.getElementById('graphFit').addEventListener('click', () => _fitToView());
    // The Display menu closes on a click elsewhere, or on Escape.
    const display = document.getElementById('graphDisplay');
    document.addEventListener('click', (e) => {
      if (display.open && !display.contains(e.target)) display.open = false;
    });
    display.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !display.open) return;
      display.open = false;
      display.querySelector('summary').focus();
    });
    let _filterDebounce = null;
    document.getElementById('graphFilter').addEventListener('input', (e) => {
      gState.filterText = e.target.value;
      save();
      if (_filterDebounce) clearTimeout(_filterDebounce);
      _filterDebounce = setTimeout(() => {
        _filterDebounce = null;
        _render(deriveGraph());
      }, 150);
    });
    document.getElementById('graphResetLayout').addEventListener('click', () => {
      gState.positions = {};
      gState.zoom = null;
      svg.call(zoomBehavior.transform, d3.zoomIdentity);
      fitPending = true;
      save();
      prevSnapshot = null;
      _render(deriveGraph());
    });
    document.getElementById('graphHiddenBadge').addEventListener('click', () => {
      if (typeof unhideAllNodes === 'function') unhideAllNodes();
      if (typeof unhideAllSubjects === 'function') unhideAllSubjects();
      _render(deriveGraph());
      _renderHiddenBadge();
    });
    document.getElementById('graphUnpinAll').addEventListener('click', () => {
      for (const key of Object.keys(gState.positions)) {
        delete gState.positions[key].pinned;
      }
      gNodes.selectAll('.graph-node').classed('graph-node--pinned', false);
      if (simulation) {
        simulation.nodes().forEach(n => { n.fx = null; n.fy = null; });
        simulation.alpha(0.15).restart();
      }
      save();
    });

    simulation = d3.forceSimulation([])
      .force('charge', d3.forceManyBody().strength(-160))
      .force('collide', d3.forceCollide().radius(d => d.type === 'device' ? 32 : 22))
      .force('center', d3.forceCenter(width / 2, height / 2).strength(0.03))
      .on('tick', _tick)
      .on('end', _recalcLabelSides)
      .stop();

    gState.initialized = true;
    _render(deriveGraph());
    _startRefresh();
  };

  // ── Rendering ──

  const _render = (graph) => {
    const { deviceNodes, subjectNodes, links, collapsedLinks, adjacency } = graph;

    // Not connected, or no node on the bus yet: say so, not an empty canvas.
    const noNodes = !Object.keys(state.latestNodesPayload?.nodes || {}).length;
    const placeholderHtml = (typeof eventSourcePlaceholder === 'function'
      ? eventSourcePlaceholder('view the topology graph') : null)
      || (noNodes && typeof svcStateMsg === 'function'
        ? svcStateMsg('<span class="svc-spinner"></span>', 'Waiting for nodes…',
          'Listening on the CAN bus. Nodes will appear as they send heartbeats.')
        : null);
    const svgWrap = svg?.node()?.parentNode;
    if (svgWrap) {
      let overlay = svgWrap.querySelector('.graph-conn-overlay');
      if (placeholderHtml) {
        if (!overlay) {
          overlay = document.createElement('div');
          overlay.className = 'graph-conn-overlay';
          svgWrap.appendChild(overlay);
          overlayHtml = null;
        }
        // Rewritten only when it changes: a rewrite restarts its spinner.
        if (overlayHtml !== placeholderHtml) {
          overlay.innerHTML = placeholderHtml;
          overlayHtml = placeholderHtml;
        }
        prevSnapshot = null;
        return;
      }
      if (overlay) overlay.remove();
    }

    const showSubs = _showSubs();
    const subjectsOnTop = gState.view === 'subject-centric';
    const filterStr = (gState.filterText || '').trim().toLowerCase();
    const byId = new Map([...deviceNodes, ...subjectNodes].map(n => [n.id, n]));

    const passesOfflineGate = (n) => !(n.type === 'device' && gState.hideOffline && n.disappeared);
    const passesSystemGate = (n) => !(gState.hideSystem && n.type === 'subject' && _isSystemSubject(n.subjectId));
    const matchesFilter = (n) => {
      if (!filterStr) return true;
      if (n.type === 'device') {
        return String(n.shownId).includes(filterStr) || (n.label || '').toLowerCase().includes(filterStr);
      }
      return String(n.subjectId).includes(filterStr)
        || (n.label || '').toLowerCase().includes(filterStr)
        || (n.fullType || '').toLowerCase().includes(filterStr);
    };

    const baseVisible = new Set();
    for (const n of byId.values()) {
      if (passesOfflineGate(n) && passesSystemGate(n) && matchesFilter(n)) baseVisible.add(n.id);
    }
    const visibleIds = new Set(baseVisible);
    if (filterStr) {
      for (const id of baseVisible) {
        const nb = adjacency.get(id);
        if (!nb) continue;
        for (const nbId of nb) {
          const nbNode = byId.get(nbId);
          if (nbNode && passesOfflineGate(nbNode) && passesSystemGate(nbNode)) visibleIds.add(nbId);
        }
      }
    }

    const allNodes = (showSubs ? [...deviceNodes, ...subjectNodes] : [...deviceNodes])
      .filter(n => visibleIds.has(n.id));
    const allLinks = (showSubs ? links : collapsedLinks).filter(l => {
      const src = typeof l.source === 'object' ? l.source.id : l.source;
      const tgt = typeof l.target === 'object' ? l.target.id : l.target;
      return visibleIds.has(src) && visibleIds.has(tgt);
    });

    const svgEl = svg.node();
    const width = svgEl.viewBox.baseVal.width || 800;
    const height = svgEl.viewBox.baseVal.height || 600;

    // Preserve existing simulation positions
    const oldPosMap = new Map();
    if (simulation) {
      for (const n of simulation.nodes()) {
        oldPosMap.set(n.id, { x: n.x, y: n.y, vx: n.vx, vy: n.vy });
      }
    }

    // Seed positions
    for (const node of allNodes) {
      const saved = gState.positions[node.id];
      const old = oldPosMap.get(node.id);
      if (saved) {
        node.x = saved.x;
        node.y = saved.y;
        if (saved.pinned) { node.fx = saved.x; node.fy = saved.y; }
      } else if (old) {
        node.x = old.x;
        node.y = old.y;
        node.vx = old.vx;
        node.vy = old.vy;
      } else {
        _seedNewNode(node, allNodes, allLinks, adjacency, width, height);
      }
    }

    // Update simulation
    simulation.nodes(allNodes);
    simulation.force('link', d3.forceLink(allLinks).id(d => d.id).distance(d => showSubs ? 80 : 110).strength(0.4));
    if (showSubs) {
      const top = height * 0.33;
      const bottom = height * 0.67;
      simulation.force('bipartite', d3.forceY(d => {
        const isSubject = d.type === 'subject';
        const goTop = subjectsOnTop ? isSubject : !isSubject;
        return goTop ? top : bottom;
      }).strength(0.06));
    } else {
      simulation.force('bipartite', null);
    }
    _applyGravity();
    simulation.alpha(oldPosMap.size === 0 ? 0.6 : 0.08).restart();

    // Links
    const linkSel = gLinks.selectAll('.graph-link').data(allLinks, d => {
      const src = typeof d.source === 'object' ? d.source.id : d.source;
      const tgt = typeof d.target === 'object' ? d.target.id : d.target;
      return `${src}|${tgt}`;
    });
    linkSel.exit().remove();
    const linkEnter = linkSel.enter().append('line')
      .attr('class', 'graph-link')
      .attr('marker-end', d => _hasArrow(d) ? 'url(#graph-arrow-pub)' : null);
    linkEnter.merge(linkSel).each(_paintLink);

    // Nodes
    const nodeSel = gNodes.selectAll('.graph-node').data(allNodes, d => d.id);
    nodeSel.exit().remove();
    const nodeEnter = nodeSel.enter().append('g')
      .attr('class', d => `graph-node graph-node--${d.type}`)
      .call(_dragBehavior())
      .on('click', (e, d) => { e.stopPropagation(); _selectNode(d.id); })
      .on('mouseenter', (e, d) => _hoverNode(d.id))
      .on('mouseleave', () => _hoverNode(null));

    nodeEnter.each(function(d) {
      const g = d3.select(this);
      if (d.type === 'device') {
        g.append('circle')
          .attr('r', 14)
          .attr('class', 'graph-device-circle');
        g.append('text')
          .attr('class', 'graph-node-id')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .text(d.shownId);
        g.append('text')
          .attr('class', 'graph-health-badge')
          .attr('x', 11).attr('y', -10)
          .attr('dominant-baseline', 'central');
      } else {
        g.append('rect')
          .attr('x', -8).attr('y', -8)
          .attr('width', 16).attr('height', 16)
          .attr('rx', 2)
          .attr('class', 'graph-subject-rect')
          .attr('transform', 'rotate(45)');
      }
    });

    const merged = nodeEnter.merge(nodeSel);
    merged.classed('graph-node--pinned', d => !!gState.positions[d.id]?.pinned);
    merged.filter(d => d.type === 'device').each(_paintDevice);
    merged.filter(d => d.type === 'subject').each(_paintSubject);

    // Labels: the name, and a second line for what is unusual
    const labelSel = gLabels.selectAll('.graph-label').data(allNodes, d => d.id);
    labelSel.exit().remove();
    const labelEnter = labelSel.enter().append('text')
      .attr('class', d => `graph-label graph-label--${d.type}`)
      .attr('text-anchor', 'middle');
    labelEnter.append('tspan').attr('class', 'graph-label-name');
    labelEnter.append('tspan')
      .attr('class', 'graph-label-status')
      .attr('dy', '1.2em');
    labelEnter.merge(labelSel).each(_paintLabel);

    // Store references for tick
    gState._adjacency = adjacency;
    gState._allLinks = allLinks;
    gState._visibleIds = visibleIds;
    prevSnapshot = _snapshotKey();
    _renderLinkStats();
    _renderHiddenBadge();
    _renderStatusStrip();
    if (!gState.selectedId) _applyFocus();
  };

  const _renderHiddenBadge = () => {
    const btn = document.getElementById('graphHiddenBadge');
    if (!btn) return;
    const n = (state.hiddenNodeIds?.size || 0) + (state.hiddenSubjectIds?.size || 0);
    if (n === 0) {
      btn.classList.add('hidden');
      return;
    }
    btn.classList.remove('hidden');
    btn.textContent = `${n} hidden — show`;
  };

  const _renderLinkStats = () => {
    if (!gLinkLabels) return;
    if (!gState.showLinkStats) {
      gLinkLabels.selectAll('*').remove();
      return;
    }
    const data = gLinks ? gLinks.selectAll('.graph-link').data() : [];
    const sel = gLinkLabels.selectAll('.graph-link-label').data(data, d => {
      const src = typeof d.source === 'object' ? d.source.id : d.source;
      const tgt = typeof d.target === 'object' ? d.target.id : d.target;
      return `${src}|${tgt}`;
    });
    sel.exit().remove();
    const enter = sel.enter().append('text')
      .attr('class', 'graph-link-label')
      .attr('text-anchor', 'middle');
    enter.merge(sel)
      .text(d => _linkStatText(d))
      .attr('x', d => (d.source.x + d.target.x) / 2)
      .attr('y', d => (d.source.y + d.target.y) / 2 - 3);
  };

  const _seedNewNode = (node, allNodes, allLinks, adjacency, width, height) => {
    const neighbors = adjacency.get(node.id);
    if (neighbors && neighbors.size > 0) {
      let cx = 0, cy = 0, count = 0;
      for (const nid of neighbors) {
        const saved = gState.positions[nid];
        if (saved) { cx += saved.x; cy += saved.y; count++; }
      }
      if (count > 0) {
        const angle = Math.random() * Math.PI * 2;
        const dist = 40 + Math.random() * 30;
        node.x = cx / count + Math.cos(angle) * dist;
        node.y = cy / count + Math.sin(angle) * dist;
        return;
      }
    }
    const placed = allNodes.filter(n => n.x != null && n.id !== node.id);
    if (placed.length > 0) {
      let cx = 0, cy = 0;
      for (const p of placed) { cx += (p.x || 0); cy += (p.y || 0); }
      cx /= placed.length; cy /= placed.length;
      const angle = Math.random() * Math.PI * 2;
      const dist = 80 + Math.random() * 60;
      node.x = cx + Math.cos(angle) * dist;
      node.y = cy + Math.sin(angle) * dist;
    } else {
      node.x = width / 2 + (Math.random() - 0.5) * 100;
      node.y = height / 2 + (Math.random() - 0.5) * 100;
    }
  };

  const _tick = () => {
    gLinks.selectAll('.graph-link')
      .attr('x1', d => d.source.x)
      .attr('y1', d => d.source.y)
      .attr('x2', d => _shortenTarget(d).x)
      .attr('y2', d => _shortenTarget(d).y);

    gNodes.selectAll('.graph-node')
      .attr('transform', d => `translate(${d.x},${d.y})`);

    gLabels.selectAll('.graph-label')
      .attr('x', d => d.x)
      .attr('y', d => _labelY(d));
    // A second line starts again from the label's x.
    gLabels.selectAll('.graph-label-status').attr('x', d => d.x);

    if (gState.showLinkStats) {
      gLinkLabels.selectAll('.graph-link-label')
        .attr('x', d => (d.source.x + d.target.x) / 2)
        .attr('y', d => (d.source.y + d.target.y) / 2 - 3);
    }

    if (gState.selectedId) _positionInfoPanel();
    // Fitted once the layout has nearly settled, not at its last tick.
    if (fitPending && simulation.alpha() < FIT_ALPHA && _fitToView()) fitPending = false;
  };

  // A status line goes under the name: above the node, the name moves up by
  // a line to make room.
  const STATUS_LINE = 11;
  const _labelY = (d) => {
    if (d.labelSide === 'above') {
      return d.y - (d.type === 'device' ? 18 : 14) - (d.status ? STATUS_LINE : 0);
    }
    return d.y + (d.type === 'device' ? 26 : 22);
  };

  // A label's name and, under it, what is unusual about the node. Written
  // only where it changed: this runs every second.
  const _paintLabel = function(d) {
    const label = d3.select(this);
    const name = label.select('.graph-label-name');
    if (name.text() !== d.label) name.text(d.label);
    const status = label.select('.graph-label-status');
    const text = d.status?.text || '';
    if (status.text() !== text) status.text(text);
    status.attr('x', d.x)
      .classed('graph-label-status--err', d.status?.level === 'err')
      .classed('graph-label-status--warn', d.status?.level === 'warn');
  };

  // Zoom so that every drawn node, with room for its label, is in view; no
  // closer than 2×, which would blow a small network up. False when there is
  // nothing to fit yet.
  const FIT_ALPHA = 0.05;
  const FIT_MAX_SCALE = 2;
  const FIT_MARGIN = 40;
  const _fitToView = () => {
    const nodes = simulation?.nodes() || [];
    const box = svg?.node()?.viewBox.baseVal;
    if (!nodes.length || !box?.width || !box?.height) return false;
    // Labels reach about 70 px to either side, 30 above and 40 below.
    const x0 = d3.min(nodes, (n) => n.x) - 70;
    const x1 = d3.max(nodes, (n) => n.x) + 70;
    const y0 = d3.min(nodes, (n) => n.y) - 30;
    const y1 = d3.max(nodes, (n) => n.y) + 40;
    const k = Math.max(0.1, Math.min(FIT_MAX_SCALE,
      (box.width - 2 * FIT_MARGIN) / (x1 - x0), (box.height - 2 * FIT_MARGIN) / (y1 - y0)));
    const t = d3.zoomIdentity
      .translate(box.width / 2 - k * (x0 + x1) / 2, box.height / 2 - k * (y0 + y1) / 2)
      .scale(k);
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    (still ? svg : svg.transition().duration(400)).call(zoomBehavior.transform, t);
    return true;
  };

  const _recalcLabelSides = () => {
    if (!simulation) return;
    const adj = gState._adjacency;
    const vis = gState._visibleIds;
    const nodes = simulation.nodes();
    if (!nodes.length) return;
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    for (const n of nodes) {
      const neighbors = adj?.get(n.id);
      if (!neighbors || neighbors.size === 0) { n.labelSide = 'below'; continue; }
      let sumDy = 0, count = 0;
      for (const nbId of neighbors) {
        if (vis && !vis.has(nbId)) continue;
        const nb = nodeMap.get(nbId);
        if (!nb) continue;
        sumDy += (nb.y - n.y);
        count++;
      }
      n.labelSide = count > 0 && sumDy / count > 0 ? 'above' : 'below';
    }
    if (gLabels) {
      gLabels.selectAll('.graph-label').attr('y', d => _labelY(d));
      _resolveLabelOverlaps();
    }
  };

  const _resolveLabelOverlaps = () => {
    if (!gLabels) return;
    const items = [];
    gLabels.selectAll('.graph-label').each(function(d) {
      let bbox;
      try { bbox = this.getBBox(); } catch { return; }
      items.push({ d, w: bbox.width, h: bbox.height, x: d.x, y: _labelY(d) });
    });
    if (items.length < 2) return;
    const adj = gState._adjacency;
    const overlap = (a, b) =>
      Math.abs(a.x - b.x) < (a.w + b.w) / 2 + 4 &&
      Math.abs(a.y - b.y) < (a.h + b.h) / 2 + 2;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        if (!overlap(items[i], items[j])) continue;
        const aDeg = adj?.get(items[i].d.id)?.size || 0;
        const bDeg = adj?.get(items[j].d.id)?.size || 0;
        const flipB = bDeg <= aDeg;
        const t = flipB ? items[j] : items[i];
        t.d.labelSide = t.d.labelSide === 'above' ? 'below' : 'above';
        t.y = _labelY(t.d);
      }
    }
    gLabels.selectAll('.graph-label').attr('y', d => _labelY(d));
  };

  const _shortenTarget = (d) => {
    const dx = d.target.x - d.source.x;
    const dy = d.target.y - d.source.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const r = d.target.type === 'device' ? 16 : 12;
    return { x: d.target.x - (dx / dist) * r, y: d.target.y - (dy / dist) * r };
  };

  // ── Drag ──

  // Screen pixels a press may wander and still be a click, which only selects:
  // moving and pinning a node take a drag.
  const CLICK_DISTANCE = 3;

  const _dragBehavior = () => d3.drag()
    .clickDistance(CLICK_DISTANCE)
    .on('start', (e, d) => {
      d._press = { x: e.x, y: e.y, reheat: !e.active, moved: false };
    })
    .on('drag', (e, d) => {
      const press = d._press;
      if (!press.moved) {
        const k = d3.zoomTransform(svg.node()).k;
        if (Math.hypot(e.x - press.x, e.y - press.y) * k <= CLICK_DISTANCE) return;
        press.moved = true;
        if (press.reheat) simulation.alphaTarget(0.05).restart();
      }
      d.fx = e.x;
      d.fy = e.y;
    })
    .on('end', function(e, d) {
      if (!e.active) simulation.alphaTarget(0);
      const moved = d._press?.moved;
      d._press = null;
      if (!moved) return;
      const sx = _snap(e.x);
      const sy = _snap(e.y);
      d.fx = sx;
      d.fy = sy;
      gState.positions[d.id] = { x: sx, y: sy, pinned: true };
      d3.select(this).classed('graph-node--pinned', true);
      save();
    });

  // ── Status strip ──
  //
  // What needs a look, counted over what is drawn. A count, clicked, picks
  // those nodes out of the graph; clicked again, it lets them go.
  const FOCUS_KINDS = [
    { key: 'offline', label: 'offline', level: 'err', test: (d) => d.type === 'device' && d.disappeared && !d.ghost },
    { key: 'displaced', label: 'displaced', level: 'err', test: (d) => d.type === 'device' && d.ghost },
    { key: 'health', label: 'unusual health', level: 'warn',
      test: (d) => d.type === 'device' && !d.disappeared && !!getStatusClass('health', d.health) },
    { key: 'mode', label: 'unusual mode', level: 'warn', test: (d) => d.type === 'device' && d.status?.level === 'warn' },
    { key: 'silent', label: 'silent', level: 'warn', test: (d) => d.type === 'subject' && _isSilentSubject(d.subjectId) },
    { key: 'orphan', label: 'no publisher', level: 'warn', test: (d) => d.type === 'subject' && d.status?.text === 'no publisher' },
    { key: 'untyped', label: 'type unknown', level: 'warn', test: (d) => d.type === 'subject' && d.status?.text === 'type unknown' },
  ];

  // A subject that has sent before and has now gone quiet.
  const _isSilentSubject = (sid) => {
    const ev = state.latestBySubject.get(sid);
    return !!ev && !_isFresh(ev, Date.now());
  };

  const _renderStatusStrip = () => {
    const strip = document.getElementById('graphStatus');
    if (!strip) return;
    const nodes = simulation?.nodes() || [];
    const counts = FOCUS_KINDS.map((k) => ({ ...k, count: nodes.filter(k.test).length })).filter((k) => k.count);
    if (gState.focus && !counts.some((k) => k.key === gState.focus)) _setFocus(null);
    const devices = nodes.filter((d) => d.type === 'device').length;
    const fresh = document.createElement('div');
    fresh.innerHTML = `<span class="graph-status-total">${devices} device${devices === 1 ? '' : 's'}</span>`
      + (counts.length
        ? counts.map((k) => `<button type="button" class="graph-chip graph-chip--${k.level}" data-focus="${k.key}"`
          + ` aria-pressed="${gState.focus === k.key}">${k.count} ${escapeHtml(k.label)}</button>`).join('')
        : '<span class="graph-status-usual">nothing unusual</span>');
    patchChildren(strip, fresh);
  };

  const _setFocus = (key) => {
    gState.focus = key;
    _selectNode(null);  // clears the highlight, which then draws the focus
  };

  // Dims all but the nodes of the focused kind, and the edges between others.
  const _applyFocus = () => {
    const kind = FOCUS_KINDS.find((k) => k.key === gState.focus);
    if (!kind || !gNodes) return;
    const picked = new Set((simulation?.nodes() || []).filter(kind.test).map((d) => d.id));
    gNodes.selectAll('.graph-node').classed('graph-dim', (d) => !picked.has(d.id));
    gLabels.selectAll('.graph-label').classed('graph-dim', (d) => !picked.has(d.id));
    gLinks.selectAll('.graph-link').classed('graph-dim', (d) => !picked.has(d.source.id) && !picked.has(d.target.id));
  };

  // ── Selection & highlighting ──

  const _selectNode = (id) => {
    gState.selectedId = id;
    _applyHighlight(id, false);
    _renderInfo(id);
  };

  const _hoverNode = (id) => {
    if (gState.selectedId || gState.focus) return;
    _applyHighlight(id, true);
    if (!id) _renderInfo(null);
  };

  const _applyHighlight = (id, isHover) => {
    if (!id) {
      gNodes.selectAll('.graph-node').classed('graph-dim', false).classed('graph-hi', false);
      gLinks.selectAll('.graph-link').classed('graph-dim', false).classed('graph-link--hi', false)
        .attr('marker-end', d => _hasArrow(d) ? 'url(#graph-arrow-pub)' : null);
      gLabels.selectAll('.graph-label').classed('graph-dim', false);
      _applyFocus();
      return;
    }

    const adj = gState._adjacency;
    const neighbors = adj?.get(id) || new Set();

    gNodes.selectAll('.graph-node').each(function(d) {
      const isSelected = d.id === id;
      const isNeighbor = neighbors.has(d.id);
      d3.select(this)
        .classed('graph-hi', isSelected)
        .classed('graph-dim', !isSelected && !isNeighbor);
    });

    gLinks.selectAll('.graph-link').each(function(d) {
      const src = typeof d.source === 'object' ? d.source.id : d.source;
      const tgt = typeof d.target === 'object' ? d.target.id : d.target;
      const connected = src === id || tgt === id;
      d3.select(this)
        .classed('graph-link--hi', connected)
        .classed('graph-dim', !connected)
        .attr('marker-end', !_hasArrow(d) ? null : connected ? 'url(#graph-arrow-pub-hi)' : 'url(#graph-arrow-pub)');
    });

    gLabels.selectAll('.graph-label').each(function(d) {
      const isSelected = d.id === id;
      const isNeighbor = neighbors.has(d.id);
      d3.select(this).classed('graph-dim', !isSelected && !isNeighbor);
    });
  };

  // ── Info panel ──

  const _renderInfo = (id) => {
    const panel = document.getElementById('graphInfo');
    if (!panel) return;

    // Don't wipe an in-progress inline rename when the periodic refresh tick
    // (REFRESH_MS / nodes-payload update) re-enters here. The rename input
    // commits on Enter/blur; until then keep the panel DOM intact.
    const renameInput = panel.querySelector('.graph-info-rename');
    if (renameInput && document.activeElement === renameInput) return;

    if (!id) {
      panel.classList.add('hidden');
      panel.innerHTML = '';
      return;
    }

    panel.classList.remove('hidden');
    const allNodes = simulation?.nodes() || [];
    const node = allNodes.find(n => n.id === id);
    if (!node) { panel.classList.add('hidden'); return; }

    // Patched in place, not rebuilt: this runs every second, and a click on a
    // button swapped out between press and release is lost.
    const fresh = document.createElement('div');
    fresh.innerHTML = node.type === 'device' ? _deviceInfoHtml(node) : _subjectInfoHtml(node);
    patchChildren(panel, fresh);
    _positionInfoPanel();
  };

  const _positionInfoPanel = () => {
    const panel = document.getElementById('graphInfo');
    if (!panel || !gState.selectedId || panel.classList.contains('hidden')) return;
    const node = simulation?.nodes().find(n => n.id === gState.selectedId);
    if (!node) return;

    const svgEl = svg.node();
    const wrap = svgEl.parentNode;
    if (!wrap) return;
    const wrapW = wrap.clientWidth || 800;
    const wrapH = wrap.clientHeight || 600;

    const t = d3.zoomTransform(svgEl);
    const px = t.applyX(node.x);
    const py = t.applyY(node.y);

    const panelW = panel.offsetWidth || 256;
    const panelH = panel.offsetHeight || 0;
    const gap = 24;
    const nodeR = node.type === 'device' ? 14 : 12;

    const rightX = px + nodeR + gap;
    const leftX = px - nodeR - gap - panelW;
    const roomRight = wrapW - rightX;
    const roomLeft = px - nodeR - gap;
    let x;
    if (roomRight >= panelW) x = rightX;
    else if (roomLeft >= panelW) x = leftX;
    else x = roomRight >= roomLeft ? rightX : leftX;
    x = Math.max(8, Math.min(wrapW - panelW - 8, x));

    let y = py - panelH / 2;
    y = Math.max(8, Math.min(wrapH - panelH - 8, y));

    panel.style.left = `${x}px`;
    panel.style.top = `${y}px`;
  };

  const _deviceInfoHtml = (node) => {
    const raw = state.latestNodesPayload?.nodes?.[node.payloadKey];
    const health = node.health || 'UNKNOWN';
    const hClass = getStatusClass('health', health);
    const displayName = raw ? _deviceFullName(raw, node.shownId) : node.fullName;
    // A displaced device goes without an alias, so it offers no renaming.
    const renameButton = node.ghost ? '' : `<button class="graph-info-icon-btn" id="graphInfoRename" aria-label="Rename device" title="Rename">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
        </button>`;

    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Device</span>
      <div class="graph-info-actions">
        ${renameButton}
        <button class="graph-info-icon-btn" id="graphInfoHide" aria-label="Hide device" title="Hide">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
        </button>
        <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
      </div>
    </div>
    <div class="graph-info-title" id="graphInfoTitle" data-uid="${escapeHtml(raw?.unique_id_hex || '')}">${escapeHtml(displayName)}</div>
    <div class="graph-info-meta">
      <span>${node.ghost ? 'Last ID' : 'ID'}: ${escapeHtml(String(node.shownId))}</span>
      <span class="graph-info-health ${hClass}">${escapeHtml(health)}</span>
      ${node.status ? `<span class="graph-info-status graph-info-status--${node.status.level}">${escapeHtml(node.status.text)}</span>` : ''}
    </div>`;

    // An online device's heartbeat: its mode (coloured when unusual, as in
    // the Nodes table), uptime and vendor-specific status code.
    if (raw && !node.disappeared) {
      const mode = getNodeModeValue(node.nodeId);
      const vssc = getNodeHeartbeatValue(node.nodeId, 'vssc');
      const facts = [
        mode ? `<span class="${getStatusClass('mode', mode)}">${escapeHtml(mode)}</span>` : '',
        raw.uptime != null ? `<span>up ${escapeHtml(formatUptime(raw.uptime))}</span>` : '',
        vssc != null ? `<span>VSSC ${escapeHtml(vssc)}</span>` : '',
      ].join('');
      if (facts) html += `<div class="graph-info-meta">${facts}</div>`;
    }
    html += '<div class="graph-info-buttons"><button type="button" class="graph-btn" id="graphInfoOpen">Open in Nodes</button></div>';

    if (raw) {
      // A port's own rate: what this device publishes, or what reaches it.
      const online = !node.disappeared;
      html += _portSectionHtml('Publishers', raw.publishers, (sid) => {
        const ev = online ? state.latestByNode.get(node.nodeId)?.get(sid) : null;
        return [_subjectShortType(sid, state.latestBySubject.get(sid)), ev, Number(ev?.rate) || 0];
      });
      html += _portSectionHtml('Subscribers', raw.subscribers, (sid) => {
        const ev = online ? state.latestBySubject.get(sid) : null;
        return [_subjectShortType(sid, state.latestBySubject.get(sid)), ev, getSubjectRate(ev)];
      });
      html += _portSectionHtml('Servers', raw.servers, (sid) => [_serviceShortName(sid)]);
      html += _portSectionHtml('Clients', raw.clients, (sid) => [_serviceShortName(sid)]);
    }

    return html;
  };

  // "uavcan.register.Access" for 384: the standard services have fixed IDs.
  const _serviceShortName = (sid) => {
    const type = typeof STANDARD_SERVICE_TYPES === 'object' ? STANDARD_SERVICE_TYPES[sid] : null;
    return type ? type.split('.').slice(-2).join('.') : '';
  };

  // One section of ports: ID, name and, where messages are judged, the rate
  // or that it has gone silent. describe(id) gives [name, message, rate].
  const _portSectionHtml = (title, ids, describe) => {
    if (!ids?.length) return '';
    const rows = ids.map((id) => {
      const [name, ev, rate] = describe(id);
      const rateHtml = !ev ? ''
        : _isFresh(ev, Date.now()) ? `<span class="graph-info-rate">${Number(rate).toFixed(1)} Hz</span>`
        : '<span class="graph-info-rate graph-info-status--warn">silent</span>';
      return `<div class="graph-info-row"><span class="graph-info-sid">${id}</span>`
        + `<span class="graph-info-mtype">${escapeHtml(name || '')}</span>${rateHtml}</div>`;
    }).join('');
    return `<div class="graph-info-section"><div class="graph-info-section-label">${title} (${ids.length})</div>${rows}</div>`;
  };

  // Over to the Nodes tab, with this device selected there.
  const _openInNodes = (node) => {
    switchView('nodes');
    setSelectedNode(node.nodeId ?? node.payloadKey);
  };

  // Over to the Subjects tab, with this subject's plot open.
  const _openPlot = (node) => {
    state._subjectsPlotSubject = node.subjectId;
    state.plotPaused = false;
    switchView('subjects');
  };

  const _hideFromGraph = (node) => {
    if (node.type === 'device') {
      // hideNode() takes a node-ID; a displaced device has only its key.
      if (typeof hideNode === 'function') hideNode(node.nodeId ?? node.payloadKey);
    } else if (typeof hideSubject === 'function') {
      hideSubject({ id: node.subjectId });
    }
    _selectNode(null);
    _render(deriveGraph());
    _renderHiddenBadge();
  };

  const _startRename = (node) => {
    const titleEl = document.getElementById('graphInfoTitle');
    if (!titleEl) return;
    const raw = state.latestNodesPayload?.nodes?.[node.payloadKey];
    const uid = raw?.unique_id;
    const current = (typeof getNodeAlias === 'function' && getNodeAlias(uid)) || raw?.name || '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'graph-info-rename';
    input.value = current;
    input.maxLength = 64;
    input.setAttribute('aria-label', 'Rename device');
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    let committed = false;
    const commit = (save) => {
      if (committed) return;
      committed = true;
      if (save && typeof setNodeAlias === 'function') setNodeAlias(uid, input.value);
      // Out of the input, or _renderInfo() keeps it as an edit in progress.
      input.blur();
      const id = node.id;
      _render(deriveGraph());
      _selectNode(id);
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(true); }
      else if (e.key === 'Escape') { e.preventDefault(); commit(false); }
    });
    input.addEventListener('blur', () => commit(true));
  };

  // A device listed in a subject's panel, by its /api/nodes key.
  const _deviceRowHtml = (key) => {
    const raw = state.latestNodesPayload?.nodes?.[key];
    const shownId = _shownNodeId(raw);
    return `<div class="graph-info-row"><span class="graph-info-sid">${escapeHtml(String(shownId))}</span><span class="graph-info-mtype">${escapeHtml(_deviceFullName(raw, shownId))}</span></div>`;
  };

  const _subjectInfoHtml = (node) => {
    const ev = state.latestBySubject.get(node.subjectId);
    const rateHtml = !ev ? ''
      : _isFresh(ev, Date.now()) ? `<span>${getSubjectRate(ev).toFixed(1)} msg/s</span>`
      : '<span class="graph-info-status graph-info-status--warn">silent</span>';
    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Subject</span>
      <div class="graph-info-actions">
        <button class="graph-info-icon-btn" id="graphInfoHide" aria-label="Hide subject" title="Hide">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
        </button>
        <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
      </div>
    </div>
    <div class="graph-info-title">${escapeHtml(node.fullType || `Subject ${node.subjectId}`)}</div>
    <div class="graph-info-meta">
      <span>ID: ${node.subjectId}</span>
      ${rateHtml}
      ${node.status ? `<span class="graph-info-status graph-info-status--warn">${escapeHtml(node.status.text)}</span>` : ''}
    </div>
    <div class="graph-info-buttons"><button type="button" class="graph-btn" id="graphInfoPlot">Plot in Subjects</button></div>`;

    if (node.pubs?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Publishers</div>`;
      html += node.pubs.map(_deviceRowHtml).join('');
      html += '</div>';
    }
    if (node.subs?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Subscribers</div>`;
      html += node.subs.map(_deviceRowHtml).join('');
      html += '</div>';
    }

    if (ev?.attributes?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Attributes</div>`;
      html += ev.attributes.map(a =>
        `<div class="graph-info-row"><span class="graph-info-sid">${escapeHtml(a.attribute)}</span><span class="graph-info-mtype">${escapeHtml(String(a.value))}${a.unit ? ' ' + escapeHtml(a.unit) : ''}</span></div>`
      ).join('');
      html += '</div>';
    }

    return html;
  };

  // ── Live refresh ──

  const _snapshotKey = () => {
    const p = state.latestNodesPayload;
    if (!p?.nodes) return '';
    const parts = [];
    for (const [nid, node] of Object.entries(p.nodes)) {
      parts.push(`${nid}:${node.publishers?.join(',') || ''}:${node.subscribers?.join(',') || ''}:${node.has_disappeared}`);
    }
    return parts.sort().join('|');
  };

  const _refresh = () => {
    if (state.activeView !== 'graph') return;
    const snap = _snapshotKey();
    if (snap === prevSnapshot) {
      // Update health/rate visuals without full re-render
      _updateVisuals();
      return;
    }
    _render(deriveGraph());
  };

  const _updateVisuals = () => {
    gNodes.selectAll('.graph-node--device').each(function(d) {
      const raw = state.latestNodesPayload?.nodes?.[d.payloadKey];
      d.health = getNodeHealthValue(d.nodeId);
      d.disappeared = raw?.has_disappeared || false;
      d.status = _deviceStatus(raw, d.nodeId);
      d.label = _deviceDisplayLabel(raw, d.shownId);
      d.fullName = _deviceFullName(raw, d.shownId);
      _paintDevice.call(this, d);
    });
    // A subject's type is known from its first message on.
    gNodes.selectAll('.graph-node--subject').each(function(d) {
      const ev = state.latestBySubject.get(d.subjectId);
      d.label = _subjectLabel(d.subjectId, ev);
      d.fullType = _subjectFullType(d.subjectId, ev);
      d.status = _subjectStatus(d.subjectId, d.pubs, d.subs);
      _paintSubject.call(this, d);
    });
    gLabels.selectAll('.graph-label').each(_paintLabel).attr('y', d => _labelY(d));

    gLinks.selectAll('.graph-link').each(_paintLink);

    if (gState.showLinkStats) {
      gLinkLabels.selectAll('.graph-link-label').text(d => _linkStatText(d));
    }

    _renderHiddenBadge();
    _renderStatusStrip();
    if (!gState.selectedId) _applyFocus();

    if (gState.selectedId) _renderInfo(gState.selectedId);
  };

  const _startRefresh = () => {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(_refresh, REFRESH_MS);
  };

  // One SVG unit per screen pixel, as the layout and the info panel's placement
  // assume. A hidden tab has no size; it keeps the last one.
  const _syncViewBox = () => {
    const wrap = svg?.node()?.parentNode;
    if (!wrap?.clientWidth || !wrap.clientHeight) return;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    svg.attr('viewBox', `0 0 ${w} ${h}`);
    if (simulation) simulation.force('center', d3.forceCenter(w / 2, h / 2).strength(0.03));
  };

  // ── Public API ──

  const show = () => {
    // Shown before init(), so that the first layout has the real size.
    const root = document.getElementById('graphContainer');
    if (root) root.classList.remove('hidden');
    if (!gState.initialized) init();
    _startRefresh();
    _syncViewBox();
    _refresh();
  };

  const hide = () => {
    const root = document.getElementById('graphContainer');
    if (root) root.classList.add('hidden');
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  };

  return { init, show, hide };
})();
