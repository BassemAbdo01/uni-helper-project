const ALARM_NAME = 'nexora-auto-sync';
const BRIGHTSPACE_PATTERNS = ['https://brightspace.carleton.ca/*', 'https://*.brightspace.com/*'];

async function getConfig() {
  const values = await chrome.storage.sync.get(['apiBase', 'token', 'autoSyncEnabled', 'intervalMinutes']);
  return {
    apiBase: values.apiBase || '',
    token: values.token || '',
    autoSyncEnabled: values.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(values.intervalMinutes) || 30)
  };
}

async function setStatus(patch) {
  const old = await chrome.storage.local.get(['syncStatus']);
  await chrome.storage.local.set({
    syncStatus: {...(old.syncStatus || {}), ...patch},
    lastSync: new Date().toISOString()
  });
}

async function api(path, options = {}) {
  const {apiBase, token} = await getConfig();
  if (!apiBase || !token) throw new Error('Configure Nexora URL and extension key first.');
  const response = await fetch(`${apiBase.replace(/\/$/, '')}${path}`, {
    method: options.method || 'GET',
    headers: {'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`},
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `Nexora returned ${response.status}`);
  return data;
}

function waitForTab(tabId, timeoutMs = 25000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Brightspace page timed out.'));
    }, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        setTimeout(resolve, 1800);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function scanOpenBrightspaceTabs() {
  const tabs = await chrome.tabs.query({url: BRIGHTSPACE_PATTERNS});
  const results = [];
  for (const tab of tabs) {
    try {
      const result = await chrome.tabs.sendMessage(tab.id, {type: 'NEXORA_SCAN_HOME'});
      results.push({tabId: tab.id, url: tab.url, ...result});
    } catch (error) {
      results.push({tabId: tab.id, url: tab.url, ok: false, error: error.message});
    }
  }
  await setStatus({phase: 'course-discovery', openTabs: tabs.length, tabResults: results});
  return results;
}

function coursePageUrls(course) {
  const url = new URL(course.url || course.brightspace_url);
  const host = url.origin;
  const ou = course.source_uid || (url.pathname.match(/\/d2l\/home\/(\d+)/) || [])[1];
  if (!ou) return [course.url || course.brightspace_url].filter(Boolean);
  return [
    `${host}/d2l/le/lessons/${ou}/home`,
    `${host}/d2l/le/content/${ou}/Home`,
    `${host}/d2l/lms/dropbox/user/folders_list.d2l?ou=${ou}`,
    `${host}/d2l/lms/quizzing/user/quizzes_list.d2l?ou=${ou}`,
    `${host}/d2l/lms/news/main.d2l?ou=${ou}`
  ];
}

async function scanCoursePage(course, url) {
  let tab;
  try {
    tab = await chrome.tabs.create({url, active: false});
    await waitForTab(tab.id);
    return await chrome.tabs.sendMessage(tab.id, {
      type: 'NEXORA_SCAN_COURSE_PAGE',
      course: {
        source_uid: course.source_uid,
        course_code: course.code || course.course_code,
        course_name: course.name || course.course_name,
        url
      }
    });
  } finally {
    if (tab?.id) await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function syncAllCourses() {
  const config = await getConfig();
  if (!config.apiBase || !config.token) throw new Error('Configure Nexora Sync first.');
  await setStatus({running: true, phase: 'loading-courses', error: ''});
  const status = await api('/api/sync/status');
  const courses = status.courses || [];
  const summary = {courses: courses.length, scanned: 0, failed: 0, itemsFound: 0, added: 0, updated: 0, unchanged: 0, details: []};

  for (let i = 0; i < courses.length; i++) {
    const course = courses[i];
    await setStatus({running: true, phase: 'scanning-course', currentCourse: course.code, progress: `${i + 1}/${courses.length}`});
    const deduped = new Map();
    const pageDiagnostics = [];
    for (const target of coursePageUrls(course)) {
      try {
        const result = await scanCoursePage(course, target);
        pageDiagnostics.push({url: target, ok: !!result?.ok, found: result?.items?.length || 0, pageKind: result?.pageKind || ''});
        for (const item of result?.items || []) deduped.set(item.source_uid || item.source_url, item);
      } catch (error) {
        pageDiagnostics.push({url: target, ok: false, error: error.message});
      }
    }

    const items = [...deduped.values()];
    try {
      const imported = items.length ? await api('/api/sync/content', {method: 'POST', body: {items}}) : {added: 0, updated: 0, unchanged: 0};
      summary.scanned += 1;
      summary.itemsFound += items.length;
      summary.added += imported.added || 0;
      summary.updated += imported.updated || 0;
      summary.unchanged += imported.unchanged || 0;
      summary.details.push({course: course.code, items: items.length, pages: pageDiagnostics});
    } catch (error) {
      summary.failed += 1;
      summary.details.push({course: course.code, error: error.message, pages: pageDiagnostics});
    }
  }

  await setStatus({running: false, phase: 'complete', currentCourse: '', progress: '', lastContentSync: new Date().toISOString(), contentSummary: summary});
  return summary;
}

async function configureAlarm() {
  const {autoSyncEnabled, intervalMinutes} = await getConfig();
  await chrome.alarms.clear(ALARM_NAME);
  if (autoSyncEnabled) chrome.alarms.create(ALARM_NAME, {periodInMinutes: intervalMinutes});
}

chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.sync.get(['autoSyncEnabled', 'intervalMinutes']);
  await chrome.storage.sync.set({
    autoSyncEnabled: current.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(current.intervalMinutes) || 30)
  });
  await configureAlarm();
});

chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== ALARM_NAME) return;
  const {autoSyncEnabled} = await getConfig();
  if (!autoSyncEnabled) return;
  try { await syncAllCourses(); } catch (error) { await setStatus({running: false, phase: 'error', error: error.message}); }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === 'NEXORA_COURSES') {
        const courses = message.courses || [];
        await chrome.storage.local.set({detectedCourses: courses});
        const data = await api('/api/sync/courses', {method: 'POST', body: {courses}});
        await setStatus({phase: 'courses-imported', courseDiscovery: {found: courses.length, ...data}});
        sendResponse({ok: true, ...data}); return;
      }
      if (message?.type === 'NEXORA_SCAN_OPEN_TABS') {
        sendResponse({ok: true, results: await scanOpenBrightspaceTabs()}); return;
      }
      if (message?.type === 'NEXORA_SYNC_ALL_COURSES') {
        sendResponse({ok: true, summary: await syncAllCourses()}); return;
      }
      if (message?.type === 'NEXORA_START_IMPORT') {
        const tab = await chrome.tabs.create({url: 'https://brightspace.carleton.ca/d2l/home', active: true});
        sendResponse({ok: true, tabId: tab.id}); return;
      }
      if (message?.type === 'NEXORA_CONFIG_CHANGED') {
        await configureAlarm(); sendResponse({ok: true}); return;
      }
      if (message?.type === 'NEXORA_STATUS') {
        const config = await getConfig();
        const local = await chrome.storage.local.get(['syncStatus', 'lastSync', 'detectedCourses']);
        sendResponse({ok: true, configured: !!(config.apiBase && config.token), config, ...local}); return;
      }
      sendResponse({ok: true});
    } catch (error) {
      await setStatus({running: false, phase: 'error', error: error.message});
      sendResponse({ok: false, error: error.message});
    }
  })();
  return true;
});
