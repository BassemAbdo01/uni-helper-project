(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const absolute = href => { try { return new URL(href, location.href).href; } catch { return ''; } };

  function allRoots() {
    const roots = [document];
    const queue = [document.documentElement];
    const seen = new Set(queue);
    while (queue.length) {
      const node = queue.shift();
      if (!node?.querySelectorAll) continue;
      for (const el of node.querySelectorAll('*')) {
        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
          seen.add(el.shadowRoot); roots.push(el.shadowRoot); queue.push(el.shadowRoot);
        }
      }
    }
    return roots;
  }

  function queryAll(selector) {
    const found = [], seen = new Set();
    for (const root of allRoots()) {
      try {
        for (const el of root.querySelectorAll(selector)) {
          if (!seen.has(el)) { seen.add(el); found.push(el); }
        }
      } catch (_) {}
    }
    return found;
  }

  function closestAcrossShadow(el, selector) {
    let node = el;
    while (node) {
      if (node.matches?.(selector)) return node;
      node = node.parentElement || node.getRootNode?.()?.host || null;
    }
    return null;
  }

  function orgUnitIdFromUrl(url) {
    for (const p of [/\/d2l\/home\/(\d+)/i, /[?&]ou=(\d+)/i, /\/content\/(\d+)/i, /\/lessons\/(\d+)/i]) {
      const m = String(url || '').match(p); if (m) return m[1];
    }
    return '';
  }

  function parseCourse(rawTitle, sourceUid) {
    const original = clean(rawTitle).replace(/\b(Open|Closed)\b/ig, '').trim();
    const termMatch = original.match(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/i);
    const term = termMatch ? termMatch[0].replace(/^./, c => c.toUpperCase()) : '';
    const codeMatches = [...original.matchAll(/\b([A-Z]{3,6})\s*[-:]?\s*(\d{4}[A-Z]?)\b/ig)];
    const code = codeMatches.length ? `${codeMatches[0][1]}${codeMatches[0][2]}`.toUpperCase() : `COURSE${sourceUid}`;

    let name = original
      .replace(/^\s*(Merge|Crosslist)\s+/i, '')
      .replace(new RegExp(`^${code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*[-:]?\\s*`, 'i'), '')
      .replace(/\[[\d:\s,]+\]/g, ' ')
      .replace(/\bXLS[A-Z0-9_-]+\b/ig, ' ')
      .replace(/\bSP[_-]?\d+\b/ig, ' ')
      .replace(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/ig, ' ')
      .replace(/\((LEC|LAB|TUT|SEM|DIS|PRA)(?:\s+[^)]*)?\)/ig, ' ')
      .replace(/\b[A-Z]{3,6}\d{4}[A-Z]?\s*:\s*[A-Z]{3,6}\d{4}[A-Z]?\b/ig, ' ')
      .replace(/^[\s,._-]+|[\s,._-]+$/g, '')
      .replace(/\s+/g, ' ')
      .trim();

    if (!name || name.length < 3) name = code;
    if (name.length > 180) name = name.slice(0, 180).replace(/\s+\S*$/, '');
    return {code, name, term, original};
  }

  function imageFromContainer(container) {
    for (const img of container?.querySelectorAll?.('img') || []) {
      const src = absolute(img.currentSrc || img.src || img.getAttribute('data-src'));
      if (src && !/avatar|icon|logo/i.test(src)) return src;
    }
    for (const node of container?.querySelectorAll ? [container, ...container.querySelectorAll('*')] : []) {
      try {
        const bg = getComputedStyle(node).backgroundImage;
        const m = bg && bg !== 'none' ? bg.match(/url\(["']?(.*?)["']?\)/) : null;
        if (m) return absolute(m[1]);
      } catch (_) {}
    }
    return '';
  }

  function discoverCourses() {
    const courses = new Map();
    const anchors = queryAll('a[href*="/d2l/home/"],a[href*="/d2l/le/content/"][href*="ou="],[href*="/d2l/home/"]');
    for (const anchor of anchors) {
      const url = absolute(anchor.getAttribute?.('href') || anchor.href || '');
      const sourceUid = orgUnitIdFromUrl(url);
      if (!sourceUid) continue;
      const card = closestAcrossShadow(anchor, 'd2l-enrollment-card,d2l-card,article,li,[role="listitem"],[class*="enrollment"],[class*="course"],[class*="tile"],[class*="card"]') || anchor;
      const raw = clean(`${card.getAttribute?.('aria-label') || ''} ${card.textContent || ''} ${card.shadowRoot?.textContent || ''}`);
      if (!raw) continue;
      const parsed = parseCourse(raw, sourceUid);
      const record = {
        source_uid: sourceUid,
        course_code: parsed.code,
        course_name: parsed.name,
        raw_course_name: parsed.original,
        term: parsed.term,
        instructor: '',
        image_url: imageFromContainer(card),
        brightspace_url: `${location.origin}/d2l/home/${sourceUid}`
      };
      const existing = courses.get(sourceUid);
      if (!existing || (!existing.image_url && record.image_url) || record.course_name.length < existing.course_name.length) courses.set(sourceUid, {...existing, ...record});
    }
    return [...courses.values()];
  }

  function pageKind() {
    const u = location.href.toLowerCase();
    if (/dropbox|assignment/.test(u)) return 'assignments';
    if (/quizzing|quiz/.test(u)) return 'quizzes';
    if (/\/news\//.test(u) || /news\/main/.test(u)) return 'announcements';
    if (/lessons|\/content\//.test(u)) return 'content';
    return 'course-home';
  }

  function classify(title, url, kind) {
    const s = `${title} ${url}`.toLowerCase();
    if (kind === 'assignments' || /dropbox|assignment/.test(s)) return 'assignment';
    if (kind === 'quizzes' || /quizzing|quiz/.test(s)) return 'quiz';
    if (kind === 'announcements' || /announcement|\/news\//.test(s)) return 'announcement';
    if (/\.(pdf|ppt|pptx|doc|docx|xls|xlsx|csv|zip)(?:$|[?#])/i.test(url)) return 'file';
    if (/lecture|slides?|week\s*\d+|module\s*\d+|chapter\s*\d+|lesson\s*\d+/i.test(title)) return 'lecture';
    if (/\/lessons\/|\/content\//.test(url)) return 'page';
    return 'resource';
  }

  function sourceUid(url, title, courseUid) {
    try {
      const u = new URL(url);
      const keys = ['topicId', 'moduleId', 'folderId', 'qi', 'ou'];
      const parts = keys.map(k => u.searchParams.get(k)).filter(Boolean);
      const pathId = (u.pathname.match(/\/(\d+)(?:\/|$)/g) || []).join('');
      return clean(`${courseUid}|${parts.join('|')}|${pathId}|${u.pathname}`) || `${courseUid}|${title}|${url}`;
    } catch { return `${courseUid}|${title}|${url}`; }
  }

  function nearbyModule(el) {
    const container = closestAcrossShadow(el, 'li,article,[role="listitem"],[class*="topic"],[class*="module"],[class*="lesson"],[class*="content"]');
    const heading = container?.querySelector?.('h1,h2,h3,h4,h5,[class*="module-title"],[class*="heading"]');
    return clean(heading?.textContent || '');
  }

  function scanCoursePage(course) {
    const kind = pageKind();
    const items = new Map();
    const selectors = 'a[href],button[data-href],[role="link"][href],d2l-link[href]';
    for (const el of queryAll(selectors)) {
      const rawHref = el.getAttribute?.('href') || el.getAttribute?.('data-href') || '';
      const url = absolute(rawHref);
      if (!url || !url.startsWith(location.origin)) continue;
      let title = clean(el.getAttribute?.('aria-label') || el.getAttribute?.('title') || el.textContent || '');
      if (!title || title.length < 2) continue;
      if (/^(home|content|course home|grades|classlist|calendar|help|logout|notifications?)$/i.test(title)) continue;
      if (/\/d2l\/home\/?$/.test(url)) continue;
      const contentType = classify(title, url, kind);
      if (contentType === 'resource' && title.length < 4) continue;
      const uid = sourceUid(url, title, course.source_uid);
      items.set(uid, {
        course: course.course_code,
        source_uid: uid,
        title: title.slice(0, 500),
        content_type: contentType,
        source_url: url,
        module_name: nearbyModule(el),
        content_text: '',
        published_at: ''
      });
    }

    // Capture visible announcement/assignment/quiz cards that are not normal anchors.
    if (kind !== 'content') {
      for (const el of queryAll('article,li,[role="listitem"],[class*="announcement"],[class*="assignment"],[class*="quiz"]')) {
        const title = clean(el.querySelector?.('h1,h2,h3,h4,a,[class*="title"]')?.textContent || '');
        if (!title || title.length < 3 || title.length > 300) continue;
        const link = el.querySelector?.('a[href]');
        const url = absolute(link?.getAttribute('href') || location.href);
        const contentType = classify(title, url, kind);
        const uid = sourceUid(url, title, course.source_uid);
        if (!items.has(uid)) items.set(uid, {course: course.course_code, source_uid: uid, title, content_type: contentType, source_url: url, module_name: '', content_text: '', published_at: ''});
      }
    }

    return {ok: true, page: location.href, pageKind: kind, roots: allRoots().length, items: [...items.values()]};
  }

  async function scanHome() {
    const courses = discoverCourses();
    if (!courses.length) return {ok: false, courses: [], error: 'No course cards found on this page.'};
    const response = await chrome.runtime.sendMessage({type: 'NEXORA_COURSES', courses});
    return {ok: !!response?.ok, courses, response};
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'NEXORA_SCAN_HOME' || message?.type === 'NEXORA_SCAN_FULL') {
      scanHome().then(sendResponse); return true;
    }
    if (message?.type === 'NEXORA_SCAN_COURSE_PAGE') {
      sendResponse(scanCoursePage(message.course || {})); return false;
    }
  });

  if (/\/d2l\/home\/?(?:$|[?#])/.test(location.href)) setTimeout(() => scanHome().catch(() => {}), 4500);
})();
