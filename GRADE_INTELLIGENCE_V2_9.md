# Nexora Grade Intelligence v2.9

Implemented:
- Multi-file outline analysis: upload up to 8 PDFs/pages/screenshots together.
- Combined interpretation of grading distribution, pass requirements, alternative schemes, and letter-grade requirements.
- Persistent per-user university grading scale in Settings.
- Numeric-to-letter conversion in every course outcome.
- Letter rules are scope-aware: prerequisite eligibility does not incorrectly fail the course.
- PNG/JPG/WEBP screenshot support remains available through OCR/Gemini Vision fallback.

Validation completed in this sandbox:
- Python compilation passed.
- Jinja syntax parsing passed for every template.
- Extension JavaScript syntax checks passed.

Not verified here:
- Live Render deployment, authenticated Brightspace sync, and live Gemini API calls.
