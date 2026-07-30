const ALARM_NAME = 'nexora-auto-sync';
const DEFAULT_ORIGIN = 'https://brightspace.carleton.ca';
const BRIGHTSPACE_PATTERNS = ['https://brightspace.carleton.ca/*', 'https://*.brightspace.com/*'];

const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function getConfig() {
  const v = await chrome.storage.sync.get([
    'apiBase', 'token', 'autoSyncEnabled', 'intervalMinutes', 'brightspaceOrigin'
  ]);
  return {
    apiBase: String(v.apiBase || '').replace(/\/$/, ''),
    token: String(v.token || ''),
    autoSyncEnabled: v.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(v.intervalMinutes) || 30),
    brightspaceOrigin: normalizeOrigin(v.brightspaceOrigin || DEFAULT_ORIGIN)
  };
}

function normalizeOrigin(value) {
  try {
    const url = new URL(String(value || DEFAULT_ORIGIN));
    return url.origin;
  } catch (_) {
    return DEFAULT_ORIGIN;
  }
}

async function setStatus(patch) {
  const old = await chrome.storage.local.get(['syncStatus']);
  const syncStatus = { ...(old.syncStatus || {}), ...patch };
  await chrome.storage.local.set({ syncStatus, lastSync: new Date().toISOString() });
  await updateBadge(syncStatus);
}

async function updateBadge(status) {
  try {
    if (status.running) {
      await chrome.action.setBadgeText({ text: '…' });
      await chrome.action.setBadgeBackgroundColor({ color: '#5b5ce2' });
    } else if (status.connectionExpired || status.error) {
      await chrome.action.setBadgeText({ text: '!' });
      await chrome.action.setBadgeBackgroundColor({ color: '#c0392b' });
    } else {
      await chrome.action.setBadgeText({ text: '' });
    }
  } catch (_) {}
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

async function brightspaceFetch(path, options = {}) {
  const cfg = await getConfig();
  const origin = normalizeOrigin(options.origin || cfg.brightspaceOrigin);
  const url = new URL(path, origin).href;
  const response = await fetch(url, {
    method: options.method || 'GET',
    credentials: 'include',
    redirect: 'follow',
    cache: 'no-store',
    headers: {
      Accept: options.accept || 'application/json, text/plain, */*',
      ...(options.headers || {})
    }
  });

  const finalUrl = response.url || url;
  const contentType = response.headers.get('content-type') || '';
  if (response.status === 401 || response.status === 403 || /\/login|\/d2l\/login/i.test(finalUrl)) {
    const error = new Error('Brightspace connection expired. Open Brightspace and sign in again.');
    error.code = 'AUTH_EXPIRED';
    throw error;
  }
  if (!response.ok) throw new Error(`Brightspace returned ${response.status} for ${new URL(url).pathname}`);
  return { response, contentType, finalUrl, origin };
}

async function brightspaceJson(path, origin) {
  const { response, contentType, finalUrl } = await brightspaceFetch(path, { origin, accept: 'application/json' });
  if (!/json/i.test(contentType)) {
    const text = await response.text();
    if (/sign in|login|authenticate/i.test(text.slice(0, 6000))) {
      const error = new Error('Brightspace connection expired. Open Brightspace and sign in again.');
      error.code = 'AUTH_EXPIRED';
      throw error;
    }
    throw new Error(`Expected JSON but received ${contentType || 'unknown content type'} at ${finalUrl}`);
  }
  return response.json();
}

async function findBrightspaceTab() {
  const tabs = await chrome.tabs.query({ url: BRIGHTSPACE_PATTERNS });
  return tabs.find(t => /\/d2l\//i.test(t.url || '')) || tabs[0] || null;
}

async function sendToBrightspace(message, retries = 2) {
  const tab = await findBrightspaceTab();
  if (!tab) throw new Error('Open Brightspace once and sign in so Nexora can connect your courses.');
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try { return await chrome.tabs.sendMessage(tab.id, message); }
    catch (error) {
      lastError = error;
      if (attempt < retries) await sleep(900);
    }
  }
  throw new Error(`Could not communicate with Brightspace: ${lastError?.message || 'unknown error'}`);
}

async function discoverCourses() {
  const tab = await findBrightspaceTab();
  if (!tab) throw new Error('Open the Brightspace My Courses page and sign in first.');
  const origin = normalizeOrigin(tab.url);
  await chrome.storage.sync.set({ brightspaceOrigin: origin });

  const result = await sendToBrightspace({ type: 'NEXORA_DISCOVER_COURSES' });
  if (!result?.ok) throw new Error(result?.error || 'Brightspace course discovery failed.');
  const courses = result.courses || [];
  const imported = await api('/api/sync/courses', { method: 'POST', body: { courses } });
  await chrome.storage.local.set({ detectedCourses: courses });
  await setStatus({
    phase: 'courses-imported',
    courseDiscovery: { found: courses.length, ...imported },
    connectionExpired: false,
    error: ''
  });
  return { courses, imported };
}

function htmlToText(value) {
  if (!value) return '';
  const html = typeof value === 'string' ? value : value.Html || value.Text || value.html || value.text || '';
  return clean(String(html)
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<br\s*\/?\s*>/gi, '\n')
    .replace(/<\/p\s*>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
  ).slice(0, 120000);
}

function dateValue(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return clean(value.Date || value.date || value.StartDate || value.startDate || '');
}

function absolute(origin, href) {
  try { return new URL(href, origin).href; } catch (_) { return ''; }
}

function classifyContent(title, url, activityType) {
  const text = `${title} ${url} ${activityType || ''}`.toLowerCase();
  const ext = ((title.match(/\.([a-z0-9]{1,8})\b/i) || url.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
  if (/practice|sample question|review question|worksheet|problem set/.test(text)) return 'practice';
  if (/assignment|dropbox/.test(text)) return 'assignment';
  if (/quiz|quizzing/.test(text)) return 'quiz';
  if (/announcement|news/.test(text)) return 'announcement';
  if (['py', 'c', 'cpp', 'h', 'java', 'js', 'ts', 'ipynb', 'zip', 'rar', '7z', 'csv', 'xlsx', 'xls'].includes(ext)) return 'resource';
  if (['pdf', 'ppt', 'pptx'].includes(ext) && /lecture|slides?|week\s*\d+|module\s*\d+|chapter\s*\d+|lesson\s*\d+/i.test(title)) return 'lecture';
  if (/lecture|slides?|lesson\s*\d+/i.test(title)) return 'lecture';
  if (['pdf', 'ppt', 'pptx', 'doc', 'docx'].includes(ext)) return 'resource';
  return 'page';
}

function itemUid(courseId, type, id, fallback) {
  return clean(`${courseId}|${type}|${id || fallback}`).slice(0, 255);
}

function fileInfoFromTopic(topic, course, version, origin) {
  const url = topic.Url || topic.URL || topic.ContentUrl || topic.ContentURL || topic.Location || '';
  const absoluteUrl = absolute(origin, url);
  const title = clean(topic.Title || topic.Name || 'Untitled content');
  const extension = ((title.match(/\.([a-z0-9]{1,8})\b/i) || absoluteUrl.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
  const topicId = topic.Id || topic.TopicId || topic.Identifier || '';
  const downloadUrl = topicId ? `${origin}/d2l/api/le/${version}/${course.source_uid}/content/topics/${topicId}/file` : '';
  return { source_url: absoluteUrl || downloadUrl, download_url: downloadUrl, file_name: title, file_ext: extension };
}

function flattenToc(toc, course, version, origin) {
  const items = [];
  const walk = (nodes, parentPath = []) => {
    if (!Array.isArray(nodes)) return;
    for (const node of nodes) {
      const title = clean(node.Title || node.Name || 'Untitled');
      const id = node.Id || node.ModuleId || node.TopicId || node.Identifier || title;
      const isModule = Array.isArray(node.Modules) || Array.isArray(node.Topics) || Number(node.Type) === 0 || /module/i.test(clean(node.Type));
      if (isModule) {
        const nextPath = title ? [...parentPath, title] : parentPath;
        walk(node.Modules || node.Children || [], nextPath);
        walk(node.Topics || node.Content || [], nextPath);
        continue;
      }
      const file = fileInfoFromTopic(node, course, version, origin);
      const activity = node.ActivityType || node.TypeIdentifier || node.Type || '';
      items.push({
        course: course.code,
        source_uid: itemUid(course.source_uid, 'content', id, file.source_url || title),
        title,
        content_type: classifyContent(title, file.source_url, activity),
        source_url: file.download_url || file.source_url,
        download_url: file.download_url,
        file_name: file.file_name,
        file_ext: file.file_ext,
        module_name: parentPath.join(' › '),
        content_text: htmlToText(node.Description || node.Body || node.Instructions || ''),
        published_at: dateValue(node.StartDate || node.LastModifiedDate || node.ModifiedDate),
        smart_sync: true
      });
    }
  };
  walk(Array.isArray(toc) ? toc : toc.Modules || toc.Items || toc.Content || toc.Objects || []);
  return items;
}

function listFromPaged(data) {
  if (Array.isArray(data)) return data;
  return data?.Objects || data?.Items || data?.Results || data?.objects || data?.items || [];
}

function assignmentItems(data, course, origin) {
  return listFromPaged(data).map(row => {
    const id = row.Id || row.FolderId || row.Identifier || row.Name;
    const title = clean(row.Name || row.Title || 'Assignment');
    return {
      course: course.code,
      source_uid: itemUid(course.source_uid, 'assignment', id, title),
      title,
      content_type: 'assignment',
      source_url: `${origin}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=${encodeURIComponent(id)}&grpid=0&isprv=0&bp=0&ou=${course.source_uid}`,
      module_name: clean(row.Category?.Name || row.CategoryName || 'Assignments'),
      content_text: htmlToText(row.Instructions || row.Description || ''),
      published_at: dateValue(row.DueDate || row.EndDate || row.StartDate),
      due_at: dateValue(row.DueDate),
      smart_sync: true
    };
  }).filter(x => x.title);
}

function quizItems(data, course, origin) {
  return listFromPaged(data).map(row => {
    const id = row.QuizId || row.Id || row.Identifier || row.Name;
    const title = clean(row.Name || row.Title || 'Quiz');
    return {
      course: course.code,
      source_uid: itemUid(course.source_uid, 'quiz', id, title),
      title,
      content_type: 'quiz',
      source_url: `${origin}/d2l/lms/quizzing/user/quiz_summary.d2l?qi=${encodeURIComponent(id)}&ou=${course.source_uid}`,
      module_name: clean(row.Category?.Name || row.CategoryName || 'Quizzes'),
      content_text: htmlToText(row.Description || row.Header || ''),
      published_at: dateValue(row.StartDate || row.DueDate || row.EndDate),
      due_at: dateValue(row.DueDate || row.EndDate),
      smart_sync: true
    };
  }).filter(x => x.title);
}

function announcementItems(data, course, origin) {
  return listFromPaged(data).map(row => {
    const id = row.Id || row.NewsItemId || row.Identifier || row.Title;
    const title = clean(row.Title || row.Name || 'Announcement');
    return {
      course: course.code,
      source_uid: itemUid(course.source_uid, 'announcement', id, title),
      title,
      content_type: 'announcement',
      source_url: `${origin}/d2l/lms/news/main.d2l?ou=${course.source_uid}`,
      module_name: 'Announcements',
      content_text: htmlToText(row.Body || row.Content || row.Description || ''),
      published_at: dateValue(row.StartDate || row.CreatedDate || row.LastModifiedDate),
      smart_sync: true
    };
  }).filter(x => x.title);
}

async function detectApiVersions(origin) {
  const candidates = [];
  try {
    const versions = await brightspaceJson('/d2l/api/versions/', origin);
    const rows = Array.isArray(versions) ? versions : versions?.Versions || versions?.versions || [];
    for (const row of rows) {
      const product = clean(row.ProductCode || row.productCode || row.Product || '').toLowerCase();
      const version = clean(row.LatestVersion || row.Version || row.version || '');
      if (version && (!product || /le|lp/.test(product))) candidates.push(version);
    }
  } catch (_) {}
  for (const version of ['1.96', '1.95', '1.90', '1.82', '1.75', '1.74', '1.67', '1.60', '1.50', '1.48', '1.44']) {
    if (!candidates.includes(version)) candidates.push(version);
  }
  return candidates;
}

async function firstWorking(builders, versions, origin) {
  const errors = [];
  for (const version of versions) {
    for (const build of builders) {
      const path = build(version);
      try { return { data: await brightspaceJson(path, origin), version, path }; }
      catch (error) {
        if (error.code === 'AUTH_EXPIRED') throw error;
        errors.push(`${path}: ${error.message}`);
      }
    }
  }
  throw new Error(errors.slice(-4).join(' | ') || 'No compatible endpoint found');
}

async function scanCourseDirect(course, origin) {
  const versions = await detectApiVersions(origin);
  const diagnostics = { versionsTried: versions.slice(0, 10), endpoints: {}, errors: {} };
  const items = [];
  let successfulSections = 0;

  const sections = [
    ['content', [v => `/d2l/api/le/${v}/${course.source_uid}/content/toc`], (data, v) => flattenToc(data, course, v, origin)],
    ['assignments', [v => `/d2l/api/le/${v}/${course.source_uid}/dropbox/folders/`], data => assignmentItems(data, course, origin)],
    ['quizzes', [v => `/d2l/api/le/${v}/${course.source_uid}/quizzes/`], data => quizItems(data, course, origin)],
    ['announcements', [v => `/d2l/api/le/${v}/${course.source_uid}/news/`], data => announcementItems(data, course, origin)]
  ];

  for (const [name, builders, transform] of sections) {
    try {
      const result = await firstWorking(builders, versions, origin);
      diagnostics.endpoints[name] = result.path;
      const rows = transform(result.data, result.version);
      items.push(...rows);
      successfulSections++;
      diagnostics[`${name}Count`] = rows.length;
    } catch (error) {
      if (error.code === 'AUTH_EXPIRED') throw error;
      diagnostics.errors[name] = error.message;
    }
  }

  const deduped = new Map();
  for (const item of items) deduped.set(item.source_uid, item);
  return {
    ok: successfulSections > 0,
    authenticated: true,
    complete: successfulSections >= 3,
    method: 'background-session-api',
    diagnostics: { ...diagnostics, successfulSections },
    items: [...deduped.values()],
    error: successfulSections ? '' : 'Brightspace API routes were unavailable for this session.'
  };
}

async function verifySession(origin) {
  try {
    // A JSON endpoint is safer than guessing from homepage HTML.
    await brightspaceJson('/d2l/api/versions/', origin);
    return { ok: true };
  } catch (error) {
    if (error.code === 'AUTH_EXPIRED') return { ok: false, expired: true, error: error.message };
    // Some tenants restrict the versions route even while signed in; use home as a fallback.
    try {
      const { response, finalUrl } = await brightspaceFetch('/d2l/home', { origin, accept: 'text/html' });
      const text = await response.text();
      const signedOut = /\/login|sign in|authenticate/i.test(finalUrl) || (/sign in|login|authenticate/i.test(text.slice(0, 7000)) && !/logout|my courses|course home/i.test(text.slice(0, 7000)));
      return signedOut ? { ok: false, expired: true, error: 'Brightspace connection expired. Open Brightspace and sign in again.' } : { ok: true };
    } catch (fallbackError) {
      return { ok: false, expired: fallbackError.code === 'AUTH_EXPIRED', error: fallbackError.message };
    }
  }
}

async function syncAllCourses(trigger = 'manual') {
  const cfg = await getConfig();
  if (!cfg.apiBase || !cfg.token) throw new Error('Configure Nexora Sync first.');
  const origin = cfg.brightspaceOrigin;

  await setStatus({ running: true, phase: 'checking-session', trigger, error: '', connectionExpired: false, currentCourse: '', progress: '' });
  const session = await verifySession(origin);
  if (!session.ok) {
    await setStatus({ running: false, phase: 'reconnect-required', connectionExpired: !!session.expired, error: session.error });
    throw new Error(session.error);
  }

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
    method: 'background-smart-sync',
    trigger
  };

  for (let i = 0; i < courses.length; i++) {
    const course = courses[i];
    await setStatus({ running: true, phase: 'smart-sync', trigger, currentCourse: course.code, progress: `${i + 1}/${courses.length}` });
    try {
      const scan = await scanCourseDirect(course, origin);
      if (!scan.ok) throw new Error(scan.error || 'Unknown Brightspace scan error.');
      const items = Array.isArray(scan.items) ? scan.items : [];
      const trustworthy = scan.complete === true && scan.authenticated === true;
      const replaceCourses = trustworthy && items.length > 0 ? [course.code] : [];
      const imported = await api('/api/sync/content', {
        method: 'POST',
        body: {
          items,
          replace_courses: replaceCourses,
          scan_meta: { method: scan.method, complete: trustworthy, diagnostics: scan.diagnostics || {}, trigger }
        }
      });

      summary.scanned++;
      summary.itemsFound += items.length;
      summary.added += imported.added || 0;
      summary.updated += imported.updated || 0;
      summary.unchanged += imported.unchanged || 0;
      if (!replaceCourses.length) summary.preserved++;
      summary.details.push({ course: course.code, items: items.length, method: scan.method, complete: trustworthy, diagnostics: scan.diagnostics || {} });
    } catch (error) {
      if (error.code === 'AUTH_EXPIRED') {
        await setStatus({ running: false, phase: 'reconnect-required', connectionExpired: true, error: error.message });
        throw error;
      }
      summary.failed++;
      summary.details.push({ course: course.code, items: 0, error: error.message });
    }
  }

  await setStatus({
    running: false,
    phase: 'complete',
    trigger,
    currentCourse: '',
    progress: '',
    lastContentSync: new Date().toISOString(),
    connectionExpired: false,
    contentSummary: summary,
    error: summary.failed === courses.length ? 'Every course failed. Open Brightspace once to verify your session.' : ''
  });
  return summary;
}

async function configureAlarm() {
  const { autoSyncEnabled, intervalMinutes } = await getConfig();
  await chrome.alarms.clear(ALARM_NAME);
  if (autoSyncEnabled) {
    await chrome.alarms.create(ALARM_NAME, {
      delayInMinutes: Math.min(1, intervalMinutes),
      periodInMinutes: intervalMinutes
    });
  }
}


async function fetchAndProcessLecture(request) {
  const cfg = await getConfig();
  if (!request?.source_uid || !request?.source_url) throw new Error('Nexora did not provide a valid lecture reference.');
  const { response, contentType, finalUrl } = await brightspaceFetch(request.source_url, {
    origin: cfg.brightspaceOrigin,
    accept: 'application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*'
  });
  const blob = await response.blob();
  if (!blob.size) throw new Error('Brightspace returned an empty lecture file.');
  if (/text\/html/i.test(contentType)) {
    const preview = (await blob.slice(0, 12000).text()).toLowerCase();
    if (/sign in|log in|authenticate|saml|single sign-on/.test(preview)) {
      const error = new Error('Brightspace connection expired. Open Brightspace and sign in again.');
      error.code = 'AUTH_EXPIRED';
      throw error;
    }
    throw new Error('Brightspace returned a webpage instead of the lecture file. Run a fresh course sync so Nexora can capture the direct download link.');
  }
  if (blob.size > 30 * 1024 * 1024) throw new Error('This lecture is larger than Nexora’s 30 MB processing limit.');
  const fallbackName = clean(request.title || 'lecture-file');
  let fileName = fallbackName;
  try {
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    if (match) fileName = decodeURIComponent(match[1].replace(/[\"]/g, '').trim());
    else {
      const pathName = new URL(finalUrl).pathname.split('/').pop();
      if (pathName && /\.[a-z0-9]{2,6}$/i.test(pathName)) fileName = decodeURIComponent(pathName);
    }
  } catch (_) {}
  if (!/\.[a-z0-9]{2,6}$/i.test(fileName)) {
    const ext = /pdf/i.test(contentType) ? '.pdf' : /presentation|powerpoint/i.test(contentType) ? '.pptx' : /wordprocessingml/i.test(contentType) ? '.docx' : '.bin';
    fileName += ext;
  }
  const form = new FormData();
  form.append('source_uid', request.source_uid);
  form.append('file', blob, fileName);
  const uploaded = await fetch(`${cfg.apiBase}/api/sync/process-content`, {
    method: 'POST', headers: { Authorization: `Bearer ${cfg.token}` }, body: form
  });
  let data = {};
  try { data = await uploaded.json(); } catch (_) {}
  if (!uploaded.ok) throw new Error(data.error || `Nexora could not process this lecture (${uploaded.status}).`);
  return data;
}

chrome.runtime.onInstalled.addListener(async () => {
  const c = await chrome.storage.sync.get(['autoSyncEnabled', 'intervalMinutes', 'brightspaceOrigin']);
  await chrome.storage.sync.set({
    autoSyncEnabled: c.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(c.intervalMinutes) || 30),
    brightspaceOrigin: normalizeOrigin(c.brightspaceOrigin || DEFAULT_ORIGIN)
  });
  await configureAlarm();
});

chrome.runtime.onStartup.addListener(configureAlarm);

chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== ALARM_NAME) return;
  const { autoSyncEnabled } = await getConfig();
  if (!autoSyncEnabled) return;
  try { await syncAllCourses('automatic'); }
  catch (error) {
    const old = await chrome.storage.local.get(['syncStatus']);
    if (!old.syncStatus?.connectionExpired) {
      await setStatus({ running: false, phase: 'error', error: error.message });
    }
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === 'NEXORA_SCAN_OPEN_TABS') {
        sendResponse({ ok: true, ...(await discoverCourses()) });
        return;
      }
      if (message?.type === 'NEXORA_SYNC_ALL_COURSES') {
        sendResponse({ ok: true, summary: await syncAllCourses('manual') });
        return;
      }
      if (message?.type === 'NEXORA_START_IMPORT') {
        const cfg = await getConfig();
        const tab = await chrome.tabs.create({ url: `${cfg.brightspaceOrigin}/d2l/home`, active: true });
        sendResponse({ ok: true, tabId: tab.id });
        return;
      }
      if (message?.type === 'NEXORA_FETCH_AND_PROCESS_LECTURE') {
        sendResponse({ ok: true, result: await fetchAndProcessLecture(message.request || {}) });
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
      const expired = error.code === 'AUTH_EXPIRED' || /connection expired|sign in again/i.test(error.message || '');
      await setStatus({ running: false, phase: expired ? 'reconnect-required' : 'error', connectionExpired: expired, error: error.message });
      sendResponse({ ok: false, error: error.message, connectionExpired: expired });
    }
  })();
  return true;
});
