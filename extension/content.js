(() => {
  if (globalThis.__NEXORA_BRIGHTSPACE_CONTENT_V11__) return;
  globalThis.__NEXORA_BRIGHTSPACE_CONTENT_V11__ = true;
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const absolute = href => { try { return new URL(href, location.href).href; } catch (_) { return ''; } };
  const orgUnitIdFrom = value => {
    const text = String(value || '');
    for (const pattern of [/\/d2l\/home\/(\d+)/i, /[?&]ou=(\d+)/i, /\/lessons\/(\d+)/i, /\/content\/(\d+)/i]) {
      const match = text.match(pattern);
      if (match) return match[1];
    }
    return '';
  };

  function allRoots() {
    const result = [document];
    const queue = [document.documentElement];
    const seen = new Set(queue);
    while (queue.length) {
      const node = queue.shift();
      if (!node?.querySelectorAll) continue;
      for (const element of node.querySelectorAll('*')) {
        if (element.shadowRoot && !seen.has(element.shadowRoot)) {
          seen.add(element.shadowRoot);
          result.push(element.shadowRoot);
          queue.push(element.shadowRoot);
        }
      }
    }
    return result;
  }

  function queryAll(selector) {
    const output = [];
    const seen = new Set();
    for (const root of allRoots()) {
      try {
        for (const element of root.querySelectorAll(selector)) {
          if (!seen.has(element)) { seen.add(element); output.push(element); }
        }
      } catch (_) {}
    }
    return output;
  }

  function shadowClosest(element, selector) {
    let node = element;
    while (node) {
      if (node.matches?.(selector)) return node;
      node = node.parentElement || node.getRootNode?.()?.host || null;
    }
    return null;
  }

  function parseCourse(raw, id) {
    const original = clean(raw).replace(/\b(Open|Closed)\b/ig, '').trim();
    const term = (original.match(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/i) || [])[0] || '';
    const codeMatch = original.match(/\b([A-Z]{3,6})\s*[-:]?\s*(\d{4}[A-Z]?)\b/i);
    const code = codeMatch ? `${codeMatch[1]}${codeMatch[2]}`.toUpperCase() : `COURSE${id}`;
    let name = original
      .replace(/^\s*(Merge|Crosslist)\s+/i, '')
      .replace(/\[[\d:\s,]+\]/g, ' ')
      .replace(/\bXLS[A-Z0-9_-]+\b/ig, ' ')
      .replace(/\bSP[_-]?\d+\b/ig, ' ')
      .replace(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/ig, ' ')
      .replace(/\((LEC|LAB|TUT|SEM|DIS|PRA)(?:\s+[^)]*)?\)/ig, ' ')
      .replace(new RegExp(`^${code}\\s*[-:]?\\s*`, 'i'), '')
      .replace(/^\s*[,._-]+|[,._-]+\s*$/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (!name || name.length < 3) name = code;
    return { code, name, term, original };
  }

  function imageFrom(container) {
    for (const image of container?.querySelectorAll?.('img') || []) {
      const src = absolute(image.currentSrc || image.src || image.getAttribute('data-src'));
      if (src && !/avatar|icon|logo/i.test(src)) return src;
    }
    for (const node of container?.querySelectorAll ? [container, ...container.querySelectorAll('*')] : []) {
      try {
        const match = getComputedStyle(node).backgroundImage?.match(/url\(["']?(.*?)["']?\)/);
        if (match) return absolute(match[1]);
      } catch (_) {}
    }
    return '';
  }

  function discoverCoursesFromDom() {
    const map = new Map();
    for (const anchor of queryAll('a[href*="/d2l/home/"],[href*="/d2l/home/"]')) {
      const url = absolute(anchor.getAttribute?.('href') || anchor.href || '');
      const id = orgUnitIdFrom(url);
      if (!id) continue;
      const card = shadowClosest(anchor, 'd2l-enrollment-card,d2l-card,article,li,[role="listitem"],[class*="enrollment"],[class*="course"],[class*="tile"],[class*="card"]') || anchor;
      const raw = clean(`${card.getAttribute?.('aria-label') || ''} ${card.textContent || ''} ${card.shadowRoot?.textContent || ''}`);
      if (!raw) continue;
      const parsed = parseCourse(raw, id);
      const old = map.get(id);
      map.set(id, {
        source_uid: id,
        course_code: parsed.code,
        course_name: parsed.name,
        raw_course_name: parsed.original,
        term: parsed.term,
        instructor: '',
        image_url: imageFrom(card) || old?.image_url || '',
        brightspace_url: `${location.origin}/d2l/home/${id}`
      });
    }
    return [...map.values()];
  }

  async function fetchJson(path) {
    const response = await fetch(path, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    });
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    if (!contentType.includes('json')) throw new Error(`Expected JSON but received ${contentType || 'unknown content type'}`);
    return response.json();
  }

  async function detectApiVersions() {
    const candidates = [];
    try {
      const versions = await fetchJson('/d2l/api/versions/');
      const rows = Array.isArray(versions) ? versions : versions?.Versions || versions?.versions || [];
      for (const row of rows) {
        const product = clean(row.ProductCode || row.productCode || row.Product || '').toLowerCase();
        const version = clean(row.LatestVersion || row.Version || row.version || '');
        if (version && (!product || /le|lp/.test(product))) candidates.push(version);
      }
    } catch (_) {}
    // Conservative fallbacks cover older and current Brightspace tenants.
    for (const version of ['1.82', '1.75', '1.74', '1.67', '1.60', '1.50', '1.48', '1.44']) {
      if (!candidates.includes(version)) candidates.push(version);
    }
    return candidates;
  }

  async function firstWorking(builders, versions) {
    const errors = [];
    for (const version of versions) {
      for (const build of builders) {
        const path = build(version);
        try { return { data: await fetchJson(path), version, path }; }
        catch (error) { errors.push(`${path}: ${error.message}`); }
      }
    }
    throw new Error(errors.slice(-4).join(' | ') || 'No compatible endpoint found');
  }

  function htmlToText(value) {
    if (!value) return '';
    const html = typeof value === 'string' ? value : value.Html || value.Text || value.html || value.text || '';
    const template = document.createElement('template');
    template.innerHTML = String(html);
    return clean(template.content.textContent || '').slice(0, 120000);
  }

  function dateValue(value) {
    if (!value) return '';
    if (typeof value === 'string') return value;
    return clean(value.Date || value.date || value.StartDate || value.startDate || '');
  }

  function fileInfoFromTopic(topic, orgUnitId, version) {
    const url = topic.Url || topic.URL || topic.ContentUrl || topic.ContentURL || topic.Location || '';
    const absoluteUrl = absolute(url);
    const title = clean(topic.Title || topic.Name || 'Untitled content');
    const extension = ((title.match(/\.([a-z0-9]{1,8})\b/i) || absoluteUrl.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
    const topicId = topic.Id || topic.TopicId || topic.Identifier || '';
    const downloadUrl = topicId ? `${location.origin}/d2l/api/le/${version}/${orgUnitId}/content/topics/${topicId}/file` : '';
    return { source_url: absoluteUrl || downloadUrl, download_url: downloadUrl, file_name: title, file_ext: extension };
  }

  function classifyContent(title, url, activityType) {
    const text = `${title} ${url} ${activityType || ''}`.toLowerCase();
    const ext = ((title.match(/\.([a-z0-9]{1,8})\b/i) || url.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
    if (/practice|sample question|review question|worksheet|problem set/.test(text)) return 'practice';
    if (/assignment|dropbox/.test(text)) return 'assignment';
    if (/quiz|quizzing/.test(text)) return 'quiz';
    if (/announcement|news/.test(text)) return 'announcement';
    if (['py', 'c', 'cpp', 'h', 'java', 'js', 'ts', 'ipynb', 'zip', 'rar', '7z', 'csv', 'xlsx', 'xls'].includes(ext)) return 'resource';
    if (['pdf', 'ppt', 'pptx'].includes(ext) && /(?:lecture|lec|slides?|week|module|chapter|lesson|unit)\s*[-_#:]?\s*\d+|\b(?:l|lec|wk|ch)\s*[-_#]?\s*\d+\b/i.test(text)) return 'lecture';
    if (/(?:lecture|lec|slides?|lesson)\s*[-_#:]?\s*\d+|\b(?:l|lec)\s*[-_#]?\s*\d+\b/i.test(text)) return 'lecture';
    if (['pdf', 'ppt', 'pptx', 'doc', 'docx'].includes(ext)) return 'resource';
    return 'page';
  }

  function itemUid(courseId, type, id, fallback) {
    return clean(`${courseId}|${type}|${id || fallback}`).slice(0, 255);
  }

  function flattenToc(toc, course, version) {
    const items = [];
    const orgUnitId = course.source_uid;
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
        const file = fileInfoFromTopic(node, orgUnitId, version);
        const activity = node.ActivityType || node.TypeIdentifier || node.Type || '';
        const type = classifyContent(title, file.source_url, activity);
        items.push({
          course: course.course_code,
          source_uid: itemUid(orgUnitId, 'content', id, file.source_url || title),
          title,
          content_type: type,
          source_url: file.source_url,
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
    const roots = Array.isArray(toc) ? toc : toc.Modules || toc.Items || toc.Content || toc.Objects || [];
    walk(roots);
    return items;
  }

  function listFromPaged(data) {
    if (Array.isArray(data)) return data;
    return data?.Objects || data?.Items || data?.Results || data?.objects || data?.items || [];
  }

  function assignmentItems(data, course) {
    return listFromPaged(data).map(row => {
      const id = row.Id || row.FolderId || row.Identifier || row.Name;
      const title = clean(row.Name || row.Title || 'Assignment');
      return {
        course: course.course_code,
        source_uid: itemUid(course.source_uid, 'assignment', id, title),
        title,
        content_type: 'assignment',
        source_url: `${location.origin}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=${encodeURIComponent(id)}&grpid=0&isprv=0&bp=0&ou=${course.source_uid}`,
        module_name: clean(row.Category?.Name || row.CategoryName || 'Assignments'),
        content_text: htmlToText(row.Instructions || row.Description || ''),
        published_at: dateValue(row.DueDate || row.EndDate || row.StartDate),
        due_at: dateValue(row.DueDate),
        smart_sync: true
      };
    }).filter(x => x.title);
  }

  function quizItems(data, course) {
    return listFromPaged(data).map(row => {
      const id = row.QuizId || row.Id || row.Identifier || row.Name;
      const title = clean(row.Name || row.Title || 'Quiz');
      return {
        course: course.course_code,
        source_uid: itemUid(course.source_uid, 'quiz', id, title),
        title,
        content_type: 'quiz',
        source_url: `${location.origin}/d2l/lms/quizzing/user/quiz_summary.d2l?qi=${encodeURIComponent(id)}&ou=${course.source_uid}`,
        module_name: clean(row.Category?.Name || row.CategoryName || 'Quizzes'),
        content_text: htmlToText(row.Description || row.Header || ''),
        published_at: dateValue(row.StartDate || row.DueDate || row.EndDate),
        due_at: dateValue(row.DueDate || row.EndDate),
        smart_sync: true
      };
    }).filter(x => x.title);
  }

  function announcementItems(data, course) {
    return listFromPaged(data).map(row => {
      const id = row.Id || row.NewsItemId || row.Identifier || row.Title;
      const title = clean(row.Title || row.Name || 'Announcement');
      return {
        course: course.course_code,
        source_uid: itemUid(course.source_uid, 'announcement', id, title),
        title,
        content_type: 'announcement',
        source_url: `${location.origin}/d2l/lms/news/main.d2l?ou=${course.source_uid}`,
        module_name: 'Announcements',
        content_text: htmlToText(row.Body || row.Content || row.Description || ''),
        published_at: dateValue(row.StartDate || row.CreatedDate || row.LastModifiedDate),
        smart_sync: true
      };
    }).filter(x => x.title);
  }

  async function apiScanCourse(course) {
    const versions = await detectApiVersions();
    const diagnostics = { versionsTried: versions.slice(0, 8), endpoints: {}, errors: {} };
    const items = [];
    let successfulSections = 0;

    try {
      const result = await firstWorking([
        v => `/d2l/api/le/${v}/${course.source_uid}/content/toc`
      ], versions);
      diagnostics.endpoints.content = result.path;
      const content = flattenToc(result.data, course, result.version);
      items.push(...content);
      successfulSections++;
      diagnostics.contentCount = content.length;
    } catch (error) { diagnostics.errors.content = error.message; }

    try {
      const result = await firstWorking([
        v => `/d2l/api/le/${v}/${course.source_uid}/dropbox/folders/`
      ], versions);
      diagnostics.endpoints.assignments = result.path;
      const assignments = assignmentItems(result.data, course);
      items.push(...assignments);
      successfulSections++;
      diagnostics.assignmentCount = assignments.length;
    } catch (error) { diagnostics.errors.assignments = error.message; }

    try {
      const result = await firstWorking([
        v => `/d2l/api/le/${v}/${course.source_uid}/quizzes/`
      ], versions);
      diagnostics.endpoints.quizzes = result.path;
      const quizzes = quizItems(result.data, course);
      items.push(...quizzes);
      successfulSections++;
      diagnostics.quizCount = quizzes.length;
    } catch (error) { diagnostics.errors.quizzes = error.message; }

    try {
      const result = await firstWorking([
        v => `/d2l/api/le/${v}/${course.source_uid}/news/`
      ], versions);
      diagnostics.endpoints.announcements = result.path;
      const announcements = announcementItems(result.data, course);
      items.push(...announcements);
      successfulSections++;
      diagnostics.announcementCount = announcements.length;
    } catch (error) { diagnostics.errors.announcements = error.message; }

    const deduped = new Map();
    for (const item of items) deduped.set(item.source_uid, item);
    return {
      ok: successfulSections > 0,
      authenticated: true,
      complete: successfulSections >= 3,
      method: 'brightspace-session-api',
      diagnostics: { ...diagnostics, successfulSections },
      items: [...deduped.values()],
      error: successfulSections ? '' : 'Brightspace API routes were unavailable for this student session.'
    };
  }

  async function expandDynamicPage() {
    let clicks = 0;
    let stableRounds = 0;
    let previousCount = -1;
    for (let round = 0; round < 30; round++) {
      const button = queryAll('button,a,[role="button"]').find(element => {
        const text = clean(element.textContent || element.getAttribute?.('aria-label') || '').toLowerCase();
        const visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects?.().length);
        return visible && !element.disabled && /load more|show more|view more|more results/.test(text);
      });
      if (button) { button.click(); clicks++; await sleep(900); }
      else { window.scrollTo(0, document.body.scrollHeight); await sleep(650); }
      const count = queryAll('a[href],article,li,[role="listitem"]').length;
      stableRounds = count === previousCount ? stableRounds + 1 : 0;
      previousCount = count;
      if (!button && stableRounds >= 3) break;
    }
    return clicks;
  }

  async function domFallbackCourse(course) {
    const currentOu = orgUnitIdFrom(location.href);
    if (currentOu !== String(course.source_uid)) {
      return { ok: false, authenticated: true, complete: false, method: 'dom-fallback', items: [], error: 'DOM fallback requires the course page to be open.' };
    }
    const expanded = await expandDynamicPage();
    const map = new Map();
    for (const element of queryAll('a[href],[role="link"][href],d2l-link[href]')) {
      const url = absolute(element.getAttribute?.('href') || '');
      const title = clean(element.getAttribute?.('aria-label') || element.getAttribute?.('title') || element.textContent || '');
      if (!url || !title || title.length < 2 || title.length > 500) continue;
      if (/^(home|content|grades|calendar|help|logout|settings|search)$/i.test(title)) continue;
      const type = classifyContent(title, url, '');
      if (type === 'page' && !/\/content\/|\/lessons\//.test(url)) continue;
      const id = itemUid(course.source_uid, type, '', `${title}|${url}`);
      map.set(id, {
        course: course.course_code,
        source_uid: id,
        title,
        content_type: type,
        source_url: url,
        module_name: '',
        content_text: '',
        published_at: '',
        smart_sync: true
      });
    }
    return {
      ok: map.size > 0,
      authenticated: true,
      complete: false,
      method: 'dom-fallback',
      diagnostics: { expanded, found: map.size },
      items: [...map.values()],
      error: map.size ? '' : 'No usable items found in the open course page.'
    };
  }

  async function smartScanCourse(course) {
    const apiResult = await apiScanCourse(course);
    if (apiResult.ok && apiResult.items.length) return apiResult;
    const fallback = await domFallbackCourse(course);
    if (fallback.ok) {
      fallback.diagnostics = { ...(fallback.diagnostics || {}), apiErrors: apiResult.diagnostics?.errors || {} };
      return fallback;
    }
    return apiResult;
  }

  async function sessionCheck() {
    try {
      const response = await fetch('/d2l/home', { credentials: 'include', redirect: 'follow', cache: 'no-store' });
      const text = await response.text();
      const signedOut = /login|sign in|authenticate/i.test(text.slice(0, 5000)) && !/logout|my courses|course home/i.test(text.slice(0, 5000));
      return signedOut ? { ok: false, error: 'Brightspace appears signed out.' } : { ok: true };
    } catch (error) {
      return { ok: false, error: `Could not verify Brightspace session: ${error.message}` };
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'NEXORA_CONTENT_PING') { sendResponse({ ok: true, version: '1.1.0' }); return false; }
    if (message?.type === 'NEXORA_SESSION_CHECK') { sessionCheck().then(sendResponse); return true; }
    if (message?.type === 'NEXORA_DISCOVER_COURSES') {
      (async () => {
        for (let i = 0; i < 8; i++) {
          const courses = discoverCoursesFromDom();
          if (courses.length) return { ok: true, courses, method: 'course-card-dom' };
          await sleep(600);
        }
        return { ok: false, courses: [], error: 'No course cards were found. Open the Brightspace My Courses page.' };
      })().then(sendResponse);
      return true;
    }
    if (message?.type === 'NEXORA_SMART_SCAN_COURSE') {
      smartScanCourse(message.course || {}).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message, items: [] }));
      return true;
    }
  });
})();
