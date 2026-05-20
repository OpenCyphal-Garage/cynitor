// D3 line plot — multi-panel time series with crosshair tooltip, legend
// toggle, and resizable split layout. Extracted from detail-panel.js.

const PLOT_MARGIN = { top: 8, right: 12, bottom: 24, left: 48 };
const PLOT_PANEL_GAP = 8;
const PLOT_TIME_WINDOWS = [
  { label: '30s', secs: 30 },
  { label: '1m', secs: 60 },
  { label: '5m', secs: 300 },
  { label: '15m', secs: 900 },
  { label: 'All', secs: 0 },
];

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
  let tDataMin = Infinity, tDataMax = -Infinity;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.t < tDataMin) tDataMin = p.t;
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  const now = Date.now() / 1000;
  if (!isFinite(tDataMax)) tDataMax = now;
  if (!isFinite(tDataMin)) tDataMin = now - 60;

  const isSubjects = state.activeView === 'subjects';
  const windowSecs = isSubjects ? state.plotTimeWindow : 60;
  const anchor = isSubjects && state.plotPaused && state.plotPausedAt ? state.plotPausedAt : now;

  let domainLeft, domainRight;
  if (windowSecs === 0) {
    const pad = Math.max(2, (tDataMax - tDataMin) * 0.03);
    domainLeft = tDataMin - pad;
    domainRight = tDataMax + pad;
  } else {
    domainRight = anchor + windowSecs * 0.20;
    domainLeft = anchor - windowSecs;
  }

  const xScale = d3.scaleLinear()
    .domain([domainLeft, domainRight])
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

  return { xScale, yScales, panelH };
};

const buildPlotControls = () => {
  const wrap = document.createElement('div');
  wrap.className = 'plot-controls';
  if (state.activeView !== 'subjects') return wrap;

  const pauseBtn = document.createElement('button');
  pauseBtn.className = `plot-pause-btn${state.plotPaused ? ' active' : ''}`;
  pauseBtn.type = 'button';
  pauseBtn.setAttribute('aria-label', 'Pause plot');
  pauseBtn.textContent = state.plotPaused ? '▶' : '⏸';
  pauseBtn.addEventListener('click', () => {
    state.plotPaused = !state.plotPaused;
    state.plotPausedAt = state.plotPaused ? Date.now() / 1000 : null;
    pauseBtn.textContent = state.plotPaused ? '▶' : '⏸';
    pauseBtn.classList.toggle('active', state.plotPaused);
    _lastPlotFingerprint = '';
    if (!state.plotPaused) startPlotAnim();
  });
  wrap.appendChild(pauseBtn);

  for (const tw of PLOT_TIME_WINDOWS) {
    const btn = document.createElement('button');
    btn.className = `plot-window-btn${state.plotTimeWindow === tw.secs ? ' active' : ''}`;
    btn.type = 'button';
    btn.textContent = tw.label;
    btn.dataset.secs = tw.secs;
    btn.addEventListener('click', () => {
      state.plotTimeWindow = tw.secs;
      wrap.querySelectorAll('.plot-window-btn').forEach((b) =>
        b.classList.toggle('active', Number(b.dataset.secs) === tw.secs));
      _lastPlotFingerprint = '';
      saveSettings();
      if (state.plotPaused) renderPlot(el('selectedNodeContent'));
    });
    wrap.appendChild(btn);
  }

  const sep = document.createElement('span');
  sep.className = 'plot-controls-sep';
  wrap.appendChild(sep);

  const smoothGroup = document.createElement('div');
  smoothGroup.className = 'plot-slider-group';
  const smoothLbl = document.createElement('span');
  smoothLbl.className = 'plot-slider-label';
  smoothLbl.textContent = 'Smooth';
  smoothGroup.appendChild(smoothLbl);
  const smoothSlider = document.createElement('input');
  smoothSlider.type = 'range';
  smoothSlider.className = 'plot-slider';
  smoothSlider.min = '0';
  smoothSlider.max = '30';
  smoothSlider.step = '5';
  smoothSlider.value = String(state.plotSmooth);
  smoothSlider.setAttribute('aria-label', 'Interpolation frequency (Hz)');
  smoothSlider.addEventListener('input', () => {
    state.plotSmooth = Number(smoothSlider.value);
    if (state.plotSmooth === 0) flushAllInterpolations();
    _lastPlotFingerprint = '';
    saveSettings();
    if (state.plotPaused) renderPlot(el('selectedNodeContent'));
  });
  smoothGroup.appendChild(smoothSlider);
  const smoothInfo = document.createElement('span');
  smoothInfo.className = 'plot-info-icon';
  smoothInfo.textContent = '?';
  smoothInfo.title = 'Feeds interpolated points between samples at the selected Hz rate — one sample period delay';
  smoothInfo.setAttribute('aria-label', 'Feeds interpolated points between samples at the selected Hz rate — one sample period delay');
  smoothGroup.appendChild(smoothInfo);
  wrap.appendChild(smoothGroup);

  const dotsLabel = document.createElement('label');
  dotsLabel.className = 'plot-check-label';
  const dotsCb = document.createElement('input');
  dotsCb.type = 'checkbox';
  dotsCb.checked = state.plotDisconnectPoints;
  dotsCb.setAttribute('aria-label', 'Show disconnected points');
  dotsCb.addEventListener('change', () => {
    state.plotDisconnectPoints = dotsCb.checked;
    _lastPlotFingerprint = '';
    saveSettings();
    if (state.plotPaused) renderPlot(el('selectedNodeContent'));
  });
  dotsLabel.appendChild(dotsCb);
  dotsLabel.append(' Points');
  wrap.appendChild(dotsLabel);

  const strokeGroup = document.createElement('div');
  strokeGroup.className = 'plot-slider-group';
  const strokeLbl = document.createElement('span');
  strokeLbl.className = 'plot-slider-label';
  strokeLbl.textContent = 'Size';
  strokeGroup.appendChild(strokeLbl);
  const strokeSlider = document.createElement('input');
  strokeSlider.type = 'range';
  strokeSlider.className = 'plot-slider';
  strokeSlider.min = '1';
  strokeSlider.max = '5';
  strokeSlider.step = '0.5';
  strokeSlider.value = String(state.plotStroke);
  strokeSlider.setAttribute('aria-label', 'Line and point size');
  strokeSlider.addEventListener('input', () => {
    state.plotStroke = Number(strokeSlider.value);
    _lastPlotFingerprint = '';
    saveSettings();
    if (state.plotPaused) renderPlot(el('selectedNodeContent'));
  });
  strokeGroup.appendChild(strokeSlider);
  wrap.appendChild(strokeGroup);

  return wrap;
};

const setupPlotSvg = (plotArea, margin) => {
  plotArea.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'plot-header';
  const titleNode = document.createElement('div');
  titleNode.className = 'plot-title';
  header.appendChild(titleNode);
  const controls = buildPlotControls();
  if (controls.childElementCount) header.appendChild(controls);
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
    .append('path').attr('fill', 'none');
  panelsEnter.append('g').attr('class', 'panel-dots');
  panelsEnter.append('text').attr('class', 'panel-label').attr('x', 4).attr('y', 11);
  panels.exit().remove();

  const isSubjects = state.activeView === 'subjects';

  panelsG.selectAll('.plot-panel').each(function (d, i) {
    const yScale = yScales[i];
    const color = PLOT_COLORS[i % PLOT_COLORS.length];
    const panel = d3.select(this);
    panel.attr('transform', `translate(0, ${i * (panelH + PLOT_PANEL_GAP)})`);
    panel.select('clipPath rect').attr('width', w).attr('height', panelH);
    panel.select('.panel-y-axis').call(d3.axisLeft(yScale).ticks(3).tickSize(2));
    const lineGen = d3.line().x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
    const showLine = isSubjects ? !state.plotDisconnectPoints : true;
    panel.select('.panel-line')
      .attr('clip-path', `url(#panel-clip-${sid}-${d.name})`)
      .select('path')
      .attr('stroke', color)
      .attr('stroke-width', isSubjects ? state.plotStroke : 1.5)
      .attr('d', showLine && d.data.length >= 2 ? lineGen(d.data) : null)
      .attr('opacity', showLine && d.data.length >= 2 ? 1 : 0);

    const dotsG = panel.select('.panel-dots')
      .attr('clip-path', `url(#panel-clip-${sid}-${d.name})`);
    if (isSubjects && state.plotDisconnectPoints) {
      const visibleData = d.data.filter((p) => xScale(p.t) >= 0 && xScale(p.t) <= w);
      const maxDots = 600;
      const step = visibleData.length > maxDots ? Math.ceil(visibleData.length / maxDots) : 1;
      const sampled = step > 1 ? visibleData.filter((_, j) => j % step === 0) : visibleData;
      const dots = dotsG.selectAll('circle').data(sampled, (p) => p.t);
      dots.enter().append('circle').attr('fill', color)
        .merge(dots)
        .attr('r', state.plotStroke)
        .attr('cx', (p) => xScale(p.t))
        .attr('cy', (p) => yScale(p.v));
      dots.exit().remove();
    } else {
      dotsG.selectAll('circle').remove();
    }

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

let _lastPlotFingerprint = '';

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const allSeries = collectPlotSeries(sid);
  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const lastPts = allSeries.map((s) => s.data.length ? s.data[s.data.length - 1].t : 0);
  const isSubjects = state.activeView === 'subjects';
  const fp = isSubjects
    ? `${sid}:${allSeries.length}:${lastPts.join(',')}:v:s:w${state.plotTimeWindow}:p${state.plotPaused ? state.plotPausedAt : 0}:s${state.plotSmooth}:d${state.plotDisconnectPoints}:k${state.plotStroke}`
    : `${sid}:${allSeries.length}:${lastPts.join(',')}:v:n`;
  const rect = plotArea.getBoundingClientRect();
  const sizeKey = `${Math.round(rect.width)}x${Math.round(rect.height)}`;
  const fullFp = `${fp}:${sizeKey}`;
  if (fullFp === _lastPlotFingerprint) return;
  _lastPlotFingerprint = fullFp;

  if (!state.hiddenPlotSeries.has(sid)) {
    state.hiddenPlotSeries.set(sid, new Set());
  }
  const hidden = state.hiddenPlotSeries.get(sid);
  const visible = allSeries.filter((s) => !hidden.has(s.name));

  const w = rect.width - PLOT_MARGIN.left - PLOT_MARGIN.right;
  const headerEl = plotArea.querySelector('.plot-header');
  const HEADER_H = headerEl ? Math.max(28, Math.ceil(headerEl.getBoundingClientRect().height)) : 28;
  const totalPanelsH = rect.height - PLOT_MARGIN.top - PLOT_MARGIN.bottom - HEADER_H;
  if (w < 40 || totalPanelsH < 40) return;

  const { xScale, yScales, panelH } = computePlotScales(visible, w, totalPanelsH);

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels') || !plotArea.querySelector('.plot-header') || plotArea.dataset.plotView !== state.activeView) {
    gNode = setupPlotSvg(plotArea, PLOT_MARGIN);
    plotArea.dataset.plotView = state.activeView;
  }

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - HEADER_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    let ctx;
    if (state.activeView === 'subjects') {
      const event = state.latestBySubject.get(sid);
      ctx = event?.message_type || 'network';
    } else if (state.selectedDetailTab === 'subscribers') {
      ctx = 'network broadcast';
    } else {
      ctx = `published by node ${state.selectedNodeId ?? '?'}`;
    }
    const next = `Subject ${sid} · ${ctx}`;
    if (titleEl.textContent !== next) titleEl.textContent = next;
  }

  const g = d3.select(gNode);
  renderPanelLines(g, sid, visible, xScale, yScales, panelH, w, totalPanelsH);
  bindPlotTooltip(g, plotArea, visible, xScale, w, HEADER_H, rect);
  updatePlotLegend(plotArea, allSeries, hidden);
};

const stopPlotAnim = () => {
  if (state.plotTimer) {
    clearTimeout(state.plotTimer);
    state.plotTimer = null;
  }
  _lastPlotFingerprint = '';
};

const startPlotAnim = () => {
  stopPlotAnim();
  if (state.detailPanelCollapsed || state.selectedPlotSubject == null) return;
  const container = el('selectedNodeContent');
  if (state.activeView === 'subjects' && state.plotPaused) {
    renderPlot(container);
    return;
  }
  const tick = () => {
    if (state.detailPanelCollapsed) { state.plotTimer = null; return; }
    if (state.activeView === 'subjects' && state.plotPaused) { state.plotTimer = null; return; }
    if (state.activeView !== 'subjects') {
      const node = getSelectedNode();
      if (node?.has_disappeared) { state.plotTimer = null; return; }
    }
    renderPlot(container);
    state.plotTimer = window.setTimeout(tick, PLOT_TICK_MS);
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
