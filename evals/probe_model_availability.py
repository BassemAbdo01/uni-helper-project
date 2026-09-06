"""Which of these will this key actually answer with, and which 429?

The chain's top two are Pro-tier and the free key cannot serve them, so every
analysis lands on flash. But the key also lists gemini-3.7-flash and
gemini-3.8-flash, which the chain never tries. If those answer, there is an
accuracy gain available for no money at all.

Tiny prompts on purpose -- this is a capability probe, not a benchmark.
"""
import os, sys, json, time, urllib.request, urllib.error

sys.stdout.reconfigure(encoding='utf-8')
key = os.environ.get('GEMINI_API_KEY', '')
if not key:
    sys.exit('GEMINI_API_KEY not set -- source .env first')

CANDIDATES = [
    'gemini-pro-latest',
    'gemini-3.1-pro-preview',
    'gemini-3.8-flash',
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash-lite',
]

body = json.dumps({
    'contents': [{'parts': [{'text': 'Reply with the single word: ok'}]}],
    'generationConfig': {'maxOutputTokens': 800, 'temperature': 0},
}).encode()

for model in CANDIDATES:
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.load(r)
        finish = (payload.get('candidates') or [{}])[0].get('finishReason')
        usage = payload.get('usageMetadata') or {}
        print(f'  200  {model:<26} {time.time()-started:5.1f}s  finish={finish} '
              f'in={usage.get("promptTokenCount")} out={usage.get("candidatesTokenCount")}')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = (json.load(exc) or {}).get('error', {}).get('status', '')
        except Exception:
            pass
        print(f'  {exc.code}  {model:<26} {time.time()-started:5.1f}s  {detail}')
    except Exception as exc:
        print(f'  ERR  {model:<26} {type(exc).__name__}: {exc}')
