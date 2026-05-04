// Detail panel: per-node subject cards (Publishers/Subscribers/Servers/Clients
// tabs), the D3 line plot for the selected subject, and the per-row
// selection helpers used by nodes-table.js.

const renderMetric = (a, subjectId) => {
  const statusCls = getStatusClass(a.attribute, a.value);
  const rawStr = String(a.value ?? '-');
  const displayStr = typeof a.value === 'number' && !Number.isInteger(a.value)
    ? a.value.toFixed(2) : rawStr;
  const minW = getMetricMinWidth(subjectId, a.attribute, displayStr);
  const unitStr = a.unit ? `<span class="metric-unit">${escapeHtml(a.unit)}</span>` : '';
  const valCls = `metric-val${statusCls ? ' ' + statusCls : ''}`;
  return `<span class="metric" data-subject="${subjectId}" data-attr="${escapeHtml(a.attribute)}"><span class="metric-key">${escapeHtml(a.attribute)}</span><span class="${valCls}" title="${escapeHtml(rawStr)}"><span class="metric-val-text" style="min-width:${minW}ch">${escapeHtml(displayStr)}</span>${unitStr}</span></span>`;
};

const renderSubjectTable = (title, subjects) => {
  if (!subjects.length) {
    return `<div class="subject-cards"><div class="detail-empty">No ${title.toLowerCase()} discovered.</div></div>`;
  }
  const cards = subjects.map((s) => {
    const rateStr = s.rate != null ? `${s.rate} Hz` : '';
    const liveDot = s.rate != null && s.rate > 0
      ? '<span class="live-dot"></span>' : '';
    const metrics = s.attributes.length
      ? `<div class="card-metrics">${s.attributes.map((a) => renderMetric(a, s.subjectId)).join('')}</div>`
      : '<div class="card-metrics"><span class="metrics-empty">no telemetry data</span></div>';

    return `<div class="subject-card" data-subject="${s.subjectId}" tabindex="0" role="button">
      <div class="card-header">
        <span class="card-subject-id">${escapeHtml(String(s.subjectId))}</span>
        <span class="card-type" title="${escapeHtml(s.messageType || '')}">${escapeHtml(s.messageType || 'awaiting data')}</span>
        <span class="card-rate">${liveDot}${escapeHtml(rateStr)}</span>
      </div>
      ${metrics}
    </div>`;
  }).join('');

  return `<div class="subject-cards">${cards}</div>`;
};

const updateSubjectTableInPlace = (container, subjects) => {
  const wrap = container.querySelector('.subject-cards');
  if (!wrap) return false;

  const existingIds = [...wrap.querySelectorAll('.subject-card')].map((c) => c.dataset.subject);
  const newIds = subjects.map((s) => String(s.subjectId));
  if (existingIds.length !== newIds.length || existingIds.some((id, i) => id !== newIds[i])) {
    return false;
  }

  for (const s of subjects) {
    for (const a of s.attributes) {
      const metric = wrap.querySelector(`.metric[data-subject="${s.subjectId}"][data-attr="${a.attribute}"]`);
      if (!metric) return false;

      const valEl = metric.querySelector('.metric-val');
      const valText = valEl?.querySelector('.metric-val-text');
      if (!valText) return false;

      const rawStr = String(a.value ?? '-');
      const displayStr = typeof a.value === 'number' && !Number.isInteger(a.value)
        ? a.value.toFixed(2) : rawStr;

      if (valText.textContent !== displayStr) {
        const minW = getMetricMinWidth(s.subjectId, a.attribute, displayStr);
        valText.textContent = displayStr;
        valText.style.minWidth = minW + 'ch';
        valEl.title = rawStr;

        const statusCls = getStatusClass(a.attribute, a.value);
        valEl.classList.remove('status-ok', 'status-warn', 'status-err', 'status-init');
        if (statusCls) valEl.classList.add(statusCls);
      }
    }
  }
  return true;
};

const PLOT_MARGIN = { top: 8, right: 12, bottom: 24, left: 48 };
const PLOT_PANEL_GAP = 8;
const PLOT_WINDOW_SECS = 60;

const collectPlotSeries = (sid) => {
  const allSeries = [];
  for (const [key, buf] of state.subjectHistory) {
    if (!key.startsWith(sid + ':')) continue;
    if (buf.length < 2) continue;
    allSeries.push({ name: key.split(':')[1], data: buf });
  }
  return allSeries;
};

const computePlotScales = (visible, w, totalPanelsH) => {
  let tDataMax = -Infinity;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  if (!isFinite(tDataMax)) tDataMax = Date.now() / 1000;

  const now = Date.now() / 1000;
  const dataIsLive = (now - tDataMax) < PLOT_STALE_THRESHOLD;
  const tRight = dataIsLive ? now : tDataMax;
  const xScale = d3.scaleLinear()
    .domain([tRight - PLOT_WINDOW_SECS, tRight + PLOT_WINDOW_SECS * 0.5])
    .range([0, w]);

  const numPanels = Math.max(1, visible.length);
  const panelH = (totalPanelsH - (numPanels - 1) * PLOT_PANEL_GAP) / numPanels;
  const yScales = visible.map((s) => {
    let min = Infinity, max = -Infinity;
    for (const p of s.data) {
      if (p.v < min) min = p.v;
      if (p.v > max) max = p.v;
    }
    if (min === max) { min -= 1; max += 1; }
    const pad = (max - min) * 0.05;
    return d3.scaleLinear().domain([min - pad, max + pad]).range([panelH, 0]);
  });

  return { xScale, yScales, panelH, dataIsLive };
};

const setupPlotSvg = (plotArea, margin) => {
  plotArea.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'plot-header';
  const titleNode = document.createElement('div');
  titleNode.className = 'plot-title';
  header.appendChild(titleNode);
  const legendNode = document.createElement('div');
  legendNode.className = 'plot-legend';
  legendNode.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-series]');
    if (!btn) return;
    const activeSid = state.selectedPlotSubject;
    if (activeSid == null) return;
    if (!state.hiddenPlotSeries.has(activeSid)) {
      state.hiddenPlotSeries.set(activeSid, new Set());
    }
    const activeHidden = state.hiddenPlotSeries.get(activeSid);
    const name = btn.dataset.series;
    if (activeHidden.has(name)) activeHidden.delete(name);
    else activeHidden.add(name);
    const isActive = !activeHidden.has(name);
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-pressed', String(isActive));
    startPlotAnim();
  });
  header.appendChild(legendNode);
  plotArea.appendChild(header);
  const svgEl = d3.select(plotArea).append('svg')
    .attr('width', '100%')
    .attr('aria-label', 'Time series plot')
    .attr('tabindex', '0')
    .attr('role', 'img');
  const g = svgEl.append('g').attr('class', 'plot-root')
    .attr('transform', `translate(${margin.left},${margin.top})`);
  g.append('g').attr('class', 'plot-panels');
  g.append('g').attr('class', 'plot-x-axis');
  g.append('line').attr('class', 'plot-crosshair').attr('opacity', 0);
  g.append('rect').attr('class', 'plot-overlay').attr('fill', 'none').style('pointer-events', 'all');
  const tooltip = document.createElement('div');
  tooltip.className = 'plot-tooltip';
  tooltip.style.display = 'none';
  plotArea.appendChild(tooltip);
  return g.node();
};

const renderPanelLines = (g, sid, visible, xScale, yScales, panelH, w, totalPanelsH) => {
  const panelsG = g.select('.plot-panels');
  const panels = panelsG.selectAll('.plot-panel').data(visible, (d) => d.name);
  const panelsEnter = panels.enter().append('g').attr('class', 'plot-panel');
  panelsEnter.append('clipPath').attr('id', (d) => `panel-clip-${sid}-${d.name}`)
    .append('rect');
  panelsEnter.append('g').attr('class', 'panel-y-axis');
  panelsEnter.append('g').attr('class', 'panel-line')
    .append('path').attr('fill', 'none').attr('stroke-width', 1.5);
  panelsEnter.append('text').attr('class', 'panel-label').attr('x', 4).attr('y', 11);
  panels.exit().remove();

  panelsG.selectAll('.plot-panel').each(function (d, i) {
    const yScale = yScales[i];
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    const panel = d3.select(this);
    panel.attr('transform', `translate(0, ${i * (panelH + PLOT_PANEL_GAP)})`);
    panel.select('clipPath rect').attr('width', w).attr('height', panelH);
    panel.select('.panel-y-axis').call(d3.axisLeft(yScale).ticks(3).tickSize(2));
    const lineGen = d3.line().x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
    panel.select('.panel-line')
      .attr('clip-path', `url(#panel-clip-${sid}-${d.name})`)
      .select('path').attr('stroke', color).attr('d', lineGen(d.data));
    panel.select('.panel-label').text(d.name).attr('fill', color);
  });

  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  g.select('.plot-x-axis').attr('transform', `translate(0, ${totalPanelsH})`).call(xAxis);
  g.select('.plot-overlay').attr('width', w).attr('height', totalPanelsH);
  g.select('.plot-crosshair').attr('y1', 0).attr('y2', totalPanelsH);
};

const bindPlotTooltip = (g, plotArea, visible, xScale, w, HEADER_H, rect) => {
  const tooltipEl = plotArea.querySelector('.plot-tooltip');
  const overlay = g.select('.plot-overlay');
  const crosshair = g.select('.plot-crosshair');
  const bisect = d3.bisector((d) => d.t).left;

  const showCrosshairAt = (mx) => {
    if (mx < 0 || mx > w || !visible.length) {
      crosshair.attr('opacity', 0);
      tooltipEl.style.display = 'none';
      return;
    }
    const t0 = xScale.invert(mx);
    const samples = visible.map((s) => {
      const i = bisect(s.data, t0);
      const a = s.data[i - 1];
      const b = s.data[i];
      const sample = !b ? a : !a ? b : (Math.abs(a.t - t0) < Math.abs(b.t - t0) ? a : b);
      return { name: s.name, sample };
    }).filter((x) => x.sample);
    if (!samples.length) {
      crosshair.attr('opacity', 0);
      tooltipEl.style.display = 'none';
      return;
    }
    crosshair.attr('opacity', 1).attr('x1', mx).attr('x2', mx);
    const formattedT = formatPlotTime(samples[0].sample.t);
    const rows = samples.map((s) => {
      const idx = visible.findIndex((v) => v.name === s.name);
      const color = PLOT_COLORS[idx % PLOT_COLORS.length];
      const v = typeof s.sample.v === 'number' && !Number.isInteger(s.sample.v)
        ? s.sample.v.toFixed(2) : String(s.sample.v);
      return `<div class="plot-tooltip-row"><span class="plot-tooltip-swatch" style="background:${color}"></span><span class="plot-tooltip-name">${escapeHtml(s.name)}</span><span class="plot-tooltip-val">${escapeHtml(v)}</span></div>`;
    }).join('');
    tooltipEl.innerHTML = `<div class="plot-tooltip-time">${formattedT}</div>${rows}`;
    tooltipEl.style.display = 'block';
    let tx = mx + PLOT_MARGIN.left + 12;
    const tw = tooltipEl.offsetWidth;
    if (tx + tw > rect.width - 4) tx = mx + PLOT_MARGIN.left - 12 - tw;
    tooltipEl.style.left = `${Math.max(4, tx)}px`;
    tooltipEl.style.top = `${HEADER_H + 4}px`;
  };

  overlay
    .on('mousemove', (event) => {
      const [mx] = d3.pointer(event);
      showCrosshairAt(mx);
    })
    .on('mouseleave', () => {
      crosshair.attr('opacity', 0);
      tooltipEl.style.display = 'none';
    });

  const svgEl = plotArea.querySelector('svg');
  if (svgEl && !svgEl._kbBound) {
    svgEl._kbBound = true;
    let kbPos = w / 2;
    svgEl.addEventListener('keydown', (e) => {
      const step = w / 20;
      if (e.key === 'ArrowLeft') { kbPos = Math.max(0, kbPos - step); }
      else if (e.key === 'ArrowRight') { kbPos = Math.min(w, kbPos + step); }
      else if (e.key === 'Escape') { crosshair.attr('opacity', 0); tooltipEl.style.display = 'none'; return; }
      else return;
      e.preventDefault();
      showCrosshairAt(kbPos);
    });
  }
};

const updatePlotLegend = (plotArea, allSeries, hidden) => {
  const legend = plotArea.querySelector('.plot-legend');
  if (!legend) return;
  const seriesKey = allSeries.map((s) => s.name).join('|');
  if (legend.dataset.seriesKey !== seriesKey) {
    legend.dataset.seriesKey = seriesKey;
    legend.innerHTML = allSeries.map((s, i) => {
      const isActive = !hidden.has(s.name);
      const color = PLOT_COLORS[i % PLOT_COLORS.length];
      return `<button type="button" class="plot-legend-item${isActive ? ' active' : ''}" data-series="${escapeHtml(s.name)}" aria-pressed="${isActive}"><span class="plot-legend-swatch" style="background:${color}"></span>${escapeHtml(s.name)}</button>`;
    }).join('');
  } else {
    for (const btn of legend.querySelectorAll('button[data-series]')) {
      const isActive = !hidden.has(btn.dataset.series);
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-pressed', String(isActive));
    }
  }
};

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    return;
  }

  const allSeries = collectPlotSeries(sid);
  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    return;
  }

  if (!state.hiddenPlotSeries.has(sid)) {
    state.hiddenPlotSeries.set(sid, new Set());
  }
  const hidden = state.hiddenPlotSeries.get(sid);
  const visible = allSeries.filter((s) => !hidden.has(s.name));

  const rect = plotArea.getBoundingClientRect();
  const w = rect.width - PLOT_MARGIN.left - PLOT_MARGIN.right;
  const headerEl = plotArea.querySelector('.plot-header');
  const HEADER_H = headerEl ? Math.max(28, Math.ceil(headerEl.getBoundingClientRect().height)) : 28;
  const totalPanelsH = rect.height - PLOT_MARGIN.top - PLOT_MARGIN.bottom - HEADER_H;
  if (w < 40 || totalPanelsH < 40) return;

  const { xScale, yScales, panelH, dataIsLive } = computePlotScales(visible, w, totalPanelsH);

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels') || !plotArea.querySelector('.plot-header')) {
    gNode = setupPlotSvg(plotArea, PLOT_MARGIN);
  }

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - HEADER_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    const ctx = state.selectedDetailTab === 'subscribers'
      ? 'network broadcast'
      : `published by node ${state.selectedNodeId ?? '?'}`;
    const next = `Subject ${sid} · ${ctx}`;
    if (titleEl.textContent !== next) titleEl.textContent = next;
  }

  const g = d3.select(gNode);
  renderPanelLines(g, sid, visible, xScale, yScales, panelH, w, totalPanelsH);
  bindPlotTooltip(g, plotArea, visible, xScale, w, HEADER_H, rect);
  updatePlotLegend(plotArea, allSeries, hidden);

  return dataIsLive;
};

const stopPlotAnim = () => {
  if (state.plotTimer) {
    clearTimeout(state.plotTimer);
    state.plotTimer = null;
  }
};

const startPlotAnim = () => {
  stopPlotAnim();
  if (state.detailPanelCollapsed || state.selectedPlotSubject == null) return;
  const container = el('selectedNodeContent');
  const tick = () => {
    if (state.detailPanelCollapsed) { state.plotTimer = null; return; }
    const node = getSelectedNode();
    if (node?.has_disappeared) { state.plotTimer = null; return; }
    const isLive = renderPlot(container);
    if (isLive) {
      state.plotTimer = window.setTimeout(tick, PLOT_TICK_MS);
    } else {
      state.plotTimer = null;
    }
  };
  state.plotTimer = window.setTimeout(tick, PLOT_TICK_MS);
};

const bindSplitHandle = (splitEl) => {
  const handle = splitEl.querySelector('.detail-split-handle');
  if (!handle) return;
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const list = splitEl.querySelector('.detail-subject-list');
    const startX = e.clientX;
    const startW = list.getBoundingClientRect().width;
    const totalW = splitEl.getBoundingClientRect().width;

    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      const ratio = Math.min(0.85, Math.max(0.25, (startW + dx) / totalW));
      list.style.flex = `0 0 ${(ratio * 100).toFixed(1)}%`;
      state.splitRatio = ratio;
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      saveSettings();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
};

const renderClientCards = (clients, enrichedMap) => {
  const cards = clients.map((clientId) => {
    const info = enrichedMap.get(clientId);
    const typeName = info?.full_type || '';
    const serverNodes = info?.server_nodes || [];
    const serverHtml = serverNodes.length
      ? `<span class="svc-client-servers" title="Nodes serving this service">→ node ${serverNodes.join(', ')}</span>`
      : '';
    return `<div class="svc-card">
      <div class="svc-card-header svc-card-header-static">
        <span class="svc-service-id">${clientId}</span>
        <span class="svc-service-type" title="${escapeHtml(typeName)}">${escapeHtml(typeName || `Client ${clientId}`)}</span>
        ${serverHtml}
      </div>
    </div>`;
  }).join('');
  return `<section class="svc-panel">${cards}</section>`;
};

const renderClientsTab = async () => {
  const content = el('selectedNodeContent');
  const nodeId = state.selectedNodeId;

  if (!state.dashboardConnected || state.canState !== CONN.CONNECTED) {
    content.innerHTML = svcStateMsg('○', 'No clients advertised', 'Connect to the CAN bus to see client info.');
    return;
  }
  if (nodeId == null) {
    content.innerHTML = svcStateMsg('○', 'Select a node', 'Choose a node to view its client ports.');
    return;
  }

  const node = getSelectedNode();
  const clients = node?.clients || [];
  if (!clients.length) {
    content.innerHTML = svcStateMsg('○', 'No clients advertised', 'This node does not use any service clients.');
    return;
  }

  if (node.has_disappeared) {
    content.innerHTML = `<div class="svc-stale-banner"><span class="svc-stale-icon">⚠</span>Node ${nodeId} is offline — client data may be stale.</div>`
      + `<div class="svc-panel-stale">${renderClientCards(clients, new Map())}</div>`;
    return;
  }

  content.innerHTML = svcStateMsg('<span class="svc-spinner"></span>', 'Loading client info…', '');

  let enriched = null;
  try {
    const data = await requestJson(`/api/clients/${nodeId}`);
    enriched = data.clients || [];
  } catch {
    enriched = null;
  }

  if (state.selectedNodeId !== nodeId || state.selectedDetailTab !== 'clients') return;

  const enrichedMap = new Map();
  if (enriched) {
    for (const c of enriched) enrichedMap.set(c.service_id, c);
  }

  content.innerHTML = renderClientCards(clients, enrichedMap);
};

const renderListTab = (title, items) => `
  <section class="details-panel">
    <h3>${escapeHtml(title)}</h3>
    <ul class="details-list">
      ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
    </ul>
  </section>
`;

const renderSelectedNodeContent = () => {
  const content = el('selectedNodeContent');
  const node = getSelectedNode();

  document.querySelectorAll('.detail-tab').forEach((button) => {
    const isActive = button.dataset.tab === state.selectedDetailTab;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', String(isActive));
  });

  const tabCounts = node ? {
    publishers: (node.publishers || []).length,
    subscribers: (node.subscribers || []).length,
    servers: (node.servers || []).length,
    clients: (node.clients || []).length,
  } : {};
  document.querySelectorAll('.detail-tab').forEach((button) => {
    const count = tabCounts[button.dataset.tab];
    let badge = button.querySelector('.tab-count');
    if (count != null) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'tab-count';
        button.appendChild(badge);
      }
      badge.textContent = count;
    } else if (badge) {
      badge.remove();
    }
  });

  if (!node) {
    stopPlotAnim();
    if (!state.dashboardConnected) {
      content.innerHTML = state.pendingReconnect
        ? svcStateMsg('<span class="svc-spinner"></span>', 'Reconnecting to backend…', 'Restoring previous session.')
        : svcStateMsg('⏻', 'Not connected to backend', 'Connect to the backend server to inspect node details.');
    } else if (state.canState !== CONN.CONNECTED) {
      content.innerHTML = state.canState === CONN.CONNECTING
        ? svcStateMsg('<span class="svc-spinner"></span>', 'Connecting to CAN interface…', 'Establishing CAN bus connection.')
        : svcStateMsg('⛓', 'CAN bus not connected', 'Connect a CAN interface to discover online nodes.');
    } else if (state.selectedNodeId == null) {
      content.innerHTML = svcStateMsg('◎', 'Select a node to inspect details', 'Choose a node from the table above.');
    } else {
      content.innerHTML = svcStateMsg('⚠', `Node ${state.selectedNodeId} is offline`, 'This node disappeared from the CAN bus.');
    }
    return;
  }

  const wasOffline = content.dataset.nodeOffline === 'true';
  const isOffline = !!node.has_disappeared;
  content.dataset.nodeOffline = String(isOffline);

  if (wasOffline !== isOffline) {
    delete content.dataset.svcTab;
  }

  const staleBanner = isOffline
    ? `<div class="svc-stale-banner"><span class="svc-stale-icon">⚠</span>Node ${node.node_id} is offline — data may be stale.</div>`
    : '';

  const renderSubjectTab = (title, subjects) => {
    if (!staleBanner && updateSubjectTableInPlace(content, subjects)) {
      if (!state.plotTimer) startPlotAnim();
      return;
    }
    const selected = state.selectedPlotSubject;
    const pct = (state.splitRatio * 100).toFixed(1);
    content.innerHTML = `${staleBanner}<div class="detail-split${isOffline ? ' svc-panel-stale' : ''}">
      <div class="detail-subject-list" style="flex:0 0 ${pct}%">${renderSubjectTable(title, subjects)}</div>
      <div class="detail-split-handle"></div>
      <div class="detail-plot-area">${''}
      </div>
    </div>`;
    content.querySelectorAll('.subject-card').forEach((card) => {
      if (Number(card.dataset.subject) === selected) card.classList.add('selected');
    });
    bindSplitHandle(content.querySelector('.detail-split'));
    if (!isOffline) startPlotAnim();
  };

  if (state.selectedDetailTab !== 'servers' && state.selectedDetailTab !== 'clients' && state.selectedDetailTab !== 'registers') {
    delete content.dataset.svcTab;
  }

  if (state.selectedDetailTab === 'publishers') {
    renderSubjectTab('Publishers', buildSubjectDetailData(node.publishers || [], node.node_id));
    return;
  }

  if (state.selectedDetailTab === 'subscribers') {
    renderSubjectTab('Subscribers', buildSubjectDetailData(node.subscribers || [], null));
    return;
  }

  if (state.selectedDetailTab === 'servers') {
    stopPlotAnim();
    if (content.dataset.svcTab !== 'servers' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'servers';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderServicesTab();
    }
    return;
  }

  if (state.selectedDetailTab === 'clients') {
    stopPlotAnim();
    if (content.dataset.svcTab !== 'clients' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'clients';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderClientsTab();
    }
    return;
  }

  if (state.selectedDetailTab === 'registers') {
    stopPlotAnim();
    if (content.dataset.svcTab !== 'registers' || content.dataset.svcNodeId !== String(state.selectedNodeId)) {
      content.dataset.svcTab = 'registers';
      content.dataset.svcNodeId = String(state.selectedNodeId);
      renderRegistersTab();
    }
    return;
  }

  renderSubjectTab('Publishers', buildSubjectDetailData(node.publishers || [], node.node_id));
};

const scheduleDetailRefresh = () => {
  if (_detailRefreshPending) return;
  _detailRefreshPending = window.setTimeout(() => {
    _detailRefreshPending = null;
    renderSelectedNodeContent();
  }, 100);
};

const setSelectedNode = (nodeId) => {
  const numericNodeId = Number.parseInt(String(nodeId), 10);
  if (!Number.isInteger(numericNodeId)) {
    return;
  }
  state.selectedNodeId = numericNodeId;
  // Immediately highlight the selected row in the table
  if (nodesTabulator) {
    for (const row of nodesTabulator.getRows()) {
      const rowEl = row.getElement();
      if (row.getData().id === numericNodeId) {
        rowEl.classList.add('selected-row');
      } else {
        rowEl.classList.remove('selected-row');
      }
    }
  }
  renderSelectedNodeContent();
};

const clearSelectedNode = () => {
  state.selectedNodeId = null;
  if (nodesTabulator) {
    for (const row of nodesTabulator.getRows()) {
      row.getElement().classList.remove('selected-row');
    }
  }
  renderSelectedNodeContent();
};
