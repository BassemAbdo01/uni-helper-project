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

function show({ step, headline, detail, action, disabled = false, choices = false }) {
  $('step').textContent = step || '';
  $('step').style.visibility = step ? 'visible' : 'hidden';
  $('headline').textContent = headline;
  $('detail').textContent = detail;
  $('go').textContent = action;
  $('go').disabled = disabled;
  $('scope').hidden = !choices;
}

/* ---------------- which terms exist ----------------
   Asked of Brightspace itself rather than guessed, because only the enrolments
   API knows what a student has ever been registered in -- the page shows one
   term tab at a time. The date decides which of them is preselected: on the
   1st of September the answer is Fall, whatever tab happens to be mounted. */
let terms = [];
let termsLoaded = false;
let pollTimer = null;

function fillTermSelect(select, chosen) {
  if (!select) return;
  select.innerHTML = '';
  if (!terms.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = termsLoaded ? 'The term on screen' : 'Finding your terms…';
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const term of terms) {
    const option = document.createElement('option');
    option.value = term.label;
    option.textContent = `${term.label} · ${term.courses} course${term.courses === 1 ? '' : 's'}`;
    select.appendChild(option);
  }
  const wanted = terms.some(t => t.label === chosen) ? chosen : terms[0].label;
  select.value = wanted;
}

async function loadTerms(chosen) {
  let response = null;
  try { response = await chrome.runtime.sendMessage({ type: 'NEXORA_LIST_TERMS' }); }
  catch (_) { response = null; }
  termsLoaded = true;
  terms = Array.isArray(response?.terms) ? response.terms : [];
  // The current term by the calendar, falling back to the newest one found.
  const preferred = chosen
    || (terms.some(t => t.label === response?.current) ? response.current : '')
    || (terms[0]?.label || '');
  fillTermSelect($('scopeTerm'), preferred);
  fillTermSelect($('termSetting'), preferred);
  const hint = $('scopeTermHint');
  if (hint) {
    hint.textContent = terms.length
      ? 'The term you are enrolled in now, or pick another below.'
      : 'Nexora could not list your terms, so it will import the term your Brightspace is showing.';
  }
  return preferred;
}

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
  $('scopeSetting').value = status.config?.importScope === 'all' ? 'all' : 'term';
  $('termSetting').disabled = $('scopeSetting').value === 'all';
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
  if (onBrightspace) {
    const asked = Boolean(status.config?.importScope);
    const previous = new Date(status.lastSync || 0).getTime()
      ? new Date(status.lastSync).toLocaleString() : null;
    state = { kind: 'scope', origin: tabOrigin };
    show({
      step: asked ? '' : 'Step 2 of 2',
      headline: asked ? 'Import your courses' : 'What should Nexora import?',
      detail: previous
        ? `Last synced ${previous}. Changing this changes what Nexora keeps.`
        : 'Nexora remembers your answer; you can change it here any time.',
      action: previous ? 'Sync again' : 'Import my courses',
      choices: true
    });
    const wantsAll = status.config?.importScope === 'all';
    const radio = value => document.querySelector(`input[name="scope"][value="${value}"]`);
    if (radio('all')) radio('all').checked = wantsAll;
    if (radio('term')) radio('term').checked = !wantsAll;
    if (!termsLoaded) loadTerms(status.config?.importTerm);
    else fillTermSelect($('scopeTerm'), status.config?.importTerm);
    reportLastSync(status);
    return;
  }

  // 4. Ready.
  const last = status.lastSync ? new Date(status.lastSync).toLocaleString() : null;
  const scopeText = status.config?.importScope === 'all'
    ? 'every term'
    : (status.config?.importTerm || 'the term on screen');
  state = { kind: 'sync', origin: tabOrigin || status.config?.brightspaceOrigin };
  show({
    step: '',
    headline: 'Ready to sync',
    detail: last ? `Last synced ${last}. Importing ${scopeText}.` : `Bringing in ${scopeText}.`,
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

  if (state?.kind === 'scope') {
    const all = document.querySelector('input[name="scope"]:checked')?.value === 'all';
    await chrome.storage.sync.set({
      importScope: all ? 'all' : 'term',
      importTerm: all ? '' : ($('scopeTerm').value || ''),
      // Kept in step so a downgrade to 3.3 still imports what was asked for.
      includeFinished: all
    });
    await chrome.runtime.sendMessage({ type: 'NEXORA_CONFIG_CHANGED' });
    state = { kind: 'sync', origin: state.origin };
    $('scope').hidden = true;
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
  const all = $('scopeSetting').value === 'all';
  await chrome.storage.sync.set({
    apiBase: $('apiBase').value.trim() || DEFAULT_API_BASE,
    token: $('token').value.trim(),
    autoSyncEnabled: $('autoSync').checked,
    importScope: all ? 'all' : 'term',
    importTerm: all ? '' : ($('termSetting').value || ''),
    includeFinished: all,
    intervalMinutes: Number($('interval').value)
  });
  await chrome.runtime.sendMessage({ type: 'NEXORA_CONFIG_CHANGED' });
  say('Settings saved.', 'good');
  await evaluate();
};
$('autoSync').onchange = () => $('save').click();
$('scopeSetting').onchange = () => { $('termSetting').disabled = $('scopeSetting').value === 'all'; };
// Opening Advanced is the only moment the term list is worth fetching for it.
$('advanced').addEventListener('toggle', () => {
  if ($('advanced').open && !termsLoaded) loadTerms();
});

evaluate();
