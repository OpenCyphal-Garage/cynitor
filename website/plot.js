// D3 line plot — multi-panel time series with crosshair tooltip, legend
// toggle, and resizable split layout. Extracted from detail-panel.js.

const PLOT_MARGIN = { top: 8, right: 12, bottom: 24, left: 48 };
const PLOT_PANEL_GAP = 8;
const PLOT_GAP_THRESHOLD = 3;
const _safeId = (s) => s.replace(/[^a-zA-Z0-9_-]/g, '_');
const _safeColor = (c) => /^#[0-9a-fA-F]{3,8}$/.test(c) ? c : '#888';
const PLOT_TIME_WINDOWS = [
  { label: '30s', secs: 30 },
  { label: '1m', secs: 60 },
  { label: '5m', secs: 300 },
  { label: '15m', secs: 900 },
  { label: 'All', secs: 0 },
];

const DERIVED_TYPES = {
  delta:       { label: 'Delta (A−B)',   sources: 2, hasWindow: false },
  rolling_avg: { label: 'Rolling Avg',        sources: 1, hasWindow: true  },
  min_max:     { label: 'Min/Max',             sources: 1, hasWindow: false },
  rate:        { label: 'Rate (dv/dt)',       sources: 1, hasWindow: false },
  ratio:       { label: 'Ratio (A/B)',        sources: 2, hasWindow: false },
};

const _subjectsPlotCfg = {
  get paused() { return state.plotPaused; },
  set paused(v) { state.plotPaused = v; },
  get pausedAt() { return state.plotPausedAt; },
  set pausedAt(v) { state.plotPausedAt = v; },
  get timeWindow() { return state.plotTimeWindow; },
  set timeWindow(v) { state.plotTimeWindow = v; },
  get smooth() { return state.plotSmooth; },
  set smooth(v) { state.plotSmooth = v; },
  get stroke() { return state.plotStroke; },
  set stroke(v) { state.plotStroke = v; },
  get disconnectPoints() { return state.plotDisconnectPoints; },
  set disconnectPoints(v) { state.plotDisconnectPoints = v; },
  get grid() { return state.plotGrid; },
  set grid(v) { state.plotGrid = v; },
  get _resumeFrom() { return state._plotResumeFrom; },
  set _resumeFrom(v) { state._plotResumeFrom = v; },
  get _resumeStart() { return state._plotResumeStart; },
  set _resumeStart(v) { state._plotResumeStart = v; },
};

const _colorToHex = (str) => {
  if (!str) return '#58a6ff';
  if (str.startsWith('#')) {
    if (str.length === 4) return `#${str[1]}${str[1]}${str[2]}${str[2]}${str[3]}${str[3]}`;
    return str;
  }
  const m = str.match(/\d+/g);
  if (m && m.length >= 3) return '#' + m.slice(0, 3).map(n => (+n).toString(16).padStart(2, '0')).join('');
  return '#58a6ff';
};

const _openSwatchPicker = (swatch, currentColor, onChange) => {
  if (swatch._pickerOpen) return;
  swatch._pickerOpen = true;
  const input = document.createElement('input');
  input.type = 'color';
  input.value = _colorToHex(currentColor);
  input.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;opacity:0;cursor:pointer;border:none;padding:0';
  swatch.appendChild(input);
  let committed = false;
  input.addEventListener('input', () => {
    swatch.style.background = input.value;
  });
  input.addEventListener('change', () => {
    committed = true;
    const val = input.value;
    if (input.parentNode) input.remove();
    swatch._pickerOpen = false;
    onChange(val);
  });
  const cleanup = () => {
    if (committed) return;
    setTimeout(() => {
      if (!committed && input.parentNode) {
        input.remove();
        swatch._pickerOpen = false;
        swatch.style.background = currentColor;
      }
    }, 300);
  };
  input.addEventListener('blur', cleanup);
  if (input.showPicker) {
    try { input.showPicker(); } catch (_) { input.click(); }
  } else {
    input.click();
  }
};

const _resetSmoothCaches = (cfg) => {
  cfg._smoothBufs = null;
  cfg._rawCursors = null;
  cfg._activeInterps = null;
};

const _processSmooth = (cfg, keys) => {
  if (!cfg.smooth || cfg.smooth <= 0) return;
  if (!cfg._smoothBufs) cfg._smoothBufs = new Map();
  if (!cfg._rawCursors) cfg._rawCursors = new Map();
  if (!cfg._activeInterps) cfg._activeInterps = new Map();

  const now = Date.now();
  for (const key of keys) {
    const raw = state.subjectHistory.get(key);
    if (!raw || raw.length < 1) continue;

    if (!cfg._smoothBufs.has(key)) cfg._smoothBufs.set(key, []);
    const buf = cfg._smoothBufs.get(key);
    const cursor = cfg._rawCursors.get(key) || 0;

    for (let i = cursor; i < raw.length; i++) {
      const pt = raw[i];
      const ip = cfg._activeInterps.get(key);
      if (ip) {
        buf.push({ t: ip.to.t, v: ip.to.v });
        if (buf.length > 7200) buf.shift();
        cfg._activeInterps.delete(key);
      }
      if (buf.length > 0) {
        const prev = buf[buf.length - 1];
        const gap = pt.t - prev.t;
        const steps = Math.max(1, Math.round(cfg.smooth * gap));
        if (steps <= 1) {
          buf.push(pt);
          if (buf.length > 7200) buf.shift();
        } else {
          cfg._activeInterps.set(key, {
            from: prev, to: pt, step: 0, totalSteps: steps,
            intervalMs: (gap * 1000) / steps, lastPush: now,
          });
        }
      } else {
        buf.push(pt);
      }
    }
    cfg._rawCursors.set(key, raw.length);

    const ip = cfg._activeInterps.get(key);
    if (ip) {
      while (ip.step < ip.totalSteps && now - ip.lastPush >= ip.intervalMs) {
        ip.step++;
        ip.lastPush += ip.intervalMs;
        const frac = ip.step / ip.totalSteps;
        buf.push({
          t: ip.from.t + (ip.to.t - ip.from.t) * frac,
          v: ip.from.v + (ip.to.v - ip.from.v) * frac,
        });
        if (buf.length > 7200) buf.shift();
      }
      if (ip.step >= ip.totalSteps) cfg._activeInterps.delete(key);
    }
  }
};

const _getSmoothBuf = (cfg, key) => {
  if (cfg.smooth > 0 && cfg._smoothBufs) {
    const buf = cfg._smoothBufs.get(key);
    if (buf && buf.length >= 2) return buf;
  }
  return state.subjectHistory.get(key);
};

const _tagGaps = (data) => {
  for (let i = 0; i < data.length; i++) {
    data[i]._gap = i > 0 && data[i].t - data[i - 1].t > PLOT_GAP_THRESHOLD;
  }
};

const _ALIGN_TOLERANCE = 2;

const _alignSeries = (dataA, dataB) => {
  if (!dataA?.length || !dataB?.length) return [];
  const result = [];
  let j = 0;
  for (const a of dataA) {
    while (j < dataB.length - 1 && dataB[j + 1].t <= a.t) j++;
    let best = dataB[j];
    if (j + 1 < dataB.length && Math.abs(dataB[j + 1].t - a.t) < Math.abs(best.t - a.t)) {
      best = dataB[j + 1];
    }
    if (Math.abs(best.t - a.t) <= _ALIGN_TOLERANCE) {
      result.push({ t: a.t, vA: a.v, vB: best.v });
    }
  }
  return result;
};

const _computeDerived = (type, dataA, dataB, windowSize) => {
  switch (type) {
    case 'delta': {
      const aligned = _alignSeries(dataA, dataB);
      return [aligned.map(p => ({ t: p.t, v: p.vA - p.vB }))];
    }
    case 'ratio': {
      const aligned = _alignSeries(dataA, dataB);
      return [aligned.filter(p => p.vB !== 0).map(p => ({ t: p.t, v: p.vA / p.vB }))];
    }
    case 'rate': {
      if (!dataA || dataA.length < 2) return [[]];
      const result = [];
      for (let i = 1; i < dataA.length; i++) {
        const dt = dataA[i].t - dataA[i - 1].t;
        if (dt > 0 && dt <= PLOT_GAP_THRESHOLD) {
          result.push({ t: dataA[i].t, v: (dataA[i].v - dataA[i - 1].v) / dt });
        }
      }
      return [result];
    }
    case 'rolling_avg': {
      if (!dataA || dataA.length < 2) return [[]];
      const w = Math.max(2, windowSize || 10);
      const result = [];
      let sum = 0;
      for (let i = 0; i < dataA.length; i++) {
        sum += dataA[i].v;
        if (i >= w) sum -= dataA[i - w].v;
        const count = Math.min(i + 1, w);
        result.push({ t: dataA[i].t, v: sum / count });
      }
      return [result];
    }
    case 'min_max': {
      if (!dataA || dataA.length < 2) return [[], []];
      const tMin = windowSize > 0 ? dataA[dataA.length - 1].t - windowSize : -Infinity;
      let lo = Infinity, hi = -Infinity;
      for (const p of dataA) {
        if (p.t < tMin) continue;
        if (p.v < lo) lo = p.v;
        if (p.v > hi) hi = p.v;
      }
      if (!isFinite(lo)) return [[], []];
      const t0 = Math.max(dataA[0].t, tMin === -Infinity ? dataA[0].t : tMin);
      const t1 = dataA[dataA.length - 1].t;
      return [
        [{ t: t0, v: lo }, { t: t1, v: lo }],
        [{ t: t0, v: hi }, { t: t1, v: hi }],
      ];
    }
    default:
      return [];
  }
};

const _derivedLabel = (d) => {
  const nameA = d.sourceA ? `S${d.sourceA.split(':')[0]} · ${d.sourceA.split(':')[1]}` : '?';
  const nameB = d.sourceB ? `S${d.sourceB.split(':')[0]} · ${d.sourceB.split(':')[1]}` : '';
  switch (d.type) {
    case 'delta': return `Δ(${nameA} − ${nameB})`;
    case 'ratio': return `${nameA} / ${nameB}`;
    case 'rolling_avg': return `Avg${d.window || 10}(${nameA})`;
    case 'rate': return `d/dt(${nameA})`;
    default: return '?';
  }
};

const _derivedMinMaxLabels = (d) => {
  const nameA = d.sourceA ? `S${d.sourceA.split(':')[0]} · ${d.sourceA.split(':')[1]}` : '?';
  return [`Min(${nameA})`, `Max(${nameA})`];
};

const _nextDerivedId = (graph) => {
  let max = 0;
  for (const d of graph.derivedSeries || []) {
    const m = d.id?.match(/^ds_(\d+)$/);
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `ds_${max + 1}`;
};

const collectPlotSeries = (sid, cfg = null) => {
  const keys = [];
  for (const key of state.subjectHistory.keys()) {
    if (key.startsWith(sid + ':')) keys.push(key);
  }
  if (cfg) _processSmooth(cfg, keys);
  const allSeries = [];
  for (const key of keys) {
    const buf = cfg ? _getSmoothBuf(cfg, key) : state.subjectHistory.get(key);
    if (!buf || buf.length < 2) continue;
    _tagGaps(buf);
    allSeries.push({ name: key.split(':')[1], data: buf });
  }
  return allSeries;
};

const computePlotScales = (visible, w, totalPanelsH, compareSeries = [], cfg = null) => {
  let tDataMin = Infinity, tDataMax = -Infinity;
  for (const s of [...visible, ...compareSeries]) {
    for (const p of s.data) {
      if (p.t < tDataMin) tDataMin = p.t;
      if (p.t > tDataMax) tDataMax = p.t;
    }
  }
  const now = Date.now() / 1000;
  if (!isFinite(tDataMax)) tDataMax = now;
  if (!isFinite(tDataMin)) tDataMin = now - 60;

  const RESUME_DURATION = 2;
  const _resumeAnchor = (resumeFrom, resumeStart) => {
    if (!resumeFrom || !resumeStart) return now;
    const elapsed = now - resumeStart;
    if (elapsed >= RESUME_DURATION) return now;
    const t = elapsed / RESUME_DURATION;
    return resumeFrom + (now - resumeFrom) * t * t;
  };

  // During replay, the events carry their original (recorded) timestamps —
  // potentially hours, days, or years before "now". Anchoring the plot's
  // right edge to wall-clock time would push every replay point off the
  // left edge of the visible window. Use the latest event timestamp seen
  // so far instead, so the plot tracks the replay head as events arrive.
  const replayAnchor = state.replayActive ? tDataMax : null;

  let windowSecs, anchor;
  if (cfg) {
    windowSecs = cfg.timeWindow;
    if (cfg.paused && cfg.pausedAt) {
      anchor = cfg.pausedAt;
    } else if (cfg._resumeFrom) {
      anchor = _resumeAnchor(cfg._resumeFrom, cfg._resumeStart);
      if (now - cfg._resumeStart >= RESUME_DURATION) { cfg._resumeFrom = null; cfg._resumeStart = null; }
    } else {
      anchor = replayAnchor ?? now;
    }
  } else if (state.activeView === 'subjects') {
    windowSecs = state.plotTimeWindow;
    if (state.plotPaused && state.plotPausedAt) {
      anchor = state.plotPausedAt;
    } else if (state._plotResumeFrom) {
      anchor = _resumeAnchor(state._plotResumeFrom, state._plotResumeStart);
      if (now - state._plotResumeStart >= RESUME_DURATION) { state._plotResumeFrom = null; state._plotResumeStart = null; }
    } else {
      anchor = replayAnchor ?? now;
    }
  } else {
    windowSecs = 60;
    anchor = replayAnchor ?? now;
  }

  let domainLeft, domainRight;
  if (windowSecs === 0) {
    const pad = Math.max(2, (tDataMax - tDataMin) * 0.03);
    domainLeft = tDataMin - pad;
    domainRight = tDataMax + pad;
  } else {
    domainRight = anchor + windowSecs * 0.20;
    domainLeft = anchor - windowSecs;
  }

  if (cfg && cfg._zoom && cfg._zoom !== 1) {
    const span = domainRight - domainLeft;
    const center = (domainLeft + domainRight) / 2 + (cfg._panOffset || 0);
    const zoomedSpan = span / cfg._zoom;
    domainLeft = center - zoomedSpan / 2;
    domainRight = center + zoomedSpan / 2;
  } else if (cfg && cfg._panOffset) {
    domainLeft += cfg._panOffset;
    domainRight += cfg._panOffset;
  }

  const xScale = d3.scaleLinear()
    .domain([domainLeft, domainRight])
    .range([0, w]);

  const extraPanels = compareSeries.length > 0 ? 1 : 0;
  const numPanels = Math.max(1, visible.length + extraPanels);
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

const _estimateMaxHz = (cfg) => {
  let keys;
  if (cfg.series) {
    keys = cfg.series.map(s => `${s.subjectId}:${s.attribute}`);
  } else {
    const sid = state.selectedPlotSubject;
    if (sid == null) return 0;
    keys = [];
    for (const key of state.subjectHistory.keys()) {
      if (key.startsWith(sid + ':')) keys.push(key);
    }
  }
  let maxHz = 0;
  for (const key of keys) {
    const buf = state.subjectHistory.get(key);
    if (!buf || buf.length < 3) continue;
    const n = Math.min(20, buf.length);
    const start = buf.length - n;
    const dt = buf[buf.length - 1].t - buf[start].t;
    if (dt > 0) maxHz = Math.max(maxHz, (n - 1) / dt);
  }
  return maxHz;
};

const buildPlotControls = (opts = {}) => {
  const wrap = document.createElement('div');
  wrap.className = 'plot-controls';

  const cfg = opts.cfg || (state.activeView === 'subjects' ? _subjectsPlotCfg : null);
  if (!cfg) return wrap;

  const invalidate = opts.invalidate || _plotInvalidate;
  const rerender = opts.rerender || (() => renderPlot(el('selectedNodeContent')));
  const restart = opts.restart || (() => startPlotAnim());

  const pauseBtn = document.createElement('button');
  pauseBtn.className = `plot-pause-btn${cfg.paused ? ' active' : ''}`;
  pauseBtn.type = 'button';
  pauseBtn.setAttribute('aria-label', 'Pause plot');
  pauseBtn.textContent = cfg.paused ? '▶' : '⏸';
  pauseBtn.addEventListener('click', () => {
    if (cfg.paused) {
      cfg._resumeFrom = cfg.pausedAt;
      cfg._resumeStart = Date.now() / 1000;
      _resetSmoothCaches(cfg);
    }
    cfg.paused = !cfg.paused;
    cfg.pausedAt = cfg.paused ? Date.now() / 1000 : null;
    pauseBtn.textContent = cfg.paused ? '▶' : '⏸';
    pauseBtn.classList.toggle('active', cfg.paused);
    invalidate();
    if (!cfg.paused) restart();
  });
  wrap.appendChild(pauseBtn);

  for (const tw of PLOT_TIME_WINDOWS) {
    const btn = document.createElement('button');
    btn.className = `plot-window-btn${cfg.timeWindow === tw.secs ? ' active' : ''}`;
    btn.type = 'button';
    btn.textContent = tw.label;
    btn.dataset.secs = tw.secs;
    btn.addEventListener('click', () => {
      cfg.timeWindow = tw.secs;
      cfg._zoom = 1;
      cfg._panOffset = 0;
      (btn.parentElement || wrap).querySelectorAll('.plot-window-btn').forEach((b) =>
        b.classList.toggle('active', Number(b.dataset.secs) === tw.secs));
      invalidate();
      saveSettings();
      if (cfg.paused) rerender();
    });
    wrap.appendChild(btn);
  }

  const sep = document.createElement('span');
  sep.className = 'plot-controls-sep';
  wrap.appendChild(sep);

  const fillLabel = document.createElement('label');
  fillLabel.className = 'plot-check-label';
  const fillCb = document.createElement('input');
  fillCb.type = 'checkbox';
  fillCb.checked = cfg.smooth > 0;
  fillCb.setAttribute('aria-label', 'Fill rate interpolation');
  fillCb.addEventListener('change', () => {
    cfg.smooth = fillCb.checked ? 20 : 0;
    cfg._smoothBufs = null;
    cfg._rawCursors = null;
    cfg._activeInterps = null;
    invalidate();
    saveSettings();
    if (cfg.paused) rerender();
  });
  fillLabel.appendChild(fillCb);
  fillLabel.append(' Fill Rate');
  const fillInfo = document.createElement('span');
  fillInfo.className = 'plot-info-icon';
  fillInfo.textContent = '?';
  const fillTip = 'Interpolates to 20 Hz for slow signals — disabled when signal is already fast enough';
  fillInfo.title = fillTip;
  fillInfo.setAttribute('aria-label', fillTip);
  cfg._updateFillRate = () => {
    const now = Date.now();
    if (cfg._lastFillCheck && now - cfg._lastFillCheck < 2000) return;
    cfg._lastFillCheck = now;
    const maxHz = _estimateMaxHz(cfg);
    const tooFast = maxHz >= 20;
    fillCb.disabled = tooFast;
    if (tooFast && cfg.smooth > 0) {
      cfg.smooth = 0;
      fillCb.checked = false;
      cfg._smoothBufs = null;
      cfg._rawCursors = null;
      cfg._activeInterps = null;
      invalidate();
      saveSettings();
    }
  };
  cfg._updateFillRate();
  wrap.appendChild(fillLabel);
  wrap.appendChild(fillInfo);

  const sep2 = document.createElement('span');
  sep2.className = 'plot-controls-sep';
  wrap.appendChild(sep2);

  const strokeGroup = document.createElement('div');
  strokeGroup.className = 'plot-slider-group';
  const strokeLbl = document.createElement('span');
  strokeLbl.className = 'plot-slider-label';
  strokeLbl.textContent = 'Line width';
  strokeGroup.appendChild(strokeLbl);
  const strokeSlider = document.createElement('input');
  strokeSlider.type = 'range';
  strokeSlider.className = 'plot-slider';
  strokeSlider.min = '1';
  strokeSlider.max = '5';
  strokeSlider.step = '0.5';
  strokeSlider.value = String(cfg.stroke);
  strokeSlider.setAttribute('aria-label', 'Line width');
  strokeSlider.addEventListener('input', () => {
    cfg.stroke = Number(strokeSlider.value);
    invalidate();
    saveSettings();
    if (cfg.paused) rerender();
  });
  strokeGroup.appendChild(strokeSlider);
  wrap.appendChild(strokeGroup);

  const dotsLabel = document.createElement('label');
  dotsLabel.className = 'plot-check-label';
  const dotsCb = document.createElement('input');
  dotsCb.type = 'checkbox';
  dotsCb.checked = cfg.disconnectPoints;
  dotsCb.setAttribute('aria-label', 'Show disconnected points');
  dotsCb.addEventListener('change', () => {
    cfg.disconnectPoints = dotsCb.checked;
    invalidate();
    saveSettings();
    if (cfg.paused) rerender();
  });
  dotsLabel.appendChild(dotsCb);
  dotsLabel.append(' Points');
  wrap.appendChild(dotsLabel);

  const gridLabel = document.createElement('label');
  gridLabel.className = 'plot-check-label';
  const gridCb = document.createElement('input');
  gridCb.type = 'checkbox';
  gridCb.checked = cfg.grid;
  gridCb.setAttribute('aria-label', 'Show grid lines');
  gridCb.addEventListener('change', () => {
    cfg.grid = gridCb.checked;
    invalidate();
    saveSettings();
    if (cfg.paused) rerender();
  });
  gridLabel.appendChild(gridCb);
  gridLabel.append(' Grid');
  wrap.appendChild(gridLabel);

  if (opts.includeDraw) {
    const drawSep = document.createElement('span');
    drawSep.className = 'plot-controls-sep';
    wrap.appendChild(drawSep);

    const drawGroup = document.createElement('div');
    drawGroup.className = 'plot-draw-group';

    const drawIcon = document.createElement('span');
    drawIcon.className = 'plot-draw-icon';
    drawIcon.textContent = '✎';
    drawGroup.appendChild(drawIcon);

    const drawInfo = document.createElement('span');
    drawInfo.className = 'plot-info-icon';
    drawInfo.textContent = '?';
    const drawTip = 'Shift+click: add/edit marker · Click on marker: edit · Alt+drag: freehand draw · Alt+dblclick: clear drawings · Click: pause/resume · Dblclick: reset zoom';
    drawInfo.title = drawTip;
    drawInfo.setAttribute('aria-label', drawTip);
    drawGroup.appendChild(drawInfo);

    const drawColorWrap = document.createElement('div');
    drawColorWrap.className = 'plot-draw-color-wrap';
    const drawSwatch = document.createElement('span');
    drawSwatch.className = 'plot-draw-swatch';
    drawSwatch.style.background = cfg._drawColor || '#ef4444';
    const drawColorInput = document.createElement('input');
    drawColorInput.type = 'color';
    drawColorInput.value = _colorToHex(cfg._drawColor || '#ef4444');
    drawColorInput.setAttribute('aria-label', 'Drawing color');
    drawColorInput.addEventListener('input', () => {
      cfg._drawColor = drawColorInput.value;
      drawSwatch.style.background = drawColorInput.value;
    });
    drawColorWrap.appendChild(drawSwatch);
    drawColorWrap.appendChild(drawColorInput);
    drawGroup.appendChild(drawColorWrap);

    const drawStyleSel = document.createElement('select');
    drawStyleSel.className = 'plot-draw-style';
    drawStyleSel.setAttribute('aria-label', 'Drawing line style');
    for (const key of ['solid', 'dashed', 'dotted']) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = key;
      drawStyleSel.appendChild(opt);
    }
    drawStyleSel.value = cfg._drawStyle || 'solid';
    drawStyleSel.addEventListener('change', () => { cfg._drawStyle = drawStyleSel.value; });
    drawGroup.appendChild(drawStyleSel);

    const drawSizeSlider = document.createElement('input');
    drawSizeSlider.type = 'range';
    drawSizeSlider.className = 'plot-slider plot-draw-size';
    drawSizeSlider.min = '1';
    drawSizeSlider.max = '6';
    drawSizeSlider.step = '0.5';
    drawSizeSlider.value = String(cfg._drawWidth || 2);
    drawSizeSlider.setAttribute('aria-label', 'Drawing line size');
    drawSizeSlider.addEventListener('input', () => { cfg._drawWidth = Number(drawSizeSlider.value); });
    drawGroup.appendChild(drawSizeSlider);

    wrap.appendChild(drawGroup);
  }

  return wrap;
};

const _getCompareSubjects = () => {
  const subjects = new Map();
  for (const key of state.subjectHistory.keys()) {
    const [sidStr, attr] = key.split(':');
    const sid = Number(sidStr);
    if (!subjects.has(sid)) subjects.set(sid, []);
    subjects.get(sid).push(attr);
  }
  return subjects;
};

const _refreshCompareSubjects = (panel) => {
  const sel = panel.querySelector('.plot-compare-subject');
  if (!sel) return;
  const prev = sel.value;
  const subjects = _getCompareSubjects();
  sel.innerHTML = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = 'Subject…';
  sel.appendChild(def);
  for (const [sid] of subjects) {
    const event = state.latestBySubject.get(sid);
    const label = event?.message_type ? `S${sid} · ${event.message_type}` : `Subject ${sid}`;
    const opt = document.createElement('option');
    opt.value = sid;
    opt.textContent = label;
    sel.appendChild(opt);
  }
  if (prev && sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
};

const buildComparePanel = (graph, onUpdate) => {
  const panel = document.createElement('div');
  panel.className = 'plot-compare-panel';

  const hdr = document.createElement('div');
  hdr.className = 'plot-compare-hdr';
  const title = document.createElement('span');
  title.textContent = 'Series';
  hdr.appendChild(title);
  panel.appendChild(hdr);

  const picker = document.createElement('div');
  picker.className = 'plot-compare-picker';

  const subjectSel = document.createElement('select');
  subjectSel.className = 'plot-compare-subject';
  subjectSel.setAttribute('aria-label', 'Subject to compare');
  const defSubj = document.createElement('option');
  defSubj.value = '';
  defSubj.textContent = 'Subject…';
  subjectSel.appendChild(defSubj);

  const attrSel = document.createElement('select');
  attrSel.className = 'plot-compare-attr';
  attrSel.disabled = true;
  attrSel.setAttribute('aria-label', 'Attribute to compare');
  const defAttr = document.createElement('option');
  defAttr.value = '';
  defAttr.textContent = 'Attribute…';
  attrSel.appendChild(defAttr);

  const addBtn = document.createElement('button');
  addBtn.className = 'plot-compare-add';
  addBtn.type = 'button';
  addBtn.textContent = 'Add';
  addBtn.disabled = true;
  addBtn.setAttribute('aria-label', 'Add comparison series');

  subjectSel.addEventListener('mousedown', () => _refreshCompareSubjects(panel));

  subjectSel.addEventListener('change', () => {
    const sid = Number(subjectSel.value);
    attrSel.innerHTML = '';
    const def = document.createElement('option');
    def.value = '';
    def.textContent = 'Attribute…';
    attrSel.appendChild(def);
    if (sid) {
      const subjects = _getCompareSubjects();
      for (const a of subjects.get(sid) || []) {
        const opt = document.createElement('option');
        opt.value = a;
        opt.textContent = a;
        attrSel.appendChild(opt);
      }
      attrSel.disabled = false;
    } else {
      attrSel.disabled = true;
    }
    addBtn.disabled = true;
  });

  attrSel.addEventListener('mousedown', () => {
    const sid = Number(subjectSel.value);
    if (!sid) return;
    const prev = attrSel.value;
    attrSel.innerHTML = '';
    const def = document.createElement('option');
    def.value = '';
    def.textContent = 'Attribute…';
    attrSel.appendChild(def);
    const subjects = _getCompareSubjects();
    for (const a of subjects.get(sid) || []) {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = a;
      attrSel.appendChild(opt);
    }
    if (prev && attrSel.querySelector(`option[value="${prev}"]`)) attrSel.value = prev;
  });

  attrSel.addEventListener('change', () => {
    addBtn.disabled = !attrSel.value;
  });

  addBtn.addEventListener('click', () => {
    const sid = Number(subjectSel.value);
    const attr = attrSel.value;
    if (!sid || !attr) return;
    if (graph.series.some((c) => c.subjectId === sid && c.attribute === attr)) return;
    graph.series.push({ subjectId: sid, attribute: attr });
    onUpdate();
    _refreshCompareSubjects(panel);
    subjectSel.value = '';
    attrSel.innerHTML = '<option value="">Attribute…</option>';
    attrSel.disabled = true;
    addBtn.disabled = true;
  });

  picker.appendChild(subjectSel);
  picker.appendChild(attrSel);
  picker.appendChild(addBtn);
  panel.appendChild(picker);

  const derivedSection = document.createElement('div');
  derivedSection.className = 'plot-derived-section';
  const derivedHdr = document.createElement('span');
  derivedHdr.className = 'plot-threshold-hdr';
  derivedHdr.textContent = 'Derived';
  derivedSection.appendChild(derivedHdr);

  const derivedPicker = document.createElement('div');
  derivedPicker.className = 'plot-compare-picker';

  const typeSel = document.createElement('select');
  typeSel.className = 'plot-derived-type';
  typeSel.setAttribute('aria-label', 'Derived series type');
  for (const [key, info] of Object.entries(DERIVED_TYPES)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = info.label;
    typeSel.appendChild(opt);
  }

  const srcASel = document.createElement('select');
  srcASel.className = 'plot-derived-source';
  srcASel.setAttribute('aria-label', 'Source A');

  const srcBSel = document.createElement('select');
  srcBSel.className = 'plot-derived-source';
  srcBSel.setAttribute('aria-label', 'Source B');

  const windowInput = document.createElement('input');
  windowInput.type = 'number';
  windowInput.className = 'plot-threshold-input';
  windowInput.placeholder = 'Window';
  windowInput.value = '10';
  windowInput.min = '2';
  windowInput.max = '200';
  windowInput.setAttribute('aria-label', 'Window size (samples)');

  const _refreshDerivedSources = () => {
    const buildOpts = (sel, label) => {
      const prev = sel.value;
      sel.innerHTML = '';
      const def = document.createElement('option');
      def.value = '';
      def.textContent = label;
      sel.appendChild(def);
      for (const s of graph.series) {
        const key = `${s.subjectId}:${s.attribute}`;
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = `S${s.subjectId} · ${s.attribute}`;
        sel.appendChild(opt);
      }
      if (prev && sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
    };
    buildOpts(srcASel, 'Source A…');
    buildOpts(srcBSel, 'Source B…');
  };

  const _updateDerivedVisibility = () => {
    const info = DERIVED_TYPES[typeSel.value];
    srcBSel.style.display = info?.sources === 2 ? '' : 'none';
    windowInput.style.display = info?.hasWindow ? '' : 'none';
  };

  typeSel.addEventListener('change', _updateDerivedVisibility);

  const derivedAddBtn = document.createElement('button');
  derivedAddBtn.className = 'plot-compare-add';
  derivedAddBtn.type = 'button';
  derivedAddBtn.textContent = 'Add';
  derivedAddBtn.setAttribute('aria-label', 'Add derived series');
  derivedAddBtn.addEventListener('click', () => {
    const type = typeSel.value;
    const info = DERIVED_TYPES[type];
    if (!info) return;
    const srcA = srcASel.value;
    if (!srcA) return;
    if (info.sources === 2 && !srcBSel.value) return;
    const srcB = info.sources === 2 ? srcBSel.value : '';
    const win = info.hasWindow ? Math.max(2, parseInt(windowInput.value) || 10) : 0;
    if (!graph.derivedSeries) graph.derivedSeries = [];
    graph.derivedSeries.push({
      id: _nextDerivedId(graph),
      type,
      sourceA: srcA,
      sourceB: srcB,
      window: win,
      color: '',
    });
    onUpdate();
  });

  derivedPicker.appendChild(typeSel);
  derivedPicker.appendChild(srcASel);
  derivedPicker.appendChild(srcBSel);
  derivedPicker.appendChild(windowInput);
  derivedPicker.appendChild(derivedAddBtn);
  derivedSection.appendChild(derivedPicker);

  panel.appendChild(derivedSection);

  panel._refreshDerivedSources = _refreshDerivedSources;
  _refreshDerivedSources();
  _updateDerivedVisibility();

  const thSection = document.createElement('div');
  thSection.className = 'plot-threshold-section';
  const thLabel = document.createElement('span');
  thLabel.className = 'plot-threshold-hdr';
  thLabel.textContent = 'Thresholds';
  thSection.appendChild(thLabel);
  const thPicker = document.createElement('div');
  thPicker.className = 'plot-threshold-picker';
  const thInput = document.createElement('input');
  thInput.type = 'number';
  thInput.className = 'plot-threshold-input';
  thInput.placeholder = 'Value';
  thInput.setAttribute('aria-label', 'Threshold value');
  const thNameInput = document.createElement('input');
  thNameInput.type = 'text';
  thNameInput.className = 'plot-threshold-name-input';
  thNameInput.placeholder = 'Label';
  thNameInput.setAttribute('aria-label', 'Threshold label');
  const thAddBtn = document.createElement('button');
  thAddBtn.className = 'plot-compare-add';
  thAddBtn.type = 'button';
  thAddBtn.textContent = 'Add';
  thAddBtn.setAttribute('aria-label', 'Add threshold line');
  thAddBtn.addEventListener('click', () => {
    const val = parseFloat(thInput.value);
    if (isNaN(val)) return;
    graph.thresholds.push({
      value: val,
      label: thNameInput.value.trim() || String(val),
      color: '#ef4444',
      style: 'dashed',
    });
    thInput.value = '';
    thNameInput.value = '';
    onUpdate();
  });
  thPicker.appendChild(thInput);
  thPicker.appendChild(thNameInput);
  thPicker.appendChild(thAddBtn);
  thSection.appendChild(thPicker);
  panel.appendChild(thSection);

  return panel;
};

const setupPlotSvg = (plotArea, margin, opts = {}) => {
  plotArea.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'plot-header';
  const titleNode = document.createElement('div');
  titleNode.className = 'plot-title';
  header.appendChild(titleNode);
  if (!opts.noControls) {
    const controls = buildPlotControls(opts);
    if (controls.childElementCount) header.appendChild(controls);
  }
  const legendNode = document.createElement('div');
  legendNode.className = 'plot-legend';
  const _legendCommit = () => {
    if (opts.invalidate) opts.invalidate();
    else _plotInvalidate();
    saveSettings();
    if (opts.restart) opts.restart();
    else _plotRestart();
  };
  const _legendGetThreshold = (btn) => {
    const ti = btn?.dataset.thresholdIdx;
    if (ti != null && opts.cfg?.thresholds) return opts.cfg.thresholds[Number(ti)];
    return null;
  };
  legendNode.addEventListener('click', (e) => {
    const swatch = e.target.closest('.plot-legend-swatch');
    if (swatch) {
      const btn = swatch.closest('.plot-legend-item[data-series]');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      const seriesName = btn.dataset.series;
      const th = _legendGetThreshold(btn);
      _openSwatchPicker(swatch, swatch.dataset.hex || '#58a6ff', (newColor) => {
        swatch.dataset.hex = newColor;
        if (th) {
          th.color = newColor;
        } else if (opts.cfg && opts.cfg.series) {
          const idx = opts.cfg.series.findIndex(s =>
            `S${s.subjectId} · ${s.attribute}` === seriesName
          );
          if (idx >= 0) {
            opts.cfg.series[idx].color = newColor;
          } else if (opts.cfg.derivedSeries) {
            const di = opts.cfg.derivedSeries.findIndex(d => {
              if (d.type === 'min_max') {
                const [minL, maxL] = _derivedMinMaxLabels(d);
                return seriesName === minL || seriesName === maxL;
              }
              return _derivedLabel(d) === seriesName;
            });
            if (di >= 0) opts.cfg.derivedSeries[di].color = newColor;
          }
        } else {
          const sid = state.selectedPlotSubject;
          if (sid != null) state.plotColorOverrides[`${sid}:${seriesName}`] = newColor;
        }
        _legendCommit();
      });
      return;
    }
    const removeEl = e.target.closest('.plot-legend-remove');
    if (removeEl) {
      e.preventDefault();
      e.stopPropagation();
      const rbtn = removeEl.closest('.plot-legend-item[data-series]');
      const ti = rbtn?.dataset.thresholdIdx;
      if (ti != null && opts.cfg?.thresholds) {
        opts.cfg.thresholds.splice(Number(ti), 1);
        _legendCommit();
        return;
      }
      const seriesName = rbtn?.dataset.series;
      const did = rbtn?.dataset.derivedId;
      let removed = false;
      if (did && opts.cfg?.derivedSeries) {
        const idx = opts.cfg.derivedSeries.findIndex(d => d.id === did);
        if (idx >= 0) { opts.cfg.derivedSeries.splice(idx, 1); removed = true; }
      } else if (seriesName && opts.cfg?.series) {
        const idx = opts.cfg.series.findIndex(s => `S${s.subjectId} · ${s.attribute}` === seriesName);
        if (idx >= 0) { opts.cfg.series.splice(idx, 1); removed = true; }
        const card = plotArea.closest('.compare-graph-card');
        const panel = card?.querySelector('.plot-compare-panel');
        if (panel?._refreshDerivedSources) panel._refreshDerivedSources();
      }
      if (removed) _legendCommit();
      return;
    }
    const styleEl = e.target.closest('.plot-legend-style');
    if (styleEl) {
      e.preventDefault();
      e.stopPropagation();
      const sbtn = styleEl.closest('.plot-legend-item[data-series]');
      const styles = LINE_STYLES;
      const th = _legendGetThreshold(sbtn);
      if (th) {
        th.style = styles[(styles.indexOf(th.style || 'dashed') + 1) % styles.length];
        _legendCommit();
        return;
      }
      const seriesName = sbtn?.dataset.series;
      const did = sbtn?.dataset.derivedId;
      let changed = false;
      if (did && opts.cfg?.derivedSeries) {
        const d = opts.cfg.derivedSeries.find(d => d.id === did);
        if (d) { d.lineStyle = styles[(styles.indexOf(d.lineStyle || 'solid') + 1) % styles.length]; changed = true; }
      } else if (seriesName && opts.cfg?.series) {
        const s = opts.cfg.series.find(s => `S${s.subjectId} · ${s.attribute}` === seriesName);
        if (s) { s.lineStyle = styles[(styles.indexOf(s.lineStyle || 'solid') + 1) % styles.length]; changed = true; }
      }
      if (changed) _legendCommit();
      return;
    }
    const btn = e.target.closest('.plot-legend-item[data-series]');
    if (!btn) return;
    let hiddenSet;
    if (opts.cfg && opts.cfg._hidden) {
      hiddenSet = opts.cfg._hidden;
    } else {
      const hiddenKey = state.selectedPlotSubject;
      if (hiddenKey == null) return;
      if (!state.hiddenPlotSeries.has(hiddenKey)) state.hiddenPlotSeries.set(hiddenKey, new Set());
      hiddenSet = state.hiddenPlotSeries.get(hiddenKey);
    }
    const name = btn.dataset.series;
    if (hiddenSet.has(name)) hiddenSet.delete(name);
    else hiddenSet.add(name);
    const isActive = !hiddenSet.has(name);
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-pressed', String(isActive));
    if (opts.restart) opts.restart();
    else _plotRestart();
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


const _renderGrid = (container, xScale, yScale, w, h) => {
  let gridG = container.select('.plot-grid');
  if (gridG.empty()) gridG = container.insert('g', ':first-child').attr('class', 'plot-grid');
  gridG.selectAll('*').remove();
  const yTicks = yScale.ticks(3);
  for (const t of yTicks) {
    gridG.append('line')
      .attr('x1', 0).attr('x2', w)
      .attr('y1', yScale(t)).attr('y2', yScale(t))
      .attr('class', 'plot-grid-line');
  }
  const xTicks = xScale.ticks(5);
  for (const t of xTicks) {
    gridG.append('line')
      .attr('x1', xScale(t)).attr('x2', xScale(t))
      .attr('y1', 0).attr('y2', h)
      .attr('class', 'plot-grid-line');
  }
};

const THRESHOLD_STYLES = { solid: 'none', dashed: '6 3', dotted: '2 3', dashdot: '6 3 2 3', longdash: '12 4' };
const MARKER_SHAPES = {
  circle: (x, y, r) => `M${x - r},${y}a${r},${r} 0 1,0 ${r * 2},0a${r},${r} 0 1,0 -${r * 2},0`,
  square: (x, y, r) => `M${x - r},${y - r}h${r * 2}v${r * 2}h-${r * 2}Z`,
  triangle: (x, y, r) => `M${x},${y - r}L${x + r},${y + r}L${x - r},${y + r}Z`,
  diamond: (x, y, r) => `M${x},${y - r}L${x + r},${y}L${x},${y + r}L${x - r},${y}Z`,
};
const LINE_STYLES = ['solid', 'dashed', 'dotted', 'dashdot', 'longdash', 'circle', 'square', 'triangle', 'diamond'];

const _renderThresholds = (container, thresholds, yScale, w) => {
  let thG = container.select('.plot-thresholds');
  if (!thresholds || !thresholds.length) {
    if (!thG.empty()) thG.remove();
    return;
  }
  if (thG.empty()) thG = container.append('g').attr('class', 'plot-thresholds');
  const lines = thG.selectAll('.plot-threshold').data(thresholds, (d) => `${d.value}:${d.label || ''}:${d.color || ''}:${d.style || ''}`);
  const enter = lines.enter().append('g').attr('class', 'plot-threshold');
  enter.append('line');
  enter.append('text');
  const merged = enter.merge(lines);
  merged.select('line')
    .attr('x1', 0).attr('x2', w)
    .attr('y1', d => yScale(d.value)).attr('y2', d => yScale(d.value))
    .attr('stroke', d => d.color || '#ef4444')
    .attr('stroke-width', 1)
    .attr('stroke-dasharray', d => THRESHOLD_STYLES[d.style] || THRESHOLD_STYLES.dashed);
  merged.select('text')
    .attr('x', w - 4).attr('y', d => yScale(d.value) - 3)
    .attr('text-anchor', 'end')
    .attr('class', 'plot-threshold-label')
    .attr('fill', d => d.color || '#ef4444')
    .text(d => d.label || String(d.value));
  lines.exit().remove();
};

const MARKER_LINE_STYLES = { solid: 'none', dashed: '6 3', dotted: '2 3', dashdot: '6 3 2 3' };

const _renderMarkers = (container, markers, xScale, h) => {
  let mG = container.select('.plot-markers');
  if (!markers || !markers.length) {
    if (!mG.empty()) mG.remove();
    return;
  }
  if (mG.empty()) mG = container.append('g').attr('class', 'plot-markers');
  const items = mG.selectAll('.plot-marker').data(markers, d => `${d.t}:${d.label}`);
  const enter = items.enter().append('g').attr('class', 'plot-marker');
  enter.append('line');
  enter.append('text');
  enter.append('title');
  const merged = enter.merge(items);
  merged.select('line')
    .attr('x1', d => xScale(d.t)).attr('x2', d => xScale(d.t))
    .attr('y1', 0).attr('y2', h)
    .attr('stroke', d => d.color || 'var(--accent)')
    .attr('stroke-width', 1)
    .attr('stroke-dasharray', d => MARKER_LINE_STYLES[d.lineStyle] || MARKER_LINE_STYLES.dashed);
  merged.select('text')
    .attr('x', d => xScale(d.t) + 4).attr('y', 11)
    .attr('class', 'plot-marker-label')
    .attr('fill', d => d.color || 'var(--accent)')
    .text(d => d.label);
  merged.select('title').text(d => d.note || '');
  items.exit().remove();
};

const _drawDashFor = (style, width) => {
  const w = Math.max(0.5, Number(width) || 2);
  if (style === 'dashed') return `${w * 3} ${w * 2}`;
  if (style === 'dotted') return `0 ${w * 2}`;
  return 'none';
};

const _renderDrawings = (container, drawings, xScale, panelH) => {
  let dG = container.select('.plot-drawings');
  if (!drawings || !drawings.length) {
    if (!dG.empty()) dG.remove();
    return;
  }
  if (dG.empty()) dG = container.append('g').attr('class', 'plot-drawings').attr('pointer-events', 'none');
  const paths = dG.selectAll('path').data(drawings);
  paths.enter().append('path')
    .attr('fill', 'none')
    .merge(paths)
    .attr('stroke', d => d.color || '#ef4444')
    .attr('stroke-width', d => d.width || 2)
    .attr('stroke-dasharray', d => _drawDashFor(d.dash, d.width || 2))
    .attr('stroke-linecap', d => d.dash === 'dotted' ? 'round' : (d.dash === 'dashed' ? 'butt' : 'round'))
    .attr('stroke-linejoin', 'round')
    .attr('d', d => {
      if (!d.points || d.points.length < 2) return null;
      return d.points.map((p, i) => {
        const px = xScale(p.t);
        const py = p.y * panelH;
        return `${i === 0 ? 'M' : 'L'}${px},${py}`;
      }).join('');
    });
  paths.exit().remove();
};

const _openMarkerForm = (plotArea, cfg, marker, isNew, onDone) => {
  const old = plotArea.querySelector('.plot-marker-form');
  if (old) old.remove();

  const form = document.createElement('div');
  form.className = 'plot-marker-form';

  const labelInput = document.createElement('input');
  labelInput.type = 'text';
  labelInput.placeholder = 'Label *';
  labelInput.value = marker.label || '';
  labelInput.setAttribute('aria-label', 'Marker label');

  const noteInput = document.createElement('input');
  noteInput.type = 'text';
  noteInput.placeholder = 'Note (optional)';
  noteInput.value = marker.note || '';
  noteInput.setAttribute('aria-label', 'Marker note');

  const colorWrap = document.createElement('div');
  colorWrap.className = 'plot-marker-form-color';
  const colorSwatch = document.createElement('span');
  colorSwatch.className = 'plot-marker-form-swatch';
  colorSwatch.style.background = marker.color || 'var(--accent)';
  const colorInput = document.createElement('input');
  colorInput.type = 'color';
  colorInput.value = _colorToHex(marker.color || '#0969da');
  colorInput.setAttribute('aria-label', 'Marker color');
  colorInput.addEventListener('input', () => {
    colorSwatch.style.background = colorInput.value;
  });
  colorWrap.appendChild(colorSwatch);
  colorWrap.appendChild(colorInput);

  const styleSel = document.createElement('select');
  styleSel.setAttribute('aria-label', 'Marker line style');
  for (const key of Object.keys(MARKER_LINE_STYLES)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = key;
    styleSel.appendChild(opt);
  }
  styleSel.value = marker.lineStyle || 'dashed';

  const btnRow = document.createElement('div');
  btnRow.className = 'plot-marker-form-btns';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', () => {
    const label = labelInput.value.trim();
    if (!label) { labelInput.focus(); return; }
    marker.label = label;
    marker.note = noteInput.value.trim() || '';
    marker.color = colorInput.value;
    marker.lineStyle = styleSel.value;
    if (isNew) {
      if (!cfg.markers) cfg.markers = [];
      cfg.markers.push(marker);
    }
    form.remove();
    onDone();
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => form.remove());

  btnRow.appendChild(saveBtn);
  if (!isNew) {
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'plot-marker-form-delete';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', () => {
      const idx = cfg.markers.indexOf(marker);
      if (idx >= 0) cfg.markers.splice(idx, 1);
      form.remove();
      onDone();
    });
    btnRow.appendChild(delBtn);
  }
  btnRow.appendChild(cancelBtn);

  form.appendChild(labelInput);
  form.appendChild(noteInput);
  const row2 = document.createElement('div');
  row2.className = 'plot-marker-form-row';
  row2.appendChild(colorWrap);
  row2.appendChild(styleSel);
  form.appendChild(row2);
  form.appendChild(btnRow);

  plotArea.appendChild(form);
  labelInput.focus();

  const close = (e) => {
    if (!form.contains(e.target) && form.parentNode) {
      form.remove();
      document.removeEventListener('mousedown', close);
    }
  };
  setTimeout(() => document.addEventListener('mousedown', close), 0);
};

const renderPanelLines = (g, sid, visible, xScale, yScales, panelH, w, totalPanelsH) => {
  const panelsG = g.select('.plot-panels');
  const panels = panelsG.selectAll('.plot-panel').data(visible, (d) => d.name);
  const panelsEnter = panels.enter().append('g').attr('class', 'plot-panel');
  panelsEnter.append('clipPath').attr('id', (d) => `panel-clip-${sid}-${_safeId(d.name)}`)
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
    const color = d.color || PLOT_COLORS[i % PLOT_COLORS.length];
    const panel = d3.select(this);
    panel.attr('transform', `translate(0, ${i * (panelH + PLOT_PANEL_GAP)})`);
    panel.select('clipPath rect').attr('width', w).attr('height', panelH);
    panel.select('.panel-y-axis').call(d3.axisLeft(yScale).ticks(3).tickSize(2));
    if (state.plotGrid) _renderGrid(panel, xScale, yScale, w, panelH);
    else panel.select('.plot-grid').remove();
    const lineGen = d3.line().defined((p) => !p._gap).x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
    const showLine = isSubjects ? !state.plotDisconnectPoints : true;
    panel.select('.panel-line')
      .attr('clip-path', `url(#panel-clip-${sid}-${_safeId(d.name)})`)
      .select('path')
      .attr('stroke', color)
      .attr('stroke-width', isSubjects ? state.plotStroke : 1.5)
      .attr('d', showLine && d.data.length >= 2 ? lineGen(d.data) : null)
      .attr('opacity', showLine && d.data.length >= 2 ? 1 : 0);

    const dotsG = panel.select('.panel-dots')
      .attr('clip-path', `url(#panel-clip-${sid}-${_safeId(d.name)})`);
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

const _renderCompareOverlay = (g, compareSeries, xScale, panelH, w, primaryCount, cfg = null) => {
  let overlay = g.select('.plot-compare-overlay');

  if (!compareSeries.length) {
    if (!overlay.empty()) overlay.selectAll('*').remove();
    return;
  }

  if (overlay.empty()) {
    overlay = g.insert('g', '.plot-x-axis').attr('class', 'plot-compare-overlay');
  }

  const yOffset = primaryCount * (panelH + PLOT_PANEL_GAP);
  overlay.attr('transform', `translate(0, ${yOffset})`);

  let vMin = Infinity, vMax = -Infinity;
  for (const s of compareSeries) {
    for (const p of s.data) {
      if (p.v < vMin) vMin = p.v;
      if (p.v > vMax) vMax = p.v;
    }
  }
  if (vMin === vMax) { vMin -= 1; vMax += 1; }
  const pad = (vMax - vMin) * 0.05;
  const yScale = d3.scaleLinear().domain([vMin - pad, vMax + pad]).range([panelH, 0]);

  let clip = overlay.select('clipPath');
  if (clip.empty()) {
    clip = overlay.append('clipPath').attr('id', 'compare-panel-clip');
    clip.append('rect');
  }
  clip.select('rect').attr('width', w).attr('height', panelH);

  let yAxisG = overlay.select('.panel-y-axis');
  if (yAxisG.empty()) yAxisG = overlay.append('g').attr('class', 'panel-y-axis');
  yAxisG.call(d3.axisLeft(yScale).ticks(3).tickSize(2));

  const showGrid = cfg ? cfg.grid : false;
  if (showGrid) _renderGrid(overlay, xScale, yScale, w, panelH);
  else overlay.select('.plot-grid').remove();

  _renderThresholds(overlay, cfg?.thresholds, yScale, w);
  _renderMarkers(overlay, cfg?.markers, xScale, panelH);
  _renderDrawings(overlay, cfg?.drawings, xScale, panelH);

  const strokeW = cfg ? cfg.stroke : 1.5;
  const showDots = cfg ? cfg.disconnectPoints : false;
  const lineGen = d3.line().defined((p) => !p._gap).x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
  const showLine = !showDots;

  const lines = overlay.selectAll('.compare-line').data(compareSeries, (d) => d.name);
  lines.enter().append('path')
    .attr('class', 'compare-line')
    .attr('fill', 'none')
    .attr('clip-path', 'url(#compare-panel-clip)')
    .merge(lines)
    .attr('stroke', (d, i) => d.color || PLOT_COLORS[(primaryCount + i) % PLOT_COLORS.length])
    .attr('stroke-width', strokeW)
    .attr('stroke-dasharray', (d) => {
      const st = d._lineStyle || 'solid';
      if (MARKER_SHAPES[st]) return null;
      return THRESHOLD_STYLES[st] || null;
    })
    .attr('d', (d) => showLine && d.data.length >= 2 ? lineGen(d.data) : null)
    .attr('opacity', (d) => showLine && d.data.length >= 2 ? 1 : 0);
  lines.exit().remove();

  const markerR = Math.max(2, strokeW);
  const maxMarkers = 200;
  compareSeries.forEach((s, i) => {
    const color = s.color || PLOT_COLORS[(primaryCount + i) % PLOT_COLORS.length];
    const safeN = _safeId(s.name);
    const shape = MARKER_SHAPES[s._lineStyle];
    const needMarkers = showLine && shape;
    const needDots = showDots && !shape;
    const cls = `compare-markers-${safeN}`;
    let mG = overlay.select(`.${cls}`);
    if (!needMarkers && !needDots) {
      if (!mG.empty()) mG.remove();
      return;
    }
    if (mG.empty()) {
      mG = overlay.append('g').attr('class', cls)
        .attr('clip-path', 'url(#compare-panel-clip)');
    }
    const vis = s.data.filter((p) => !p._gap && xScale(p.t) >= 0 && xScale(p.t) <= w);
    const step = vis.length > maxMarkers ? Math.ceil(vis.length / maxMarkers) : 1;
    const sampled = step > 1 ? vis.filter((_, j) => j % step === 0) : vis;
    if (needMarkers) {
      mG.selectAll('circle').remove();
      const paths = mG.selectAll('path').data(sampled, (p) => p.t);
      paths.enter().append('path')
        .merge(paths)
        .attr('d', (p) => shape(xScale(p.t), yScale(p.v), markerR))
        .attr('fill', color)
        .attr('stroke', 'none');
      paths.exit().remove();
    } else {
      mG.selectAll('path').remove();
      const dots = mG.selectAll('circle').data(sampled, (p) => p.t);
      dots.enter().append('circle').attr('fill', color)
        .merge(dots)
        .attr('r', strokeW)
        .attr('cx', (p) => xScale(p.t))
        .attr('cy', (p) => yScale(p.v));
      dots.exit().remove();
    }
  });
  const activeMarkerClasses = new Set(compareSeries.map(s => `compare-markers-${_safeId(s.name)}`));
  overlay.selectAll('[class^="compare-markers-"]').each(function () {
    if (!activeMarkerClasses.has(this.getAttribute('class'))) d3.select(this).remove();
  });

  let label = overlay.select('.compare-panel-label');
  if (label.empty()) {
    label = overlay.append('text').attr('class', 'panel-label compare-panel-label').attr('x', 4).attr('y', 11);
  }
  label.text('Compare').attr('fill', 'var(--muted)');
};

const bindPlotTooltip = (g, plotArea, visible, xScale, w, HEADER_H, rect, cfg = null, restart = null) => {
  plotArea._plotCtx = { visible, xScale, w };
  if (restart) plotArea._zoomRestart = restart;
  const tooltipEl = plotArea.querySelector('.plot-tooltip');
  const overlay = g.select('.plot-overlay');
  const crosshair = g.select('.plot-crosshair');
  const bisect = d3.bisector((d) => d.t).left;
  const card = plotArea.closest('.compare-graph-card');
  const syncContainer = card ? card.closest('.compare-cards') : null;

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
      const color = visible[idx]?.color || PLOT_COLORS[idx % PLOT_COLORS.length];
      const v = typeof s.sample.v === 'number' && !Number.isInteger(s.sample.v)
        ? s.sample.v.toFixed(2) : String(s.sample.v);
      return `<div class="plot-tooltip-row"><span class="plot-tooltip-swatch" style="background:${_safeColor(color)}"></span><span class="plot-tooltip-name">${escapeHtml(s.name)}</span><span class="plot-tooltip-val">${escapeHtml(v)}</span></div>`;
    }).join('');
    tooltipEl.innerHTML = `<div class="plot-tooltip-time">${formattedT}</div>${rows}`;
    tooltipEl.style.display = 'block';
    let tx = mx + PLOT_MARGIN.left + 12;
    const tw = tooltipEl.offsetWidth;
    if (tx + tw > rect.width - 4) tx = mx + PLOT_MARGIN.left - 12 - tw;
    tooltipEl.style.left = `${Math.max(4, tx)}px`;
    tooltipEl.style.top = `${HEADER_H + 4}px`;
  };

  const showAtTimestamp = (t0) => {
    if (!visible.length) return;
    const mx = xScale(t0);
    showCrosshairAt(mx);
  };

  const hideCrosshair = () => {
    crosshair.attr('opacity', 0);
    tooltipEl.style.display = 'none';
  };

  if (syncContainer) {
    if (plotArea._crosshairSync) syncContainer.removeEventListener('crosshair-sync', plotArea._crosshairSync);
    if (plotArea._crosshairHide) syncContainer.removeEventListener('crosshair-hide', plotArea._crosshairHide);
    plotArea._crosshairSync = (e) => {
      if (e.detail.source === plotArea) return;
      plotArea._syncedT = e.detail.t;
      showAtTimestamp(e.detail.t);
    };
    plotArea._crosshairHide = (e) => {
      if (e.detail.source === plotArea) return;
      plotArea._syncedT = null;
      hideCrosshair();
    };
    syncContainer.addEventListener('crosshair-sync', plotArea._crosshairSync);
    syncContainer.addEventListener('crosshair-hide', plotArea._crosshairHide);
  }

  if (plotArea._cursorMx != null) {
    showCrosshairAt(plotArea._cursorMx);
    if (syncContainer) {
      const t0 = xScale.invert(plotArea._cursorMx);
      syncContainer.dispatchEvent(new CustomEvent('crosshair-sync', { detail: { t: t0, source: plotArea } }));
    }
  } else if (plotArea._syncedT != null) {
    showAtTimestamp(plotArea._syncedT);
  }

  overlay
    .on('mousemove', (event) => {
      const [mx] = d3.pointer(event);
      plotArea._cursorMx = mx;
      showCrosshairAt(mx);
      if (syncContainer) {
        const t0 = xScale.invert(mx);
        syncContainer.dispatchEvent(new CustomEvent('crosshair-sync', { detail: { t: t0, source: plotArea } }));
      }
    })
    .on('mouseleave', () => {
      plotArea._cursorMx = null;
      hideCrosshair();
      if (syncContainer) {
        syncContainer.dispatchEvent(new CustomEvent('crosshair-hide', { detail: { source: plotArea } }));
      }
    });

  const svgEl = plotArea.querySelector('svg');
  if (svgEl && !svgEl._kbBound) {
    svgEl._kbBound = true;
    let kbPos = w / 2;
    svgEl.addEventListener('keydown', (e) => {
      const ctx = plotArea._plotCtx || {};
      const curW = ctx.w || w;
      const step = curW / 20;
      if (e.key === 'ArrowLeft') { kbPos = Math.max(0, kbPos - step); }
      else if (e.key === 'ArrowRight') { kbPos = Math.min(curW, kbPos + step); }
      else if (e.key === 'Escape') { hideCrosshair(); return; }
      else return;
      e.preventDefault();
      showCrosshairAt(kbPos);
      if (syncContainer) {
        const curXScale = ctx.xScale || xScale;
        const t0 = curXScale.invert(kbPos);
        syncContainer.dispatchEvent(new CustomEvent('crosshair-sync', { detail: { t: t0, source: plotArea } }));
      }
    });
  }

  if (cfg && svgEl && !svgEl._zoomBound) {
    svgEl._zoomBound = true;
    svgEl.addEventListener('wheel', (e) => {
      e.preventDefault();
      const [mx] = d3.pointer(e, overlay.node());
      const ctx = plotArea._plotCtx || {};
      const curXScale = ctx.xScale || xScale;
      const curW = ctx.w || w;
      const tAtCursor = curXScale.invert(mx);
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const oldZoom = cfg._zoom || 1;
      cfg._zoom = Math.max(0.1, Math.min(100, oldZoom * factor));
      const domain = curXScale.domain();
      const span = domain[1] - domain[0];
      const newSpan = span * oldZoom / cfg._zoom;
      const ratio = mx / curW;
      const newLeft = tAtCursor - newSpan * ratio;
      const newCenter = newLeft + newSpan / 2;
      const baseCenter = (domain[1] + domain[0]) / 2 - (cfg._panOffset || 0);
      cfg._panOffset = newCenter - baseCenter;
      cfg._fingerprint = '';
      if (plotArea._zoomRestart) plotArea._zoomRestart();
    }, { passive: false });

    let dragStart = null;
    let dragPanStart = 0;
    let didDrag = false;
    let clickTimer = null;
    const DRAG_THRESHOLD = 3;

    const _syncPauseBtn = () => {
      const pb = plotArea.querySelector('.plot-pause-btn');
      if (pb) {
        pb.textContent = cfg.paused ? '▶' : '⏸';
        pb.classList.toggle('active', cfg.paused);
      }
    };

    let downMx = 0;
    let downShift = false;
    let drawingStroke = null;

    svgEl.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      const [mx, my] = d3.pointer(e, overlay.node());
      if (e.altKey) {
        e.preventDefault();
        const ctx = plotArea._plotCtx || {};
        const curXScale = ctx.xScale || xScale;
        const totalH = parseFloat(g.select('.plot-overlay').attr('height')) || 200;
        const width = cfg._drawWidth || 2;
        const style = cfg._drawStyle || 'solid';
        drawingStroke = {
          points: [{ t: curXScale.invert(mx), y: my / totalH }],
          color: cfg._drawColor || '#ef4444',
          width,
          dash: style,
          _totalH: totalH,
          _dashArray: _drawDashFor(style, width),
          _linecap: style === 'dashed' ? 'butt' : 'round',
        };
        svgEl.style.cursor = 'crosshair';
        return;
      }
      dragStart = e.clientX;
      dragPanStart = cfg._panOffset || 0;
      didDrag = false;
      downShift = e.shiftKey;
      downMx = mx;
    });
    window.addEventListener('mousemove', (e) => {
      if (drawingStroke) {
        const [mx, my] = d3.pointer(e, overlay.node());
        const ctx = plotArea._plotCtx || {};
        const curXScale = ctx.xScale || xScale;
        drawingStroke.points.push({ t: curXScale.invert(mx), y: my / drawingStroke._totalH });
        const tempPath = g.select('.plot-drawing-temp');
        const pts = drawingStroke.points;
        const d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${curXScale(p.t)},${p.y * drawingStroke._totalH}`).join('');
        if (tempPath.empty()) {
          const ov = g.select('.plot-compare-overlay');
          (ov.empty() ? g : ov).append('path').attr('class', 'plot-drawing-temp')
            .attr('fill', 'none').attr('stroke', drawingStroke.color)
            .attr('stroke-width', drawingStroke.width)
            .attr('stroke-dasharray', drawingStroke._dashArray)
            .attr('stroke-linecap', drawingStroke._linecap).attr('stroke-linejoin', 'round').attr('pointer-events', 'none')
            .attr('d', d);
        } else {
          tempPath.attr('d', d);
        }
        return;
      }
      if (dragStart === null) return;
      if (!didDrag && Math.abs(e.clientX - dragStart) < DRAG_THRESHOLD) return;
      if (!didDrag) {
        didDrag = true;
        svgEl.style.cursor = 'grabbing';
      }
      const ctx = plotArea._plotCtx || {};
      const curXScale = ctx.xScale || xScale;
      const domain = curXScale.domain();
      const pxPerSec = (ctx.w || w) / (domain[1] - domain[0]);
      const dx = e.clientX - dragStart;
      cfg._panOffset = dragPanStart - dx / pxPerSec;
      cfg._fingerprint = '';
      if (plotArea._zoomRestart) plotArea._zoomRestart();
    });
    window.addEventListener('mouseup', (e) => {
      if (drawingStroke) {
        g.select('.plot-drawing-temp').remove();
        if (drawingStroke.points.length >= 2) {
          if (!cfg.drawings) cfg.drawings = [];
          const { _totalH, _dashArray, _linecap, ...stroke } = drawingStroke;
          cfg.drawings.push(stroke);
          cfg._fingerprint = '';
          saveSettings();
          if (plotArea._zoomRestart) plotArea._zoomRestart();
        }
        drawingStroke = null;
        svgEl.style.cursor = '';
        return;
      }
      if (dragStart === null) return;
      const wasDrag = didDrag;
      const wasShift = downShift;
      const mx = downMx;
      dragStart = null;
      svgEl.style.cursor = '';
      const _markerDone = () => {
        cfg._fingerprint = '';
        saveSettings();
        if (plotArea._zoomRestart) plotArea._zoomRestart();
      };
      const _findNearMarker = (t) => {
        if (!cfg.markers?.length) return null;
        const ctx2 = plotArea._plotCtx || {};
        const xs = ctx2.xScale || xScale;
        const SNAP_PX = 8;
        const pxPerSec = (ctx2.w || w) / (xs.domain()[1] - xs.domain()[0]);
        const snapSec = SNAP_PX / pxPerSec;
        let best = null, bestDist = Infinity;
        for (const m of cfg.markers) {
          const d = Math.abs(m.t - t);
          if (d < snapSec && d < bestDist) { best = m; bestDist = d; }
        }
        return best;
      };
      if (!wasDrag && wasShift) {
        const ctx = plotArea._plotCtx || {};
        const curXScale = ctx.xScale || xScale;
        const t = curXScale.invert(mx);
        const near = _findNearMarker(t);
        if (near) {
          _openMarkerForm(plotArea, cfg, near, false, _markerDone);
        } else {
          _openMarkerForm(plotArea, cfg, { t, label: '', note: '', color: '', lineStyle: 'dashed' }, true, _markerDone);
        }
        return;
      }
      if (!wasDrag) {
        const ctx = plotArea._plotCtx || {};
        const curXScale = ctx.xScale || xScale;
        const t = curXScale.invert(mx);
        const near = _findNearMarker(t);
        if (near) {
          _openMarkerForm(plotArea, cfg, near, false, _markerDone);
          return;
        }
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; }
        clickTimer = setTimeout(() => {
          clickTimer = null;
          if (cfg.paused) {
            cfg._resumeFrom = cfg.pausedAt;
            cfg._resumeStart = Date.now() / 1000;
            _resetSmoothCaches(cfg);
          }
          cfg.paused = !cfg.paused;
          cfg.pausedAt = cfg.paused ? Date.now() / 1000 : null;
          _syncPauseBtn();
          cfg._fingerprint = '';
          if (plotArea._zoomRestart) plotArea._zoomRestart();
        }, 250);
      }
    });

    svgEl.addEventListener('dblclick', (e) => {
      e.preventDefault();
      if (e.altKey && cfg.drawings?.length) {
        cfg.drawings = [];
        cfg._fingerprint = '';
        saveSettings();
        if (plotArea._zoomRestart) plotArea._zoomRestart();
        return;
      }
      if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
      cfg._zoom = 1;
      cfg._panOffset = 0;
      cfg._fingerprint = '';
      if (plotArea._zoomRestart) plotArea._zoomRestart();
    });
  }
};

const updatePlotLegend = (plotArea, allSeries, hidden) => {
  const legend = plotArea.querySelector('.plot-legend');
  if (!legend) return;
  const isCompare = !!plotArea.closest('.compare-graph-card');
  const seriesKey = allSeries.map((s) => `${s.name}:${s.color || ''}:${s._lineStyle || ''}:${s._derivedId || ''}:${s._thresholdIdx ?? ''}`).join('|');
  if (legend.dataset.seriesKey !== seriesKey) {
    legend.dataset.seriesKey = seriesKey;
    legend.innerHTML = allSeries.map((s, i) => {
      const isActive = !hidden.has(s.name);
      const color = s.color || PLOT_COLORS[i % PLOT_COLORS.length];
      const derivedAttr = s._derivedId ? ` data-derived-id="${escapeHtml(s._derivedId)}"` : '';
      const threshAttr = s._threshold ? ` data-threshold-idx="${s._thresholdIdx}"` : '';
      let styleHtml = '';
      let removeHtml = '';
      if (s._derived || s._threshold || isCompare) {
        const st = s._lineStyle || 'solid';
        styleHtml = `<span class="plot-legend-style" data-style="${st}" title="Click to change line style"></span>`;
        removeHtml = `<span class="plot-legend-remove" aria-label="Remove series">×</span>`;
      }
      const cls = s._derived ? ' plot-legend-derived' : s._threshold ? ' plot-legend-threshold' : '';
      return `<div class="plot-legend-item${isActive ? ' active' : ''}${cls}" data-series="${escapeHtml(s.name)}"${derivedAttr}${threshAttr} aria-pressed="${isActive}"><span class="plot-legend-swatch" style="background:${_safeColor(color)}" data-hex="${escapeHtml(color)}"></span>${styleHtml}<span class="plot-legend-label">${escapeHtml(s.name)}</span>${removeHtml}</div>`;
    }).join('');
  } else {
    for (const item of legend.querySelectorAll('.plot-legend-item[data-series]')) {
      const isActive = !hidden.has(item.dataset.series);
      item.classList.toggle('active', isActive);
      item.setAttribute('aria-pressed', String(isActive));
    }
  }
};

let _lastPlotFingerprint = '';

const _plotInvalidate = () => { _lastPlotFingerprint = ''; };
const _plotRerender = () => { renderPlot(el('selectedNodeContent')); };
const _plotRestart = () => { startPlotAnim(); };

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;
  _subjectsPlotCfg._updateFillRate?.();

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const isSubjects = state.activeView === 'subjects';
  const allSeries = collectPlotSeries(sid, isSubjects ? _subjectsPlotCfg : null);
  allSeries.forEach((s, i) => {
    s.color = state.plotColorOverrides[`${sid}:${s.name}`] || PLOT_COLORS[i % PLOT_COLORS.length];
  });
  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const lastPts = allSeries.map((s) => s.data.length ? s.data[s.data.length - 1].t : 0);
  const fp = isSubjects
    ? `${sid}:${allSeries.length}:${lastPts.join(',')}:v:s:w${state.plotTimeWindow}:p${state.plotPaused ? state.plotPausedAt : 0}:s${state.plotSmooth}:d${state.plotDisconnectPoints}:k${state.plotStroke}:g${state.plotGrid}`
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

  const [tLeft, tRight] = xScale.domain();
  let inWindow = 0;
  for (const s of visible) {
    for (const p of s.data) {
      if (p.t >= tLeft && p.t <= tRight) { inWindow++; break; }
    }
    if (inWindow) break;
  }
  let emptyOverlay = plotArea.querySelector('.plot-empty-window');
  if (!inWindow && visible.length) {
    const windowSecs = Math.max(0, Math.round(tRight - tLeft));
    if (!emptyOverlay) {
      emptyOverlay = document.createElement('div');
      emptyOverlay.className = 'plot-empty-window';
      plotArea.appendChild(emptyOverlay);
    }
    emptyOverlay.textContent = `No data in last ${windowSecs}s`;
  } else if (emptyOverlay) {
    emptyOverlay.remove();
  }

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels') || !plotArea.querySelector('.plot-header') || plotArea.dataset.plotView !== state.activeView) {
    gNode = setupPlotSvg(plotArea, PLOT_MARGIN);
    plotArea.dataset.plotView = state.activeView;
  }

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - HEADER_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    let next = `Subject ${sid}`;
    if (state.activeView === 'subjects') {
      const event = state.latestBySubject.get(sid);
      next += ` · ${event?.message_type || 'network'}`;
    } else if (state.selectedDetailTab === 'subscribers') {
      next += ' · network broadcast';
    }
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
