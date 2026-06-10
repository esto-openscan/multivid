const state = {
  nodes: [],
  dashboard: null,
  busy: false,
  refreshTimer: null,
  snapshotTimers: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `HTTP ${response.status}`);
  }
  return data;
}

function setBusy(isBusy) {
  state.busy = isBusy;
  $$('button').forEach((button) => { button.disabled = isBusy; });
}

function value(id) {
  return $(id).value.trim();
}

function selectedProfile() {
  const manual = value('#profile-manual');
  return manual || value('#profile-select');
}

function requireValue(label, actual) {
  if (!actual) throw new Error(`${label} is required`);
  return actual;
}

function selectedOverlays() {
  return $$('.overlays input[type="checkbox"]:checked').map((input) => input.value);
}

async function loadNodes() {
  const data = await api('/api/nodes');
  state.dashboard = data.dashboard;
  $('#node-count').textContent = data.node_count;
  $('#config-path').textContent = `config: ${data.config_path}`;
  state.nodes = data.nodes;
  renderConfiguredNodes(data.nodes);
  applyDashboardDefaults(data.dashboard);
}

function applyDashboardDefaults(config) {
  if (!config || !config.positioning) return;
  const defaults = new Set(config.positioning.overlays || []);
  $$('.overlays input[type="checkbox"]').forEach((input) => {
    input.checked = defaults.has(input.value);
  });
}

async function loadProfiles() {
  const data = await api('/api/profiles');
  const select = $('#profile-select');
  select.innerHTML = '';
  for (const name of data.profile_names || []) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  $('#profile-warning').textContent = data.warning || '';
  select.disabled = !data.compatible || !data.profile_names || data.profile_names.length === 0;
}

async function refreshStatus() {
  const data = await api('/api/status');
  renderOperation(data, false);
  renderNodes(data.nodes || []);
  $('#last-refresh').textContent = `refreshed: ${new Date().toLocaleTimeString()}`;
}

async function runOperation(name, path, bodyBuilder) {
  try {
    setBusy(true);
    const body = bodyBuilder ? bodyBuilder() : undefined;
    const data = await api(path, body ? { method: 'POST', body: JSON.stringify(body) } : { method: 'POST' });
    renderOperation(data, true);
    renderNodes(data.nodes || []);
    await refreshStatus();
  } catch (error) {
    renderError(name, error.message);
  } finally {
    setBusy(false);
  }
}

function renderConfiguredNodes(nodes) {
  renderNodes(nodes.map((node) => ({
    node,
    ok: false,
    online: false,
    state: 'offline',
    message: `${node.name}: not checked yet`,
  })));
}

function renderNodes(nodes) {
  state.nodes = nodes;
  clearSnapshotTimers();
  const grid = $('#node-grid');
  grid.innerHTML = '';
  const template = $('#node-card-template');
  for (const item of nodes) {
    const card = template.content.cloneNode(true);
    const node = item.node || {};
    const stateName = item.state || (item.online ? 'online' : 'offline');
    card.querySelector('.node-title').textContent = node.name || 'unknown node';
    card.querySelector('.node-meta').textContent = `${node.camera_id || '-'} | ${node.base_url || '-'}`;
    const pill = card.querySelector('.state-pill');
    pill.textContent = stateName;
    pill.classList.add(`state-${String(stateName).replace(/_/g, '-')}`);

    const previewWrap = card.querySelector('.preview-wrap');
    const preview = card.querySelector('.preview');
    if (item.positioning_running && item.stream_url) {
      preview.src = item.stream_url;
      previewWrap.classList.add('active');
    } else if (item.positioning_running && item.snapshot_url) {
      preview.src = withCacheBust(item.snapshot_url);
      previewWrap.classList.add('active');
      const timer = window.setInterval(() => { preview.src = withCacheBust(item.snapshot_url); }, 1000);
      state.snapshotTimers.set(node.name, timer);
    }

    card.querySelector('.facts').innerHTML = facts({
      Online: item.online ? 'yes' : 'no',
      State: stateName,
      Session: item.current_session_id || null,
      Take: item.current_take_id || null,
      Profile: item.current_profile || item.prepared_profile || null,
      Positioning: item.positioning_running ? 'yes' : 'no',
      Recording: item.recording_running ? 'yes' : 'no',
      Calibrating: item.calibration_running ? 'yes' : 'no',
      Prepared: item.prepared_valid ? 'yes' : 'no',
    });

    const links = [];
    if (item.snapshot_url) links.push(`<a href="${escapeAttr(item.snapshot_url)}" target="_blank" rel="noreferrer">snapshot</a>`);
    if (item.stream_url) links.push(`<a href="${escapeAttr(item.stream_url)}" target="_blank" rel="noreferrer">stream</a>`);
    card.querySelector('.links').innerHTML = links.join('');

    const output = item.output_path ? `Output: ${item.output_path}` : '';
    const still = stillSummary(item.last_still_capture);
    const calibration = calibrationSummary(item.last_calibration);
    const warnings = (item.warnings || []).length ? `Warnings: ${(item.warnings || []).join('; ')}` : '';
    card.querySelector('.output').textContent = output;
    card.querySelector('.last-still').textContent = still;
    card.querySelector('.last-calibration').textContent = calibration;
    card.querySelector('.warnings').textContent = warnings;
    card.querySelector('.error').textContent = item.error || item.last_error ? `Error: ${item.error || item.last_error}` : '';
    card.querySelector('.node-details').hidden = !output && !still && !calibration && !warnings;
    grid.appendChild(card);
  }
}

function facts(values) {
  return Object.entries(values)
    .filter(([, val]) => val !== null && val !== undefined && val !== '')
    .map(([key, val]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(String(val))}</dd></div>`)
    .join('');
}

function renderOperation(data, showSummary) {
  if (!showSummary) return;
  $('#operation-summary').textContent = `${data.operation}: ${data.success_count}/${data.node_count} succeeded`;
  const list = $('#operation-results');
  list.innerHTML = '';
  for (const node of data.nodes || []) {
    const li = document.createElement('li');
    li.textContent = operationLine(node);
    list.appendChild(li);
  }
}

function operationLine(node) {
  const parts = [node.message || `${node.node.name}: ${node.ok ? 'ok' : 'failed'}`];
  const still = node.last_still_capture;
  if (still) {
    if (still.label) parts.push(`label=${still.label}`);
    if (still.image_file_path) parts.push(`image=${still.image_file_path}`);
    if (still.warnings && still.warnings.length) parts.push(`warnings=${still.warnings.join('; ')}`);
  }
  const calibration = node.last_calibration;
  if (calibration) {
    if (calibration.calibration_id) parts.push(`calibration=${calibration.calibration_id}`);
    if (calibration.suggested_controls_path) parts.push(`suggestions=${calibration.suggested_controls_path}`);
    if (calibration.confidence) parts.push(`confidence=${calibration.confidence}`);
    if (calibration.warnings && calibration.warnings.length) parts.push(`warnings=${calibration.warnings.join('; ')}`);
  }
  if (node.output_path) parts.push(`output=${node.output_path}`);
  return parts.join(' | ');
}

function renderError(operation, message) {
  $('#operation-summary').textContent = `${operation}: failed before node requests`;
  $('#operation-results').innerHTML = `<li>${escapeHtml(message)}</li>`;
}

function stillSummary(still) {
  if (!still) return '';
  const label = still.label || '-';
  const status = still.status || '-';
  const path = still.image_file_path || still.image_file_name || '';
  return `Last still: ${label} (${status}) ${path}`;
}

function calibrationSummary(calibration) {
  if (!calibration) return '';
  const id = calibration.calibration_id || '-';
  const status = calibration.status || '-';
  const confidence = calibration.confidence || (calibration.suggested_controls && calibration.suggested_controls.confidence) || '-';
  return `Last calibration: ${id} (${status}, confidence ${confidence})`;
}

function clearSnapshotTimers() {
  for (const timer of state.snapshotTimers.values()) window.clearInterval(timer);
  state.snapshotTimers.clear();
}

function withCacheBust(url) {
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}t=${Date.now()}`;
}

function escapeHtml(value) {
  return value.replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]));
}

function escapeAttr(value) {
  return escapeHtml(String(value));
}

function wireButtons() {
  $('#refresh-status').addEventListener('click', () => runSafeRefresh());
  $('#start-positioning').addEventListener('click', () => runOperation('Start Positioning', '/api/positioning/start', () => ({
    overlays: selectedOverlays(),
    profile: selectedProfile() || null,
  })));
  $('#stop-positioning').addEventListener('click', () => runOperation('Stop Positioning', '/api/positioning/stop'));
  $('#capture-stills').addEventListener('click', () => runOperation('Capture Reference Stills', '/api/stills/capture', () => ({
    session_id: requireValue('Session ID', value('#session-id')),
    label: value('#still-label') || null,
    profile: selectedProfile() || null,
  })));
  $('#run-calibration').addEventListener('click', () => runOperation('Run Calibration', '/api/calibration/run', () => ({
    session_id: requireValue('Session ID', value('#session-id')),
    profile: requireValue('Profile', selectedProfile()),
    duration_seconds: value('#calibration-duration') ? Number(value('#calibration-duration')) : null,
  })));
  $('#start-recording').addEventListener('click', () => runOperation('Start Recording', '/api/recordings/start', () => ({
    session_id: requireValue('Session ID', value('#session-id')),
    profile: requireValue('Profile', selectedProfile()),
    take_id: value('#take-id') || null,
  })));
  $('#stop-recording').addEventListener('click', () => runOperation('Stop Recording', '/api/recordings/stop'));
}

async function runSafeRefresh() {
  try {
    setBusy(true);
    await refreshStatus();
  } catch (error) {
    renderError('Refresh Status', error.message);
  } finally {
    setBusy(false);
  }
}

async function boot() {
  wireButtons();
  try {
    await loadNodes();
    await loadProfiles();
    await refreshStatus();
    const seconds = (state.dashboard && state.dashboard.status_refresh_seconds) || 3;
    state.refreshTimer = window.setInterval(() => {
      if (!state.busy) refreshStatus().catch((error) => renderError('Auto Refresh', error.message));
    }, seconds * 1000);
  } catch (error) {
    renderError('Startup', error.message);
  }
}

boot();
