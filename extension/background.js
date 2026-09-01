const ALARM_NAME = 'nexora-auto-sync';
const KEEPALIVE_ALARM = 'nexora-keepalive';

// Every Brightspace instance, whoever hosts it, serves its app under /d2l/ and
// exposes the same /d2l/api/le/ REST API. That path is the reliable signal --
// the domain is not, because most universities self-host on their own.
const D2L_PATH = /\/d2l\//i;
const DEFAULT_ORIGIN = '';

// A student should never have to type this. It is still editable under
// Advanced for anyone running their own copy.
const DEFAULT_API_BASE = 'https://uni-helper-project.onrender.com';

const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function getConfig() {
  const v = await chrome.storage.sync.get([
    'apiBase', 'token', 'autoSyncEnabled', 'intervalMinutes', 'brightspaceOrigin',
    'includeFinished', 'importScope', 'importTerm'
  ]);
  // importScope is the student's answer to "everything, or just one term?".
  // includeFinished was the 3.3 spelling of the same choice as a buried
  // checkbox; anyone who ticked it keeps meaning 'all' after they update.
  const scope = v.importScope === 'all' || v.importScope === 'term'
    ? v.importScope
    : (v.includeFinished === true ? 'all' : '');
  return {
    apiBase: String(v.apiBase || DEFAULT_API_BASE).replace(/\/$/, ''),
    token: String(v.token || ''),
    autoSyncEnabled: v.autoSyncEnabled !== false,
    intervalMinutes: Math.max(15, Number(v.intervalMinutes) || 30),
    brightspaceOrigin: normalizeOrigin(v.brightspaceOrigin || DEFAULT_ORIGIN),
    includeFinished: scope === 'all',
    importScope: scope,
    importTerm: String(v.importTerm || '')
  };
}

function normalizeOrigin(value) {
  try {
    return new URL(String(value || '')).origin;
  } catch (_) {
    return DEFAULT_ORIGIN;   // '' until the student's own Brightspace is seen
  }
}

// Has the student granted us their university's domain? Needed for background
// sync; a click on the toolbar icon covers the active tab on its own.
async function hasOriginPermission(origin) {
  if (!origin) return false;
  try {
    return await chrome.permissions.contains({ origins: [`${origin}/*`] });
  } catch (_) {
    return false;
  }
}

// A Manifest V3 worker is collected after roughly thirty seconds without an
// event, and a full sync runs for minutes. Touching an extension API on an
// interval resets that timer, so a sync survives the popup closing and the
// student clicking elsewhere. Held only while work is genuinely in flight.
let keepAliveTimer = null;
let keepAliveDepth = 0;

function holdWorkerAwake() {
  keepAliveDepth += 1;
  if (!keepAliveTimer) {
    keepAliveTimer = setInterval(() => {
      chrome.runtime.getPlatformInfo().catch(() => {});
    }, 20000);
    // setInterval lives inside the worker, so it dies with the very worker it
    // was supposed to keep alive -- which is why a sync still stopped when the
    // popup was closed. An alarm is held by Chrome, not by the worker: it
    // fires whether or not the worker is running, and starts one if it is not.
    chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 });
  }
}

function releaseWorker() {
  keepAliveDepth = Math.max(0, keepAliveDepth - 1);
  if (keepAliveDepth === 0 && keepAliveTimer) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
    chrome.alarms.clear(KEEPALIVE_ALARM).catch(() => {});
  }
}

/* ---- resuming a sync the worker did not live to finish ----
   Closing the popup does not stop the work, but a collected worker does, and
   nothing then restarted it: the student saw "Ready to sync" on reopening and
   reasonably concluded it had given up. The marker below outlives the worker,
   and the keepalive alarm is what notices and picks the sync back up. */
const SYNC_GIVE_UP_MS = 20 * 60 * 1000;

async function markSyncInFlight(trigger) {
  await chrome.storage.local.set({ syncInFlight: { trigger, startedAt: Date.now() } });
}
async function clearSyncInFlight() {
  await chrome.storage.local.remove('syncInFlight');
}
async function resumeSyncIfDropped() {
  if (keepAliveDepth > 0) return;            // this worker is already on it
  const { syncInFlight } = await chrome.storage.local.get('syncInFlight');
  if (!syncInFlight) return;
  if (Date.now() - Number(syncInFlight.startedAt || 0) > SYNC_GIVE_UP_MS) {
    await clearSyncInFlight();
    await setStatus({ running: false, phase: 'error', error: 'The sync was interrupted and did not finish. Open Brightspace and sync again.' });
    return;
  }
  // Already-scanned courses are cheap the second time -- scanCourseDirect
  // compares against what the server holds -- so restarting the whole run is
  // simpler and safer than trying to resume from a saved index.
  try { await syncAllCourses(syncInFlight.trigger || 'resumed'); }
  catch (_) { /* setStatus in runSyncAllCourses has already recorded why */ }
}

async function notify(title, message) {
  // The popup is usually gone by the time a sync finishes; without this the
  // student has no way to know it worked.
  try {
    await chrome.notifications.create({
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon128.png'),
      title,
      message,
      priority: 0
    });
  } catch (_) {}
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

function withBookmark(path, bookmark) {
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}bookmark=${encodeURIComponent(bookmark)}`;
}

// Mirrors content.js's fetchAllPages: the background/direct-session sync path hits the same
// bookmark-paginated dropbox/quiz/news endpoints, so it needs the same cursor-following fix
// or large courses silently lose everything past page one here too.
async function brightspaceJsonAllPages(path, origin, { maxPages = 40 } = {}) {
  const merged = [];
  let nextPath = path;
  let pages = 0;
  while (nextPath && pages < maxPages) {
    const data = await brightspaceJson(nextPath, origin);
    pages++;
    const pageItems = listFromPaged(data);
    merged.push(...pageItems);
    const pagingInfo = data?.PagingInfo || data?.pagingInfo || null;
    const hasMore = !!(pagingInfo && (pagingInfo.HasMoreItems || pagingInfo.hasMoreItems));
    const bookmark = pagingInfo && (pagingInfo.Bookmark || pagingInfo.bookmark);
    nextPath = hasMore && bookmark ? withBookmark(path, bookmark) : null;
    if (hasMore && bookmark && pageItems.length === 0) break;
  }
  return { items: merged, pages, truncated: pages >= maxPages };
}

// Canvas, for students whose university forbids them a token. The browser can
// read what the server cannot, so the extension fetches and the server parses.
async function syncCanvasThroughSession(tab) {
  holdWorkerAwake();
  try {
    if (!tab?.id) throw new Error('Open Canvas in a tab first.');
    await ensureBrightspaceContentScript(tab);   // same injection, any LMS page

    const collected = await chrome.tabs.sendMessage(tab.id, { type: 'NEXORA_CANVAS_COLLECT' });
    if (!collected?.ok) throw new Error(collected?.error || 'Could not read Canvas from this page.');

    const result = await api('/api/canvas/ingest', {
      method: 'POST',
      body: { base_url: collected.base_url, pages: collected.pages }
    });
    await chrome.storage.local.set({ lastFullSync: Date.now() });
    return result;
  } finally {
    releaseWorker();
  }
}


async function findBrightspaceTab() {
  // Look at every open tab and pick the one that is actually Brightspace,
  // whatever domain the university runs it on. A tab sitting on /d2l/home or
  // deeper wins over one merely parked on the login page.
  const tabs = await chrome.tabs.query({});
  const candidates = tabs.filter(t => D2L_PATH.test(t.url || ''));
  if (!candidates.length) return null;
  return candidates.find(t => /\/d2l\/(home|le|lms)\b/i.test(t.url || ''))
      || candidates.find(t => t.active)
      || candidates[0];
}

async function pingBrightspaceContentScript(tabId) {
  try {
    const result = await chrome.tabs.sendMessage(tabId, { type: 'NEXORA_CONTENT_PING' });
    return result?.ok === true;
  } catch (_) {
    return false;
  }
}

async function ensureBrightspaceContentScript(tab) {
  if (!tab?.id) throw new Error('Brightspace tab is unavailable.');
  if (await pingBrightspaceContentScript(tab.id)) return;
  try {
    // Brightspace puts My Courses in a widget iframe, so the top document alone
    // is the one place the course cards are guaranteed not to be.
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ['content.js']
    });
  } catch (error) {
    const origin = normalizeOrigin(tab.url);
    if (origin && !(await hasOriginPermission(origin))) {
      throw new Error(`Nexora needs your permission for ${origin}. Open the Nexora Sync popup while that tab is in front and press "Allow this Brightspace".`);
    }
    throw new Error(`Nexora could not activate on this Brightspace page: ${error?.message || 'script injection failed'}`);
  }
  for (let attempt = 0; attempt < 8; attempt++) {
    if (await pingBrightspaceContentScript(tab.id)) return;
    await sleep(250);
  }
  throw new Error('Nexora activated on Brightspace, but the page did not respond. Refresh Brightspace once and try again.');
}

async function sendToBrightspace(message, retries = 2) {
  const tab = await findBrightspaceTab();
  if (!tab) throw new Error('Open your university\'s Brightspace in a tab and sign in, then try again.');
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      await ensureBrightspaceContentScript(tab);
      return await chrome.tabs.sendMessage(tab.id, message);
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        await sleep(500 + attempt * 500);
        try { await chrome.tabs.reload(tab.id); } catch (_) {}
        await sleep(900);
      }
    }
  }
  throw new Error(`Could not communicate with Brightspace: ${lastError?.message || 'unknown error'}`);
}

async function discoverCourses() {
  holdWorkerAwake();
  try {
    return await runDiscoverCourses();
  } finally {
    releaseWorker();
  }
}


async function runDiscoverCourses() {
  const tab = await findBrightspaceTab();
  if (!tab) throw new Error('Open your university\'s Brightspace My Courses page and sign in first.');
  const origin = normalizeOrigin(tab.url);
  await chrome.storage.sync.set({ brightspaceOrigin: origin });

  const config = await getConfig();
  const result = await sendToBrightspace({
    type: 'NEXORA_DISCOVER_COURSES',
    // An unanswered scope keeps the 3.3 behaviour: the term on screen.
    scope: config.importScope || 'term',
    term: config.importScope === 'term' ? config.importTerm : '',
    includeFinished: config.includeFinished
  });
  if (!result?.ok) throw new Error(result?.error || 'Brightspace course discovery failed.');
  const courses = result.courses || [];
  // replace lets the server drop courses an earlier sync stored that the
  // student's current choice does not include -- otherwise picking one term
  // only ever adds to the pile. `complete` is false when discovery fell back
  // to reading the page, which sees a single term tab and must never be
  // treated as the whole picture.
  const imported = await api('/api/sync/courses', {
    method: 'POST',
    body: { courses, replace: true, complete: result.complete === true, origin }
  });
  await chrome.storage.local.set({ detectedCourses: courses });
  // found is what the page offered, stored is what Nexora now holds. They differ
  // when two sections share a course code or a course carries a removal
  // tombstone, and showing the first while the dashboard shows the second is
  // what produced "62 here, 60 there".
  const courseDiscovery = { found: courses.length, ...imported, term: result.term || '' };
  await setStatus({
    phase: 'courses-imported',
    courseDiscovery,
    connectionExpired: false,
    error: ''
  });
  return { courses, imported, courseDiscovery, term: result.term || '' };
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

// moduleContext is the module/folder breadcrumb the item lives under. See content.js for the
// full rationale — the item's own title alone was missing lecture material filed under a
// numbered folder, and loose "assignment"/"quiz" keyword matches were duplicating real
// dropbox/quiz entries with unrelated instructions/rubric pages.
function classifyContent(title, url, activityType, moduleContext) {
  const ownText = `${title} ${url} ${activityType || ''}`.toLowerCase();
  const fullText = `${ownText} ${moduleContext || ''}`.toLowerCase();
  const ext = ((title.match(/\.([a-z0-9]{1,8})\b/i) || url.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
  const numberedLecturePattern = /(?:lecture|lec|slides?|week|module|chapter|lesson|unit)\s*[-_#:]?\s*\d+|\b(?:l|lec|wk|ch)\s*[-_#]?\s*\d+\b/i;

  if (/course\s*(?:outline|syllabus|information|guide)|syllabus|grading\s*(?:scheme|breakdown|policy)|evaluation\s*(?:scheme|breakdown)|marking\s*scheme|assessment\s*(?:scheme|breakdown)|course\s*policies/.test(ownText)) return 'outline';
  if (/practice|sample question|review question|worksheet|problem set/.test(ownText)) return 'practice';

  const isSupportingDoc = /instructions?|guideline|rubric|handout|answer key|solutions?|prep\b/.test(ownText);
  if (/assignment|dropbox/.test(ownText) && !isSupportingDoc) return 'assignment';
  if (/\bquiz(zing)?\b/.test(ownText) && !isSupportingDoc) return 'quiz';
  if (/announcement|news/.test(ownText)) return 'announcement';

  if (numberedLecturePattern.test(fullText)) return 'lecture';
  if (['mp4', 'mov', 'm4v', 'webm', 'mp3', 'm4a', 'wav'].includes(ext) && /lecture|lec|recording|class|week|module/.test(fullText)) return 'lecture';
  if (['py', 'c', 'cpp', 'h', 'java', 'js', 'ts', 'ipynb', 'zip', 'rar', '7z', 'csv', 'xlsx', 'xls'].includes(ext)) return 'resource';
  if (['pdf', 'ppt', 'pptx', 'doc', 'docx', 'mp4', 'mov', 'm4v', 'webm', 'mp3', 'm4a', 'wav'].includes(ext)) return 'resource';
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
      const moduleContext = parentPath.join(' › ');
      items.push({
        course: course.code,
        source_uid: itemUid(course.source_uid, 'content', id, file.source_url || title),
        title,
        content_type: classifyContent(title, file.source_url, activity, moduleContext),
        source_url: file.download_url || file.source_url,
        download_url: file.download_url,
        file_name: file.file_name,
        file_ext: file.file_ext,
        module_name: moduleContext,
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
  const diagnostics = { versionsTried: versions.slice(0, 10), endpoints: {}, errors: {}, truncated: {} };
  const items = [];
  let successfulSections = 0;

  // 'content' (lectures) is a single-shot fetch — D2L's TOC endpoint returns the whole module
  // tree in one response. The other three are bookmark-paginated list endpoints, so they need
  // to follow the cursor via brightspaceJsonAllPages or courses with more than one page of
  // assignments/quizzes/announcements quietly lose everything past page one.
  const sections = [
    ['content', [v => `/d2l/api/le/${v}/${course.source_uid}/content/toc`], (data, v) => flattenToc(data, course, v, origin), false],
    ['assignments', [v => `/d2l/api/le/${v}/${course.source_uid}/dropbox/folders/`], items => assignmentItems(items, course, origin), true],
    ['quizzes', [v => `/d2l/api/le/${v}/${course.source_uid}/quizzes/`], items => quizItems(items, course, origin), true],
    ['announcements', [v => `/d2l/api/le/${v}/${course.source_uid}/news/`], items => announcementItems(items, course, origin), true]
  ];

  for (const [name, builders, transform, paginated] of sections) {
    try {
      if (paginated) {
        let result = null;
        const errors = [];
        for (const version of versions) {
          for (const build of builders) {
            const path = build(version);
            try { result = { ...(await brightspaceJsonAllPages(path, origin)), path }; break; }
            catch (error) {
              if (error.code === 'AUTH_EXPIRED') throw error;
              errors.push(`${path}: ${error.message}`);
            }
          }
          if (result) break;
        }
        if (!result) throw new Error(errors.slice(-4).join(' | ') || 'No compatible endpoint found');
        diagnostics.endpoints[name] = result.path;
        diagnostics.truncated[name] = result.truncated;
        const rows = transform(result.items);
        items.push(...rows);
        successfulSections++;
        diagnostics[`${name}Count`] = rows.length;
      } else {
        const result = await firstWorking(builders, versions, origin);
        diagnostics.endpoints[name] = result.path;
        const rows = transform(result.data, result.version);
        items.push(...rows);
        successfulSections++;
        diagnostics[`${name}Count`] = rows.length;
      }
    } catch (error) {
      if (error.code === 'AUTH_EXPIRED') throw error;
      diagnostics.errors[name] = error.message;
    }
  }

  const deduped = new Map();
  for (const item of items) deduped.set(item.source_uid, item);
  const contentOk = !diagnostics.errors.content;
  const nothingTruncated = !Object.values(diagnostics.truncated).some(Boolean);
  return {
    ok: successfulSections > 0,
    authenticated: true,
    complete: successfulSections >= 3 && contentOk && nothingTruncated,
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
  holdWorkerAwake();
  await markSyncInFlight(trigger);
  try {
    return await runSyncAllCourses(trigger);
  } finally {
    await clearSyncInFlight();
    releaseWorker();
  }
}


async function runSyncAllCourses(trigger = 'manual') {
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
      const items = Array.isArray(scan.items) ? scan.items : [];
      const trustworthy = scan.ok === true && scan.complete === true && scan.authenticated === true;
      const replaceCourses = trustworthy && items.length > 0 ? [course.code] : [];
      // Always report back to Nexora, even when the scan found nothing --
      // otherwise a total failure (every Brightspace API call failing for
      // one specific course) never reaches the website at all, and the
      // course just looks silently empty with no explanation.
      const imported = await api('/api/sync/content', {
        method: 'POST',
        body: {
          items,
          course: course.code,
          replace_courses: replaceCourses,
          scan_meta: { method: scan.method, complete: trustworthy, diagnostics: scan.diagnostics || {}, trigger }
        }
      });
      if (!scan.ok) throw new Error(scan.error || 'Brightspace did not return any content for this course.');

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
  // Stamped only on a completed pass. Without it the staleness check reads
  // undefined, treats the data as infinitely old, and syncs on every single
  // page view -- the hammering the check exists to prevent.
  await chrome.storage.local.set({ lastFullSync: Date.now() });
  // Only failure earns an OS notification. Auto-sync runs on a timer, so
  // announcing every success meant a desktop notification every half hour
  // saying nothing had changed. The popup already carries the result, which
  // is where someone looks when they want it.
  if (summary.failed === courses.length) {
    await notify('Nexora Sync could not finish',
                 'Open Brightspace and sign in again, then sync.');
  }
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


async function responseLooksLikeLogin(blob, finalUrl = '') {
  if (/\/login|\/d2l\/login|saml|authenticate/i.test(finalUrl || '')) return true;
  const preview = (await blob.slice(0, 20000).text()).toLowerCase();
  return /sign in|log in|single sign-on|samlrequest|authentication required/.test(preview);
}

function extractDownloadCandidates(html, baseUrl) {
  const candidates = [];
  const patterns = [
    /(?:href|src)=["']([^"']+(?:download|file|content\/topics)[^"']*)["']/gi,
    /["'](\/d2l\/api\/le\/[^"']+\/content\/topics\/[^"']+\/file)["']/gi,
    /["'](\/d2l\/common\/dialogues\/quickLink\/quickLink\.d2l[^"']+)["']/gi
  ];
  for (const pattern of patterns) {
    let match;
    while ((match = pattern.exec(html)) && candidates.length < 12) {
      try { candidates.push(new URL(match[1].replace(/&amp;/g, '&'), baseUrl).href); } catch (_) {}
    }
  }
  return [...new Set(candidates)];
}

async function downloadLectureBlob(request, cfg) {
  const urls = [...new Set([request.download_url, request.source_url].filter(Boolean))];
  let lastError = null;
  for (const url of urls) {
    try {
      const fetched = await brightspaceFetch(url, {
        origin: cfg.brightspaceOrigin,
        accept: 'application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*'
      });
      let blob = await fetched.response.blob();
      if (!blob.size) throw new Error('Brightspace returned an empty file.');
      if (/text\/html/i.test(fetched.contentType)) {
        if (await responseLooksLikeLogin(blob, fetched.finalUrl)) {
          const error = new Error('Brightspace connection expired. Open Brightspace and sign in again.');
          error.code = 'AUTH_EXPIRED';
          throw error;
        }
        const html = await blob.text();
        const candidates = extractDownloadCandidates(html, fetched.finalUrl);
        for (const candidate of candidates) {
          try {
            const nested = await brightspaceFetch(candidate, { origin: cfg.brightspaceOrigin, accept: '*/*' });
            const nestedBlob = await nested.response.blob();
            if (nestedBlob.size && !/text\/html/i.test(nested.contentType)) {
              return { ...nested, blob: nestedBlob };
            }
          } catch (_) {}
        }
        throw new Error('Brightspace returned a page instead of the file. Run “Scan every course now” using Nexora Sync v3.0 so the direct file link is refreshed.');
      }
      return { ...fetched, blob };
    } catch (error) {
      if (error.code === 'AUTH_EXPIRED') throw error;
      lastError = error;
    }
  }
  throw lastError || new Error('No downloadable Brightspace file URL was available for this lecture.');
}

async function fetchAndProcessLecture(request) {
  const cfg = await getConfig();
  if (!request?.source_uid || !(request?.source_url || request?.download_url)) {
    throw new Error('Nexora did not provide a valid lecture file reference. Run a fresh Smart Sync.');
  }
  const { response, contentType, finalUrl, blob } = await downloadLectureBlob(request, cfg);
  if (blob.size > 30 * 1024 * 1024) throw new Error('This lecture is larger than Nexora’s 30 MB processing limit.');

  const fallbackName = clean(request.file_name || request.title || 'lecture-file');
  let fileName = fallbackName;
  try {
    const disposition = response.headers.get('content-disposition') || '';
    const utf = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const basic = disposition.match(/filename=["']?([^"';]+)/i);
    if (utf) fileName = decodeURIComponent(utf[1].trim());
    else if (basic) fileName = basic[1].trim();
    else {
      const pathName = new URL(finalUrl).pathname.split('/').pop();
      if (pathName && /\.[a-z0-9]{2,6}$/i.test(pathName)) fileName = decodeURIComponent(pathName);
    }
  } catch (_) {}
  if (!/\.[a-z0-9]{2,6}$/i.test(fileName)) {
    const ext = /pdf/i.test(contentType) ? '.pdf'
      : /presentation|powerpoint/i.test(contentType) ? '.pptx'
      : /wordprocessingml|msword/i.test(contentType) ? '.docx'
      : /text\//i.test(contentType) ? '.txt' : '.bin';
    fileName += ext;
  }

  const form = new FormData();
  form.append('source_uid', request.source_uid);
  form.append('source_url', request.source_url || request.download_url || '');
  form.append('file', blob, fileName);
  const uploaded = await fetch(`${cfg.apiBase}/api/sync/process-content`, {
    method: 'POST', headers: { Authorization: `Bearer ${cfg.token}` }, body: form
  });
  const raw = await uploaded.text();
  let data = {};
  try { data = JSON.parse(raw); } catch (_) { data = { error: raw.slice(0, 500) }; }
  if (!uploaded.ok) {
    const stage = data.stage ? ` (${data.stage})` : '';
    throw new Error((data.error || `Nexora returned ${uploaded.status}`) + stage);
  }
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
  if (alarm.name === KEEPALIVE_ALARM) { await resumeSyncIfDropped(); return; }
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
      // The popup cannot reach a Brightspace tab itself, so the terms a student
      // can choose between come back through here.
      if (message?.type === 'NEXORA_LIST_TERMS') {
        sendResponse(await sendToBrightspace({ type: 'NEXORA_LIST_TERMS' }));
        return;
      }
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
        if (!cfg.brightspaceOrigin) {
          throw new Error('Nexora does not know your university yet. Open your Brightspace in a tab and press "Allow this Brightspace" first.');
        }
        const tab = await chrome.tabs.create({ url: `${cfg.brightspaceOrigin}/d2l/home`, active: true });
        sendResponse({ ok: true, tabId: tab.id });
        return;
      }
      // The popup asks for the permission (it needs a user gesture) and then
      // hands the origin here to be remembered as this student's university.
      if (message?.type === 'NEXORA_SET_ORIGIN') {
        const origin = normalizeOrigin(message.origin || '');
        if (!origin) throw new Error('That tab does not look like a Brightspace page.');
        await chrome.storage.sync.set({ brightspaceOrigin: origin });
        sendResponse({ ok: true, origin, granted: await hasOriginPermission(origin) });
        return;
      }
      if (message?.type === 'NEXORA_FETCH_AND_PROCESS_LECTURE') {
        sendResponse({ ok: true, result: await fetchAndProcessLecture(message.request || {}) });
        return;
      }
      // The website hands the key over directly, so nobody copies a token.
      // The page can only reach here through the bridge content script, which
      // runs on the Nexora origin and nowhere else.
      if (message?.type === 'NEXORA_PAIR') {
        const token = String(message.token || '').trim();
        if (!token) throw new Error('Nexora did not return a key.');
        const apiBase = String(message.apiBase || DEFAULT_API_BASE).replace(/\/$/, '');
        await chrome.storage.sync.set({ token, apiBase });
        await configureAlarm();
        sendResponse({ ok: true, apiBase });
        return;
      }

      // "Detect courses" then "scan every course" was never two decisions a
      // student needed to make; it is one button now.
      if (message?.type === 'NEXORA_CANVAS_SESSION_SYNC') {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        sendResponse({ ok: true, result: await syncCanvasThroughSession(tab) });
        return;
      }

      if (message?.type === 'NEXORA_SYNC_EVERYTHING') {
        await setStatus({ running: true, phase: 'finding-courses', error: '' });
        // One button, either LMS. Brightspace needs a /d2l/ tab; if there is not
        // one, the active tab may be Canvas, which a student whose university
        // forbids access tokens can only reach this way.
        if (!(await findBrightspaceTab())) {
          const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
          const result = await syncCanvasThroughSession(tab);
          sendResponse({ ok: true, canvas: true, discovery: { courseDiscovery: { stored: result.courses } }, summary: {} });
          return;
        }
        const discovery = await discoverCourses();
        const summary = await syncAllCourses('manual');
        sendResponse({ ok: true, discovery, summary });
        return;
      }

      // The bridge reports that the student is looking at Nexora. Freshness
      // matters at that moment and almost never in between, which is why this
      // exists instead of a faster alarm: a minute-by-minute poll would walk
      // every course's content tree thousands of times an hour against the
      // university's servers, and Chrome throttles short alarms regardless.
      if (message?.type === 'NEXORA_VIEWING') {
        const cfg = await getConfig();
        const local = await chrome.storage.local.get(['lastFullSync']);
        const age = Date.now() - Number(local.lastFullSync || 0);
        if (cfg.autoSyncEnabled && age > cfg.intervalMinutes * 60 * 1000) {
          syncAllCourses('viewing').catch(() => {});   // never block the page
          sendResponse({ ok: true, started: true });
          return;
        }
        sendResponse({ ok: true, started: false });
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
        sendResponse({
          ok: true,
          configured: !!(config.apiBase && config.token),
          originGranted: await hasOriginPermission(config.brightspaceOrigin),
          config,
          ...local
        });
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
