const ALARM_NAME = 'nexora-sync-scan';

async function getConfig() {
  return chrome.storage.sync.get(['apiBase', 'token']);
}

async function postToNexora(path, payload) {
  const {apiBase = '', token = ''} = await getConfig();
  if (!apiBase || !token) throw new Error('Configure Nexora Sync first.');
  const response = await fetch(`${apiBase.replace(/\/$/, '')}${path}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`},
    body: JSON.stringify(payload)
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.error || `Nexora returned ${response.status}`);
  return data;
}

async function saveResult(type, data) {
  const current = await chrome.storage.local.get(['lastResult']);
  await chrome.storage.local.set({
    lastResult: {...(current.lastResult || {}), [type]: data},
    lastSync: new Date().toISOString()
  });
}

async function scanOpenBrightspaceTabs() {
  const tabs = await chrome.tabs.query({url: ['https://brightspace.carleton.ca/*', 'https://*.brightspace.com/*']});
  const results = [];
  for (const tab of tabs) {
    try {
      const result = await chrome.tabs.sendMessage(tab.id, {type: 'NEXORA_SCAN_FULL'});
      results.push({tabId: tab.id, url: tab.url, ...result});
    } catch (error) {
      results.push({tabId: tab.id, url: tab.url, ok: false, error: error.message});
    }
  }
  await saveResult('manualScan', {ok: true, tabs: results.length, results});
  return results;
}

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create(ALARM_NAME, {periodInMinutes: 5}));
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name === ALARM_NAME) await scanOpenBrightspaceTabs();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === 'NEXORA_DISCOVERY_DIAGNOSTIC') {
        await saveResult('diagnostic', {ok: true, ...(message.diagnostics || {})});
        sendResponse({ok: true}); return;
      }
      if (message?.type === 'NEXORA_COURSES') {
        const courses = message.courses || [];
        await saveResult('detectedCourses', {ok: true, found: courses.length, courses});
        const data = await postToNexora('/api/sync/courses', {courses});
        await saveResult('courses', data);
        sendResponse(data); return;
      }
      if (message?.type === 'NEXORA_SCAN_OPEN_TABS') {
        const results = await scanOpenBrightspaceTabs();
        sendResponse({ok: true, results}); return;
      }
      if (message?.type === 'NEXORA_START_IMPORT') {
        const tab = await chrome.tabs.create({url: 'https://brightspace.carleton.ca/d2l/home', active: true});
        sendResponse({ok: true, tabId: tab.id}); return;
      }
      if (message?.type === 'NEXORA_STATUS') {
        const config = await getConfig();
        const local = await chrome.storage.local.get(['lastResult', 'lastSync']);
        sendResponse({ok: true, configured: !!(config.apiBase && config.token), ...local}); return;
      }
      sendResponse({ok: true});
    } catch (error) {
      await saveResult('error', {ok: false, error: error.message});
      sendResponse({ok: false, error: error.message});
    }
  })();
  return true;
});
