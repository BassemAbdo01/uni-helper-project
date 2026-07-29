const ALARM_NAME = 'nexora-sync-scan';
const queue = [];
let queueBusy = false;

async function getConfig() {
  return chrome.storage.sync.get(['apiBase','token','brightspaceBase']);
}
async function postToNexora(path, payload) {
  const { apiBase = '', token = '' } = await getConfig();
  if (!apiBase || !token) throw new Error('Configure Nexora Sync first.');
  const response = await fetch(`${apiBase.replace(/\/$/,'')}${path}`, {
    method: 'POST',
    headers: {'Content-Type':'application/json','Authorization':`Bearer ${token}`},
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Sync failed (${response.status})`);
  return data;
}
async function saveResult(type, data) {
  const current = await chrome.storage.local.get(['lastResult']);
  await chrome.storage.local.set({
    lastResult: {...(current.lastResult || {}), [type]: data},
    lastSync: new Date().toISOString()
  });
}

async function enqueueUrls(urls = []) {
  const state = await chrome.storage.local.get(['seenScanUrls']);
  const seen = new Set(state.seenScanUrls || []);
  for (const url of urls) {
    if (!url || seen.has(url) || queue.includes(url)) continue;
    if (!/^https:\/\//i.test(url)) continue;
    queue.push(url);
  }
  processQueue();
}

async function processQueue() {
  if (queueBusy || !queue.length) return;
  queueBusy = true;
  const url = queue.shift();
  let tab;
  try {
    tab = await chrome.tabs.create({url, active:false});
    await new Promise(resolve => setTimeout(resolve, 6500));
    try { await chrome.tabs.sendMessage(tab.id, {type:'NEXORA_SCAN_FULL'}); } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 3500));
    const state = await chrome.storage.local.get(['seenScanUrls']);
    const seen = new Set(state.seenScanUrls || []);
    seen.add(url);
    await chrome.storage.local.set({seenScanUrls:[...seen].slice(-300)});
  } catch (error) {
    await saveResult('crawlerError', {ok:false,error:error.message,url});
  } finally {
    if (tab?.id) { try { await chrome.tabs.remove(tab.id); } catch (_) {} }
    queueBusy = false;
    setTimeout(processQueue, 700);
  }
}

async function scanOpenBrightspaceTabs() {
  const tabs = await chrome.tabs.query({url:['https://brightspace.carleton.ca/*','https://*.brightspace.com/*']});
  for (const tab of tabs) {
    try { await chrome.tabs.sendMessage(tab.id,{type:'NEXORA_SCAN_FULL'}); } catch (_) {}
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME,{periodInMinutes:1});
});
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name === ALARM_NAME) await scanOpenBrightspaceTabs();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === 'NEXORA_COURSES') {
        const data = await postToNexora('/api/sync/courses',{courses:message.courses || []});
        await saveResult('courses',data);
        await enqueueUrls((message.courses || []).map(c => c.brightspace_url).filter(Boolean));
        sendResponse(data); return;
      }
      if (message?.type === 'NEXORA_ITEMS') {
        const data = await postToNexora('/api/sync/content',{items:message.items || []});
        await saveResult('content',data);
        sendResponse(data); return;
      }
      if (message?.type === 'NEXORA_DISCOVERED_URLS') {
        await enqueueUrls(message.urls || []);
        sendResponse({ok:true,queued:(message.urls || []).length}); return;
      }
      if (message?.type === 'NEXORA_START_IMPORT') {
        await chrome.storage.local.set({seenScanUrls:[],lastResult:{}});
        const target = message.url || 'https://brightspace.carleton.ca/d2l/home';
        const tab = await chrome.tabs.create({url:target,active:true});
        sendResponse({ok:true,tabId:tab.id}); return;
      }
      if (message?.type === 'NEXORA_STATUS') {
        const config = await getConfig();
        const local = await chrome.storage.local.get(['lastResult','lastSync','seenScanUrls']);
        sendResponse({ok:true,configured:!!(config.apiBase&&config.token),...local,queued:queue.length,busy:queueBusy}); return;
      }
      sendResponse({ok:true});
    } catch (error) {
      await saveResult('error',{ok:false,error:error.message});
      sendResponse({ok:false,error:error.message});
    }
  })();
  return true;
});
