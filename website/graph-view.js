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
    hideSystem: false,
    filterText: '',
    gravityMetric: 'none',
    showGrid: true,
    showLinkStats: false,
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

    for (const node of Object.values(nodes)) {
      if (_isHiddenNode(node)) continue;
      const nid = node.node_id;
      const id = `dev:${nid}`;
      deviceNodes.push({
        id,
        nodeId: nid,
        type: 'device',
        label: _deviceDisplayLabel(node, nid),
        fullName: _deviceFullName(node, nid),
        uniqueId: node.unique_id || null,
        stableKey: typeof nodeStableKey === 'function' ? nodeStableKey(node) : null,
        health: getNodeHealthValue(nid),
        disappeared: !!node.has_disappeared,
      });

      for (const sid of node.publishers || []) {
        if (_isHiddenSubject(sid)) continue;
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).pubs.push(nid);
        links.push({ source: id, target: `sub:${sid}`, type: 'pub', subjectId: sid });
      }
      for (const sid of node.subscribers || []) {
        if (_isHiddenSubject(sid)) continue;
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).subs.push(nid);
        links.push({ source: `sub:${sid}`, target: id, type: 'sub', subjectId: sid });
      }
    }

    for (const [sid, meta] of subjectSet) {
      const ev = state.latestBySubject.get(sid);
      subjectNodes.push({
        id: `sub:${sid}`,
        subjectId: sid,
        type: 'subject',
        label: _truncate(ev?.message_type ? ev.message_type.split('.').pop() : `Subject ${sid}`),
        fullType: ev?.message_type || null,
        rate: ev?.rate ?? null,
        pubs: meta.pubs,
        subs: meta.subs,
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
      for (const pub of meta.pubs) {
        for (const sub of meta.subs) {
          if (pub === sub) continue;
          const src = `dev:${pub}`;
          const tgt = `dev:${sub}`;
          const key = `${src}|${tgt}`;
          if (!collapsedAdj.has(key)) {
            collapsedAdj.set(key, { source: src, target: tgt, subjects: [] });
          }
          collapsedAdj.get(key).subjects.push(sid);
        }
      }
    }
    for (const link of collapsedAdj.values()) collapsedLinks.push(link);

    return { deviceNodes, subjectNodes, links, collapsedLinks, adjacency };
  };

  // ── Traffic visualization helpers ──

  const _linkSubjectIds = (link) => {
    if (link.subjectId != null) return [link.subjectId];
    if (Array.isArray(link.subjects)) return link.subjects;
    return [];
  };

  const linkRate = (link) => {
    let total = 0;
    for (const sid of _linkSubjectIds(link)) {
      total += state.latestBySubject.get(sid)?.rate || 0;
    }
    return total;
  };

  const rateToWidth = (rate) => {
    if (!rate || rate <= 0) return 1;
    return Math.min(4, 1 + Math.log10(rate + 1) * 1.1);
  };

  const isLinkLive = (link) => {
    const now = Date.now() / 1000;
    for (const sid of _linkSubjectIds(link)) {
      const ev = state.latestBySubject.get(sid);
      if (!ev) continue;
      if ((ev.rate || 0) > 0) return true;
      if (ev.timestamp_unix && now - ev.timestamp_unix < 2) return true;
    }
    return false;
  };

  const isDeviceLive = (nodeId, disappeared) => {
    if (disappeared) return false;
    return typeof getNodeRate === 'function' && getNodeRate(nodeId) > 0;
  };

  const _deviceFullName = (raw, nid) => {
    const alias = typeof getNodeAlias === 'function' ? getNodeAlias(raw?.unique_id) : null;
    return alias || raw?.name || `Node ${nid}`;
  };

  const _deviceDisplayLabel = (raw, nid) => _truncate(_deviceFullName(raw, nid), 20);

  const _linkStatText = (link) => {
    let rate = 0, payload = 0;
    for (const sid of _linkSubjectIds(link)) {
      const ev = state.latestBySubject.get(sid);
      if (!ev) continue;
      rate += ev.rate || 0;
      payload += ev.payload_bytes || 0;
    }
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
    const raw = state.latestNodesPayload?.nodes?.[String(node.nodeId ?? '')];
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
      case 'rate': return ev?.rate || 0;
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
      <label class="graph-toggle">
        <input type="checkbox" id="graphShowGrid" ${gState.showGrid ? 'checked' : ''} />
        <span>Show grid</span>
      </label>
      <label class="graph-toggle">
        <input type="checkbox" id="graphShowLinkStats" ${gState.showLinkStats ? 'checked' : ''} />
        <span>Link stats</span>
      </label>
      <input type="text" id="graphFilter" class="graph-filter" placeholder="Filter by id, name, or type…" aria-label="Filter graph" />
      <div class="graph-select-group">
        <label for="graphGravity">Gravity</label>
        <select id="graphGravity" class="graph-select" aria-label="Gravity metric">
          ${GRAVITY_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
        </select>
      </div>
      <button class="graph-btn" id="graphResetLayout" aria-label="Reset graph layout">Reset layout</button>
      <button class="graph-btn" id="graphUnpinAll" aria-label="Unpin all nodes">Unpin all</button>
      <button class="graph-btn graph-hidden-badge hidden" id="graphHiddenBadge" aria-label="Show all hidden">0 hidden — show</button>
    `;
    root.appendChild(toolbar);

    const legendBar = document.createElement('div');
    legendBar.className = 'graph-legend-bar';
    legendBar.innerHTML = `
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--device"></span>device</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--subject"></span>subject</span>
      <span class="graph-legend-item"><span class="graph-legend-line"></span>idle link</span>
      <span class="graph-legend-item"><span class="graph-legend-line graph-legend-line--live"></span>live link</span>
      <span class="graph-legend-item"><span class="graph-legend-dot graph-legend-dot--pulse"></span>publishing</span>
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
    gLinks = container.append('g').attr('class', 'graph-links');
    gNodes = container.append('g').attr('class', 'graph-nodes');
    gLabels = container.append('g').attr('class', 'graph-labels');
    gLinkLabels = container.append('g').attr('class', 'graph-link-labels');

    // Zoom
    zoomBehavior = d3.zoom()
      .scaleExtent([0.1, 4])
      .on('zoom', (e) => {
        container.attr('transform', e.transform);
        gState.zoom = { k: e.transform.k, x: e.transform.x, y: e.transform.y };
        if (gState.selectedId) _positionInfoPanel();
        save();
      });
    svg.call(zoomBehavior);

    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => { if (gState.selectedId) _positionInfoPanel(); });
      ro.observe(svgWrap);
    }

    if (gState.zoom) {
      svg.call(zoomBehavior.transform,
        d3.zoomIdentity.translate(gState.zoom.x, gState.zoom.y).scale(gState.zoom.k));
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

    const placeholderHtml = typeof eventSourcePlaceholder === 'function'
      ? eventSourcePlaceholder('view the topology graph') : null;
    const svgWrap = svg?.node()?.parentNode;
    if (svgWrap) {
      let overlay = svgWrap.querySelector('.graph-conn-overlay');
      if (placeholderHtml) {
        if (!overlay) {
          overlay = document.createElement('div');
          overlay.className = 'graph-conn-overlay';
          svgWrap.appendChild(overlay);
        }
        overlay.innerHTML = placeholderHtml;
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
        return String(n.nodeId).includes(filterStr) || (n.label || '').toLowerCase().includes(filterStr);
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
      .attr('marker-end', d => d.type === 'pub' ? 'url(#graph-arrow-pub)' : null);
    linkEnter.merge(linkSel).each(function(d) {
      const rate = linkRate(d);
      d3.select(this)
        .attr('stroke-width', rateToWidth(rate))
        .classed('graph-link--live', isLinkLive(d));
    });

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
          .text(d.nodeId);
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
    merged.each(function(d) {
      const g = d3.select(this);
      if (d.type === 'device') {
        g.select('.graph-device-circle')
          .attr('stroke', d.disappeared ? 'var(--unknown)' : getHealthColor(d.health))
          .attr('opacity', d.disappeared ? 0.4 : 1);
        g.select('.graph-node-id')
          .attr('opacity', d.disappeared ? 0.4 : 1);
        g.classed('graph-node--live', isDeviceLive(d.nodeId, d.disappeared));
      }
    });

    // Labels
    const labelSel = gLabels.selectAll('.graph-label').data(allNodes, d => d.id);
    labelSel.exit().remove();
    const labelEnter = labelSel.enter().append('text')
      .attr('class', d => `graph-label graph-label--${d.type}`)
      .attr('text-anchor', 'middle');
    labelEnter.merge(labelSel).text(d => d.label);

    // Store references for tick
    gState._adjacency = adjacency;
    gState._allLinks = allLinks;
    gState._visibleIds = visibleIds;
    prevSnapshot = _snapshotKey();
    _renderLinkStats();
    _renderHiddenBadge();
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

    if (gState.showLinkStats) {
      gLinkLabels.selectAll('.graph-link-label')
        .attr('x', d => (d.source.x + d.target.x) / 2)
        .attr('y', d => (d.source.y + d.target.y) / 2 - 3);
    }

    if (gState.selectedId) _positionInfoPanel();
  };

  const _labelY = (d) => {
    if (d.labelSide === 'above') {
      return d.y - (d.type === 'device' ? 18 : 14);
    }
    return d.y + (d.type === 'device' ? 26 : 22);
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

  const _dragBehavior = () => d3.drag()
    .on('start', (e, d) => {
      if (!e.active) simulation.alphaTarget(0.05).restart();
      d.fx = d.x;
      d.fy = d.y;
    })
    .on('drag', (e, d) => {
      d.fx = e.x;
      d.fy = e.y;
    })
    .on('end', (e, d) => {
      if (!e.active) simulation.alphaTarget(0);
      const sx = _snap(e.x);
      const sy = _snap(e.y);
      d.fx = sx;
      d.fy = sy;
      gState.positions[d.id] = { x: sx, y: sy, pinned: true };
      d3.select(e.sourceEvent.target.closest('.graph-node'))
        .classed('graph-node--pinned', true);
      save();
    });

  // ── Selection & highlighting ──

  const _selectNode = (id) => {
    gState.selectedId = id;
    _applyHighlight(id, false);
    _renderInfo(id);
  };

  const _hoverNode = (id) => {
    if (gState.selectedId) return;
    _applyHighlight(id, true);
    if (!id) _renderInfo(null);
  };

  const _applyHighlight = (id, isHover) => {
    if (!id) {
      gNodes.selectAll('.graph-node').classed('graph-dim', false).classed('graph-hi', false);
      gLinks.selectAll('.graph-link').classed('graph-dim', false).classed('graph-link--hi', false);
      gLabels.selectAll('.graph-label').classed('graph-dim', false);
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
        .attr('marker-end', connected && d.type === 'pub' ? 'url(#graph-arrow-pub-hi)' : (d.type === 'pub' ? 'url(#graph-arrow-pub)' : null));
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

    if (!id) {
      panel.classList.add('hidden');
      panel.innerHTML = '';
      return;
    }

    panel.classList.remove('hidden');
    const allNodes = simulation?.nodes() || [];
    const node = allNodes.find(n => n.id === id);
    if (!node) { panel.classList.add('hidden'); return; }

    if (node.type === 'device') {
      _renderDeviceInfo(panel, node);
    } else {
      _renderSubjectInfo(panel, node);
    }
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

  const _renderDeviceInfo = (panel, node) => {
    const raw = state.latestNodesPayload?.nodes?.[String(node.nodeId)];
    const health = node.health || 'UNKNOWN';
    const hClass = getHealthCssClass(health);
    const alias = typeof getNodeAlias === 'function' ? getNodeAlias(raw?.unique_id) : null;
    const displayName = alias || node.fullName || node.label;

    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Device</span>
      <div class="graph-info-actions">
        <button class="graph-info-icon-btn" id="graphInfoRename" aria-label="Rename device" title="Rename">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
        </button>
        <button class="graph-info-icon-btn" id="graphInfoHide" aria-label="Hide device" title="Hide">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
        </button>
        <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
      </div>
    </div>
    <div class="graph-info-title" id="graphInfoTitle" data-uid="${escapeHtml(raw?.unique_id_hex || '')}">${escapeHtml(displayName)}</div>
    <div class="graph-info-meta">
      <span>ID: ${node.nodeId}</span>
      <span class="graph-info-health ${hClass}">${escapeHtml(health)}</span>
      ${node.disappeared ? '<span class="graph-info-offline">OFFLINE</span>' : ''}
    </div>`;

    if (raw) {
      if (raw.publishers?.length) {
        html += `<div class="graph-info-section"><div class="graph-info-section-label">Publishers (${raw.publishers.length})</div>`;
        html += raw.publishers.map(sid => {
          const ev = state.latestBySubject.get(sid);
          const type = ev?.message_type ? ev.message_type.split('.').pop() : '';
          return `<div class="graph-info-row"><span class="graph-info-sid">${sid}</span><span class="graph-info-mtype">${escapeHtml(type)}</span></div>`;
        }).join('');
        html += '</div>';
      }
      if (raw.subscribers?.length) {
        html += `<div class="graph-info-section"><div class="graph-info-section-label">Subscribers (${raw.subscribers.length})</div>`;
        html += raw.subscribers.map(sid => {
          const ev = state.latestBySubject.get(sid);
          const type = ev?.message_type ? ev.message_type.split('.').pop() : '';
          return `<div class="graph-info-row"><span class="graph-info-sid">${sid}</span><span class="graph-info-mtype">${escapeHtml(type)}</span></div>`;
        }).join('');
        html += '</div>';
      }
      if (raw.servers?.length) {
        html += `<div class="graph-info-section"><div class="graph-info-section-label">Servers (${raw.servers.length})</div>`;
        html += raw.servers.map(sid => `<div class="graph-info-row"><span class="graph-info-sid">${sid}</span></div>`).join('');
        html += '</div>';
      }
      if (raw.clients?.length) {
        html += `<div class="graph-info-section"><div class="graph-info-section-label">Clients (${raw.clients.length})</div>`;
        html += raw.clients.map(sid => `<div class="graph-info-row"><span class="graph-info-sid">${sid}</span></div>`).join('');
        html += '</div>';
      }
    }

    panel.innerHTML = html;
    document.getElementById('graphInfoClose')?.addEventListener('click', () => _selectNode(null));
    document.getElementById('graphInfoHide')?.addEventListener('click', () => {
      if (typeof hideNode === 'function') hideNode(node.nodeId);
      _selectNode(null);
      _render(deriveGraph());
      _renderHiddenBadge();
    });
    document.getElementById('graphInfoRename')?.addEventListener('click', () => {
      _startRename(node, raw);
    });
  };

  const _startRename = (node, raw) => {
    const titleEl = document.getElementById('graphInfoTitle');
    if (!titleEl) return;
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

  const _renderSubjectInfo = (panel, node) => {
    const ev = state.latestBySubject.get(node.subjectId);
    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Subject</span>
      <div class="graph-info-actions">
        <button class="graph-info-icon-btn" id="graphInfoHide" aria-label="Hide subject" title="Hide">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
        </button>
        <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
      </div>
    </div>
    <div class="graph-info-title">${escapeHtml(node.fullType || node.label)}</div>
    <div class="graph-info-meta">
      <span>ID: ${node.subjectId}</span>
      ${node.rate != null ? `<span>${Number(node.rate).toFixed(1)} msg/s</span>` : ''}
    </div>`;

    if (node.fullType) {
      html += `<div class="graph-info-row"><span class="graph-info-mtype">${escapeHtml(node.fullType)}</span></div>`;
    }

    if (node.pubs?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Publishers</div>`;
      html += node.pubs.map(nid => {
        const raw = state.latestNodesPayload?.nodes?.[String(nid)];
        const label = raw?.name || `Node ${nid}`;
        return `<div class="graph-info-row"><span class="graph-info-sid">${nid}</span><span class="graph-info-mtype">${escapeHtml(label)}</span></div>`;
      }).join('');
      html += '</div>';
    }
    if (node.subs?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Subscribers</div>`;
      html += node.subs.map(nid => {
        const raw = state.latestNodesPayload?.nodes?.[String(nid)];
        const label = raw?.name || `Node ${nid}`;
        return `<div class="graph-info-row"><span class="graph-info-sid">${nid}</span><span class="graph-info-mtype">${escapeHtml(label)}</span></div>`;
      }).join('');
      html += '</div>';
    }

    if (ev?.attributes?.length) {
      html += `<div class="graph-info-section"><div class="graph-info-section-label">Attributes</div>`;
      html += ev.attributes.map(a =>
        `<div class="graph-info-row"><span class="graph-info-sid">${escapeHtml(a.attribute)}</span><span class="graph-info-mtype">${escapeHtml(String(a.value))}${a.unit ? ' ' + escapeHtml(a.unit) : ''}</span></div>`
      ).join('');
      html += '</div>';
    }

    panel.innerHTML = html;
    document.getElementById('graphInfoClose')?.addEventListener('click', () => _selectNode(null));
    document.getElementById('graphInfoHide')?.addEventListener('click', () => {
      if (typeof hideSubject === 'function') hideSubject({ id: node.subjectId });
      _selectNode(null);
      _render(deriveGraph());
      _renderHiddenBadge();
    });
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
    let labelTextChanged = false;
    gNodes.selectAll('.graph-node--device').each(function(d) {
      const newHealth = getNodeHealthValue(d.nodeId);
      const raw = state.latestNodesPayload?.nodes?.[String(d.nodeId)];
      d.health = newHealth;
      d.disappeared = raw?.has_disappeared || false;
      const newLabel = _deviceDisplayLabel(raw, d.nodeId);
      if (newLabel !== d.label) { d.label = newLabel; labelTextChanged = true; }
      d.fullName = _deviceFullName(raw, d.nodeId);
      const g = d3.select(this);
      g.select('.graph-device-circle')
        .attr('stroke', d.disappeared ? 'var(--unknown)' : getHealthColor(newHealth))
        .attr('opacity', d.disappeared ? 0.4 : 1);
      g.classed('graph-node--live', isDeviceLive(d.nodeId, d.disappeared));
    });
    if (labelTextChanged && gLabels) {
      gLabels.selectAll('.graph-label--device').text(d => d.label);
    }

    gLinks.selectAll('.graph-link').each(function(d) {
      const rate = linkRate(d);
      d3.select(this)
        .attr('stroke-width', rateToWidth(rate))
        .classed('graph-link--live', isLinkLive(d));
    });

    if (gState.showLinkStats) {
      gLinkLabels.selectAll('.graph-link-label').text(d => _linkStatText(d));
    }

    _renderHiddenBadge();

    if (gState.selectedId) _renderInfo(gState.selectedId);
  };

  const _startRefresh = () => {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(_refresh, REFRESH_MS);
  };

  // ── Public API ──

  const show = () => {
    if (!gState.initialized) init();
    const root = document.getElementById('graphContainer');
    if (root) root.classList.remove('hidden');
    _startRefresh();
    _refresh();
    // Resize viewBox to current container
    if (svg) {
      const wrap = root?.querySelector('.graph-svg-wrap');
      if (wrap) {
        const w = wrap.clientWidth || 800;
        const h = wrap.clientHeight || 600;
        svg.attr('viewBox', `0 0 ${w} ${h}`);
        if (simulation) simulation.force('center', d3.forceCenter(w / 2, h / 2).strength(0.03));
      }
    }
  };

  const hide = () => {
    const root = document.getElementById('graphContainer');
    if (root) root.classList.add('hidden');
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  };

  return { init, show, hide };
})();
