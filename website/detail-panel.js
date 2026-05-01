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

  // Hidden-series state is keyed by subject so unchecking "uptime" on
  // one subject doesn't carry over when the user clicks a different one.
  if (!state.hiddenPlotSeries.has(sid)) {
    state.hiddenPlotSeries.set(sid, new Set());
  }
  const hidden = state.hiddenPlotSeries.get(sid);

  const visible = allSeries.filter((s) => !hidden.has(s.name));

  const margin = { top: 8, right: 12, bottom: 24, left: 48 };
  const TITLE_H = 20;
  const LEGEND_H = 28;
  const PANEL_GAP = 8;
  const rect = plotArea.getBoundingClientRect();
  const w = rect.width - margin.left - margin.right;
  const totalPanelsH = rect.height - margin.top - margin.bottom - TITLE_H - LEGEND_H;
  if (w < 40 || totalPanelsH < 40) return;

  // Time domain across all visible series (shared x-axis)
  let tDataMin = Infinity, tDataMax = -Infinity;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.t < tDataMin) tDataMin = p.t;
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  if (!isFinite(tDataMin)) {
    tDataMin = Date.now() / 1000 - 60;
    tDataMax = Date.now() / 1000;
  }

  const WINDOW_SECS = 60;
  const now = Date.now() / 1000;
  const dataIsLive = (now - tDataMax) < PLOT_STALE_THRESHOLD;
  const tRight = dataIsLive ? now : tDataMax;
  const tWindowStart = tRight - WINDOW_SECS;
  const tWindowEnd = tRight + WINDOW_SECS * 0.5;
  const xScale = d3.scaleLinear().domain([tWindowStart, tWindowEnd]).range([0, w]);

  // Per-series y-scale: each attribute gets its own panel and its own
  // domain so a fast-growing uptime doesn't squash a small voltage.
  const numPanels = Math.max(1, visible.length);
  const panelH = (totalPanelsH - (numPanels - 1) * PANEL_GAP) / numPanels;
  const seriesScales = visible.map((s) => {
    let min = Infinity, max = -Infinity;
    for (const p of s.data) {
      if (p.v < min) min = p.v;
      if (p.v > max) max = p.v;
    }
    if (min === max) { min -= 1; max += 1; }
    const pad = (max - min) * 0.05;
    return d3.scaleLinear().domain([min - pad, max + pad]).range([panelH, 0]);
  });

  // One-time SVG setup. Detect both "no plot yet" and "old single-panel
  // structure" so a hot reload after the multi-panel refactor wipes
  // cleanly.
  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels')) {
    plotArea.innerHTML = '';
    const titleEl = document.createElement('div');
    titleEl.className = 'plot-title';
    plotArea.appendChild(titleEl);
    const svgEl = d3.select(plotArea).append('svg').attr('width', '100%');
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
    gNode = g.node();
  }

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - TITLE_H - LEGEND_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    const ctx = state.selectedDetailTab === 'subscribers'
      ? 'network broadcast'
      : `published by node ${state.selectedNodeId ?? '?'}`;
    const next = `Subject ${sid} · ${ctx}`;
    if (titleEl.textContent !== next) titleEl.textContent = next;
  }

  const g = d3.select(gNode);
  const panelsG = g.select('.plot-panels');

  // Stacked per-attribute panels. Data joined by attribute name so a
  // panel persists across renders and only its line/axis update.
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
    const yScale = seriesScales[i];
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    const panel = d3.select(this);
    const panelY = i * (panelH + PANEL_GAP);
    panel.attr('transform', `translate(0, ${panelY})`);
    panel.select('clipPath rect').attr('width', w).attr('height', panelH);
    panel.select('.panel-y-axis').call(d3.axisLeft(yScale).ticks(3).tickSize(2));
    const lineGen = d3.line()
      .x((p) => xScale(p.t))
      .y((p) => yScale(p.v))
      .curve(d3.curveLinear);
    panel.select('.panel-line')
      .attr('clip-path', `url(#panel-clip-${sid}-${d.name})`)
      .select('path')
      .attr('stroke', color)
      .attr('d', lineGen(d.data));
    panel.select('.panel-label')
      .text(d.name)
      .attr('fill', color);
  });

  // Shared x-axis at bottom of the stack
  const xAxis = d3.axisBottom(xScale).ticks(5).tickFormat(formatPlotTime);
  g.select('.plot-x-axis')
    .attr('transform', `translate(0, ${totalPanelsH})`)
    .call(xAxis);

  // Crosshair + overlay span the full panel stack
  g.select('.plot-overlay').attr('width', w).attr('height', totalPanelsH);
  g.select('.plot-crosshair').attr('y1', 0).attr('y2', totalPanelsH);

  const tooltipEl = plotArea.querySelector('.plot-tooltip');
  const overlay = g.select('.plot-overlay');
  const crosshair = g.select('.plot-crosshair');
  const bisect = d3.bisector((d) => d.t).left;

  overlay
    .on('mousemove', (event) => {
      const [mx] = d3.pointer(event);
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
          ? s.sample.v.toFixed(2)
          : String(s.sample.v);
        return `<div class="plot-tooltip-row"><span class="plot-tooltip-swatch" style="background:${color}"></span><span class="plot-tooltip-name">${escapeHtml(s.name)}</span><span class="plot-tooltip-val">${escapeHtml(v)}</span></div>`;
      }).join('');
      tooltipEl.innerHTML = `<div class="plot-tooltip-time">${formattedT}</div>${rows}`;
      tooltipEl.style.display = 'block';
      let tx = mx + margin.left + 12;
      const tw = tooltipEl.offsetWidth;
      if (tx + tw > rect.width - 4) tx = mx + margin.left - 12 - tw;
      tooltipEl.style.left = `${Math.max(4, tx)}px`;
      tooltipEl.style.top = `${TITLE_H + 4}px`;
    })
    .on('mouseleave', () => {
      crosshair.attr('opacity', 0);
      tooltipEl.style.display = 'none';
    });

  let legend = plotArea.querySelector('.plot-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.className = 'plot-legend';
    // Look up the active subject's hidden set fresh each click so the
    // listener stays correct after the user switches subjects. Restart
    // the plot tick so the change reflects immediately even when the
    // anim loop has paused for stale data.
    legend.addEventListener('click', (e) => {
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
      startPlotAnim();
    });
    plotArea.appendChild(legend);
  }
  legend.innerHTML = allSeries.map((s, i) => {
    const isActive = !hidden.has(s.name);
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    return `<button type="button" class="plot-legend-item${isActive ? ' active' : ''}" data-series="${escapeHtml(s.name)}" aria-pressed="${isActive}"><span class="plot-legend-swatch" style="background:${color}"></span>${escapeHtml(s.name)}</button>`;
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
