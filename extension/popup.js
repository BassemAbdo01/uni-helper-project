const $ = id => document.getElementById(id);

async function refresh() {
  const s = await chrome.storage.sync.get(['apiBase', 'token']);
  $('apiBase').value = s.apiBase || '';
  $('token').value = s.token || '';
  const result = await chrome.runtime.sendMessage({type: 'NEXORA_STATUS'}).catch(() => null);
  if (!result) return;
  const lr = result.lastResult || {};
  const d = lr.diagnostic || {};
  const detected = lr.detectedCourses || {};
  const imported = lr.courses || {};
  const err = lr.error || {};
  const lines = [
    result.configured ? '✓ Connected' : 'Not configured',
    result.lastSync ? `Last activity: ${new Date(result.lastSync).toLocaleString()}` : 'No scan yet',
    Number.isFinite(d.found) ? `Detected on page: ${d.found} course(s)` : '',
    d.candidateAnchors !== undefined ? `Course-link candidates: ${d.candidateAnchors} · DOM roots: ${d.roots}` : '',
    detected.ok ? `Prepared for import: ${detected.found || 0}` : '',
    imported.ok ? `Nexora import: +${imported.added || 0}, ${imported.updated || 0} updated, ${imported.unchanged || 0} unchanged` : '',
    err.error ? `Error: ${err.error}` : '',
    d.names?.length ? `\n${d.names.slice(0, 8).join('\n')}` : ''
  ].filter(Boolean);
  $('status').textContent = lines.join('\n');
}

refresh();
$('save').onclick = async () => {
  const apiBase = $('apiBase').value.trim();
  const token = $('token').value.trim();
  await chrome.storage.sync.set({apiBase, token});
  $('status').textContent = 'Connection saved.';
  await refresh();
};
$('import').onclick = async () => {
  await $('save').onclick();
  await chrome.runtime.sendMessage({type: 'NEXORA_START_IMPORT'});
  window.close();
};
$('scan').onclick = async () => {
  $('status').textContent = 'Scanning open Carleton Brightspace pages…';
  await chrome.runtime.sendMessage({type: 'NEXORA_SCAN_OPEN_TABS'});
  setTimeout(refresh, 1200);
};
