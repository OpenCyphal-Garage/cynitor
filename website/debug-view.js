// Debug view — transport-layer ("below the DSDL") diagnostics.
//
// Two sections:
//   1. Transport Diagnostics (Phase 1) — the bus's health in one strip
//      (controller state, error counters, load, frame rates), with protocol
//      params (MTU / CAN-FD), pycyphal frame statistics and the controller's
//      counters folded away beneath it. Polls GET /api/can/transport at 1 Hz
//      while active.
//   2. Frame Monitor — every frame on the bus, error frames included, from a
//      listen-only tap the backend opens while a dashboard captures: it
//      changes nothing Cynitor sends or receives. The frames caught before
//      (for another dashboard) come with the `capture_status` reply; live
//      ones arrive on the WebSocket `can_frame` stream (batched).
const DebugView = (() => {
  const POLL_MS = 1000;
  const MAX_KEPT = 5000; // frames kept for the filter, so a rare frame is still found
  const OVERSCAN = 10;   // rows drawn beyond each edge of the view, so a scroll finds them drawn
  const MAX_IDS = 1000;  // CAN IDs By ID lists: a bus of random IDs must not make it slow
  const IDS_EVERY_MS = 500; // By ID is redrawn this often at most as frames come

  let pollTimer = null;
  let prev = null;        // the previous sample, for rates: {stats, link, at: performance.now()}
  let detailsOpen = false; // the folded diagnostics cards, opened by the user

  // Frame-monitor state
  let captureOn = false;  // are we currently forwarding frames to this client?
  let paused = false;     // freeze the table without unsubscribing
  let filterTests = [];   // one test per filter term, all to pass (parseFilter)
  let rows = [];          // recent frames, newest first
  let shown = [];         // the frames of `rows` the filter lets through: the table's rows
  let shownChanged = 0;   // counts changes to `shown` other than frames coming and going at its ends
  let drawn = { first: 0, key: '' };  // the rows of `shown` in the page now (see draw)
  let pending = [];       // frames that came while paused, newest first: shown on Resume
  let lastSeq = 0;        // frames are numbered as they come, for trimming the table
  let view = 'trace';     // 'trace': every frame; 'ids': a row per CAN ID
  let byId = new Map();   // CAN ID as shown -> {f: its last frame, num: the ID, count, cycle: ms, at: Date.now()}
  let idsShown = 0;       // rows the By ID table shows
  let idsDrawnAt = 0;     // performance.now() of the last By ID redraw
  let idsTimer = null;    // a By ID redraw waiting for its turn
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
            <div class="view-segmented fm-views" role="group" aria-label="Frames shown">
              <button class="view-seg-btn" type="button" data-fm-view="trace" aria-pressed="true">Trace</button>
              <button class="view-seg-btn" type="button" data-fm-view="ids" aria-pressed="false">By ID</button>
            </div>
            <input id="fmFilter" class="fm-filter" type="text" placeholder="Filter: node:42 port:7509 -dir:tx, or any text"
                   aria-label="Filter captured frames"
                   title="node: src: dst: port: kind: prio: dir: tid: len: id: match that field (id: by its first digits); a leading - leaves out what a term matches; other words match the row's text as typed. All must match." />
            <span id="fmCounters" class="fm-counters"></span>
            <span id="fmStatus" class="fm-status" role="status" aria-live="polite"></span>
          </div>
          <div id="fmWrap" class="fm-table-wrap">
            <table id="fmTable" class="fm-table" aria-rowcount="1">
              <colgroup>
                <col class="fm-col-time"><col class="fm-col-delta"><col class="fm-col-dir"><col class="fm-col-id"><col class="fm-col-prio">
                <col class="fm-col-transfer"><col class="fm-col-tid"><col class="fm-col-flags"><col class="fm-col-len">
                <col>
              </colgroup>
              <thead><tr>
                <th>Time</th><th title="Milliseconds since the frame below it, the one shown before">Δ <span class="fm-unit">ms</span></th>
                <th>Dir</th><th>CAN ID</th><th>Prio</th><th>Transfer</th>
                <th title="Transfer-ID">TID</th>
                <th title="Start of transfer, end of transfer, toggle bit; - where clear">Flags</th>
                <th>Len</th><th>Data</th>
              </tr></thead>
              <tbody class="fm-pad" aria-hidden="true"><tr><td colspan="10"></td></tr></tbody>
              <tbody id="fmRows"></tbody>
              <tbody class="fm-pad fm-pad-below" aria-hidden="true"><tr><td colspan="10"></td></tr></tbody>
            </table>
            <table id="fmIdTable" class="fm-table hidden">
              <colgroup>
                <col class="fm-col-id"><col class="fm-col-dir"><col class="fm-col-prio"><col class="fm-col-transfer">
                <col class="fm-col-count"><col class="fm-col-delta"><col class="fm-col-age"><col class="fm-col-len">
                <col>
              </colgroup>
              <thead><tr>
                <th>CAN ID</th><th>Dir</th><th>Prio</th><th>Transfer</th>
                <th title="Frames with this ID since capture started, or since Clear">Count</th>
                <th title="The time between its frames, averaged over the last few">Cycle <span class="fm-unit">ms</span></th>
                <th title="Seconds since its last frame; amber once it has been quiet for three cycles, two seconds at least">Age <span class="fm-unit">s</span></th>
                <th>Len</th><th>Last data</th>
              </tr></thead>
              <tbody id="fmIdRows"></tbody>
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
  // A template parses table rows too, which a div would strip of their cells.
  const patchHtml = (target, html) => {
    const fresh = document.createElement('template');
    fresh.innerHTML = html;
    patchChildren(target, fresh.content);
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

    // A CAN FD frame's 64 data bytes need wider frame tables.
    document.querySelectorAll('.fm-table').forEach((table) => table.classList.toggle('fm-fd', proto.is_fd === true));
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
      renderProblem(problem);
      return;
    }
    shownProblem = null;
    try {
      const data = await requestJson('/api/can/transport');
      if (state.activeView === 'debug') renderDiagnostics(data);
      if (!paused) drawIds();  // the IDs' ages follow the clock
    } catch (err) {
      if (state.activeView === 'debug') renderDiagError(err && err.message);
    }
  };

  // ── Section 2: frame monitor ───────────────────────────────────────────

  // The time from the frame shown before `f` (the row below it), in ms to
  // the µs: with a filter, one subject's period. From `t`, the precise clock.
  const fmtDelta = (f, before) => (before ? ((f.t - before.t) * 1000).toFixed(3) : '');

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

  // Cells both tables draw alike.
  const dirCell = (f) => `<td><span class="fm-dir ${f.dir === 'tx' ? 'fm-tx' : 'fm-rx'}">${f.dir === 'tx' ? 'TX' : 'RX'}</span></td>`;
  // Transport fields only a Cyphal frame has.
  const cyphalCell = (cls, f, value) => `<td class="${cls}">${f.cyphal ? escapeHtml(String(value ?? '')) : ''}</td>`;
  const transferCell = (f) => `<td class="fm-transfer">${f.cyphal
    ? `<span class="fm-kind fm-kind-${escapeHtml(f.kind || 'x')}">${escapeHtml(f.kind || '?')}</span> `
      + `${escapeHtml(String(f.port ?? ''))} · ${escapeHtml(`n${f.src ?? '?'}${f.dst != null ? `→n${f.dst}` : ''}`)}`
    : '<span class="fm-foreign-tag">foreign</span>'}</td>`;
  const rowClass = (f) => `fm-row${f.cyphal ? '' : ' fm-foreign'}`;

  // Row `index` of `shown`; aria-rowindex tells screen readers where it is
  // among all the rows, few of which are drawn.
  const rowHtml = (f, index) => `<tr class="${rowClass(f)}" aria-rowindex="${index + 2}">
      <td class="fm-time">${escapeHtml(fmtTime(f.ts))}</td>
      <td class="fm-delta">${escapeHtml(fmtDelta(f, shown[index + 1]))}</td>
      ${dirCell(f)}
      <td class="fm-id">${escapeHtml(fmtId(f))}</td>
      ${cyphalCell('fm-prio', f, f.priority?.toLowerCase())}
      ${transferCell(f)}
      ${cyphalCell('fm-tid', f, f.transfer_id)}
      ${cyphalCell('fm-flags', f, fmtFlags(f))}
      <td class="fm-len">${escapeHtml(String(f.dlc))}</td>
      <td class="fm-data">${escapeHtml(f.data)}</td>
    </tr>`;

  // A CAN ID gone quiet: none of its frames for three cycles, two seconds at
  // least (the rule by which the dashboard calls a subject silent).
  const quiet = (entry, age) => age > Math.max(2, (3 * (entry.cycle ?? 0)) / 1000);

  // A row of the By ID table: an ID, how often it comes, and its last frame.
  const idRowHtml = (entry) => {
    const f = entry.f;
    const age = (Date.now() - entry.at) / 1000;
    return `<tr class="${rowClass(f)}">
      <td class="fm-id">${escapeHtml(fmtId(f))}</td>
      ${dirCell(f)}
      ${cyphalCell('fm-prio', f, f.priority?.toLowerCase())}
      ${transferCell(f)}
      <td class="fm-count">${entry.count.toLocaleString()}</td>
      <td class="fm-delta">${entry.cycle != null ? entry.cycle.toFixed(1) : ''}</td>
      <td class="fm-age${quiet(entry, age) ? ' fm-quiet' : ''}">${age.toFixed(1)}</td>
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
    if (!empty) return;
    const hasRows = view === 'ids' ? idsShown > 0 : shown.length > 0;
    empty.classList.toggle('hidden', hasRows);
    if (!hasRows) {
      const text = (view === 'ids' ? byId.size : rows.length) ? 'No frames match the filter.'
        : captureOn ? 'Waiting for frames…'
          : startBlockedBy() || 'Capture is off — click "Start capture" to inspect raw frames.';
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

  // The height of a drawn row in px: --fm-row-h (in rem), which the pads
  // above and below the drawn rows count in too.
  const rowHeight = () => parseFloat(getComputedStyle(el('fmTable')).getPropertyValue('--fm-row-h'))
    * parseFloat(getComputedStyle(document.documentElement).fontSize);

  // Only the rows in view, and OVERSCAN more each way, are in the page; the
  // pads above and below stand in for the others. Rows the page has already
  // are left alone, so a selection in them holds while frames come.
  const draw = () => {
    const wrap = el('fmWrap');
    const table = el('fmTable');
    if (!wrap || view !== 'trace') return;
    const height = rowHeight();
    const first = Math.min(shown.length, Math.max(0, Math.floor(wrap.scrollTop / height) - OVERSCAN));
    const last = Math.min(shown.length, Math.ceil((wrap.scrollTop + wrap.clientHeight) / height) + OVERSCAN);
    table.style.setProperty('--fm-above', first);
    table.style.setProperty('--fm-below', Math.max(0, shown.length - last));
    table.setAttribute('aria-rowcount', shown.length + 1);
    // `shown` only gains frames at its top and loses them at its bottom
    // between its other changes, so its first and last frame drawn say
    // which frames lie between.
    const key = first < last ? `${shownChanged}:${shown[first].seq}:${shown[last - 1].seq}` : '';
    const tbody = el('fmRows');
    if (key !== drawn.key) {
      tbody.innerHTML = shown.slice(first, last).map((f, i) => rowHtml(f, first + i)).join('');
    } else if (first !== drawn.first) {  // the same frames, moved down by frames come above them
      [...tbody.rows].forEach((row, i) => row.setAttribute('aria-rowindex', first + i + 2));
    }
    drawn = { first, key };
    updateEmpty();
  };

  let drawQueued = false;
  const queueDraw = () => {
    if (drawQueued) return;
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; draw(); });
  };

  // Every CAN ID's count, cycle and last frame, from frames oldest first.
  // Counted as they come, paused or not, so Resume shows them up to date.
  const countIds = (frames) => {
    const now = Date.now();
    for (const f of frames) {
      const entry = byId.get(fmtId(f));
      if (!entry) {
        if (byId.size < MAX_IDS) byId.set(fmtId(f), { f, num: parseInt(f.id, 16), count: 1, cycle: null, at: now });
        continue;
      }
      const gap = (f.t - entry.f.t) * 1000;
      entry.cycle = entry.cycle == null ? gap : entry.cycle + (gap - entry.cycle) / 8;
      Object.assign(entry, { f, count: entry.count + 1, at: now });
    }
  };

  // The By ID table: the IDs the filter lets through, by CAN ID, patched in
  // place so the rows hold still and a selection in them too.
  const drawIds = () => {
    if (view !== 'ids' || !el('fmIdRows')) return;
    idsDrawnAt = performance.now();
    const entries = [...byId.values()].filter((entry) => matchesFilter(entry.f)).sort((a, b) => a.num - b.num);
    idsShown = entries.length;
    const full = byId.size >= MAX_IDS
      ? `<tr class="fm-row"><td class="fm-ids-full" colspan="9">Only the first ${MAX_IDS.toLocaleString()} CAN IDs seen are listed; Clear starts again.</td></tr>`
      : '';
    patchHtml(el('fmIdRows'), entries.map(idRowHtml).join('') + full);
    updateEmpty();
  };

  // As frames come, By ID is redrawn twice a second at most: a summary gains
  // nothing from more, and each redraw patches every row, whose age moved.
  const drawIdsSoon = () => {
    if (idsTimer) return;
    idsTimer = setTimeout(() => {
      idsTimer = null;
      if (!paused) drawIds();
    }, Math.max(0, idsDrawnAt + IDS_EVERY_MS - performance.now()));
  };

  const drawView = () => (view === 'ids' ? drawIds() : draw());

  const setView = (next) => {
    view = next;
    el('fmTable').classList.toggle('hidden', view !== 'trace');
    el('fmIdTable').classList.toggle('hidden', view !== 'ids');
    document.querySelectorAll('[data-fm-view]').forEach((btn) => {
      btn.setAttribute('aria-pressed', String(btn.dataset.fmView === view));
    });
    el('fmWrap').scrollTop = 0;
    drawn = { first: 0, key: '' };  // the trace's rows are drawn anew when it is shown again
    drawView();
  };

  // `shown` anew from the frames kept, as after the filter, Clear or Resume,
  // with the newest frames in view.
  const showRows = () => {
    shown = rows.filter(matchesFilter);
    shownChanged += 1;
    const wrap = el('fmWrap');
    if (wrap) wrap.scrollTop = 0;
    drawView();
  };

  // Frames as the server sends them (oldest first), numbered, newest first.
  const numbered = (frames) => {
    for (const f of frames) f.seq = ++lastSeq;
    return frames.slice().reverse();
  };

  const appendBatch = (frames) => {
    const wrap = el('fmWrap');
    if (!wrap || !frames || !frames.length) return;
    countIds(frames);
    const newest = numbered(frames);
    if (paused || state.activeView !== 'debug') {  // kept for Resume, or for the tab's return
      pending = newest.concat(pending).slice(0, MAX_KEPT);
      return;
    }
    rows = newest.concat(rows).slice(0, MAX_KEPT);
    const added = newest.filter(matchesFilter);
    // The newest matches among the frames kept, as a filter typed now would show.
    const oldestKept = rows[rows.length - 1].seq;
    shown = added.concat(shown);
    while (shown.length && shown[shown.length - 1].seq < oldestKept) shown.pop();
    // Scrolled down to older frames: they stay in view as frames come above
    // them. The pad above grows first, so the scroll has room to follow.
    if (view === 'ids') {
      drawIdsSoon();
      return;
    }
    if (wrap.scrollTop > 0 && added.length) {
      el('fmTable').style.setProperty('--fm-above', drawn.first + added.length);
      wrap.scrollTop += added.length * rowHeight();
    }
    draw();
  };

  // The frames that came while paused or while another tab was shown.
  const takePending = () => {
    if (!pending.length) return;
    rows = pending.concat(rows).slice(0, MAX_KEPT);
    pending = [];
    showRows();
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
    if (event.stats) { captureStats = event.stats; renderCounters(); }
    setStatus('');
    setToggleLabel();
    // The frames caught before this client subscribed come with the reply
    // (oldest first); the live stream carries on from there.
    if (captureOn && event.frames?.length && rows.length === 0) {
      countIds(event.frames);
      rows = numbered(event.frames);
      showRows();
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
      byId = new Map();
      showRows();
    });
    document.querySelectorAll('[data-fm-view]').forEach((btn) => {
      btn.addEventListener('click', () => setView(btn.dataset.fmView));
    });
    el('fmFilter').addEventListener('input', (e) => {
      filterTests = parseFilter(e.target.value);
      showRows();
    });
    // The rows in view change as the table scrolls and as its room changes.
    el('fmWrap').addEventListener('scroll', queueDraw, { passive: true });
    new ResizeObserver(queueDraw).observe(el('fmWrap'));
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
