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

// Cyphal v1.1 traffic on the bus, which this dashboard (a v1.0 monitor)
// cannot decode: devices sending it would otherwise just look silent.
const V11_NODES_SHOWN = 4;
const renderV11Notice = () => {
  const notice = el('canV11Notice');
  if (!notice) return;
  const v11 = state.canConnected ? state.cyphalV11 : null;
  notice.classList.toggle('hidden', !v11);
  if (!v11) return;
  const shown = v11.nodes.slice(0, V11_NODES_SHOWN).join(', ');
  const more = v11.nodes.length > V11_NODES_SHOWN ? ` +${v11.nodes.length - V11_NODES_SHOWN}` : '';
  notice.textContent = `Cyphal v1.1 from node${v11.nodes.length === 1 ? '' : 's'} ${shown}${more} · not decoded`;
  const subjects = v11.subject_ids.join(', ') + (v11.subject_count > v11.subject_ids.length ? ', …' : '');
  const ago = formatLastSeen([new Date(v11.last_seen_unix * 1000).toISOString()]);
  notice.title = `${v11.transfers} Cyphal v1.1 transfers on ${v11.subject_count} subject-ID`
    + `${v11.subject_count === 1 ? '' : 's'} (${subjects}), the last ${ago}. This dashboard speaks Cyphal v1.0: `
    + 'v1.1 topics are not shown unless the device pins them to a v1.0 subject-ID.';
};

// Errors on the bus: the CAN controller in an error state, or its error
// counters growing. Most often the bus is not terminated, or a node runs
// another bitrate; the backend logs it once, and this shows it while it lasts.
const BUS_ERROR_CAUSES = 'Usual causes: the bus is not terminated (120 Ω at each end, about 60 Ω across '
  + 'CAN_H and CAN_L with the power off), a node runs another bitrate or sample point, CAN_H and CAN_L '
  + 'are swapped or loose, or no other node is on the bus to acknowledge frames. '
  + "The Debug tab shows the controller's counters.";
const renderBusNotice = () => {
  const notice = el('canBusNotice');
  if (!notice) return;
  const errors = state.canConnected ? state.busErrors : null;
  notice.classList.toggle('hidden', !errors);
  if (!errors) return;
  // The counters, which change with every poll, only in the title: a screen
  // reader is told again when the state changes, not every few seconds.
  const what = ['Bus errors', errors.state].filter(Boolean).join(' · ');
  // Transmit errors alone: Cynitor's frames go unacknowledged, as when it is alone on the bus.
  const hint = errors.tx_errors >= 128 && errors.rx_errors === 0
    ? 'No other node acknowledges: check the bus is connected and a node is powered'
    : 'Check 120 Ω termination and bitrate';
  const html = `${escapeHtml(what)}<span class="can-bus-notice-hint">${escapeHtml(hint)}</span>`;
  if (notice.innerHTML !== html) notice.innerHTML = html;
  const counted = errors.tx_errors != null && errors.rx_errors != null
    ? ` The controller's error counters: TX ${errors.tx_errors}, RX ${errors.rx_errors} (128 or more is error-passive).`
    : '';
  notice.title = `Errors on the bus since ${formatPlotTime(errors.since_unix)}.${counted} ${BUS_ERROR_CAUSES}`;
};

// Listen only: the choice in the connect form, and while connected whether
// the session sends nothing, which is why nodes then have no names or registers.
const LISTEN_ONLY_HINT = 'Connect without sending anything on the bus: no node-ID, no heartbeat, no requests. '
  + 'Nodes then show no names, versions or registers, and nothing can be written or called; '
  + 'subjects decode from their fixed IDs and the types you set.';
// No commands: the choice in the connect form, and while connected whether
// the session refuses everything that would change a node.
const NO_COMMANDS_HINT = 'Connect to watch the bus and read its nodes without changing any: register writes, '
  + 'service calls, firmware updates and test nodes on a real bus are refused, and no allocator runs.';

// A connect-form choice that holds for the session: shown while idle, and while
// connected only if the session runs with it, then locked.
const renderSessionToggle = (rowId, toggleId, chosen, active, activeHint, hint) => {
  const row = el(rowId);
  if (!row) return;
  const idle = !state.canConnected && !state.canDisconnecting;
  const on = state.canConnected && active;
  row.classList.toggle('hidden', !(idle || on));
  const toggle = el(toggleId);
  toggle.checked = idle ? chosen : on;
  toggle.disabled = !idle || !state.dashboardConnected || state.canConnecting;
  row.classList.toggle('is-locked', toggle.disabled);
  row.title = on ? activeHint : hint;
};

const renderListenOnly = () => {
  renderSessionToggle('canListenOnlyRow', 'canListenOnlyToggle', state.canListenOnly, state.listenOnly,
    'This session sends nothing on the bus: nodes show no names or registers.', LISTEN_ONLY_HINT);
  renderSessionToggle('canNoCommandsRow', 'canNoCommandsToggle', state.canNoCommands, state.noCommands,
    'This session changes no node: writes, calls and updates are refused.', NO_COMMANDS_HINT);
};

// Cynitor's node-ID allocator: the choice in the connect form, and while
// connected the session's own, if it runs one. One that another allocator on
// the bus has silenced says which node that is.
const ALLOCATOR_HINT = 'Have Cynitor give node-IDs to the nodes that ask for one (Cyphal plug-and-play). '
  + 'Only for a bus with no allocator of its own: a bus may have one only.';
const renderAllocator = () => {
  const row = el('canAllocatorRow');
  if (!row) return;
  const idle = !state.canConnected && !state.canDisconnecting;
  const running = state.canConnected ? state.allocatorStatus : null;
  row.classList.toggle('hidden', !(idle || running));
  const toggle = el('canAllocatorToggle');
  toggle.checked = idle ? state.canAllocator : !!running;
  toggle.disabled = !idle || !state.dashboardConnected || state.canConnecting;
  row.classList.toggle('is-locked', toggle.disabled);
  row.title = running
    ? `Cynitor gives node-IDs to the nodes that ask for one: ${running.allocated} given in this session.`
    : ALLOCATOR_HINT;
  const other = running ? running.other_allocator : null;
  const notice = el('canAllocatorNotice');
  notice.classList.toggle('hidden', other == null);
  if (other == null) return;
  notice.textContent = `Allocator silent · node ${other} allocates here`;
  notice.title = `Node ${other} answers node-ID requests on this bus too. A bus has one allocator, so `
    + "Cynitor's answers no more. Connect without the allocator on this bus.";
};

// The server's CPU use, under its data rate. It takes the bus's traffic on one
// core, so the reading turns amber, then red, as it nears all of one.
const SERVER_CPU_WARN = 70;
const SERVER_CPU_FULL = 90;
const renderServerCpu = () => {
  const reading = el('serverCpu');
  if (!reading) return;
  const cpu = state.dashboardConnected ? state.serverCpu : null;
  reading.classList.toggle('hidden', cpu == null);
  reading.classList.toggle('load-warn', cpu != null && cpu >= SERVER_CPU_WARN && cpu < SERVER_CPU_FULL);
  reading.classList.toggle('load-err', cpu != null && cpu >= SERVER_CPU_FULL);
  if (cpu == null) return;
  reading.textContent = `CPU ${Math.round(cpu)}%`;
  reading.title = `The server uses ${cpu}% of one core. It takes the bus's traffic on one core: near 100% `
    + 'it cannot keep up, and what it then loses is counted as dropped.';
};

// The server's switch between decoding every subject and only those watched
// or recorded: for a bus that carries more than it can decode.
const renderDecodeSwitch = () => {
  const toggle = el('decodeOnDemandToggle');
  if (!toggle) return;
  toggle.checked = state.dashboardConnected && state.decodeOnDemand;
  toggle.disabled = !state.dashboardConnected;
  el('decodeOnDemandRow').classList.toggle('is-locked', toggle.disabled);
};

const setDecodeOnDemand = async (on) => {
  try {
    const data = await requestJson('/api/decoding', { method: 'POST', body: JSON.stringify({ on_demand: on }) });
    state.decodeOnDemand = data?.decode_on_demand === true;
  } catch (error) {
    showToast(`Decoding not switched: ${error.message}`, 'error');
  }
  renderDecodeSwitch();
};

// The subjects whose values this dashboard shows: the selected node's, those
// plotted here or in Compare, those the log panel lists. The server is told
// whenever they change, and while it decodes on demand decodes no others
// (but what a recording takes). Asked several times a second: nothing is sent
// unless they changed.
const watchedSubjects = () => {
  const watched = new Set(state.logSubjectIds);
  const node = getSelectedNode();
  for (const sid of [...(node?.publishers || []), ...(node?.subscribers || [])]) watched.add(sid);
  for (const sid of [state.selectedPlotSubject, state._subjectsPlotSubject]) {
    if (sid != null) watched.add(Number(sid));
  }
  for (const graph of state.compareGraphs) {
    for (const series of graph.series || []) watched.add(series.subjectId);
  }
  return [...watched].filter((sid) => Number.isInteger(sid) && sid >= 0 && sid <= 8191).sort((a, b) => a - b);
};

const syncWatchedSubjects = () => {
  if (state.ws?.readyState !== WebSocket.OPEN) {
    state._watchedSent = null;  // a new socket is told anew
    return;
  }
  const watched = watchedSubjects();
  const sent = watched.join(',');
  if (sent === state._watchedSent) return;
  state._watchedSent = sent;
  state.ws.send(JSON.stringify({ type: 'watch', subject_ids: watched }));
};

const updateSemaphores = () => {
  renderV11Notice();
  renderBusNotice();
  renderListenOnly();
  renderAllocator();
  renderDecodeSwitch();
  const serverDot = el('serverSemaphore');
  const canDot = el('canSemaphore');
  const serverInfo = el('serverThroughput');
  const canInfo = el('canUtilization');

  if (serverDot) {
    // Pulsing while the backend is being reached, a lost one tried again too.
    serverDot.className = state.dashboardConnected ? 'semaphore ok'
      : state.dashboardConnecting || state.dashboardRetry ? 'semaphore connecting' : 'semaphore';
  }

  if (canDot) {
    if (state.canDisconnecting) {
      canDot.className = 'semaphore disconnecting';
    } else if (state.canConnecting) {
      canDot.className = 'semaphore connecting';
    } else if (state.canConnected) {
      canDot.className = state.busErrors ? 'semaphore warn' : 'semaphore ok';
    } else {
      canDot.className = 'semaphore';
    }
  }

  // Lock fields when connected; the CAN form needs the server, whose list it shows.
  el('apiBase').disabled = state.dashboardConnected || Boolean(state.dashboardRetry);
  const canFormOff = !state.dashboardConnected || state.canConnecting;
  el('interfacesSelect').disabled = canFormOff || state.canConnected;
  el('canSpecInput').disabled = canFormOff;
  el('canBitrateSelect').disabled = canFormOff;
  el('canBitrateCustom').disabled = canFormOff;
  el('canDataBitrateSelect').disabled = canFormOff;
  renderRecordStart();  // the Record tab's Start needs both connections
  DebugView.renderControls();  // so does the Debug tab's Start capture, and its socket
  renderLogConnection();  // and the log panel says what can reach it

  if (serverInfo) {
    if (state.dashboardConnected && state.wsThroughput > 0) {
      serverInfo.textContent = formatThroughput(state.wsThroughput);
      serverInfo.classList.remove('hidden');
    } else if (state.dashboardConnected) {
      serverInfo.textContent = '0 B/s';
      serverInfo.classList.remove('hidden');
    } else if (state.dashboardRetry) {
      serverInfo.textContent = 'reconnecting…';
      serverInfo.classList.remove('hidden');
    } else {
      serverInfo.classList.add('hidden');
    }
  }
  renderServerCpu();

  if (canInfo) {
    if (state.canConnected) {
      // A reading a line: the traffic, coloured by its load; what was lost, in amber; who Cynitor is on the bus.
      const rate = getTotalMessageRate();
      const util = state.busUtilization;
      const traffic = el('canTraffic');
      traffic.textContent = `${rate.toFixed(1)} msg/s${util != null ? ` · ${util}% load` : ''}`;
      traffic.classList.remove('load-ok', 'load-warn', 'load-err', 'load-crit');
      if (util != null) {
        traffic.classList.add(util > 100 ? 'load-crit' : util >= 80 ? 'load-err' : util >= 50 ? 'load-warn' : 'load-ok');
      }
      const dropped = state.droppedEvents;
      const droppedTotal = dropped ? dropped.scanner + dropped.logger + dropped.clients : 0;
      const lost = el('canDropped');
      lost.textContent = `${droppedTotal} dropped`;
      lost.title = droppedTotal > 0
        ? `Messages lost because the backend could not keep up — before decoding: ${dropped.scanner}, `
          + `from history/recordings: ${dropped.logger}, from dashboard views: ${dropped.clients}`
        : '';
      lost.classList.toggle('hidden', droppedTotal === 0);
      const who = el('canSelf');
      who.textContent = [state.listenOnly ? 'listening only' : state.ownNodeId != null ? `as node ${state.ownNodeId}` : '',
        state.noCommands && !state.listenOnly ? 'no commands' : ''].filter(Boolean).join(' · ');
      who.classList.toggle('hidden', !who.textContent);
      canInfo.classList.remove('hidden');
    } else {
      canInfo.classList.add('hidden');
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
    // A session waiting to reconnect is still on: Disconnect ends it.
    button.textContent = state.dashboardConnected || state.dashboardRetry ? 'Disconnect' : 'Connect';
  }
  button.disabled = state.canConnecting || state.canDisconnecting || state.dashboardConnecting;
  updateSemaphores();
};

const updateCanConnectButton = () => {
  const button = el('connectCanBtn');
  if (!button) {
    return;
  }
  button.textContent = state.canConnecting ? 'Connecting…' : state.canDisconnecting ? 'Disconnecting…'
    : state.canConnected ? 'Disconnect' : 'Connect';
  const incomplete = !state.canConnected && !canFormReady();
  const serverOff = !state.dashboardConnected;
  button.disabled = serverOff || state.canConnecting || state.canDisconnecting || incomplete;
  button.title = serverOff ? (state.dashboardRetry ? 'Waiting for the server to answer' : 'Connect to the server first')
    : incomplete && selectedCanTarget().interface ? 'Choose the bus bitrate first' : '';
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

// How long to wait after `attempts` failed tries to reconnect: 2 s, 4 s, 8 s,
// 16 s, then every 30 s. The socket and the backend itself are tried so.
const retryDelayMs = (attempts) => Math.min(30, 2 ** Math.min(attempts, 5)) * 1000;

// ── WebSocket ──

const scheduleReconnect = () => {
  if (state.userClosedWs) {
    return;
  }
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
  }

  state.wsReconnectAttempts += 1;
  state.wsReconnectTimer = window.setTimeout(() => connectWs(), retryDelayMs(state.wsReconnectAttempts));
};

const connectWs = () => {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  state.userClosedWs = false;
  state.ws = new WebSocket(wsUrlWithToken('/ws'));

  state.ws.onopen = () => {
    state._watchedSent = null;
    syncWatchedSubjects();
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
    DebugView.onSocketClosed();
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
          state.replayEventsEmitted = state.replayTotalEvents;  // the last poll was before the last events
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
        state.serverCpu = event.server_cpu ?? null;
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
      // A rate counted from frames (the server decodes on demand) has no text.
      if (!event.undecoded) ingestLogEvent(event);
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
// is SocketCAN, whose bitrate the kernel owns, and a simulated bus (sim:...)
// has its own; every other adapter runs at whatever bitrate it is opened
// with, so one has to be chosen.
const canSpecNeedsBitrate = (spec) => {
  const value = spec.trim().replace(/^pythoncan:/i, '');
  return value.includes(':') && !value.toLowerCase().startsWith('socketcan:') && !value.startsWith('sim:');
};

// python-can interfaces the backend can open as CAN FD; keep in step with
// FD_INTERFACES in server/can_config.py. Listed adapters carry supports_fd.
const FD_CAN_INTERFACES = ['pcan', 'kvaser', 'vector', 'ixxat', 'gs_usb', 'slcan', 'virtual'];

// Those that run the CAN FD data phase at some bitrates only: the CANable
// 2.0's slcan presets. FD_DATA_BITRATES in server/can_config.py; listed
// adapters carry data_bitrates.
const FD_CAN_DATA_BITRATES = { slcan: [2000000, 5000000] };

const canSpecInterface = (spec) => spec.trim().replace(/^pythoncan:/i, '').split(':')[0].toLowerCase();

const canSpecSupportsFd = (spec) => FD_CAN_INTERFACES.includes(canSpecInterface(spec));

const canSpecDataBitrates = (spec) => FD_CAN_DATA_BITRATES[canSpecInterface(spec)] || null;

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
// whether it can be opened as CAN FD with a data bitrate, and which data
// bitrates it takes if it has presets for them (null: any).
const selectedCanTarget = () => {
  const selected = el('interfacesSelect').value;
  if (selected === OTHER_CAN_INTERFACE) {
    const spec = el('canSpecInput').value.trim();
    return {
      interface: spec,
      needsBitrate: canSpecNeedsBitrate(spec),
      supportsFd: canSpecSupportsFd(spec),
      dataBitrates: canSpecDataBitrates(spec),
    };
  }
  const adapter = state.canAdapters.find((a) => a.interface === selected);
  return {
    interface: selected,
    needsBitrate: adapter ? adapter.needs_bitrate : canSpecNeedsBitrate(selected),
    supportsFd: adapter ? !!adapter.supports_fd : canSpecSupportsFd(selected),
    dataBitrates: adapter ? adapter.data_bitrates || null : canSpecDataBitrates(selected),
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
  const dataSelect = el('canDataBitrateSelect');
  for (const option of dataSelect.options) {
    const offered = !option.value || !target.dataBitrates || target.dataBitrates.includes(Number(option.value));
    option.hidden = !offered;
    option.disabled = !offered;  // Safari shows hidden options
  }
  if (restoreBitrate) {
    const select = el('canBitrateSelect');
    const remembered = state.canBitrates[target.interface];
    const listed = !!remembered && [...select.options].some((o) => o.value === String(remembered));
    select.value = remembered ? (listed ? String(remembered) : 'custom') : '';
    el('canBitrateCustom').value = remembered && !listed ? String(remembered) : '';
    const rememberedData = String(state.canDataBitrates[target.interface] || '');
    dataSelect.value = [...dataSelect.options].some((o) => o.value === rememberedData && !o.disabled)
      ? rememberedData : '';
  }
  // A data bitrate picked for another adapter, which this one cannot run.
  if (dataSelect.selectedOptions[0]?.disabled) {
    dataSelect.value = '';
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
    // The user's pick, kept when it is not listed for a while (a USB adapter
    // enumerating again), so that it is picked again once it is.
    const preferredValue = state.preferredCanInterface || previousValue;
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

// A first connect compiles the DSDL, and every connect listens for heartbeats
// to pick a node-ID: it can take well over the 15 s other requests get.
const CAN_CONNECT_TIMEOUT_MS = 120000;

const selectInterface = async () => {
  const target = selectedCanTarget();
  if (!target.interface) {
    return null;
  }
  const body = {
    interface: target.interface, allocator: state.canAllocator,
    listen_only: state.canListenOnly, no_commands: state.canNoCommands,
  };
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
      timeoutMs: CAN_CONNECT_TIMEOUT_MS,
    });
    state.preferredCanInterface = el('interfacesSelect').value;
    state.allocatorStatus = data?.allocator ?? null;
    state.listenOnly = data?.listen_only === true;
    state.noCommands = data?.no_commands === true;
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
    setCanError(`Connect failed: ${error.message}`);
    return null;
  }
};

// Why the last CAN connect failed, or the session ended, kept under the CAN
// form until the next attempt: a toast is gone in seconds.
const setCanError = (message) => {
  state.canError = message || null;
  el('canError').textContent = state.canError || '';
  el('canError').classList.toggle('hidden', !state.canError);
};

// The bus's nodes and load curve go with its CAN session, however it ends,
// rather than stay on screen looking live.
const clearCanSession = () => {
  state.latestNodesPayload = { node_count: 0, nodes: {} };
  state.busUtilization = null;
  state.droppedEvents = null;
  state.allocatorStatus = null;
  state.listenOnly = false;
  state.noCommands = false;
  state.busLoadHistory.length = 0;
  drawBusLoadSparkline();
  renderNodesTable();
  refreshSubjectsTable();
  renderSelectedNodeContent();
  TestNodesView.refreshIndicator();
};

const disconnectAll = ({ persist = true } = {}) => {
  stopRetryingDashboard();
  state.dashboardConnected = false;
  state.canConnected = false;
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
  // The DSDL tab shows the server's types: with no server it says so, as
  // when opened disconnected, and connectDashboard brings them back.
  if (state.activeView === 'dsdl') DsdlView.init();
  disconnectWs();
  updateDashboardConnectButton();
  updateCanConnectButton();
  clearCanSession();
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
  } catch (error) {
    if (state.dashboardConnected) {
      // Still saved as connected, and tried again until it answers; a token
      // it asks for is the prompt's to give.
      disconnectAll({ persist: false });
      if (error.status !== 401) retryDashboard();
    }
    return;
  }

  state.busUtilization = data.bus_utilization ?? null;
  state.droppedEvents = data.dropped ?? null;
  state.cyphalV11 = data.cyphal_v11 ?? null;
  state.busErrors = data.bus_errors ?? null;
  state.allocatorStatus = data.allocator ?? null;
  state.listenOnly = data.listen_only === true;
  state.noCommands = data.no_commands === true;
  state.ownNodeId = Number.isInteger(data.own_node_id) ? data.own_node_id : null;
  state.serverCpu = data.server_cpu ?? state.serverCpu;
  state.decodeOnDemand = data.decode_on_demand === true;
  state.update = data.update ?? null;
  renderVersionTag();

  const backendCanRunning = data.status === 'running' && !!data.can_interface;

  if (backendCanRunning && !state.canConnected) {
    // Another client connected CAN
    state.canConnected = true;
    setCanError(null);
    showConnectedCanInterface(data.can_interface, data.can_bitrate, data.can_data_bitrate, data.can_fd);
    state.preferredCanInterface = data.can_interface;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(3000);
    saveSettings();
  } else if (!backendCanRunning && state.canConnected) {
    // CAN disconnected (by another client or due to error)
    state.canConnected = false;
    REG_CACHE.clear();
    updateCanConnectButton();
    stopCanStartupDelay();
    stopNodesPolling();
    stopThroughputTimer();
    disconnectWs();
    clearCanSession();
    saveSettings();
    await loadInterfaces();
    startInterfacePolling();

    if (data.last_error) {
      showToast(`CAN disconnected: ${data.last_error}`, 'error');
      setCanError(`Disconnected: ${data.last_error}`);
    }
  } else if (backendCanRunning) {
    // A problem the session goes on with: another node using Cynitor's
    // node-ID, or a database that cannot be written (a full disk).
    const problem = data.last_error
      || (data.history_error ? `History and recordings are not being saved: ${data.history_error}` : null);
    if (problem !== state.canError) setCanError(problem);
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
    // Here too, not only on messages: a bus gone quiet sends none.
    refreshSubjectsTable();
    renderSelectedNodeContent();
  } catch (error) {
    state.latestNodesPayload = { node_count: 0, nodes: {} };
    state.selectedNodeId = null;
    renderNodesTable();
    renderSelectedNodeContent();
  }
  TestNodesView.refreshIndicator();
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

// A backend that stops answering mid-session, or does not answer a page that
// opens with a session saved, is tried again, less and less often, until it
// answers or the user disconnects. The session stays saved meanwhile.
const retryDashboard = () => {
  const retry = state.dashboardRetry || { timer: null, attempts: 0 };
  clearTimeout(retry.timer);
  retry.attempts += 1;
  retry.timer = setTimeout(openDashboard, retryDelayMs(retry.attempts));
  state.dashboardRetry = retry;
  updateDashboardConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();
};

const stopRetryingDashboard = () => {
  clearTimeout(state.dashboardRetry?.timer);
  state.dashboardRetry = null;
};

// The Server button: connects, or ends the session, one waiting to reconnect too.
const connectDashboard = async () => {
  if (state.canConnecting || state.canDisconnecting) return;
  if (state.dashboardConnected || state.dashboardRetry) {
    disconnectAll();
    return;
  }
  await openDashboard();
};

const openDashboard = async () => {
  state.dashboardConnecting = true;
  updateDashboardConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();

  let statusData;
  try {
    statusData = await requestJson('/api/status');
  } catch (error) {
    state.dashboardConnecting = false;
    if ((state.dashboardRetry || state.pendingReconnect) && error.status !== 401) {
      retryDashboard();
      return;
    }
    stopRetryingDashboard();
    updateDashboardConnectButton();
    renderNodesTable();
    renderSelectedNodeContent();
    // A backend that wants a token was reached: its prompt says so.
    if (error.status !== 401) showToast(`Backend unreachable: ${error.message}`, 'error');
    return;
  }

  stopRetryingDashboard();
  state.dashboardConnecting = false;
  state.dashboardConnected = true;
  state.decodeOnDemand = statusData.decode_on_demand === true;
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
    state.allocatorStatus = statusData.allocator ?? null;
    state.listenOnly = statusData.listen_only === true;
    state.noCommands = statusData.no_commands === true;
    setCanError(null);
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
  fetchRawLogs();  // a raw log already running lights the Record tab's dot
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
    clearCanSession();
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
  setCanError(null);
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
    await openDashboard();
  }
};
