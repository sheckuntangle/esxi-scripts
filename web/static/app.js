let interfaceBinding = null;
let interfaceRequest = 0;
let setupDownload = false;

function clearInterfaces() {
  interfaceRequest++;
  interfaceBinding = null;
  $('interface-rows').replaceChildren();
  $('interface-message').textContent = 'Load interfaces and networks for the selected saved OVA.';
}

async function refreshInterfaces() {
  const sequence = ++interfaceRequest;
  interfaceBinding = null;
  $('interface-rows').replaceChildren();
  if (!$('saved-file').value) {
    $('interface-message').textContent = 'Select a downloaded OVA to load its interfaces.';
    return;
  }
  const filename = $('saved-file').value;
  $('interface-message').textContent = 'Loading OVA interfaces and ESXi networks…';
  try {
    const [ova, host] = await Promise.all([api('/api/ova/interfaces?filename=' + encodeURIComponent(filename)), api('/api/networks')]);
    if (sequence !== interfaceRequest) return;
    if (host.host !== $('host').value.trim() || host.user !== $('user').value.trim()) throw new Error('Save the ESXi connection, then refresh networks.');
    interfaceBinding = { filename, host: host.host, user: host.user, interfaces: ova.interfaces };
    for (const nic of ova.interfaces) {
      const row = document.createElement('div'); row.className = 'interface-row'; row.dataset.index = nic.index;
      const title = document.createElement('strong'); title.textContent = nic.label;
      const networkLabel = document.createElement('label');
      const select = document.createElement('select'); select.className = 'nic-network'; select.setAttribute('aria-label', `${nic.label} ESXi network`);
      for (const name of host.networks) select.add(new Option(name, name));
      if (!host.networks.includes('VM Network')) select.add(new Option('VM Network (unavailable)', 'VM Network'));
      select.add(new Option('Create new…', '__create__')); select.value = 'VM Network'; networkLabel.append(select);
      const name = document.createElement('input'); name.className = 'nic-new-name'; name.placeholder = 'New switch and port group name'; name.maxLength = 31; name.hidden = true; name.disabled = true; name.setAttribute('aria-label', `${nic.label} new network name`);
      select.addEventListener('change', () => { name.hidden = select.value !== '__create__'; name.disabled = name.hidden; name.required = !name.hidden; });
      const connected = document.createElement('label'); connected.className = 'check';
      const check = document.createElement('input'); check.type = 'checkbox'; check.className = 'nic-connected'; check.checked = nic.ge6; check.setAttribute('aria-label', `${nic.label} connected`);
      connected.append(check, document.createTextNode('Connected'));
      row.append(title, networkLabel, connected, name); $('interface-rows').append(row);
    }
    applyGe6Override();
    $('interface-message').textContent = `${ova.interfaces.length} interfaces loaded. Only GE6 is connected by default.`;
  } catch (error) { if (sequence === interfaceRequest) $('interface-message').textContent = error.message; }
}

function applyGe6Override() {
  if (!interfaceBinding) return;
  const override = Number($('ge6-nic').value);
  for (const nic of interfaceBinding.interfaces) {
    const check = document.querySelector(`.interface-row[data-index="${nic.index}"] .nic-connected`);
    check.checked = override ? nic.index === override : nic.ge6;
  }
}

function interfaceMappings() {
  if (!interfaceBinding) return [];
  if (interfaceBinding.filename !== $('saved-file').value || interfaceBinding.host !== $('host').value.trim() || interfaceBinding.user !== $('user').value.trim()) throw new Error('Reload interfaces and networks for the selected OVA and ESXi connection.');
  return interfaceBinding.interfaces.map((nic) => {
    const row = document.querySelector(`.interface-row[data-index="${nic.index}"]`);
    const create = row.querySelector('.nic-network').value === '__create__';
    return { index: nic.index, label: nic.label, network: create ? row.querySelector('.nic-new-name').value.trim() : row.querySelector('.nic-network').value, connected: row.querySelector('.nic-connected').checked, create };
  });
}

const $ = (id) => document.getElementById(id);
let events = null;
let currentJob = null;
let busy = false;
let downloadedFiles = [];
let vmConnection = null;
let vmLoading = false;
let vmRequest = 0;

function invalidateVms() {
  vmRequest += 1;
  vmLoading = false;
  vmConnection = null;
  $('clone-source').replaceChildren(new Option('Refresh VMs from the saved ESXi connection', ''));
  $('vm-list-message').textContent = 'Save the connection and refresh to load its VMs.';
  $('refresh-vms').disabled = false;
}

async function refreshVms() {
  if (vmLoading) return;
  const sequence = ++vmRequest;
  const previous = vmConnection;
  const selected = $('clone-source').value;
  vmLoading = true;
  $('refresh-vms').disabled = true;
  $('vm-list-message').className = 'field-help';
  $('vm-list-message').textContent = 'Loading VM inventory from ESXi…';
  $('clone-source').replaceChildren(new Option('Loading VMs…', ''));
  vmConnection = null;
  try {
    const data = await api('/api/vms');
    if (sequence !== vmRequest) return;
    if (data.host !== $('host').value.trim() || data.user !== $('user').value.trim()) {
      throw new Error('Save the connection above, then refresh the VM list.');
    }
    $('clone-source').replaceChildren(new Option(data.vms.length ? 'Select a source VM' : 'No VMs found on this ESXi host', ''));
    for (const vm of data.vms) $('clone-source').add(new Option(`${vm.name} · VM ID ${vm.id}`, vm.id));
    vmConnection = { host: data.host, user: data.user, vms: data.vms };
    if (previous && previous.host === data.host && previous.user === data.user && data.vms.some((vm) => vm.id === selected)) $('clone-source').value = selected;
    $('vm-list-message').textContent = `${data.vms.length} ${data.vms.length === 1 ? 'VM' : 'VMs'} on ${data.host}.`;
  } catch (error) {
    if (sequence !== vmRequest) return;
    $('clone-source').replaceChildren(new Option('Unable to load VMs · click Refresh to retry', ''));
    $('vm-list-message').className = 'field-help error';
    $('vm-list-message').textContent = error.message;
  } finally {
    if (sequence === vmRequest) { vmLoading = false; $('refresh-vms').disabled = false; }
  }
}

function setTab(tab) {
  const misc = tab === 'misc';
  $('deployment-panel').hidden = misc;
  $('misc-panel').hidden = !misc;
  for (const name of ['deployment', 'misc']) {
    const selected = name === tab;
    $(`tab-${name}`).classList.toggle('nav-active', selected);
    $(`tab-${name}`).setAttribute('aria-selected', selected);
    $(`tab-${name}`).tabIndex = selected ? 0 : -1;
  }
  $('breadcrumb').textContent = misc ? 'Misc ESXi Actions' : 'Deployment';
  $('page-title').textContent = misc ? 'Misc ESXi Actions' : 'Velo OVA deployment tool for testing';
  $('page-subtitle').textContent = misc ? 'Clone VMs and create internal networks with the same saved connection.' : 'Download appliances to your library, then deploy them to your ESXi host.';
  $('page-eyebrow').textContent = misc ? 'MANAGE YOUR LAB' : 'DEPLOY & CONFIGURE';
  $('page-tag').textContent = misc ? 'ESXi ACTIONS' : 'OVA WORKFLOW';
  history.replaceState(null, '', `#${tab}`);
  if (misc && !vmConnection && $('host').value.trim() && $('user').value.trim()) refreshVms();
}

function output(id, job = currentJob) {
  return $(`${job && job.kind && job.kind !== 'deployment' ? 'misc-' : ''}${id}`);
}


function fileSize(bytes) {
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MiB`;
  return `${(bytes / 1073741824).toFixed(2)} GiB`;
}

async function refreshLibrary() {
  try {
    const data = await api('/api/downloads');
    downloadedFiles = data.files;
    const previous = $('saved-file').value;
    $('saved-file').replaceChildren(new Option('Select a downloaded OVA', ''));
    $('library-files').replaceChildren();
    let total = 0;
    for (const file of data.files) {
      total += file.size;
      $('saved-file').add(new Option(file.name, file.name));
      const row = document.createElement('tr');
      const name = document.createElement('td');
      name.className = 'file-name';
      name.textContent = file.name;
      if (file.in_use) {
        const badge = document.createElement('span');
        badge.className = 'file-badge';
        badge.textContent = 'In use';
        name.append(badge);
      }
      const size = document.createElement('td');
      size.textContent = fileSize(file.size);
      const date = document.createElement('td');
      date.textContent = new Date(file.modified).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
      const actions = document.createElement('td');
      actions.className = 'file-actions';
      const use = document.createElement('button');
      use.type = 'button';
      use.className = 'file-use';
      use.dataset.kind = 'use';
      use.textContent = 'Use for deployment';
      use.addEventListener('click', () => {
        if (busy) return;
        clearInterfaces();
        $('saved-file').value = file.name;
        refreshInterfaces();
        $('deployment-heading').scrollIntoView({ behavior: 'smooth', block: 'start' });
        $('vm-name').focus({ preventScroll: true });
      });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'file-delete';
      remove.dataset.kind = 'delete';
      remove.dataset.filename = file.name;
      remove.textContent = 'Delete';
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete "${file.name}" from OVA-download? This removes only the saved file, not any deployed VM.`)) return;
        remove.disabled = true;
        try {
          await api('/api/downloads/delete', { method: 'POST', body: JSON.stringify({ filename: file.name }) });
          $('library-message').className = 'message';
          $('library-message').textContent = `Deleted ${file.name}.`;
          await refreshLibrary();
        } catch (error) {
          $('library-message').className = 'message error';
          $('library-message').textContent = error.message;
          await refreshLibrary();
        }
      });
      actions.append(use, remove);
      row.append(name, size, date, actions);
      $('library-files').append(row);
    }
    if (data.files.some((file) => file.name === previous)) $('saved-file').value = previous;
    else if (previous) clearInterfaces();
    $('library-count').textContent = `${data.files.length} ${data.files.length === 1 ? 'file' : 'files'} · ${fileSize(total)}`;
    $('library-empty').hidden = data.files.length > 0;
    document.querySelector('.library-scroll').hidden = data.files.length === 0;
    setBusy(busy);
  } catch (error) {
    $('library-message').className = 'message error';
    $('library-message').textContent = error.message;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error('The server could not complete this request. Check the container logs.'); }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function connectionSaved(data) {
  $('connection-status').textContent = data.password_saved ? 'Saved' : 'Not configured';
  $('connection-status').className = `pill${data.password_saved ? ' saved' : ''}`;
  $('password').placeholder = data.password_saved ? 'Saved · leave blank to keep' : 'Enter password';
  $('password-help').textContent = data.password_saved ? 'Leave blank to keep the saved password for this host.' : 'Stored in your local creds.json.';
}

async function saveConnection() {
  const data = await api('/api/connection', { method: 'POST', body: JSON.stringify({ host: $('host').value, user: $('user').value, password: $('password').value }) });
  if (interfaceBinding && (interfaceBinding.host !== data.host || interfaceBinding.user !== data.user)) clearInterfaces();
  if (vmConnection && (vmConnection.host !== data.host || vmConnection.user !== data.user)) invalidateVms();
  $('password').value = '';
  connectionSaved(data);
  $('connection-message').className = 'message';
  $('connection-message').textContent = 'Connection saved.';
}

function setBusy(value) {
  busy = value;
  $('deployment-fields').disabled = value;
  $('download-fields').disabled = value;
  $('download-button').textContent = value ? 'Action in progress' : 'Download OVA';
  $('clone-fields').disabled = value;
  $('network-fields').disabled = value;
  $('clone-button').firstElementChild.textContent = value ? 'Action in progress' : 'Clone VM';
  $('network-button').firstElementChild.textContent = value ? 'Action in progress' : 'Create network';
  $('deploy-button').firstElementChild.textContent = value ? 'Action in progress' : 'Deploy saved OVA';
  document.querySelectorAll('[data-kind="use"]').forEach((button) => { button.disabled = value; });
  document.querySelectorAll('[data-kind="delete"]').forEach((button) => {
    button.disabled = downloadedFiles.some((file) => file.name === button.dataset.filename && file.in_use);
  });
}

function jobState(job) {
  currentJob = job;
  setBusy(job.status === 'running');
  output('job-status', job).textContent = ({ running: 'Running', success: 'Complete', failed: 'Failed' })[job.status];
  output('job-status', job).className = `pill ${job.status}`;
  output('job-caption', job).textContent = job.name;
  const phase = job.phase;
  output('phase-label', job).textContent = job.status === 'failed' ? 'Stopped · review the output below' : ({ download: 'Downloading OVA', deploy: 'Deploying to ESXi', clone: 'Cloning VM', network: 'Creating internal network', done: 'Action complete' })[phase];
  output('job-progress', job).value = job.percent || 0;
  output('progress-label', job).textContent = `${job.percent || 0}%`;
  if (!job.kind || job.kind === 'deployment') {
    ['download', 'deploy', 'done'].forEach((stage) => { $(`stage-${stage}`).className = phase === stage ? 'active' : ''; });
  } else {
    ['start', 'action', 'done'].forEach((stage) => { $(`misc-stage-${stage}`).className = (phase === 'done' ? stage === 'done' : stage === 'action') ? 'active' : ''; });
  }
}

function follow(job, clear = false) {
  if (events) events.close();
  if (clear) output('console', job).textContent = '';
  output('console-empty', job).hidden = true;
  jobState(job);
  events = new EventSource(`/api/jobs/${job.id}/events`);
  events.addEventListener('output', (event) => {
    const line = JSON.parse(event.data);
    const console = output('console', job);
    const atBottom = console.scrollHeight - console.scrollTop - console.clientHeight < 60;
    console.textContent += `${line.text}\n`;
    if (atBottom) console.scrollTop = console.scrollHeight;
    jobState({ ...currentJob, phase: line.phase, percent: line.percent });
  });
  events.addEventListener('complete', async (event) => {
    jobState(JSON.parse(event.data));
    events.close();
    const selectDownload = setupDownload && currentJob.status === 'success';
    setupDownload = false;
    await refreshLibrary();
    if (selectDownload) {
      clearInterfaces();
      $('saved-file').value = currentJob.filename;
      $('interface-options').open = true;
      refreshInterfaces();
    }
  });
  events.onerror = async () => {
    output('phase-label', job).textContent = 'Reconnecting to live output…';
    try {
      const data = await api('/api/jobs/current');
      if (!data.job || data.job.id !== job.id) {
        events.close();
        setBusy(false);
        output('job-status', job).textContent = 'Interrupted';
        output('job-status', job).className = 'pill failed';
        output('phase-label', job).textContent = 'Server restarted · check ESXi before retrying';
      }
    } catch { /* EventSource retries when the connection returns. */ }
  };
}

$('connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('save-connection').disabled = true;
  try { await saveConnection(); if (!$('misc-panel').hidden) refreshVms(); }
  catch (error) { $('connection-message').className = 'message error'; $('connection-message').textContent = error.message; }
  finally { $('save-connection').disabled = false; }
});

$('deploy-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  if (!$('connection-form').reportValidity()) return;
  $('deploy-message').textContent = '';
  setBusy(true);
  try {
    await saveConnection();
    const nicMappings = interfaceMappings();
    const data = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ nic_mappings: nicMappings, network_host: interfaceBinding?.host, network_user: interfaceBinding?.user, source: 'downloaded', filename: $('saved-file').value, name: $('vm-name').value, datastore: $('datastore').value, iso_folder: $('iso-folder').value, ge6_nic: $('ge6-nic').value }) });
    follow(data.job, true);
    refreshLibrary();
  } catch (error) { $('deploy-message').textContent = error.message; setBusy(false); }
});

for (const prefix of ['', 'misc-']) {
  $(`${prefix}copy-output`).addEventListener('click', async () => {
    const button = $(`${prefix}copy-output`);
    try { await navigator.clipboard.writeText($(`${prefix}console`).textContent); button.textContent = 'Copied'; }
    catch { button.textContent = 'Select the log to copy'; }
    setTimeout(() => { button.textContent = 'Copy output'; }, 2000);
  });
}

async function runMisc(kind, data) {
  if (busy || !$('connection-form').reportValidity()) return;
  $(`${kind}-message`).textContent = '';
  setBusy(true);
  try {
    await saveConnection();
    const result = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ kind, ...data }) });
    follow(result.job, true);
  } catch (error) {
    $(`${kind}-message`).textContent = error.message;
    setBusy(false);
  }
}

$('clone-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const selected = vmConnection && vmConnection.vms.find((vm) => vm.id === $('clone-source').value);
  if (!selected) { $('clone-message').textContent = 'Refresh the VM list and select a source VM.'; return; }
  runMisc('clone', { source_vm: selected.name, source_vm_id: selected.id, source_host: vmConnection.host, source_user: vmConnection.user, name: $('clone-name').value, datastore: $('clone-datastore').value });
});
$('network-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runMisc('network', { name: $('network-name').value });
});

async function initialize() {
  try {
    const data = await api('/api/connection');
    $('host').value = data.host;
    $('user').value = data.user;
    connectionSaved(data);
    const state = await api('/api/jobs/current');
    if (state.job) follow(state.job, true);
    await refreshLibrary();
    if (!$('misc-panel').hidden) refreshVms();
  } catch (error) { $('connection-message').className = 'message error'; $('connection-message').textContent = error.message; }
}
$('refresh-library').addEventListener('click', refreshLibrary);
$('refresh-vms').addEventListener('click', refreshVms);
for (const field of ['host', 'user']) $(field).addEventListener('input', invalidateVms);
for (const tab of ['deployment', 'misc']) {
  $(`tab-${tab}`).addEventListener('click', () => setTab(tab));
  $(`tab-${tab}`).addEventListener('keydown', (event) => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 'deployment' : event.key === 'End' ? 'misc' : tab === 'deployment' ? 'misc' : 'deployment';
      setTab(next);
      $(`tab-${next}`).focus();
    }
  });
}
setTab(location.hash === '#misc' ? 'misc' : 'deployment');
setInterval(refreshLibrary, 5000);
initialize();

$('refresh-interfaces').addEventListener('click', refreshInterfaces);
$('saved-file').addEventListener('change', () => { clearInterfaces(); refreshInterfaces(); });
$('ge6-nic').addEventListener('change', applyGe6Override);
for (const field of ['host', 'user']) $(field).addEventListener('input', clearInterfaces);
$('download-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true); $('download-message').textContent = '';
  try {
    const data = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ source: 'url', url: $('ova-url').value, filename: $('filename').value, name: 'OVA download', download_only: true, skip_download_tls_verification: $('skip-download-tls').checked, reuse_existing: $('reuse-existing').checked }) });
    setupDownload = true; follow(data.job, true);
  } catch (error) { $('download-message').textContent = error.message; setBusy(false); }
});
