// Debug view — transport-layer ("below the DSDL") diagnostics.
//
// Two sections:
//   1. Transport Diagnostics (Phase 1) — read-only snapshot of protocol params
//      (MTU / CAN-FD), pycyphal frame statistics, and controller/bus error
//      state. Polls GET /api/can/transport at 1 Hz while active.
//   2. Frame Monitor (Phase 2) — raw frame/transfer inspection via pycyphal's
//      capture API. OPT-IN and sticky: starting it reconfigures the bus
//      (accept-all filter + forced loopback) and cannot be stopped without a
//      CAN disconnect, so the user starts it explicitly. Live frames arrive on
//      the WebSocket `can_frame` stream (batched); the ring buffer is backfilled
//      from GET /api/can/capture.
const DebugView = (() => {
  const POLL_MS = 1000;
  const MAX_ROWS = 1000; // cap rendered frame rows to bound DOM size

  let pollTimer = null;
  let prevStats = null;   // previous transport statistics sample (for rates)

  // Frame-monitor state
  let captureOn = false;  // are we currently forwarding frames to this client?
  let paused = false;     // freeze the table without unsubscribing
  let filterText = '';    // lowercased substring filter
  let rows = [];          // recent frames, newest first
  let captureStats = null;

  const body = () => el('debugBody');

  // ── Skeleton ──────────────────────────────────────────────────────────

  const renderSkeleton = () => {
    el('debugContainer').innerHTML = `
      <div class="debug-view">
        <div class="debug-header">
          <h2 class="debug-title">Transport Diagnostics</h2>
          <span class="debug-sub">Below-DSDL view of the CAN transport — frame statistics, MTU, controller error state, and raw frame inspection.</span>
        </div>
        <div id="debugBody" class="debug-body"><div class="debug-empty">Loading…</div></div>
        <section class="frame-monitor">
          <div class="fm-bar">
            <button id="fmToggle" class="fm-btn fm-btn-primary" type="button">Start capture</button>
            <button id="fmPause" class="fm-btn" type="button" aria-pressed="false">Pause</button>
            <button id="fmClear" class="fm-btn" type="button">Clear</button>
            <input id="fmFilter" class="fm-filter" type="text" placeholder="Filter: id, node, port, hex…" aria-label="Filter captured frames" />
            <span id="fmCounters" class="fm-counters" aria-live="polite"></span>
            <span id="fmStatus" class="fm-status" role="status" aria-live="polite"></span>
            <span class="fm-note">Capture forces loopback + accept-all filtering and stays on until CAN disconnect.</span>
          </div>
          <div class="fm-table-wrap">
            <table class="fm-table">
              <thead><tr>
                <th>Time</th><th>Dir</th><th>CAN ID</th><th>Len</th><th>Transfer</th><th>Data</th>
              </tr></thead>
              <tbody id="fmRows"></tbody>
            </table>
            <div id="fmEmpty" class="fm-empty"></div>
          </div>
        </section>
      </div>`;
  };

  // ── Section 1: transport diagnostics ──────────────────────────────────

  const stateStatus = (state_) => {
    if (!state_) return null;
    const s = String(state_).toUpperCase();
    if (s === 'ERROR-ACTIVE') return 'ok';
    if (s === 'ERROR-WARNING' || s === 'ERROR-PASSIVE') return 'warn';
    if (s === 'BUS-OFF' || s === 'STOPPED') return 'error';
    return null;
  };

  const fmt = (v) => (v === null || v === undefined ? '—' : v);

  const statRow = (label, value, status) => {
    const cls = status ? ` stat-${status}` : '';
    return `<div class="debug-stat${cls}">
      <span class="debug-stat-label">${escapeHtml(label)}</span>
      <span class="debug-stat-val">${escapeHtml(String(fmt(value)))}</span>
    </div>`;
  };

  const card = (title, rowsHtml) => `
    <section class="debug-card">
      <h3 class="debug-card-title">${escapeHtml(title)}</h3>
      <div class="debug-grid">${rowsHtml.join('')}</div>
    </section>`;

  const rateSuffix = (cur, prev) => {
    if (prev == null || cur == null) return '';
    const d = cur - prev;
    return d < 0 ? '' : `  (+${d}/s)`;
  };

  const renderDiagnostics = (data) => {
    const target = body();
    if (!target) return;

    if (!data || data.connected === false) {
      prevStats = null;
      target.innerHTML = `<div class="debug-empty">CAN not connected — connect a bus to inspect the transport layer.</div>`;
      return;
    }

    const proto = data.protocol || {};
    const stats = data.statistics || {};
    const link = data.link || {};

    const protoCard = card('Transport / MTU', [
      statRow('Interface', data.interface),
      statRow('Mode', proto.is_fd === true ? 'CAN FD' : (proto.is_fd === false ? 'Classic CAN' : null)),
      statRow('MTU (payload bytes)', proto.mtu),
      statRow('Arbitration bitrate', link.bitrate != null ? `${link.bitrate} bit/s` : null),
      statRow('Data bitrate (FD)', link.dbitrate != null ? `${link.dbitrate} bit/s` : null),
      statRow('Transfer-ID modulo', proto.transfer_id_modulo),
      statRow('Max nodes', proto.max_nodes),
      statRow('Bus utilization', data.bus_utilization != null ? `${data.bus_utilization}%` : null),
    ]);

    const effPct = stats.media_acceptance_filtering_efficiency != null
      ? `${Math.round(stats.media_acceptance_filtering_efficiency * 100)}%` : null;
    const statsCard = card('Frame statistics', [
      statRow('Frames in', stats.in_frames != null ? `${stats.in_frames}${rateSuffix(stats.in_frames, prevStats?.in_frames)}` : null),
      statRow('— Cyphal frames', stats.in_frames_cyphal),
      statRow('— Accepted (for us)', stats.in_frames_cyphal_accepted),
      statRow('Frames errored', stats.in_frames_errored, stats.in_frames_errored > 0 ? 'error' : null),
      statRow('Frames out', stats.out_frames != null ? `${stats.out_frames}${rateSuffix(stats.out_frames, prevStats?.out_frames)}` : null),
      statRow('Out timed out', stats.out_frames_timeout, stats.out_frames_timeout > 0 ? 'warn' : null),
      statRow('Filtering efficiency', effPct),
      statRow('Lost loopback', stats.lost_loopback_frames, stats.lost_loopback_frames ? 'warn' : null),
    ]);

    const busCard = card('Controller / bus state', [
      statRow('CAN state', link.state, stateStatus(link.state)),
      statRow('Link operstate', link.operstate),
      statRow('Error counter TX', link.berr_tx, link.berr_tx > 0 ? 'warn' : null),
      statRow('Error counter RX', link.berr_rx, link.berr_rx > 0 ? 'warn' : null),
      statRow('Bus-off events', link.bus_off, link.bus_off > 0 ? 'error' : null),
      statRow('Error-passive events', link.error_passive, link.error_passive > 0 ? 'warn' : null),
      statRow('Error-warning events', link.error_warning, link.error_warning > 0 ? 'warn' : null),
      statRow('Bus errors', link.bus_errors, link.bus_errors > 0 ? 'warn' : null),
      statRow('Arbitration lost', link.arbitration_lost),
      statRow('Auto-restart (ms)', link.restart_ms),
      // Only for adapters Cynitor opens itself (not SocketCAN): sends the
      // adapter refused, e.g. because nothing on the bus acknowledges.
      ...(link.adapter_send_failures != null ? [
        statRow('Adapter send failures', link.adapter_send_failures, link.adapter_send_failures > 0 ? 'warn' : null),
      ] : []),
    ]);

    target.innerHTML = protoCard + statsCard + busCard;
    prevStats = stats;
  };

  const renderDiagError = (message) => {
    const target = body();
    if (target) target.innerHTML = `<div class="debug-error">${escapeHtml(message || 'Failed to load transport diagnostics')}</div>`;
  };

  const poll = async () => {
    if (state.activeView !== 'debug') return;
    try {
      const data = await requestJson('/api/can/transport');
      if (state.activeView === 'debug') renderDiagnostics(data);
    } catch (err) {
      if (state.activeView === 'debug') renderDiagError(err && err.message);
    }
  };

  // ── Section 2: frame monitor ───────────────────────────────────────────

  const fmtTime = (ts) => {
    if (ts == null) return '—';
    const d = new Date(ts * 1000);
    const p = (n, len = 2) => String(n).padStart(len, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  };

  const matchesFilter = (f) => {
    if (!filterText) return true;
    const decoded = f.cyphal
      ? `${f.kind} ${f.port} n${f.src} n${f.dst} tid${f.transfer_id}`
      : 'foreign';
    return `${f.id} ${f.dir} ${f.dlc} ${decoded} ${f.data}`.toLowerCase().includes(filterText);
  };

  const rowHtml = (f) => {
    const dirCls = f.dir === 'tx' ? 'fm-tx' : 'fm-rx';
    let transfer;
    if (f.cyphal) {
      const route = `n${f.src ?? '?'}` + (f.dst != null ? `→n${f.dst}` : '');
      const flags = `${f.start ? 'S' : ''}${f.end ? 'E' : ''}`;
      transfer = `<span class="fm-kind fm-kind-${escapeHtml(f.kind || 'x')}">${escapeHtml(f.kind || '?')}</span> `
        + `${escapeHtml(String(f.port ?? ''))} · ${escapeHtml(route)} · TID${escapeHtml(String(f.transfer_id))}`
        + (flags ? ` · ${escapeHtml(flags)}` : '');
    } else {
      transfer = '<span class="fm-foreign-tag">foreign</span>';
    }
    return `<tr class="fm-row${f.cyphal ? '' : ' fm-foreign'}">
      <td class="fm-time">${escapeHtml(fmtTime(f.ts))}</td>
      <td><span class="fm-dir ${dirCls}">${f.dir === 'tx' ? 'TX' : 'RX'}</span></td>
      <td class="fm-id">${escapeHtml(f.id)}</td>
      <td class="fm-len">${escapeHtml(String(f.dlc))}</td>
      <td class="fm-transfer">${transfer}</td>
      <td class="fm-data">${escapeHtml(f.data)}</td>
    </tr>`;
  };

  const updateEmpty = () => {
    const empty = el('fmEmpty');
    const tbody = el('fmRows');
    if (!empty || !tbody) return;
    const hasRows = tbody.children.length > 0;
    empty.classList.toggle('hidden', hasRows);
    if (!hasRows) {
      empty.textContent = captureOn
        ? 'Waiting for frames…'
        : 'Capture is off — click "Start capture" to inspect raw frames.';
    }
  };

  const renderCounters = () => {
    const node = el('fmCounters');
    if (!node) return;
    const c = captureStats;
    if (!c) { node.textContent = ''; return; }
    let text = `captured ${c.captured} · rx ${c.rx} · tx ${c.tx} · foreign ${c.foreign}`;
    if (c.dropped) text += ` · dropped ${c.dropped}`;
    node.textContent = text;
  };

  const setStatus = (msg) => {
    const node = el('fmStatus');
    if (node) node.textContent = msg || '';
  };

  const setToggleLabel = () => {
    const btn = el('fmToggle');
    if (!btn) return;
    btn.textContent = captureOn ? 'Stop forwarding' : 'Start capture';
    btn.classList.toggle('active', captureOn);
  };

  // Full rebuild from the rows array — used on filter change / clear / backfill.
  const renderTableFromRows = () => {
    const tbody = el('fmRows');
    if (!tbody) return;
    const visible = rows.filter(matchesFilter).slice(0, MAX_ROWS);
    tbody.innerHTML = visible.map(rowHtml).join('');
    updateEmpty();
  };

  const appendBatch = (frames) => {
    const tbody = el('fmRows');
    if (!tbody || !frames || !frames.length) return;
    const newest = frames.slice().reverse(); // server batch is oldest→newest
    rows = newest.concat(rows);
    if (rows.length > MAX_ROWS) rows.length = MAX_ROWS;
    const html = newest.filter(matchesFilter).map(rowHtml).join('');
    if (html) tbody.insertAdjacentHTML('afterbegin', html);
    while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.lastChild);
    updateEmpty();
  };

  const sendWs = (obj) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  };

  const backfill = async () => {
    try {
      const data = await requestJson('/api/can/capture?limit=500');
      if (!data) return;
      if (data.stats) { captureStats = data.stats; renderCounters(); }
      if (data.active && Array.isArray(data.frames) && rows.length === 0) {
        rows = data.frames.slice().reverse();
        renderTableFromRows();
      }
    } catch (err) {
      // Non-fatal: live stream still works without the historical backfill.
      console.warn('Frame snapshot fetch failed:', err);
    }
  };

  const toggleCapture = () => {
    if (captureOn) {
      sendWs({ type: 'capture', enabled: false });
      // captureOn flips on the capture_status reply.
    } else if (!sendWs({ type: 'capture', enabled: true })) {
      setStatus('Dashboard not connected — connect first.');
    } else {
      setStatus('');
    }
  };

  const onCaptureStatus = (event) => {
    if (event.error) {
      captureOn = false;
      setStatus(event.error);
      setToggleLabel();
      updateEmpty();
      return;
    }
    captureOn = !!event.active;
    if (event.stats) { captureStats = event.stats; renderCounters(); }
    setStatus('');
    setToggleLabel();
    if (captureOn) backfill();
    updateEmpty();
  };

  const onFrames = (event) => {
    // A batch can land just after the user leaves the tab; skip rendering then.
    if (state.activeView !== 'debug') return;
    if (event.stats) { captureStats = event.stats; renderCounters(); }
    if (paused) return;
    appendBatch(event.frames);
  };

  const wireControls = () => {
    el('fmToggle').addEventListener('click', toggleCapture);
    el('fmPause').addEventListener('click', () => {
      paused = !paused;
      const btn = el('fmPause');
      btn.textContent = paused ? 'Resume' : 'Pause';
      btn.setAttribute('aria-pressed', String(paused));
      btn.classList.toggle('active', paused);
    });
    el('fmClear').addEventListener('click', () => {
      rows = [];
      const tbody = el('fmRows');
      if (tbody) tbody.innerHTML = '';
      updateEmpty();
    });
    el('fmFilter').addEventListener('input', (e) => {
      filterText = e.target.value.trim().toLowerCase();
      renderTableFromRows();
    });
  };

  // ── Lifecycle ──────────────────────────────────────────────────────────

  const init = () => {
    renderSkeleton();
    wireControls();
    prevStats = null;
    captureOn = false;
    paused = false;
    filterText = '';
    rows = [];
    captureStats = null;
    setToggleLabel();
    updateEmpty();
    poll();
    pollTimer = window.setInterval(poll, POLL_MS);
  };

  const hide = () => {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
    // Stop the server forwarding frames to us (capture itself stays active on
    // the transport — it is sticky until disconnect).
    if (captureOn) sendWs({ type: 'capture', enabled: false });
    captureOn = false;
    prevStats = null;
  };

  return { init, hide, onFrames, onCaptureStatus };
})();
