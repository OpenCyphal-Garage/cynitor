// Replay playback strip: rendered above the main view while a recording
// replay session is active. Owns the play/pause/stop/seek/speed controls,
// the /api/replay/status poll, and the visual sync with state.replay*.
//
// The backend pushes a {type:"replay_ended"} sentinel through the WS when
// replay ends; connection.js handles that and calls hideReplayStrip().

const REPLAY_POLL_MS = 1000;
const REPLAY_SPEEDS = [0.5, 1, 2, 5, 10];

const _formatReplayTime = (s) => {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const total = Math.floor(s);
  const mm = Math.floor(total / 60).toString().padStart(2, '0');
  const ss = (total % 60).toString().padStart(2, '0');
  return `${mm}:${ss}`;
};

const startReplay = async (recordingId, speed = 1.0) => {
  try {
    const status = await requestJson('/api/replay/start', {
      method: 'POST',
      body: JSON.stringify({ recording_id: recordingId, speed }),
    });
    _applyReplayStatus(status);
    showReplayStrip();
    return status;
  } catch (e) {
    const detail = e?.data?.error || e.message;
    showToast(`Replay start failed: ${detail}`, 'error');
    return null;
  }
};

const _replayControl = async (action) => {
  try {
    const status = await requestJson('/api/replay/control', {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
    _applyReplayStatus(status);
  } catch (e) {
    showToast(`Replay ${action} failed: ${e?.data?.error || e.message}`, 'error');
  }
};

const _replaySeek = async (positionS) => {
  try {
    const status = await requestJson('/api/replay/seek', {
      method: 'POST',
      body: JSON.stringify({ position_s: positionS }),
    });
    _applyReplayStatus(status);
  } catch (e) {
    showToast(`Replay seek failed: ${e?.data?.error || e.message}`, 'error');
  }
};

const _replaySetSpeed = async (speed) => {
  try {
    const status = await requestJson('/api/replay/speed', {
      method: 'POST',
      body: JSON.stringify({ speed }),
    });
    _applyReplayStatus(status);
  } catch (e) {
    showToast(`Replay speed failed: ${e?.data?.error || e.message}`, 'error');
  }
};

const _applyReplayStatus = (s) => {
  if (!s) return;
  state.replayActive = !!s.active;
  state.replayRecordingId = s.recording_id ?? state.replayRecordingId;
  state.replayPositionS = Number.isFinite(s.position_s) ? s.position_s : state.replayPositionS;
  state.replayDurationS = Number.isFinite(s.duration_s) ? s.duration_s : state.replayDurationS;
  state.replaySpeed = Number.isFinite(s.speed) ? s.speed : state.replaySpeed;
  state.replayPaused = !!s.paused;
  state.replayEventsEmitted = s.events_emitted ?? state.replayEventsEmitted;
  state.replayTotalEvents = s.total_events ?? state.replayTotalEvents;
  syncReplayStrip();
};

const showReplayStrip = () => {
  const strip = el('replayStrip');
  if (!strip) return;
  strip.classList.remove('hidden');
  _ensureStripBuilt(strip);
  syncReplayStrip();
  _startReplayPoll();
};

const hideReplayStrip = () => {
  _stopReplayPoll();
  const strip = el('replayStrip');
  if (strip) strip.classList.add('hidden');
  // Reset replay state so the rest of the UI can read a clean baseline.
  state.replayActive = false;
  state.replayRecordingId = null;
  state.replayPositionS = 0;
  state.replayDurationS = 0;
  state.replaySpeed = 1.0;
  state.replayPaused = false;
  state.replayEventsEmitted = 0;
  state.replayTotalEvents = 0;
};

const _ensureStripBuilt = (strip) => {
  if (strip.dataset.built) return;
  strip.dataset.built = '1';
  strip.innerHTML = `
    <span class="replay-strip-icon" aria-hidden="true">▶</span>
    <span class="replay-strip-label" id="replayStripLabel">Replay</span>
    <button class="replay-strip-btn" id="replayPlayPauseBtn" aria-label="Pause replay">Pause</button>
    <button class="replay-strip-btn replay-strip-btn-stop" id="replayStopBtn" aria-label="Stop replay">Stop</button>
    <input type="range" class="replay-strip-seek" id="replaySeek" min="0" max="100" step="0.1" value="0" aria-label="Replay position" />
    <span class="replay-strip-time" id="replayTime" aria-live="polite">00:00 / 00:00</span>
    <span class="replay-strip-speed-group" role="group" aria-label="Replay speed">
      ${REPLAY_SPEEDS.map(v => `<button class="replay-strip-speed-btn" data-speed="${v}">${v}×</button>`).join('')}
    </span>
    <span class="replay-strip-events" id="replayEvents" aria-live="polite">0 / 0</span>
  `;

  el('replayPlayPauseBtn').addEventListener('click', () => {
    if (state.replayPaused) _replayControl('resume');
    else _replayControl('pause');
  });
  el('replayStopBtn').addEventListener('click', () => {
    _replayControl('stop');
  });
  el('replaySeek').addEventListener('change', (e) => {
    const target = Number(e.target.value);
    if (Number.isFinite(target)) _replaySeek(target);
  });
  strip.querySelectorAll('.replay-strip-speed-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const speed = Number(btn.dataset.speed);
      if (Number.isFinite(speed) && speed > 0) _replaySetSpeed(speed);
    });
  });
};

const syncReplayStrip = () => {
  const strip = el('replayStrip');
  if (!strip || strip.classList.contains('hidden')) return;
  const labelEl = el('replayStripLabel');
  const pauseBtn = el('replayPlayPauseBtn');
  const seek = el('replaySeek');
  const timeEl = el('replayTime');
  const events = el('replayEvents');

  if (labelEl) {
    const rec = state.recordings.find(r => r.id === state.replayRecordingId);
    const name = rec?.name || `Recording #${state.replayRecordingId ?? ''}`;
    labelEl.textContent = `Replay · ${name}`;
  }
  if (pauseBtn) {
    pauseBtn.textContent = state.replayPaused ? 'Play' : 'Pause';
    pauseBtn.setAttribute('aria-label', state.replayPaused ? 'Resume replay' : 'Pause replay');
  }
  if (seek) {
    seek.max = String(Math.max(0.1, state.replayDurationS || 0.1));
    if (document.activeElement !== seek) seek.value = String(state.replayPositionS || 0);
  }
  if (timeEl) {
    timeEl.textContent = `${_formatReplayTime(state.replayPositionS)} / ${_formatReplayTime(state.replayDurationS)}`;
  }
  if (events) {
    events.textContent = `${state.replayEventsEmitted ?? 0} / ${state.replayTotalEvents ?? 0}`;
  }
  strip.querySelectorAll('.replay-strip-speed-btn').forEach(btn => {
    const v = Number(btn.dataset.speed);
    btn.classList.toggle('active', Math.abs(v - state.replaySpeed) < 0.01);
  });
};

const _pollReplayStatus = async () => {
  if (!state.dashboardConnected) return;
  try {
    const s = await requestJson('/api/replay/status');
    if (!s || s.active === false) {
      hideReplayStrip();
      return;
    }
    _applyReplayStatus(s);
  } catch (_) { /* transient — try next tick */ }
};

const _startReplayPoll = () => {
  _stopReplayPoll();
  state.replayStatusTimer = setInterval(_pollReplayStatus, REPLAY_POLL_MS);
};

const _stopReplayPoll = () => {
  if (state.replayStatusTimer) {
    clearInterval(state.replayStatusTimer);
    state.replayStatusTimer = null;
  }
};
