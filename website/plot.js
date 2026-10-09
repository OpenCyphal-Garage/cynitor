// D3 line plot — multi-panel time series with crosshair tooltip, legend
// toggle, and resizable split layout. Extracted from detail-panel.js.
// The Nodes and Subjects plots and Compare share it; what only Compare
// uses (its editing rows, derived series, how a graph draws) is in compare-view.js.

const PLOT_MARGIN = { top: 8, right: 12, bottom: 24, left: 48 };
const PLOT_PANEL_GAP = 8;
const PLOT_GAP_THRESHOLD = 3;
const PLOT_SHOWN_MAX = 8;  // series a subject's plot shows at first, at most
const _safeId = (s) => s.replace(/[^a-zA-Z0-9_-]/g, '_');
// A colour safe to write into a style: hex, or a theme colour (var(--error)).
const _safeColor = (c) => /^(#[0-9a-fA-F]{3,8}|var\(--[a-z0-9-]+\))$/.test(c) ? c : 'var(--muted)';
const PLOT_TIME_WINDOWS = [
  { label: '30s', secs: 30 },
  { label: '1m', secs: 60 },
  { label: '5m', secs: 300 },
  { label: '15m', secs: 900 },
  { label: 'All', secs: 0 },
];

// The detail panel's plot, in the Nodes and Subjects tabs alike: its controls
// (pause, window, Fill Rate, line, points, grid) are the persisted plot* settings.
const _detailPlotCfg = {
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

// A colour as a colour box takes it (#rrggbb): a theme colour (var(--error))
// as the theme in use has it; one it cannot read, the theme's first plot colour.
const _colorToHex = (str) => {
  const token = /^var\((--[a-z0-9-]+)\)$/.exec(str || '');
  if (token) str = getComputedStyle(document.documentElement).getPropertyValue(token[1]).trim();
  if (!str) return PLOT_COLORS[0];
  if (str.startsWith('#')) {
    if (str.length === 4) return `#${str[1]}${str[1]}${str[2]}${str[2]}${str[3]}${str[3]}`;
    return str;
  }
  const m = str.match(/\d+/g);
  if (m && m.length >= 3) return '#' + m.slice(0, 3).map(n => (+n).toString(16).padStart(2, '0')).join('');
  return PLOT_COLORS[0];
};

const _openSwatchPicker = (swatch, currentColor, onChange) => {
  if (swatch._pickerOpen) return;
  swatch._pickerOpen = true;
  const input = document.createElement('input');
  input.type = 'color';
  input.className = 'plot-swatch-picker-input';
  input.value = _colorToHex(currentColor);
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

// Pause a plot where it is (its right edge: now, or a replay's head; see
// computePlotScales), or resume it, gliding back to live.
const togglePlotPause = (cfg) => {
  const now = Date.now() / 1000;
  if (cfg.paused) {
    cfg._resumeFrom = cfg.pausedAt;
    cfg._resumeStart = now;
    _resetSmoothCaches(cfg);
  }
  cfg.paused = !cfg.paused;
  cfg.pausedAt = cfg.paused ? (cfg._anchor ?? now) : null;
};

// A plot's pause button shows whether the plot is paused: ▶, lit and pressed
// while it is; ⏸ while it runs.
const syncPauseButton = (btn, cfg) => {
  if (!btn) return;
  const paused = Boolean(cfg.paused);
  btn.textContent = paused ? '▶' : '⏸';
  btn.classList.toggle('active', paused);
  btn.setAttribute('aria-pressed', String(paused));
};

const _processSmooth = (cfg, keys) => {
  if (!cfg.smooth || cfg.smooth <= 0) return;
  if (!cfg._smoothBufs) cfg._smoothBufs = new Map();
  if (!cfg._rawCursors) cfg._rawCursors = new Map();
  if (!cfg._activeInterps) cfg._activeInterps = new Map();

  const now = Date.now();
  for (const key of keys) {
    const raw = plotData(key);
    if (!raw?.length) {
      // Its history was cleared (a disconnect): what Fill Rate made of it goes too.
      cfg._smoothBufs.delete(key);
      cfg._rawCursors.delete(key);
      cfg._activeInterps.delete(key);
      continue;
    }

    if (!cfg._smoothBufs.has(key)) cfg._smoothBufs.set(key, []);
    const buf = cfg._smoothBufs.get(key);
    // The cursor is the time of the last raw point taken, not its index: a
    // full history drops a point at its front for each one it gains, so its
    // length stops moving. Time going back (history cleared, a replay from
    // its start) starts the field over.
    let cursor = cfg._rawCursors.get(key) ?? -Infinity;
    if (raw[raw.length - 1].t < cursor) {
      buf.length = 0;
      cfg._activeInterps.delete(key);
      cursor = -Infinity;
    }
    let first = raw.length;
    while (first > 0 && raw[first - 1].t > cursor) first--;

    for (let i = first; i < raw.length; i++) {
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
    cfg._rawCursors.set(key, raw[raw.length - 1].t);

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
  return plotData(key);
};

const _tagGaps = (data) => {
  for (let i = 0; i < data.length; i++) {
    data[i]._gap = i > 0 && data[i].t - data[i - 1].t > PLOT_GAP_THRESHOLD;
  }
};

// ── Compare series ──
//
// A compare series is a subject's field: from one publisher (`nodeId`) when
// the subject has several, else from whichever sends it, as are all series
// saved before publishers were told apart. Its key names its data.
const compareSeriesKey = (s) => `${s.subjectId}:${s.attribute}${s.nodeId != null ? `@${s.nodeId}` : ''}`;

// "1300:value@20" -> "S1300 · value · n20": a key as the legend names it.
const _keyLabel = (key) => {
  const [base, nid] = String(key).split('@');
  const at = base.indexOf(':');
  return `S${base.slice(0, at)} · ${base.slice(at + 1)}${nid != null ? ` · n${nid}` : ''}`;
};

const compareSeriesName = (s) => _keyLabel(compareSeriesKey(s));

// The points a key names: a field's history, or its one publisher's part of
// it, filtered once while that history stays as it is (a redraw asks for a
// series' points more than once). Kept by the history itself: cleared, it
// takes its filtered parts with it.
const _publisherParts = new WeakMap();  // a field's history -> node-ID -> {length, last, points}
const plotData = (key) => {
  const [base, nid] = key.split('@');
  const points = state.subjectHistory.get(base);
  if (nid == null || !points) return points;
  let parts = _publisherParts.get(points);
  if (!parts) _publisherParts.set(points, (parts = new Map()));
  const last = points[points.length - 1];
  const part = parts.get(nid);
  if (part && part.length === points.length && part.last === last) return part.points;
  const mine = points.filter((p) => p.n === Number(nid));
  parts.set(nid, { length: points.length, last, points: mine });
  return mine;
};

// A field's points by the node that published them (`n`, see cacheEvent).
const _byPublisher = (points) => {
  const groups = new Map();
  for (const p of points) {
    if (!groups.has(p.n)) groups.set(p.n, []);
    groups.get(p.n).push(p);
  }
  return groups;
};

const _publisherLabel = (nid) => (nid == null ? 'anonymous' : `n${nid}`);

// A subject's series: one per field, or per field and publisher when several
// nodes publish it; only `publisher`'s own points when one is given. Fill Rate
// interpolates a field's series only where a single publisher makes it.
const collectPlotSeries = (sid, cfg = null, publisher = null) => {
  const keys = [];
  for (const key of state.subjectHistory.keys()) {
    if (key.startsWith(sid + ':')) keys.push(key);
  }
  if (cfg) _processSmooth(cfg, keys);
  const allSeries = [];
  // `nodeId`: the series' publisher, when one node makes it (see openPlotInCompare).
  const add = (name, field, buf, nid) => {
    if (!buf || buf.length < 2) return;
    _tagGaps(buf);
    allSeries.push({ name, field, data: buf, nodeId: Number.isInteger(nid) ? nid : null });
  };
  for (const key of keys) {
    const field = key.slice(key.indexOf(':') + 1);
    const groups = _byPublisher(state.subjectHistory.get(key));
    if (publisher != null) {
      add(field, field, groups.get(publisher), publisher);
    } else if (groups.size <= 1) {
      add(field, field, cfg ? _getSmoothBuf(cfg, key) : state.subjectHistory.get(key), groups.keys().next().value);
    } else {
      const nids = [...groups.keys()].sort((a, b) => (a ?? Infinity) - (b ?? Infinity));
      for (const nid of nids) add(`${field} · ${_publisherLabel(nid)}`, field, groups.get(nid), nid);
    }
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

  // During replay, the events carry their original (recorded) timestamps —
  // potentially hours, days, or years before "now". Anchoring the plot's
  // right edge to wall-clock time would push every replay point off the
  // left edge of the visible window. Use the latest event timestamp seen
  // so far instead, so the plot tracks the replay head as events arrive.
  const live = state.replayActive ? tDataMax : now;

  // Resumed, a plot glides from where it was paused back to live.
  const RESUME_DURATION = 2;
  const _resumeAnchor = (resumeFrom, resumeStart) => {
    if (!resumeFrom || !resumeStart) return live;
    const elapsed = now - resumeStart;
    if (elapsed >= RESUME_DURATION) return live;
    const t = elapsed / RESUME_DURATION;
    return resumeFrom + (live - resumeFrom) * t * t;
  };

  // The detail panel's plot keeps its settings in state (see _detailPlotCfg).
  const c = cfg || _detailPlotCfg;
  const windowSecs = c.timeWindow;
  let anchor;
  if (c.paused && c.pausedAt) {
    anchor = c.pausedAt;
  } else if (c._resumeFrom) {
    anchor = _resumeAnchor(c._resumeFrom, c._resumeStart);
    if (now - c._resumeStart >= RESUME_DURATION) { c._resumeFrom = null; c._resumeStart = null; }
  } else {
    anchor = live;
  }
  c._anchor = anchor;  // where a pause now holds the plot (togglePlotPause)

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
    keys = cfg.series.map(compareSeriesKey);
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
    const buf = plotData(key);
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

  const cfg = opts.cfg || _detailPlotCfg;
  if (!cfg) return wrap;

  const invalidate = opts.invalidate || _plotInvalidate;
  const rerender = opts.rerender || _plotRerender;
  const restart = opts.restart || _plotRestart;

  const pauseBtn = document.createElement('button');
  pauseBtn.className = 'plot-pause-btn';
  pauseBtn.type = 'button';
  pauseBtn.setAttribute('aria-label', 'Pause plot');
  syncPauseButton(pauseBtn, cfg);
  pauseBtn.addEventListener('click', () => {
    togglePlotPause(cfg);
    syncPauseButton(pauseBtn, cfg);
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
    const drawTip = 'Shift+click: add/edit marker · Click on marker: edit · Alt+drag: freehand draw · Alt+dblclick: clear drawings · Ctrl+wheel: zoom · Drag: pan · Dblclick: reset zoom · Click: pause/resume, with Click pauses on';
    drawInfo.title = drawTip;
    drawInfo.setAttribute('aria-label', drawTip);
    drawGroup.appendChild(drawInfo);

    const drawColorWrap = document.createElement('div');
    drawColorWrap.className = 'plot-draw-color-wrap';
    // Until a colour is picked, drawings take the theme's red (see styles.css).
    const drawSwatch = document.createElement('span');
    drawSwatch.className = 'plot-draw-swatch';
    if (cfg._drawColor) drawSwatch.style.background = cfg._drawColor;
    const drawColorInput = document.createElement('input');
    drawColorInput.type = 'color';
    drawColorInput.value = _colorToHex(cfg._drawColor || 'var(--error)');
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

const setupPlotSvg = (plotArea, margin, opts = {}) => {
  plotArea.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'plot-header';
  const titleNode = document.createElement('div');
  titleNode.className = 'plot-title';
  header.appendChild(titleNode);
  if (!opts.noControls) {
    const controls = buildPlotControls(opts);
    if (!opts.cfg) {  // a Nodes or Subjects plot: its series can go to Compare
      const sep = document.createElement('span');
      sep.className = 'plot-controls-sep';
      const toCompare = document.createElement('button');
      toCompare.type = 'button';
      toCompare.className = 'plot-to-compare';
      toCompare.textContent = 'Compare';
      toCompare.title = 'Open the series shown here in a new Compare graph';
      toCompare.addEventListener('click', () => openPlotInCompare(plotArea));
      controls.append(sep, toCompare);
    }
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
      _openSwatchPicker(swatch, swatch.dataset.hex || PLOT_COLORS[0], (newColor) => {
        swatch.dataset.hex = newColor;
        if (th) {
          th.color = newColor;
        } else if (opts.cfg && opts.cfg.series) {
          // A derived series found by its id, as its style and × buttons find it.
          const s = btn.dataset.derivedId
            ? opts.cfg.derivedSeries?.find((d) => d.id === btn.dataset.derivedId)
            : opts.cfg.series.find((c) => compareSeriesName(c) === seriesName);
          if (s) s.color = newColor;
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
        const idx = opts.cfg.series.findIndex(s => compareSeriesName(s) === seriesName);
        if (idx >= 0) { opts.cfg.series.splice(idx, 1); removed = true; }
        const card = plotArea.closest('.compare-graph-card');
        const panel = card?.querySelector('.plot-compare-panel');
        if (panel?._refreshDerivedSources) panel._refreshDerivedSources();
        panel?._refreshSeriesList?.();  // its box unticks
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
        const lineStyles = Object.keys(THRESHOLD_STYLES);  // a threshold is a line: no marker shapes
        th.style = lineStyles[(lineStyles.indexOf(th.style || 'dashed') + 1) % lineStyles.length];
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
        const s = opts.cfg.series.find(s => compareSeriesName(s) === seriesName);
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
    btn.querySelector('.plot-legend-label')?.setAttribute('aria-pressed', String(isActive));
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
  g.append('rect').attr('class', 'plot-overlay').attr('fill', 'none').attr('pointer-events', 'all');
  const tooltip = document.createElement('div');
  tooltip.className = 'plot-tooltip hidden';
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
// A threshold's name in the legend: its label, else its value.
const thresholdName = (th) => `${th.label || th.value}`;
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
    .attr('stroke', d => d.color || 'var(--error)')
    .attr('stroke-width', 1)
    .attr('stroke-dasharray', d => THRESHOLD_STYLES[d.style] || THRESHOLD_STYLES.dashed);
  merged.select('text')
    .attr('x', w - 4).attr('y', d => yScale(d.value) - 3)
    .attr('text-anchor', 'end')
    .attr('class', 'plot-threshold-label')
    .attr('fill', d => d.color || 'var(--error)')
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
    .attr('stroke', d => d.color || 'var(--error)')
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
  const closeForm = () => {
    form.remove();
    document.removeEventListener('mousedown', closeIfOutside);
  };
  const closeIfOutside = (e) => {
    if (!form.contains(e.target)) closeForm();
  };

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
  // Until a colour is picked, a marker takes the theme's accent (see styles.css).
  const colorSwatch = document.createElement('span');
  colorSwatch.className = 'plot-marker-form-swatch';
  if (marker.color) colorSwatch.style.background = marker.color;
  const colorInput = document.createElement('input');
  colorInput.type = 'color';
  colorInput.value = _colorToHex(marker.color || 'var(--accent)');
  colorInput.setAttribute('aria-label', 'Marker color');
  let picked = Boolean(marker.color);
  colorInput.addEventListener('input', () => {
    picked = true;
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
    marker.color = picked ? colorInput.value : '';
    marker.lineStyle = styleSel.value;
    if (isNew) {
      if (!cfg.markers) cfg.markers = [];
      cfg.markers.push(marker);
    }
    closeForm();
    onDone();
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => closeForm());

  btnRow.appendChild(saveBtn);
  if (!isNew) {
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'plot-marker-form-delete';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', () => {
      const idx = cfg.markers.indexOf(marker);
      if (idx >= 0) cfg.markers.splice(idx, 1);
      closeForm();
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

  // Enter in a text box saves, Escape cancels, as does a click outside.
  form.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.type === 'text') {
      e.preventDefault();
      saveBtn.click();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeForm();
    }
  });
  setTimeout(() => document.addEventListener('mousedown', closeIfOutside), 0);
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
    const showLine = !state.plotDisconnectPoints;
    panel.select('.panel-line')
      .attr('clip-path', `url(#panel-clip-${sid}-${_safeId(d.name)})`)
      .select('path')
      .attr('stroke', color)
      .attr('stroke-width', state.plotStroke)
      .attr('d', showLine && d.data.length >= 2 ? lineGen(d.data) : null)
      .attr('opacity', showLine && d.data.length >= 2 ? 1 : 0);

    const dotsG = panel.select('.panel-dots')
      .attr('clip-path', `url(#panel-clip-${sid}-${_safeId(d.name)})`);
    if (state.plotDisconnectPoints) {
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

// A value as a tooltip reads it: a whole number as it is, others to six
// significant digits, so a small one keeps its digits (0.00095, not 0.00).
const formatPlotValue = (v) => (typeof v === 'number' && !Number.isInteger(v)
  ? String(Number(v.toPrecision(6))) : String(v));

const bindPlotTooltip = (g, plotArea, visible, xScale, w, HEADER_H, rect, cfg = null, restart = null) => {
  plotArea._plotCtx = { visible, xScale, w };
  if (restart) plotArea._zoomRestart = restart;
  const tooltipEl = plotArea.querySelector('.plot-tooltip');
  const overlay = g.select('.plot-overlay');
  const crosshair = g.select('.plot-crosshair');
  const bisect = d3.bisector((d) => d.t).left;
  const card = plotArea.closest('.compare-graph-card');
  const syncContainer = card ? card.closest('.compare-cards') : null;

  // A series' sample at time t: the nearest one where its line is drawn, or
  // near either end of it; none in a gap, as once it stopped (shown "–").
  const sampleAt = (data, t) => {
    const i = bisect(data, t);
    const a = data[i - 1];
    const b = data[i];
    const nearest = !b ? a : !a ? b : (Math.abs(a.t - t) < Math.abs(b.t - t) ? a : b);
    if (a && b && !b._gap) return nearest;
    return nearest && Math.abs(nearest.t - t) <= PLOT_GAP_THRESHOLD ? nearest : null;
  };

  const showCrosshairAt = (mx) => {
    if (mx < 0 || mx > w || !visible.length) {
      crosshair.attr('opacity', 0);
      tooltipEl.classList.add('hidden');
      return;
    }
    const t0 = xScale.invert(mx);
    const samples = visible.map((s) => ({ name: s.name, sample: sampleAt(s.data, t0) }));
    const first = samples.find((x) => x.sample);
    if (!first) {
      crosshair.attr('opacity', 0);
      tooltipEl.classList.add('hidden');
      return;
    }
    crosshair.attr('opacity', 1).attr('x1', mx).attr('x2', mx);
    const formattedT = formatPlotTime(first.sample.t);
    const rows = samples.map((s) => {
      const idx = visible.findIndex((v) => v.name === s.name);
      const color = visible[idx]?.color || PLOT_COLORS[idx % PLOT_COLORS.length];
      const v = s.sample ? formatPlotValue(s.sample.v) : '–';
      return `<div class="plot-tooltip-row"><span class="plot-tooltip-swatch" style="background:${_safeColor(color)}"></span><span class="plot-tooltip-name">${escapeHtml(s.name)}</span><span class="plot-tooltip-val">${escapeHtml(v)}</span></div>`;
    }).join('');
    tooltipEl.innerHTML = `<div class="plot-tooltip-time">${formattedT}</div>${rows}`;
    tooltipEl.classList.remove('hidden');
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
    tooltipEl.classList.add('hidden');
  };

  if (syncContainer) {
    if (plotArea._crosshairSync) syncContainer.removeEventListener('crosshair-sync', plotArea._crosshairSync);
    if (plotArea._crosshairHide) syncContainer.removeEventListener('crosshair-hide', plotArea._crosshairHide);
    // A removed graph's handlers go at the next event: nothing else removes them.
    const removed = () => {
      if (plotArea.isConnected) return false;
      syncContainer.removeEventListener('crosshair-sync', plotArea._crosshairSync);
      syncContainer.removeEventListener('crosshair-hide', plotArea._crosshairHide);
      return true;
    };
    plotArea._crosshairSync = (e) => {
      if (removed() || e.detail.source === plotArea) return;
      plotArea._syncedT = e.detail.t;
      showAtTimestamp(e.detail.t);
    };
    plotArea._crosshairHide = (e) => {
      if (removed() || e.detail.source === plotArea) return;
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
    // Ctrl+wheel zooms (a trackpad pinch sends it too); the wheel alone
    // scrolls the page on, past graphs that fill it.
    svgEl.addEventListener('wheel', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
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

    let downMx = 0;
    let downShift = false;
    let drawingStroke = null;

    // The page follows the pointer only while a button is down on the plot:
    // listeners left on it would outlive the graph.
    svgEl.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      window.addEventListener('mousemove', onDragMove);
      window.addEventListener('mouseup', onDragEnd);
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
          color: cfg._drawColor || '',  // the theme's red, unless one is picked
          width,
          dash: style,
          _totalH: totalH,
          _dashArray: _drawDashFor(style, width),
          _linecap: style === 'dashed' ? 'butt' : 'round',
        };
        svgEl.classList.add('plot-drawing');
        return;
      }
      dragStart = e.clientX;
      dragPanStart = cfg._panOffset || 0;
      didDrag = false;
      downShift = e.shiftKey;
      downMx = mx;
    });
    const onDragMove = (e) => {
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
          const lines = ov.select('.compare-lines');  // their clip keeps a drawing to the plot
          (ov.empty() ? g : ov).append('path').attr('class', 'plot-drawing-temp')
            .attr('clip-path', lines.empty() ? null : lines.attr('clip-path'))
            .attr('fill', 'none').attr('stroke', drawingStroke.color || 'var(--error)')
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
        svgEl.classList.add('plot-grabbing');
      }
      const ctx = plotArea._plotCtx || {};
      const curXScale = ctx.xScale || xScale;
      const domain = curXScale.domain();
      const pxPerSec = (ctx.w || w) / (domain[1] - domain[0]);
      const dx = e.clientX - dragStart;
      cfg._panOffset = dragPanStart - dx / pxPerSec;
      cfg._fingerprint = '';
      if (plotArea._zoomRestart) plotArea._zoomRestart();
    };
    const onDragEnd = () => {
      window.removeEventListener('mousemove', onDragMove);
      window.removeEventListener('mouseup', onDragEnd);
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
        svgEl.classList.remove('plot-drawing');
        return;
      }
      if (dragStart === null) return;
      const wasDrag = didDrag;
      const wasShift = downShift;
      const mx = downMx;
      dragStart = null;
      svgEl.classList.remove('plot-grabbing');
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
        if (!cfg.clickPauses) return;  // a click pauses only where the graph asks it to
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; }
        clickTimer = setTimeout(() => {
          clickTimer = null;
          togglePlotPause(cfg);
          // A Compare graph's pause button is in its card's header, not in the plot.
          syncPauseButton((plotArea.closest('.compare-graph-card') || plotArea).querySelector('.plot-pause-btn'), cfg);
          cfg._fingerprint = '';
          if (plotArea._zoomRestart) plotArea._zoomRestart();
        }, 250);
      }
    };

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

// The values a Compare legend row gives, and how a cell reads one: blank for a
// threshold, – for a series with nothing in view (see _statsInView).
const LEGEND_STATS = ['last', 'min', 'max'];
const _legendValue = (s, stat) => (s._stats === undefined ? '' : s._stats ? formatPlotValue(s._stats[stat]) : '–');

const updatePlotLegend = (plotArea, allSeries, hidden) => {
  const legend = plotArea.querySelector('.plot-legend');
  if (!legend) return;
  const isCompare = !!plotArea.closest('.compare-graph-card');
  // Compare's legend is a table: a row a series, its last, lowest and highest
  // value in view in columns that line up across the rows.
  legend.classList.toggle('plot-legend--table', isCompare);
  const seriesKey = allSeries.map((s) => `${s.name}:${s.color || ''}:${s._lineStyle || ''}:${s._derivedId || ''}:${s._thresholdIdx ?? ''}:${s._silent ? 's' : ''}:${s._unit || ''}`).join('|');
  if (legend.dataset.seriesKey !== seriesKey) {
    legend.dataset.seriesKey = seriesKey;
    const head = isCompare ? '<div class="plot-legend-head" aria-hidden="true"><span class="plot-legend-head-name">in view</span>'
      + `${LEGEND_STATS.map((stat) => `<span class="plot-legend-value">${stat}</span>`).join('')}<span class="plot-legend-head-end"></span></div>` : '';
    legend.innerHTML = head + allSeries.map((s, i) => {
      const isActive = !hidden.has(s.name);
      const color = s.color || PLOT_COLORS[i % PLOT_COLORS.length];
      const derivedAttr = s._derivedId ? ` data-derived-id="${escapeHtml(s._derivedId)}"` : '';
      const threshAttr = s._threshold ? ` data-threshold-idx="${s._thresholdIdx}"` : '';
      let styleHtml = '';
      let removeHtml = '';
      // Buttons, so the keyboard reaches them: show or hide, line style, remove.
      const name = escapeHtml(s.name);
      if (s._derived || s._threshold || isCompare) {
        const st = s._lineStyle || 'solid';
        styleHtml = `<button type="button" class="plot-legend-style" data-style="${st}" title="Click to change line style" aria-label="Line style of ${name}"></button>`;
        removeHtml = `<button type="button" class="plot-legend-remove" aria-label="Remove ${name}">×</button>`;
      }
      const cls = (s._derived ? ' plot-legend-derived' : s._threshold ? ' plot-legend-threshold' : '')
        + (s._silent ? ' plot-legend-silent' : '');
      const silentAttr = s._silent ? ' title="Nothing to plot: no data from it yet, or none kept"' : '';
      const unitHtml = s._unit ? `<span class="plot-legend-unit">${escapeHtml(s._unit)}</span>` : '';
      const valuesHtml = isCompare ? LEGEND_STATS.map((stat) =>
        `<span class="plot-legend-value" data-stat="${stat}">${escapeHtml(_legendValue(s, stat))}</span>`).join('') : '';
      return `<div class="plot-legend-item${isActive ? ' active' : ''}${cls}" data-series="${name}"${derivedAttr}${threshAttr}${silentAttr}><span class="plot-legend-swatch" style="background:${_safeColor(color)}" data-hex="${escapeHtml(color)}"></span>${styleHtml}<button type="button" class="plot-legend-label" aria-pressed="${isActive}">${name}${unitHtml}</button>${valuesHtml}${removeHtml}</div>`;
    }).join('');
  } else {
    for (const item of legend.querySelectorAll('.plot-legend-item[data-series]')) {
      const isActive = !hidden.has(item.dataset.series);
      item.classList.toggle('active', isActive);
      item.querySelector('.plot-legend-label')?.setAttribute('aria-pressed', String(isActive));
    }
  }
  if (!isCompare) return;
  // The values change with every draw: written into their cells, the rows stay.
  const byName = new Map(allSeries.map((s) => [s.name, s]));
  for (const item of legend.querySelectorAll('.plot-legend-item[data-series]')) {
    const s = byName.get(item.dataset.series);
    for (const cell of item.querySelectorAll('.plot-legend-value')) {
      const text = s ? _legendValue(s, cell.dataset.stat) : '';
      if (cell.textContent !== text) cell.textContent = text;
    }
  }
};

// A note over a plot saying why it shows nothing; none when `text` is empty.
const setPlotNote = (plotArea, text) => {
  let note = plotArea.querySelector('.plot-empty-window');
  if (!text) {
    note?.remove();
    return;
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'plot-empty-window';
    plotArea.appendChild(note);
  }
  if (note.textContent !== text) note.textContent = text;
};

let _lastPlotFingerprint = '';

const _plotInvalidate = () => { _lastPlotFingerprint = ''; };

// The theme changed: the plots on show draw again in its colours, paused ones too.
const redrawPlotsInTheme = () => {
  refreshPlotColors();
  if (state.activeView === 'nodes' || state.activeView === 'subjects') startPlotAnim();
  for (const graph of state.compareGraphs) graph._fingerprint = '';
  if (state.activeView === 'compare') startCompareAnim();
};
const _plotRerender = () => { renderPlot(el('selectedNodeContent')); };
const _plotRestart = () => { startPlotAnim(); };

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;
  _detailPlotCfg._updateFillRate?.();

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const isSubjects = state.activeView === 'subjects';
  // A node's own card plots that node's messages, not the subject's other publishers'.
  const publisher = !isSubjects && state.selectedDetailTab === 'publishers'
    && Number.isInteger(state.selectedNodeId) ? state.selectedNodeId : null;
  const allSeries = collectPlotSeries(sid, _detailPlotCfg, publisher);
  allSeries.forEach((s, i) => {
    s.color = state.plotColorOverrides[`${sid}:${s.name}`] || PLOT_COLORS[i % PLOT_COLORS.length];
  });
  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const lastPts = allSeries.map((s) => s.data.length ? s.data[s.data.length - 1].t : 0);
  const fp = `${sid}:${state.activeView}:${publisher ?? '-'}:${allSeries.length}:${lastPts.join(',')}`
    + `:w${state.plotTimeWindow}:p${state.plotPaused ? state.plotPausedAt : 0}:s${state.plotSmooth}`
    + `:d${state.plotDisconnectPoints}:k${state.plotStroke}:g${state.plotGrid}`;
  const rect = plotArea.getBoundingClientRect();
  const sizeKey = `${Math.round(rect.width)}x${Math.round(rect.height)}`;
  const fullFp = `${fp}:${sizeKey}`;
  if (fullFp === _lastPlotFingerprint) return;
  _lastPlotFingerprint = fullFp;

  // A series starts hidden, its legend pill showing it on demand, when it is a
  // timestamp (metadata, in uavcan.si types usually 0) or more than fit: a
  // field per publisher makes dozens on Heartbeat, which every node sends.
  if (!state.hiddenPlotSeries.has(sid)) state.hiddenPlotSeries.set(sid, new Set());
  if (!state.plotSeriesSeen.has(sid)) state.plotSeriesSeen.set(sid, new Set());
  const hidden = state.hiddenPlotSeries.get(sid);
  const seen = state.plotSeriesSeen.get(sid);
  let shown = allSeries.filter((s) => seen.has(s.name) && !hidden.has(s.name)).length;
  for (const s of allSeries) {
    if (seen.has(s.name)) continue;
    seen.add(s.name);
    if (s.field === 'timestamp' || shown >= PLOT_SHOWN_MAX) hidden.add(s.name);
    else shown++;
  }
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
  setPlotNote(plotArea, !inWindow && visible.length
    ? `No data in last ${Math.max(0, Math.round(tRight - tLeft))}s` : '');

  let gNode = plotArea.querySelector('.plot-root');
  if (!gNode || !gNode.querySelector('.plot-panels') || !plotArea.querySelector('.plot-header') || plotArea.dataset.plotView !== state.activeView) {
    gNode = setupPlotSvg(plotArea, PLOT_MARGIN);
    plotArea.dataset.plotView = state.activeView;
  }

  const svgEl = plotArea.querySelector('svg');
  if (svgEl) svgEl.setAttribute('height', String(rect.height - HEADER_H));

  const titleEl = plotArea.querySelector('.plot-title');
  if (titleEl) {
    let next = `Subject ${sid} · ${subjectTypeName(sid, state.latestBySubject.get(sid))}`;
    if (state.activeView !== 'subjects' && state.selectedDetailTab === 'subscribers') {
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
  if (state.plotPaused) {
    renderPlot(container);
    return;
  }
  const tick = () => {
    if (state.detailPanelCollapsed) { state.plotTimer = null; return; }
    if (state.plotPaused) { state.plotTimer = null; return; }
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
