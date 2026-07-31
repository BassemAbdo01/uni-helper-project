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
      window.postMessage({ channel: CHANNEL, type: 'EXTENSION_READY', version: '1.1.0' }, window.location.origin);
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

  window.postMessage({ channel: CHANNEL, type: 'EXTENSION_READY', version: '1.1.0' }, window.location.origin);
})();
