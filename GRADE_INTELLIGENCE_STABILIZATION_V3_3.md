# Grade Intelligence Stabilization v3.3

Implemented in this build:

- New outline analysis fully replaces the previous course outline calculator and grade entries.
- Multiple screenshots/pages are analyzed together (up to 8 files).
- Grade inputs accept `75%`, `15/20`, `15 out of 20`, or a plain score with a separate maximum.
- Each grade card shows the official course weight separately from the student's converted result.
- Added executable lab and non-lab minimum pass rules.
- Recent course content and announcements are sorted newest first using publication time when available.
- Existing Statics and MATH 1104 outline parser acceptance tests still pass locally.

Local acceptance checks:

- ECOR 1033: Midterm I 20%, Midterm II 30%, Final 50%.
- MATH 1104: four grading options and all pass gates.
- Synthetic PHYS rule: labs >= 50% and non-lab average >= 50%.
- Mark parsing: 15/20 and 75%.

Live Render/Gemini/Brightspace integration was not available in this environment and still requires deployment testing.
