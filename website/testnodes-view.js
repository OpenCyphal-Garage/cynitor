// Test nodes view: nodes Cynitor runs on the bus for testing (server/testnodes.py).
//
// The test nodes are listed as cards, each started, stopped, changed or
// deleted from its own; health and mode change as one runs. The editor makes
// or changes one: its name, node-ID and status, and up to eight publishers,
// each a subject, a compiled message type and a rate, with every number in
// the message a constant or a waveform. Its form is built from the type's
// value schema (GET /api/dsdl/value-schema/<type>).
//
// On a real bus, starting one asks first: its messages reach every node on it.
// Polls GET /api/testnodes every 2 s while the tab is open; the tab's dot, a
// test node running, comes from the node list every page polls.
const TestNodesView = (() => {
  const POLL_MS = 2000;
  const WAVE_LABELS = { sine: 'Sine', ramp: 'Ramp', square: 'Square', noise: 'Noise', count: 'Count' };
  const PERIODIC = new Set(['sine', 'ramp', 'square']);
  const SHOWN_ELEMENTS = 16;  // a fixed-length array's elements shown at first; the rest keep their default
  const NODE_ID_RANGE = [1, 125];

  let drawn = false;
  let pollTimer = null;
  let listing = null;       // the last GET /api/testnodes
  let listedJson = '';      // what the cards were drawn from, so that a poll changing nothing redraws nothing
  let failed = null;        // why the last poll failed
  const busy = new Set();   // test nodes with a request under way
  let editor = null;        // the open editor: {id: test id or null for a new one, publishers: [...]}
  const schemas = new Map(); // message type -> Promise of its value schema

  // ── Small DOM helper ──────────────────────────────────────────────────

  // An element with attributes (class, text, on<event> and the rest) and children.
  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children.filter((child) => child != null));
    return node;
  };

  // ── Skeleton ──────────────────────────────────────────────────────────

  const draw = () => {
    el('testNodesContainer').innerHTML = `
      <div class="tn-view">
        <div class="tn-header">
          <div class="tn-header-text">
            <h2 class="tn-title">Test nodes</h2>
            <span class="tn-sub">Nodes Cynitor runs on the bus for testing, each with the name, node-ID, health
              and mode you give it, publishing what you set. They run while CAN is connected, and all stop when
              it disconnects.</span>
          </div>
          <button id="tnNew" class="svc-btn svc-send-btn" type="button">New test node</button>
        </div>
        <div id="tnBus" class="tn-bus" role="status" aria-live="polite"></div>
        <div id="tnList" class="tn-list"></div>
        <div id="tnEditor" class="tn-editor hidden"></div>
        <datalist id="tnTypeNames"></datalist>
      </div>`;
    el('tnNew').addEventListener('click', () => openEditor(null));
    el('tnList').addEventListener('click', onListClick);
    el('tnList').addEventListener('change', onListChange);
    drawn = true;
  };

  // ── The bus they run on ───────────────────────────────────────────────

  const renderBus = () => {
    const box = el('tnBus');
    let text;
    let level = '';
    if (!state.dashboardConnected) {
      text = 'Not connected to the server.';
    } else if (failed) {
      text = `The test nodes could not be read: ${failed}`;
      level = 'error';
    } else if (!listing) {
      text = 'Loading…';
    } else if (!listing.bus) {
      text = 'CAN is not connected: test nodes run on the bus Cynitor connects to. '
        + 'The simulated bus needs no hardware.';
    } else if (listing.bus.simulated) {
      text = `On the simulated bus ${listing.bus.interface}: nothing real can move.`;
      level = 'ok';
    } else {
      text = `On a real bus, ${listing.bus.interface}: a test node's messages reach every node on it, `
        + 'so starting one asks first.';
      level = 'warn';
    }
    if (box.textContent !== text) box.textContent = text;
    box.className = `tn-bus${level ? ` tn-bus-${level}` : ''}`;
    const full = Boolean(listing) && listing.test_nodes.length >= listing.limits.test_nodes;
    el('tnNew').disabled = !state.dashboardConnected || !listing || full;
    el('tnNew').title = full ? `There can be up to ${listing.limits.test_nodes} test nodes` : '';
  };

  // ── Cards ─────────────────────────────────────────────────────────────

  const optionsHtml = (names, chosen) => names.map((name) =>
    `<option value="${escapeHtml(name)}"${name === chosen ? ' selected' : ''}>${escapeHtml(name)}</option>`).join('');

  const rateText = (hz) => (hz >= 1 ? `${+hz.toFixed(2)} Hz` : `every ${+(1 / hz).toFixed(1)} s`);

  const cardHtml = (node) => {
    const name = escapeHtml(node.name);
    const off = busy.has(node.id) ? ' disabled' : '';
    const stateClass = node.error ? 'error' : node.running ? 'running' : 'stopped';
    const stateText = node.error ? 'Running, with an error' : node.running ? 'Running' : 'Stopped';
    const toggle = node.running
      ? `<button type="button" class="svc-btn" data-action="stop" aria-label="Stop ${name}"${off}>Stop</button>`
      : `<button type="button" class="svc-btn svc-send-btn" data-action="start" aria-label="Start ${name}"${
        off || (listing?.bus ? '' : ' disabled title="Connect CAN first"')}>Start</button>`;
    const publishers = node.publishers.length
      ? `<ul class="tn-card-pubs">${node.publishers.map((p) => `
          <li><span class="tn-pub-subject">${p.subject_id}</span>
            <code class="tn-pub-type">${escapeHtml(p.type)}</code>
            <span class="tn-pub-rate">${rateText(p.rate_hz)}</span></li>`).join('')}</ul>`
      : '<p class="tn-muted">Publishes nothing but its heartbeat.</p>';
    return `
      <article class="tn-card" data-id="${escapeHtml(node.id)}" aria-label="Test node ${name}">
        <div class="tn-card-head">
          <span class="tn-state tn-state-${stateClass}">${stateText}</span>
          <h3 class="tn-card-name">${name}</h3>
          <span class="tn-card-id">${escapeHtml(nodeIdText(node))}</span>
          <span class="tn-card-actions">
            ${toggle}
            <button type="button" class="svc-btn" data-action="edit" aria-label="Change ${name}"${off}>Change</button>
            <button type="button" class="svc-btn svc-btn-danger" data-action="delete" aria-label="Delete ${name}"${off}>Delete</button>
          </span>
        </div>
        <div class="tn-card-status">
          <label>Health <select data-action="health" aria-label="Health of ${name}"${off}>${
            optionsHtml(listing.health, node.health)}</select></label>
          <label>Mode <select data-action="mode" aria-label="Mode of ${name}"${off}>${
            optionsHtml(listing.modes, node.mode)}</select></label>
        </div>
        ${publishers}
        ${node.error ? `<p class="tn-card-error" role="alert">${escapeHtml(node.error)}</p>` : ''}
      </article>`;
  };

  const renderList = (force = false) => {
    const list = el('tnList');
    if (!state.dashboardConnected || !listing) {
      list.innerHTML = '';
      listedJson = '';
      return;
    }
    const json = JSON.stringify([listing.test_nodes, listing.bus, [...busy]]);
    if (!force && json === listedJson) return;
    listedJson = json;
    // The control with the focus keeps it as the cards are drawn again.
    const active = list.contains(document.activeElement) ? document.activeElement : null;
    const focus = active && { id: active.closest('.tn-card')?.dataset.id, action: active.dataset.action };
    list.innerHTML = listing.test_nodes.length ? listing.test_nodes.map(cardHtml).join('') : `
      <p class="tn-empty">No test nodes yet. Make one with New test node: it can publish any compiled message
        type, your own included, each number in it a constant or a waveform.</p>`;
    if (focus?.id) focusControl(focus);
  };

  // Give the focus to control `action` of test node `id`'s card. Start and
  // Stop trade places: the one that took the other's place takes its focus.
  const focusControl = ({ id, action }) => {
    const card = el('tnList').querySelector(`.tn-card[data-id="${CSS.escape(id)}"]`);
    const swapped = { start: 'stop', stop: 'start' }[action];
    const control = card?.querySelector(`[data-action="${action}"]`) || card?.querySelector(`[data-action="${swapped}"]`);
    if (control && !control.disabled) control.focus();
    return Boolean(control);
  };

  const findNode = (id) => listing?.test_nodes.find((node) => node.id === id);

  // A test node as the API takes it back: what the listing adds to it left out.
  // A test node's node-ID on its card: the one it is set to; or, left to
  // Cynitor, the one it was given when it last started, "auto" until then.
  const nodeIdText = (node) => {
    if (node.node_id != null) return `node-ID ${node.node_id}`;
    return node.assigned_node_id != null ? `node-ID ${node.assigned_node_id} · auto` : 'node-ID auto';
  };

  const definitionOf = (node) => {
    const { id, unique_id, assigned_node_id, running, error, started_unix, ...definition } = node;
    return definition;
  };

  // `action` for test node `id`, its card's controls off meanwhile; what goes
  // wrong is said in a toast.
  const act = async (id, action) => {
    // Its controls are off meanwhile, which takes the focus from them: it comes back after.
    const active = document.activeElement;
    const focus = el('tnList').contains(active) ? { id, action: active.dataset.action } : null;
    busy.add(id);
    renderList();
    try {
      await action();
    } catch (e) {
      showToast(e.message, 'error');
    } finally {
      busy.delete(id);
      await refresh();
      const lost = !document.activeElement || document.activeElement === document.body;
      if (focus && lost && !focusControl(focus)) el('tnNew').focus();  // deleted: on to New test node
    }
  };

  const start = async (node) => {
    try {
      await requestJson(`/api/testnodes/${node.id}/start`, { method: 'POST' });
    } catch (e) {
      if (e.status !== 409 || e.data?.confirm !== 'real_bus') throw e;
      const question = `Start ${node.name} on the real bus ${listing.bus?.interface || ''}?\n\n`
        + 'Its messages reach every node on the bus, and may move real things: an actuator taking its '
        + 'setpoints from a subject it publishes, say. '
        + (node.node_id != null ? `Node-ID ${node.node_id} must be free.` : 'Cynitor gives it a node-ID that is free.');
      if (!await askConfirm({ title: 'Start on the real bus', message: question, confirmLabel: 'Start', danger: true })) {
        return;
      }
      await requestJson(`/api/testnodes/${node.id}/start`,
        { method: 'POST', body: JSON.stringify({ confirm_real_bus: true }) });
    }
  };

  const onListClick = async (e) => {
    const button = e.target.closest('button[data-action]');
    const node = button && findNode(button.closest('.tn-card')?.dataset.id);
    if (!node) return;
    const action = button.dataset.action;
    if (action === 'start') {
      act(node.id, () => start(node));
    } else if (action === 'stop') {
      act(node.id, () => requestJson(`/api/testnodes/${node.id}/stop`, { method: 'POST' }));
    } else if (action === 'edit') {
      openEditor(node);
    } else if (action === 'delete') {
      const running = node.running ? ' It stops first.' : '';
      if (!await askConfirm({ title: 'Delete test node', confirmLabel: 'Delete', danger: true,
        message: `Delete the test node ${node.name}?${running}` })) return;
      act(node.id, () => requestJson(`/api/testnodes/${node.id}`, { method: 'DELETE' }));
    }
  };

  // Health and mode: a running node takes them as it runs.
  const onListChange = (e) => {
    const select = e.target.closest('select[data-action]');
    const node = select && findNode(select.closest('.tn-card')?.dataset.id);
    if (!node) return;
    const change = { [select.dataset.action]: select.value };
    act(node.id, () => requestJson(`/api/testnodes/${node.id}`,
      { method: 'PUT', body: JSON.stringify({ ...definitionOf(node), ...change }) }));
  };

  // ── Value editors, built from a value schema ──────────────────────────
  //
  // Each is {node, get}: get() gives the value as the API takes it, or
  // undefined for the field's default.

  const rangeHint = (schema) => (schema.kind === 'int' || Math.abs(schema.max) < 1e6
    ? `${schema.type}, ${schema.min} to ${schema.max}` : schema.type);

  // Where a new waveform runs: 0 to 100, within the field's range.
  const defaultSpan = (schema) => [0, 100].map((v) => Math.min(Math.max(v, schema.min), schema.max));

  // A number: a constant, or a waveform from one value to another.
  const numberEditor = (schema, initial, label) => {
    const wave = initial && typeof initial === 'object' ? initial : null;
    const integer = schema.kind === 'int';
    const input = (value, what, extra = {}) => h('input', {
      type: 'number', class: 'tn-num', step: integer ? '1' : 'any',
      min: integer ? String(schema.min) : null, max: integer ? String(schema.max) : null,
      'aria-label': `${label}, ${what}`, value: value ?? '', ...extra,
    });
    const kind = h('select', { class: 'tn-wave-kind', 'aria-label': `${label}, constant or waveform` },
      h('option', { value: 'constant', text: 'Constant' }),
      ...listing.waves.map((name) => h('option', { value: name, text: WAVE_LABELS[name] || name })));
    kind.value = wave ? wave.$wave : 'constant';
    const [low, high] = defaultSpan(schema);
    const constant = input(wave ? '' : initial, 'value', { placeholder: '0', title: rangeHint(schema) });
    const min = input(wave ? wave.min : low, 'from');
    const max = input(wave ? wave.max : high, 'to');
    const period = input(wave?.period_s ?? 10, 'period in seconds', { step: 'any', min: '0', max: null });
    const periodic = h('span', { class: 'tn-wave-period' }, 'every', period, 's');
    const waveBox = h('span', { class: 'tn-wave' }, 'from', min, 'to', max, periodic);
    // What is hidden is off, too: the form's checks pass over it.
    const sync = () => {
      const isConstant = kind.value === 'constant';
      const isPeriodic = PERIODIC.has(kind.value);
      constant.classList.toggle('hidden', !isConstant);
      constant.disabled = !isConstant;
      waveBox.classList.toggle('hidden', isConstant);
      min.disabled = max.disabled = isConstant;
      min.required = max.required = !isConstant;
      periodic.classList.toggle('hidden', !isPeriodic);
      period.disabled = !isPeriodic;
      period.required = isPeriodic;
    };
    kind.addEventListener('change', sync);
    sync();
    return {
      node: h('span', { class: 'tn-number' }, kind, constant, waveBox),
      get: () => {
        if (kind.value === 'constant') return constant.value === '' ? undefined : Number(constant.value);
        const value = { $wave: kind.value, min: Number(min.value), max: Number(max.value) };
        if (PERIODIC.has(kind.value)) value.period_s = Number(period.value);
        return value;
      },
    };
  };

  const boolEditor = (schema, initial, label) => {
    const box = h('input', { type: 'checkbox', class: 'tn-check', 'aria-label': label });
    box.checked = initial === true;
    return { node: box, get: () => box.checked };
  };

  // What a variant left at its default is sent as: the variant must still be named.
  const emptyValue = (schema) => ({ int: 0, float: 0, bool: false, array: [] })[schema.kind] ?? {};

  const arrayEditor = (schema, initial, label) => {
    if (schema.text && (initial == null || typeof initial === 'string')) {
      const text = h('input', { type: 'text', class: 'tn-text', 'aria-label': label, value: initial ?? '',
        placeholder: `text, up to ${schema.capacity} bytes` });
      return { node: text, get: () => (text.value === '' ? undefined : text.value) };
    }
    const items = [];
    const list = h('div', { class: 'tn-array-items' });
    const addButton = h('button', { type: 'button', class: 'svc-btn svc-btn-small', text: 'Add element' });
    const removeButton = h('button', { type: 'button', class: 'svc-btn svc-btn-small', text: 'Remove last' });
    const sync = () => {
      addButton.disabled = items.length >= schema.capacity;
      removeButton.disabled = items.length === 0;
    };
    const add = (value) => {
      const index = items.length;
      const item = valueEditor(schema.element, value, `${label}[${index}]`);
      const row = h('div', { class: 'tn-array-item' }, h('span', { class: 'tn-index', text: `[${index}]` }), item.node);
      items.push({ row, item });
      list.append(row);
      sync();
    };
    addButton.addEventListener('click', () => add(undefined));
    removeButton.addEventListener('click', () => {
      items.pop()?.row.remove();
      sync();
    });
    (Array.isArray(initial) ? initial : []).forEach(add);
    while (schema.fixed && items.length < Math.min(schema.capacity, SHOWN_ELEMENTS)) add(undefined);
    sync();
    const note = schema.fixed && schema.capacity > SHOWN_ELEMENTS
      ? h('span', { class: 'tn-muted', text: `${schema.capacity} elements; those not shown are 0` }) : null;
    return {
      node: h('div', { class: 'tn-array' }, list, h('div', { class: 'tn-array-actions' }, addButton, removeButton, note)),
      // An element left at its default is sent as null, which keeps it so.
      get: () => (items.length ? items.map(({ item }) => item.get() ?? null) : undefined),
    };
  };

  // A field: its name and type, then its editor; a composite one in a box of its own.
  const fieldRow = (name, schema, editor) => {
    const composite = schema.kind === 'struct' || schema.kind === 'union' || (schema.kind === 'array' && !schema.text);
    const title = h('span', { class: 'tn-field-name' }, name, ' ', h('span', { class: 'tn-type-hint', text: schema.type }));
    return composite
      ? h('fieldset', { class: 'tn-fieldset' }, h('legend', {}, title), editor.node)
      : h('div', { class: 'tn-field' }, title, editor.node);
  };

  const constantsNote = (schema) => (schema.constants?.length
    ? h('p', { class: 'tn-muted tn-constants',
      text: `Constants: ${schema.constants.map((c) => `${c.name} = ${c.value}`).join(', ')}` })
    : null);

  const structEditor = (schema, initial, label) => {
    const fields = schema.fields.map((f) => ({
      name: f.name, editor: valueEditor(f.schema, initial?.[f.name], label ? `${label}.${f.name}` : f.name),
    }));
    const node = h('div', { class: 'tn-struct' }, constantsNote(schema),
      ...fields.map(({ name, editor }, i) => fieldRow(name, schema.fields[i].schema, editor)));
    if (!fields.length) node.append(h('span', { class: 'tn-muted', text: 'No fields' }));
    return {
      node,
      get: () => {
        const value = {};
        for (const { name, editor } of fields) {
          const v = editor.get();
          if (v !== undefined) value[name] = v;
        }
        return value;
      },
    };
  };

  const unionEditor = (schema, initial, label) => {
    const [chosen] = Object.keys(initial || {});
    const select = h('select', { class: 'tn-variant-select', 'aria-label': `${label}, variant` },
      ...schema.variants.map((v) => h('option', { value: v.name, text: `${v.name} (${v.schema.type})` })));
    select.value = chosen ?? schema.variants[0].name;
    const slot = h('div', { class: 'tn-variant' });
    let variant;
    let editor;
    const build = (value) => {
      variant = schema.variants.find((v) => v.name === select.value);
      editor = valueEditor(variant.schema, value, `${label}.${variant.name}`);
      slot.replaceChildren(editor.node);
    };
    build(chosen ? initial[chosen] : undefined);
    select.addEventListener('change', () => build(undefined));
    return {
      node: h('div', { class: 'tn-union' }, constantsNote(schema), select, slot),
      get: () => ({ [variant.name]: editor.get() ?? emptyValue(variant.schema) }),
    };
  };

  const valueEditor = (schema, initial, label) => {
    switch (schema.kind) {
      case 'int':
      case 'float': return numberEditor(schema, initial, label);
      case 'bool': return boolEditor(schema, initial, label);
      case 'array': return arrayEditor(schema, initial, label);
      case 'union': return unionEditor(schema, initial, label);
      default: return structEditor(schema, initial, label);
    }
  };

  const valueSchema = (type) => {
    if (!schemas.has(type)) {
      const asked = requestJson(`/api/dsdl/value-schema/${encodeURIComponent(type)}`);
      asked.catch(() => schemas.delete(type));  // asked again next time: it may compile meanwhile
      schemas.set(type, asked);
    }
    return schemas.get(type);
  };

  // ── Editor ────────────────────────────────────────────────────────────

  // Who else has node-ID `nodeId`: a node online on the bus, or another test node.
  const nodeIdClash = (nodeId, ownId) => {
    if (!listing) return '';
    const other = listing.test_nodes.find((n) => n.node_id === nodeId && n.id !== ownId);
    if (other) return `The test node ${other.name} has it too: the two cannot run at once.`;
    const online = Object.values(state.latestNodesPayload?.nodes || {}).find((n) =>
      n?.node_id === nodeId && !n.has_disappeared && n.test_node !== ownId);
    return online ? `${online.name || `Node ${nodeId}`} has it on the bus: a test node cannot start on it.` : '';
  };

  const publisherEditor = (index, initial) => {
    const legend = h('legend', { text: `Publisher ${index + 1}` });
    const subject = h('input', { type: 'number', min: '0', max: String(listing.limits.subject_id[1]), step: '1',
      required: true, value: initial?.subject_id ?? '', 'aria-label': 'Subject-ID' });
    const type = h('input', { type: 'text', list: 'tnTypeNames', required: true, value: initial?.type ?? '',
      placeholder: 'uavcan.si.unit.temperature.Scalar.1.0', 'aria-label': 'Message type', autocomplete: 'off',
      spellcheck: 'false' });
    const [minRate, maxRate] = listing.limits.rate_hz;
    const rate = h('input', { type: 'number', min: String(minRate), max: String(maxRate), step: 'any', required: true,
      value: initial?.rate_hz ?? 1, 'aria-label': 'Messages a second' });
    const port = h('input', { type: 'text', value: initial?.port ?? '', placeholder: 'from its type',
      pattern: '[a-z](_?[a-z0-9])*', maxlength: '32', 'aria-label': 'Port name', spellcheck: 'false',
      title: 'Its registers are uavcan.pub.<port>.id and .type: lowercase letters and digits, single underscores' });
    const remove = h('button', { type: 'button', class: 'svc-btn svc-btn-danger svc-btn-small', text: 'Remove' });
    const valueBox = h('div', { class: 'tn-value' });
    let value = null;  // the value editor, once the type's schema is in
    let shownType = null;
    let pending = initial?.value;  // the value to fill in once the schema comes

    const loadType = async () => {
      const name = type.value.trim();
      if (name === shownType) return;
      shownType = name;
      value = null;
      if (!name) {
        valueBox.replaceChildren(h('p', { class: 'tn-muted', text: 'Choose a type to set what it publishes.' }));
        return;
      }
      valueBox.replaceChildren(h('p', { class: 'tn-muted', text: 'Reading the type…' }));
      try {
        const schema = await valueSchema(name);
        if (shownType !== name) return;  // another type was chosen meanwhile
        value = valueEditor(schema, pending, 'value');
        pending = undefined;
        valueBox.replaceChildren(value.node);
      } catch (e) {
        if (shownType === name) valueBox.replaceChildren(h('p', { class: 'tn-field-error', text: e.message }));
      }
    };
    type.addEventListener('change', loadType);
    loadType();

    const label = (text, input, cls) => h('label', { class: `tn-label${cls ? ` ${cls}` : ''}` },
      h('span', { text }), input);
    const node = h('fieldset', { class: 'tn-pub' }, legend,
      h('div', { class: 'tn-pub-row' },
        label('Subject-ID', subject), label('Type', type, 'tn-label-wide'), label('Messages a second', rate),
        label('Port name', port), remove),
      valueBox);
    return {
      node,
      remove,
      renumber: (i) => { legend.textContent = `Publisher ${i + 1}`; },
      get: () => {
        const publisher = { subject_id: Number(subject.value), type: type.value.trim(), rate_hz: Number(rate.value) };
        if (port.value.trim()) publisher.port = port.value.trim();
        // A type whose schema did not come keeps the value it had.
        const v = value ? value.get() : pending;
        if (v !== undefined) publisher.value = v;
        return publisher;
      },
    };
  };

  const openEditor = (node) => {
    if (!listing) return;
    const box = el('tnEditor');
    const definition = node ? definitionOf(node) : {
      name: '', node_id: null, health: 'nominal', mode: 'operational', vendor_status: 0,
      software_version: { major: 1, minor: 0 }, publishers: [],
    };
    const select = (names, chosen, label) => {
      const s = h('select', { 'aria-label': label }, ...names.map((n) => h('option', { value: n, text: n })));
      s.value = chosen;
      return s;
    };
    const number = (value, min, max, label) => h('input', { type: 'number', min: String(min), max: String(max),
      step: '1', required: true, value, 'aria-label': label });
    const name = h('input', { type: 'text', required: true, maxlength: '50', value: definition.name,
      'aria-label': 'Name', placeholder: 'e.g. test.battery', spellcheck: 'false' });
    // Left empty, the node-ID is Cynitor's to give: a free one when the node
    // starts, the same from run to run while it stays free.
    const nodeId = h('input', { type: 'number', min: String(NODE_ID_RANGE[0]), max: String(NODE_ID_RANGE[1]),
      step: '1', value: definition.node_id ?? '', 'aria-label': 'Node-ID', placeholder: 'auto',
      title: 'Empty: Cynitor gives it a free node-ID when it starts' });
    const health = select(listing.health, definition.health, 'Health');
    const mode = select(listing.modes, definition.mode, 'Mode');
    const vendor = number(definition.vendor_status, 0, 255, 'Vendor-specific status code');
    const major = number(definition.software_version.major, 0, 255, 'Software version, major');
    const minor = number(definition.software_version.minor, 0, 255, 'Software version, minor');
    const clash = h('p', { class: 'tn-field-error', role: 'status' });
    const showClash = () => {
      clash.textContent = nodeId.value.trim() === '' ? '' : nodeIdClash(Number(nodeId.value), node?.id);
    };
    nodeId.addEventListener('input', showClash);
    showClash();

    const publishers = [];
    const pubBox = h('div', { class: 'tn-pubs' });
    const addPublisher = h('button', { type: 'button', class: 'svc-btn', text: 'Add publisher' });
    const syncPublishers = () => {
      publishers.forEach((p, i) => p.renumber(i));
      addPublisher.disabled = publishers.length >= listing.limits.publishers;
      addPublisher.title = addPublisher.disabled ? `A test node has up to ${listing.limits.publishers} publishers` : '';
    };
    const add = (initial) => {
      const publisher = publisherEditor(publishers.length, initial);
      publisher.remove.addEventListener('click', () => {
        publishers.splice(publishers.indexOf(publisher), 1);
        publisher.node.remove();
        syncPublishers();
      });
      publishers.push(publisher);
      pubBox.append(publisher.node);
      syncPublishers();
      return publisher;
    };
    definition.publishers.forEach(add);
    addPublisher.addEventListener('click', () => {
      const used = new Set(publishers.map((p) => p.get().subject_id));
      let subjectId = 1000;
      while (used.has(subjectId)) subjectId += 1;
      add({ subject_id: subjectId, rate_hz: 1 }).node.querySelector('input[list]').focus();
    });

    const error = h('p', { class: 'tn-form-error', role: 'alert' });
    const save = h('button', { type: 'submit', class: 'svc-btn svc-send-btn', text: node ? 'Save' : 'Make it' });
    const cancel = h('button', { type: 'button', class: 'svc-btn', text: 'Cancel', onclick: closeEditor });
    const label = (text, input) => h('label', { class: 'tn-label' }, h('span', { text }), input);
    const form = h('form', { class: 'tn-form', 'aria-label': node ? `Change ${node.name}` : 'New test node' },
      h('h3', { class: 'tn-form-title', text: node ? `Change ${node.name}` : 'New test node' }),
      node?.running ? h('p', { class: 'tn-muted',
        text: 'It runs: a change of health, mode or status it takes as it runs; any other change restarts it.' })
        : null,
      h('div', { class: 'tn-form-grid' },
        label('Name', name), label('Node-ID', nodeId), label('Health', health), label('Mode', mode),
        label('Vendor status', vendor),
        h('span', { class: 'tn-label' }, h('span', { text: 'Software version' }),
          h('span', { class: 'tn-version' }, major, '.', minor))),
      clash,
      h('h4', { class: 'tn-form-subtitle', text: 'Publishers' }),
      pubBox, addPublisher, error,
      h('div', { class: 'tn-form-actions' }, save, cancel));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const sent = {
        name: name.value.trim(), node_id: nodeId.value.trim() === '' ? null : Number(nodeId.value),
        health: health.value, mode: mode.value,
        vendor_status: Number(vendor.value), software_version: { major: Number(major.value), minor: Number(minor.value) },
        publishers: publishers.map((p) => p.get()),
      };
      save.disabled = true;
      error.textContent = '';
      try {
        await requestJson(node ? `/api/testnodes/${node.id}` : '/api/testnodes',
          { method: node ? 'PUT' : 'POST', body: JSON.stringify(sent) });
        closeEditor();
        await refresh();
      } catch (err) {
        error.textContent = err.message;
      } finally {
        save.disabled = false;
        // Off while it saved, it lost the focus: it gets it back, so that the form goes on by keyboard.
        if (save.isConnected && (!document.activeElement || document.activeElement === document.body)) save.focus();
      }
    });
    form.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeEditor();
    });

    editor = { id: node?.id ?? null };
    box.replaceChildren(form);
    box.classList.remove('hidden');
    el('tnList').classList.add('hidden');
    el('tnNew').classList.add('hidden');
    name.focus();
    fetchMessageTypeNames().then((names) => {
      el('tnTypeNames').replaceChildren(...names.map((n) => h('option', { value: n })));
    }).catch(() => {});  // suggestions only
  };

  function closeEditor() {
    const opener = editor?.id;
    editor = null;
    el('tnEditor').classList.add('hidden');
    el('tnEditor').replaceChildren();
    el('tnList').classList.remove('hidden');
    el('tnNew').classList.remove('hidden');
    const card = opener && el('tnList').querySelector(`.tn-card[data-id="${CSS.escape(opener)}"]`);
    (card?.querySelector('[data-action="edit"]') || el('tnNew')).focus();
  }

  // ── Polling ───────────────────────────────────────────────────────────

  const refresh = async () => {
    if (!state.dashboardConnected) {
      listing = null;
      failed = null;
    } else {
      try {
        listing = await requestJson('/api/testnodes');
        failed = null;
      } catch (e) {
        failed = e.message;
      }
    }
    if (!drawn) return;
    renderBus();
    renderList();
  };

  const init = () => {
    if (!drawn) draw();
    renderBus();
    renderList(true);
    refresh();
    clearInterval(pollTimer);
    pollTimer = setInterval(refresh, POLL_MS);
  };

  const hide = () => {
    clearInterval(pollTimer);
    pollTimer = null;
  };

  // The tab's dot: a test node runs. From the node list, which every page
  // polls, so that it shows whichever tab is open.
  const refreshIndicator = () => {
    const running = Object.values(state.latestNodesPayload?.nodes || {})
      .some((node) => node?.test_node && !node.has_disappeared);
    const tab = el('viewTabTestNodes');
    if (!tab) return;
    tab.classList.toggle('testnodes-active', running);
    tab.title = running ? 'A test node runs on the bus' : '';
  };

  return { init, hide, refreshIndicator };
})();
