# Gemini model compatibility fix v3.1

The previous build defaulted to `gemini-2.5-flash-lite`, which can return HTTP 404 for newer Gemini API projects.

This build:
- keeps Render environment model overrides,
- automatically falls back across current Gemini models,
- supports screenshot/image outline analysis with multimodal Gemini models,
- moves to the next model on 400/403/404 model-availability errors,
- retries transient 429/5xx errors,
- returns shorter, clearer error details.

Recommended Render values (optional):
- `GEMINI_MODEL=gemini-3.6-flash`
- `GEMINI_VISION_MODEL=gemini-3.5-flash-lite`
- `GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite`

The code also tries `gemini-flash-latest` and `gemini-flash-lite-latest` as final fallbacks.
