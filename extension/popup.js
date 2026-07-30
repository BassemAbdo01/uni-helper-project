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
