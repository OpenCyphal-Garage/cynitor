// Boot file: wires DOM event listeners, kicks off the lifecycle, and runs
// the frontend-server heartbeat. Loaded last; everything it calls is
// declared in the earlier scripts (state.js, cache.js, detail-panel.js,
// nodes-table.js, connection.js).

const bindTabs = () => {
  document.querySelectorAll('.detail-tab').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedDetailTab = button.dataset.tab;
      saveSettings();
      renderSelectedNodeContent();
    });
  });
};

const bind = () => {
  el('connectDashboardBtn').addEventListener('click', connectDashboard);
  el('connectCanBtn').addEventListener('click', connectCan);
  el('interfacesSelect').addEventListener('change', () => {
    state.preferredCanInterface = el('interfacesSelect').value;
    saveSettings();
  });
  el('sidebarCollapseBtn').addEventListener('click', () => {
    const sidebar = document.querySelector('.sidebar');
    sidebar.classList.toggle('collapsed');
    state.sidebarCollapsed = sidebar.classList.contains('collapsed');
    saveSettings();
  });

  // Detail panel resize / collapse
  const detailPanel = el('detailPanel');
  const resizeHandle = el('detailResizeHandle');
  const mainArea = detailPanel.parentElement;

  const applyDetailHeight = () => {
    if (state.detailPanelCollapsed) {
      detailPanel.classList.add('collapsed');
    } else if (state.detailPanelHeight != null) {
      detailPanel.style.height = state.detailPanelHeight + 'px';
    }
  };
  applyDetailHeight();

  const collapseBtn = el('detailCollapseBtn');
  collapseBtn.addEventListener('mousedown', (e) => e.stopPropagation());

  const updateCollapseChevron = () => {
    collapseBtn.classList.toggle('pointing-up', state.detailPanelCollapsed);
  };
  updateCollapseChevron();

  collapseBtn.addEventListener('click', () => {
    state.detailPanelCollapsed = !state.detailPanelCollapsed;
    if (state.detailPanelCollapsed) {
      detailPanel.classList.add('collapsed');
      detailPanel.style.height = '';
    } else {
      detailPanel.classList.remove('collapsed');
      detailPanel.style.height = state.detailPanelHeight ? state.detailPanelHeight + 'px' : '33.3%';
    }
    updateCollapseChevron();
    saveSettings();
  });

  (() => {
    let startY, startH;

    resizeHandle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      startY = e.clientY;
      startH = detailPanel.getBoundingClientRect().height;
      detailPanel.classList.add('no-transition');
      resizeHandle.classList.add('dragging');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      const dy = startY - e.clientY;
      const totalH = mainArea.getBoundingClientRect().height;
      const newH = Math.max(0, Math.min(totalH * 0.9, startH + dy));
      detailPanel.classList.remove('collapsed');
      detailPanel.style.height = newH + 'px';
    };

    const onUp = (e) => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      detailPanel.classList.remove('no-transition');
      resizeHandle.classList.remove('dragging');

      const totalH = mainArea.getBoundingClientRect().height;
      const currentH = detailPanel.getBoundingClientRect().height;

      if (currentH < totalH / 10) {
        state.detailPanelCollapsed = true;
        state.detailPanelHeight = null;
        detailPanel.classList.add('collapsed');
        detailPanel.style.height = '';
      } else {
        state.detailPanelCollapsed = false;
        state.detailPanelHeight = currentH;
        detailPanel.style.height = currentH + 'px';
      }
      updateCollapseChevron();
      saveSettings();
    };
  })();

  resizeHandle.addEventListener('dblclick', () => {
    state.detailPanelCollapsed = false;
    state.detailPanelHeight = null;
    detailPanel.classList.remove('collapsed');
    detailPanel.style.height = '33.3%';
    updateCollapseChevron();
    saveSettings();
  });

  el('themeToggle').addEventListener('click', () => {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') === 'dark';
    if (isDark) {
      html.removeAttribute('data-theme');
    } else {
      html.setAttribute('data-theme', 'dark');
    }
    el('themeLabel').textContent = isDark ? 'Light' : 'Dark';
    saveSettings();
  });
  el('apiBase').addEventListener('change', saveSettings);
  el('hiddenNodesChip').addEventListener('click', renderHiddenPopover);
  document.addEventListener('click', (e) => {
    const popover = el('hiddenNodesPopover');
    if (popover && !popover.classList.contains('hidden')
      && !popover.contains(e.target) && e.target !== el('hiddenNodesChip')) {
      popover.classList.add('hidden');
    }
  });

  const selectSubjectCard = (card) => {
    const sid = Number(card.dataset.subject);
    state.selectedPlotSubject = sid;
    el('selectedNodeContent').querySelectorAll('.subject-card').forEach((c) => {
      c.classList.toggle('selected', Number(c.dataset.subject) === sid);
    });
    startPlotAnim();
  };

  el('selectedNodeContent').addEventListener('click', (e) => {
    const card = e.target.closest('.subject-card');
    if (!card) return;
    selectSubjectCard(card);
  });

  el('selectedNodeContent').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const card = e.target.closest('.subject-card');
    if (!card) return;
    e.preventDefault();
    selectSubjectCard(card);
  });

  bindTabs();
  initNodesTable();
};

loadSettings();
bind();
updateDashboardConnectButton();
updateCanConnectButton();
renderSelectedNodeContent();
updateSemaphores();
updateHiddenChip();

// Restore previous connection state
(async () => {
  if (state.pendingReconnect) {
    await connectDashboard();
    delete state.pendingReconnect;
  }
  updateSemaphores();
})();

// ── Frontend server heartbeat ──
(() => {
  const HEARTBEAT_INTERVAL = 5000;
  const FAIL_THRESHOLD = 2;
  const overlay = el('serverDownOverlay');
  let serverDown = false;
  let consecutiveFailures = 0;

  const tearDown = () => disconnectAll({ persist: false });

  const check = async () => {
    try {
      const resp = await fetch(window.location.href, { method: 'HEAD', cache: 'no-store' });
      if (!resp.ok) throw new Error();
      consecutiveFailures = 0;
      if (serverDown) {
        serverDown = false;
        overlay.classList.add('hidden');
        window.location.reload();
      }
    } catch {
      consecutiveFailures += 1;
      if (!serverDown && consecutiveFailures >= FAIL_THRESHOLD) {
        serverDown = true;
        tearDown();
        overlay.classList.remove('hidden');
      }
    }
  };

  setInterval(check, HEARTBEAT_INTERVAL);
})();
