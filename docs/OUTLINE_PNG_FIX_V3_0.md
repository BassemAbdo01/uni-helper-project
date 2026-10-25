# Nexora Outline Upload Fix v3.0

## Root cause fixed
The live Gemini REST request for screenshots used snake_case fields (`inline_data`, `mime_type`). The Gemini generateContent REST API expects the canonical JSON fields `inlineData` and `mimeType`. This caused PNG/JPG uploads to fail whenever Render did not have a local Tesseract binary and the request fell back to Gemini Vision.

## Changes
- Correct Gemini multimodal REST payload (`inlineData` / `mimeType`).
- Correct text system instruction field (`systemInstruction`).
- Retry 429 and transient 5xx failures across configured primary/fallback models.
- Preserve detailed API errors in server logs and return a useful upload message.
- Keep local OCR as an independent fallback when Tesseract is available.
- Increased Flask request limit to 64 MB for multi-page screenshot uploads.

## Acceptance test completed locally
A PNG screenshot containing Midterm Exam I 20%, Midterm Exam II 30%, Final Exam 50%, and a C- prerequisite rule was OCR-read and parsed into exactly the three components totaling 100%.

## Still requires live verification
The authenticated Render + Gemini API path cannot be exercised from this sandbox because the deployed secret key and account session are not available here.
