// Debug view — transport-layer ("below the DSDL") diagnostics.
//
// Two sections:
//   1. Transport Diagnostics (Phase 1) — the bus's health in one strip
//      (controller state, error counters, load, frame rates), with protocol
//      params (MTU / CAN-FD), pycyphal frame statistics and the controller's
//      counters folded away beneath it. Polls GET /api/can/transport at 1 Hz
//      while active.
//   2. Frame Monitor (Phase 2) — raw frame/transfer inspection via pycyphal's
//      capture API. OPT-IN and sticky: starting it reconfigures the bus
//      (accept-all filter + forced loopback) and cannot be stopped without a
//      CAN disconnect, so the user starts it explicitly. The frames caught
//      before come with the `capture_status` reply; live ones arrive on the
//      WebSocket `can_frame` stream (batched).
const DebugView = (() => {
  const POLL_MS = 1000;
  const MAX_ROWS = 1000; // cap rendered frame rows to bound DOM size
  const MAX_KEPT = 5000; // frames kept for the filter, so a rare frame is still found

  let pollTimer = null;
  let prev = null;        // the previous sample, for rates: {stats, link, at: performance.now()}
  let detailsOpen = false; // the folded diagnostics cards, opened by the user

  // Frame-monitor state
  let captureOn = false;  // are we currently forwarding frames to this client?
  let paused = false;     // freeze the table without unsubscribing
  let filterTests = [];   // one test per filter term, all to pass (parseFilter)
  let rows = [];          // recent frames, newest first
  let pending = [];       // frames that came while paused, newest first: shown on Resume
  let lastSeq = 0;        // frames are numbered as they come, for trimming the table
  let captureStats = null;
  let busCapturing = false; // the backend's capture is on, for whichever dashboard started it

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
            <input id="fmFilter" class="fm-filter" type="text" placeholder="Filter: node:42 port:7509 -dir:tx, or any text"
                   aria-label="Filter captured frames"
                   title="Every word must match. node: src: dst: port: kind: prio: dir: tid: len: id: match that field (id: by its first digits); a leading - leaves out what the rest matches; any other word matches the row's text." />
            <span id="fmCounters" class="fm-counters"></span>
            <span id="fmStatus" class="fm-status" role="status" aria-live="polite"></span>
            <span class="fm-note">Capture forces loopback + accept-all filtering and stays on until CAN disconnect.</span>
          </div>
          <div class="fm-table-wrap">
            <table class="fm-table">
              <thead><tr>
                <th>Time</th><th>Dir</th><th>CAN ID</th><th>Prio</th><th>Transfer</th>
                <th title="Transfer-ID">TID</th>
                <th title="Start of transfer, end of transfer, toggle bit; - where clear">Flags</th>
                <th>Len</th><th>Data</th>
              </tr></thead>
              <tbody id="fmRows"></tbody>
            </table>
            <div id="fmEmpty" class="fm-empty"></div>
          </div>
        </section>
      </div>`;
  };

  // ── Section 1: transport diagnostics ──────────────────────────────────

  // Colour for a controller in trouble; ERROR-ACTIVE, the normal state, has none.
  const stateStatus = (state_) => {
    if (!state_) return null;
    const s = String(state_).toUpperCase();
    if (s === 'ERROR-WARNING' || s === 'ERROR-PASSIVE') return 'warn';
    if (s === 'BUS-OFF' || s === 'STOPPED') return 'error';
    return null;
  };

  // A row for what the interface reports, and none for what it does not.
  const statRow = (label, value, status) => {
    if (value === null || value === undefined) return '';
    const cls = status ? ` stat-${status}` : '';
    return `<div class="debug-stat${cls}">
      <span class="debug-stat-label">${escapeHtml(label)}</span>
      <span class="debug-stat-val">${escapeHtml(String(value))}</span>
    </div>`;
  };

  const card = (title, rowsHtml) => `
    <section class="debug-card">
      <h3 class="debug-card-title">${escapeHtml(title)}</h3>
      <div class="debug-grid">${rowsHtml.join('') || '<div class="debug-none">Not reported by this interface.</div>'}</div>
    </section>`;

  // The counter's growth per second since the previous sample, which a
  // late poll may have taken more than a second ago; null while unknown.
  const perSecond = (cur, before, seconds) => {
    if (before == null || cur == null || !(seconds > 0) || cur < before) return null;
    return Math.round((cur - before) / seconds);
  };

  // A health figure in the strip; none for what the interface does not report.
  const tile = (label, value, { note = '', status = null } = {}) => {
    if (value === null || value === undefined) return '';
    const cls = status ? ` debug-tile-${status}` : '';
    return `<div class="debug-tile${cls}">
      <span class="debug-tile-label">${escapeHtml(label)}</span>
      <span class="debug-tile-val">${escapeHtml(String(value))}</span>
      ${note ? `<span class="debug-tile-note">${escapeHtml(note)}</span>` : ''}
    </div>`;
  };

  // What to look at first when the bus misbehaves: the controller's state
  // and error counters, errors as they happen, then how busy the bus is.
  const healthTiles = (data, rate) => {
    const proto = data.protocol || {};
    const stats = data.statistics || {};
    const link = data.link || {};
    const worst = Math.max(link.berr_tx ?? 0, link.berr_rx ?? 0);
    // An error count, coloured while it grows.
    const errors = (label, value, before) => {
      const growth = rate(value, before);
      return tile(label, value?.toLocaleString(), {
        note: growth != null ? `+${growth.toLocaleString()}/s` : '', status: growth > 0 ? 'warn' : null,
      });
    };
    // A frame rate, with the total since the interface came up.
    const frames = (label, value, before) => {
      if (value == null) return '';
      const perS = rate(value, before);
      return tile(label, perS != null ? `${perS.toLocaleString()}/s` : '…', { note: `${value.toLocaleString()} total` });
    };
    return [
      tile('Interface', data.interface, {
        note: formatCanRates(link.bitrate, link.dbitrate, proto.is_fd) || (proto.is_fd === false ? 'Classic CAN' : ''),
      }),
      tile('Controller', link.state, { status: stateStatus(link.state) }),
      tile('Error counters', link.berr_tx != null || link.berr_rx != null
        ? `${link.berr_tx ?? '?'} / ${link.berr_rx ?? '?'}` : null,
      { note: 'TX / RX', status: worst >= 128 ? 'error' : (worst > 0 ? 'warn' : null) }),
      tile('Bus-off', link.bus_off, { status: link.bus_off > 0 ? 'error' : null }),
      errors('Bus errors', link.bus_errors, prev?.link.bus_errors),
      errors('Error frames', link.adapter_error_frames, prev?.link.adapter_error_frames),
      errors('Send failures', link.adapter_send_failures, prev?.link.adapter_send_failures),
      tile('Bus load', data.bus_utilization != null ? `${data.bus_utilization}%` : null),
      frames('Frames in', stats.in_frames, prev?.stats.in_frames),
      frames('Frames out', stats.out_frames, prev?.stats.out_frames),
    ].join('');
  };

  // The strip, and the cards folded beneath it: drawn once, then patched,
  // so the fold stays as the user left it.
  const renderFrame = (target) => {
    target.innerHTML = `
      <div id="debugHealth" class="debug-health"></div>
      <details id="debugDetails" class="debug-details"${detailsOpen ? ' open' : ''}>
        <summary class="debug-details-summary">Details
          <span class="debug-details-hint">MTU, frame statistics, controller and adapter counters</span></summary>
        <div id="debugCards" class="debug-cards"></div>
      </details>`;
    el('debugDetails').addEventListener('toggle', (e) => { detailsOpen = e.target.open; });
  };

  // In place, so a value being selected or read out is not redrawn under it.
  const patchHtml = (target, html) => {
    const fresh = document.createElement('div');
    fresh.innerHTML = html;
    patchChildren(target, fresh);
  };

  const renderDiagnostics = (data) => {
    const target = body();
    if (!target) return;

    if (!data || data.connected === false) {
      prev = null;
      target.innerHTML = `<div class="debug-empty">CAN not connected — connect a bus to inspect the transport layer.</div>`;
      return;
    }

    const proto = data.protocol || {};
    const stats = data.statistics || {};
    const link = data.link || {};
    const now = performance.now();
    const seconds = prev ? (now - prev.at) / 1000 : 0;
    const rate = (value, before) => perSecond(value, before, seconds);
    // A count, with its growth per second since the previous sample.
    const counted = (value, before) => {
      const growth = rate(value, before);
      return growth != null ? `${value}  (+${growth}/s)` : value;
    };

    const protoCard = card('Transport / MTU', [
      statRow('Interface', data.interface),
      statRow('Mode', proto.is_fd === true ? 'CAN FD' : (proto.is_fd === false ? 'Classic CAN' : null)),
      statRow('MTU (payload bytes)', proto.mtu),
      statRow('Arbitration bitrate', link.bitrate != null ? formatBitrate(link.bitrate) : null),
      statRow('Data bitrate (FD)', link.dbitrate != null ? formatBitrate(link.dbitrate) : null),
      statRow('Transfer-ID modulo', proto.transfer_id_modulo),
      statRow('Max nodes', proto.max_nodes),
      statRow('Bus utilization', data.bus_utilization != null ? `${data.bus_utilization}%` : null),
    ]);

    const effPct = stats.media_acceptance_filtering_efficiency != null
      ? `${Math.round(stats.media_acceptance_filtering_efficiency * 100)}%` : null;
    const statsCard = card('Frame statistics', [
      statRow('Frames in', counted(stats.in_frames, prev?.stats.in_frames)),
      statRow('— Cyphal frames', stats.in_frames_cyphal),
      statRow('— Accepted (for us)', stats.in_frames_cyphal_accepted),
      statRow('Frames errored', stats.in_frames_errored, stats.in_frames_errored > 0 ? 'error' : null),
      statRow('Frames out', counted(stats.out_frames, prev?.stats.out_frames)),
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
      statRow('Controller restarts', link.restarts, link.restarts > 0 ? 'warn' : null),
      statRow('Auto-restart (ms)', link.restart_ms),
      // Only for adapters Cynitor opens itself (not SocketCAN): every frame
      // the adapter passed, before any filter, so the bus's own traffic.
      statRow('Adapter frames in', counted(link.adapter_frames_in, prev?.link.adapter_frames_in)),
      statRow('Adapter frames out', counted(link.adapter_frames_out, prev?.link.adapter_frames_out)),
      // Sends the adapter refused, e.g. because nothing on the bus acknowledges.
      statRow('Adapter send failures', link.adapter_send_failures, link.adapter_send_failures > 0 ? 'warn' : null),
      // Error frames the adapter's driver reports; not every driver does.
      statRow('Adapter error frames', link.adapter_error_frames, link.adapter_error_frames > 0 ? 'warn' : null),
    ]);

    if (!el('debugHealth')) renderFrame(target);
    target.querySelector('.debug-stale-note')?.remove();
    target.classList.remove('debug-stale');
    patchHtml(el('debugHealth'), healthTiles(data, rate));
    patchHtml(el('debugCards'), protoCard + statsCard + busCard);
    prev = { stats, link, at: now };
  };

  // A failed poll keeps the values last shown, marked stale; with none
  // shown yet, it says what failed.
  const renderDiagError = (message) => {
    const target = body();
    if (!target) return;
    if (!el('debugHealth')) {
      target.innerHTML = `<div class="debug-error">${escapeHtml(message || 'Failed to load transport diagnostics')}</div>`;
      return;
    }
    target.classList.add('debug-stale');
    let note = target.querySelector('.debug-stale-note');
    if (!note) {
      note = document.createElement('div');
      note.className = 'debug-stale-note';
      target.prepend(note);
    }
    note.textContent = `Last update failed: ${message || 'no answer'}. Retrying…`;
  };

  // Why the transport cannot be read, in the other tabs' words. Drawn only
  // when it changes: a redraw on every poll would restart its spinner.
  let shownProblem = null;
  const renderProblem = (html) => {
    const target = body();
    if (target && html !== shownProblem) target.innerHTML = html;
    shownProblem = html;
  };

  const poll = async () => {
    if (state.activeView !== 'debug') return;
    // Not connected: nothing to ask. The backend may be down, or want a
    // token, and every refused poll would open the token prompt again.
    const problem = connectionPlaceholder('inspect the CAN transport');
    if (problem) {
      prev = null;
      busCapturing = false;
      renderProblem(problem);
      return;
    }
    shownProblem = null;
    try {
      const data = await requestJson('/api/can/transport');
      busCapturing = data?.capture_active === true;
      updateEmpty();
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

  // A standard (11-bit) ID in three hex digits, as candump writes it; an extended one in eight.
  const fmtId = (f) => (f.ext === false ? `0x${parseInt(f.id, 16).toString(16).toUpperCase().padStart(3, '0')}` : f.id);

  // The tail byte's start-of-transfer, end-of-transfer and toggle bits, '-' where clear.
  const fmtFlags = (f) => `${f.start ? 'S' : '-'}${f.end ? 'E' : '-'}${f.toggle ? 'T' : '-'}`;

  // What the free-text filter looks through: the row as shown.
  const frameText = (f) => {
    const decoded = f.cyphal
      ? `${f.kind} ${f.port} n${f.src ?? '?'}${f.dst != null ? ` n${f.dst}` : ''} tid${f.transfer_id} ${f.priority}`
      : 'foreign';
    return `${fmtId(f)} ${f.dir} ${f.dlc} ${decoded} ${f.data}`.toLowerCase();
  };

  // Filter terms naming a field: `node:42` is a frame from or to node 42.
  const FIELD_TERMS = {
    node: (f, v) => f.cyphal === true && (f.src === Number(v) || f.dst === Number(v)),
    src: (f, v) => f.cyphal === true && f.src === Number(v),
    dst: (f, v) => f.cyphal === true && f.dst === Number(v),
    port: (f, v) => f.cyphal === true && f.port === Number(v),
    tid: (f, v) => f.cyphal === true && f.transfer_id === Number(v),
    kind: (f, v) => f.kind === v,
    prio: (f, v) => String(f.priority).toLowerCase() === v,
    dir: (f, v) => f.dir === v,
    len: (f, v) => f.dlc === Number(v),
    id: (f, v) => fmtId(f).toLowerCase().slice(2).startsWith(v.replace(/^0x/, '')),
  };

  // All must match: each `key:value` its field; the other words the row's
  // text, together as typed (so `01 02` finds those bytes in a row). A
  // leading `-` or `!` leaves out what the rest of the word matches. A term
  // still being typed (`node:`, a lone `-`) is left out until it has a value.
  const parseFilter = (text) => {
    const tests = [];
    const words = [];
    for (const word of text.toLowerCase().split(/\s+/)) {
      const negate = word[0] === '-' || word[0] === '!';
      const term = negate ? word.slice(1) : word;
      const [, key, value] = term.match(/^(\w+):(.*)$/) || [];
      if (FIELD_TERMS[key]) {
        if (value) tests.push((f) => FIELD_TERMS[key](f, value) !== negate);
      } else if (negate) {
        if (term) tests.push((f) => !frameText(f).includes(term));
      } else if (term) {
        words.push(term);
      }
    }
    const typed = words.join(' ');
    if (typed) tests.push((f) => frameText(f).includes(typed));
    return tests;
  };

  const matchesFilter = (f) => filterTests.every((test) => test(f));

  const rowHtml = (f) => {
    const dirCls = f.dir === 'tx' ? 'fm-tx' : 'fm-rx';
    const transfer = f.cyphal
      ? `<span class="fm-kind fm-kind-${escapeHtml(f.kind || 'x')}">${escapeHtml(f.kind || '?')}</span> `
        + `${escapeHtml(String(f.port ?? ''))} · ${escapeHtml(`n${f.src ?? '?'}${f.dst != null ? `→n${f.dst}` : ''}`)}`
      : '<span class="fm-foreign-tag">foreign</span>';
    // Transport fields only a Cyphal frame has.
    const cyphal = (value) => (f.cyphal ? escapeHtml(String(value ?? '')) : '');
    return `<tr class="fm-row${f.cyphal ? '' : ' fm-foreign'}" data-seq="${f.seq}">
      <td class="fm-time">${escapeHtml(fmtTime(f.ts))}</td>
      <td><span class="fm-dir ${dirCls}">${f.dir === 'tx' ? 'TX' : 'RX'}</span></td>
      <td class="fm-id">${escapeHtml(fmtId(f))}</td>
      <td class="fm-prio">${cyphal(f.priority?.toLowerCase())}</td>
      <td class="fm-transfer">${transfer}</td>
      <td class="fm-tid">${cyphal(f.transfer_id)}</td>
      <td class="fm-flags">${cyphal(fmtFlags(f))}</td>
      <td class="fm-len">${escapeHtml(String(f.dlc))}</td>
      <td class="fm-data">${escapeHtml(f.data)}</td>
    </tr>`;
  };

  // Why Start cannot work now, or '' when it can (in the Record tab's words).
  const startBlockedBy = () => {
    if (!state.dashboardConnected) return 'Connect to the backend to capture frames.';
    if (!state.canConnected) return 'Connect a CAN interface to capture frames.';
    if (state.ws?.readyState !== WebSocket.OPEN) return 'Waiting for the live connection to the backend…';
    return '';
  };

  const updateEmpty = () => {
    const empty = el('fmEmpty');
    const tbody = el('fmRows');
    if (!empty || !tbody) return;
    const hasRows = tbody.children.length > 0;
    empty.classList.toggle('hidden', hasRows);
    if (!hasRows) {
      const text = rows.length ? 'No frames match the filter.'
        : captureOn ? 'Waiting for frames…'
          : startBlockedBy() || (busCapturing
            ? 'Capture runs on this bus until CAN disconnects: "Start capture" shows its frames.'
            : 'Capture is off — click "Start capture" to inspect raw frames.');
      if (empty.textContent !== text) empty.textContent = text;  // runs every second
    }
  };

  const renderCounters = () => {
    const node = el('fmCounters');
    if (!node) return;
    const c = captureStats;
    if (!c) { node.textContent = ''; return; }
    node.textContent = `captured ${c.captured} · rx ${c.rx} · tx ${c.tx} · foreign ${c.foreign}`;
    if (c.dropped) {  // frames lost before they reached this table
      const lost = document.createElement('span');
      lost.className = 'fm-dropped';
      lost.textContent = `dropped ${c.dropped}`;
      node.append(' · ', lost);
    }
  };

  const setStatus = (msg) => {
    const node = el('fmStatus');
    if (node) node.textContent = msg || '';
  };

  // Start is off, saying why, while there is no bus to capture from.
  const setToggleLabel = () => {
    const btn = el('fmToggle');
    if (!btn) return;
    const why = captureOn ? '' : startBlockedBy();
    btn.textContent = captureOn ? 'Stop' : 'Start capture';
    btn.classList.toggle('active', captureOn);
    btn.disabled = Boolean(why);
    btn.title = why;
  };

  // On every connection change (updateSemaphores), as the Record tab's Start.
  const renderControls = () => {
    setToggleLabel();
    updateEmpty();
  };

  // Full rebuild from the rows array — used on filter change / clear / backfill.
  const renderTableFromRows = () => {
    const tbody = el('fmRows');
    if (!tbody) return;
    const visible = rows.filter(matchesFilter).slice(0, MAX_ROWS);
    tbody.innerHTML = visible.map(rowHtml).join('');
    updateEmpty();
  };

  // Frames as the server sends them (oldest first), numbered, newest first.
  const numbered = (frames) => {
    for (const f of frames) f.seq = ++lastSeq;
    return frames.slice().reverse();
  };

  const appendBatch = (frames) => {
    const tbody = el('fmRows');
    if (!tbody || !frames || !frames.length) return;
    const newest = numbered(frames);
    if (paused || state.activeView !== 'debug') {  // kept for Resume, or for the tab's return
      pending = newest.concat(pending).slice(0, MAX_KEPT);
      return;
    }
    rows = newest.concat(rows).slice(0, MAX_KEPT);
    const html = newest.filter(matchesFilter).slice(0, MAX_ROWS).map(rowHtml).join('');
    if (html) tbody.insertAdjacentHTML('afterbegin', html);
    // What a rebuild would show: the newest matches among the frames kept.
    const oldestKept = rows[rows.length - 1].seq;
    while (tbody.lastChild && (tbody.children.length > MAX_ROWS
        || Number(tbody.lastChild.dataset.seq) < oldestKept)) {
      tbody.lastChild.remove();
    }
    updateEmpty();
  };

  // The frames that came while paused or while another tab was shown.
  const takePending = () => {
    if (!pending.length) return;
    rows = pending.concat(rows).slice(0, MAX_KEPT);
    pending = [];
    renderTableFromRows();
  };

  const sendWs = (obj) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  };

  const toggleCapture = () => {
    // captureOn flips on the capture_status reply.
    if (!sendWs({ type: 'capture', enabled: !captureOn })) {
      renderControls();  // the socket has just gone: Start says why it is off
      return;
    }
    setStatus('');
  };

  const onCaptureStatus = (event) => {
    if (event.error) {
      captureOn = false;
      setStatus(event.error);
      setToggleLabel();
      updateEmpty();
      return;
    }
    // `active` is how backends from before `forwarding` and `capturing` said it.
    captureOn = Boolean(event.forwarding ?? event.active);
    busCapturing = event.capturing ?? (busCapturing || captureOn);  // on until CAN disconnects
    if (event.stats) { captureStats = event.stats; renderCounters(); }
    setStatus('');
    setToggleLabel();
    // The frames caught before this client subscribed come with the reply
    // (oldest first); the live stream carries on from there.
    if (captureOn && event.frames?.length && rows.length === 0) {
      rows = numbered(event.frames);
      renderTableFromRows();
    }
    updateEmpty();
  };

  // The socket closed (CAN or the dashboard disconnected, the backend went
  // away): the backend forgets what this client asked for, so capture stops.
  const onSocketClosed = () => {
    if (!captureOn) return;
    captureOn = false;
    setStatus('Capture stopped: the connection to the backend closed.');
    setToggleLabel();
    updateEmpty();
  };

  const onFrames = (event) => {
    if (event.stats) { captureStats = event.stats; renderCounters(); }
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
      if (!paused) takePending();
    });
    el('fmClear').addEventListener('click', () => {
      rows = [];
      pending = [];
      const tbody = el('fmRows');
      if (tbody) tbody.innerHTML = '';
      updateEmpty();
    });
    el('fmFilter').addEventListener('input', (e) => {
      filterTests = parseFilter(e.target.value);
      renderTableFromRows();
    });
  };

  // ── Lifecycle ──────────────────────────────────────────────────────────

  const init = () => {
    // Every dashboard connect calls this again: one poller, not one per call.
    if (pollTimer) window.clearInterval(pollTimer);
    // Drawn once: the frame table keeps its frames, filter and pause across
    // tab switches, and a capture goes on while another tab is shown.
    if (!el('fmRows')) {
      renderSkeleton();
      wireControls();
    }
    prev = null;
    if (!paused) takePending();
    renderControls();
    poll();
    pollTimer = window.setInterval(poll, POLL_MS);
  };

  const hide = () => {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
    prev = null;
  };

  return { init, hide, onFrames, onCaptureStatus, onSocketClosed, renderControls };
})();
