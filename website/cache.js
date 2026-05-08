// Telemetry cache and per-node accessors. Reads/writes the latestBy*
// maps and subjectHistory in `state` (state.js). No DOM access here.

const isNodeDisappeared = (nodeId) => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes) return false;
  const node = nodes[String(nodeId)];
  return node?.has_disappeared === true;
};

const cacheEvent = (event) => {
  if (!event || !Number.isInteger(event.subject_id)) {
    return;
  }

  if (Number.isInteger(event.publisher_node_id) && isNodeDisappeared(event.publisher_node_id)) {
    return;
  }

  state.latestBySubject.set(event.subject_id, event);

  if (Number.isInteger(event.publisher_node_id)) {
    if (!state.latestByNode.has(event.publisher_node_id)) {
      state.latestByNode.set(event.publisher_node_id, new Map());
    }
    state.latestByNode.get(event.publisher_node_id).set(event.subject_id, event);
  }

  const now = event.timestamp_unix || (Date.now() / 1000);
  for (const a of event.attributes || []) {
    if (typeof a.value !== 'number') continue;
    const key = `${event.subject_id}:${a.attribute}`;
    if (!state.subjectHistory.has(key)) state.subjectHistory.set(key, []);
    const buf = state.subjectHistory.get(key);
    buf.push({ t: now, v: a.value });
    if (buf.length > 3600) buf.shift();
  }
};

const getSelectedNode = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes || state.selectedNodeId == null) {
    return null;
  }
  return nodes[String(state.selectedNodeId)] || null;
};

const getNodeRate = (nodeId) => {
  const nodes = state.latestNodesPayload?.nodes;
  const node = nodes ? nodes[String(nodeId)] : null;
  if (node && node.has_disappeared) {
    return 0;
  }
  const map = state.latestByNode.get(nodeId);
  if (!map) {
    return 0;
  }
  let total = 0;
  for (const event of map.values()) {
    total += Number(event.rate) || 0;
  }
  return total;
};

const getNodeHealthValue = (nodeId) => {
  const map = state.latestByNode.get(nodeId);
  if (!map) {
    return null;
  }
  for (const event of map.values()) {
    if (!Array.isArray(event.attributes)) {
      continue;
    }
    for (const attr of event.attributes) {
      if (String(attr.attribute).toLowerCase() === 'health') {
        return String(attr.value);
      }
    }
  }
  return null;
};

const getNodeVisualState = (node) => {
  if (!node) {
    return 'idle';
  }
  if (node.has_disappeared) {
    return 'offline';
  }
  const health = getNodeHealthValue(node.node_id);
  if (health && health !== 'NOMINAL') {
    return 'error';
  }
  return getNodeRate(node.node_id) > 0 ? 'active' : 'idle';
};

const getTotalMessageRate = () => {
  const nodes = state.latestNodesPayload?.nodes;
  if (!nodes || typeof nodes !== 'object') return 0;
  let total = 0;
  for (const node of Object.values(nodes)) {
    total += getNodeRate(node.node_id);
  }
  return total;
};

const buildSubjectDetailData = (subjectIds, nodeId) => {
  if (!Array.isArray(subjectIds) || !subjectIds.length) {
    return [];
  }
  const perNodeEvents = Number.isInteger(nodeId) ? state.latestByNode.get(nodeId) : null;

  return subjectIds.map((subjectId) => {
    const nodeEvent = perNodeEvents?.get(subjectId);
    const networkEvent = state.latestBySubject.get(subjectId);
    const event = nodeEvent || networkEvent;
    return {
      subjectId,
      messageType: event?.message_type || null,
      rate: event?.rate ?? null,
      attributes: Array.isArray(event?.attributes) ? event.attributes : [],
    };
  });
};

const pruneNodeCache = () => {
  const payloadNodes = state.latestNodesPayload?.nodes;
  if (!payloadNodes || typeof payloadNodes !== 'object') return;

  const knownNodeIds = new Set();
  const knownSubjectIds = new Set();
  for (const node of Object.values(payloadNodes)) {
    if (Number.isInteger(node.node_id)) knownNodeIds.add(node.node_id);
    for (const sid of node.publishers || []) {
      if (Number.isInteger(sid)) knownSubjectIds.add(sid);
    }
  }

  for (const nodeId of [...state.latestByNode.keys()]) {
    if (!knownNodeIds.has(nodeId)) {
      state.latestByNode.delete(nodeId);
    }
  }

  for (const sid of [...state.latestBySubject.keys()]) {
    if (!knownSubjectIds.has(sid)) {
      state.latestBySubject.delete(sid);
    }
  }

  for (const key of [...state.subjectHistory.keys()]) {
    const sid = Number(key.split(':')[0]);
    if (!knownSubjectIds.has(sid)) {
      state.subjectHistory.delete(key);
    }
  }

  for (const key of [...metricMaxLen.keys()]) {
    const sid = Number(key.split(':')[0]);
    if (!knownSubjectIds.has(sid)) {
      metricMaxLen.delete(key);
    }
  }
};
