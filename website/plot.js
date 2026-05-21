// D3 line plot — multi-panel time series with crosshair tooltip, legend
// toggle, and resizable split layout. Extracted from detail-panel.js.

const PLOT_MARGIN = { top: 8, right: 12, bottom: 24, left: 48 };
const PLOT_PANEL_GAP = 8;
const _safeId = (s) => s.replace(/[^a-zA-Z0-9_-]/g, '_');
const PLOT_TIME_WINDOWS = [
  { label: '30s', secs: 30 },
  { label: '1m', secs: 60 },
  { label: '5m', secs: 300 },
  { label: '15m', secs: 900 },
  { label: 'All', secs: 0 },
];

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
  input.style.cssText = 'position:absolute;opacity:0;width:0;height:0;pointer-events:none';
  swatch.style.position = 'relative';
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
  input.addEventListener('blur', () => {
    if (committed) return;
    setTimeout(() => {
      if (!committed && input.parentNode) {
        input.remove();
        swatch._pickerOpen = false;
        swatch.style.background = currentColor;
      }
    }, 300);
  });
  input.click();
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

  let windowSecs, anchor;
  if (cfg) {
    windowSecs = cfg.timeWindow;
    if (cfg.paused && cfg.pausedAt) {
      anchor = cfg.pausedAt;
    } else if (cfg._resumeFrom) {
      anchor = _resumeAnchor(cfg._resumeFrom, cfg._resumeStart);
      if (now - cfg._resumeStart >= RESUME_DURATION) { cfg._resumeFrom = null; cfg._resumeStart = null; }
    } else {
      anchor = now;
    }
  } else if (state.activeView === 'subjects') {
    windowSecs = state.plotTimeWindow;
    if (state.plotPaused && state.plotPausedAt) {
      anchor = state.plotPausedAt;
    } else if (state._plotResumeFrom) {
      anchor = _resumeAnchor(state._plotResumeFrom, state._plotResumeStart);
      if (now - state._plotResumeStart >= RESUME_DURATION) { state._plotResumeFrom = null; state._plotResumeStart = null; }
    } else {
      anchor = now;
    }
  } else {
    windowSecs = 60;
    anchor = now;
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
      wrap.querySelectorAll('.plot-window-btn').forEach((b) =>
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
  smoothSlider.value = String(cfg.smooth);
  smoothSlider.setAttribute('aria-label', 'Interpolation frequency (Hz)');
  smoothSlider.addEventListener('input', () => {
    cfg.smooth = Number(smoothSlider.value);
    cfg._smoothBufs = null;
    cfg._rawCursors = null;
    cfg._activeInterps = null;
    invalidate();
    saveSettings();
    if (cfg.paused) rerender();
  });
  smoothGroup.appendChild(smoothSlider);
  const smoothInfo = document.createElement('span');
  smoothInfo.className = 'plot-info-icon';
  smoothInfo.textContent = '?';
  smoothInfo.title = 'Feeds interpolated points between samples at the selected Hz rate — one sample period delay';
  smoothInfo.setAttribute('aria-label', 'Feeds interpolated points between samples at the selected Hz rate — one sample period delay');
  smoothGroup.appendChild(smoothInfo);
  wrap.appendChild(smoothGroup);

  const sep2 = document.createElement('span');
  sep2.className = 'plot-controls-sep';
  wrap.appendChild(sep2);

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
  strokeSlider.value = String(cfg.stroke);
  strokeSlider.setAttribute('aria-label', 'Line and point size');
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

const _updateComparePanelList = (panel, graph, onUpdate) => {
  const list = panel.querySelector('.plot-compare-list');
  if (!list) return;
  list.innerHTML = '';
  graph.series.forEach((item, i) => {
    const div = document.createElement('div');
    div.className = 'plot-compare-item';
    const swatchColor = item.color || PLOT_COLORS[i % PLOT_COLORS.length];
    const swatch = document.createElement('span');
    swatch.className = 'plot-compare-swatch';
    swatch.style.background = swatchColor;
    swatch.style.cursor = 'pointer';
    swatch.setAttribute('aria-label', 'Click to change color');
    swatch.addEventListener('click', (e) => {
      e.stopPropagation();
      _openSwatchPicker(swatch, item.color || PLOT_COLORS[i % PLOT_COLORS.length], (newColor) => {
        item.color = newColor;
        onUpdate();
      });
    });
    div.appendChild(swatch);
    const name = document.createElement('span');
    name.className = 'plot-compare-name';
    name.textContent = `S${item.subjectId} · ${item.attribute}`;
    div.appendChild(name);
    const rm = document.createElement('button');
    rm.className = 'plot-compare-remove';
    rm.textContent = '×';
    rm.setAttribute('aria-label', `Remove S${item.subjectId} ${item.attribute}`);
    rm.addEventListener('click', () => {
      graph.series.splice(i, 1);
      onUpdate();
    });
    div.appendChild(rm);
    list.appendChild(div);
  });
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

  const list = document.createElement('div');
  list.className = 'plot-compare-list';
  panel.appendChild(list);

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
  const thColorInput = document.createElement('input');
  thColorInput.type = 'color';
  thColorInput.className = 'plot-threshold-color';
  thColorInput.value = '#ef4444';
  thColorInput.setAttribute('aria-label', 'Threshold color');

  const thStyleSel = document.createElement('select');
  thStyleSel.className = 'plot-threshold-style';
  thStyleSel.setAttribute('aria-label', 'Threshold line style');
  for (const s of ['dashed', 'solid', 'dotted']) {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    thStyleSel.appendChild(opt);
  }

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
      color: thColorInput.value,
      style: thStyleSel.value,
    });
    thInput.value = '';
    thNameInput.value = '';
    onUpdate();
    _updateThresholdList(thList, graph, onUpdate);
  });
  thPicker.appendChild(thInput);
  thPicker.appendChild(thNameInput);
  thPicker.appendChild(thColorInput);
  thPicker.appendChild(thStyleSel);
  thPicker.appendChild(thAddBtn);
  thSection.appendChild(thPicker);
  const thList = document.createElement('div');
  thList.className = 'plot-threshold-list';
  thSection.appendChild(thList);
  panel.appendChild(thSection);

  return panel;
};

const _updateThresholdList = (container, graph, onUpdate) => {
  container.innerHTML = '';
  for (let i = 0; i < graph.thresholds.length; i++) {
    const th = graph.thresholds[i];
    const row = document.createElement('div');
    row.className = 'plot-threshold-item';
    const line = document.createElement('span');
    line.className = 'plot-threshold-line-preview';
    line.style.borderColor = th.color || '#ef4444';
    line.style.borderTopStyle = th.style || 'dashed';
    line.style.cursor = 'pointer';
    line.addEventListener('click', (e) => {
      e.stopPropagation();
      _openSwatchPicker(line, th.color || '#ef4444', (newColor) => {
        th.color = newColor;
        line.style.borderColor = newColor;
        onUpdate();
      });
    });
    row.appendChild(line);
    const name = document.createElement('span');
    name.className = 'plot-compare-name';
    name.textContent = `${th.label || th.value} = ${th.value}`;
    row.appendChild(name);
    const styleSel = document.createElement('select');
    styleSel.className = 'plot-threshold-style-mini';
    for (const s of ['dashed', 'solid', 'dotted']) {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = s;
      if ((th.style || 'dashed') === s) opt.selected = true;
      styleSel.appendChild(opt);
    }
    styleSel.addEventListener('change', () => {
      th.style = styleSel.value;
      line.style.borderTopStyle = styleSel.value;
      onUpdate();
    });
    row.appendChild(styleSel);
    const rm = document.createElement('button');
    rm.className = 'plot-compare-remove';
    rm.textContent = '×';
    rm.setAttribute('aria-label', `Remove threshold ${th.label || th.value}`);
    rm.addEventListener('click', () => {
      graph.thresholds.splice(i, 1);
      onUpdate();
      _updateThresholdList(container, graph, onUpdate);
    });
    row.appendChild(rm);
    container.appendChild(row);
  }
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
  legendNode.addEventListener('click', (e) => {
    const swatch = e.target.closest('.plot-legend-swatch');
    if (swatch) {
      const btn = swatch.closest('button[data-series]');
      if (!btn) return;
      e.preventDefault();
      const seriesName = btn.dataset.series;
      _openSwatchPicker(swatch, swatch.dataset.hex || '#58a6ff', (newColor) => {
        swatch.dataset.hex = newColor;
        if (opts.cfg && opts.cfg.series) {
          const idx = opts.cfg.series.findIndex(s =>
            `S${s.subjectId} · ${s.attribute}` === seriesName
          );
          if (idx >= 0) {
            opts.cfg.series[idx].color = newColor;
            const card = plotArea.closest('.compare-graph-card');
            if (card) {
              const panelSwatches = card.querySelectorAll('.plot-compare-swatch');
              if (panelSwatches[idx]) panelSwatches[idx].style.background = newColor;
            }
          }
        } else {
          const sid = state.selectedPlotSubject;
          if (sid != null) state.plotColorOverrides[`${sid}:${seriesName}`] = newColor;
        }
        if (opts.invalidate) opts.invalidate();
        else _plotInvalidate();
        saveSettings();
        if (opts.restart) opts.restart();
        else _plotRestart();
      });
      return;
    }
    const btn = e.target.closest('button[data-series]');
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

const THRESHOLD_STYLES = { solid: 'none', dashed: '6 3', dotted: '2 3' };

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
    const lineGen = d3.line().x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
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

  const strokeW = cfg ? cfg.stroke : 1.5;
  const showDots = cfg ? cfg.disconnectPoints : false;
  const lineGen = d3.line().x((p) => xScale(p.t)).y((p) => yScale(p.v)).curve(d3.curveLinear);
  const showLine = !showDots;

  const lines = overlay.selectAll('.compare-line').data(compareSeries, (d) => d.name);
  lines.enter().append('path')
    .attr('class', 'compare-line')
    .attr('fill', 'none')
    .attr('clip-path', 'url(#compare-panel-clip)')
    .merge(lines)
    .attr('stroke', (d, i) => d.color || PLOT_COLORS[(primaryCount + i) % PLOT_COLORS.length])
    .attr('stroke-width', strokeW)
    .attr('d', (d) => showLine && d.data.length >= 2 ? lineGen(d.data) : null)
    .attr('opacity', (d) => showLine && d.data.length >= 2 ? 1 : 0);
  lines.exit().remove();

  if (showDots) {
    compareSeries.forEach((s, i) => {
      const color = s.color || PLOT_COLORS[(primaryCount + i) % PLOT_COLORS.length];
      const safeN = _safeId(s.name);
      let dotsG = overlay.select(`.compare-dots-${safeN}`);
      if (dotsG.empty()) {
        dotsG = overlay.append('g').attr('class', `compare-dots-${safeN}`)
          .attr('clip-path', 'url(#compare-panel-clip)');
      }
      const vis = s.data.filter((p) => xScale(p.t) >= 0 && xScale(p.t) <= w);
      const maxDots = 600;
      const step = vis.length > maxDots ? Math.ceil(vis.length / maxDots) : 1;
      const sampled = step > 1 ? vis.filter((_, j) => j % step === 0) : vis;
      const dots = dotsG.selectAll('circle').data(sampled, (p) => p.t);
      dots.enter().append('circle').attr('fill', color)
        .merge(dots)
        .attr('r', strokeW)
        .attr('cx', (p) => xScale(p.t))
        .attr('cy', (p) => yScale(p.v));
      dots.exit().remove();
    });
  } else {
    overlay.selectAll('[class^="compare-dots"]').remove();
  }

  let label = overlay.select('.compare-panel-label');
  if (label.empty()) {
    label = overlay.append('text').attr('class', 'panel-label compare-panel-label').attr('x', 4).attr('y', 11);
  }
  label.text('Compare').attr('fill', 'var(--muted)');
};

const bindPlotTooltip = (g, plotArea, visible, xScale, w, HEADER_H, rect) => {
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
      showAtTimestamp(e.detail.t);
    };
    plotArea._crosshairHide = (e) => {
      if (e.detail.source === plotArea) return;
      hideCrosshair();
    };
    syncContainer.addEventListener('crosshair-sync', plotArea._crosshairSync);
    syncContainer.addEventListener('crosshair-hide', plotArea._crosshairHide);
  }

  overlay
    .on('mousemove', (event) => {
      const [mx] = d3.pointer(event);
      showCrosshairAt(mx);
      if (syncContainer) {
        const t0 = xScale.invert(mx);
        syncContainer.dispatchEvent(new CustomEvent('crosshair-sync', { detail: { t: t0, source: plotArea } }));
      }
    })
    .on('mouseleave', () => {
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
      const step = w / 20;
      if (e.key === 'ArrowLeft') { kbPos = Math.max(0, kbPos - step); }
      else if (e.key === 'ArrowRight') { kbPos = Math.min(w, kbPos + step); }
      else if (e.key === 'Escape') { hideCrosshair(); return; }
      else return;
      e.preventDefault();
      showCrosshairAt(kbPos);
      if (syncContainer) {
        const t0 = xScale.invert(kbPos);
        syncContainer.dispatchEvent(new CustomEvent('crosshair-sync', { detail: { t: t0, source: plotArea } }));
      }
    });
  }
};

const updatePlotLegend = (plotArea, allSeries, hidden) => {
  const legend = plotArea.querySelector('.plot-legend');
  if (!legend) return;
  const seriesKey = allSeries.map((s) => `${s.name}:${s.color || ''}`).join('|');
  if (legend.dataset.seriesKey !== seriesKey) {
    legend.dataset.seriesKey = seriesKey;
    legend.innerHTML = allSeries.map((s, i) => {
      const isActive = !hidden.has(s.name);
      const color = s.color || PLOT_COLORS[i % PLOT_COLORS.length];
      return `<button type="button" class="plot-legend-item${isActive ? ' active' : ''}" data-series="${escapeHtml(s.name)}" aria-pressed="${isActive}"><span class="plot-legend-swatch" style="background:${color}" data-hex="${escapeHtml(color)}"></span>${escapeHtml(s.name)}</button>`;
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

const _plotInvalidate = () => { _lastPlotFingerprint = ''; };
const _plotRerender = () => { renderPlot(el('selectedNodeContent')); };
const _plotRestart = () => { startPlotAnim(); };

const renderPlot = (container) => {
  const plotArea = container.querySelector('.detail-plot-area');
  if (!plotArea) return;

  const sid = state.selectedPlotSubject;
  if (sid == null) {
    plotArea.innerHTML = '<div class="plot-empty">Click a subject to plot its data</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const allSeries = collectPlotSeries(sid, _subjectsPlotCfg);
  allSeries.forEach((s, i) => {
    s.color = state.plotColorOverrides[`${sid}:${s.name}`] || PLOT_COLORS[i % PLOT_COLORS.length];
  });
  if (!allSeries.length) {
    plotArea.innerHTML = '<div class="plot-empty">No numeric data to plot</div>';
    _lastPlotFingerprint = '';
    return;
  }

  const lastPts = allSeries.map((s) => s.data.length ? s.data[s.data.length - 1].t : 0);
  const isSubjects = state.activeView === 'subjects';
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
