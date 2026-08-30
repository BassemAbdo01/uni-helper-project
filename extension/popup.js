const $ = id => document.getElementById(id);

const D2L_PATH = /\/d2l\//i;

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function originOf(url) {
  try { return new URL(String(url || '')).origin; } catch (_) { return ''; }
}

async function refresh() {
  const result = await chrome.runtime.sendMessage({ type: 'NEXORA_STATUS' }).catch(() => null);
  if (!result) return;

  $('apiBase').value = result.config?.apiBase || '';
  $('token').value = result.config?.token || '';
  $('autoSync').checked = result.config?.autoSyncEnabled !== false;
  $('interval').value = String(result.config?.intervalMinutes || 30);

  // The university is whatever Brightspace this student actually uses; it is
  // learned from their own tab rather than shipped as a list of domains.
  const origin = result.config?.brightspaceOrigin || '';
  const granted = !!result.originGranted;
  const box = $('siteBox');
  box.classList.toggle('on', granted && !!origin);
  if (origin) {
    $('siteState').textContent = granted ? 'Connected' : 'Permission needed';
    $('siteOrigin').textContent = granted
      ? origin
      : `${origin} — allow it so background sync can run.`;
  } else {
    $('siteState').textContent = 'No university connected';
    $('siteOrigin').textContent = "Open your university's Brightspace in a tab, then press the button below.";
  }

  const s = result.syncStatus || {};
  const c = s.courseDiscovery || {};
  const x = s.contentSummary || {};
  const lines = [
    result.configured ? '✓ Connected to Nexora' : 'Nexora URL and key not set',
    result.lastSync ? `Last activity: ${new Date(result.lastSync).toLocaleString()}` : 'No activity yet',
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
  await chrome.runtime.sendMessage({ type: 'NEXORA_CONFIG_CHANGED' });
  await refresh();
}

/* Asking for the student's own Brightspace domain, one origin at a time.
   chrome.permissions.request has to be called straight from a user gesture,
   which is why it lives in the popup and not in the service worker. */
$('allow').onclick = async () => {
  const tab = await activeTab();
  const origin = originOf(tab?.url);

  if (!origin) {
    $('status').textContent = 'Could not read the current tab.';
    return;
  }
  if (!D2L_PATH.test(tab.url || '')) {
    $('status').textContent =
      'That tab is not a Brightspace page. Open your university\'s Brightspace ' +
      '(the address will contain /d2l/), then press this again.';
    return;
  }

  let granted = false;
  try {
    granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
  } catch (error) {
    $('status').textContent = `Chrome refused the permission request: ${error?.message || error}`;
    return;
  }

  if (!granted) {
    $('status').textContent = 'Permission declined, so Nexora cannot read that Brightspace.';
    return;
  }

  const result = await chrome.runtime.sendMessage({ type: 'NEXORA_SET_ORIGIN', origin });
  $('status').textContent = result?.ok
    ? `${origin} connected. Press "Detect & clean courses" next.`
    : (result?.error || 'Could not save that university.');
  await refresh();
};

$('save').onclick = save;

$('scan').onclick = async () => {
  await save();
  $('status').textContent = 'Scanning your open Brightspace for courses…';
  await chrome.runtime.sendMessage({ type: 'NEXORA_SCAN_OPEN_TABS' });
  await refresh();
};

$('sync').onclick = async () => {
  await save();
  $('status').textContent = 'Smart Sync is checking lectures, assignments, quizzes and announcements…';
  chrome.runtime.sendMessage({ type: 'NEXORA_SYNC_ALL_COURSES' }).then(refresh);
  setTimeout(refresh, 1500);
};

$('outline').onclick = async () => {
  await save();
  $('status').textContent = 'Reading the current page for a course outline…';
  try {
    const tab = await activeTab();
    if (!tab?.id) throw new Error('No active tab was found.');

    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    } catch (_) {
      // Clicking the toolbar icon grants activeTab, so this reaches any page.
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    }
    if (!response?.ok || !response.text) {
      throw new Error(response?.error || 'No outline text was detected on this page.');
    }

    await chrome.storage.local.set({
      pendingOutlineText: response.text,
      pendingOutlineTitle: response.title || 'Detected course outline',
      pendingOutlineSource: response.url || tab.url || ''
    });

    const cfg = await chrome.storage.sync.get(['apiBase']);
    const base = String(cfg.apiBase || '').replace(/\/$/, '');
    if (!base) throw new Error('Save your Nexora URL first.');
    await chrome.tabs.create({ url: `${base}/grade-calculator` });
    $('status').textContent = 'Outline detected. Opening the Nexora grade calculator…';
  } catch (error) {
    $('status').textContent = `Outline detection failed: ${error?.message || error}`;
  }
};

refresh();
