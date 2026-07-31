NEXORA SMART SYNC v1.2

Main fixes:
- Expands View More / Load More on the Brightspace My Courses page before discovery.
- Preserves course images when a later scan cannot see an image.
- Stores original topic URL, direct file URL, topic ID, and API version separately.
- Resolves Brightspace topic metadata and retries compatible API versions instead of trusting one stale /file URL.
- No longer exposes raw Brightspace 404 paths to students; provides a recovery message.
- Classifies Lecture Notes and slide decks as lectures even when titles do not contain a lecture number.
- Sends up to 8192 output tokens for full lecture explanations and instructs Gemini to teach the whole lecture.
- Lecture rows no longer redirect students to Brightspace when clicked.

Install both parts:
1. Deploy the website/backend from this project.
2. Remove the old unpacked Nexora Sync extension.
3. Load the extension folder from this project. Confirm version 1.2.0.
4. Refresh Brightspace, Detect & clean courses, then Scan every course now.
