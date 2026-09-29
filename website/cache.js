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

// Rate of a subject across all its publishers. `rate` on an event is only its
// own publisher's; recordings made before `subject_rate` existed fall back to it.
const getSubjectRate = (event) => Number(event?.subject_rate ?? event?.rate) || 0;

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

// A field of the node's latest heartbeat (health, mode, ...), or null.
const getNodeHeartbeatValue = (nodeId, attribute) => {
  const map = state.latestByNode.get(nodeId);
  if (!map) {
    return null;
  }
  for (const event of map.values()) {
    if (!Array.isArray(event.attributes)) {
      continue;
    }
    for (const attr of event.attributes) {
      if (String(attr.attribute).toLowerCase() === attribute) {
        return String(attr.value);
      }
    }
  }
  return null;
};

const getNodeHealthValue = (nodeId) => getNodeHeartbeatValue(nodeId, 'health');
const getNodeModeValue = (nodeId) => getNodeHeartbeatValue(nodeId, 'mode');

const getNodeVisualState = (node) => {
  if (!node) {
    return 'idle';
  }
  if (node.has_disappeared) {
    return 'offline';
  }
  // Cyphal health: ADVISORY is a minor note (the Health column shows it),
  // CAUTION a degraded node, WARNING a failing one.
  const health = getNodeHealthValue(node.node_id);
  if (health === 'WARNING') return 'warning';
  if (health === 'CAUTION') return 'caution';
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

// A node by its alias or GetInfo name, when the dashboard knows one.
const nodeDisplayName = (nodeId) => {
  const node = state.latestNodesPayload?.nodes?.[nodeId];
  return node ? getNodeAlias(node.unique_id) || node.name || '' : '';
};

// "10, 11" -> "10 org.zubax.myxa (front_left)\n11 ...": node-IDs with names, for a tooltip.
const nodeIdsTitle = (idsText) => String(idsText).split(', ').filter((id) => id && id !== '-')
  .map((id) => `${id} ${nodeDisplayName(Number(id))}`.trim()).join('\n');

// Cyphal's fixed port-IDs: subjects from 6144 and services from 256 are
// the standard ones every node may have (heartbeat, GetInfo, registers, ...).
const FIRST_FIXED_SERVICE_ID = 256;
const isFixedPortId = (kind, id) =>
  id >= (kind === 'service' ? FIRST_FIXED_SERVICE_ID : FIRST_FIXED_SUBJECT_ID);

// The standard types on their fixed port-IDs (public regulated DSDL), for
// naming a port before anything has been decoded on it.
const STANDARD_SUBJECT_TYPES = {
  7168: 'uavcan.time.Synchronization', 7509: 'uavcan.node.Heartbeat', 7510: 'uavcan.node.port.List',
  8164: 'uavcan.pnp.cluster.Discovery', 8165: 'uavcan.pnp.NodeIDAllocationData (v2)',
  8166: 'uavcan.pnp.NodeIDAllocationData (v1)', 8174: 'uavcan.internet.udp.OutgoingPacket',
  8184: 'uavcan.diagnostic.Record',
};
const STANDARD_SERVICE_TYPES = {
  384: 'uavcan.register.Access', 385: 'uavcan.register.List', 390: 'uavcan.pnp.cluster.AppendEntries',
  391: 'uavcan.pnp.cluster.RequestVote', 405: 'uavcan.file.GetInfo', 406: 'uavcan.file.List',
  407: 'uavcan.file.Modify', 408: 'uavcan.file.Read', 409: 'uavcan.file.Write', 430: 'uavcan.node.GetInfo',
  434: 'uavcan.node.GetTransportStatistics', 435: 'uavcan.node.ExecuteCommand',
  500: 'uavcan.internet.udp.HandleIncomingPacket', 510: 'uavcan.time.GetSynchronizationMasterInfo',
};

// Where a subject's type comes from: 'registers', 'user' (set in Subjects),
// or null. Subject-IDs from 6144 up are fixed ports, decoded by their own types.
const FIRST_FIXED_SUBJECT_ID = 6144;
const subjectTypeSource = (subjectId) =>
  state.latestNodesPayload?.subject_types?.[subjectId]?.set_by ?? null;

// A subject nothing names the type of, so it cannot be decoded. Only a live
// session reports subject types; a replay's subjects decode regardless.
const isUntypedSubject = (subjectId) => {
  const types = state.latestNodesPayload?.subject_types;
  return Boolean(types) && subjectId < FIRST_FIXED_SUBJECT_ID && !types[subjectId]
    && !state.latestBySubject.get(subjectId)?.message_type;
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
      untyped: isUntypedSubject(subjectId),
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
