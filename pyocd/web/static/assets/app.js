/* pyOCD web UI.
 *
 * Layout: a session bar (MCU, connect mode, GDB, options, connect) that turns into
 * live target controls once connected; Home / Flash / Debug / Settings views;
 * a bottom dock with the log, console and SWD traffic; and a drawer holding
 * every connection option.
 */
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const GDB_PORT = 3030;
const webClientId = crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`;

const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
};

let state = {state: 'disconnected', connected: false, jobs: [], artifacts: []};
let savedProfile = {};
let connectionDefaults = {frequency: 1000000, connect_mode: 'under-reset', dap_protocol: 'default'};
let targetCatalog = null;
let packSequenceInfo = null;
let token = localStorage.getItem('pyocd-token') || '';
let recentTargets = store.get('pyocd-recent-targets', []);
let recentSetups = store.get('pyocd-recent-setups', []);
let selectedArtifact = null;
let attachedElf = '';
let lastDebugState = 'inactive';
let selectedFrame = 0;
let variableMode = 'locals';
let dumpActivity = null;
let lastConnectionError = '';

/* ---------- Formatting ---------- */

function fmtClock(hz) {
  if (!hz) return '—';
  if (hz >= 1e6) return `${+(hz / 1e6).toFixed(2)} MHz`;
  return `${+(hz / 1e3).toFixed(0)} kHz`;
}
function fmtBytes(n) {
  if (n == null) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${units[i]}`;
}
function fmtUptime(seconds) {
  seconds = Math.floor(seconds || 0);
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  return [d && d + 'd', h && h + 'h', m + 'm'].filter(Boolean).join(' ');
}
const hex32 = v => '0x' + v.toString(16).padStart(8, '0');
const timeOf = t => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const modeLabels = {'halt': 'Halt on connect', 'attach': 'Attach', 'under-reset': 'Under reset', 'pre-reset': 'Pre-reset'};

/* ---------- API and waiting overlay ---------- */

const headers = () => ({'Content-Type': 'application/json', 'X-pyOCD-CSRF': '1', 'X-pyOCD-Client-ID': webClientId, ...(token ? {Authorization: `Bearer ${token}`} : {})});
let waitingCount = 0, waitingTimer = null, waitingRestartTimer = null;

function waitingMessage(path) {
  const map = [['/session/connect', 'Connecting to target…'], ['/session/disconnect', 'Disconnecting…'], ['/probe/reset', 'Pulsing reset pin…'],
    ['/elf/attach', 'Attaching ELF symbols…'], ['/gdb/', 'Waiting for GDB…'], ['/debug/', 'Waiting for debugger…'], ['/jobs/program', 'Starting programming…'],
    ['/jobs/verify', 'Starting verification…'], ['/jobs/erase', 'Starting erase…'], ['/memory/', 'Reading target…'], ['/cores/', 'Reading target…'], ['/packs/', 'Updating device support…']];
  return map.find(([key]) => path.includes(key))?.[1] || 'Waiting for pyOCD…';
}
function beginWaiting(message) {
  waitingCount++;
  $('#waitingTitle').textContent = message;
  clearTimeout(waitingTimer);
  waitingTimer = setTimeout(() => { if (waitingCount) $('#waitingOverlay').hidden = false; }, 300);
  clearTimeout(waitingRestartTimer);
  waitingRestartTimer = setTimeout(() => { $('#waitingRestart').hidden = false; }, 8000);
}
function endWaiting() {
  waitingCount = Math.max(0, waitingCount - 1);
  if (waitingCount) return;
  clearTimeout(waitingTimer);
  clearTimeout(waitingRestartTimer);
  $('#waitingOverlay').hidden = true;
  $('#waitingRestart').hidden = true;
}
async function withWaiting(message, fn) {
  beginWaiting(message);
  try { return await fn(); } finally { endWaiting(); }
}

async function api(path, opts = {}) {
  const {waiting, timeout, ...request} = opts;
  const foreground = waiting !== false;
  const controller = new AbortController();
  const limit = timeout || ((path.includes('/gdb/') || path.includes('/debug/') || path.includes('/session/disconnect')) ? 12000 : 0);
  const timer = limit ? setTimeout(() => controller.abort(), limit) : null;
  if (foreground) beginWaiting(typeof waiting === 'string' ? waiting : waitingMessage(path));
  try {
    const response = await fetch('/api/v1' + path, {...request, signal: controller.signal, headers: {...headers(), ...(request.headers || {})}});
    if (response.status === 401) {
      const entered = prompt('Password / authentication token');
      if (entered) {
        token = entered;
        localStorage.setItem('pyocd-token', entered);
        return api(path, {...opts, waiting: false});
      }
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error?.message || response.statusText);
    return data;
  } catch (error) {
    if (error.name === 'AbortError' && limit) {
      restartPyocd(true);
      throw Error('Target operation timed out; restarting pyOCD…');
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (foreground) endWaiting();
  }
}
const post = (path, body = {}, extra = {}) => api(path, {method: 'POST', body: JSON.stringify(body), ...extra});

async function restartPyocd(automatic = false) {
  if (restartPyocd.running) return;
  if (!automatic && !confirm('Restart the pyOCD process? Active debug sessions will end.')) return;
  restartPyocd.running = true;
  beginWaiting(automatic ? 'pyOCD stopped responding. Restarting…' : 'Restarting pyOCD…');
  await fetch('/api/v1/runtime/restart', {method: 'POST', headers: headers(), body: '{}'}).catch(() => {});
  setTimeout(waitForPyocd, 700);
}
async function waitForPyocd() {
  try {
    const response = await fetch('/api/v1/health', {cache: 'no-store'});
    if (response.ok) return location.reload();
  } catch {}
  setTimeout(waitForPyocd, 700);
}

/* ---------- Toasts ---------- */

function toast(message, error = false, action = null) {
  const host = $('#toast'), key = (error ? 'e:' : 'i:') + message;
  if ([...host.children].some(x => x.dataset.key === key)) return;
  const node = document.createElement('div');
  node.className = 'toast' + (error ? ' error' : '');
  node.dataset.key = key;
  node.setAttribute('role', error ? 'alert' : 'status');
  node.innerHTML = `<span class="copy"></span>${action ? '<button class="act"></button>' : ''}<button class="close" aria-label="Dismiss"><svg class="i"><use href="#i-x"/></svg></button>`;
  node.querySelector('.copy').textContent = message;
  if (action) {
    const button = node.querySelector('.act');
    button.textContent = action.label;
    button.onclick = () => { node.remove(); action.run(); };
  }
  node.querySelector('.close').onclick = () => node.remove();
  host.append(node);
  while (host.children.length > 4) host.firstElementChild.remove();
  let timer;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(() => node.remove(), error || action ? 12000 : 6000); };
  node.onmouseenter = () => clearTimeout(timer);
  node.onmouseleave = schedule;
  schedule();
}
const fail = error => toast(error.message || String(error), true);

/* ---------- Connection options ---------- */

// Every option the drawer edits. `section` places it in the profile sent to
// the server; `adapter` limits it to one probe kind.
const FIELDS = [
  {id: 'protocol', key: 'dap_protocol', def: () => connectionDefaults.dap_protocol},
  {id: 'connectMode', key: 'connect_mode', def: () => connectionDefaults.connect_mode, inBar: true},
  {id: 'resetMethod', key: 'reset_method', def: 'hardware'},
  {id: 'resumeOnDisconnect', section: 'options', key: 'resume_on_disconnect', def: true},
  {id: 'swjEnable', section: 'options', key: 'dap_swj_enable', def: true},
  {id: 'swjDormant', section: 'options', key: 'dap_swj_use_dormant', def: false},
  {id: 'resetHoldTime', section: 'options', key: 'reset.hold_time', type: 'number', def: 0.1},
  {id: 'resetPostDelay', section: 'options', key: 'reset.post_delay', type: 'number', def: 0.1},
  {id: 'eraseMode', section: 'options', key: 'chip_erase', def: 'sector'},
  {id: 'smartFlash', section: 'options', key: 'smart_flash', def: true},
  {id: 'trustCrc', section: 'options', key: 'fast_program', def: true},
  {id: 'keepUnwritten', section: 'options', key: 'keep_unwritten', def: false},
  {id: 'autoUnlock', section: 'options', key: 'auto_unlock', def: true},
  {id: 'packSequencesEnable', section: 'options', key: 'pack.debug_sequences.enable', def: true},
  {id: 'cmsisPreferV1', section: 'options', key: 'cmsis_dap.prefer_v1', def: false, adapter: 'cmsis-dap'},
  {id: 'cmsisDeferred', section: 'options', key: 'cmsis_dap.deferred_transfers', def: true, adapter: 'cmsis-dap'},
  {id: 'cmsisLimitPackets', section: 'options', key: 'cmsis_dap.limit_packets', def: false, adapter: 'cmsis-dap'},
  {id: 'jlinkPower', section: 'options', key: 'jlink.power', def: true, adapter: 'jlink'},
  {id: 'stlinkPrescaler', section: 'options', key: 'stlink.v3_prescaler', type: 'number', def: 1, adapter: 'stlink'},
  {id: 'swclk', section: 'gpio', key: 'swclk', type: 'number', def: 20, adapter: 'rpi'},
  {id: 'swdio', section: 'gpio', key: 'swdio', type: 'number', def: 21, adapter: 'rpi'},
  {id: 'nreset', section: 'gpio', key: 'nreset', type: 'optional', def: 16, adapter: 'rpi'},
  {id: 'swdioDir', section: 'gpio', key: 'swdio_dir', type: 'optional', def: undefined, adapter: 'rpi'},
  {id: 'gpioWaitRetries', section: 'gpio', key: 'wait_retries', type: 'number', def: 50, adapter: 'rpi'},
  {id: 'restorePins', section: 'gpio', key: 'restore_pins', def: true, adapter: 'rpi'},
];
const fieldDefault = f => typeof f.def === 'function' ? f.def() : f.def;
function readField(f) {
  const el = $('#' + f.id);
  if (el.type === 'checkbox') return el.checked;
  if (f.type === 'number') return +el.value;
  if (f.type === 'optional') return el.value === '' ? undefined : +el.value;
  return el.value;
}
function writeField(f, value) {
  const el = $('#' + f.id);
  if (el.type === 'checkbox') el.checked = !!value;
  else el.value = value ?? '';
}
const probeKindNow = () => $('#probeSelect').selectedOptions[0]?.dataset.kind || 'generic';
const fieldApplies = f => !f.adapter || f.adapter === probeKindNow();

function connectionProfile() {
  const profile = {
    probe: $('#probeSelect').value || null,
    target_override: $('#targetSelect').value.trim() || null,
    frequency: +$('#frequency').value,
    options: {}, gpio: {},
    interface_name: savedProfile.interface_name || '',
  };
  for (const f of FIELDS) {
    if (!fieldApplies(f)) continue;
    const value = readField(f);
    if (value === undefined) continue;
    if (f.section) profile[f.section][f.key] = value;
    else profile[f.key] = value;
  }
  return profile;
}
function applyProfile(profile) {
  $('#targetSelect').value = profile.target_override || '';
  $('#frequency').value = profile.frequency ?? connectionDefaults.frequency;
  for (const f of FIELDS) {
    const stored = f.section ? profile[f.section]?.[f.key] : profile[f.key];
    writeField(f, stored ?? fieldDefault(f));
  }
  updateClock();
  renderPins();
}
async function saveConfig() {
  savedProfile = {...savedProfile, ...connectionProfile()};
  updateOptionsBadge();
  try {
    await api('/config', {method: 'PUT', body: JSON.stringify(savedProfile), waiting: false});
    if (packSequenceInfo?.target !== savedProfile.target_override) await loadPackSequenceInfo();
  } catch (error) { fail(error); }
}
// Counts drawer settings that differ from their defaults. Fields shown in the
// session bar are already visible, so they don't count.
function updateOptionsBadge() {
  const clockChanged = +$('#frequency').value !== +connectionDefaults.frequency;
  $('#frequency').closest('label').classList.toggle('is-modified', clockChanged);
  let count = clockChanged ? 1 : 0;
  for (const f of FIELDS) {
    if (f.inBar) continue;
    const el = $('#' + f.id), label = el.closest('label');
    const changed = fieldApplies(f) && String(readField(f) ?? '') !== String(fieldDefault(f) ?? '');
    label?.classList.toggle('is-modified', changed);
    if (changed) count++;
  }
  $('#optionsBadge').hidden = !count;
  $('#optionsBadge').textContent = count;
  $('#optionsBtn').title = count ? `Connection options · ${count} changed from default` : 'Connection options';
  $('#unlockCaution').hidden = !$('#autoUnlock').checked || state.connected;
}
async function restoreDefaults() {
  for (const f of FIELDS) if (!f.inBar) writeField(f, fieldDefault(f));
  $('#frequency').value = connectionDefaults.frequency;
  updateClock();
  renderPins();
  await saveConfig();
  toast('Connection options restored to defaults');
}

/* ---------- Probes ---------- */

let probeSignature = '';
function probeKind(probe) {
  const text = `${probe.vendor_name || ''} ${probe.product_name || ''} ${probe.info || ''}`.toLowerCase();
  if (probe.unique_id === 'rpi-gpio:') return 'rpi';
  if (text.includes('j-link') || text.includes('jlink')) return 'jlink';
  if (text.includes('st-link') || text.includes('stlink')) return 'stlink';
  if (text.includes('cmsis-dap') || text.includes('daplink')) return 'cmsis-dap';
  return 'generic';
}
function shortProbeNames(boards) {
  const names = boards.map(b => (b.info || b.product_name || b.unique_id).split(' · ')[0]);
  return boards.map((b, i) => names.filter(n => n === names[i]).length > 1 ? `${names[i]} …${b.unique_id.slice(-6)}` : names[i]);
}
async function loadDiscovery() {
  let boards;
  try { boards = (await api('/probes', {waiting: false})).boards || []; } catch (error) { return; }
  const signature = JSON.stringify(boards.map(b => [b.unique_id, b.info]));
  if (signature === probeSignature) return;
  probeSignature = signature;
  const previous = $('#probeSelect').value || savedProfile.probe;
  const short = shortProbeNames(boards);
  $('#probeSelect').innerHTML = (boards.length ? '' : '<option value="">No probe detected</option>') +
    boards.map(b => `<option value="${esc(b.unique_id)}" data-kind="${probeKind(b)}">${esc(b.info)} · ${esc(b.unique_id)}</option>`).join('');
  $('#probeQuick').innerHTML = boards.map((b, i) => `<option value="${esc(b.unique_id)}" title="${esc(b.info)} · ${esc(b.unique_id)}">${esc(short[i])}</option>`).join('');
  if (boards.some(b => b.unique_id === previous)) $('#probeSelect').value = previous;
  $('#probeQuick').value = $('#probeSelect').value;
  updateProbeFields();
  renderBar();
  renderRecentSetups();
}
function probeName(uid) {
  const option = [...$('#probeSelect').options].find(o => o.value === uid);
  return option ? option.textContent.split(' · ')[0] : '';
}
function updateProbeFields() {
  const kind = probeKindNow();
  const panels = {'cmsis-dap': '#cmsisDapOptions', jlink: '#jlinkOptions', stlink: '#stlinkOptions', rpi: '#rpiOptions'};
  for (const [name, selector] of Object.entries(panels)) $(selector).hidden = name !== kind;
  $('#probeResetBtn').disabled = !$('#probeSelect').value || state.connected;
  updateOptionsBadge();
}
function onProbeChanged(value) {
  $('#probeSelect').value = value;
  $('#probeQuick').value = value;
  updateProbeFields();
  saveConfig();
}

const physicalToBcm = {3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22, 16: 23, 18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0, 28: 1, 29: 5, 31: 6, 32: 12, 33: 13, 35: 19, 36: 16, 37: 26, 38: 20, 40: 21};
function renderPins() {
  const signals = new Map([[+$('#swclk').value, 'SWCLK'], [+$('#swdio').value, 'SWDIO']]);
  if ($('#nreset').value) signals.set(+$('#nreset').value, 'nRESET');
  let html = '';
  for (let pin = 1; pin <= 40; pin++) {
    const bcm = physicalToBcm[pin], signal = signals.get(bcm);
    const label = signal || ((pin === 1 || pin === 17) ? '3V3' : (pin === 2 || pin === 4) ? '5V' : [6, 9, 14, 20, 25, 30, 34, 39].includes(pin) ? 'GND' : bcm === undefined ? '' : `GPIO${bcm}`);
    html += `<span class="${signal ? 'hot' : ''}" title="Physical pin ${pin}">${pin % 2 ? `${label} ${pin}` : `${pin} ${label}`}</span>`;
  }
  $('#pins').innerHTML = html;
}

/* ---------- MCU picker ---------- */

let targetBeforeEditing = '', targetActiveIndex = -1;
const catalogSource = x => ({pack: 'CMSIS-Pack', 'cbuild-run': 'cbuild-run'}[x.source] || 'built-in');

async function loadTargets() {
  try {
    targetCatalog = (await api('/targets', {waiting: false})).targets || [];
    if (!$('#targetOptions').hidden) renderTargetOptions($('#targetSelect').value);
  } catch (error) { targetCatalog = targetCatalog || []; fail(error); }
}
function renderTargetOptions(query) {
  const list = $('#targetOptions');
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const option = (x, sub) => `<button type="button" class="target-option" role="option" data-target="${esc(x.name)}"><b>${esc(x.name)}</b><small>${esc(sub)}</small></button>`;
  let html = '';
  if (!terms.length && recentTargets.length) {
    html += '<div class="group">Recent</div>' + recentTargets.map(name => {
      const entry = targetCatalog?.find(x => x.name === name);
      return option({name}, entry ? `${entry.vendor || ''}` : '');
    }).join('');
  }
  if (targetCatalog === null) {
    html += '<div class="loading"><i class="spin"></i> Loading MCU list…</div>';
  } else {
    const exact = query.trim().toLowerCase();
    const matches = targetCatalog
      .filter(x => { const text = `${x.name} ${x.part_number || ''} ${x.vendor || ''}`.toLowerCase(); return terms.every(t => text.includes(t)); })
      .sort((a, b) => Number(b.name.toLowerCase() === exact) - Number(a.name.toLowerCase() === exact) || a.name.localeCompare(b.name))
      .slice(0, 80);
    if (terms.length || !recentTargets.length) {
      html += matches.map(x => option(x, `${x.vendor || 'Unknown vendor'} · ${catalogSource(x)}`)).join('');
      if (!matches.length) html += `<div class="loading">No MCU matches “${esc(query)}”.</div>`;
    } else {
      html += '<div class="group">Type to search all MCUs</div>';
    }
  }
  html += '<div class="menu-foot"><button type="button" data-open-packs><svg class="i"><use href="#i-plus"/></svg>Add MCU support…</button></div>';
  list.innerHTML = html;
  targetActiveIndex = -1;
  list.querySelectorAll('.target-option').forEach(button => button.onmousedown = e => { e.preventDefault(); selectTarget(button.dataset.target); });
  list.querySelector('[data-open-packs]').onmousedown = e => { e.preventDefault(); closeTargetMenu(); openPackDialog(); };
}
function openTargetMenu() {
  const input = $('#targetSelect');
  if (!$('#targetOptions').hidden) return;
  targetBeforeEditing = input.value;
  input.select();
  renderTargetOptions('');
  $('#targetOptions').hidden = false;
  input.setAttribute('aria-expanded', 'true');
}
function closeTargetMenu(restore = false) {
  const input = $('#targetSelect');
  $('#targetOptions').hidden = true;
  input.setAttribute('aria-expanded', 'false');
  if (restore || !input.value.trim()) input.value = targetBeforeEditing;
}
function moveTargetActive(delta) {
  const options = $$('#targetOptions .target-option');
  if (!options.length) return;
  targetActiveIndex = (targetActiveIndex + delta + options.length) % options.length;
  options.forEach((o, i) => o.classList.toggle('active', i === targetActiveIndex));
  options[targetActiveIndex].scrollIntoView({block: 'nearest'});
}
async function selectTarget(name) {
  $('#targetSelect').value = name;
  targetBeforeEditing = name;
  closeTargetMenu();
  $('#targetSelect').blur();
  rememberTarget(name);
  await saveConfig();
  renderBar();
}
function rememberTarget(name) {
  if (!name) return;
  recentTargets = [name, ...recentTargets.filter(x => x !== name)].slice(0, 6);
  store.set('pyocd-recent-targets', recentTargets);
}

/* ---------- Clock ---------- */

function updateClock() {
  const hz = +$('#frequency').value;
  $('#frequencyOut').value = fmtClock(hz);
  $$('[data-clock]').forEach(b => b.classList.toggle('active', +b.dataset.clock === hz));
}

/* ---------- Recent setups ---------- */

function rememberSetup(profile) {
  const setup = {
    target: profile.target_override, probe: profile.probe, probeName: probeName(profile.probe),
    frequency: profile.frequency, protocol: profile.dap_protocol, connectMode: profile.connect_mode,
  };
  recentSetups = [setup, ...recentSetups.filter(x => !(x.target === setup.target && x.probe === setup.probe))].slice(0, 6);
  store.set('pyocd-recent-setups', recentSetups);
}
function renderRecentSetups() {
  const box = $('#recentSetups');
  const attached = new Set([...$('#probeSelect').options].map(o => o.value).filter(Boolean));
  box.innerHTML = recentSetups.map((s, i) => {
    const probeNote = !s.probe ? 'Any probe' : attached.has(s.probe) ? s.probeName || 'Probe' : `${s.probeName || 'Probe'} (not attached)`;
    const detail = [probeNote, fmtClock(s.frequency), s.protocol && s.protocol !== 'default' ? s.protocol.toUpperCase() : null, modeLabels[s.connectMode]].filter(Boolean).join(' · ');
    return `<div class="row"><div class="main"><b>${esc(s.target)}</b><small>${esc(detail)}</small></div>` +
      `<button data-setup="${i}" class="${i === 0 ? 'primary' : ''}">Connect</button>` +
      `<button data-forget="${i}" class="icon-btn" aria-label="Forget this setup" title="Forget"><svg class="i"><use href="#i-x"/></svg></button></div>`;
  }).join('');
  box.querySelectorAll('[data-setup]').forEach(b => b.onclick = () => applySetup(recentSetups[+b.dataset.setup]));
  box.querySelectorAll('[data-forget]').forEach(b => b.onclick = () => {
    recentSetups.splice(+b.dataset.forget, 1);
    store.set('pyocd-recent-setups', recentSetups);
    renderRecentSetups();
  });
  $('#recentSetups').previousElementSibling.hidden = !recentSetups.length;
  $('#homeHint').hidden = recentSetups.length > 0;
}
async function applySetup(setup) {
  $('#targetSelect').value = setup.target;
  if ([...$('#probeSelect').options].some(o => o.value === setup.probe)) onProbeChanged(setup.probe);
  if (setup.frequency) $('#frequency').value = setup.frequency;
  if (setup.protocol) $('#protocol').value = setup.protocol;
  if (setup.connectMode) $('#connectMode').value = setup.connectMode;
  updateClock();
  await saveConfig();
  connectWithGdb().catch(fail);
}

/* ---------- Connect, disconnect, GDB ---------- */

const gdbOnConnect = () => store.get('pyocd-auto-start-gdb', false) === true;

async function connectTarget() {
  const profile = connectionProfile(), target = profile.target_override;
  if (!target || target === 'cortex_m') {
    $('#targetSelect').focus();
    throw Error('Choose a target MCU first');
  }
  await loadPackSequenceInfo();
  await post('/session/connect', profile, {waiting: `Connecting to ${target}…`});
  savedProfile = profile;
  rememberTarget(target);
  rememberSetup(profile);
  renderRecentSetups();
  toast(`Connected to ${target}`);
  await refresh();
}

// auto-gdb:begin
async function connectWithGdb() {
  await connectTarget();
  if (!gdbOnConnect()) return;
  try {
    await api('/gdb/start', {method: 'POST', body: JSON.stringify({port: GDB_PORT})});
    toast('GDB server started');
  } catch (error) {
    toast(`Target connected, but GDB could not start: ${error.message}`, true);
  }
  await refresh();
}
// auto-gdb:end

async function disconnect() {
  try { await post('/session/disconnect'); } catch (error) { fail(error); }
  await refresh();
}
async function toggleGdb(enable) {
  store.set('pyocd-auto-start-gdb', enable);
  if (!state.connected) { renderBar(); return; }
  try {
    await post(enable ? '/gdb/start' : '/gdb/stop', {port: GDB_PORT});
    toast(enable ? `GDB server listening on port ${GDB_PORT}` : 'GDB server stopped');
  } catch (error) { fail(error); }
  await refresh();
}
const gdbEndpoint = () => `${location.hostname || 'localhost'}:${state.gdb?.[0]?.port || GDB_PORT}`;

/* ---------- Target control ---------- */

const isDirect = () => state.connected && !(state.gdb || []).length && state.state !== 'busy';
const browserDebugger = () => state.debugger?.owner === 'browser' && state.debugger?.active;
const coreState = () => (browserDebugger() ? state.debugger.state : state.target?.state) || '';

async function action(name) {
  try {
    const selected = name === 'reset-halt' ? `reset-halt-${$('#resetMethod').value}` : name;
    await post('/target/' + selected);
  } catch (error) { fail(error); }
  await refresh();
}
async function resetTarget(mode) {
  if (mode === 'halt') return action('reset-halt');
  const hardware = $('#resetMethod').value === 'hardware';
  try {
    await post(hardware ? '/target/reset-hardware' : '/target/reset');
    toast(hardware ? 'Reset by pin, running' : 'Core reset, running');
  } catch (error) { fail(error); }
  await refresh();
}
async function pulseResetPin() {
  try {
    await post('/probe/reset', connectionProfile());
    toast('Reset pin pulsed');
  } catch (error) { fail(error); }
}

/* ---------- Rendering ---------- */

function render(snapshot) {
  state = snapshot;
  if (snapshot.connection_defaults) connectionDefaults = snapshot.connection_defaults;
  if (snapshot.error && snapshot.error !== lastConnectionError) toast(snapshot.error, true);
  lastConnectionError = snapshot.error || '';
  renderBar();
  renderHome();
  renderArtifacts();
  renderProgress();
  renderFlashLog();
  renderDebug();
  updateDumpInputs();
  updateRecoveryState();
  updatePackSequenceRunState();
  updateOptionsBadge();
  renderSwdSummary(snapshot.swd_traffic || {});
  const others = snapshot.web_clients?.others || 0;
  $('#otherUsers').hidden = !others;
  $('#otherUsers').textContent = others === 1 ? 'Another user is connected' : `${others} other users connected`;
}

function renderBar() {
  const s = state, connected = !!s.connected, busy = s.state === 'busy';
  const gdb = s.gdb || [], browser = browserDebugger(), boards = [...$('#probeSelect').options].filter(o => o.value);
  $('#barSetup').hidden = connected;
  $('#barLive').hidden = !connected;
  $('#probeQuickWrap').hidden = connected || boards.length < 2;
  $('#probeMissing').hidden = connected || boards.length > 0;

  const button = $('#connectBtn');
  button.textContent = connected ? 'Disconnect' : busy ? 'Connecting…' : s.state === 'error' ? 'Retry' : 'Connect';
  button.className = 'connect-btn ' + (connected ? '' : s.state === 'error' ? 'error' : 'primary');
  button.disabled = busy && !connected;
  const target = $('#targetSelect').value.trim();
  button.title = connected ? 'Disconnect from the target' : target ? `Connect to ${target}${boards.length ? ' via ' + probeName($('#probeSelect').value) : ''}` : 'Choose an MCU first';

  if (connected) {
    const t = s.target || {}, p = s.probe || {}, run = coreState();
    $('#liveTarget').textContent = t.part_number || t.name || 'Target';
    $('#liveDetail').textContent = [p.protocol?.toUpperCase(), fmtClock(p.frequency)].filter(Boolean).join(' · ');
    $('#coreState').textContent = run || '—';
    $('#coreState').className = 'state-chip ' + run;
    const canControl = isDirect() || browser;
    const running = run === 'running';
    $('#haltRun').disabled = !canControl;
    $('#haltRun').title = canControl ? '' : 'Stop the GDB server to control the core from here';
    $('#haltRun').innerHTML = `<svg class="i"><use href="#i-${running ? 'pause' : 'play'}"/></svg><span>${running ? 'Halt' : 'Run'}</span>`;
    $('#resetBtn').disabled = $('#resetMenuBtn').disabled = !canControl;
    $('#targetFacts').innerHTML = factsHtml([
      ['Vendor', t.vendor], ['Target type', t.name], ['Probe', p.product], ['Serial', p.unique_id],
      ['Transport', p.protocol?.toUpperCase()], ['Clock', fmtClock(p.frequency)],
      ['Cores', t.cores ? `${t.cores.length} · using ${t.selected_core ?? 0}` : null], ['Security', t.locked === true ? 'Locked' : t.locked === false ? 'Unlocked' : 'Unknown'],
    ]);
  }

  const sw = $('#gdbSwitch'), detail = $('#gdbDetail'), gdbJob = (s.jobs || []).find(j => j.source === 'gdb' && j.state === 'running');
  const clients = gdb.reduce((sum, x) => sum + x.clients, 0);
  if (!connected) {
    sw.checked = gdbOnConnect();
    sw.disabled = false;
    detail.textContent = sw.checked ? 'on connect' : '';
    detail.className = 'gdb-detail';
  } else if (browser) {
    sw.checked = false;
    sw.disabled = true;
    detail.textContent = 'browser debugger';
    detail.className = 'gdb-detail';
  } else {
    sw.checked = gdb.length > 0;
    sw.disabled = busy && !gdb.length;
    detail.textContent = gdbJob ? `${gdbJob.message} ${Math.round(gdbJob.progress * 100)}%` : gdb.length ? `:${gdb[0].port}${clients ? ` · ${clients} client${clients > 1 ? 's' : ''}` : ''}` : '';
    detail.className = 'gdb-detail' + (clients ? ' live' : '');
  }
  $('#copyEndpoint').hidden = !(connected && gdb.length);

  const running = (s.jobs || []).find(j => j.state === 'running');
  $('#busyChip').hidden = !busy;
  $('#busyLabel').textContent = running ? `${running.message || running.kind} ${Math.round((running.progress || 0) * 100)}%` : 'Working…';
}

function factsHtml(rows) {
  return rows.filter(([, v]) => v != null && v !== '').map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
}
const regionTotal = type => (state.target?.memory_map || []).filter(r => r.type === type).reduce((sum, r) => sum + r.length, 0);

let mapSignature = '';
function renderHome() {
  const connected = !!state.connected;
  $('#homeOffline').hidden = connected;
  $('#homeOnline').hidden = !connected;
  if (!connected) return;
  const t = state.target || {}, p = state.probe || {};
  $('#homeTarget').textContent = t.part_number || t.name || 'Target';
  const security = $('#homeSecurity');
  security.textContent = t.locked === true ? 'Locked' : t.locked === false ? 'Unlocked' : 'Security unknown';
  security.className = 'tag ' + (t.locked === true ? 'bad' : t.locked === false ? 'ok' : '');
  $('#homeFacts').innerHTML = factsHtml([
    ['Vendor', t.vendor], ['Probe', p.product], ['Link', [p.protocol?.toUpperCase(), fmtClock(p.frequency)].filter(Boolean).join(' · ')],
    ['Cores', t.cores?.length], ['Flash', fmtBytes(regionTotal('flash'))], ['RAM', fmtBytes(regionTotal('ram'))],
  ]);
  const regions = t.memory_map || [], signature = JSON.stringify(regions);
  if (signature !== mapSignature) {
    mapSignature = signature;
    $('#memoryMap').innerHTML = regions.length ? '<thead><tr><th>Region</th><th>Type</th><th>Start</th><th>Size</th><th>Access</th></tr></thead><tbody>' +
      regions.map(r => `<tr><td>${esc(r.name || r.type)}</td><td>${esc(r.type)}</td><td class="mono">${hex32(r.start)}</td><td>${fmtBytes(r.length)}</td><td>${esc(r.access || '—')}</td></tr>`).join('') + '</tbody>' : '';
  }
  renderQuickFlash();
  const jobs = (state.jobs || []).slice(0, 6);
  $('#jobs').innerHTML = jobs.length ? jobs.map(j => {
    const tone = j.state === 'completed' ? 'ok' : j.state === 'failed' ? 'bad' : '';
    return `<div class="row"><div class="main"><b>${esc(j.kind)}</b><small>${esc(j.error || j.message)}</small></div><span class="tag ${tone}">${esc(j.state)}${j.state === 'running' ? ` ${Math.round(j.progress * 100)}%` : ''}</span></div>`;
  }).join('') : '<div class="empty">Nothing has run yet.</div>';
}

function renderQuickFlash() {
  const items = state.artifacts || [], artifact = items.find(a => a.id === $('#mainImage').value) || items[0];
  const box = $('#quickFlash');
  const gdbActive = (state.gdb || []).length || state.debugger?.active;
  let html;
  if (!artifact) {
    html = '<div class="main"><b>No firmware yet</b><small>Drop a file anywhere in this window</small></div><button data-browse>Browse…</button>';
  } else {
    const bin = artifact.name.toLowerCase().endsWith('.bin'), needsAddress = bin && !$('#mainOffset').value;
    const note = gdbActive ? 'Stop GDB to flash from here' : needsAddress ? 'BIN needs a load address' : fmtBytes(artifact.size);
    html = `<div class="main"><b>${esc(artifact.name)}</b><small>${esc(note)}</small></div>` +
      (needsAddress ? '<button data-view-link="flash">Set address</button>' : `<button class="primary" data-quick-flash="${esc(artifact.id)}" ${isDirect() ? '' : 'disabled'}>Flash</button>`);
  }
  if (box.dataset.html !== html) {
    box.dataset.html = html;
    box.innerHTML = html;
    box.querySelector('[data-browse]')?.addEventListener('click', () => $('#fileInput').click());
    box.querySelector('[data-quick-flash]')?.addEventListener('click', e => {
      $('#mainImage').value = e.currentTarget.dataset.quickFlash;
      $('#bootImage').value = '';
      toggleBinFields();
      submitPlan('program');
    });
    box.querySelector('[data-view-link]')?.addEventListener('click', () => showView('flash'));
  }
}

/* ---------- Flash ---------- */

let artifactSignature = '';
function renderArtifacts() {
  const items = state.artifacts || [];
  if (!selectedArtifact || !items.some(a => a.id === selectedArtifact)) selectedArtifact = items[0]?.id || null;
  const signature = JSON.stringify([items.map(a => [a.id, a.name, a.size]), selectedArtifact]);
  if (signature === artifactSignature) return;
  artifactSignature = signature;
  $('#artifactList').innerHTML = items.map(a => `<div class="row selectable ${a.id === selectedArtifact ? 'selected' : ''}" data-id="${esc(a.id)}">` +
    `<div class="main"><b>${esc(a.name)}</b><small>${fmtBytes(a.size)}</small></div>` +
    `<button class="icon-btn" data-delete aria-label="Delete ${esc(a.name)}" title="Delete"><svg class="i"><use href="#i-trash"/></svg></button></div>`).join('');
  $$('#artifactList .row').forEach(row => {
    row.onclick = () => { selectedArtifact = row.dataset.id; $('#mainImage').value = selectedArtifact; toggleBinFields(); renderArtifacts(); renderQuickFlash(); };
    row.querySelector('[data-delete]').onclick = async e => {
      e.stopPropagation();
      try { await api(`/artifacts/${encodeURIComponent(row.dataset.id)}`, {method: 'DELETE', waiting: 'Deleting…'}); } catch (error) { fail(error); }
      await refresh();
    };
  });
  const oldMain = $('#mainImage').value, oldBoot = $('#bootImage').value;
  const options = items.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  $('#mainImage').innerHTML = '<option value="">Select an image…</option>' + options;
  $('#bootImage').innerHTML = '<option value="">None</option>' + options;
  $('#mainImage').value = items.some(a => a.id === oldMain) ? oldMain : selectedArtifact || '';
  $('#bootImage').value = items.some(a => a.id === oldBoot) ? oldBoot : '';
  toggleBinFields();
}
const isBin = id => !!(state.artifacts || []).find(a => a.id === id)?.name.toLowerCase().endsWith('.bin');
function toggleBinFields() {
  $('#mainOffsetWrap').hidden = !isBin($('#mainImage').value);
  $('#bootOffsetWrap').hidden = !isBin($('#bootImage').value);
}
function updateFlashButtons() {
  const direct = isDirect();
  for (const id of ['#programBtn', '#verifyBtn', '#eraseBtn']) {
    $(id).disabled = !direct;
    $(id).title = direct ? '' : !state.connected ? 'Connect a target first' : (state.gdb || []).length ? 'Stop the GDB server first' : 'Busy';
  }
}

async function upload(file) {
  return withWaiting(`Uploading ${file.name}…`, async () => {
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/api/v1/artifacts', {method: 'POST', headers: {'X-pyOCD-CSRF': '1', 'X-pyOCD-Client-ID': webClientId, ...(token ? {Authorization: `Bearer ${token}`} : {})}, body: form});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error?.message || response.statusText);
    selectedArtifact = data.id;
    await refresh();
    $('#mainImage').value = data.id;
    toggleBinFields();
    return data;
  });
}
async function uploadFiles(files, offerFlash) {
  let last = null;
  for (const file of files) {
    try { last = await upload(file); } catch (error) { fail(error); }
  }
  if (!last) return;
  renderQuickFlash();
  const bin = last.name.toLowerCase().endsWith('.bin');
  if (offerFlash && isDirect() && !bin) toast(`${last.name} uploaded`, false, {label: 'Flash now', run: () => submitPlan('program')});
  else if (offerFlash && bin) toast(`${last.name} uploaded. Set its load address before flashing.`, false, {label: 'Open Flash', run: () => showView('flash')});
  else toast(`${last.name} uploaded`);
}

async function submitPlan(kind) {
  const main = $('#mainImage').value, boot = $('#bootImage').value;
  if (!main) return toast('Select an image first', true);
  $('#flashStatus').hidden = true;
  const images = [{artifact_id: main, base_address: $('#mainOffset').value || null}];
  if (boot) images.push({artifact_id: boot, base_address: $('#bootOffset').value || null});
  try {
    await post(`/jobs/${kind}`, {images, options: {post_action: $('#postAction').value, reset_method: $('#resetMethod').value}});
  } catch (error) { fail(error); }
  await refresh();
}

const flashJob = () => (state.jobs || []).filter(j => ['program', 'erase', 'verify'].includes(j.kind)).sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
function renderProgress() {
  updateFlashButtons();
  const job = flashJob();
  for (const box of [$('#progress'), $('#quickProgress')]) {
    if (!job) { box.hidden = true; continue; }
    const label = {erase: 'Erase', verify: 'Verify', program: 'Programming'}[job.kind];
    const done = job.state === 'completed', failed = job.state === 'failed';
    box.hidden = box.id === 'quickProgress' && (done || failed) && Date.now() / 1000 - (job.created_at || 0) > 60;
    box.className = 'progress' + (done ? ' ok' : failed ? ' bad' : '');
    box.querySelector('.progress-label').textContent = done ? `${label} complete` : failed ? `${label} failed` : job.message;
    box.querySelector('.progress-value').textContent = (done ? 100 : Math.round(job.progress * 100)) + '%';
    box.querySelector('progress').value = done ? 1 : job.progress || 0;
  }
  // The progress line already reports success; only failures need the detail.
  const status = $('#flashStatus');
  status.hidden = job?.state !== 'failed';
  if (!status.hidden) {
    status.className = 'status-line bad';
    status.textContent = job.error || job.message || `${job.kind} failed.`;
  }
}
function renderFlashLog() {
  const flash = flashJob();
  const job = dumpActivity && (!flash || dumpActivity.created_at > flash.created_at) ? dumpActivity : flash;
  const box = $('#flashJobLog');
  $('#flashLogState').textContent = job ? job.state : 'Idle';
  $('#flashLogState').className = 'tag ' + (job?.state === 'completed' ? 'ok' : job?.state === 'failed' ? 'bad' : '');
  const events = job?.events || [];
  const html = events.length ? events.map(e => `<div class="ev ${esc(e.level).toLowerCase()}"><time>${timeOf(e.time)}</time><b>${esc(e.level)}</b><span>${esc(e.message)}</span></div>`).join('')
    : `<div class="empty">${job ? 'No detailed messages were reported.' : 'Program, verify, erase and dump messages appear here.'}</div>`;
  if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; box.scrollTop = box.scrollHeight; }
}
function recordDump(message, stateName = 'running', level = 'INFO') {
  const now = Date.now() / 1000;
  if (!dumpActivity || dumpActivity.state !== 'running') dumpActivity = {kind: 'dump', state: stateName, created_at: now, events: []};
  dumpActivity.state = stateName;
  dumpActivity.events.push({time: now, level, message});
  renderFlashLog();
}
function confirmAction(title, text, confirmLabel) {
  const dialog = $('#confirmDialog');
  $('#confirmTitle').textContent = title;
  $('#confirmText').textContent = text;
  $('#confirmYes').textContent = confirmLabel;
  dialog.showModal();
  return new Promise(resolve => {
    $('#confirmNo').onclick = () => { dialog.close(); resolve(false); };
    $('#confirmYes').onclick = () => { dialog.close(); resolve(true); };
    dialog.oncancel = () => resolve(false);
  });
}
async function eraseChip() {
  if (!await confirmAction('Erase chip?', 'All flash contents will be erased. This can’t be undone.', 'Erase')) return;
  try { await post('/jobs/erase', {mode: 'chip'}); } catch (error) { fail(error); }
  await refresh();
}

function saveBlob(blob, name) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
async function downloadRange(address, length, name) {
  return withWaiting(`Downloading ${name}…`, async () => {
    const response = await fetch('/api/v1/memory/dump', {method: 'POST', headers: headers(), body: JSON.stringify({address, length})});
    if (!response.ok) { const d = await response.json().catch(() => ({})); throw Error(d.error?.message || 'Dump failed'); }
    saveBlob(await response.blob(), name);
  });
}
const readableRegions = preset => (state.target?.memory_map || []).filter(r => r.type === preset && (r.access || '').includes('r'));
function updateDumpInputs() {
  const preset = $('#dumpRegion').value, address = $('#dumpAddress'), length = $('#dumpLength'), hint = $('#dumpHint');
  const custom = preset === 'custom';
  address.readOnly = length.readOnly = !custom;
  $('#dumpRange').disabled = $('#dumpRegisters').disabled = !isDirect();
  if (custom) { hint.textContent = 'Custom dumps support up to 512 MiB.'; return; }
  const regions = readableRegions(preset);
  if (!regions.length) { address.value = length.value = ''; hint.textContent = `No readable ${preset.toUpperCase()} regions available.`; return; }
  const total = regions.reduce((sum, r) => sum + r.length, 0);
  address.value = hex32(Math.min(...regions.map(r => r.start)));
  length.value = hex32(total);
  hint.textContent = regions.length === 1 ? `${regions[0].name || preset} · ${fmtBytes(total)}` : `${regions.length} regions · ${fmtBytes(total)} · one file per region`;
}
async function dumpRange() {
  const preset = $('#dumpRegion').value;
  recordDump(`Starting ${preset} memory dump`);
  try {
    if (preset === 'custom') {
      const address = parseInt($('#dumpAddress').value, 0), length = parseInt($('#dumpLength').value, 0);
      recordDump(`Reading ${hex32(address)}, ${length} bytes`);
      await downloadRange(address, length, `memory-${address.toString(16)}-${length}.bin`);
    } else {
      const regions = readableRegions(preset);
      if (!regions.length) throw Error(`No readable ${preset.toUpperCase()} regions found`);
      for (const r of regions) {
        recordDump(`Reading ${r.name || preset} at ${hex32(r.start)} (${r.length} bytes)`);
        await downloadRange(r.start, r.length, `${preset}-${r.name || r.start.toString(16)}.bin`);
      }
    }
    recordDump('Memory dump completed', 'completed');
    toast('Memory dump downloaded');
  } catch (error) { recordDump(error.message, 'failed', 'ERROR'); fail(error); }
}
async function dumpRegisters() {
  recordDump('Reading core 0 registers');
  try {
    const data = await api('/cores/0/registers');
    const report = {captured_at: new Date().toISOString(), target: state.target, probe: state.probe, ...data};
    saveBlob(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}), `registers-${state.target?.name || 'target'}.json`);
    recordDump('Register dump downloaded', 'completed');
  } catch (error) { recordDump(error.message, 'failed', 'ERROR'); fail(error); }
}

/* ---------- Debug ---------- */

function renderDebug() {
  const dbg = state.debugger || {}, browser = browserDebugger(), external = dbg.owner === 'external';
  const direct = isDirect(), inspect = browser && dbg.state === 'stopped';
  const elf = dbg.executable || attachedElf;
  $('#elfName').textContent = elf ? elf.split(/[\\/]/).pop() : 'Load ELF…';
  $('.elf-chip').classList.toggle('loaded', !!elf);
  $('.elf-chip').classList.toggle('disabled', !direct);
  $('#browserDebug').disabled = !state.connected || external;
  $('#browserDebug').textContent = browser ? 'Stop debugger' : 'Start debugger';
  $('#browserDebug').className = browser ? '' : 'primary';
  $$('[data-action]').forEach(b => b.disabled = !(direct || browser));
  $('#readRegs').disabled = $('#readMemory').disabled = $('#dumpMemory').disabled = !(direct || inspect);
  $('#readStack').disabled = $('#refreshVariables').disabled = !inspect;
  $('#debugNotice').textContent = !state.connected ? 'Connect a target to debug.'
    : browser ? `GDB · ${dbg.state} · core ${dbg.core}`
    : external ? 'An external GDB server owns the target. Stop it to debug here.'
    : 'Load an ELF with debug info, then start the debugger.';
  if (browser && dbg.state === 'stopped' && lastDebugState !== 'stopped') setTimeout(refreshDebugWorkspace, 0);
  if (!browser && lastDebugState !== 'inactive') clearDebugWorkspace();
  lastDebugState = browser ? dbg.state : 'inactive';
  if (!browser && !$('#stackView').children.length) clearDebugWorkspace();
}
function clearDebugWorkspace() {
  $('#stackView').innerHTML = '<div class="empty">Frames appear when the debugger halts.</div>';
  $('#variablesView').innerHTML = '<div class="empty">Select a stack frame to inspect its variables.</div>';
  if (!$('#registers').children.length) $('#registers').innerHTML = '<div class="empty">Halt the target, then read registers.</div>';
  if (!$('#hexView').textContent) $('#hexView').textContent = 'Address    00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F   ASCII';
}
async function refreshDebugWorkspace() {
  if (!browserDebugger() || state.debugger.state !== 'stopped') return;
  await loadFrames();
  await Promise.all([readRegisters(), variableMode === 'locals' ? loadLocals() : null]);
}
async function readRegisters() {
  try {
    const data = await api('/cores/0/registers', {waiting: false});
    $('#registers').innerHTML = Object.entries(data.registers).filter(([, v]) => v !== '' && v != null)
      .map(([k, v]) => `<div class="reg"><span>${esc(k.toUpperCase())}</span><b>${typeof v === 'number' ? hex32(v) : esc(v)}</b></div>`).join('');
  } catch (error) { fail(error); }
}
function variableMarkup(v) {
  const expandable = Number(v.numchild) > 0 && v.handle;
  return `<div class="var"><button ${expandable ? `data-var="${esc(v.handle)}"` : 'tabindex="-1"'}>${expandable ? '▸ ' : ''}${esc(v.arg ? 'arg ' + v.name : v.name)}</button><span class="value">${esc(v.value)}</span><span class="type">${esc(v.type)}</span></div>`;
}
function bindVariableExpand(root) {
  root.querySelectorAll('[data-var]').forEach(button => button.onclick = async e => {
    e.stopPropagation();
    const row = button.closest('.var'), open = row.querySelector('.var-children');
    if (open) { open.remove(); button.firstChild.textContent = button.firstChild.textContent.replace('▾', '▸'); return; }
    try {
      const data = await api(`/debug/variables/${encodeURIComponent(button.dataset.var)}/children`, {waiting: false});
      const box = document.createElement('div');
      box.className = 'var-children';
      box.innerHTML = (data.variables || []).map(variableMarkup).join('') || '<div class="empty">No visible children.</div>';
      row.append(box);
      button.firstChild.textContent = button.firstChild.textContent.replace('▸', '▾');
      bindVariableExpand(box);
    } catch (error) { fail(error); }
  });
}
function showVariables(items) {
  const view = $('#variablesView');
  view.innerHTML = items.length ? items.map(variableMarkup).join('') : `<div class="empty">${variableMode === 'globals' ? 'No matching globals.' : 'No variables in this frame.'}</div>`;
  bindVariableExpand(view);
}
async function loadLocals() {
  try { showVariables((await api('/debug/variables/locals', {waiting: false})).variables || []); } catch (error) { fail(error); }
}
async function loadGlobals() {
  const q = $('#globalQuery').value.trim();
  try { showVariables((await api(`/debug/variables/globals?q=${encodeURIComponent(q)}&limit=50`)).variables || []); } catch (error) { fail(error); }
}
async function loadFrames() {
  try {
    const frames = (await api('/debug/frames', {waiting: false})).frames || [];
    if (!frames.some(f => Number(f.level) === selectedFrame)) selectedFrame = Number(frames[0]?.level || 0);
    $('#stackView').innerHTML = frames.length ? frames.map(f => `<div class="row selectable frame-row ${Number(f.level) === selectedFrame ? 'selected' : ''}" data-frame="${esc(f.level)}">` +
      `<code>#${esc(f.level)}</code><div class="main"><b>${esc(f.func || '??')}</b><small>${esc(f.fullname || f.file || 'No source')}${f.line ? ':' + esc(f.line) : ''}</small></div><small class="muted">${esc(f.addr || '')}</small></div>`).join('')
      : '<div class="empty">GDB returned no frames.</div>';
    $$('.frame-row').forEach(row => row.onclick = async () => {
      try {
        selectedFrame = Number(row.dataset.frame);
        await api('/debug/frame/' + selectedFrame, {method: 'PUT', body: '{}', waiting: false});
        $$('.frame-row').forEach(x => x.classList.toggle('selected', x === row));
        if (variableMode === 'locals') await loadLocals();
      } catch (error) { fail(error); }
    });
  } catch (error) { fail(error); }
}
function hexDump(hex, address) {
  const bytes = hex.match(/../g) || [];
  let out = 'Address    00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F   ASCII\n';
  for (let i = 0; i < bytes.length; i += 16) {
    const row = bytes.slice(i, i + 16);
    out += (address + i).toString(16).padStart(8, '0') + '   ' + row.join(' ').padEnd(47) + '   ' + row.map(x => { const c = parseInt(x, 16); return c >= 32 && c < 127 ? String.fromCharCode(c) : '.'; }).join('') + '\n';
  }
  return out;
}
async function readMemory() {
  try {
    const address = $('#memAddress').value, data = await post('/memory/read', {address, length: +$('#memLength').value}, {waiting: false});
    $('#hexView').textContent = hexDump(data.data, parseInt(address, 0));
  } catch (error) { fail(error); }
}
async function attachElfFile(file) {
  try {
    const artifact = await upload(file);
    await post('/elf/attach', {artifact_id: artifact.id});
    attachedElf = artifact.name;
    toast(`Symbols loaded from ${artifact.name}`);
    renderDebug();
  } catch (error) { fail(error); }
}
async function toggleBrowserDebugger() {
  const active = state.debugger?.owner === 'browser';
  try {
    await post(active ? '/debug/stop' : '/debug/start', {core: state.target?.selected_core || 0, port: GDB_PORT});
    toast(active ? 'Debugger stopped' : 'Debugger started');
  } catch (error) { fail(error); }
  await refresh();
}

/* ---------- Device support, sequences, recovery ---------- */

function openPackDialog() {
  $('#packDialog').showModal();
  $('#packSearch').focus();
}
function renderPackResults(items, indexAvailable = true) {
  $('#packResults').innerHTML = items.length ? items.map(x => `<div class="row"><div class="main"><b>${esc(x.name)}</b><small>${esc(x.vendor)} · ${esc(x.pack)} ${esc(x.version)}</small></div>` +
    `<button data-pack-device="${esc(x.name)}" ${x.installed ? 'disabled' : ''}>${x.installed ? 'Installed' : 'Install'}</button></div>`).join('')
    : `<div class="empty">${indexAvailable ? ($('#packSearch').value.trim().length < 2 ? 'Type at least two characters.' : 'No matching devices.') : 'The pack index has not been downloaded yet. Use Refresh index.'}</div>`;
  $$('[data-pack-device]').forEach(b => b.onclick = () => installPack(b));
}
async function searchPacks() {
  const query = $('#packSearch').value.trim();
  if (query.length < 2) return renderPackResults([]);
  try {
    const result = await api('/packs/devices?q=' + encodeURIComponent(query), {waiting: false});
    renderPackResults(result.devices, result.index_available);
  } catch (error) { fail(error); }
}
async function updatePackIndex() {
  const button = $('#packIndex');
  button.disabled = true;
  button.textContent = 'Refreshing…';
  try {
    const result = await post('/packs/index');
    toast(`Pack index refreshed · ${result.device_count} devices`);
    await searchPacks();
  } catch (error) { fail(error); } finally { button.disabled = false; button.textContent = 'Refresh index'; }
}
async function installPack(button) {
  const device = button.dataset.packDevice;
  button.disabled = true;
  button.textContent = 'Installing…';
  try {
    await post('/packs/install', {device});
    await loadTargets();
    const match = targetCatalog.find(x => x.name.toLowerCase() === device.toLowerCase() || (x.part_number || '').toLowerCase() === device.toLowerCase());
    $('#packDialog').close();
    await selectTarget(match?.name || device);
    toast(`${device} support installed and selected`);
  } catch (error) { button.disabled = false; button.textContent = 'Install'; fail(error); }
}

async function loadPackSequenceInfo() {
  const target = $('#targetSelect').value.trim();
  if (!target) return renderPackSequenceInfo(null);
  try { renderPackSequenceInfo(await api(`/targets/${encodeURIComponent(target)}/pack`, {waiting: false})); }
  catch (error) { renderPackSequenceInfo(null); }
}
function renderPackSequenceInfo(info) {
  packSequenceInfo = info;
  const recovery = info?.recovery, method = recovery?.implementation || 'Unavailable';
  $('#targetRecoveryMethod').textContent = method;
  $('#targetRecoveryImplementation').textContent = recovery?.available
    ? `${recovery.automatic ? 'Automatic security detection' : 'Manual recovery'} provided by ${method}.`
    : 'No target-specific recovery implementation was found.';
  $('#targetUnlock').dataset.available = String(!!recovery?.available);
  updateRecoveryState();
  const isPack = info?.source === 'pack', items = info?.sequences || [];
  $('#packSequenceOptions').hidden = !isPack;
  if (!isPack) return;
  $('#packSequenceIdentity').textContent = `${info.pack?.name || 'CMSIS-Pack'}${info.processors?.length ? ' · ' + info.processors.join(', ') : ''}`;
  $('#packSequenceRunSelect').innerHTML = `<option value="">${items.length ? 'Select a sequence…' : 'No vendor sequences in this pack'}</option>` +
    items.filter(x => x.enabled).map(x => `<option value="${esc(x.name + '\u0001' + (x.pname || ''))}">${esc(x.name + (x.pname ? ' · ' + x.pname : ''))}</option>`).join('');
  updatePackSequenceRunState();
}
function updatePackSequenceRunState() {
  $('#packSequenceRun').disabled = !state.connected || state.state === 'busy' || !$('#packSequenceRunSelect').value;
}
async function runSelectedPackSequence() {
  const [name, pname] = $('#packSequenceRunSelect').value.split('\u0001');
  if (!name || !confirm(`Run vendor debug sequence ${name}${pname ? ' for ' + pname : ''}?\n\nThis can change reset, security, flash or debug-port state.`)) return;
  try {
    await post('/pack/sequences/run', {name, pname: pname || null}, {waiting: `Running ${name}…`});
    toast(`${name} completed`);
  } catch (error) { fail(error); }
  await refresh();
}
function updateRecoveryState() {
  const locked = state.target?.locked;
  $('#targetSecurityState').textContent = !state.connected ? 'Not connected' : locked === true ? 'Locked' : locked === false ? 'Unlocked' : 'Unknown';
  const direct = isDirect() && !state.debugger?.active;
  $('#targetUnlock').disabled = !direct || $('#targetUnlock').dataset.available !== 'true';
}
async function unlockTarget() {
  if (!await confirmAction('Mass erase and unlock?', 'All flash contents will be permanently erased before device security is removed.', 'Erase and unlock')) return;
  try {
    await post('/target/unlock', {}, {waiting: 'Mass erasing and unlocking…'});
    toast('Target unlocked');
  } catch (error) { fail(error); }
  await refresh();
}

/* ---------- Settings ---------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('pyocd-ui-theme', theme); } catch {}
  $$('[data-theme-choice]').forEach(b => b.classList.toggle('active', b.dataset.themeChoice === theme));
  const dark = theme === 'dark' || (theme === 'auto' && !matchMedia('(prefers-color-scheme: light)').matches);
  $('meta[name="theme-color"]').content = dark ? '#15171c' : '#f4f5f7';
}
function applyScale(percent) {
  const scale = Math.min(125, Math.max(80, percent)) / 100;
  document.documentElement.style.setProperty('--ui-scale', scale);
  $('#uiScale').value = Math.round(scale * 100);
  $('#uiScaleOut').value = Math.round(scale * 100) + '%';
  try { localStorage.setItem('pyocd-ui-scale', String(scale)); } catch {}
}
function applyInterfaceName(name) {
  const value = (name || '').trim();
  $('#brandName').textContent = value || 'pyOCD';
  document.title = (value ? value + ' · ' : '') + 'pyOCD Control Center';
}
async function saveInterfaceName() {
  const name = $('#interfaceName').value.trim();
  savedProfile = {...savedProfile, interface_name: name};
  try {
    savedProfile = await api('/config', {method: 'PUT', body: JSON.stringify(savedProfile), waiting: false});
    applyInterfaceName(name);
    toast(name ? 'Name saved' : 'Default name restored');
  } catch (error) { fail(error); }
}
async function loadSystemInfo() {
  try {
    const info = await api('/system', {waiting: false}), memory = info.memory || {};
    $('#aboutVersion').textContent = 'v' + info.pyocd_version;
    $('#systemFacts').innerHTML = factsHtml([
      ['Host', info.hostname], ['Platform', info.platform], ['Python', info.python_version], ['Process', info.pid],
      ['Uptime', fmtUptime(info.uptime)], ['pyOCD memory', fmtBytes(memory.process_used)],
      ['System memory', `${fmtBytes(memory.system_used)} / ${fmtBytes(memory.system_total)}`],
    ]);
    const addresses = info.addresses || [];
    $('#networkAddresses').innerHTML = addresses.length ? addresses.map(x => `<code>${esc(x)}</code>`).join('') : '<span class="muted">No non-loopback addresses.</span>';
    const supported = info.system_power_supported;
    $('#powerSupport').textContent = supported ? 'Available on this Linux host. Administrative permission may be required.' : 'Reboot and shut down are available only on Linux hosts.';
    $('#rebootSystem').disabled = $('#shutdownSystem').disabled = !supported;
  } catch (error) { fail(error); }
}
async function checkForUpdates() {
  const button = $('#checkUpdates'), result = $('#updateResult');
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    const info = await api('/update/check', {waiting: false}), head = info.latest_revision.slice(0, 8);
    const status = info.update_available === true ? `A newer revision of main is available (${esc(head)}).`
      : info.update_available === false ? `Up to date with main (${esc(head)}).` : `Main is at ${esc(head)}; the installed commit could not be identified.`;
    result.innerHTML = `<span>${status} <span class="muted">Installed ${esc(info.current)} · ${esc(info.install_source)}</span></span>` +
      (info.update_available !== false ? `<code>${esc(info.command)}</code><span class="actions"><button id="copyUpdateCommand">Copy command</button><a href="${esc(info.release_url)}" target="_blank" rel="noopener">View commit</a></span>` : '');
    $('#copyUpdateCommand')?.addEventListener('click', () => navigator.clipboard.writeText(info.command).then(() => toast('Command copied')));
  } catch (error) { result.textContent = error.message; } finally { button.disabled = false; button.textContent = 'Check for updates'; }
}
async function requestSystemPower(kind) {
  const label = kind === 'reboot' ? 'reboot' : 'shut down';
  if (!confirm(`Really ${label} this host? Active debug sessions will end.`)) return;
  try { await post('/system/power/' + kind); toast(`System ${label} requested`); } catch (error) { fail(error); }
}

/* ---------- Dock: log, console, SWD traffic ---------- */

const dockState = Object.assign({tab: 'log', collapsed: innerWidth < 700, height: 0}, store.get('pyocd-dock', {}));
let logRecords = [], logSignature = '', lastSeenError = 0;

function applyDock() {
  const dock = $('#dock');
  dock.classList.toggle('collapsed', dockState.collapsed);
  if (dockState.height) dock.style.setProperty('--dock-height', dockState.height + 'px');
  $$('[data-dock]').forEach(b => { const active = b.dataset.dock === dockState.tab; b.classList.toggle('active', active); b.setAttribute('aria-selected', String(active)); });
  $$('[data-pane]').forEach(p => p.hidden = p.dataset.pane !== dockState.tab);
  $$('.dock-actions').forEach(a => a.hidden = a.dataset.for !== dockState.tab);
  if (dockState.tab === 'log' && !dockState.collapsed) { lastSeenError = Date.now() / 1000; $('#logAlert').hidden = true; }
  if (dockState.tab === 'traffic' && !dockState.collapsed) refreshSwdTraffic(true);
  store.set('pyocd-dock', dockState);
}
function selectDockTab(tab) {
  if (dockState.tab === tab && !dockState.collapsed) dockState.collapsed = true;
  else { dockState.tab = tab; dockState.collapsed = false; }
  applyDock();
  if (tab === 'console' && !dockState.collapsed) $('#consoleInput').focus();
}
function setupDockResize() {
  const handle = $('#dockResize');
  handle.onpointerdown = e => {
    handle.setPointerCapture(e.pointerId);
    const move = ev => {
      dockState.height = Math.round(Math.min(window.innerHeight * .7, Math.max(90, window.innerHeight - ev.clientY)));
      $('#dock').style.setProperty('--dock-height', dockState.height + 'px');
    };
    const up = () => { handle.onpointermove = null; handle.onpointerup = null; store.set('pyocd-dock', dockState); };
    handle.onpointermove = move;
    handle.onpointerup = up;
  };
}
async function refreshLogs() {
  try { logRecords = (await api('/logs', {waiting: false})).records || []; } catch { return; }
  const last = logRecords[logRecords.length - 1], signature = `${logRecords.length}:${last?.time}`;
  if (signature === logSignature) return;
  logSignature = signature;
  const out = $('#logOutput'), stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  out.innerHTML = logRecords.length ? logRecords.map(x => `<span class="${esc(x.level).toLowerCase()}">${new Date(x.time * 1000).toISOString().slice(11, 23)} ${esc(x.level.padEnd(8))} ${esc(x.logger)} · ${esc(x.message)}</span>`).join('\n') : '<span class="muted">Waiting for pyOCD messages…</span>';
  if (stick) out.scrollTop = out.scrollHeight;
  const newError = logRecords.some(x => /ERROR|CRITICAL/.test(x.level) && x.time > lastSeenError);
  if (newError && (dockState.collapsed || dockState.tab !== 'log')) $('#logAlert').hidden = false;
  else if (!dockState.collapsed && dockState.tab === 'log') lastSeenError = Date.now() / 1000;
}
async function runConsoleCommand(e) {
  e.preventDefault();
  const command = $('#consoleInput').value, out = $('#terminalOutput');
  if (!command) return;
  out.textContent += `\n> ${command}\n`;
  $('#consoleInput').value = '';
  try { out.textContent += (await post('/console', {command}, {waiting: false})).output; }
  catch (error) { out.textContent += 'Error: ' + error.message + '\n'; }
  out.scrollTop = out.scrollHeight;
}

let swdCursor = 0, swdRows = [], swdGroups = new Map(), swdLoading = false, swdOpen = new Set();
function renderSwdSummary(info) {
  if (document.activeElement !== $('#swdTrafficSwitch')) $('#swdTrafficSwitch').checked = !!info.enabled;
  const parts = [`${Number(info.records || swdRows.length).toLocaleString()} transactions`, `${Number(info.groups || swdGroups.size).toLocaleString()} groups`];
  if (info.dropped) parts.push(`${Number(info.dropped).toLocaleString()} dropped`);
  $('#swdTrafficStats').textContent = parts.join(' · ');
}
function trafficData(row) {
  if (row.status === 'error') return row.error || 'Transfer failed';
  if (row.response) return row.response.map(x => x || '—').join(' · ');
  return (row.data || '—') + (row.data_truncated ? ' …' : '');
}
function renderSwdTraffic() {
  const box = $('#swdTrafficGroups'), grouped = new Map();
  for (const row of swdRows) {
    const key = row.group_id ?? 'none';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  if (!grouped.size) {
    box.innerHTML = `<div class="empty">${$('#swdTrafficSwitch').checked ? 'No SWD transactions captured yet.' : 'Turn on Capture, then connect or operate the target. Captures use a bounded in-memory buffer.'}</div>`;
    return;
  }
  box.innerHTML = [...grouped.entries()].sort((a, b) => (a[1][0].id || 0) - (b[1][0].id || 0)).map(([key, rows]) => {
    const group = key === 'none' ? {name: 'Other SWD activity', category: 'unclassified'} : swdGroups.get(key) || {name: 'SWD operation', category: 'operation'};
    const errored = rows.some(x => x.status === 'error');
    return `<details data-group="${esc(key)}" ${swdOpen.has(String(key)) ? 'open' : ''}><summary><b>${esc(group.name)}${group.details?.target ? ' · ' + esc(group.details.target) : ''}</b>` +
      `<small>${esc(group.category || 'swd')} · ${timeOf(group.started_at || rows[0].time)} · ${rows.length} transaction${rows.length === 1 ? '' : 's'}</small>` +
      `<span class="tag ${errored ? 'bad' : ''}">${errored ? 'error' : esc(group.status || 'done')}</span></summary>` +
      `<table><thead><tr><th>Time</th><th>Operation</th><th>Address</th><th>Data / response</th><th>Result</th></tr></thead><tbody>` +
      rows.map(r => `<tr><td>${timeOf(r.time)}</td><td>${esc(r.operation || r.kind || 'SWD')}</td><td>${esc(r.address_hex || r.port || '—')}</td><td class="data" title="${esc(trafficData(r))}">${esc(trafficData(r))}</td><td class="${r.status === 'error' ? 'error' : ''}">${r.status === 'error' ? 'error' : `${esc(r.duration_ms ?? 0)} ms`}</td></tr>`).join('') +
      '</tbody></table></details>';
  }).join('');
  box.querySelectorAll('details').forEach(d => d.addEventListener('toggle', () => d.open ? swdOpen.add(d.dataset.group) : swdOpen.delete(d.dataset.group)));
}
async function refreshSwdTraffic(force = false) {
  const visible = dockState.tab === 'traffic' && !dockState.collapsed;
  if ((!force && !visible && !state.swd_traffic?.enabled) || swdLoading) return;
  swdLoading = true;
  try {
    const batch = await api(`/swd-traffic?after=${swdCursor}&limit=500`, {waiting: false});
    for (const group of batch.groups || []) swdGroups.set(group.id, group);
    if (batch.oldest_id && swdCursor && swdCursor < batch.oldest_id - 1) { swdRows = []; swdGroups.clear(); swdOpen.clear(); }
    swdRows.push(...(batch.transactions || []));
    if (swdRows.length > 2500) swdRows.splice(0, swdRows.length - 2500);
    const changed = (batch.transactions || []).length || force;
    swdCursor = batch.next_cursor ?? swdCursor;
    renderSwdSummary(batch);
    if (changed && visible) renderSwdTraffic();
  } catch {} finally { swdLoading = false; }
}
async function toggleSwdTraffic(e) {
  const enabled = e.currentTarget.checked;
  try {
    renderSwdSummary(await api('/swd-traffic', {method: 'PUT', body: JSON.stringify({enabled}), waiting: false}));
    await refreshSwdTraffic(true);
    renderSwdTraffic();
  } catch (error) { e.currentTarget.checked = !enabled; fail(error); }
}
async function clearSwdTraffic() {
  try {
    const result = await api('/swd-traffic', {method: 'DELETE', waiting: false});
    swdRows = []; swdGroups.clear(); swdOpen.clear(); swdCursor = result.latest_id || 0;
    renderSwdSummary(result);
    renderSwdTraffic();
  } catch (error) { fail(error); }
}

/* ---------- Navigation, popovers, drawer ---------- */

const VIEWS = ['home', 'flash', 'debug', 'settings'];
function showView(id) {
  if (!VIEWS.includes(id)) id = 'home';
  $$('#nav [data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === id));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === id));
  if (location.hash.slice(1) !== id) history.replaceState(null, '', id === 'home' ? location.pathname : '#' + id);
  if (id === 'settings') loadSystemInfo();
  $('#main').scrollTop = 0;
}

let openPopover = null;
function togglePopover(button, popover) {
  const opening = popover.hidden;
  closePopovers();
  if (!opening) return;
  popover.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  openPopover = [button, popover];
}
function closePopovers() {
  if (!openPopover) return;
  openPopover[1].hidden = true;
  openPopover[0].setAttribute('aria-expanded', 'false');
  openPopover = null;
}
function openDrawer() {
  closePopovers();
  $('#optionsDrawer').hidden = false;
  $('#drawerScrim').hidden = false;
  $('#closeOptions').focus();
}
function closeDrawer() {
  $('#optionsDrawer').hidden = true;
  $('#drawerScrim').hidden = true;
}

/* ---------- Wiring ---------- */

function wire() {
  // Session bar
  const input = $('#targetSelect');
  input.onfocus = openTargetMenu;
  input.onclick = openTargetMenu;
  input.oninput = () => { $('#targetOptions').hidden = false; renderTargetOptions(input.value); };
  input.onkeydown = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if ($('#targetOptions').hidden) openTargetMenu(); moveTargetActive(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveTargetActive(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const active = $$('#targetOptions .target-option')[targetActiveIndex];
      const value = active?.dataset.target || input.value.trim();
      if (value) selectTarget(value);
    } else if (e.key === 'Escape') { closeTargetMenu(true); input.blur(); }
  };
  // Leaving the field only commits an exact catalog name; Enter accepts any
  // text so unlisted targets still work.
  input.onblur = () => {
    if ($('#targetOptions').hidden) return;
    const value = input.value.trim().toLowerCase();
    const match = (targetCatalog || []).find(x => x.name.toLowerCase() === value);
    closeTargetMenu(!match);
    if (match && match.name !== targetBeforeEditing) selectTarget(match.name);
  };
  $('#probeQuick').onchange = e => onProbeChanged(e.target.value);
  $('#probeSelect').onchange = e => onProbeChanged(e.target.value);
  $('#probeMissing').onclick = openDrawer;
  $('#frequency').oninput = () => { updateClock(); updateOptionsBadge(); };
  $('#frequency').onchange = saveConfig;
  $$('[data-clock]').forEach(b => b.onclick = () => { $('#frequency').value = b.dataset.clock; updateClock(); saveConfig(); });
  $('#targetSummary').onclick = () => togglePopover($('#targetSummary'), $('#targetPopover'));
  $('#haltRun').onclick = () => action(coreState() === 'running' ? 'halt' : 'resume');
  $('#resetBtn').onclick = () => resetTarget('run');
  $('#resetMenuBtn').onclick = () => togglePopover($('#resetMenuBtn'), $('#resetMenu'));
  $$('[data-reset]').forEach(b => b.onclick = () => { closePopovers(); resetTarget(b.dataset.reset); });
  $('#gdbSwitch').onchange = e => toggleGdb(e.target.checked);
  $('#copyEndpoint').onclick = () => navigator.clipboard.writeText(gdbEndpoint()).then(() => toast(`Copied ${gdbEndpoint()}`), () => toast('Clipboard unavailable', true));
  $('#optionsBtn').onclick = openDrawer;
  $('#connectBtn').onclick = () => state.connected ? disconnect() : connectWithGdb().catch(fail);

  // Navigation
  $$('#nav [data-view]').forEach(b => b.onclick = () => showView(b.dataset.view));
  document.addEventListener('click', e => {
    const viewLink = e.target.closest('[data-view-link]');
    if (viewLink && !viewLink.closest('#quickFlash')) showView(viewLink.dataset.viewLink);
    if (e.target.closest('[data-open-options]')) openDrawer();
    if (e.target.closest('[data-open-packs]') && !e.target.closest('#targetOptions')) openPackDialog();
    if (openPopover && !openPopover[0].contains(e.target) && !openPopover[1].contains(e.target)) closePopovers();
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (openPopover) closePopovers();
    else if (!$('#optionsDrawer').hidden) closeDrawer();
  });
  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));

  // Drawer
  $('#closeOptions').onclick = closeDrawer;
  $('#drawerScrim').onclick = closeDrawer;
  $('#resetOptions').onclick = restoreDefaults;
  $('#probeResetBtn').onclick = pulseResetPin;
  for (const f of FIELDS) $('#' + f.id).addEventListener('change', saveConfig);
  ['swclk', 'swdio', 'nreset'].forEach(id => $('#' + id).addEventListener('input', renderPins));
  $('#autoUnlock').addEventListener('change', updateOptionsBadge);
  $('#packSequenceRunSelect').onchange = updatePackSequenceRunState;
  $('#packSequenceRun').onclick = runSelectedPackSequence;
  $('#targetUnlock').onclick = unlockTarget;

  // Flash
  $('#fileInput').onclick = e => { e.currentTarget.value = ''; };
  $('#fileInput').onchange = e => { const files = [...(e.target.files || [])]; e.target.value = ''; if (files.length) uploadFiles(files, !$('#flash').classList.contains('active')); };
  const zone = $('#dropzone');
  ['dragenter', 'dragover'].forEach(n => zone.addEventListener(n, e => { e.preventDefault(); zone.classList.add('drag'); }));
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', e => { e.preventDefault(); e.stopPropagation(); zone.classList.remove('drag'); document.body.classList.remove('dragging'); uploadFiles([...e.dataTransfer.files], false); });
  $('#mainImage').onchange = () => { selectedArtifact = $('#mainImage').value; toggleBinFields(); renderArtifacts(); renderQuickFlash(); };
  $('#bootImage').onchange = toggleBinFields;
  $('#mainOffset').oninput = renderQuickFlash;
  $('#programBtn').onclick = () => submitPlan('program');
  $('#verifyBtn').onclick = () => submitPlan('verify');
  $('#eraseBtn').onclick = eraseChip;
  $('#dumpRegion').onchange = updateDumpInputs;
  $('#dumpRange').onclick = dumpRange;
  $('#dumpRegisters').onclick = dumpRegisters;

  // Drop a firmware file anywhere in the window.
  let dragDepth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', e => { if (hasFiles(e)) { dragDepth++; document.body.classList.add('dragging'); } });
  window.addEventListener('dragleave', e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    document.body.classList.remove('dragging');
    const files = [...e.dataTransfer.files];
    const elfOnDebug = $('#debug').classList.contains('active') && files.length === 1 && /\.(elf|axf)$/i.test(files[0].name) && isDirect();
    if (elfOnDebug) attachElfFile(files[0]);
    else uploadFiles(files, true);
  });

  // Debug
  $('#elfInput').onclick = e => { e.currentTarget.value = ''; };
  $('#elfInput').onchange = e => { const file = e.target.files[0]; e.target.value = ''; if (file) attachElfFile(file); };
  $('#browserDebug').onclick = toggleBrowserDebugger;
  $$('[data-action]').forEach(b => b.onclick = () => action(b.dataset.action));
  $('#readRegs').onclick = readRegisters;
  $('#readStack').onclick = loadFrames;
  $('#readMemory').onclick = readMemory;
  $('#memAddress').onkeydown = $('#memLength').onkeydown = e => { if (e.key === 'Enter') readMemory(); };
  $('#dumpMemory').onclick = async () => {
    recordDump(`Reading memory at ${$('#memAddress').value}, ${+$('#memLength').value} bytes`);
    try {
      await downloadRange($('#memAddress').value, +$('#memLength').value, 'memory.bin');
      recordDump('Memory dump downloaded', 'completed');
    } catch (error) { recordDump(error.message, 'failed', 'ERROR'); fail(error); }
  };
  $('#localsTab').onclick = () => {
    variableMode = 'locals';
    $('#localsTab').classList.add('active'); $('#globalsTab').classList.remove('active');
    $('#globalSearch').hidden = true;
    if (browserDebugger()) loadLocals();
  };
  $('#globalsTab').onclick = () => {
    variableMode = 'globals';
    $('#globalsTab').classList.add('active'); $('#localsTab').classList.remove('active');
    $('#globalSearch').hidden = false;
    $('#variablesView').innerHTML = '<div class="empty">Search globals by name.</div>';
    $('#globalQuery').focus();
  };
  $('#refreshVariables').onclick = () => variableMode === 'locals' ? loadLocals() : loadGlobals();
  $('#searchGlobals').onclick = loadGlobals;
  $('#globalQuery').onkeydown = e => { if (e.key === 'Enter') loadGlobals(); };

  // Settings
  $$('[data-theme-choice]').forEach(b => b.onclick = () => applyTheme(b.dataset.themeChoice));
  $('#uiScale').oninput = e => applyScale(+e.target.value);
  $('#saveInterfaceName').onclick = saveInterfaceName;
  $('#checkUpdates').onclick = checkForUpdates;
  $('#restartPyocd').onclick = () => restartPyocd(false);
  $('#waitingRestart').onclick = () => restartPyocd(false);
  $('#rebootSystem').onclick = () => requestSystemPower('reboot');
  $('#shutdownSystem').onclick = () => requestSystemPower('shutdown');

  // Pack catalog
  $('#closePackDialog').onclick = () => $('#packDialog').close();
  $('#packDialog').onclick = e => { if (e.target === $('#packDialog')) $('#packDialog').close(); };
  $('#packIndex').onclick = updatePackIndex;
  let packTimer;
  $('#packSearch').oninput = () => { clearTimeout(packTimer); packTimer = setTimeout(searchPacks, 300); };

  // Dock
  $$('[data-dock]').forEach(b => b.onclick = () => selectDockTab(b.dataset.dock));
  $('#dockToggle').onclick = () => { dockState.collapsed = !dockState.collapsed; applyDock(); };
  $('#clearLogs').onclick = async () => { try { await api('/logs', {method: 'DELETE', waiting: false}); } catch (error) { fail(error); } logSignature = ''; refreshLogs(); };
  $('#downloadLogs').onclick = () => saveBlob(new Blob([$('#logOutput').textContent], {type: 'text/plain'}), 'pyocd.log');
  $('#consoleForm').onsubmit = runConsoleCommand;
  $('#clearConsole').onclick = () => { $('#terminalOutput').textContent = ''; };
  $('#swdTrafficSwitch').onchange = toggleSwdTraffic;
  $('#clearSwdTraffic').onclick = clearSwdTraffic;
  setupDockResize();
}

/* ---------- Startup and polling ---------- */

let refreshBusy = false, lastRefreshError = '';
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    render(await api('/state', {waiting: false}));
    lastRefreshError = '';
  } catch (error) {
    const message = error.message || String(error);
    if (message !== lastRefreshError) { toast(message, true); lastRefreshError = message; }
  } finally { refreshBusy = false; }
}

async function initialise() {
  wire();
  applyTheme(document.documentElement.dataset.theme || 'dark');
  applyScale(Math.round((Number(getComputedStyle(document.documentElement).getPropertyValue('--ui-scale')) || 1) * 100));
  applyDock();
  showView(location.hash.slice(1));
  clearDebugWorkspace();
  try {
    const [profile, snapshot] = await Promise.all([api('/config', {waiting: false}), api('/state', {waiting: false})]);
    if (snapshot.connection_defaults) connectionDefaults = snapshot.connection_defaults;
    savedProfile = profile;
    applyInterfaceName(profile.interface_name);
    $('#interfaceName').value = profile.interface_name || '';
    applyProfile(profile);
    targetBeforeEditing = $('#targetSelect').value;
    render(snapshot);
  } catch (error) { fail(error); }
  renderRecentSetups();
  await loadDiscovery();
  // The target catalog and pack lookup share registries on the server; listing
  // targets while a pack loads fails, so run them one after the other.
  loadTargets().then(loadPackSequenceInfo);
  refreshLogs();
  setInterval(refresh, 1000);
  setInterval(refreshLogs, 1500);
  setInterval(loadDiscovery, 3000);
  setInterval(refreshSwdTraffic, 750);
}

initialise();
