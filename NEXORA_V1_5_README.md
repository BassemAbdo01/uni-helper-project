# Nexora Academic OS v1.5

## Main fixes

- Course Outline manual upload fallback (PDF/PPTX/DOCX/TXT/MD/CSV/HTML)
- Broader automatic outline detection in the Brightspace extension
- Outline parsing continues with a conservative built-in parser if Gemini is temporarily unavailable
- Grade calculator uses the detected course scheme total and final-exam minimum rules
- One-click target calculations for Pass, 70%, 80%, and 91%
- Manual lecture upload fallback when Brightspace blocks a file
- Flashcards, quizzes, summaries, and study guides always return usable output; a deterministic grounded fallback is used if Gemini is unavailable
- Human-readable study guide and summary rendering
- Extension version 1.5.0

## Install

1. Deploy the website project to Render.
2. Remove the old unpacked extension.
3. Load the `extension` folder through `chrome://extensions`.
4. Confirm version 1.5.0.
5. Re-enter the Nexora URL and extension key, then run course detection and Smart Sync.
