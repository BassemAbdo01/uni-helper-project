const ALARM_NAME = 'nexora-auto-sync';
const BRIGHTSPACE_PATTERNS = ['https://brightspace.carleton.ca/*', 'https://*.brightspace.com/*'];

async function getConfig() {
  const v = await chrome.storage.sync.get(['apiBase', 'token', 'autoSyncEnabled', 'intervalMinutes']);
  return {
    apiBase: String(v.apiBase || '').replace(/\/$/, ''),
    token: String(v.token || ''),
    autoSyncEnabled: v.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(v.intervalMinutes) || 30)
  };
}

async function setStatus(patch) {
  const old = await chrome.storage.local.get(['syncStatus']);
  await chrome.storage.local.set({
    syncStatus: { ...(old.syncStatus || {}), ...patch },
    lastSync: new Date().toISOString()
  });
}

async function api(path, options = {}) {
  const cfg = await getConfig();
  if (!cfg.apiBase || !cfg.token) throw new Error('Configure the Nexora URL and extension key first.');
  const response = await fetch(`${cfg.apiBase}${path}`, {
    method: options.method || 'GET',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${cfg.token}`
    },
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `Nexora returned ${response.status}`);
  return data;
}

async function findBrightspaceTab() {
  const tabs = await chrome.tabs.query({ url: BRIGHTSPACE_PATTERNS });
  const usable = tabs.find(t => /\/d2l\//i.test(t.url || '')) || tabs[0];
  if (!usable) throw new Error('Open Brightspace and sign in first, then run the scan again.');
  return usable;
}

async function sendToBrightspace(message, retries = 2) {
  const tab = await findBrightspaceTab();
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await chrome.tabs.sendMessage(tab.id, message);
    } catch (error) {
      lastError = error;
      if (attempt < retries) await new Promise(r => setTimeout(r, 900));
    }
  }
  throw new Error(`Could not communicate with Brightspace: ${lastError?.message || 'unknown error'}`);
}

async function discoverCourses() {
  const result = await sendToBrightspace({ type: 'NEXORA_DISCOVER_COURSES' });
  if (!result?.ok) throw new Error(result?.error || 'Brightspace course discovery failed.');
  const courses = result.courses || [];
  const imported = await api('/api/sync/courses', { method: 'POST', body: { courses } });
  await chrome.storage.local.set({ detectedCourses: courses });
  await setStatus({
    phase: 'courses-imported',
    courseDiscovery: { found: courses.length, ...imported },
    error: ''
  });
  return { courses, imported };
}

async function syncAllCourses() {
  const cfg = await getConfig();
  if (!cfg.apiBase || !cfg.token) throw new Error('Configure Nexora Sync first.');

  await setStatus({ running: true, phase: 'checking-session', error: '', currentCourse: '', progress: '' });
  const session = await sendToBrightspace({ type: 'NEXORA_SESSION_CHECK' });
  if (!session?.ok) throw new Error(session?.error || 'Your Brightspace session is not available.');

  const status = await api('/api/sync/status');
  const courses = status.courses || [];
  if (!courses.length) throw new Error('No imported courses found. Run “Detect & clean courses” first.');

  const summary = {
    courses: courses.length,
    scanned: 0,
    failed: 0,
    itemsFound: 0,
    added: 0,
    updated: 0,
    unchanged: 0,
    preserved: 0,
    details: [],
    method: 'smart-sync-api-first'
  };

  for (let i = 0; i < courses.length; i++) {
    const course = courses[i];
    await setStatus({
      running: true,
      phase: 'smart-sync',
      currentCourse: course.code,
      progress: `${i + 1}/${courses.length}`
    });

    try {
      const scan = await sendToBrightspace({
        type: 'NEXORA_SMART_SCAN_COURSE',
        course: {
          source_uid: String(course.source_uid || ''),
          course_code: course.code,
          course_name: course.name,
          brightspace_url: course.url
        }
      }, 1);

      if (!scan?.ok) throw new Error(scan?.error || 'Unknown Brightspace scan error.');
      const items = Array.isArray(scan.items) ? scan.items : [];
      const trustworthy = scan.complete === true && scan.authenticated === true;

      // Never erase a course because a selector/API temporarily returned zero.
      const replaceCourses = trustworthy && items.length > 0 ? [course.code] : [];
      const imported = await api('/api/sync/content', {
        method: 'POST',
        body: {
          items,
          replace_courses: replaceCourses,
          scan_meta: {
            method: scan.method,
            complete: trustworthy,
            diagnostics: scan.diagnostics || {}
          }
        }
      });

      summary.scanned++;
      summary.itemsFound += items.length;
      summary.added += imported.added || 0;
      summary.updated += imported.updated || 0;
      summary.unchanged += imported.unchanged || 0;
      if (!replaceCourses.length) summary.preserved++;
      summary.details.push({
        course: course.code,
        items: items.length,
        method: scan.method,
        complete: trustworthy,
        diagnostics: scan.diagnostics || {}
      });
    } catch (error) {
      summary.failed++;
      summary.details.push({ course: course.code, items: 0, error: error.message });
    }
  }

  await setStatus({
    running: false,
    phase: 'complete',
    currentCourse: '',
    progress: '',
    lastContentSync: new Date().toISOString(),
    contentSummary: summary,
    error: summary.failed === courses.length ? 'Every course failed. Open Brightspace and try again.' : ''
  });
  return summary;
}

async function configureAlarm() {
  const { autoSyncEnabled, intervalMinutes } = await getConfig();
  await chrome.alarms.clear(ALARM_NAME);
  if (autoSyncEnabled) chrome.alarms.create(ALARM_NAME, { periodInMinutes: intervalMinutes });
}

chrome.runtime.onInstalled.addListener(async () => {
  const c = await chrome.storage.sync.get(['autoSyncEnabled', 'intervalMinutes']);
  await chrome.storage.sync.set({
    autoSyncEnabled: c.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(c.intervalMinutes) || 30)
  });
  await configureAlarm();
});

chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== ALARM_NAME) return;
  const { autoSyncEnabled } = await getConfig();
  if (!autoSyncEnabled) return;
  try { await syncAllCourses(); }
  catch (error) { await setStatus({ running: false, phase: 'error', error: error.message }); }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === 'NEXORA_SCAN_OPEN_TABS') {
        sendResponse({ ok: true, ...(await discoverCourses()) });
        return;
      }
      if (message?.type === 'NEXORA_SYNC_ALL_COURSES') {
        sendResponse({ ok: true, summary: await syncAllCourses() });
        return;
      }
      if (message?.type === 'NEXORA_START_IMPORT') {
        const tab = await chrome.tabs.create({ url: 'https://brightspace.carleton.ca/d2l/home', active: true });
        sendResponse({ ok: true, tabId: tab.id });
        return;
      }
      if (message?.type === 'NEXORA_CONFIG_CHANGED') {
        await configureAlarm();
        sendResponse({ ok: true });
        return;
      }
      if (message?.type === 'NEXORA_STATUS') {
        const config = await getConfig();
        const local = await chrome.storage.local.get(['syncStatus', 'lastSync', 'detectedCourses']);
        sendResponse({ ok: true, configured: !!(config.apiBase && config.token), config, ...local });
        return;
      }
      sendResponse({ ok: true });
    } catch (error) {
      await setStatus({ running: false, phase: 'error', error: error.message });
      sendResponse({ ok: false, error: error.message });
    }
  })();
  return true;
});
