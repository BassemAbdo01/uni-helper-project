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
    // "Winter 2026" fits "three to six letters then four digits" as neatly as
    // "ECOR1056" does, so the first match is not necessarily a course code.
    const NOT_A_SUBJECT = /^(WINTER|SPRING|SUMMER|FALL|AUTUMN|TERM|SEM|YEAR|SECTION)$/i;
    let codeMatch = null;
    for (const m of original.matchAll(/\b([A-Z]{3,6})\s*[-:]?\s*(\d{4}[A-Z]?)\b/gi)) {
      if (!NOT_A_SUBJECT.test(m[1])) { codeMatch = m; break; }
    }
    const code = codeMatch ? `${codeMatch[1]}${codeMatch[2]}`.toUpperCase() : `COURSE${id}`;
    let name = original
      .replace(/^\s*(Merge|Crosslist)\s+/i, '')
      .replace(/\[[\d:\s,]+\]/g, ' ')
      .replace(/\bXLS[A-Z0-9_-]+\b/ig, ' ')
      .replace(/\bSP[_-]?\d+\b/ig, ' ')
      .replace(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/ig, ' ')
      .replace(/\((LEC|LAB|TUT|SEM|DIS|PRA)(?:\s+[^)]*)?\)/ig, ' ')
      .replace(new RegExp(`^${code}\\s*[-:]?\\s*`, 'i'), '')
      // A crosslist names both courses; the second one is not part of the title.
      .replace(/^[A-Z]{3,6}\s*[-:]?\s*\d{4}[A-Z]?\s*[-:]?\s*/i, '')
      // A merge lists its sections -- "D:E", "A:B:C" -- ahead of the real name.
      .replace(/^[A-Z](?:\s*:\s*[A-Z])+\s+/, '')
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

  // Brightspace types its org units; 3 is a Course Offering. Without this filter
  // the list also carries departments, semesters and the org itself.
  const COURSE_OFFERING_TYPE = 3;

  function courseFromEnrollment(row) {
    const unit = row?.OrgUnit || row?.orgUnit || {};
    const typeId = Number(unit?.Type?.Id ?? unit?.type?.id ?? NaN);
    if (Number.isFinite(typeId) && typeId !== COURSE_OFFERING_TYPE) return null;

    const id = String(unit.Id ?? unit.id ?? '').trim();
    if (!id) return null;

    // Name is what a student sees; Code is the registrar's string. Either can
    // carry the course code, so both are offered to the same parser the DOM
    // path uses, and whichever yields a real code wins.
    const name = clean(unit.Name || unit.name || '');
    const code = clean(unit.Code || unit.code || '');
    if (!name && !code) return null;

    let parsed = parseCourse(name || code, id);
    if (parsed.code.startsWith('COURSE') && code) {
      const fromCode = parseCourse(`${code} ${name}`, id);
      if (!fromCode.code.startsWith('COURSE')) parsed = fromCode;
    }

    return {
      source_uid: id,
      course_code: parsed.code,
      course_name: parsed.name,
      raw_course_name: parsed.original,
      term: parsed.term,
      instructor: '',
      image_url: '',
      brightspace_url: `${location.origin}/d2l/home/${id}`
    };
  }

  // D2L labels its term tabs "2026.10 Winter", "2026.30 Fall". The selected one
  // is the term the student is looking at, and the only one they asked for.
  const SEASON_BY_CODE = { '10': 'Winter', '20': 'Summer', '30': 'Fall' };

  function selectedTermLabel() {
    const candidates = queryAll('[aria-selected="true"],[selected],.d2l-tab-selected,[class*="selected"]');
    for (const node of candidates) {
      const text = clean(node.textContent || node.getAttribute?.('aria-label') || '');
      if (!text || text.length > 40) continue;

      // "2026.10 Winter" and "Winter 2026" are the same tab written two ways.
      const coded = text.match(/\b(20\d{2})\s*[.\-]\s*(10|20|30)\b/);
      if (coded) {
        const season = SEASON_BY_CODE[coded[2]];
        if (season) return `${season} ${coded[1]}`;
      }
      const plain = text.match(/\b(Winter|Spring|Summer|Fall|Autumn)\s+(20\d{2})\b/i);
      if (plain) {
        const season = plain[1][0].toUpperCase() + plain[1].slice(1).toLowerCase();
        return `${season === 'Autumn' ? 'Fall' : season} ${plain[2]}`;
      }
    }
    return '';
  }

  async function discoverCoursesFromApi(includeFinished) {
    const versions = await detectApiVersions();

    // The term a student is in is what they want on their dashboard. A finished
    // course drags its whole content tree along, fills the page with work that
    // is over and the calendar with dates that have passed -- so past terms are
    // asked for only when the student says so.
    const queries = includeFinished
      ? [`?orgUnitTypeId=${COURSE_OFFERING_TYPE}`,
         `?orgUnitTypeId=${COURSE_OFFERING_TYPE}&isActive=false`,
         `?orgUnitTypeId=${COURSE_OFFERING_TYPE}&isActive=true`,
         '']
      : [`?orgUnitTypeId=${COURSE_OFFERING_TYPE}&isActive=true`,
         `?orgUnitTypeId=${COURSE_OFFERING_TYPE}`];

    const map = new Map();
    let answered = false;

    for (const query of queries) {
      try {
        const paged = await firstWorkingPaged(
          [v => `/d2l/api/lp/${v}/enrollments/myenrollments/${query}`], versions);
        answered = true;
        for (const row of paged.items || []) {
          const course = courseFromEnrollment(row);
          if (course && !map.has(course.source_uid)) map.set(course.source_uid, course);
        }
      } catch (_) {
        // a tenant rejecting one variant says nothing about the others
      }
    }

    if (!answered) throw new Error('No enrolments endpoint answered.');
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

  function withBookmark(path, bookmark) {
    const url = new URL(path, location.origin);
    url.searchParams.set('bookmark', bookmark);
    return url.pathname + url.search;
  }

  // D2L's dropbox/quiz/news list endpoints page results behind a `PagingInfo.Bookmark` +
  // `HasMoreItems` cursor. A single fetchJson() call only ever returns page one, which is
  // exactly why large courses were quietly losing items past the first page. This follows
  // the cursor until Brightspace reports there's nothing left, or we hit a sane hard cap.
  async function fetchAllPages(path, { maxPages = 40 } = {}) {
    const merged = [];
    let nextPath = path;
    let pages = 0;
    let lastPageInfo = null;
    while (nextPath && pages < maxPages) {
      const data = await fetchJson(nextPath);
      pages++;
      const pageItems = listFromPaged(data);
      merged.push(...pageItems);
      const pagingInfo = data?.PagingInfo || data?.pagingInfo || null;
      lastPageInfo = pagingInfo;
      const hasMore = !!(pagingInfo && (pagingInfo.HasMoreItems || pagingInfo.hasMoreItems));
      const bookmark = pagingInfo && (pagingInfo.Bookmark || pagingInfo.bookmark);
      nextPath = hasMore && bookmark ? withBookmark(path, bookmark) : null;
      if (hasMore && bookmark && pageItems.length === 0) break;
    }
    return { items: merged, pages, truncated: pages >= maxPages, lastPageInfo };
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

  async function firstWorkingPaged(builders, versions) {
    const errors = [];
    for (const version of versions) {
      for (const build of builders) {
        const path = build(version);
        try {
          const paged = await fetchAllPages(path);
          return { ...paged, version, path };
        } catch (error) { errors.push(`${path}: ${error.message}`); }
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

  // moduleContext is the module/folder breadcrumb the item lives under (e.g. "Week 5 › Lecture 12").
  // Brightspace courses very often number the *folder*, not every file inside it — a recording or
  // slide deck named just "Recording" or "Slides" is still clearly lecture material if it's filed
  // under "Lecture 12". Checking only the item's own title (as before) missed all of these.
  function classifyContent(title, url, activityType, moduleContext) {
    const ownText = `${title} ${url} ${activityType || ''}`.toLowerCase();
    const fullText = `${ownText} ${moduleContext || ''}`.toLowerCase();
    const ext = ((title.match(/\.([a-z0-9]{1,8})\b/i) || url.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();
    const numberedLecturePattern = /(?:lecture|lec|slides?|week|module|chapter|lesson|unit)\s*[-_#:]?\s*\d+|\b(?:l|lec|wk|ch)\s*[-_#]?\s*\d+\b/i;

    if (/course\s*(?:outline|syllabus|information|guide)|syllabus|grading\s*(?:scheme|breakdown|policy)|evaluation\s*(?:scheme|breakdown)|marking\s*scheme|assessment\s*(?:scheme|breakdown)|course\s*policies/.test(ownText)) return 'outline';
    if (/practice|sample question|review question|worksheet|problem set/.test(ownText)) return 'practice';

    // Real, gradeable assignments and quizzes already come from their own dedicated Brightspace
    // endpoints (dropbox/folders, quizzes/). A content-page merely *mentioning* "assignment" or
    // "quiz" (instructions, rubric, answer key, prep guide) shouldn't duplicate into those tabs —
    // route it to Resources instead so Assignments/Quizzes only show the real, tracked items.
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
        const moduleContext = parentPath.join(' › ');
        const type = classifyContent(title, file.source_url, activity, moduleContext);
        items.push({
          course: course.course_code,
          source_uid: itemUid(orgUnitId, 'content', id, file.source_url || title),
          title,
          content_type: type,
          source_url: file.source_url,
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
    const diagnostics = { versionsTried: versions.slice(0, 8), endpoints: {}, errors: {}, truncated: {} };
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
      const result = await firstWorkingPaged([
        v => `/d2l/api/le/${v}/${course.source_uid}/dropbox/folders/`
      ], versions);
      diagnostics.endpoints.assignments = result.path;
      diagnostics.truncated.assignments = result.truncated;
      const assignments = assignmentItems(result.items, course);
      items.push(...assignments);
      successfulSections++;
      diagnostics.assignmentCount = assignments.length;
    } catch (error) { diagnostics.errors.assignments = error.message; }

    try {
      const result = await firstWorkingPaged([
        v => `/d2l/api/le/${v}/${course.source_uid}/quizzes/`
      ], versions);
      diagnostics.endpoints.quizzes = result.path;
      diagnostics.truncated.quizzes = result.truncated;
      const quizzes = quizItems(result.items, course);
      items.push(...quizzes);
      successfulSections++;
      diagnostics.quizCount = quizzes.length;
    } catch (error) { diagnostics.errors.quizzes = error.message; }

    try {
      const result = await firstWorkingPaged([
        v => `/d2l/api/le/${v}/${course.source_uid}/news/`
      ], versions);
      diagnostics.endpoints.announcements = result.path;
      diagnostics.truncated.announcements = result.truncated;
      const announcements = announcementItems(result.items, course);
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
    let cutOffWithButtonRemaining = false;
    const maxRounds = 60;
    for (let round = 0; round < maxRounds; round++) {
      const button = queryAll('button,a,[role="button"]').find(element => {
        const text = clean(element.textContent || element.getAttribute?.('aria-label') || '').toLowerCase();
        const visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects?.().length);
        return visible && !element.disabled && /load more|show more|view more|more results|next page/.test(text);
      });
      if (button) {
        button.click();
        clicks++;
        // D2L's own lazy-loaded lists can take a beat longer than a plain scroll reveal.
        await sleep(1200);
      } else {
        window.scrollTo(0, document.body.scrollHeight);
        await sleep(900);
      }
      const count = queryAll('a[href],article,li,[role="listitem"]').length;
      stableRounds = count === previousCount ? stableRounds + 1 : 0;
      previousCount = count;
      // Require more consecutive stable rounds before declaring done, since a slow network
      // response can look "stable" for a round or two right before new items land.
      if (!button && stableRounds >= 4) break;
      if (round === maxRounds - 1 && button) cutOffWithButtonRemaining = true;
    }
    return { clicks, cutOffWithButtonRemaining };
  }

  async function domFallbackCourse(course) {
    const currentOu = orgUnitIdFrom(location.href);
    if (currentOu !== String(course.source_uid)) {
      return { ok: false, authenticated: true, complete: false, method: 'dom-fallback', items: [], error: 'DOM fallback requires the course page to be open.' };
    }
    const { clicks: expanded, cutOffWithButtonRemaining } = await expandDynamicPage();
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
      complete: !cutOffWithButtonRemaining,
      method: 'dom-fallback',
      diagnostics: { clicks: expanded, found: map.size, possiblyTruncated: cutOffWithButtonRemaining },
      items: [...map.values()],
      error: map.size ? '' : 'No usable items found in the open course page.'
    };
  }

  async function smartScanCourse(course) {
    const apiResult = await apiScanCourse(course);
    const contentFailed = !!apiResult.diagnostics?.errors?.content || !apiResult.diagnostics?.contentCount;
    const anythingTruncated = Object.values(apiResult.diagnostics?.truncated || {}).some(Boolean);
    // Previously: any successful API section short-circuited straight past DOM fallback,
    // even when the content/toc call (i.e. lectures) itself had failed. That's exactly how
    // a course could come back with assignments/quizzes intact but zero lectures, silently.
    if (apiResult.ok && apiResult.items.length && !contentFailed && !anythingTruncated) return apiResult;

    const fallback = await domFallbackCourse(course);
    if (!fallback.ok) {
      apiResult.diagnostics = { ...(apiResult.diagnostics || {}), domFallbackError: fallback.error };
      return apiResult;
    }

    // Merge rather than replace: API results for assignments/quizzes/announcements are more
    // structured than DOM-scraped ones, so keep them and only use the DOM fallback to fill in
    // whatever the API missed (lectures/content most commonly).
    const merged = new Map();
    for (const item of apiResult.items || []) merged.set(item.source_uid, item);
    for (const item of fallback.items || []) if (!merged.has(item.source_uid)) merged.set(item.source_uid, item);

    return {
      ok: true,
      authenticated: true,
      complete: apiResult.complete || fallback.complete,
      method: apiResult.ok ? 'hybrid-api-dom' : fallback.method,
      diagnostics: {
        ...(apiResult.diagnostics || {}),
        domFallback: { clicks: fallback.diagnostics?.clicks, itemsAdded: [...merged.values()].length - (apiResult.items?.length || 0) },
        apiErrors: apiResult.diagnostics?.errors || {},
      },
      items: [...merged.values()],
      error: '',
    };
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

  // ---------------------------------------------------------------- Canvas --
  // Same idea as the Brightspace reader: use the session the student already
  // has. Canvas answers /api/v1 for same-origin requests carrying the session
  // cookie, which is what makes this work where a token is forbidden.

  async function canvasJson(path) {
    const url = path.startsWith('/') ? path : `/api/v1/${path}`;
    const response = await fetch(url, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const type = response.headers.get('content-type') || '';
    if (!type.includes('json')) throw new Error('Canvas did not return JSON.');
    return { data: await response.json(), link: response.headers.get('Link') || '' };
  }

  function canvasNextLink(header) {
    for (const part of String(header || '').split(',')) {
      const chunk = part.trim();
      if (!chunk.startsWith('<')) continue;
      const close = chunk.indexOf('>');
      if (close > 0 && /rel="next"/.test(chunk.slice(close))) return chunk.slice(1, close);
    }
    return '';
  }

  async function canvasList(path, params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (Array.isArray(value)) value.forEach(v => query.append(key, v));
      else query.append(key, value);
    }
    if (!query.has('per_page')) query.set('per_page', '100');

    let url = `/api/v1/${path}?${query.toString()}`;
    const merged = [];
    for (let page = 0; page < 12 && url; page++) {
      const { data, link } = await canvasJson(url);
      const rows = Array.isArray(data) ? data : [data];
      merged.push(...rows);
      url = canvasNextLink(link);
    }
    return merged;
  }

  async function isCanvas() {
    try {
      const { data } = await canvasJson('users/self');
      return !!(data && data.id);
    } catch (_) {
      return false;
    }
  }

  // The same paths the server asks for with a token. Keys match exactly, because
  // the server looks them up by path when the extension supplies the data.
  async function collectCanvas() {
    if (!(await isCanvas())) return { ok: false, error: 'This page is not Canvas.' };

    const pages = {};
    pages['courses'] = await canvasList('courses', {
      enrollment_state: 'active',
      'include[]': ['term', 'course_image'],
      'state[]': ['available']
    });

    for (const course of pages['courses']) {
      const id = course && course.id;
      if (!id) continue;
      const per = [
        [`courses/${id}/assignments`, { order_by: 'due_at' }],
        [`courses/${id}/assignment_groups`, { 'include[]': ['assignments', 'submission'] }],
        [`courses/${id}/discussion_topics`, { only_announcements: 'true' }],
        [`courses/${id}/modules`, { 'include[]': ['items'] }]
      ];
      for (const [path, params] of per) {
        try {
          pages[path] = await canvasList(path, params);
        } catch (_) {
          pages[path] = [];   // one unreadable course must not stop the rest
        }
      }
    }

    return { ok: true, base_url: location.origin, pages, courses: pages['courses'].length };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'NEXORA_CONTENT_PING') { sendResponse({ ok: true, version: '3.3.1' }); return false; }
    if (message?.type === 'NEXORA_CANVAS_COLLECT') {
      collectCanvas().then(sendResponse).catch(error =>
        sendResponse({ ok: false, error: error?.message || 'Could not read Canvas.' }));
      return true;
    }
    if (message?.type === 'NEXORA_SESSION_CHECK') { sessionCheck().then(sendResponse); return true; }
    if (message?.type === 'NEXORA_DISCOVER_COURSES') {
      // Every frame gets asked. A frame that finds nothing stays quiet so the
      // one holding the My Courses widget wins the reply; the top document
      // answers for failure, and waits longer than a subframe needs to succeed.
      const isTop = window === window.top;
      (async () => {
        // The API knows every enrolment. The page only knows the tab it is on.
        let fromApi = [];
        try {
          fromApi = await discoverCoursesFromApi(message.includeFinished === true);
        } catch (_) {
          fromApi = [];
        }

        // Only the term on screen, when the page says which one that is.
        const term = selectedTermLabel();
        if (term && fromApi.length) {
          const onlyThisTerm = fromApi.filter(c => c.term === term);
          if (onlyThisTerm.length) fromApi = onlyThisTerm;
        }

        if (fromApi.length) {
          // Cards on the page are real enrolments too. The API may have been
          // filtered, and it carries no images, so the page is merged in rather
          // than mined for pictures and discarded.
          const merged = new Map(fromApi.map(c => [c.source_uid, c]));
          for (const card of discoverCoursesFromDom()) {
            const existing = merged.get(card.source_uid);
            if (existing) {
              if (!existing.image_url && card.image_url) existing.image_url = card.image_url;
            } else if (!term || card.term === term) {
              // Cards from a term the student is not looking at would undo the
              // filter, so only this term's are added.
              merged.set(card.source_uid, card);
            }
          }
          return { ok: true, courses: [...merged.values()], term,
                   method: term ? `enrollments-api+dom (${term})` : 'enrollments-api+dom' };
        }

        // No API on this tenant: fall back to reading the page, which sees one
        // term at a time but is better than nothing.
        for (let i = 0; i < (isTop ? 14 : 8); i++) {
          const courses = discoverCoursesFromDom();
          if (courses.length) return { ok: true, courses, method: 'course-card-dom' };
          await sleep(600);
        }
        return null;
      })().then(result => {
        if (result) { sendResponse(result); return; }
        if (isTop) {
          sendResponse({ ok: false, courses: [], error: 'No course cards were found. Open the Brightspace My Courses page.' });
        }
        // a subframe with nothing to add says nothing at all
      });
      return true;
    }
    if (message?.type === 'NEXORA_SMART_SCAN_COURSE') {
      smartScanCourse(message.course || {}).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message, items: [] }));
      return true;
    }
  });
})();

// Gemini-requested course-outline extractor. Kept alongside Nexora Smart Sync so
// outline detection does not replace or weaken the existing course scanner.
chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request?.action !== 'extract_outline') return;
  try {
    const text = String(document.body?.innerText || '').replace(/\u0000/g, '').trim();
    sendResponse({ ok: true, text, url: location.href, title: document.title });
  } catch (error) {
    sendResponse({ ok: false, error: error?.message || 'Could not read this page.' });
  }
  return true;
});
