/* Task-first presentation. Keep the original controls mounted for their handlers. */
function setupWorkspace() {
  document.documentElement.classList.add('task-workspace');
  tabObserver?.disconnect();
  selectTab = function(id) {
    document.documentElement.dataset.activeTab = id;
    $$('#nav [data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === id));
    $$('.tab').forEach(section => section.classList.toggle('active', section.id === id));
    if (id === 'system') loadSystemInfo();
  };
  document.documentElement.dataset.activeTab = 'overview';
  const overview = $('#overview');
  overview.querySelector('.heading h1').textContent = 'Overview';
  overview.querySelector('.heading p').textContent = 'Your target and what you can do next.';
  overview.insertAdjacentHTML('beforeend', `
    <div id="workspaceOverview" class="workspace-overview">
      <article class="card focus-card">
        <div class="focus-main">
          <div class="focus-status"><i class="status-dot"></i><span id="focusStatus">Disconnected</span></div>
          <h2 id="focusTitle">Connect a target</h2>
          <p id="focusDescription">Choose a debug probe and MCU to get started.</p>
          <div class="focus-actions">
            <button id="focusPrimary" class="primary">Set up connection</button>
            <button id="focusProgram" hidden>Program firmware</button>
            <button id="focusDebug" hidden>Open debugger</button>
            <button id="focusRun" hidden>Run</button>
            <button id="focusReset" hidden>Reset</button>
          </div>
        </div>
        <dl id="focusFacts" class="focus-facts" hidden>
          <div><dt>Flash</dt><dd id="focusFlash">—</dd></div>
          <div><dt>RAM</dt><dd id="focusRam">—</dd></div>
        </dl>
      </article>
      <div id="workspaceSupporting" class="workspace-supporting" hidden>
        <article class="card service-card">
          <div><span class="eyebrow">DEBUG SERVICE</span><h3>GDB server</h3><p id="serviceDescription">Use an external debugger with this target.</p></div>
          <div class="service-actions"><code id="serviceEndpoint" hidden>127.0.0.1:3030</code><button id="serviceCopy" hidden>Copy address</button><button id="serviceToggle">Start server</button></div>
        </article>
        <details id="technicalDetails" class="technical-details"><summary>Technical details <span>Memory map and link information</span></summary>
          <div class="technical-body"><dl>
            <div><dt>Debug probe</dt><dd id="technicalProbe">—</dd></div>
            <div><dt>Transport</dt><dd id="technicalTransport">—</dd></div>
            <div><dt>Clock</dt><dd id="technicalClock">—</dd></div>
          </dl><div id="technicalMap"></div></div>
        </details>
      </div>
    </div>`);
  $('#overview .hero').classList.add('legacy-overview');
  $('#overview .quick-actions').classList.add('legacy-overview');
  $('#overview > .cols').classList.add('legacy-overview');
  $('#overview .connection-banner').classList.add('legacy-overview');

  const connection = $('#connection');
  connection.querySelector('.heading p').textContent = 'Choose a probe and target. The defaults work for most boards.';
  const setup = connection.querySelector('.cols.setup');
  const basic = setup.querySelector('.card.form');
  basic.querySelector('h3').textContent = 'Connection setup';
  const autoGdb = $('.auto-gdb');
  if (autoGdb) basic.insertBefore(autoGdb, basic.querySelector('.formactions'));
  basic.querySelector('.formactions').insertAdjacentHTML('afterend', '<p id="autoUnlockNotice" class="connection-caution" hidden>Automatic unlock is on. Connecting to a secured target may erase its flash. Change this in Advanced link settings → Target recovery.</p>');

  $('#advancedCard > h3').textContent = 'Advanced link settings';
  $('#targetRecoveryOptions').open = !!state.target?.locked;

  $('#focusPrimary').onclick = () => {
    if ($('#probeSelect').value && $('#targetSelect').value) $('#connectTop').click();
    else navigateToTab('connection');
  };
  $('#focusProgram').onclick = () => navigateToTab('program');
  $('#focusDebug').onclick = () => navigateToTab('debug');
  $('#focusRun').onclick = () => action(state.target?.state === 'running' ? 'halt' : 'resume');
  $('#focusReset').onclick = () => $('#topReset').click();
  $('#serviceToggle').onclick = () => $('#topGdb').click();
  $('#serviceCopy').onclick = () => $('#copyEndpoint').click();
  $('#statePill').onclick = () => navigateToTab('connection');
  $('#statePill').title = 'Open connection settings';
  const originalConnect = $('#connectTop').onclick;
  $('#connectTop').onclick = event => {
    if (!state.connected && (!$('#probeSelect').value || !$('#targetSelect').value)) navigateToTab('connection');
    else originalConnect.call($('#connectTop'), event);
  };
  relabelProbes();
  new MutationObserver(relabelProbes).observe($('#probeSelect'), { childList: true });
  $('#autoUnlock').addEventListener('change', () => renderWorkspace(state));
  renderWorkspace(state);
}

function relabelProbes() {
  const options = [...$('#probeSelect').options].filter(option => option.value);
  const names = options.map(option => option.textContent.split(' · ')[0]);
  options.forEach((option, index) => {
    if (!option.title) option.title = option.textContent;
    const label = names.filter(name => name === names[index]).length > 1
      ? `${names[index]} · …${option.value.slice(-6)}` : names[index];
    if (option.textContent !== label) option.textContent = label;
  });
}

function renderWorkspace(snapshot) {
  if (!$('#workspaceOverview')) return;
  const connected = !!snapshot.connected;
  const target = snapshot.target || {};
  const debuggerState = snapshot.debugger || {};
  const gdb = snapshot.gdb || [];
  const coreState = debuggerState.owner === 'browser' ? debuggerState.state : target.state;
  document.documentElement.classList.toggle('target-connected', connected);
  $('#overview .heading h1').textContent = connected ? 'Overview' : 'Connect to hardware';
  $('#overview .heading p').textContent = connected ? 'Your target and what you can do next.' : 'Choose a probe and MCU, then connect with the settings below.';
  $('#focusStatus').textContent = connected ? (coreState ? `Connected · ${coreState}` : 'Connected') : 'Disconnected';
  $('#focusTitle').textContent = connected ? (target.part_number || target.name || 'Target connected') : 'Connect a target';
  $('#focusDescription').textContent = connected
    ? `${target.vendor || 'Target'} is ready. Choose a task below.`
    : ($('#targetSelect').value ? `${$('#targetSelect').value} selected. Choose a probe and connect.` : 'Choose a debug probe and MCU to get started.');
  $('#focusPrimary').hidden = connected;
  $('#focusPrimary').textContent = $('#probeSelect').value && $('#targetSelect').value ? 'Connect target' : 'Set up connection';
  for (const id of ['focusProgram', 'focusDebug', 'focusReset']) $('#' + id).hidden = !connected;
  $('#focusRun').hidden = !connected || !!gdb.length && !debuggerState.active;
  $('#focusRun').textContent = coreState === 'running' ? 'Halt' : 'Run';
  $('#focusRun').disabled = snapshot.state === 'busy';
  $('#focusReset').disabled = snapshot.state === 'busy';
  const externallyOwned = gdb.length > 0 && debuggerState.owner !== 'browser';
  $('#focusProgram').disabled = gdb.length > 0 || !!debuggerState.active;
  $('#focusProgram').title = $('#focusProgram').disabled ? 'Stop debugging or the GDB server before programming' : '';
  $('#focusDebug').disabled = externallyOwned;
  $('#focusDebug').title = externallyOwned ? 'Stop the GDB server to use the browser debugger' : '';
  $('#focusFacts').hidden = !connected;
  $('#workspaceSupporting').hidden = !connected;
  if (connected) {
    const bytes = type => (target.memory_map || []).filter(region => region.type === type).reduce((sum, region) => sum + region.length, 0);
    const size = amount => amount ? `${Math.round(amount / 1024)} KiB` : '—';
    $('#focusFlash').textContent = size(bytes('flash'));
    $('#focusRam').textContent = size(bytes('ram'));
    $('#technicalProbe').textContent = snapshot.probe?.product || '—';
    $('#technicalTransport').textContent = snapshot.probe?.protocol?.toUpperCase() || '—';
    $('#technicalClock').textContent = fmtFreq(snapshot.probe?.frequency);
    const map = $('#technicalMap');
    map.replaceChildren();
    if (target.memory_map?.length) {
      const title = document.createElement('h4'); title.textContent = 'Memory map'; map.append(title);
      for (const region of target.memory_map) {
        const row = document.createElement('div'); row.className = 'technical-region';
        const name = document.createElement('span'); name.textContent = region.name || region.type;
        const address = document.createElement('code'); address.textContent = `0x${region.start.toString(16).padStart(8, '0')}`;
        const length = document.createElement('span'); length.textContent = size(region.length);
        row.append(name, address, length); map.append(row);
      }
    }
  }
  const serving = gdb.length > 0;
  $('#serviceDescription').textContent = debuggerState.owner === 'browser' ? 'Browser debugger is using this target.' : serving ? `Listening · ${gdb.reduce((sum, server) => sum + server.clients, 0)} clients · Stop server to program or debug here.` : 'Use an external debugger with this target.';
  $('#serviceEndpoint').hidden = !serving;
  $('#serviceCopy').hidden = !serving;
  $('#serviceEndpoint').textContent = `${location.hostname || 'localhost'}:${gdb[0]?.port || 3030}`;
  $('#serviceToggle').textContent = debuggerState.owner === 'browser' ? 'Stop debugger' : serving ? 'Stop server' : 'Start server';
  $('#serviceToggle').disabled = snapshot.state === 'busy';
  if (target.locked) $('#targetRecoveryOptions').open = true;
  $('#autoUnlockNotice').hidden = !$('#autoUnlock').checked || connected;
  $('#connectBtn').disabled = connected || snapshot.state === 'busy';
  $('#connectTop').setAttribute('aria-label', connected ? 'Disconnect target' : 'Connect or set up target');
}

const workspaceRenderPrevious = render;
render = function(snapshot) { workspaceRenderPrevious(snapshot); renderWorkspace(snapshot); };
const workspaceDiscoveryPrevious = loadDiscovery;
loadDiscovery = async function() { await workspaceDiscoveryPrevious(); relabelProbes(); renderWorkspace(state); };
const workspaceConnectButtonPrevious = updateConnectButton;
updateConnectButton = function() {
  workspaceConnectButtonPrevious();
  if (!state.connected && (!$('#probeSelect').value || !$('#targetSelect').value)) $('#connectTop').textContent = 'Set up connection';
};
const workspaceTopControlsPrevious = updateTopControls;
updateTopControls = function() { workspaceTopControlsPrevious(); $('#statePill').title = 'Open connection settings'; };
queueMicrotask(setupWorkspace);
