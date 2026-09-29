/* The popup is a state machine with exactly one primary button.
   A student should never have to work out which of five buttons comes first. */

const $ = id => document.getElementById(id);
const D2L_PATH = /\/d2l\//i;
// Canvas has no single address: canvas.school.edu, school.instructure.com, or a
// campus domain serving /courses. This only decides whether to offer the button;
// the page itself is asked before anything is read, and says no if it is not Canvas.
const CANVAS_HINT = /(^|\.)instructure\.com$|canvas/i;
const COURSES_PATH = /\/courses(\/|$)/i;

function looksLikeCanvas(url) {
  try {
    const parsed = new URL(String(url || ''));
    if (parsed.protocol !== 'https:') return false;
    return CANVAS_HINT.test(parsed.hostname) || COURSES_PATH.test(parsed.pathname);
  } catch (_) {
    return false;
  }
}

async function originGranted(origin) {
  if (!origin) return false;
  try { return await chrome.permissions.contains({ origins: [`${origin}/*`] }); }
  catch (_) { return false; }
}
const DEFAULT_API_BASE = 'https://uni-helper-project.onrender.com';

let state = null;   // {kind, origin, apiBase}

const originOf = url => { try { return new URL(String(url || '')).origin; } catch (_) { return ''; } };

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function show({ step, headline, detail, action, disabled = false }) {
  $('step').textContent = step || '';
  $('step').style.visibility = step ? 'visible' : 'hidden';
  $('headline').textContent = headline;
  $('detail').textContent = detail;
  $('go').textContent = action;
  $('go').disabled = disabled;
}

/* Nexora imports every term, every time, so there is no term to choose and
   nothing to list. The question used to live here: a student answered it once
   and the answer stuck, so a choice made in first year kept later terms out of
   the app -- silently, because a sync that imports nothing looks exactly like
   one that worked. */
let pollTimer = null;

function say(text, kind = '') {
  const box = $('result');
  box.className = `result on ${kind}`;
  box.textContent = text;
}
function clearSay() { $('result').className = 'result'; }
function busy(on) { $('bar').className = on ? 'bar on' : 'bar'; $('go').disabled = on; }

function reportLastSync(status) {
  const summary = status.syncStatus?.contentSummary;
  if (summary?.courses !== undefined) {
    say(`${summary.courses} courses · ${summary.itemsFound || 0} items in Nexora`, 'good');
  }
  if (status.syncStatus?.connectionExpired) {
    say('Your Brightspace session expired. Open Brightspace, sign in again, then sync.', 'bad');
  }
}

/* ---------------- work out what the student should do next ---------------- */
async function evaluate() {
  const status = await chrome.runtime.sendMessage({ type: 'NEXORA_STATUS' }).catch(() => null);
  if (!status) {
    show({ headline: 'Extension not responding', detail: 'Reload the extension from chrome://extensions.', action: 'Retry' });
    state = { kind: 'retry' };
    return;
  }

  $('apiBase').value = status.config?.apiBase || '';
  $('token').value = status.config?.token || '';
  $('autoSync').checked = status.config?.autoSyncEnabled !== false;
  $('interval').value = String(status.config?.intervalMinutes || 30);

  // A sync in flight is the whole answer: the work runs in the background and
  // survives this popup closing, so the one thing this screen must not do is
  // go back to saying "Ready to sync" and imply it stopped.
  clearTimeout(pollTimer);
  if (status.syncStatus?.running) {
    const progress = status.syncStatus;
    const where = progress.currentCourse
      ? `${progress.currentCourse}${progress.progress ? ` (${progress.progress})` : ''}`
      : (progress.phase || 'working').replace(/-/g, ' ');
    state = { kind: 'running' };
    show({
      step: '',
      headline: 'Syncing now',
      detail: `${where}. Close this whenever you like \u2014 it keeps going.`,
      action: 'Syncing\u2026',
      disabled: true
    });
    pollTimer = setTimeout(evaluate, 1500);
    return;
  }

  const apiBase = status.config?.apiBase || DEFAULT_API_BASE;
  const tab = await activeTab();
  const onBrightspace = D2L_PATH.test(tab?.url || '');
  const tabOrigin = originOf(tab?.url);

  // 1. Not linked to a Nexora account yet.
  if (!status.configured) {
    state = { kind: 'pair', apiBase };
    show({
      step: 'Step 1 of 2',
      headline: 'Connect to Nexora',
      detail: 'Opens Nexora and links this extension to your account. Nothing to copy or type.',
      action: 'Connect to Nexora'
    });
    return;
  }

  // 2. Sitting on Canvas. Worth offering even when a Brightspace is already
  //    known, because a student can be enrolled in both.
  if (!onBrightspace && looksLikeCanvas(tab?.url)) {
    state = {
      kind: 'canvas',
      origin: tabOrigin,
      needsPermission: !(await originGranted(tabOrigin))
    };
    show({
      step: '',
      headline: 'Import from Canvas',
      detail: state.needsPermission
        ? `Chrome will ask permission for ${tabOrigin.replace(/^https?:\/\//, '')} first. No access token needed.`
        : 'Reads your courses through the Canvas session you are already signed into.',
      action: 'Import my courses'
    });
    return;
  }

  // 3. Linked, but no LMS has been seen yet.
  if (!onBrightspace && !status.config?.brightspaceOrigin) {
    state = { kind: 'needs-lms' };
    show({
      step: 'Step 2 of 2',
      headline: 'Open your LMS',
      detail: "Sign in to Brightspace or Canvas in a tab, then come back here. Any university works.",
      action: 'Waiting for Brightspace or Canvas…',
      disabled: true
    });
    return;
  }

  // 3. On a Brightspace page we do not have permission for yet. Syncing will
  //    ask for it, so this is still one button.
  if (onBrightspace && tabOrigin && !status.originGranted) {
    state = { kind: 'sync', origin: tabOrigin, needsPermission: true };
    show({
      step: 'Step 2 of 2',
      headline: 'Import your courses',
      detail: `Chrome will ask permission for ${tabOrigin.replace(/^https?:\/\//, '')} first.`,
      action: 'Import my courses'
    });
    return;
  }

  // 4. On Brightspace with permission granted. The question is put every
  //    time, not only before the first import: a student who chose one term
  //    and then wanted everything had nowhere to say so, because this screen
  //    had already been replaced by "Ready to sync".
  // 4. On Brightspace with permission granted, and 5. ready elsewhere are now
  //    the same screen: there is nothing left to ask.
  const last = status.lastSync ? new Date(status.lastSync).toLocaleString() : null;
  state = { kind: 'sync', origin: tabOrigin || status.config?.brightspaceOrigin };
  show({
    step: '',
    headline: 'Ready to sync',
    detail: last
      ? `Last synced ${last}. Importing every course, every term.`
      : 'Bringing in every course, every term.',
    action: last ? 'Sync again' : 'Import my courses'
  });
  reportLastSync(status);
}

/* ---------------- the one button ---------------- */
$('go').onclick = async () => {
  clearSay();

  if (state?.kind === 'retry') { await evaluate(); return; }

  if (state?.kind === 'pair') {
    // The website hands the key straight to the extension over the bridge.
    await chrome.tabs.create({ url: `${state.apiBase}/connect-brightspace?pair=1` });
    window.close();
    return;
  }

  if (state?.kind === 'canvas') {
    // Chrome only grants an optional permission from a user gesture, which is
    // this click.
    if (state.needsPermission && state.origin) {
      // Same as the Brightspace path: the prompt closes this popup, so the
      // worker has to be able to finish without it.
      await chrome.storage.local.set({
        pendingImport: { kind: 'canvas', origin: state.origin, at: Date.now() }
      });
      busy(true);
      say('Waiting for Chrome to grant access to your Canvas…');
      let granted = false;
      try { granted = await chrome.permissions.request({ origins: [`${state.origin}/*`] }); }
      catch (error) {
        await chrome.storage.local.remove('pendingImport');
        busy(false);
        say(`Chrome refused the permission request: ${error?.message || error}`, 'bad');
        return;
      }
      if (!granted) {
        await chrome.storage.local.remove('pendingImport');
        busy(false);
        say('Without permission for that page Nexora cannot read your Canvas.', 'bad');
        return;
      }
      await chrome.storage.local.remove('pendingImport');
    }

    busy(true);
    say('Reading your Canvas courses…');
    try {
      const response = await chrome.runtime.sendMessage({ type: 'NEXORA_CANVAS_SESSION_SYNC' });
      if (!response?.ok) throw new Error(response?.error || 'Could not read Canvas.');
      const n = response.result?.courses ?? 0;
      say(`Done. ${n} course${n === 1 ? '' : 's'} imported from Canvas.`, 'good');
    } catch (error) {
      say(error?.message || 'Could not read Canvas.', 'bad');
    } finally {
      busy(false);
      await evaluate();
    }
    return;
  }

  if (state?.kind !== 'sync') return;

  // Asking for the university's domain has to happen on this click: Chrome
  // only grants an optional permission from a user gesture.
  if (state.needsPermission && state.origin) {
    // Chrome usually destroys this popup the moment the prompt appears, so the
    // line after the await may never run. Write the intent down first and the
    // service worker's permissions.onAdded finishes the import -- otherwise
    // pressing Allow appeared to do nothing at all.
    await chrome.storage.local.set({
      pendingImport: { kind: 'brightspace', origin: state.origin, at: Date.now() }
    });
    busy(true);
    say('Waiting for Chrome to grant access to your university…');
    let granted = false;
    try {
      granted = await chrome.permissions.request({ origins: [`${state.origin}/*`] });
    } catch (error) {
      await chrome.storage.local.remove('pendingImport');
      busy(false);
      say(`Chrome refused the permission request: ${error?.message || error}`, 'bad');
      return;
    }
    if (!granted) {
      await chrome.storage.local.remove('pendingImport');
      busy(false);
      say('Without permission for that page Nexora cannot read your courses.', 'bad');
      return;
    }
    // This popup survived, so take the job back off the worker and run it here
    // where the student can watch it.
    await chrome.storage.local.remove('pendingImport');
    await chrome.runtime.sendMessage({ type: 'NEXORA_SET_ORIGIN', origin: state.origin });
  }

  busy(true);
  say('Finding your courses…');
  try {
    const result = await chrome.runtime.sendMessage({ type: 'NEXORA_SYNC_EVERYTHING' });
    if (!result?.ok) throw new Error(result?.error || 'Sync failed.');
    // The count the dashboard will show, not the count discovered on the page:
    // those differ whenever two sections share a course code or a course was
    // removed, and two numbers for one thing reads as a bug.
    const d = result.discovery?.courseDiscovery || result.discovery || {};
    const courses = d.stored ?? d.found ?? 0;
    const items = result.summary?.itemsFound;
    const term = result.discovery?.term;
    const dropped = d.removed || 0;
    say(`Done. ${courses} course${courses === 1 ? '' : 's'}${term ? ` from ${term}` : ''}`
      + `${items !== undefined ? ` \u00b7 ${items} items imported` : ''}.`
      + (dropped ? `\n${dropped} course${dropped === 1 ? '' : 's'} outside that choice removed.` : ''), 'good');
  } catch (error) {
    say(error?.message || 'Sync failed.', 'bad');
  } finally {
    busy(false);
    await evaluate();
  }
};

/* ---------------- secondary: outline on the current page ---------------- */
$('outline').onclick = async () => {
  clearSay();
  say('Reading this page for a course outline…');
  try {
    const tab = await activeTab();
    if (!tab?.id) throw new Error('No active tab was found.');

    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    } catch (_) {
      // Clicking the toolbar icon grants activeTab, so this reaches any page.
      // classify.js first: content.js calls classifyContent, which now lives
      // there so the worker and the injected script cannot drift apart.
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['classify.js', 'content.js'] });
      response = await chrome.tabs.sendMessage(tab.id, { action: 'extract_outline' });
    }
    if (!response?.ok || !response.text) {
      throw new Error(response?.error || 'No outline text was found on this page.');
    }

    await chrome.storage.local.set({
      pendingOutlineText: response.text,
      pendingOutlineTitle: response.title || 'Detected course outline',
      pendingOutlineSource: response.url || tab.url || ''
    });

    const cfg = await chrome.storage.sync.get(['apiBase']);
    const base = String(cfg.apiBase || DEFAULT_API_BASE).replace(/\/$/, '');
    await chrome.tabs.create({ url: `${base}/grade-calculator` });
    window.close();
  } catch (error) {
    say(error?.message || String(error), 'bad');
  }
};

/* ---------------- advanced ---------------- */
$('save').onclick = async () => {
  await chrome.storage.sync.set({
    apiBase: $('apiBase').value.trim() || DEFAULT_API_BASE,
    token: $('token').value.trim(),
    autoSyncEnabled: $('autoSync').checked,
    // Written every save so an account carrying an old one-term choice is
    // repaired the first time anything is saved, not only on a fresh install.
    importScope: 'all',
    importTerm: '',
    includeFinished: true,
    intervalMinutes: Number($('interval').value)
  });
  await chrome.runtime.sendMessage({ type: 'NEXORA_CONFIG_CHANGED' });
  say('Settings saved.', 'good');
  await evaluate();
};
$('autoSync').onchange = () => $('save').click();
// Opening Advanced is the only moment the term list is worth fetching for it.
$('advanced').addEventListener('toggle', () => {
});

evaluate();
