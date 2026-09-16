/* Presentation only: retain the existing controls and their event handlers. */
function setupDenseWorkspace() {
  document.documentElement.classList.add('dense-ui');
  $('.style-choices')?.closest('fieldset').remove();
  const overview = $('#overview');
  const hero = overview.querySelector('.hero');
  const target = hero.querySelector('.target');
  const specs = target.querySelector('.specs');
  const cards = [
    ['Debug probe', 'heroProbe', [['Vendor', 'probeVendor'], ['Serial number', 'probeSerial']]],
    ['Transport', 'heroLink', [['Debug clock', 'heroFreq'], ['Connect mode', 'overviewMode']]],
    ['Target state', 'runState', [['Selected core', 'overviewCore'], ['Security', 'overviewSecurity']]],
  ];
  cards.forEach(([title, valueId, rows]) => {
    const card = document.createElement('article');
    card.className = 'card metric-card';
    card.innerHTML = `<h3>${title}</h3><div class="metric-value"></div><dl>${rows.map(([label, id]) => `<div><dt>${label}</dt><dd id="${id === 'heroFreq' ? 'frequencySlot' : id}">—</dd></div>`).join('')}</dl>`;
    card.querySelector('.metric-value').append($('#' + valueId));
    hero.insertBefore(card, hero.lastElementChild);
  });
  $('#frequencySlot').replaceChildren($('#heroFreq'));
  specs.remove();
  target.classList.add('metric-card');
  target.insertAdjacentHTML('afterbegin', '<h3>Target MCU</h3>');
  overview.querySelector('.heading p').textContent = 'Target, probe, and active services.';
  const actions = overview.querySelector('.actions');
  const quick = document.createElement('article');
  quick.className = 'card quick-actions';
  quick.innerHTML = '<div><h3>Quick actions</h3><small class="muted">Target execution controls</small></div>';
  quick.append(actions);
  hero.after(quick);
  const tools = document.createElement('article');
  tools.className = 'card workspace-tools';
  tools.innerHTML = '<h3>Tools & next steps</h3>' + [
    ['connection', 'Connection', 'Configure the probe and target'],
    ['program', 'Program & dump', 'Flash, verify, and back up memory'],
    ['debug', 'Debug workspace', 'Variables, stack, memory, and registers'],
    ['console', 'Console', 'Run pyOCD commands'],
    ['traffic', 'SWD traffic', 'Inspect low-level transactions'],
  ].map(([tab, title, detail]) => `<button data-open-tab="${tab}"><span><b>${title}</b><small>${detail}</small></span><span aria-hidden="true">›</span></button>`).join('');
  overview.querySelector('.cols').append(tools);
  $$('[data-open-tab]').forEach(button => button.onclick = () => $(`#nav [data-tab="${button.dataset.openTab}"]`)?.click());
  $('aside').append($('#restartPyocd'));
  const traffic = $('#nav [data-tab="traffic"]');
  if (traffic) $('#nav').append(traffic);
  const copy = document.createElement('button');
  copy.id = 'copyEndpoint';
  copy.textContent = 'Copy endpoint';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(`${location.hostname || 'localhost'}${$('#gdbPort').textContent}`);
      toast('GDB endpoint copied');
    } catch { toast('Unable to access the clipboard', true); }
  };
  $('.port').after(copy);
  renderDenseMetrics(state);
}

function renderDenseMetrics(snapshot) {
  const values = {
    probeVendor: snapshot.probe?.vendor,
    probeSerial: snapshot.probe?.unique_id,
    overviewMode: $('#connectMode').selectedOptions[0]?.textContent,
    overviewCore: snapshot.target ? `Core ${snapshot.target.selected_core}` : null,
    overviewSecurity: snapshot.target ? (snapshot.target.locked ? 'Locked' : 'Unlocked') : null,
  };
  for (const [id, value] of Object.entries(values)) {
    const element = $('#' + id);
    if (element) element.textContent = value ?? '—';
  }
}
const renderBeforeDense = render;
render = function(snapshot) { renderBeforeDense(snapshot); renderDenseMetrics(snapshot); };
// Defer until app.js has finished adding its optional pages.
queueMicrotask(() => { setupDenseWorkspace(); setupReferencePresentation(); });

function uiIcon(name) {
  const paths = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    connection: '<path d="m10 13 4-4m-6 6-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m4 2 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
    program: '<path d="M12 16V3m-5 5 5-5 5 5M4 14v7h16v-7"/>',
    debug: '<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3 3-1 3 1 1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3-3 1Z"/><circle cx="12" cy="12" r="3"/>',
    console: '<path d="m4 5 7 7-7 7m9 0h8"/>',
    logs: '<path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>',
    system: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
    traffic: '<path d="M3 7h18m-5-5 5 5-5 5M21 17H3m5-5-5 5 5 5"/>',
    chip: '<rect x="6" y="6" width="12" height="12" rx="1"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
    probe: '<path d="M9 2h6v7H9zM7 9h10v7l-3 3h-4l-3-3zm5 10v3"/>',
    pulse: '<path d="M2 12h5l3-9 4 18 3-9h5"/>',
    monitor: '<rect x="2" y="3" width="20" height="14" rx="1"/><path d="M12 17v5m-5 0h10"/>',
    bolt: '<path d="m14 2-11 12h8l-1 8 11-13h-8z"/>',
    reset: '<path d="M3 3v6h6M3 9a9 9 0 1 1 0 7"/>',
    copy: '<rect x="8" y="7" width="12" height="15" rx="1"/><path d="M16 7V2H3v15h5"/>',
    run: '<path d="m8 4 12 8-12 8z"/>',
    halt: '<path d="M8 4v16M16 4v16"/>',
    file: '<path d="M5 2h9l5 5v15H5zm9 0v6h5M8 12h8M8 16h8"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 4 3l-1 1v2m0 2v1"/>',
  };
  return `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.chip}</svg>`;
}

function setupReferencePresentation() {
  const navNames = {overview:'Overview',connection:'Connection',program:'Program & dump',debug:'Debug',console:'Console',logs:'pyOCD log',system:'System & About',traffic:'SWD Traffic'};
  $$('#nav [data-tab]').forEach(button => button.innerHTML = uiIcon(button.dataset.tab) + `<span>${navNames[button.dataset.tab]}</span>`);
  $('#restartPyocd').innerHTML = uiIcon('reset') + '<span>Restart pyOCD</span>';
  $('#topState small').textContent = 'Core state';
  $('#topGdb small').textContent = 'GDB server';
  $('#topReset small').textContent = 'Reset';
  $('#topReset b').innerHTML = uiIcon('reset');
  $('.statusbar').insertAdjacentHTML('beforeend', '<label class="auto-gdb"><input id="autoStartGdb" type="checkbox"><span>Auto-start GDB on connect</span></label>');
  $('#autoStartGdb').checked = localStorage.getItem('pyocd-auto-start-gdb') === 'true';
  $('#autoStartGdb').onchange = e => localStorage.setItem('pyocd-auto-start-gdb', String(e.target.checked));

  const hero = $('#overview .hero');
  const core = document.createElement('article');
  core.className = 'card metric-card core-summary';
  core.innerHTML = '<h3>Core</h3><div class="metric-value" id="coreName">—</div><dl><div><dt>Available cores</dt><dd id="coreCount">—</dd></div><div><dt>Selected core</dt><dd id="selectedCore">—</dd></div><div><dt>Debugger</dt><dd id="coreDebugger">Inactive</dd></div></dl>';
  hero.insertBefore(core, $('#runState').closest('article'));
  const icons = ['chip','probe','traffic','debug','pulse','monitor'];
  [...hero.children].forEach((card, index) => card.querySelector('h3').insertAdjacentHTML('afterbegin', uiIcon(icons[index])));
  $('#targetBadge').hidden = true;
  $('#heroVendor').insertAdjacentHTML('afterend','<dl><div><dt>Flash</dt><dd id="overviewFlash">—</dd></div><div><dt>RAM</dt><dd id="overviewRam">—</dd></div><div><dt>Target ID</dt><dd id="overviewTargetId">—</dd></div></dl>');
  $('#overview .heading em').textContent = 'OVERVIEW';
  $('#overview .heading').insertAdjacentHTML('beforeend', '<div id="connectionBanner" class="connection-banner"><i class="status-dot"></i><span><b id="connectionBannerTitle">Target disconnected</b><small id="connectionBannerDetail">Choose a probe and connect to begin.</small></span></div>');
  const quick = $('.quick-actions');
  quick.firstElementChild.insertAdjacentHTML('afterbegin',uiIcon('bolt'));
  const run = quick.querySelector('[data-action="resume"]');
  quick.querySelector('.actions').prepend(run);
  quick.querySelector('[data-action="halt"]').innerHTML = uiIcon('halt') + 'Halt';
  run.innerHTML = uiIcon('run') + 'Run';
  const reset = quick.querySelector('[data-action="reset-halt"]');
  reset.removeAttribute('data-action');
  reset.innerHTML = uiIcon('reset') + 'Reset';
  reset.onclick = () => $('#topReset').click();
  reset.id = 'overviewReset';
  quick.querySelector('.actions').insertAdjacentHTML('beforeend',`<button id="restartGdb">${uiIcon('reset')}Restart GDB</button><button id="quickCopyEndpoint">${uiIcon('copy')}Copy GDB endpoint</button>`);
  $('#restartGdb').onclick = restartExternalGdb;
  $('#quickCopyEndpoint').onclick = () => $('#copyEndpoint').click();
  $('#copyEndpoint').innerHTML = uiIcon('copy');
  $('#copyEndpoint').setAttribute('aria-label','Copy GDB endpoint');
  $('.port').append($('#copyEndpoint'));
  $('.port').insertAdjacentHTML('beforebegin','<div id="gdbCardState" class="metric-value">Stopped</div><small class="endpoint-label">Endpoint</small>');
  $('#gdbSwitch').setAttribute('aria-label','Enable GDB server');
  $('#gdbSwitch').closest('article').querySelector('p.muted').textContent = 'Protocol: GDB remote';
  const lower = $('#overview > .cols');
  lower.prepend($('#jobs').closest('article'));
  $('#jobs').closest('article').querySelector('h3').insertAdjacentHTML('afterbegin',uiIcon('file'));
  $('#memoryMap').closest('article').querySelector('h3').insertAdjacentHTML('afterbegin',uiIcon('chip'));
  $('.workspace-tools h3').insertAdjacentHTML('afterbegin',uiIcon('overview'));
  $$('.workspace-tools button').forEach(button => button.insertAdjacentHTML('afterbegin',uiIcon(button.dataset.openTab)));
  $('#jobs').insertAdjacentHTML('beforebegin','<div id="recentEvents" class="event-list"></div>');
  $('#jobs').closest('article').querySelector('.title').insertAdjacentHTML('beforeend','<button id="clearRecentEvents">Clear</button>');
  $('#clearRecentEvents').onclick = () => { recentEventsAfter = Date.now()/1000; renderReferenceEvents(); };
  $('#memoryMap').closest('article').insertAdjacentHTML('beforeend','<button id="viewMemoryMap" class="panel-footer">View full memory map →</button>');
  $('#viewMemoryMap').onclick = () => { navigateToTab('debug'); $('#memAddress').focus({preventScroll:true}); };
  $('#viewMemoryMap').textContent = 'Open memory browser →';

  $('#targetRecoveryOptions').open = true;
  $('#connection .heading em').textContent = 'CONNECTION';
  $('#connection .heading p').textContent = 'Configure the debug probe and target connection.';
  $('#debug .heading em').textContent = 'DEBUGGER';
  $('#debugNotice').insertAdjacentHTML('beforebegin',`<div class="elf-bar"><span id="elfPath">ELF: No symbols attached</span><button id="loadElfFile">${uiIcon('file')}Load ELF</button></div>`);
  $('#loadElfFile').onclick = () => $('#topElfInput').click();
  $('#debug .debugbottom').insertAdjacentHTML('beforeend','<article class="card debug-events"><div class="title"><h3>Debug events</h3><button id="clearDebugEvents">Clear</button></div><div id="debugEventList" class="event-list empty">No debug events yet.</div></article>');
  $('#clearDebugEvents').onclick = () => { debugEventsAfter = Date.now()/1000; renderReferenceEvents(); };
  $('#stackView').insertAdjacentHTML('beforebegin','<div class="stack-head"><span>#</span><span>Function</span><span>File</span><span>Line</span></div>');
  $('#registers').insertAdjacentHTML('beforebegin','<div class="register-head"><span>Register</span><span>Value</span></div>');
  renderReferenceState(state);
}

let debugEventsAfter = 0, recentEventsAfter = 0;
function renderReferenceState(snapshot) {
  if (!$('#connectionBanner')) return;
  const target = snapshot.target, gdb = snapshot.gdb || [], debuggerState = snapshot.debugger || {};
  const coreState = debuggerState.owner === 'browser' ? debuggerState.state : target?.state;
  $('#connectionBanner').classList.toggle('connected',!!snapshot.connected);
  $('#connectionBannerTitle').textContent = snapshot.connected ? 'Target connected' : 'Target disconnected';
  $('#connectionBannerDetail').textContent = snapshot.connected ? `Target is ${coreState || 'connected'} and ready for debug.` : 'Choose a probe and connect to begin.';
  $('#mcuState').textContent = coreState ? coreState[0].toUpperCase()+coreState.slice(1) : '—';
  $('#gdbMetric').textContent = debuggerState.owner === 'browser' ? 'Browser debugger' : gdb.length ? `Listening :${gdb[0].port}` : '—';
  $('#gdbCardState').textContent = debuggerState.owner === 'browser' ? 'Browser debugger' : gdb.length ? 'Listening' : 'Stopped';
  $('#gdbCardState').classList.toggle('live-status',!!gdb.length);
  $('#runState').textContent = coreState ? coreState[0].toUpperCase()+coreState.slice(1) : 'Unknown';
  $('#runState').classList.toggle('live-status',!!target);
  $('#topState').classList.toggle('has-state',!!target);
  $('#topGdb').classList.toggle('has-state',!!gdb.length || !!debuggerState.active);
  $('#coreName').textContent = target ? `Core ${target.selected_core ?? 0}` : '—';
  $('#coreCount').textContent = target?.cores?.length ?? '—';
  $('#selectedCore').textContent = target?.selected_core ?? '—';
  $('#coreDebugger').textContent = debuggerState.owner || 'Inactive';
  const total = type => (target?.memory_map || []).filter(region=>region.type === type).reduce((sum,region)=>sum+region.length,0);
  $('#overviewFlash').textContent = target ? `${Math.round(total('flash')/1024)} KiB` : '—';
  $('#overviewRam').textContent = target ? `${Math.round(total('ram')/1024)} KiB` : '—';
  $('#overviewTargetId').textContent = target?.name || '—';
  $('#elfPath').textContent = 'ELF: ' + (debuggerState.executable || 'Attach an ELF to load symbols');
  $('#loadElfFile').disabled = $('#topElfInput').disabled;
  $('#overviewReset').disabled = snapshot.state === 'busy';
  $('#restartGdb').disabled = !snapshot.connected || snapshot.state === 'busy' || debuggerState.owner === 'browser';

  renderReferenceEvents();
}
function renderReferenceEvents() {
  if (!$('#recentEvents')) return;
  const rows = records => records.map(record=>`<div class="event-row"><time>${new Date(record.time*1000).toLocaleTimeString([], {hour12:false})}</time><i class="status-dot ${/ERROR|CRITICAL/.test(record.level)?'event-error':'event-info'}"></i><span>${esc(record.message)}</span></div>`).join('');
  const recent = logRecords.filter(record=>record.time > recentEventsAfter).slice(-6).reverse();
  $('#recentEvents').innerHTML = rows(recent);
  $('#jobs').hidden = recent.length > 0 && !(state.jobs || []).length;
  const records = logRecords.filter(record=>record.time > debugEventsAfter && /gdb|debug|target|elf|halt|breakpoint/i.test(record.logger+' '+record.message)).slice(-12).reverse();
  $('#debugEventList').classList.toggle('empty',!records.length);
  $('#debugEventList').innerHTML = records.length ? rows(records) : 'No debug events yet.';
}
async function restartExternalGdb() {
  const port = state.gdb?.[0]?.port || 3030;
  try {
    if (state.gdb?.length) await api('/gdb/stop',{method:'POST',body:'{}'});
    await api('/gdb/start',{method:'POST',body:JSON.stringify({port})});
    await refresh();
  } catch(error) { toast(error.message,true); }
}
const connectBeforeAutoGdb = connect;
connect = async function() {
  await connectBeforeAutoGdb();
  if ($('#autoStartGdb')?.checked) {
    try {
      await api('/gdb/start',{method:'POST',body:JSON.stringify({port:3030})});
      toast('GDB server started');
    } catch(error) { toast(`Target connected, but GDB could not start: ${error.message}`,true); }
    await refresh();
  }
};
const renderBeforeReference = render;
render = function(snapshot) { renderBeforeReference(snapshot); renderReferenceState(snapshot); };
const refreshLogsBeforeReference = refreshLogs;
refreshLogs = async function() { await refreshLogsBeforeReference(); renderReferenceEvents(); };
renderMap = function(regions) {
  const box = $('#memoryMap');
  box.className = regions.length ? 'memory-table-wrap' : 'empty';
  box.innerHTML = regions.length ? `<table class="memory-table"><thead><tr><th>Region</th><th>Start</th><th>Size</th><th>Access</th></tr></thead><tbody>${regions.map(region=>`<tr><td>${esc(region.name || region.type)}</td><td>0x${region.start.toString(16).padStart(8,'0')}</td><td>${Math.round(region.length/1024)} KiB</td><td>${esc(region.access || '—')}</td></tr>`).join('')}</tbody></table>` : 'Connect to discover memory regions.';
};
