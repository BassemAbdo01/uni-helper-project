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

function say(text, kind = '') {
  const box = $('result');
  box.className = `result on ${kind}`;
  box.textContent = text;
}
function clearSay() { $('result').className = 'result'; }
function busy(on) { $('bar').className = on ? 'bar on' : 'bar'; $('go').disabled = on; }

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
  $('includeFinished').checked = status.config?.includeFinished === true;
  $('interval').value = String(status.config?.intervalMinutes || 30);

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

  // 4. Ready.
  const last = status.lastSync ? new Date(status.lastSync).toLocaleString() : null;
  const summary = status.syncStatus?.contentSummary;
  state = { kind: 'sync', origin: tabOrigin || status.config?.brightspaceOrigin };
  show({
    step: '',
    headline: 'Ready to sync',
    detail: last ? `Last synced ${last}.` : 'Bring your courses, deadlines and lectures into Nexora.',
    action: last ? 'Sync again' : 'Import my courses'
  });
  if (summary?.courses !== undefined) {
    say(`${summary.courses} courses · ${summary.itemsFound || 0} items in Nexora`, 'good');
  }
  if (status.syncStatus?.connectionExpired) {
    say('Your Brightspace session expired. Open Brightspace, sign in again, then sync.', 'bad');
  }
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
      let granted = false;
      try { granted = await chrome.permissions.request({ origins: [`${state.origin}/*`] }); }
      catch (error) {
        say(`Chrome refused the permission request: ${error?.message || error}`, 'bad');
        return;
      }
      if (!granted) {
        say('Without permission for that page Nexora cannot read your Canvas.', 'bad');
        return;
      }
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
    let granted = false;
    try {
      granted = await chrome.permissions.request({ origins: [`${state.origin}/*`] });
    } catch (error) {
      say(`Chrome refused the permission request: ${error?.message || error}`, 'bad');
      return;
    }
    if (!granted) {
      say('Without permission for that page Nexora cannot read your courses.', 'bad');
      return;
    }
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
    say(`Done. ${courses} course${courses === 1 ? '' : 's'}${term ? ` from ${term}` : ''}`
      + `${items !== undefined ? ` · ${items} items imported` : ''}.`, 'good');
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
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
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
    includeFinished: $('includeFinished').checked,
    intervalMinutes: Number($('interval').value)
  });
  await chrome.runtime.sendMessage({ type: 'NEXORA_CONFIG_CHANGED' });
  say('Settings saved.', 'good');
  await evaluate();
};
$('autoSync').onchange = () => $('save').click();
$('includeFinished').onchange = () => $('save').click();

evaluate();
