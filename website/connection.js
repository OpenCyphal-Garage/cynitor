// Connection lifecycle: WebSocket, REST polling (status, nodes, interfaces),
// throughput meter, semaphores, dashboard/CAN connect/disconnect, and the
// shared disconnectAll teardown used by pollStatus, connectDashboard, and
// the heartbeat in app.js.

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
      canInfo.textContent = `${rate.toFixed(1)} msg/s${utilStr}`;
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
  button.textContent = state.dashboardConnected ? 'Disconnect' : 'Connect';
  button.disabled = state.canConnecting || state.canDisconnecting;
  updateSemaphores();
};

const updateCanConnectButton = () => {
  const button = el('connectCanBtn');
  if (!button) {
    return;
  }
  button.textContent = state.canConnected ? 'Disconnect' : 'Connect';
  button.disabled = state.canConnecting || state.canDisconnecting;
  updateSemaphores();
};

const startThroughputTimer = () => {
  if (state.throughputTimer) clearInterval(state.throughputTimer);
  state.throughputTimer = window.setInterval(() => {
    state.wsThroughput = state.wsBytesAccum;
    state.wsBytesAccum = 0;
    updateSemaphores();
  }, 1000);
};

const stopThroughputTimer = () => {
  if (state.throughputTimer) {
    clearInterval(state.throughputTimer);
    state.throughputTimer = null;
  }
  state.wsBytesAccum = 0;
  state.wsThroughput = 0;
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
  state.ws = new WebSocket(`${wsBase()}/ws`);

  state.ws.onopen = () => {
    state.wsReconnectAttempts = 0;
    updateSemaphores();
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
      const event = JSON.parse(message.data);
      if (event.type === 'filter_updated' || event.type === 'pong') {
        return;
      }
      if (event.type === 'metrics') {
        state.busUtilization = event.bus_utilization ?? null;
        return;
      }
      cacheEvent(event);
      scheduleDetailRefresh();
      scheduleTableRefresh();
    } catch {
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

const loadInterfaces = async () => {
  try {
    const data = await requestJson('/api/status');
    const interfaces = data.available_interfaces || [];
    const select = el('interfacesSelect');
    const preferredValue = select.value || state.preferredCanInterface;
    select.innerHTML = '';
    for (const iface of interfaces) {
      const option = document.createElement('option');
      option.value = iface;
      option.textContent = iface;
      select.appendChild(option);
    }

    if (interfaces.includes(preferredValue)) {
      select.value = preferredValue;
    } else if (interfaces.length > 0) {
      select.value = interfaces[0];
    }
    state.preferredCanInterface = select.value;
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
  const selected = el('interfacesSelect').value;
  if (!selected) {
    return null;
  }

  try {
    const data = await requestJson('/api/can/connect', {
      method: 'POST',
      body: JSON.stringify({ interface: selected }),
    });
    state.preferredCanInterface = selected;
    saveSettings();
    return data;
  } catch (error) {
    return null;
  }
};

const disconnectAll = ({ persist = true } = {}) => {
  state.dashboardConnected = false;
  state.canConnected = false;
  state.busUtilization = null;
  state.latestBySubject.clear();
  state.latestByNode.clear();
  state.subjectHistory.clear();
  state.serviceSchemas.clear();
  state.serviceCallState = null;
  REG_CACHE.clear();
  stopStatusPolling();
  stopCanStartupDelay();
  stopNodesPolling();
  stopInterfacePolling();
  stopThroughputTimer();
  stopPlotAnim();
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

  const backendCanRunning = data.status === 'running' && !!data.can_interface;

  if (backendCanRunning && !state.canConnected) {
    // Another client connected CAN
    const select = el('interfacesSelect');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = data.can_interface;
    option.textContent = data.can_interface;
    select.appendChild(option);
    select.value = data.can_interface;
    state.preferredCanInterface = data.can_interface;
    state.canConnected = true;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(3000);
    saveSettings();
  } else if (!backendCanRunning && state.canConnected) {
    // CAN disconnected (by another client or due to error)
    state.canConnected = false;
    state.busUtilization = null;
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

  let statusData;
  try {
    statusData = await requestJson('/api/status');
  } catch {
    updateSemaphores();
    return;
  }

  state.dashboardConnected = true;
  updateDashboardConnectButton();
  startStatusPolling();

  // If backend already has CAN running, sync state
  if (statusData.status === 'running' && statusData.can_interface) {
    const select = el('interfacesSelect');
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = statusData.can_interface;
    option.textContent = statusData.can_interface;
    select.appendChild(option);
    select.value = statusData.can_interface;
    state.preferredCanInterface = statusData.can_interface;
    state.canConnected = true;
    updateCanConnectButton();
    stopInterfacePolling();
    schedulePostCanStartup(5000);
  } else {
    await loadInterfaces();
    startInterfacePolling();
  }

  renderNodesTable();
  renderSelectedNodeContent();
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
  updateCanConnectButton();
  renderNodesTable();
  renderSelectedNodeContent();
  saveSettings();

  schedulePostCanStartup(5000);

  if (!state.dashboardConnected) {
    await connectDashboard();
  }
};
