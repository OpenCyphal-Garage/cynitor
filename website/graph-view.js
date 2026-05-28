// Network topology graph — fully decoupled module.
// Reads state.latestNodesPayload (read-only). Owns its own state,
// persistence, rendering, selection, and info panel.

const GraphView = (() => {
  // ── Own state ──
  const STORAGE_KEY = 'cynitor.graph.v1';
  const REFRESH_MS = 1000;

  const gState = {
    positions: {},
    selectedId: null,
    zoom: null,
    showSubjects: true,
    initialized: false,
  };

  let svg, container, simulation, gLinks, gNodes, gLabels;
  let zoomBehavior;
  let refreshTimer = null;
  let prevSnapshot = null;
  let _savePending = null;

  // ── Persistence ──

  const _writeNow = () => {
    const data = {
      positions: gState.positions,
      zoom: gState.zoom,
      showSubjects: gState.showSubjects,
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
      if (typeof raw.showSubjects === 'boolean') gState.showSubjects = raw.showSubjects;
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

    for (const node of Object.values(nodes)) {
      const nid = node.node_id;
      const id = `dev:${nid}`;
      deviceNodes.push({
        id,
        nodeId: nid,
        type: 'device',
        label: node.name || `Node ${nid}`,
        health: getNodeHealthValue(nid),
        disappeared: !!node.has_disappeared,
      });

      for (const sid of node.publishers || []) {
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).pubs.push(nid);
        links.push({ source: id, target: `sub:${sid}`, type: 'pub' });
      }
      for (const sid of node.subscribers || []) {
        if (!subjectSet.has(sid)) subjectSet.set(sid, { pubs: [], subs: [] });
        subjectSet.get(sid).subs.push(nid);
        links.push({ source: `sub:${sid}`, target: id, type: 'sub' });
      }
    }

    for (const [sid, meta] of subjectSet) {
      const ev = state.latestBySubject.get(sid);
      subjectNodes.push({
        id: `sub:${sid}`,
        subjectId: sid,
        type: 'subject',
        label: ev?.message_type ? ev.message_type.split('.').pop() : `Subject ${sid}`,
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
      <label class="graph-toggle">
        <input type="checkbox" id="graphShowSubjects" ${gState.showSubjects ? 'checked' : ''} />
        <span>Show subjects</span>
      </label>
      <button class="graph-btn" id="graphResetLayout" aria-label="Reset graph layout">Reset layout</button>
      <button class="graph-btn" id="graphUnpinAll" aria-label="Unpin all nodes">Unpin all</button>
    `;
    root.appendChild(toolbar);

    // SVG
    const svgWrap = document.createElement('div');
    svgWrap.className = 'graph-svg-wrap';
    root.appendChild(svgWrap);

    // Info panel
    const info = document.createElement('div');
    info.className = 'graph-info';
    info.id = 'graphInfo';
    root.appendChild(info);

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

    container = svg.append('g').attr('class', 'graph-layer');
    gLinks = container.append('g').attr('class', 'graph-links');
    gNodes = container.append('g').attr('class', 'graph-nodes');
    gLabels = container.append('g').attr('class', 'graph-labels');

    // Zoom
    zoomBehavior = d3.zoom()
      .scaleExtent([0.1, 4])
      .on('zoom', (e) => {
        container.attr('transform', e.transform);
        gState.zoom = { k: e.transform.k, x: e.transform.x, y: e.transform.y };
        save();
      });
    svg.call(zoomBehavior);

    if (gState.zoom) {
      svg.call(zoomBehavior.transform,
        d3.zoomIdentity.translate(gState.zoom.x, gState.zoom.y).scale(gState.zoom.k));
    }

    // Click on background deselects
    svg.on('click', (e) => {
      if (e.target === svg.node() || e.target.closest('.graph-layer') === container.node() && !e.target.closest('.graph-node')) {
        _selectNode(null);
      }
    });

    // Toolbar events
    document.getElementById('graphShowSubjects').addEventListener('change', (e) => {
      gState.showSubjects = e.target.checked;
      save();
      _render(deriveGraph());
    });
    document.getElementById('graphResetLayout').addEventListener('click', () => {
      gState.positions = {};
      gState.zoom = null;
      svg.call(zoomBehavior.transform, d3.zoomIdentity);
      save();
      prevSnapshot = null;
      _render(deriveGraph());
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
      .force('charge', d3.forceManyBody().strength(-120))
      .force('collide', d3.forceCollide().radius(d => d.type === 'device' ? 28 : 18))
      .force('center', d3.forceCenter(width / 2, height / 2).strength(0.03))
      .on('tick', _tick)
      .stop();

    gState.initialized = true;
    _render(deriveGraph());
    _startRefresh();
  };

  // ── Rendering ──

  const _render = (graph) => {
    const { deviceNodes, subjectNodes, links, collapsedLinks, adjacency } = graph;

    const showSubs = gState.showSubjects;
    const allNodes = showSubs ? [...deviceNodes, ...subjectNodes] : [...deviceNodes];
    const allLinks = showSubs ? links : collapsedLinks;

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
    simulation.force('link', d3.forceLink(allLinks).id(d => d.id).distance(d => showSubs ? 60 : 90).strength(0.4));
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
    linkEnter.merge(linkSel);

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
    prevSnapshot = _snapshotKey();
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
      .attr('y', d => d.type === 'device' ? d.y + 26 : d.y + 22);
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
      d.fx = e.x;
      d.fy = e.y;
      gState.positions[d.id] = { x: e.x, y: e.y, pinned: true };
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
  };

  const _renderDeviceInfo = (panel, node) => {
    const raw = state.latestNodesPayload?.nodes?.[String(node.nodeId)];
    const health = node.health || 'UNKNOWN';
    const hClass = getHealthCssClass(health);
    const alias = typeof getNodeAlias === 'function' ? getNodeAlias(raw?.unique_id) : null;
    const displayName = alias || node.label;

    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Device</span>
      <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
    </div>
    <div class="graph-info-title">${escapeHtml(displayName)}</div>
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
  };

  const _renderSubjectInfo = (panel, node) => {
    const ev = state.latestBySubject.get(node.subjectId);
    let html = `<div class="graph-info-header">
      <span class="graph-info-type">Subject</span>
      <button class="graph-info-close" id="graphInfoClose" aria-label="Close info panel">&times;</button>
    </div>
    <div class="graph-info-title">${escapeHtml(node.label)}</div>
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
      const newHealth = getNodeHealthValue(d.nodeId);
      const raw = state.latestNodesPayload?.nodes?.[String(d.nodeId)];
      d.health = newHealth;
      d.disappeared = raw?.has_disappeared || false;
      const g = d3.select(this);
      g.select('.graph-device-circle')
        .attr('stroke', d.disappeared ? 'var(--unknown)' : getHealthColor(newHealth))
        .attr('opacity', d.disappeared ? 0.4 : 1);
    });

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
