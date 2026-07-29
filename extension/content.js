(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const absolute = href => { try { return new URL(href, location.href).href; } catch { return ''; } };

  function allRoots() {
    const roots = [document];
    const queue = [document.documentElement];
    const seen = new Set(queue);
    while (queue.length) {
      const node = queue.shift();
      if (!node || !node.querySelectorAll) continue;
      for (const el of node.querySelectorAll('*')) {
        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
          seen.add(el.shadowRoot);
          roots.push(el.shadowRoot);
          queue.push(el.shadowRoot);
        }
      }
    }
    return roots;
  }

  function queryAll(selector) {
    const found = [];
    const seen = new Set();
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

  function codeFromText(text) {
    const s = clean(text);
    const patterns = [
      /\b([A-Z]{3,6})\s*[-:]?\s*(\d{4}[A-Z]?)\b/i,
      /\b([A-Z]{3,6}\d{4}[A-Z]?)\b/i
    ];
    for (const p of patterns) {
      const m = s.match(p);
      if (m) return (m[2] ? `${m[1]}${m[2]}` : m[1]).toUpperCase();
    }
    return '';
  }

  function orgUnitIdFromUrl(url) {
    const patterns = [/\/d2l\/home\/(\d+)/i, /[?&]ou=(\d+)/i, /\/content\/(\d+)/i];
    for (const p of patterns) {
      const m = String(url || '').match(p);
      if (m) return m[1];
    }
    return '';
  }

  function imageFromContainer(container, anchor) {
    const candidates = [];
    if (container?.querySelectorAll) candidates.push(...container.querySelectorAll('img'));
    if (anchor?.querySelectorAll) candidates.push(...anchor.querySelectorAll('img'));
    for (const img of candidates) {
      const src = absolute(img.currentSrc || img.src || img.getAttribute('data-src'));
      if (src && !/avatar|icon|logo/i.test(src)) return src;
    }
    const nodes = container?.querySelectorAll ? [container, ...container.querySelectorAll('*')] : [];
    for (const node of nodes) {
      try {
        const bg = getComputedStyle(node).backgroundImage;
        const m = bg && bg !== 'none' ? bg.match(/url\(["']?(.*?)["']?\)/) : null;
        if (m) return absolute(m[1]);
      } catch (_) {}
    }
    return '';
  }

  function bestCourseContainer(anchor) {
    return closestAcrossShadow(anchor,
      'd2l-enrollment-card,d2l-card,article,li,[role="listitem"],[class*="enrollment"],[class*="course"],[class*="tile"],[class*="card"]'
    ) || anchor;
  }

  function candidateCourseAnchors() {
    const selectors = [
      'a[href*="/d2l/home/"]',
      'a[href*="/d2l/le/content/"][href*="ou="]',
      '[href*="/d2l/home/"]',
      'd2l-enrollment-card a',
      'd2l-card a'
    ];
    return queryAll(selectors.join(','));
  }

  function discoverCourses() {
    const courses = new Map();
    const anchors = candidateCourseAnchors();

    for (const anchor of anchors) {
      const rawHref = anchor.getAttribute?.('href') || anchor.href || '';
      const url = absolute(rawHref);
      const sourceUid = orgUnitIdFromUrl(url);
      if (!sourceUid || !/brightspace\.carleton\.ca|brightspace\.com/i.test(url)) continue;

      const card = bestCourseContainer(anchor);
      const texts = [
        anchor.getAttribute?.('aria-label'), anchor.getAttribute?.('title'),
        anchor.textContent, card?.getAttribute?.('aria-label'), card?.textContent,
        card?.shadowRoot?.textContent
      ].map(clean).filter(Boolean);
      const fullText = clean(texts.join(' '));
      if (!fullText) continue;

      const titleNode = card?.querySelector?.('h1,h2,h3,h4,[slot="title"],[class*="title"],[class*="name"]');
      let title = clean(titleNode?.textContent || anchor.getAttribute?.('aria-label') || anchor.textContent || fullText);
      title = title.replace(/\b(Open|Closed)\b/ig, '').replace(/\s+/g, ' ').trim();

      const code = codeFromText(fullText) || codeFromText(title) || `COURSE${sourceUid}`;
      const termMatch = fullText.match(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/i);
      const imageUrl = imageFromContainer(card, anchor);

      const existing = courses.get(sourceUid);
      const record = {
        source_uid: sourceUid,
        course_code: code,
        course_name: title || code,
        term: termMatch ? termMatch[0] : '',
        instructor: '',
        image_url: imageUrl,
        brightspace_url: `https://${location.host}/d2l/home/${sourceUid}`
      };
      if (!existing || record.course_name.length > existing.course_name.length || (!existing.image_url && record.image_url)) {
        courses.set(sourceUid, {...existing, ...record, image_url: record.image_url || existing?.image_url || ''});
      }
    }

    // Carleton's My Courses cards sometimes expose the org-unit ID on custom elements instead of anchors.
    for (const el of queryAll('[data-org-unit-id],[org-unit-id],[data-orgunitid],d2l-enrollment-card')) {
      const sourceUid = clean(el.getAttribute?.('data-org-unit-id') || el.getAttribute?.('org-unit-id') || el.getAttribute?.('data-orgunitid'));
      if (!/^\d+$/.test(sourceUid) || courses.has(sourceUid)) continue;
      const fullText = clean(`${el.getAttribute?.('aria-label') || ''} ${el.textContent || ''} ${el.shadowRoot?.textContent || ''}`);
      if (!fullText) continue;
      const code = codeFromText(fullText) || `COURSE${sourceUid}`;
      const termMatch = fullText.match(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/i);
      courses.set(sourceUid, {
        source_uid: sourceUid, course_code: code, course_name: fullText.slice(0, 500),
        term: termMatch ? termMatch[0] : '', instructor: '', image_url: imageFromContainer(el),
        brightspace_url: `https://${location.host}/d2l/home/${sourceUid}`
      });
    }

    return [...courses.values()];
  }

  async function scanAndReport() {
    const courses = discoverCourses();
    const diagnostics = {
      page: location.href,
      roots: allRoots().length,
      candidateAnchors: candidateCourseAnchors().length,
      found: courses.length,
      names: courses.map(c => `${c.course_code}: ${c.course_name}`).slice(0, 20)
    };
    try {
      await chrome.runtime.sendMessage({type: 'NEXORA_DISCOVERY_DIAGNOSTIC', diagnostics});
      if (courses.length) {
        const response = await chrome.runtime.sendMessage({type: 'NEXORA_COURSES', courses});
        return {ok: !!response?.ok, courses, response};
      }
      return {ok: false, courses: [], error: 'No courses found on this page.'};
    } catch (error) {
      return {ok: false, courses, error: error.message};
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'NEXORA_SCAN' || message?.type === 'NEXORA_SCAN_FULL') {
      scanAndReport().then(sendResponse);
      return true;
    }
  });

  // Do one quiet scan after Brightspace finishes rendering.
  setTimeout(scanAndReport, 4500);
})();
