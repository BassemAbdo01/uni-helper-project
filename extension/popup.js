const $ = id => document.getElementById(id);

async function refresh() {
  const result = await chrome.runtime.sendMessage({type: 'NEXORA_STATUS'}).catch(() => null);
  if (!result) return;
  $('apiBase').value = result.config?.apiBase || '';
  $('token').value = result.config?.token || '';
  $('autoSync').checked = result.config?.autoSyncEnabled !== false;
  $('interval').value = String(result.config?.intervalMinutes || 30);
  const s = result.syncStatus || {};
  const c = s.courseDiscovery || {};
  const x = s.contentSummary || {};
  const lines = [
    result.configured ? '✓ Connected' : 'Not configured',
    result.lastSync ? `Last activity: ${new Date(result.lastSync).toLocaleString()}` : 'No activity yet',
    result.config?.brightspaceOrigin ? `Brightspace: ${result.config.brightspaceOrigin}` : '',
    s.running ? `Working: ${s.phase || 'syncing'} ${s.currentCourse || ''} ${s.progress || ''}` : '',
    c.found !== undefined ? `Courses: ${c.found} detected · +${c.added || 0} · ${c.updated || 0} cleaned/updated · ${c.unchanged || 0} unchanged` : '',
    x.courses !== undefined ? `Smart Sync: ${x.scanned}/${x.courses} courses · ${x.itemsFound} items mapped${x.preserved ? ` · ${x.preserved} safely preserved` : ''}` : '',
    x.courses !== undefined ? `Nexora content: +${x.added || 0} · ${x.updated || 0} updated · ${x.unchanged || 0} unchanged · ${x.failed || 0} failed` : '',
    s.connectionExpired ? '⚠ Brightspace session expired — open Brightspace and sign in again.' : '',
    s.error ? `Error: ${s.error}` : '',
    x.details?.length ? '\n' + x.details.slice(-8).map(d => `${d.course}: ${d.items ?? 0} items${d.error ? ` — ${d.error}` : ''}`).join('\n') : ''
  ].filter(Boolean);
  $('status').textContent = lines.join('\n');
}

async function save() {
  await chrome.storage.sync.set({
    apiBase: $('apiBase').value.trim(),
    token: $('token').value.trim(),
    autoSyncEnabled: $('autoSync').checked,
    intervalMinutes: Number($('interval').value)
  });
  await chrome.runtime.sendMessage({type: 'NEXORA_CONFIG_CHANGED'});
  await refresh();
}

$('save').onclick = save;
$('import').onclick = async () => { await save(); await chrome.runtime.sendMessage({type: 'NEXORA_START_IMPORT'}); window.close(); };
$('scan').onclick = async () => { await save(); $('status').textContent = 'Scanning open Brightspace homepage…'; await chrome.runtime.sendMessage({type: 'NEXORA_SCAN_OPEN_TABS'}); await refresh(); };
$('sync').onclick = async () => { await save(); $('status').textContent = 'Background Smart Sync is checking lectures, assignments, quizzes and announcements…'; chrome.runtime.sendMessage({type: 'NEXORA_SYNC_ALL_COURSES'}).then(refresh); setTimeout(refresh, 1500); };
refresh();

$('outline').onclick = async () => {
  await save();
  $('status').textContent = 'Reading the current LMS page for a course outline…';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error('No active tab was found.');
    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    } catch (_) {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    }
    if (!response?.ok || !response.text) throw new Error(response?.error || 'No outline text was detected on this page.');
    await chrome.storage.local.set({
      pendingOutlineText: response.text,
      pendingOutlineTitle: response.title || 'Detected course outline',
      pendingOutlineSource: response.url || tab.url || ''
    });
    const cfg = await chrome.storage.sync.get(['apiBase']);
    const base = String(cfg.apiBase || '').replace(/\/$/, '');
    if (!base) throw new Error('Save your Nexora URL first.');
    await chrome.tabs.create({ url: `${base}/grade-calculator` });
    $('status').textContent = 'Outline detected. Opening Nexora Grade Intelligence…';
  } catch (error) {
    $('status').textContent = `Outline detection failed: ${error?.message || error}`;
  }
};
