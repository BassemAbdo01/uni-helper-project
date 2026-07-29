# Nexora Sync v0.6 — Smart Sync

Smart Sync maps Brightspace course content through the student's existing signed-in browser session.

## What it maps
- Course cards and images
- Content modules and topics
- Lectures and slides
- Resources and code files
- Assignments
- Quizzes
- Announcements

## Cost-safe architecture
The extension sends metadata, titles, module paths, dates, and Brightspace source links to Nexora. It does not permanently copy every lecture file. A later on-demand reader can temporarily fetch a selected file for AI processing.

## Reliability design
- Brightspace session API first
- Multiple API-version fallbacks
- DOM fallback when a route is unavailable
- Never wipes existing course content after an empty or incomplete scan
- Detailed per-course diagnostics in the popup
- No repeated background course tabs

## Install
1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Remove/disable the previous Nexora Sync extension.
4. Click **Load unpacked** and select this `extension` folder.
5. Open Brightspace and stay signed in.
6. Save the Nexora URL and extension key.
7. Run **Detect & clean courses**, then **Scan every course now**.
