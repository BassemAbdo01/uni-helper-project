(() => {
  if (globalThis.__NEXORA_SITE_BRIDGE_V11__) return;
  globalThis.__NEXORA_SITE_BRIDGE_V11__ = true;
  const CHANNEL = 'NEXORA_COURSE_INTELLIGENCE_V10';
  const pending = new Map();

  async function extensionRequest(payload) {
    const response = await chrome.runtime.sendMessage(payload);
    if (!response?.ok) throw new Error(response?.error || 'Nexora Sync could not complete the request.');
    return response.result;
  }

  window.addEventListener('message', async event => {
    const data = event.data;
    if (event.source !== window || data?.channel !== CHANNEL) return;
    if (data.type === 'PING_EXTENSION') {
      window.postMessage({ channel: CHANNEL, type: 'EXTENSION_READY', version: '3.4.6' }, window.location.origin);
      return;
    }
    // Pairing: the page has the student's session, fetches their own key and
    // hands it over, so the key never appears on screen or gets pasted.
    if (data.type === 'PAIR') {
      try {
        const result = await extensionRequest({
          type: 'NEXORA_PAIR',
          token: data.token,
          apiBase: data.apiBase || window.location.origin
        });
        window.postMessage({ channel: CHANNEL, type: 'PAIR_RESULT', response: { ok: true, result } }, window.location.origin);
      } catch (error) {
        window.postMessage({ channel: CHANNEL, type: 'PAIR_RESULT', response: { ok: false, error: error?.message || 'Pairing failed.' } }, window.location.origin);
      }
      return;
    }
    if (data.type !== 'FETCH_LECTURE') return;
    const requestId = data.requestId;
    if (!requestId || pending.has(requestId)) return;
    pending.set(requestId, true);
    try {
      const result = await extensionRequest({ type: 'NEXORA_FETCH_AND_PROCESS_LECTURE', request: data.request });
      window.postMessage({ channel: CHANNEL, type: 'FETCH_LECTURE_RESULT', requestId, response: { ok: true, result } }, window.location.origin);
    } catch (error) {
      window.postMessage({ channel: CHANNEL, type: 'FETCH_LECTURE_RESULT', requestId, response: { ok: false, error: error?.message || 'Lecture fetch failed.' } }, window.location.origin);
    } finally {
      pending.delete(requestId);
    }
  });

  window.postMessage({ channel: CHANNEL, type: 'EXTENSION_READY', version: '3.4.6' }, window.location.origin);

  // Freshness matters when the student is actually looking at Nexora, and
  // almost never in between. The worker decides whether anything is stale
  // enough to be worth a sync; this only reports that someone is here.
  chrome.runtime.sendMessage({ type: 'NEXORA_VIEWING' }).catch(() => {});
})();

// Deliver a course outline captured from an LMS tab to the standalone Grade Intelligence page.
(async () => {
  if (!/\/grade-calculator\/?$/.test(location.pathname)) return;
  try {
    const stored = await chrome.storage.local.get(['pendingOutlineText']);
    if (!stored.pendingOutlineText) return;
    const deliver = () => window.postMessage({
      type: 'UNI_HELPER_COURSE_OUTLINE',
      text: stored.pendingOutlineText
    }, window.location.origin);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(deliver, 100));
    else setTimeout(deliver, 100);
    await chrome.storage.local.remove(['pendingOutlineText','pendingOutlineTitle','pendingOutlineSource']);
  } catch (_) {}
})();
