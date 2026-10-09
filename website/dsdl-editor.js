// DSDL editor — the panel where a custom type is written, beside the type
// shown in the DSDL tab. Loaded before dsdl-view.js, which opens it and
// lends it what it needs of the tab (attach).

const DsdlEditor = (() => {
  let _editorOpen = false;
  let _editorDirty = false;  // the editor holds changes not saved yet
  let _editorMode = 'new';
  let _editPrefill = null;
  let _previewRatio = 0.5;
  let _customNamespaces = [];
  // What the tab lends: typeIndex(), typeShown(), showSaved(fullName),
  // fieldTypeHtml(type) and lockedAdvice.
  let _tab = null;

  const attach = (tab) => { _tab = tab; };

  // True when the editor holds nothing unsaved, or the user lets it go.
  const _mayDropDraft = () => !_editorOpen || !_editorDirty
    || window.confirm('Discard the unsaved changes in the editor?');

  // A new type, its editor empty or opened on `draft` ({type_name, version,
  // source_text, fixed_port_id}).
  const _openEditorNew = async (prefilledNs, draft = null) => {
    if (!_mayDropDraft()) return;
    _editorOpen = true;
    _editorMode = 'new';
    _editPrefill = null;
    try {
      const resp = await requestJson('/api/dsdl/custom/namespaces');
      _customNamespaces = resp.namespaces || [];
    } catch { _customNamespaces = []; }
    _renderEditorSplit(prefilledNs, draft);
  };

  // A type changed once compiled becomes a new version, Cyphal's way: the
  // editor opens on a copy, at the next minor version not taken.
  const _openEditorNewVersion = (typeData) => {
    const [major, minor] = typeData.version.split('.').map(Number);
    const taken = _tab.typeIndex();
    let next = minor + 1;
    while (taken[`${typeData.namespace}.${typeData.short_name}.${major}.${next}`]) next += 1;
    return _openEditorNew(typeData.namespace, {
      type_name: typeData.short_name,
      version: `${major}.${next}`,
      source_text: typeData.source_text || '',
      fixed_port_id: typeData.fixed_port_id,
    });
  };

  const _openEditorEdit = async (typeData) => {
    if (!_mayDropDraft()) return;
    _editorOpen = true;
    _editorMode = 'edit';
    _editPrefill = {
      namespace: typeData.namespace,
      type_name: typeData.short_name,
      version: typeData.version,
      source_text: typeData.source_text || '',
      fixed_port_id: typeData.fixed_port_id,
      full_name: typeData.full_name,
    };
    try {
      const resp = await requestJson('/api/dsdl/custom/namespaces');
      _customNamespaces = resp.namespaces || [];
    } catch { _customNamespaces = []; }
    _renderEditorSplit(typeData.namespace);
  };

  const _closeEditor = () => {
    _editorOpen = false;
    _editorDirty = false;
    const area = document.getElementById('dsdlDetailArea');
    if (!area) return;

    const editorPanel = document.getElementById('dsdlEditorPanel');
    const editorHandle = document.getElementById('dsdlEditorHandle');
    if (editorPanel) editorPanel.remove();
    if (editorHandle) editorHandle.remove();
    area.classList.remove('dsdl-editor-alone');
  };

  const _renderEditorSplit = (prefilledNs, draft = null) => {
    const area = document.getElementById('dsdlDetailArea');
    if (!area) return;

    let editorPanel = document.getElementById('dsdlEditorPanel');
    if (!editorPanel) {
      editorPanel = document.createElement('div');
      editorPanel.id = 'dsdlEditorPanel';
      editorPanel.className = 'dsdl-editor-panel';
      area.insertBefore(editorPanel, area.firstChild);

      const handle = document.createElement('div');
      handle.id = 'dsdlEditorHandle';
      handle.className = 'dsdl-editor-handle';
      area.insertBefore(handle, editorPanel.nextSibling);

      _initEditorDrag();
    }
    // A new editor, unlocked even where the one before it was locked.
    editorPanel.classList.remove('dsdl-editor-locked');

    // Beside the type shown, its share of the area dragged at the handle
    // (--dsdl-editor-share); with no type shown, all of it.
    area.classList.toggle('dsdl-editor-alone', !_tab.typeShown());

    const isEdit = _editorMode === 'edit';
    const fill = (isEdit ? _editPrefill : draft) || {};
    const nameValue = fill.type_name || '';
    const versionValue = fill.version || '1.0';
    const portValue = fill.fixed_port_id != null ? String(fill.fixed_port_id) : '';
    const sourceValue = fill.source_text || '';
    const lockAttr = isEdit ? ' disabled' : '';
    const titleText = isEdit ? 'Edit DSDL Type' : 'New DSDL Type';
    const saveLabel = isEdit ? 'Save changes' : 'Save';

    // The namespaces there are, offered as one is typed; a new one is
    // created with the type it is saved with.
    const nsOptions = _customNamespaces.map(ns => `<option value="${escapeHtml(ns)}"></option>`).join('');

    const policyTip = `Only types that aren't compiled can be edited. Compiled types are loaded by the running CAN runtime — changing them would diverge source from live code. To change one: ${_tab.lockedAdvice.toLowerCase()}`;

    editorPanel.innerHTML = `
      <div class="dsdl-editor-toolbar">
        <span class="dsdl-editor-title">${titleText}</span>
        <span class="dsdl-editor-toolbar-actions">
          <span class="dsdl-editor-status" id="dsdlEditorStatus" role="status"></span>
          <span class="dsdl-info-tip" tabindex="0" role="img" aria-label="${escapeHtml(`Save policy: ${policyTip}`)}" data-tip="${escapeHtml(policyTip)}">?</span>
          <button class="dsdl-editor-btn dsdl-editor-btn-save" id="dsdlEditorSave">${saveLabel}</button>
          <button class="dsdl-editor-close" id="dsdlEditorClose" aria-label="Close editor">&times;</button>
        </span>
      </div>
      <div class="dsdl-editor-form">
        <div class="dsdl-editor-row">
          <label class="dsdl-editor-label" for="dsdlEditorNs">Namespace</label>
          <div class="dsdl-editor-ns-wrap">
            <input type="text" class="dsdl-editor-input" id="dsdlEditorNs" list="dsdlEditorNsList" autocomplete="off"
                   placeholder="myapp or myapp.sensors" value="${escapeHtml(prefilledNs || '')}"
                   title="Pick one of yours, or name a new one: it is created with the type"${lockAttr} />
            <datalist id="dsdlEditorNsList">${nsOptions}</datalist>
          </div>
        </div>
        <div class="dsdl-editor-row dsdl-editor-row-inline">
          <div>
            <label class="dsdl-editor-label" for="dsdlEditorName">Type name</label>
            <input type="text" class="dsdl-editor-input" id="dsdlEditorName" placeholder="MyMessage" value="${escapeHtml(nameValue)}"${lockAttr} />
          </div>
          <div>
            <label class="dsdl-editor-label" for="dsdlEditorVer">Version</label>
            <input type="text" class="dsdl-editor-input dsdl-editor-ver" id="dsdlEditorVer" placeholder="1.0" value="${escapeHtml(versionValue)}"${lockAttr} />
          </div>
          <div>
            <label class="dsdl-editor-label" for="dsdlEditorPort">Port ID</label>
            <input type="text" class="dsdl-editor-input dsdl-editor-port" id="dsdlEditorPort" placeholder="optional" value="${escapeHtml(portValue)}"
                   title="Leave empty unless the type needs a fixed port ID: ${_FIXED_PORT_RANGES.message.join('–')} for a message, ${_FIXED_PORT_RANGES.service.join('–')} for a service" />
          </div>
        </div>
        <div class="dsdl-editor-row dsdl-editor-row-grow">
          <label class="dsdl-editor-label" for="dsdlEditorSource">Source</label>
          <div class="dsdl-editor-source-wrap" id="dsdlEditorSourceWrap">
            <textarea class="dsdl-editor-source" id="dsdlEditorSource" spellcheck="false"
                      placeholder="# Write your DSDL definition here&#10;uint32 my_field&#10;float32 temperature&#10;@sealed&#10;# A service: the request, then ---, then the response, each ending in @sealed">${escapeHtml(sourceValue)}</textarea>
            <div class="dsdl-editor-preview-handle" id="dsdlPreviewHandle"></div>
            <div class="dsdl-editor-preview" id="dsdlEditorPreview">
              <div class="dsdl-editor-preview-label">Preview</div>
              <div class="dsdl-editor-preview-content" id="dsdlPreviewContent">
                <div class="dsdl-custom-empty">Type DSDL source above</div>
              </div>
            </div>
          </div>
        </div>
      </div>`;

    if (sourceValue) _updatePreview(sourceValue);

    // Anything typed or picked is a draft until it is saved.
    _editorDirty = false;
    editorPanel.querySelector('.dsdl-editor-form')?.addEventListener('input', () => { _editorDirty = true; });

    document.getElementById('dsdlEditorClose')?.addEventListener('click', () => {
      if (_mayDropDraft()) _closeEditor();
    });
    document.getElementById('dsdlEditorSave')?.addEventListener('click', _saveType);

    const sourceEl = document.getElementById('dsdlEditorSource');
    let previewTimer;
    sourceEl?.addEventListener('input', () => {
      clearTimeout(previewTimer);
      // Not into another editor's preview once this one is closed or redrawn.
      previewTimer = setTimeout(() => { if (sourceEl.isConnected) _updatePreview(sourceEl.value); }, 250);
    });

    _initPreviewDrag();
    _applyPreviewRatio();
  };

  const _getEditorNamespace = () => document.getElementById('dsdlEditorNs')?.value.trim() || '';

  const _parseDsdlSource = (text) => {
    const fieldRe = /^(?:truncated\s+|saturated\s+)?(\S+)\s+([a-zA-Z_]\w*)(?:\s*=\s*([^#]+))?/;
    let kind = 'message';
    let section = 'message';
    const fields = { message: [] };
    const constants = [];

    for (const line of text.split('\n')) {
      const s = line.trim();
      if (s === '---') {
        kind = 'service';
        fields.request = fields.message || [];
        delete fields.message;
        fields.response = [];
        section = 'response';
        continue;
      }
      if (!s || s.startsWith('#') || s.startsWith('@')) continue;
      const m = s.match(fieldRe);
      if (!m) continue;
      const [, type, name, value] = m;
      if (value !== undefined) { constants.push({ type, name, value: value.trim() }); continue; }
      if (type.startsWith('void')) continue;
      if (!fields[section]) fields[section] = [];
      fields[section].push({ type, name });
    }

    return {
      kind,
      fields: kind === 'service'
        ? { request: fields.request || [], response: fields.response || [] }
        : fields.message || [],
      constants,
    };
  };

  const _updatePreview = (sourceText) => {
    const content = document.getElementById('dsdlPreviewContent');
    if (!content) return;

    if (!sourceText.trim()) {
      content.innerHTML = '<div class="dsdl-custom-empty">Type DSDL source above</div>';
      return;
    }

    const parsed = _parseDsdlSource(sourceText);
    const isService = parsed.kind === 'service';
    const kindLabel = isService ? 'Service' : 'Message';
    const kindCls = isService ? 'dsdl-badge-service' : 'dsdl-badge-message';

    let fieldsHtml;
    if (isService) {
      fieldsHtml = `
        <div class="dsdl-preview-section dsdl-preview-section-req">
          <span class="dsdl-preview-label dsdl-preview-label-req">Request</span>
          ${_renderPreviewFields(parsed.fields.request)}
        </div>
        <div class="dsdl-preview-divider" aria-hidden="true">⎯ ⎯ ⎯</div>
        <div class="dsdl-preview-section dsdl-preview-section-res">
          <span class="dsdl-preview-label dsdl-preview-label-res">Response</span>
          ${_renderPreviewFields(parsed.fields.response)}
        </div>`;
    } else {
      fieldsHtml = `
        <div class="dsdl-preview-section">
          <span class="dsdl-preview-label">Fields</span>
          ${_renderPreviewFields(parsed.fields)}
        </div>`;
    }

    const constHtml = parsed.constants.length ? `
      <div class="dsdl-preview-section">
        <span class="dsdl-preview-label">Constants</span>
        <table class="dsdl-ftable"><tbody>
          ${parsed.constants.map(c => `<tr class="dsdl-frow">
            <td class="dsdl-fcol-type">${escapeHtml(c.type)}</td>
            <td class="dsdl-fcol-name">${escapeHtml(c.name)}</td>
            <td class="dsdl-fcol-val"><span class="dsdl-const-eq">=</span> ${escapeHtml(c.value)}</td>
          </tr>`).join('')}
        </tbody></table>
      </div>` : '';

    content.innerHTML = `
      <div class="dsdl-preview-header"><span class="dsdl-badge ${kindCls}">${kindLabel}</span></div>
      ${fieldsHtml}${constHtml}`;
  };

  const _renderPreviewFields = (fields) => {
    if (!fields.length) return '<div class="dsdl-custom-empty">No fields</div>';
    return `<table class="dsdl-ftable"><tbody>
      ${fields.map(f => `<tr class="dsdl-frow">
        <td class="dsdl-fcol-type">${_tab.fieldTypeHtml(f.type)}</td>
        <td class="dsdl-fcol-name">${escapeHtml(f.name)}</td>
      </tr>`).join('')}
    </tbody></table>`;
  };

  // The source's share of its column, the preview having the rest: a
  // variable on the column, which each editor redraws, so set again then.
  const _applyPreviewRatio = () => {
    document.getElementById('dsdlEditorSourceWrap')?.style.setProperty('--dsdl-source-share', _previewRatio);
  };

  const _initPreviewDrag = () => {
    const handle = document.getElementById('dsdlPreviewHandle');
    const wrap = document.getElementById('dsdlEditorSourceWrap');
    if (!handle || !wrap) return;

    let dragging = false;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('dragging');
      document.body.classList.add('dsdl-resizing-row');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    const onMove = (e) => {
      if (!dragging) return;
      const rect = wrap.getBoundingClientRect();
      const y = e.clientY - rect.top;
      _previewRatio = Math.max(0.15, Math.min(0.85, y / rect.height));
      _applyPreviewRatio();
    };

    const onUp = () => {
      dragging = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.classList.remove('dsdl-resizing-row');
    };
  };

  // The fixed port IDs the compiler accepts on a type of your own (the server
  // checks them too); it refuses any other.
  const _FIXED_PORT_RANGES = { message: [6144, 7167], service: [256, 383] };

  const _fixedPortError = (portStr, source) => {
    if (!portStr) return null;
    const kind = _parseDsdlSource(source).kind;
    const [low, high] = _FIXED_PORT_RANGES[kind];
    const port = /^\d+$/.test(portStr) ? Number(portStr) : NaN;
    if (port >= low && port <= high) return null;
    return `A fixed port ID for a ${kind} of your own must be from ${low} to ${high}, or left empty`;
  };

  const _saveType = async () => {
    const namespace = _getEditorNamespace();
    const typeName = document.getElementById('dsdlEditorName')?.value.trim();
    const version = document.getElementById('dsdlEditorVer')?.value.trim();
    const source = document.getElementById('dsdlEditorSource')?.value;
    const portStr = document.getElementById('dsdlEditorPort')?.value.trim();
    const portId = portStr ? Number(portStr) : null;

    if (!namespace || !typeName || !version || !source) {
      _showEditorStatus('Fill in namespace, name, version, and source', true);
      return;
    }
    const portError = _fixedPortError(portStr, source);
    if (portError) {
      _showEditorStatus(portError, true);
      return;
    }

    const status = document.getElementById('dsdlEditorStatus');
    if (status) { status.textContent = 'Saving…'; status.className = 'dsdl-editor-status'; }

    const fullName = `${namespace}.${typeName}.${version}`;
    try {
      // Saving again a type this editor saved replaces it, as Edit does.
      const overwrite = _editorMode === 'edit' || _editPrefill?.full_name === fullName;
      await requestJson('/api/dsdl/custom/type', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          namespace, type_name: typeName, version,
          source_text: source, fixed_port_id: portId, overwrite,
        }),
      });

      _editPrefill = {
        namespace,
        type_name: typeName,
        version,
        source_text: source,
        fixed_port_id: portId,
        full_name: fullName,
      };

      _editorDirty = false;
      _showEditorStatus('Saved', false);
      await _tab.showSaved(fullName);
    } catch (err) {
      _showEditorStatus(err.message, true);
    }
  };

  const _lockEditorIfCompiled = () => {
    if (!_editorOpen || !_editPrefill?.full_name) return;
    const fresh = _tab.typeIndex()[_editPrefill.full_name];
    if (!fresh || !fresh.compiled) return;

    const panel = document.getElementById('dsdlEditorPanel');
    if (!panel || panel.classList.contains('dsdl-editor-locked')) return;
    panel.classList.add('dsdl-editor-locked');

    panel.querySelectorAll('input, textarea, select, button:not(.dsdl-editor-close)').forEach(el => {
      el.disabled = true;
    });

    const form = panel.querySelector('.dsdl-editor-form');
    if (form && !panel.querySelector('.dsdl-editor-locked-banner')) {
      const banner = document.createElement('div');
      banner.className = 'dsdl-editor-locked-banner';
      banner.textContent = `This type was compiled — editing is locked. ${_tab.lockedAdvice}`;
      panel.insertBefore(banner, form);
    }
  };

  // The editor's own word on the type in it; other errors are said where
  // they happen, in the tab (its dialogs, the compile errors).
  const _showEditorStatus = (msg, isError) => {
    const status = document.getElementById('dsdlEditorStatus');
    if (!status) return;
    status.textContent = msg;
    status.className = `dsdl-editor-status ${isError ? 'dsdl-editor-error' : 'dsdl-editor-ok'}`;
    setTimeout(() => { if (status.textContent === msg) status.textContent = ''; }, 5000);
  };

  const _initEditorDrag = () => {
    const handle = document.getElementById('dsdlEditorHandle');
    const area = document.getElementById('dsdlDetailArea');
    if (!handle || !area) return;

    let dragging = false;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('dragging');
      document.body.classList.add('dsdl-resizing-col');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // The editor's share of the area, kept on it for the next editor too.
    const onMove = (e) => {
      if (!dragging) return;
      const rect = area.getBoundingClientRect();
      const share = Math.max(0.15, Math.min(0.85, (e.clientX - rect.left) / rect.width));
      area.style.setProperty('--dsdl-editor-share', share);
    };

    const onUp = () => {
      dragging = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.classList.remove('dsdl-resizing-col');
    };
  };

  // Closed when the type it edits is deleted.
  const closeIfEditing = (fullName) => {
    if (_editorOpen && _editorMode === 'edit' && _editPrefill?.full_name === fullName) {
      _closeEditor();
      _editorMode = 'new';
      _editPrefill = null;
    }
  };

  return {
    attach,
    openNew: _openEditorNew,
    openEdit: _openEditorEdit,
    openNewVersion: _openEditorNewVersion,
    closeIfEditing,
    lockIfCompiled: _lockEditorIfCompiled,
  };
})();
