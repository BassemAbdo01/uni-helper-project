(() => {
  const CHANNEL = 'NEXORA_COURSE_INTELLIGENCE_V09';
  window.addEventListener('message', async event => {
    if (event.source !== window || event.data?.channel !== CHANNEL || event.data?.type !== 'FETCH_LECTURE') return;
    const requestId = event.data.requestId;
    try {
      const response = await chrome.runtime.sendMessage({ type: 'NEXORA_FETCH_AND_PROCESS_LECTURE', request: event.data.request });
      window.postMessage({ channel: CHANNEL, type: 'FETCH_LECTURE_RESULT', requestId, response }, window.location.origin);
    } catch (error) {
      window.postMessage({ channel: CHANNEL, type: 'FETCH_LECTURE_RESULT', requestId, response: { ok: false, error: error.message } }, window.location.origin);
    }
  });
  window.postMessage({ channel: CHANNEL, type: 'EXTENSION_READY' }, window.location.origin);
})();
