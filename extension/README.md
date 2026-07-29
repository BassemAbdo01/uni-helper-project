# Nexora Sync 0.3.0 — Carleton Course Discovery

This build focuses only on the first milestone: reliably detecting the course cards shown on Carleton Brightspace and importing those courses into Nexora.

## Update an existing unpacked installation
1. Extract this ZIP.
2. Open `chrome://extensions`.
3. Find **Nexora Sync** and click **Remove** (or replace the old extension folder and click Reload).
4. Click **Load unpacked** and select this ZIP's `extension` folder.
5. Open `https://brightspace.carleton.ca/d2l/home` and keep the My Courses cards visible.
6. Open Nexora Sync and click **Scan open Brightspace pages**.

The popup now reports:
- number of courses detected
- number of candidate course links
- Shadow DOM roots inspected
- course names/codes found
- server import counts

This build intentionally does not crawl lecture files yet. Course detection must work first.
