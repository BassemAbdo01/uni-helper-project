# Nexora Academic OS v1.4

## New in this build

- Course Outline Intelligence: opens/fetches the official outline, extracts the grading scheme, pass mark, final-exam minimum, and special pass rules.
- Course-specific Grade Intelligence: auto-creates weighted components from the outline, lets students enter their marks, calculates target outcomes, required final score, best/worst possible grades, and pass-rule outcomes.
- AI Study Tools: generate source-grounded flashcards, quizzes, summaries, and study guides from any synced lecture/resource without manual uploads.
- Smart Sync recommendations: newly synced lectures create proactive review recommendations; newly synced outlines prompt analysis.
- Gemini reliability: transient 429/5xx failures are retried; structured generation uses a configurable fallback model.
- Existing Smart Sync architecture remains metadata-first; source files are fetched only when needed.

## Render environment

Required:
- DATABASE_URL
- GEMINI_API_KEY

Recommended:
- GEMINI_MODEL=<your currently available preferred Gemini model>
- GEMINI_FALLBACK_MODEL=<a cheaper currently available fallback model>

## Upgrade steps

1. Deploy the whole project to Render.
2. Remove the previous unpacked Nexora Sync extension.
3. Load the new `extension` folder and confirm version 1.4.0.
4. Save the Nexora URL and extension key.
5. Run Detect & clean courses, then Scan every course now.
6. Open a course, use Course Outline > Analyze grading, then Grade Intelligence.
7. Use Study Tools to generate flashcards/quizzes from synced lectures.
