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

    return `<div class="subject-card" data-subject="${s.subjectId}">
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

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    return;
  }

  const allSeries = [];
  for (const [key, buf] of state.subjectHistory) {
    if (!key.startsWith(sid + ':')) continue;
    if (buf.length < 2) continue;
    allSeries.push({ name: key.split(':')[1], data: buf });
  }

  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    return;
  }

  const visible = allSeries.filter((s) => !state.hiddenPlotSeries.has(s.name));

  const margin = { top: 8, right: 12, bottom: 24, left: 48 };
  const TITLE_H = 20;   // reserved for the .plot-title row
  const LEGEND_H = 28;  // reserved for the .plot-legend row
  const rect = plotArea.getBoundingClientRect();
  const w = rect.width - margin.left - margin.right;
  const h = rect.height - margin.top - margin.bottom - TITLE_H - LEGEND_H;
  if (w < 40 || h < 40) return;

  let allMin = Infinity, allMax = -Infinity, tDataMin = Infinity, tDataMax = -Infinity;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.v < allMin) allMin = p.v;
      if (p.v > allMax) allMax = p.v;
      if (p.t < tDataMin) tDataMin = p.t;
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  if (!isFinite(allMin)) { allMin = 0; allMax = 1; tDataMin = Date.now() / 1000 - 60; tDataMax = Date.now() / 1000; }
  if (allMin === allMax) { allMin -= 1; allMax += 1; }
  const pad = (allMax - allMin) * 0.05;
  allMin -= pad;
  allMax += pad;

  const WINDOW_SECS = 60;
  const now = Date.now() / 1000;
  const dataIsLive = (now - tDataMax) < PLOT_STALE_THRESHOLD;
  const tRight = dataIsLive ? now : tDataMax;
  const tWindowStart = tRight - WINDOW_SECS;
  const tWindowEnd = tRight + WINDOW_SECS * 0.5;

  const xScale = d3.scaleLinear().domain([tWindowStart, tWindowEnd]).range([0, w]);
  const yScale = d3.scaleLinear().domain([allMin, allMax]).range([h, 0]);

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode) {
    plotArea.innerHTML = '';
    const titleEl = document.createElement('div');
    titleEl.className = 'plot-title';
    plotArea.appendChild(titleEl);
    const svgEl = d3.select(plotArea).append('svg')
      .attr('width', '100%').attr('height', rect.height - TITLE_H - LEGEND_H);
    const clipId = 'plot-clip-' + Date.now();
    svgEl.append('defs').append('clipPath').attr('id', clipId)
      .append('rect').attr('width', w).attr('height', h);
    const g = svgEl.append('g').attr('class', 'plot-root')
      .attr('transform', `translate(${margin.left},${margin.top})`);
    g.append('g').attr('class', 'plot-x-axis').attr('transform', `translate(0,${h})`);
    g.append('g').attr('class', 'plot-y-axis');
    g.append('g').attr('class', 'plot-lines').attr('clip-path', `url(#${clipId})`);
    gNode = g.node();
  }

  // Make the data semantics explicit: the plot is subject-keyed network
  // history, not per-node. On the subscribers tab there's no separate
  // "what this node received" log, so framing it as a network broadcast
  // avoids the implication that the curve represents the selected node.
  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    const ctx = state.selectedDetailTab === 'subscribers'
      ? 'network broadcast'
      : `published by node ${state.selectedNodeId ?? '?'}`;
    const next = `Subject ${sid} · ${ctx}`;
    if (titleEl.textContent !== next) titleEl.textContent = next;
  }

  const g = d3.select(gNode);
  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  const yAxis = d3.axisLeft(yScale).ticks(5);

  g.select('.plot-x-axis').call(xAxis);
  g.select('.plot-y-axis').call(yAxis);

  const line = d3.line()
    .x((d) => xScale(d.t))
    .y((d) => yScale(d.v))
    .curve(d3.curveLinear);

  const linesG = g.select('.plot-lines');
  const paths = linesG.selectAll('path').data(visible, (d) => d.name);
  paths.enter().append('path')
    .attr('fill', 'none')
    .attr('stroke-width', 1.5)
    .merge(paths)
    .attr('stroke', (_, i) => PLOT_COLORS[i % PLOT_COLORS.length])
    .attr('d', (d) => line(d.data));
  paths.exit().remove();

  let legend = plotArea.querySelector('.plot-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.className = 'plot-legend';
    legend.addEventListener('change', (e) => {
      const cb = e.target.closest('input[type="checkbox"]');
      if (!cb) return;
      if (cb.checked) {
        state.hiddenPlotSeries.delete(cb.dataset.series);
      } else {
        state.hiddenPlotSeries.add(cb.dataset.series);
      }
    });
    plotArea.appendChild(legend);
  }
  legend.innerHTML = allSeries.map((s, i) => {
    const checked = !state.hiddenPlotSeries.has(s.name) ? ' checked' : '';
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    return `<label class="plot-legend-item"><input type="checkbox" data-series="${escapeHtml(s.name)}"${checked}><span class="plot-legend-swatch" style="background:${color}"></span>${escapeHtml(s.name)}</label>`;
  }).join('');

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
  const container = el('selectedNodeContent');
  const tick = () => {
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

const buildServicesDetailItems = (services) => {
  if (!Array.isArray(services) || !services.length) {
    return ['No services advertised'];
  }
  return services.map((serviceId) => `Service ${serviceId}`);
};

const buildClientsDetailItems = (clients) => {
  if (!Array.isArray(clients) || !clients.length) {
    return ['No clients advertised'];
  }
  return clients.map((clientId) => `Client ${clientId}`);
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
    button.classList.toggle('active', button.dataset.tab === state.selectedDetailTab);
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
    content.innerHTML = '<div class="detail-empty">Select a node to inspect details.</div>';
    return;
  }

  const renderSubjectTab = (title, subjects) => {
    if (updateSubjectTableInPlace(content, subjects)) {
      if (!state.plotTimer) startPlotAnim();
      return;
    }
    const selected = state.selectedPlotSubject;
    const pct = (state.splitRatio * 100).toFixed(1);
    content.innerHTML = `<div class="detail-split">
      <div class="detail-subject-list" style="flex:0 0 ${pct}%">${renderSubjectTable(title, subjects)}</div>
      <div class="detail-split-handle"></div>
      <div class="detail-plot-area">${''}
      </div>
    </div>`;
    content.querySelectorAll('.subject-card').forEach((card) => {
      if (Number(card.dataset.subject) === selected) card.classList.add('selected');
    });
    bindSplitHandle(content.querySelector('.detail-split'));
    startPlotAnim();
  };

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
    content.innerHTML = renderListTab('Servers', buildServicesDetailItems(node.servers || []));
    return;
  }

  if (state.selectedDetailTab === 'clients') {
    stopPlotAnim();
    content.innerHTML = renderListTab('Clients', buildClientsDetailItems(node.clients || []));
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
