// Connection lifecycle: WebSocket, REST polling (status, nodes, interfaces),
// throughput meter, semaphores, dashboard/CAN connect/disconnect, and the
// shared disconnectAll teardown used by pollStatus, connectDashboard, and
// the heartbeat in app.js.

const BUS_LOAD_MAX_SAMPLES = 60;

const drawBusLoadSparkline = () => {
  const canvas = el('busLoadSparkline');
  if (!canvas) return;

  const history = state.busLoadHistory;
  if (!state.canConnected || history.length < 2) {
    canvas.classList.add('hidden');
    return;
  }
  canvas.classList.remove('hidden');

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth * dpr;
  const h = canvas.clientHeight * dpr;
  canvas.width = w;
  canvas.height = h;

  ctx.clearRect(0, 0, w, h);

  const max = Math.max(10, ...history);
  const fontSize = 9 * dpr;
  const padTop = fontSize + 2 * dpr;
  const padBottom = 2 * dpr;
  const plotH = h - padTop - padBottom;
  const step = w / (BUS_LOAD_MAX_SAMPLES - 1);
  const xOffset = (BUS_LOAD_MAX_SAMPLES - history.length) * step;

  const toX = (i) => xOffset + i * step;
  const toY = (v) => padTop + plotH - (v / max) * plotH;

  const style = getComputedStyle(canvas);
  const accent = style.getPropertyValue('--accent').trim() || '#58a6ff';
  const muted = style.getPropertyValue('--muted').trim() || '#656d76';

  ctx.beginPath();
  for (let i = 0; i < history.length; i++) {
    const x = toX(i);
    const y = toY(history[i]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.5 * dpr;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // Collect local peaks, keep only the tallest per cluster, skip duplicate values
  const peaks = [];
  for (let i = 1; i < history.length - 1; i++) {
    if (history[i] > history[i - 1] && history[i] >= history[i + 1]) {
      peaks.push(i);
    }
  }

  const minLabelGap = 28 * dpr;
  const labels = [];
  let cluster = [];
  for (const p of peaks) {
    if (cluster.length && toX(p) - toX(cluster[cluster.length - 1]) < minLabelGap) {
      cluster.push(p);
    } else {
      if (cluster.length) labels.push(cluster.reduce((a, b) => history[a] >= history[b] ? a : b));
      cluster = [p];
    }
  }
  if (cluster.length) labels.push(cluster.reduce((a, b) => history[a] >= history[b] ? a : b));

  ctx.font = `${fontSize}px sans-serif`;
  ctx.fillStyle = muted;
  ctx.textAlign = 'center';
  const shownValues = new Set();
  for (const i of labels) {
    const pct = history[i] % 1 === 0 ? `${history[i]}%` : `${history[i].toFixed(1)}%`;
    if (shownValues.has(pct)) continue;
    const x = toX(i);
    const textW = ctx.measureText(pct).width;
    if (x - textW / 2 < 0) continue;
    ctx.fillText(pct, x, padTop - 3 * dpr);
    shownValues.add(pct);
  }
};

const updateSemaphores = () => {
  const serverDot = el('serverSemaphore');
  const canDot = el('canSemaphore');
  const serverInfo = el('serverThroughput');
  const canInfo = el('canUtilization');

  if (serverDot) {
    if (state.dashboardConnected) {
      serverDot.className = 'semaphore ok';
    } else {
      serverDot.className = 'semaphore';
    }
  }

  if (canDot) {
    if (state.canDisconnecting) {
      canDot.className = 'semaphore disconnecting';
    } else if (state.canConnecting) {
      canDot.className = 'semaphore connecting';
    } else if (state.canConnected) {
      canDot.className = 'semaphore ok';
    } else {
      canDot.className = 'semaphore';
    }
  }

  // Lock fields when connected
  el('apiBase').disabled = state.dashboardConnected;
  el('interfacesSelect').disabled = state.canConnected || state.canConnecting;
  el('canSpecInput').disabled = state.canConnecting;
  el('canBitrateSelect').disabled = state.canConnecting;
  el('canBitrateCustom').disabled = state.canConnecting;

  if (serverInfo) {
    if (state.dashboardConnected && state.wsThroughput > 0) {
      serverInfo.textContent = formatThroughput(state.wsThroughput);
      serverInfo.classList.remove('hidden');
    } else if (state.dashboardConnected) {
      serverInfo.textContent = '0 B/s';
      serverInfo.classList.remove('hidden');
    } else {
      serverInfo.classList.add('hidden');
    }
  }

  if (canInfo) {
    if (state.canConnected) {
      const rate = getTotalMessageRate();
      const util = state.busUtilization;
      const utilStr = util != null ? ` · ${util}% load` : '';
      const dropped = state.droppedEvents;
      const droppedTotal = dropped ? dropped.scanner + dropped.logger + dropped.clients : 0;
      const dropStr = droppedTotal > 0 ? ` · ${droppedTotal} dropped` : '';
      canInfo.textContent = `${rate.toFixed(1)} msg/s${utilStr}${dropStr}`;
      canInfo.title = droppedTotal > 0
        ? `Messages lost because the backend could not keep up — before decoding: ${dropped.scanner}, `
          + `from history/recordings: ${dropped.logger}, from dashboard views: ${dropped.clients}`
        : '';
      canInfo.classList.remove('hidden');
      canInfo.classList.remove('load-ok', 'load-warn', 'load-err', 'load-crit');
      if (util != null) {
        canInfo.classList.add(util > 100 ? 'load-crit' : util >= 80 ? 'load-err' : util >= 50 ? 'load-warn' : 'load-ok');
      }
      // Lost data outranks a healthy load reading.
      if (droppedTotal > 0 && (util == null || util < 50)) {
        canInfo.classList.remove('load-ok');
        canInfo.classList.add('load-warn');
      }
    } else {
      canInfo.classList.add('hidden');
      canInfo.classList.remove('load-ok', 'load-warn', 'load-err', 'load-crit');
    }
  }
};

const updateDashboardConnectButton = () => {
  const button = el('connectDashboardBtn');
  if (!button) {
    return;
  }
  if (state.dashboardConnecting) {
    button.textContent = 'Connecting…';
  } else {
    button.textContent = state.dashboardConnected ? 'Disconnect' : 'Connect';
  }
  button.disabled = state.canConnecting || state.canDisconnecting || state.dashboardConnecting;
  updateSemaphores();
};

const updateCanConnectButton = () => {
  const button = el('connectCanBtn');
  if (!button) {
    return;
  }
  button.textContent = state.canConnected ? 'Disconnect' : 'Connect';
  const incomplete = !state.canConnected && !canFormReady();
  button.disabled = state.canConnecting || state.canDisconnecting || incomplete;
  button.title = incomplete && selectedCanTarget().interface ? 'Choose the bus bitrate first' : '';
  updateSemaphores();
};

const STALE_WS_THRESHOLD_MS = 10000;

const _attachReplayIfActive = async () => {
  if (!state.dashboardConnected) return;
  try {
    const s = await requestJson('/api/replay/status');
    if (s && s.active && typeof showReplayStrip === 'function') {
      _applyReplayStatus?.(s);
      showReplayStrip();
    }
  } catch (_) { /* nothing to attach */ }
};

const _updateStaleBanner = () => {
  const banner = el('staleBanner');
  if (!banner) return;
  const last = state.lastWsMessageMs;
  const open = state.dashboardConnected && last > 0;
  const gapMs = open ? (Date.now() - last) : 0;
  if (open && gapMs > STALE_WS_THRESHOLD_MS) {
    const seconds = Math.floor(gapMs / 1000);
    banner.textContent = `Connection paused — last update ${seconds}s ago. Data may be stale.`;
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
};

const startThroughputTimer = () => {
  if (state.throughputTimer) clearInterval(state.throughputTimer);
  state.throughputTimer = window.setInterval(() => {
    state.wsThroughput = state.wsBytesAccum;
    state.wsBytesAccum = 0;
    updateSemaphores();
    _updateStaleBanner();
  }, 1000);
};

const stopThroughputTimer = () => {
  if (state.throughputTimer) {
    clearInterval(state.throughputTimer);
    state.throughputTimer = null;
  }
  state.wsBytesAccum = 0;
  state.wsThroughput = 0;
  state.lastWsMessageMs = 0;
  _updateStaleBanner();
};

// ── WebSocket ──

const scheduleReconnect = () => {
  if (state.userClosedWs) {
    return;
  }
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
  }

  state.wsReconnectAttempts += 1;
  const delaySeconds = Math.min(30, 2 ** Math.min(state.wsReconnectAttempts, 5));
  state.wsReconnectTimer = window.setTimeout(() => connectWs(), delaySeconds * 1000);
};

const connectWs = () => {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  state.userClosedWs = false;
  state.ws = new WebSocket(wsUrlWithToken('/ws'));

  state.ws.onopen = () => {
    state.wsReconnectAttempts = 0;
    state.lastWsMessageMs = Date.now();
    _updateStaleBanner();
    updateSemaphores();
    // If a replay session is already running (other tab, page reload during
    // playback, backend restarted with state restored), attach the playback
    // strip on connect.
    _attachReplayIfActive();
  };

  state.ws.onclose = () => {
    updateSemaphores();
    if (state.userClosedWs) {
      return;
    }
    scheduleReconnect();
  };

  state.ws.onerror = () => {
    updateSemaphores();
  };

  state.ws.onmessage = (message) => {
    try {
      state.wsBytesAccum += (message.data?.length || 0);
      state.lastWsMessageMs = Date.now();
      const event = JSON.parse(message.data);
      if (event.type === 'filter_updated' || event.type === 'pong') {
        return;
      }
      if (event.type === 'replay_ended') {
        // Two distinct paths:
        //   finished=true  → playback reached the end naturally; transition
        //                    the strip to a "Finished" mode and keep the
        //                    caches populated so the user can inspect the
        //                    final state. Cache cleanup happens when the
        //                    user dismisses via Close.
        //   finished=false → user clicked Stop; tear down immediately.
        const finishedNaturally = !!event.finished;
        if (finishedNaturally) {
          state.replayActive = false;
          state.replayPaused = false;
          state.replayFinished = true;
          state.replayPositionS = state.replayDurationS;
          if (typeof syncReplayStrip === 'function') syncReplayStrip();
        } else {
          state.latestBySubject.clear();
          state.latestByNode.clear();
          state.subjectHistory.clear();
          if (typeof hideReplayStrip === 'function') hideReplayStrip();
          if (typeof getAllNodes === 'function') getAllNodes();
          renderNodesTable?.();
          renderSelectedNodeContent?.();
        }
        return;
      }
      if (event.type === 'metrics') {
        state.busUtilization = event.bus_utilization ?? null;
        if (state.busUtilization != null) {
          state.busLoadHistory.push(state.busUtilization);
          if (state.busLoadHistory.length > BUS_LOAD_MAX_SAMPLES) {
            state.busLoadHistory.shift();
          }
          drawBusLoadSparkline();
          if (state.busUtilization >= 90 && state._busFullArmed) {
            state._busFullArmed = false;
            const ov = el('busFullEaster');
            ov.classList.remove('hidden');
            setTimeout(() => ov.classList.add('hidden'), 5000);
          }
          if (state.busUtilization < 50) {
            state._busFullArmed = true;
          }
        }
        return;
      }
      // Raw frame-capture stream (Debugging view, opt-in). Only this client
      // receives it, and only after it sent a {type:'capture'} subscribe.
      if (event.type === 'can_frame') {
        DebugView.onFrames(event);
        return;
      }
      if (event.type === 'capture_status') {
        DebugView.onCaptureStatus(event);
        return;
      }
      cacheEvent(event);
      ingestLogEvent(event);
      scheduleDetailRefresh();
      scheduleTableRefresh();
    } catch (err) {
      console.warn('WS message handling error:', err);
    }
  };
};

const disconnectWs = () => {
  state.userClosedWs = true;
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
};

// ── REST polling and CAN interface management ──

const OTHER_CAN_INTERFACE = '__other__';

// Mirrors the backend's rule (server/can_config.py): a bare name such as vcan0
// is SocketCAN, whose bitrate the kernel owns; every other adapter runs at
// whatever bitrate it is opened with, so one has to be chosen.
const canSpecNeedsBitrate = (spec) => {
  const value = spec.trim().replace(/^pythoncan:/i, '');
  return value.includes(':') && !value.toLowerCase().startsWith('socketcan:');
};

// python-can interfaces the backend can open as CAN FD; keep in step with
// FD_INTERFACES in server/can_config.py. Listed adapters carry supports_fd.
const FD_CAN_INTERFACES = ['pcan', 'kvaser', 'vector', 'ixxat', 'virtual'];

const canSpecSupportsFd = (spec) => FD_CAN_INTERFACES.includes(
  spec.trim().replace(/^pythoncan:/i, '').split(':')[0].toLowerCase());

const formatBitrate = (bitrate) => (bitrate >= 1000000
  ? `${bitrate / 1000000} Mbit/s`
  : `${bitrate / 1000} kbit/s`);

// "500 kbit/s · FD 2 Mbit/s", "500 kbit/s", "CAN FD" (SocketCAN sets its own rates), or ''.
const formatCanRates = (bitrate, dataBitrate, fd) => {
  const parts = [];
  if (bitrate) parts.push(formatBitrate(bitrate));
  if (dataBitrate) parts.push(`FD ${formatBitrate(dataBitrate)}`);
  else if (fd) parts.push('CAN FD');
  return parts.join(' · ');
};

// What Connect would open: the interface spec, whether it needs a bitrate,
// and whether it can be opened as CAN FD with a data bitrate.
const selectedCanTarget = () => {
  const selected = el('interfacesSelect').value;
  if (selected === OTHER_CAN_INTERFACE) {
    const spec = el('canSpecInput').value.trim();
    return { interface: spec, needsBitrate: canSpecNeedsBitrate(spec), supportsFd: canSpecSupportsFd(spec) };
  }
  const adapter = state.canAdapters.find((a) => a.interface === selected);
  return {
    interface: selected,
    needsBitrate: adapter ? adapter.needs_bitrate : canSpecNeedsBitrate(selected),
    supportsFd: adapter ? !!adapter.supports_fd : canSpecSupportsFd(selected),
  };
};

// The chosen CAN FD data bitrate in bit/s, or null for Classic CAN.
const selectedCanDataBitrate = () => Number(el('canDataBitrateSelect').value) || null;

// The chosen bitrate in bit/s, or null if none is chosen or the custom one is invalid.
const selectedCanBitrate = () => {
  const choice = el('canBitrateSelect').value;
  const bitrate = Number(choice === 'custom' ? el('canBitrateCustom').value : choice);
  return Number.isInteger(bitrate) && bitrate > 0 && bitrate <= 1000000 ? bitrate : null;
};

const canFormReady = () => {
  const target = selectedCanTarget();
  return !!target.interface && (!target.needsBitrate || selectedCanBitrate() !== null);
};

// Show the spec field and the bitrate controls the current selection calls
// for. With restoreBitrate, preselect the bitrate last used on it, or none.
const updateCanForm = ({ restoreBitrate = false } = {}) => {
  const idle = !state.canConnected && !state.canDisconnecting;
  const target = selectedCanTarget();
  const isOther = el('interfacesSelect').value === OTHER_CAN_INTERFACE;
  el('canSpecInput').classList.toggle('hidden', !(idle && isOther));
  el('canBitrateRow').classList.toggle('hidden', !(idle && target.needsBitrate));
  el('canDataBitrateRow').classList.toggle('hidden', !(idle && target.needsBitrate && target.supportsFd));
  if (restoreBitrate) {
    const select = el('canBitrateSelect');
    const remembered = state.canBitrates[target.interface];
    const listed = !!remembered && [...select.options].some((o) => o.value === String(remembered));
    select.value = remembered ? (listed ? String(remembered) : 'custom') : '';
    el('canBitrateCustom').value = remembered && !listed ? String(remembered) : '';
    const dataSelect = el('canDataBitrateSelect');
    const rememberedData = String(state.canDataBitrates[target.interface] || '');
    dataSelect.value = [...dataSelect.options].some((o) => o.value === rememberedData) ? rememberedData : '';
  }
  el('canBitrateCustom').classList.toggle('hidden', el('canBitrateSelect').value !== 'custom');
  updateCanConnectButton();
};

// While connected, the list shows only the interface in use, with its rates.
const showConnectedCanInterface = (iface, bitrate, dataBitrate = null, fd = false) => {
  const select = el('interfacesSelect');
  const adapter = state.canAdapters.find((a) => a.interface === iface);
  select.innerHTML = '';
  const option = document.createElement('option');
  option.value = iface;
  const rates = formatCanRates(bitrate, dataBitrate, fd);
  option.textContent = (adapter ? adapter.label : iface) + (rates ? ` · ${rates}` : '');
  select.appendChild(option);
  select.value = iface;
  updateCanForm();
};

const loadInterfaces = async () => {
  try {
    const data = await requestJson('/api/status');
    // Backends from before adapter discovery send SocketCAN names only.
    state.canAdapters = data.available_adapters
      || (data.available_interfaces || []).map((name) => ({ interface: name, label: name, needs_bitrate: false }));
    const select = el('interfacesSelect');
    const previousValue = select.value;
    const preferredValue = previousValue || state.preferredCanInterface;
    select.innerHTML = '';
    for (const adapter of state.canAdapters) {
      const option = document.createElement('option');
      option.value = adapter.interface;
      option.textContent = adapter.label;
      select.appendChild(option);
    }
    const other = document.createElement('option');
    other.value = OTHER_CAN_INTERFACE;
    other.textContent = 'Other…';
    select.appendChild(other);

    const offered = state.canAdapters.map((a) => a.interface);
    if (preferredValue === OTHER_CAN_INTERFACE || offered.includes(preferredValue)) {
      select.value = preferredValue;
    } else if (preferredValue && preferredValue === state.customCanSpec) {
      // Last connected through "Other…" with this spec.
      select.value = OTHER_CAN_INTERFACE;
    } else {
      // Nothing listed (always so off Linux without a known adapter): the
      // only way forward is to name one.
      select.value = offered.length > 0 ? offered[0] : OTHER_CAN_INTERFACE;
    }
    state.preferredCanInterface = select.value;
    updateCanForm({ restoreBitrate: select.value !== previousValue });
    saveSettings();
    return data;
  } catch (error) {
    if (state.interfacesTimer) {
      clearInterval(state.interfacesTimer);
      state.interfacesTimer = null;
    }
    return null;
  }
};

const startInterfacePolling = () => {
  if (state.interfacesTimer) {
    clearInterval(state.interfacesTimer);
  }
  state.interfacesTimer = window.setInterval(() => {
    if (!state.dashboardConnected || state.canConnected) {
      return;
    }
    loadInterfaces();
  }, 3000);
};

const stopInterfacePolling = () => {
  if (state.interfacesTimer) {
    clearInterval(state.interfacesTimer);
    state.interfacesTimer = null;
  }
};

const selectInterface = async () => {
  const target = selectedCanTarget();
  if (!target.interface) {
    return null;
  }
  const body = { interface: target.interface };
  if (target.needsBitrate) {
    body.bitrate = selectedCanBitrate();
    if (body.bitrate === null) {
      showToast('Choose the bus bitrate first', 'error');
      return null;
    }
    const dataBitrate = target.supportsFd ? selectedCanDataBitrate() : null;
    if (dataBitrate) {
      body.data_bitrate = dataBitrate;
    }
  }

  try {
    const data = await requestJson('/api/can/connect', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    state.preferredCanInterface = el('interfacesSelect').value;
    if (body.bitrate) {
      state.canBitrates[target.interface] = body.bitrate;
      if (body.data_bitrate) {
        state.canDataBitrates[target.interface] = body.data_bitrate;
      } else {
        delete state.canDataBitrates[target.interface];
      }
    }
    showConnectedCanInterface(target.interface, body.bitrate, body.data_bitrate, data?.can_fd);
    saveSettings();
    return data;
  } catch (error) {
    showToast(`CAN connect failed: ${error.message}`, 'error');
    return null;
  }
};

const disconnectAll = ({ persist = true } = {}) => {
  state.dashboardConnected = false;
  state.canConnected = false;
  state.busUtilization = null;
  state.droppedEvents = null;
  state.busLoadHistory.length = 0;
  drawBusLoadSparkline();
  state.latestBySubject.clear();
  state.latestByNode.clear();
  state.subjectHistory.clear();
  state.serviceSchemas.clear();
  state.serviceCallState = null;
  state._subjectServiceCallState = null;
  state._subjectExpandedServiceId = null;
  state._subjectServiceNodeId = null;
  state.serviceCallHistory.length = 0;
  state.plotPaused = false;
  state.plotPausedAt = null;
  for (const g of state.compareGraphs) { g.paused = false; g.pausedAt = null; }
  REG_CACHE.clear();
  stopStatusPolling();
  stopCanStartupDelay();
  stopNodesPolling();
  stopInterfacePolling();
  stopThroughputTimer();
  stopPlotAnim();
  if (typeof GraphView !== 'undefined') GraphView.hide();
  disconnectWs();
  updateDashboardConnectButton();
  updateCanConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();
  updateSemaphores();
  if (persist) saveSettings();
};

const pollStatus = async () => {
  if (state.canConnecting || state.canDisconnecting) {
    return;
  }

  let data;
  try {
    data = await requestJson('/api/status');
  } catch {
    if (state.dashboardConnected) {
      disconnectAll();
    }
    return;
  }

  state.busUtilization = data.bus_utilization ?? null;
  state.droppedEvents = data.dropped ?? null;

  const backendCanRunning = data.status === 'running' && !!data.can_interface;

  if (backendCanRunning && !state.canConnected) {
    // Another client connected CAN
    state.canConnected = true;
    showConnectedCanInterface(data.can_interface, data.can_bitrate, data.can_data_bitrate, data.can_fd);
    state.preferredCanInterface = data.can_interface;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(3000);
    saveSettings();
  } else if (!backendCanRunning && state.canConnected) {
    // CAN disconnected (by another client or due to error)
    state.canConnected = false;
    state.busUtilization = null;
    state.droppedEvents = null;
    REG_CACHE.clear();
    updateCanConnectButton();
    stopCanStartupDelay();
    stopNodesPolling();
    stopThroughputTimer();
    disconnectWs();
    renderNodesTable();
    saveSettings();
    await loadInterfaces();
    startInterfacePolling();

    if (data.last_error) {
      showToast(`CAN disconnected: ${data.last_error}`, 'error');
    }
  }

  updateSemaphores();
};

const startStatusPolling = () => {
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
  }
  state.statusTimer = window.setInterval(pollStatus, 5000);
};

const stopStatusPolling = () => {
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
};

const getAllNodes = async () => {
  try {
    const data = await requestJson('/api/nodes');
    state.latestNodesPayload = data;
    upgradeStableKeys();
    pruneNodeCache();
    const selectedStillExists = state.selectedNodeId !== null && data.nodes && data.nodes[String(state.selectedNodeId)];
    if (!selectedStillExists) {
      state.selectedNodeId = null;
    }
    renderNodesTable();
    renderSelectedNodeContent();
  } catch (error) {
    state.latestNodesPayload = { node_count: 0, nodes: {} };
    state.selectedNodeId = null;
    renderNodesTable();
    renderSelectedNodeContent();
  }
};

const NODES_POLL_INTERVAL_MS = 1000;

const startNodesPolling = () => {
  if (state.nodesTimer) {
    clearInterval(state.nodesTimer);
  }
  state.nodesTimer = window.setInterval(getAllNodes, NODES_POLL_INTERVAL_MS);
};

const stopNodesPolling = () => {
  if (state.nodesTimer) {
    clearInterval(state.nodesTimer);
    state.nodesTimer = null;
  }
};

const stopCanStartupDelay = () => {
  if (state.canStartupTimer) {
    clearTimeout(state.canStartupTimer);
    state.canStartupTimer = null;
  }
};

const schedulePostCanStartup = (delayMs = 10000) => {
  stopCanStartupDelay();
  state.canStartupTimer = window.setTimeout(async () => {
    state.canStartupTimer = null;
    if (!state.dashboardConnected || !state.canConnected) {
      return;
    }

    await getAllNodes();
    startNodesPolling();
    startThroughputTimer();
    connectWs();
  }, delayMs);
};

const connectDashboard = async () => {
  if (state.canConnecting || state.canDisconnecting) return;
  if (state.dashboardConnected) {
    disconnectAll();
    return;
  }

  state.dashboardConnecting = true;
  updateDashboardConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();

  let statusData;
  try {
    statusData = await requestJson('/api/status');
  } catch (error) {
    state.dashboardConnecting = false;
    updateDashboardConnectButton();
    renderNodesTable();
    renderSelectedNodeContent();
    showToast(`Backend unreachable: ${error.message}`, 'error');
    return;
  }

  state.dashboardConnecting = false;
  state.dashboardConnected = true;
  updateDashboardConnectButton();
  startStatusPolling();

  // Open WS + throughput meter + node polling as soon as the dashboard is
  // connected, regardless of CAN state — this is the only path that lets
  // replay events reach the frontend without an active CAN session.
  startThroughputTimer();
  connectWs();
  await getAllNodes();
  startNodesPolling();

  // If backend already has CAN running, sync state
  if (statusData.status === 'running' && statusData.can_interface) {
    state.canAdapters = statusData.available_adapters || [];
    state.canConnected = true;
    showConnectedCanInterface(statusData.can_interface, statusData.can_bitrate,
      statusData.can_data_bitrate, statusData.can_fd);
    state.preferredCanInterface = statusData.can_interface;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(5000);
  } else {
    await loadInterfaces();
    startInterfacePolling();
  }

  renderNodesTable();
  renderSelectedNodeContent();
  if (state.activeView === 'dsdl') DsdlView.init();
  if (state.activeView === 'debug') DebugView.init();
  fetchRecordings();
  saveSettings();
};

const connectCan = async () => {
  if (state.canConnecting || state.canDisconnecting) return;
  if (state.canConnected) {
    // Disconnect CAN
    state.canDisconnecting = true;
    updateCanConnectButton();

    try {
      await requestJson('/api/can/disconnect', { method: 'POST' });
    } catch {
    }

    state.canDisconnecting = false;
    state.canConnected = false;
    REG_CACHE.clear();
    updateCanConnectButton();
    stopCanStartupDelay();
    stopNodesPolling();
    stopThroughputTimer();
    disconnectWs();
    renderNodesTable();
    saveSettings();

    // Reload available interfaces
    if (state.dashboardConnected) {
      await loadInterfaces();
      startInterfacePolling();
    }
    updateSemaphores();
    return;
  }

  // Connect CAN
  stopInterfacePolling();
  state.canConnecting = true;
  updateCanConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();

  const result = await selectInterface();
  state.canConnecting = false;
  if (!result) {
    updateCanConnectButton();
    renderNodesTable();
    renderSelectedNodeContent();
    startInterfacePolling();
    return;
  }

  state.canConnected = true;
  updateCanForm();  // hides the bitrate controls; updates the button too
  renderNodesTable();
  renderSelectedNodeContent();
  saveSettings();

  schedulePostCanStartup(5000);

  if (!state.dashboardConnected) {
    await connectDashboard();
  }
};
