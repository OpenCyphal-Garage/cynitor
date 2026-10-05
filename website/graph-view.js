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
    showServices: true,
    layout: 'layered',
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

  let svg, container, simulation, gLinks, gLinkHits, gNodes, gLabels, gLinkLabels;
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
      showServices: gState.showServices,
      layout: gState.layout,
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
      if (typeof raw.showServices === 'boolean') gState.showServices = raw.showServices;
      if (raw.layout === 'layered' || raw.layout === 'force') gState.layout = raw.layout;
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

    // Nodes heard only in Cyphal v1.1, which this dashboard cannot decode:
    // drawn so as not to be missed, with nothing known of their ports.
    const knownIds = new Set(Object.values(nodes).map((n) => n.node_id));
    for (const nid of _v11Nodes()) {
      if (knownIds.has(nid) || hiddenNodeKeys.has(`nid:${nid}`)) continue;
      deviceNodes.push({
        id: `dev:v11:${nid}`, nodeId: nid, payloadKey: null, shownId: nid, ghost: false, v11: true,
        type: 'device', label: 'Cyphal v1.1 node', fullName: 'Cyphal v1.1 node',
        uniqueId: null, stableKey: `nid:${nid}`, health: null, disappeared: false, status: V11_STATUS,
      });
    }

    for (const [sid, meta] of subjectSet) {
      const ev = state.latestBySubject.get(sid);
      subjectNodes.push({
        id: `sub:${sid}`,
        subjectId: sid,
        type: 'subject',
        label: _subjectLabel(sid),
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

    // Service calls: a client of a service to each online node that serves it,
    // as the backend matches them; one edge per pair, however many services.
    const serviceLinks = [];
    if (gState.showServices) {
      const servers = new Map();  // service-ID -> keys of the online nodes serving it
      for (const [key, node] of Object.entries(nodes)) {
        if (node.has_disappeared || node.node_id == null || _isHiddenNode(node)) continue;
        for (const sid of node.servers || []) {
          if (!servers.has(sid)) servers.set(sid, []);
          servers.get(sid).push(key);
        }
      }
      const pairs = new Map();
      for (const [key, node] of Object.entries(nodes)) {
        if (node.node_id == null || _isHiddenNode(node)) continue;
        for (const sid of node.clients || []) {
          if (gState.hideSystem && _isSystemService(sid)) continue;
          for (const serverKey of servers.get(sid) || []) {
            if (serverKey === key) continue;
            const pair = `${key}|${serverKey}`;
            if (!pairs.has(pair)) {
              pairs.set(pair, { source: `dev:${key}`, target: `dev:${serverKey}`, type: 'svc', services: [] });
            }
            pairs.get(pair).services.push(sid);
          }
        }
      }
      serviceLinks.push(...pairs.values());
      for (const link of serviceLinks) {
        addAdj(link.source, link.target);
        addAdj(link.target, link.source);
      }
    }

    // In "Nodes only" a device's drawn neighbours are devices: the filter and
    // the highlight must reach them, not just the (undrawn) subjects between.
    if (!_showSubs()) {
      for (const link of collapsedLinks) {
        addAdj(link.source, link.target);
        addAdj(link.target, link.source);
      }
    }

    return { deviceNodes, subjectNodes, links, collapsedLinks, serviceLinks, adjacency };
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
      .classed('graph-link--silent', silent)
      .classed('graph-link--svc', d.type === 'svc');
  };

  // ── Rate history ──
  //
  // The last minute of each drawn data edge's rate, sampled once a second
  // while the tab is open, for the inspector's sparklines.
  const SPARK_SAMPLES = 60;
  const rateHistory = new Map();  // link key -> rates, oldest first

  const _recordRates = (links) => {
    const seen = new Set();
    for (const link of links) {
      if (link.type !== 'pub' && link.type !== 'sub') continue;
      const key = _linkKey(link);
      seen.add(key);
      const rates = rateHistory.get(key) || [];
      rates.push(_linkTraffic(link).rate);
      if (rates.length > SPARK_SAMPLES) rates.shift();
      rateHistory.set(key, rates);
    }
    for (const key of rateHistory.keys()) if (!seen.has(key)) rateHistory.delete(key);
  };

  // A sparkline of an edge's rate, newest at the right, scaled to its own
  // peak with room above it, so that a steady rate is a line, not the edge
  // of the box; nothing until there are two samples.
  const _sparkHtml = (key) => {
    const rates = rateHistory.get(key);
    if (!rates || rates.length < 2) return '';
    const peak = Math.max(...rates, 0.1) * 1.4;
    const start = SPARK_SAMPLES - rates.length;
    const points = rates.map((v, i) => `${start + i},${(15 - (v / peak) * 14).toFixed(1)}`).join(' ');
    return `<svg class="graph-spark" viewBox="0 0 ${SPARK_SAMPLES - 1} 16" preserveAspectRatio="none"`
      + ` aria-hidden="true"><polyline points="${points}"/></svg>`;
  };

  // An edge's identity in a data join: a service edge and a data edge may
  // join the same two devices.
  const _linkKey = (d) => {
    const src = typeof d.source === 'object' ? d.source.id : d.source;
    const tgt = typeof d.target === 'object' ? d.target.id : d.target;
    return `${d.type}:${src}|${tgt}`;
  };

  // Publisher and device-to-device edges point at whoever receives the data;
  // a service edge, from client to server.
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
      .classed('graph-node--v11', !!d.v11)
      .call((g) => g.select('title').text(`${d.fullName} (node ${d.shownId})`))
      .select('.graph-health-badge')
      .text(health ? HEALTH_ICONS[String(d.health).toUpperCase()] || '' : '');
  };

  // Recent lifecycle events the backend has noticed, by node-ID: a restart,
  // two nodes on one node-ID, a subject published with another type. Polled
  // while the tab is open; the times are on the server's clock.
  const EVENTS_MS = 5000;
  const EVENT_TYPES = 'restart_suspected,node_id_conflict,type_conflict';
  let recentEvents = new Map();  // node-ID -> { event type -> its latest timestamp }
  let serverClockOffset = 0;     // the server's clock minus ours, in seconds
  let eventsTimer = null;

  const _fetchEvents = async () => {
    if (state.activeView !== 'graph' || !state.dashboardConnected) return;
    try {
      const data = await requestJson(`/api/nodes/events?range=15m&types=${EVENT_TYPES}`);
      serverClockOffset = data.now_unix - Date.now() / 1000;
      const byNode = new Map();
      for (const ev of data.events || []) {  // newest first: keep the first of each type
        const types = byNode.get(ev.node_id) || {};
        if (!(ev.event_type in types)) types[ev.event_type] = ev.timestamp_unix;
        byNode.set(ev.node_id, types);
      }
      recentEvents = byNode;
    } catch {
      recentEvents = new Map();  // no event logger, or no backend: nothing to mark
    }
  };

  // "3m ago" for a time on the server's clock.
  const _serverAgo = (unix) => formatLastSeen([new Date((unix - serverClockOffset) * 1000).toISOString()]);

  // What is unusual about a device, said under its name, the gravest first:
  // since when it has been offline, a node-ID it shares with another node, a
  // recent restart, a subject published with another type, a mode other than
  // OPERATIONAL. Null when all is usual.
  const _deviceStatus = (raw, nodeId) => {
    if (raw?.has_disappeared) {
      const ago = typeof formatLastSeen === 'function' ? formatLastSeen(raw.last_seen) : '-';
      return { text: ago === '-' ? 'offline' : `offline · ${ago}`, level: 'err', kind: 'offline' };
    }
    const events = recentEvents.get(nodeId);
    if (events?.node_id_conflict) return { text: 'node-ID conflict', level: 'err', kind: 'conflict' };
    if (events?.restart_suspected) {
      return { text: `restarted ${_serverAgo(events.restart_suspected)}`, level: 'warn', kind: 'restart' };
    }
    if (events?.type_conflict) return { text: 'type conflict', level: 'warn', kind: 'typeconflict' };
    const mode = getNodeModeValue(nodeId);
    return mode && getStatusClass('mode', mode) ? { text: mode, level: 'warn', kind: 'mode' } : null;
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
    d3.select(this).classed('graph-node--warn', !!d.status)
      .select('title').text(`Subject ${d.subjectId}: ${d.fullType || 'type not known yet'}`);
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

  // A subject is labelled by its ID, which tells it apart where many share
  // a type; the type is in its tooltip and the inspector.
  const _subjectLabel = (sid) => String(sid);

  const _linkStatText = (link) => {
    const { silent, rate, payload } = _linkTraffic(link);
    if (silent) return 'silent';
    const parts = [];
    if (rate > 0) parts.push(`${Math.round(rate)} Hz`);
    if (payload > 0) parts.push(`${payload} B`);
    return parts.join(' · ');
  };

  // ── Cyphal v1.1 ──
  //
  // /api/status reports the node-IDs Cyphal v1.1 traffic came from (and,
  // bus-wide, its subject-IDs), only while a CAN session runs.
  const V11_STATUS = { text: 'v1.1 · not decoded', level: 'warn', kind: 'v11' };
  const _v11Nodes = () => (state.canConnected && state.cyphalV11?.nodes) || [];

  // ── Layered layout ──
  //
  // Devices in one band, subjects in the other, each ordered by where its
  // neighbours across the gap stand (a few sweeps of the barycenter
  // heuristic), which takes most crossings out. A band longer than the
  // canvas is wide wraps into rows stacked away from the gap, so that the
  // whole fits at about full size. The same bus is laid out the same way every
  // time. "Nodes only" has no bands and keeps the force layout.
  const BANDS = {
    device: { gap: 150, rowGap: 90 },
    subject: { gap: 64, rowGap: 64 },
  };
  const BAND_MARGIN = 80;
  const LABEL_CHAR = 6.6;  // px per character of a label, monospaced
  const BAND_GAP = 220;
  const _isLayered = () => gState.layout === 'layered' && _showSubs();

  const _layoutLayered = (nodes, links, subjectsOnTop, width, height) => {
    const endId = (n) => (typeof n === 'object' ? n.id : n);
    const neighbours = new Map(nodes.map((n) => [n.id, []]));
    for (const l of links) {
      if (l.type !== 'pub' && l.type !== 'sub') continue;
      neighbours.get(endId(l.source))?.push(endId(l.target));
      neighbours.get(endId(l.target))?.push(endId(l.source));
    }
    let devices = nodes.filter((n) => n.type === 'device')
      .sort((a, b) => (a.ghost - b.ghost) || (Number(a.shownId) - Number(b.shownId)));
    let subjects = nodes.filter((n) => n.type === 'subject').sort((a, b) => a.subjectId - b.subjectId);
    // Positions as fractions of a band, so that bands of any length compare.
    const fractions = (list) => new Map(list.map((n, i) => [n.id, list.length > 1 ? i / (list.length - 1) : 0.5]));
    const reorder = (list, across) => {
      const there = fractions(across);
      const here = fractions(list);
      const key = (n) => {
        const xs = neighbours.get(n.id).map((id) => there.get(id)).filter((x) => x != null);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : here.get(n.id);
      };
      return [...list].sort((a, b) => (key(a) - key(b)) || (here.get(a.id) - here.get(b.id)));
    };
    for (let sweep = 0; sweep < 4; sweep++) {
      subjects = reorder(subjects, devices);
      devices = reorder(devices, subjects);
    }
    // Each node gets the room its longest label line needs, the band's gap at
    // least; a row ends where the next node would pass the canvas edge.
    const roomOf = (n, band) => Math.max(band.gap,
      Math.max(String(n.label || '').length, n.status?.text.length || 0) * LABEL_CHAR + 16);
    const place = (list, band, gapY, away) => {
      const rows = [[]];
      let used = 0;
      for (const n of list) {
        const room = roomOf(n, band);
        if (rows[rows.length - 1].length && used + room > width - 2 * BAND_MARGIN) {
          rows.push([]);
          used = 0;
        }
        rows[rows.length - 1].push(n);
        used += room;
      }
      rows.forEach((row, r) => {
        let x = width / 2 - row.reduce((sum, n) => sum + roomOf(n, band), 0) / 2;
        for (const n of row) {
          const room = roomOf(n, band);
          n.tx = x + room / 2;
          n.ty = gapY + away * r * band.rowGap;
          x += room;
        }
      });
    };
    const [top, bottom] = subjectsOnTop ? [subjects, devices] : [devices, subjects];
    // The gap grows with the canvas: edges between the bands get less steep.
    const gap = Math.max(BAND_GAP, height * 0.4);
    place(top, BANDS[subjectsOnTop ? 'subject' : 'device'], height / 2 - gap / 2, -1);
    place(bottom, BANDS[subjectsOnTop ? 'device' : 'subject'], height / 2 + gap / 2, 1);
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
    if (!simulation || _isLayered()) return;  // the layers place every node
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
      <input type="text" id="graphFilter" class="graph-filter" placeholder="Filter by id, name, type; Enter goes to it" aria-label="Filter graph; Enter goes to the first match" />
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
          <label class="graph-toggle">
            <input type="checkbox" id="graphShowServices" ${gState.showServices ? 'checked' : ''} />
            <span>Service calls</span>
          </label>
          <div class="graph-display-row">
            <button type="button" class="graph-btn" id="graphExportSvg">Export SVG</button>
            <button type="button" class="graph-btn" id="graphExportPng">Export PNG</button>
          </div>
          <div class="graph-select-group">
            <label for="graphLayout">Layout</label>
            <select id="graphLayout" class="graph-select" aria-label="Layout">
              <option value="layered">layered</option>
              <option value="force">force</option>
            </select>
          </div>
          <div class="graph-select-group">
            <label for="graphGravity">Gravity</label>
            <select id="graphGravity" class="graph-select" aria-label="Gravity metric">
              ${GRAVITY_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
            </select>
          </div>
        </div>
      </details>
      <details class="graph-display" id="graphLegend">
        <summary class="graph-btn" aria-label="Legend">?</summary>
        <div class="graph-display-menu graph-legend-menu">
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device"></span>device</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--subject"></span>subject</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--warn"></span>advisory ~, caution !</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--err"></span>warning !!</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--offline"></span>offline</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--v11"></span>Cyphal v1.1, not decoded</span>
          <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device graph-legend-dot--pinned"></span>pinned</span>
          <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--live"></span>traffic, by rate</span>
          <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--silent"></span>silent</span>
          <span class="graph-legend-item"><span class="graph-legend-line"></span>no data</span>
          <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--svc"></span>service call</span>
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


    document.getElementById('graphFilter').value = gState.filterText || '';
    document.getElementById('graphGravity').value = gState.gravityMetric || 'none';
    document.getElementById('graphLayout').value = gState.layout;
    document.getElementById('graphView').value = gState.view || 'node-centric';

    // The canvas, and beside it the inspector of the selected node: docked,
    // so that it narrows the canvas rather than covering part of it.
    const body = document.createElement('div');
    body.className = 'graph-body';
    root.appendChild(body);
    const svgWrap = document.createElement('div');
    svgWrap.className = 'graph-svg-wrap';
    body.appendChild(svgWrap);
    const tooltip = document.createElement('div');
    tooltip.className = 'graph-tooltip hidden';
    tooltip.id = 'graphTooltip';
    tooltip.setAttribute('role', 'tooltip');
    svgWrap.appendChild(tooltip);
    const info = document.createElement('aside');
    info.className = 'graph-info hidden';
    info.id = 'graphInfo';
    info.setAttribute('aria-label', 'Selected node');
    body.appendChild(info);
    // One listener for the panel's buttons: the panel is refreshed in place,
    // so a button outlives any single render.
    info.addEventListener('click', (e) => {
      // A port or device row: select that node, if it is drawn.
      const target = e.target.closest('[data-select]')?.dataset.select;
      if (target) { _selectNode(target); _ensureVisible(target); return; }
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
      .attr('markerUnits', 'userSpaceOnUse')
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,0 L10,3 L0,6').attr('fill', 'var(--muted)');
    defs.append('marker')
      .attr('id', 'graph-arrow-pub-hi')
      .attr('viewBox', '0 0 10 6')
      .attr('refX', 10).attr('refY', 3)
      .attr('markerUnits', 'userSpaceOnUse')
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
    // Edges are thin; a wider, invisible twin of each takes the pointer.
    gLinkHits = container.append('g').attr('class', 'graph-link-hits');
    gNodes = container.append('g').attr('class', 'graph-nodes');
    gLabels = container.append('g').attr('class', 'graph-labels');
    gLinkLabels = container.append('g').attr('class', 'graph-link-labels');

    // Zoom
    zoomBehavior = d3.zoom()
      .scaleExtent([0.1, 4])
      .on('zoom', (e) => {
        if (e.sourceEvent) fitPending = false;  // the user has placed the view
        container.attr('transform', e.transform);
        _sizeArrows(e.transform.k);
        _hideTooltip();
        gState.zoom = { k: e.transform.k, x: e.transform.x, y: e.transform.y };
        save();
      });
    svg.call(zoomBehavior);

    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => _syncViewBox());
      ro.observe(svgWrap);
    }

    _sizeArrows(1);
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
    document.getElementById('graphShowServices').addEventListener('change', (e) => {
      gState.showServices = e.target.checked;
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphLayout').addEventListener('change', (e) => {
      gState.layout = e.target.value;
      fitPending = true;  // the nodes move somewhere else: show them all once settled
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphFit').addEventListener('click', () => _fitToView());
    document.getElementById('graphExportSvg').addEventListener('click', () => _exportImage('svg'));
    document.getElementById('graphExportPng').addEventListener('click', () => _exportImage('png'));
    // The Display and Legend menus close on a click elsewhere, or on Escape.
    const menus = [...root.querySelectorAll('details.graph-display')];
    document.addEventListener('click', (e) => {
      for (const menu of menus) if (menu.open && !menu.contains(e.target)) menu.open = false;
    });
    for (const menu of menus) {
      menu.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !menu.open) return;
        e.stopPropagation();
        menu.open = false;
        menu.querySelector('summary').focus();
      });
    }
    // Escape closes the inspector, unless typing in a field.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || state.activeView !== 'graph' || !gState.selectedId) return;
      if (e.target.closest?.('input, textarea, select')) return;
      _selectNode(null);
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
    document.getElementById('graphFilter').addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      _goToMatch(e.target.value);
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
    const { deviceNodes, subjectNodes, links, collapsedLinks, serviceLinks, adjacency } = graph;

    // Not connected, or no node on the bus yet: say so, not an empty canvas.
    const noNodes = !Object.keys(state.latestNodesPayload?.nodes || {}).length && !_v11Nodes().length;
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
    const allLinks = [...(showSubs ? links : collapsedLinks), ...serviceLinks].filter(l => {
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
    if (_isLayered()) {
      // Each node eases to its place in the layers. The link force stays, at
      // no strength, for it also turns the links' IDs into their nodes.
      _layoutLayered(allNodes, allLinks, subjectsOnTop, width, height);
      simulation.force('link', d3.forceLink(allLinks).id(d => d.id).strength(0))
        .force('charge', null).force('collide', null).force('center', null)
        .force('bipartite', null).force('gravity', null)
        .force('lx', d3.forceX(d => d.tx).strength(0.3))
        .force('ly', d3.forceY(d => d.ty).strength(0.3));
      simulation.alpha(0.5).restart();
    } else {
      simulation.force('lx', null).force('ly', null)
        .force('charge', d3.forceManyBody().strength(-160))
        .force('collide', d3.forceCollide().radius(d => d.type === 'device' ? 32 : 22))
        .force('center', d3.forceCenter(width / 2, height / 2).strength(0.03));
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
    }

    // Links
    const linkSel = gLinks.selectAll('.graph-link').data(allLinks, _linkKey);
    linkSel.exit().remove();
    const linkEnter = linkSel.enter().append('line')
      .attr('class', 'graph-link')
      .attr('marker-end', d => _hasArrow(d) ? 'url(#graph-arrow-pub)' : null);
    linkEnter.merge(linkSel).each(_paintLink);

    const hitSel = gLinkHits.selectAll('.graph-link-hit').data(allLinks, _linkKey);
    hitSel.exit().remove();
    hitSel.enter().append('line')
      .attr('class', 'graph-link-hit')
      .on('mouseenter', (e, d) => _showLinkTooltip(e, d))
      .on('mousemove', (e) => _moveTooltip(e))
      .on('mouseleave', () => _hideTooltip());

    // Nodes
    const nodeSel = gNodes.selectAll('.graph-node').data(allNodes, d => d.id);
    nodeSel.exit().remove();
    const nodeEnter = nodeSel.enter().append('g')
      .attr('class', d => `graph-node graph-node--${d.type}`)
      .call(_dragBehavior())
      .on('click', (e, d) => { e.stopPropagation(); _selectNode(d.id); })
      .on('mouseenter', (e, d) => _hoverNode(d.id))
      .on('mouseleave', () => _hoverNode(null))
      // Reachable without a mouse: Tab to a node, Enter or Space selects it.
      .attr('tabindex', 0)
      .attr('role', 'button')
      .on('keydown', (e, d) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        _selectNode(d.id);
      });

    nodeEnter.append('title');  // the full name or type, on hover
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
    merged.attr('aria-label', d => (d.type === 'device'
      ? `Device ${d.shownId}, ${d.fullName}${d.status ? `, ${d.status.text}` : ''}`
      : `Subject ${d.subjectId}${d.status ? `, ${d.status.text}` : ''}`));
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
    const sel = gLinkLabels.selectAll('.graph-link-label').data(data, _linkKey);
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

    gLinkHits.selectAll('.graph-link-hit')
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);

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

  // ── Search ──
  //
  // Enter in the filter goes to the best match: a node-ID or subject-ID
  // typed in full first, then a name or type containing the text.
  const _goToMatch = (text) => {
    const q = String(text).trim().toLowerCase();
    if (!q) return;
    const nodes = simulation?.nodes() || [];
    const exact = (n) => String(n.type === 'device' ? n.shownId : n.subjectId) === q;
    const partial = (n) => [n.fullName, n.label, n.fullType].some((v) => String(v || '').toLowerCase().includes(q));
    const match = nodes.find(exact) || nodes.find(partial);
    if (!match) return;
    _selectNode(match.id);
    const box = svg.node().viewBox.baseVal;
    const k = Math.max(d3.zoomTransform(svg.node()).k, 1.2);
    const t = d3.zoomIdentity.translate(box.width / 2 - k * match.x, box.height / 2 - k * match.y).scale(k);
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    (still ? svg : svg.transition().duration(400)).call(zoomBehavior.transform, t);
  };

  // ── Export ──
  //
  // The drawing as it stands, as an SVG or PNG file. Its looks come from the
  // page's style sheet and theme, which a file does not have: each element's
  // computed paint is written onto it. Hover targets and the grid stay out.
  const EXPORT_PROPS = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity', 'stroke-dasharray',
    'stroke-linecap', 'stroke-linejoin', 'paint-order', 'opacity', 'font-family', 'font-size', 'font-weight',
    'text-anchor', 'dominant-baseline', 'vector-effect', 'visibility'];
  const EXPORT_MARGIN = 24;

  const _exportSvgText = () => {
    const source = svg.node();
    const copy = source.cloneNode(true);
    const originals = source.querySelectorAll('*');
    copy.querySelectorAll('*').forEach((el, i) => {
      const style = getComputedStyle(originals[i]);
      el.setAttribute('style', EXPORT_PROPS.map((p) => `${p}:${style.getPropertyValue(p)}`).join(';'));
    });
    copy.querySelectorAll('marker').forEach((m) => { m.setAttribute('markerWidth', 7); m.setAttribute('markerHeight', 4.2); });
    copy.querySelector('.graph-link-hits')?.remove();
    copy.querySelector('.graph-grid-bg')?.remove();
    // Framed on the drawing, not on the canvas, at its own scale.
    const layer = copy.querySelector('.graph-layer');
    layer.removeAttribute('transform');
    const parts = ['.graph-links', '.graph-nodes', '.graph-labels'].map((sel) => container.select(sel).node().getBBox());
    const x0 = Math.min(...parts.map((b) => b.x)) - EXPORT_MARGIN;
    const y0 = Math.min(...parts.map((b) => b.y)) - EXPORT_MARGIN;
    const x1 = Math.max(...parts.map((b) => b.x + b.width)) + EXPORT_MARGIN;
    const y1 = Math.max(...parts.map((b) => b.y + b.height)) + EXPORT_MARGIN;
    copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    copy.setAttribute('viewBox', `${x0} ${y0} ${x1 - x0} ${y1 - y0}`);
    copy.setAttribute('width', Math.round(x1 - x0));
    copy.setAttribute('height', Math.round(y1 - y0));
    const background = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    for (const [k, v] of Object.entries({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 })) background.setAttribute(k, v);
    background.setAttribute('fill', getComputedStyle(source.parentNode).backgroundColor);
    copy.insertBefore(background, layer);
    return { text: new XMLSerializer().serializeToString(copy), width: x1 - x0, height: y1 - y0 };
  };

  const _download = (blob, name) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  const _exportImage = (format) => {
    if (!simulation?.nodes().length) return;
    const { text, width, height } = _exportSvgText();
    const name = `cynitor-graph-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`;
    const svgBlob = new Blob([text], { type: 'image/svg+xml' });
    if (format === 'svg') { _download(svgBlob, `${name}.svg`); return; }
    // PNG at twice the size, for sharp text on a report or a ticket.
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(width * 2);
      canvas.height = Math.round(height * 2);
      const ctx = canvas.getContext('2d');
      ctx.scale(2, 2);
      ctx.drawImage(image, 0, 0, width, height);
      URL.revokeObjectURL(image.src);
      canvas.toBlob((png) => png && _download(png, `${name}.png`), 'image/png');
    };
    image.src = URL.createObjectURL(svgBlob);
  };

  // ── Edge tooltip ──
  //
  // What an edge carries, shown while the pointer is on it: the subject and
  // its type, who publishes or receives it, and its rate, or that it has gone
  // silent; for a service edge, the services one node may call on the other.
  const _deviceText = (d) => `${d.shownId} ${d.fullName}`;
  const _subjectText = (d) => `subject ${d.subjectId}${d.fullType ? ` · ${d.fullType}` : ''}`;

  const _linkTooltipHtml = (d) => {
    const { live, silent, rate } = _linkTraffic(d);
    const flow = silent ? '<span class="graph-info-status--warn">silent</span>'
      : live ? `${rate.toFixed(1)} Hz` : '<span class="graph-tooltip-muted">no data</span>';
    const lines = {
      pub: [_subjectText(d.target), `published by ${_deviceText(d.source)}`],
      sub: [_subjectText(d.source), `received by ${_deviceText(d.target)}`],
      dev: [`${_deviceText(d.source)} → ${_deviceText(d.target)}`, `subjects ${(d.subjects || []).join(', ')}`],
      svc: [`${_deviceText(d.source)} calls ${_deviceText(d.target)}`,
        `services ${(d.services || []).map((id) => [id, _serviceShortName(id)].filter(Boolean).join(' ')).join(', ')}`],
    }[d.type] || [];
    return lines.map((line) => `<div>${escapeHtml(line)}</div>`).join('')
      + (d.type === 'svc' ? '' : `<div>${flow}</div>`);
  };

  const _showLinkTooltip = (e, d) => {
    const tooltip = document.getElementById('graphTooltip');
    if (!tooltip || e.buttons) return;  // not while dragging or panning
    tooltip.innerHTML = _linkTooltipHtml(d);
    tooltip.classList.remove('hidden');
    gLinks.selectAll('.graph-link').classed('graph-link--hover', (l) => l === d);
    _moveTooltip(e);
  };

  // Beside the pointer, kept inside the canvas.
  const _moveTooltip = (e) => {
    const tooltip = document.getElementById('graphTooltip');
    const wrap = tooltip?.parentNode;
    if (!tooltip || tooltip.classList.contains('hidden') || !wrap) return;
    const box = wrap.getBoundingClientRect();
    const x = Math.min(e.clientX - box.left + 14, box.width - tooltip.offsetWidth - 8);
    const y = Math.min(e.clientY - box.top + 14, box.height - tooltip.offsetHeight - 8);
    tooltip.style.left = `${Math.max(8, x)}px`;
    tooltip.style.top = `${Math.max(8, y)}px`;
  };

  const _hideTooltip = () => {
    document.getElementById('graphTooltip')?.classList.add('hidden');
    gLinks?.selectAll('.graph-link--hover').classed('graph-link--hover', false);
  };

  // ── Status strip ──
  //
  // What needs a look, counted over what is drawn. A count, clicked, picks
  // those nodes out of the graph; clicked again, it lets them go.
  const FOCUS_KINDS = [
    { key: 'offline', label: 'offline', level: 'err', test: (d) => d.type === 'device' && d.disappeared && !d.ghost },
    { key: 'displaced', label: 'displaced', level: 'err', test: (d) => d.type === 'device' && d.ghost },
    { key: 'health', label: 'unusual health', level: 'warn',
      test: (d) => d.type === 'device' && !d.disappeared && !!getStatusClass('health', d.health) },
    { key: 'conflict', label: 'node-ID conflict', level: 'err', test: (d) => d.type === 'device' && d.status?.kind === 'conflict' },
    { key: 'restart', label: 'restarted', level: 'warn', test: (d) => d.type === 'device' && d.status?.kind === 'restart' },
    { key: 'typeconflict', label: 'type conflict', level: 'warn', test: (d) => d.type === 'device' && d.status?.kind === 'typeconflict' },
    { key: 'v11', label: 'Cyphal v1.1, not decoded', level: 'warn', test: (d) => d.type === 'device' && !!d.v11 },
    { key: 'mode', label: 'unusual mode', level: 'warn', test: (d) => d.type === 'device' && d.status?.kind === 'mode' },
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
    // The inspector has just opened or closed: measure the canvas now, and
    // keep the selected node clear of the inspector's edge.
    _syncViewBox();
    if (id) _ensureVisible(id);
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
  };

  // Arrowheads keep one size on screen: 7 by 4 px at any zoom.
  const _sizeArrows = (k) => {
    svg?.selectAll('marker').attr('markerWidth', 7 / k).attr('markerHeight', 4.2 / k);
  };

  // Pans the view, when needed, so that a node is not hidden at its edge,
  // as when the inspector opens beside it and narrows the canvas.
  const _ensureVisible = (id) => {
    const node = simulation?.nodes().find((n) => n.id === id);
    const box = svg?.node()?.viewBox.baseVal;
    if (!node || !box?.width) return;
    const t = d3.zoomTransform(svg.node());
    const [x, y] = [t.applyX(node.x), t.applyY(node.y)];
    const margin = 60;
    if (x > margin && x < box.width - margin && y > margin && y < box.height - margin) return;
    svg.transition().duration(300).call(zoomBehavior.translateTo, node.x, node.y);
  };

  // A v1.1 node: its ID, and what the bus as a whole has shown of v1.1.
  const _v11InfoHtml = (node) => {
    const v11 = state.cyphalV11 || {};
    const subjects = (v11.subject_ids || []).join(', ') + (v11.subject_count > (v11.subject_ids || []).length ? ', …' : '');
    const last = v11.last_seen_unix ? formatLastSeen([new Date(v11.last_seen_unix * 1000).toISOString()]) : '-';
    return `<div class="graph-info-header">
      <span class="graph-info-type">Device</span>
      <div class="graph-info-actions">
        <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
      </div>
    </div>
    <div class="graph-info-title">Cyphal v1.1 node</div>
    ${_factsHtml([
      ['Node-ID', escapeHtml(String(node.shownId))],
      ['Status', `<span class="graph-info-status--warn">${escapeHtml(V11_STATUS.text)}</span>`],
      ['v1.1 subjects', escapeHtml(subjects || '-')],
      ['Last v1.1', escapeHtml(last)],
    ])}
    <p class="graph-info-note">This dashboard speaks Cyphal v1.0. A v1.1 device's topics show here
      only when it pins them to a v1.0 subject-ID; until then its ports and health are not known.</p>`;
  };

  const _deviceInfoHtml = (node) => {
    if (node.v11) return _v11InfoHtml(node);
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
`;

    // Its status, then an online device's heartbeat: health and mode
    // (coloured when unusual, as in the Nodes table), uptime and VSSC.
    const online = raw && !node.disappeared;
    const mode = online ? getNodeModeValue(node.nodeId) : null;
    const vssc = online ? getNodeHeartbeatValue(node.nodeId, 'vssc') : null;
    html += _factsHtml([
      [node.ghost ? 'Last ID' : 'Node-ID', escapeHtml(String(node.shownId))],
      node.status && ['Status', `<span class="graph-info-status--${node.status.level}">${escapeHtml(node.status.text)}</span>`],
      online && ['Health', `<span class="graph-info-health ${hClass}">${escapeHtml(health)}</span>`],
      mode && ['Mode', `<span class="${getStatusClass('mode', mode)}">${escapeHtml(mode)}</span>`],
      online && raw.uptime != null && ['Uptime', escapeHtml(formatUptime(raw.uptime))],
      vssc != null && ['VSSC', escapeHtml(vssc)],
    ]);
    html += '<div class="graph-info-buttons"><button type="button" class="graph-btn" id="graphInfoOpen">Open in Nodes</button></div>';

    if (raw) {
      // A port's own rate: what this device publishes, or what reaches it.
      const online = !node.disappeared;
      html += _portSectionHtml('Publishers', raw.publishers, (sid) => {
        const ev = online ? state.latestByNode.get(node.nodeId)?.get(sid) : null;
        return [_subjectShortType(sid, state.latestBySubject.get(sid)), ev, Number(ev?.rate) || 0, `sub:${sid}`,
          `pub:dev:${node.payloadKey}|sub:${sid}`];
      });
      html += _portSectionHtml('Subscribers', raw.subscribers, (sid) => {
        const ev = online ? state.latestBySubject.get(sid) : null;
        return [_subjectShortType(sid, state.latestBySubject.get(sid)), ev, getSubjectRate(ev), `sub:${sid}`,
          `sub:sub:${sid}|dev:${node.payloadKey}`];
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
  // or that it has gone silent. describe(id) gives [name, message, rate, the
  // graph node the row selects, the edge whose rate history to draw].
  const _portSectionHtml = (title, ids, describe) => {
    if (!ids?.length) return '';
    const rows = ids.map((id) => {
      const [name, ev, rate, selects, edge] = describe(id);
      const rateHtml = !ev ? ''
        : _isFresh(ev, Date.now()) ? `<span class="graph-info-rate">${Number(rate).toFixed(1)} Hz</span>`
        : '<span class="graph-info-rate graph-info-status--warn">silent</span>';
      const cells = `<span class="graph-info-sid">${id}</span>`
        + `<span class="graph-info-mtype">${escapeHtml(name || '')}</span>${edge ? _sparkHtml(edge) : ''}${rateHtml}`;
      return _rowHtml(selects, cells);
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

  // A grid of label and value; a falsy entry is left out. Values are HTML,
  // escaped by the caller.
  const _factsHtml = (facts) => `<dl class="graph-info-facts">${facts.filter(Boolean)
    .map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('')}</dl>`;

  // A value for reading: floats to six significant digits.
  const _formatValue = (v) => (typeof v === 'number' && !Number.isInteger(v) ? String(Number(v.toPrecision(6))) : String(v));

  // A row of the inspector: a button selecting the node it names where that
  // node is drawn, plain text otherwise.
  const _rowHtml = (nodeId, cells) => {
    const drawn = nodeId && (simulation?.nodes() || []).some((n) => n.id === nodeId);
    return drawn
      ? `<button type="button" class="graph-info-row graph-info-row--link" data-select="${escapeHtml(nodeId)}">${cells}</button>`
      : `<div class="graph-info-row">${cells}</div>`;
  };

  // A device listed in a subject's panel, by its /api/nodes key.
  const _deviceRowHtml = (key) => {
    const raw = state.latestNodesPayload?.nodes?.[key];
    const shownId = _shownNodeId(raw);
    return _rowHtml(`dev:${key}`, `<span class="graph-info-sid">${escapeHtml(String(shownId))}</span>`
      + `<span class="graph-info-mtype">${escapeHtml(_deviceFullName(raw, shownId))}</span>`);
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
${_factsHtml([
      ['Subject-ID', String(node.subjectId)],
      node.status && ['Status', `<span class="graph-info-status--warn">${escapeHtml(node.status.text)}</span>`],
      rateHtml && ['Rate', rateHtml],
    ])}
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
        `<div class="graph-info-row"><span class="graph-info-sid">${escapeHtml(a.attribute)}</span><span class="graph-info-mtype">${escapeHtml(_formatValue(a.value))}${a.unit ? ' ' + escapeHtml(a.unit) : ''}</span></div>`
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
    return `${parts.sort().join('|')}|v11:${_v11Nodes().join(',')}`;
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
      if (d.v11) return;  // nothing changes about them but their presence
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
      d.label = _subjectLabel(d.subjectId);
      d.fullType = _subjectFullType(d.subjectId, ev);
      d.status = _subjectStatus(d.subjectId, d.pubs, d.subs);
      _paintSubject.call(this, d);
    });
    gLabels.selectAll('.graph-label').each(_paintLabel).attr('y', d => _labelY(d));

    gLinks.selectAll('.graph-link').each(_paintLink);
    _recordRates(gLinks.selectAll('.graph-link').data());

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
    if (simulation?.force('center')) simulation.force('center', d3.forceCenter(w / 2, h / 2).strength(0.03));
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
    if (!eventsTimer) {
      _fetchEvents();
      eventsTimer = setInterval(_fetchEvents, EVENTS_MS);
    }
  };

  const hide = () => {
    const root = document.getElementById('graphContainer');
    if (root) root.classList.add('hidden');
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    if (eventsTimer) { clearInterval(eventsTimer); eventsTimer = null; }
  };

  return { init, show, hide };
})();
