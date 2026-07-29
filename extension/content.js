(() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const absolute = href => { try { return new URL(href, location.href).href; } catch { return ''; } };
  const codeFromText = text => {
    const m = clean(text).match(/\b([A-Z]{3,5})\s*-?\s*(\d{4}[A-Z]?)\b/i);
    return m ? `${m[1]}${m[2]}`.toUpperCase() : '';
  };
  const courseFromPage = () => {
    const candidates = [document.title, ...[...document.querySelectorAll('h1,h2,[class*=course],[class*=org-unit],[data-org-unit-name]')].slice(0,30).map(x => x.textContent || x.getAttribute('data-org-unit-name'))];
    for (const text of candidates) {
      const code = codeFromText(text);
      if (code) return code;
    }
    const urlMatch = location.pathname.match(/\/home\/(\d+)|\/content\/(\d+)/i);
    return urlMatch ? `COURSE${urlMatch[1] || urlMatch[2]}` : 'OTHER';
  };
  const inferType = (text, href='') => {
    const value = `${text} ${href}`.toLowerCase();
    if (/announcement|news/.test(value)) return 'announcement';
    if (/quiz|test/.test(value)) return 'quiz';
    if (/assignment|dropbox/.test(value)) return 'assignment';
    if (/lecture|week\s*\d+|module|lesson/.test(value)) return 'lecture';
    if (/\.pdf|\.pptx?|\.docx?|\.xlsx?|download/.test(value)) return 'file';
    if (/content|module|topic/.test(value)) return 'module';
    return 'resource';
  };

  const discoverCourses = () => {
    const courses = new Map();
    const anchors = [...document.querySelectorAll('a[href*="/d2l/home/"], a[href*="/home/"]')];
    for (const anchor of anchors) {
      const url = absolute(anchor.getAttribute('href'));
      if (!url || !/\/d2l\/home\/\d+|\/home\/\d+/i.test(url)) continue;
      const card = anchor.closest('article,li,[class*=course],[class*=tile],[class*=card],[role=listitem]') || anchor;
      const fullText = clean(card.textContent || anchor.textContent);
      const title = clean(anchor.getAttribute('aria-label') || anchor.title || anchor.textContent || card.querySelector('h2,h3,h4')?.textContent);
      const code = codeFromText(fullText) || codeFromText(title) || `COURSE${(url.match(/\/home\/(\d+)/i)||[])[1] || ''}`;
      const imageNode = card.querySelector('img') || anchor.querySelector('img');
      const bgNode = [...card.querySelectorAll('*')].find(el => getComputedStyle(el).backgroundImage && getComputedStyle(el).backgroundImage !== 'none');
      let imageUrl = imageNode ? absolute(imageNode.currentSrc || imageNode.src) : '';
      if (!imageUrl && bgNode) {
        const m = getComputedStyle(bgNode).backgroundImage.match(/url\(["']?(.*?)["']?\)/);
        imageUrl = m ? absolute(m[1]) : '';
      }
      const termMatch = fullText.match(/\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b/i);
      const sourceUid = (url.match(/\/home\/(\d+)/i)||[])[1] || url;
      courses.set(sourceUid, {
        source_uid: sourceUid,
        course_code: code || 'OTHER',
        course_name: title || fullText || code || 'Brightspace Course',
        term: termMatch ? termMatch[0] : '',
        instructor: '',
        image_url: imageUrl,
        brightspace_url: url
      });
    }
    return [...courses.values()];
  };

  const discoverSectionUrls = () => {
    const urls = new Set();
    const selectors = [
      'a[href*="/d2l/le/content/"]', 'a[href*="/d2l/lms/dropbox/"]',
      'a[href*="/d2l/lms/quizzing/"]', 'a[href*="/d2l/le/news/"]',
      'a[href*="/content/"]', 'a[href*="/dropbox/"]', 'a[href*="/quizzing/"]'
    ];
    document.querySelectorAll(selectors.join(',')).forEach(a => {
      const url = absolute(a.getAttribute('href'));
      if (url && url.startsWith(location.origin)) urls.add(url);
    });
    return [...urls].slice(0, 20);
  };

  const scanItems = () => {
    const course = courseFromPage();
    const items = new Map();
    const selectors = [
      'a[href*="/content/"]','a[href*="/d2l/le/content/"]','a[href*="/quizzing/"]',
      'a[href*="/dropbox/"]','a[href*="download"]','a[href$=".pdf"]','a[href$=".pptx"]',
      '[role="main"] a','main a'
    ];
    document.querySelectorAll(selectors.join(',')).forEach(node => {
      const title = clean(node.textContent || node.getAttribute('aria-label') || node.title);
      const url = absolute(node.getAttribute('href'));
      if (!title || title.length < 3 || !url || url.startsWith('javascript:')) return;
      if (/logout|profile|account settings|help/i.test(title)) return;
      const parent = node.closest('li,article,[class*=topic],[class*=module],[class*=content]');
      const moduleName = clean(parent?.querySelector('h2,h3,h4,[class*=title]')?.textContent || '');
      const sourceUid = node.dataset.topicId || node.dataset.objectId || node.id || url;
      items.set(sourceUid, {
        course, title, source_url: url, source_uid: sourceUid,
        content_type: inferType(title, url), module_name: moduleName,
        content_text: clean(parent?.textContent || '').slice(0,12000), published_at: ''
      });
    });
    document.querySelectorAll('[class*=announcement],[class*=news-item],article').forEach(node => {
      const title = clean(node.querySelector('h2,h3,h4,a')?.textContent);
      const body = clean(node.textContent);
      if (!title || body.length < 15 || !/announcement|news/i.test(`${node.className} ${body}`)) return;
      const key = `announcement:${course}:${title}`;
      items.set(key, {course,title,source_url:location.href,source_uid:key,content_type:'announcement',module_name:'Announcements',content_text:body.slice(0,12000),published_at:''});
    });
    return [...items.values()].slice(0,250);
  };

  const sendDiscovery = async () => {
    const courses = discoverCourses();
    const items = scanItems();
    const urls = discoverSectionUrls();
    try {
      if (courses.length) await chrome.runtime.sendMessage({type:'NEXORA_COURSES', courses});
      if (items.length) await chrome.runtime.sendMessage({type:'NEXORA_ITEMS', items});
      if (urls.length) await chrome.runtime.sendMessage({type:'NEXORA_DISCOVERED_URLS', urls});
      await chrome.runtime.sendMessage({type:'NEXORA_PAGE_SCANNED', url: location.href, course: courseFromPage(), itemCount: items.length});
    } catch (_) {}
  };

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'NEXORA_SCAN' || message?.type === 'NEXORA_SCAN_FULL') {
      sendDiscovery().then(() => sendResponse({ok:true}));
      return true;
    }
  });

  let timer;
  const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(sendDiscovery, 2500); });
  observer.observe(document.documentElement, {subtree:true, childList:true});
  setTimeout(sendDiscovery, 2500);
})();
